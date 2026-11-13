"""Test Suite: S-8 / S-9 Credits and Idempotency Engine.

Validates:
1. S-8: Insufficient credits returns 402 with zero side effects.
2. S-8: Credit ledger reconciliation (ref_type='share_event', ref_id, balance_after) across hydrate and rollback.
3. S-9: Caller-supplied idempotency key validation (format check).
4. S-9: Reusing an idempotency key for a different snapshot returns 409 idempotency_key_reuse.
5. S-9: Double-click retry returns identical stored result with only one credit debit.
"""

from datetime import datetime, timezone
import random
import uuid
import pytest
from sqlalchemy import func, text
from fastapi.testclient import TestClient

from api import app
import auth
from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.credit_ledger import CreditLedger
from db.models.platform_catalog import Company, CompanyIdentifier, ShareEvent, Snapshot, SnapshotBlob
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from services.account_reuse_service import AccountReuseService

TEST_CREDIT_SLUG = "test_credit_tenant_001"
TEST_CREDIT_SCHEMA = f"client_{TEST_CREDIT_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_credit_env():
    session = SessionLocal()
    try:
        session.execute(text(f"DELETE FROM public.clients WHERE slug = '{TEST_CREDIT_SLUG}'"))
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'credit_%@corp.com'"))
        session.commit()

        create_client_schema(TEST_CREDIT_SLUG)

        client = session.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        client.status = "active"
        client.catalog_publish = True
        client.catalog_consume = True
        client.reuse_requires_approval = False
        client.api_credits_total = 100
        client.api_credits_used = 0

        # Users
        pwd = auth.hash_password("testpass123")
        u_super = session.query(User).filter_by(role="super_admin").first()
        if not u_super:
            u_super = User(email="credit_super@corp.com", hashed_password=pwd, role="super_admin", full_name="Super Admin")
            session.add(u_super)
            session.flush()

        u_admin = session.query(User).filter_by(email="credit_admin@corp.com").first()
        if not u_admin:
            u_admin = User(email="credit_admin@corp.com", hashed_password=pwd, role="user", full_name="Client Admin")
            session.add(u_admin)
            session.flush()

        access = session.query(UserClientAccess).filter_by(user_id=u_admin.id, client_id=client.id).first()
        if not access:
            session.add(UserClientAccess(user_id=u_admin.id, client_id=client.id, role="client_admin"))

        session.commit()
    finally:
        session.close()

    yield

    engine.dispose()
    drop_client_schema(TEST_CREDIT_SLUG)
    clean_session = SessionLocal()
    try:
        clean_session.execute(text(f"DELETE FROM public.clients WHERE slug = '{TEST_CREDIT_SLUG}'"))
        clean_session.execute(text("DELETE FROM public.users WHERE email LIKE 'credit_%@corp.com'"))
        clean_session.commit()
    finally:
        clean_session.close()


def _get_admin_headers():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(email="credit_admin@corp.com").first()
        token = auth.create_access_token(user, client_slug=TEST_CREDIT_SLUG)
        return {"Authorization": f"Bearer {token}", "X-Client-Slug": TEST_CREDIT_SLUG}, user.id
    finally:
        session.close()


def _get_super_headers():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(role="super_admin").first()
        token = auth.create_access_token(user)
        return {"Authorization": f"Bearer {token}"}, user.id
    finally:
        session.close()


def _create_test_snapshot(session, domain: str, cik: str) -> Snapshot:
    """Helper to create a canonical company and ready snapshot."""
    company = Company(
        canonical_key=f"cik:{cik}",
        legal_name=f"Enterprise {domain}",
        display_name=f"Enterprise {domain}",
    )
    session.add(company)
    session.flush()

    session.add(CompanyIdentifier(company_id=company.id, id_type="cik", id_value=cik))
    session.add(CompanyIdentifier(company_id=company.id, id_type="domain", id_value=domain))

    snap = Snapshot(
        company_id=company.id,
        version=1,
        status="ready",
        source_client_id=1,
        source_account_id=1,
        policy_version="1.0.0",
        skeleton_version="T0002",
        quality_score=90.0,
        quality_grade="verified",
        checksum=f"chk_{uuid.uuid4().hex[:12]}",
        checksum_version=2,
        taken_at=datetime.now(timezone.utc),
    )
    session.add(snap)
    session.flush()

    blob = SnapshotBlob(
        snapshot_id=snap.id,
        payload={
            "account": {
                "key": f"acc_{domain.replace('.', '_')}",
                "display_name": f"Enterprise {domain}",
                "legal_name": f"Enterprise {domain} Inc",
                "domain": domain,
                "sec_cik": cik,
            },
            "lobs": [],
            "sub_lobs": [],
            "personas": [],
            "posts": [],
            "cxo_movements": [],
            "linkedin_jobs": [],
            "targets": None,
        },
    )
    session.add(blob)
    session.commit()
    return snap


def test_insufficient_credits_returns_402_zero_side_effects():
    """Client with 0 remaining credits gets 402 on hydration with zero writes."""
    client = TestClient(app)
    headers, user_id = _get_admin_headers()

    session = SessionLocal()
    try:
        # Exhaust credits
        c = session.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        c.api_credits_total = 5
        c.api_credits_used = 5
        session.commit()

        snap = _create_test_snapshot(session, f"broke-{uuid.uuid4().hex[:6]}.com", f"{random.randint(1000000, 9999999):010d}")
        preview_tok = AccountReuseService.generate_preview_token(snap.id, TEST_CREDIT_SLUG, user_id)

        ledger_count_before = session.query(CreditLedger).filter_by(client_id=c.id).count()
        account_count_before = session.execute(text(f'SELECT count(*) FROM "{TEST_CREDIT_SCHEMA}"."accounts"')).scalar()
    finally:
        session.close()

    resp = client.post(
        "/api/catalog/hydrate",
        headers=headers,
        json={
            "snapshot_id": snap.id,
            "preview_token": preview_tok,
            "idempotency_key": f"insufficient_test_{uuid.uuid4().hex[:12]}",
        },
    )
    assert resp.status_code == 402, resp.text

    verify_session = SessionLocal()
    try:
        c = verify_session.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        assert c.api_credits_used == 5  # Unchanged!

        ledger_count_after = verify_session.query(CreditLedger).filter_by(client_id=c.id).count()
        assert ledger_count_after == ledger_count_before

        account_count_after = verify_session.execute(text(f'SELECT count(*) FROM "{TEST_CREDIT_SCHEMA}"."accounts"')).scalar()
        assert account_count_after == account_count_before
    finally:
        verify_session.close()


def test_credit_ledger_reconciliation_hydrate_and_rollback():
    """Validates in-transaction credit reservation, ledger rows (ref_type, ref_id, balance_after), and rollback refund."""
    client = TestClient(app)
    headers, user_id = _get_admin_headers()
    super_headers, _ = _get_super_headers()

    session = SessionLocal()
    try:
        c = session.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        c.api_credits_total = 100
        c.api_credits_used = 10
        session.commit()

        snap = _create_test_snapshot(session, f"reconcile-{uuid.uuid4().hex[:6]}.com", f"{random.randint(1000000, 9999999):010d}")
        preview_tok = AccountReuseService.generate_preview_token(snap.id, TEST_CREDIT_SLUG, user_id)
        idem_key = f"reconcile_test_{uuid.uuid4().hex[:12]}"
    finally:
        session.close()

    # 1. Hydrate
    resp = client.post(
        "/api/catalog/hydrate",
        headers=headers,
        json={
            "snapshot_id": snap.id,
            "preview_token": preview_tok,
            "idempotency_key": idem_key,
        },
    )
    assert resp.status_code == 200, resp.text
    share_event_id = resp.json()["share_event_id"]

    # Verify debit in ledger
    verify_session = SessionLocal()
    try:
        c = verify_session.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        assert c.api_credits_used == 11  # 10 + 1 fee

        debit_entry = verify_session.query(CreditLedger).filter_by(
            client_id=c.id,
            ref_type="share_event",
            ref_id=share_event_id,
        ).first()
        assert debit_entry is not None
        assert debit_entry.delta == -1
        assert debit_entry.balance_after == (100 - 11)  # 89 remaining
    finally:
        verify_session.close()

    # 2. Rollback
    rb_resp = client.post(
        f"/api/catalog/share-events/{share_event_id}/rollback",
        headers=super_headers,
        json={"reason": "Reconciliation test rollback"},
    )
    assert rb_resp.status_code == 200, rb_resp.text

    # Verify refund in ledger
    verify_session2 = SessionLocal()
    try:
        c = verify_session2.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        assert c.api_credits_used == 10  # Refunded back to 10!

        refund_entry = verify_session2.query(CreditLedger).filter(
            CreditLedger.client_id == c.id,
            CreditLedger.ref_type == "share_event",
            CreditLedger.ref_id == share_event_id,
            CreditLedger.delta > 0,
        ).first()
        assert refund_entry is not None
        assert refund_entry.delta == 1
        assert refund_entry.balance_after == (100 - 10)  # 90 remaining

        total_event_delta = verify_session2.query(func.sum(CreditLedger.delta)).filter(
            CreditLedger.client_id == c.id,
            CreditLedger.ref_type == "share_event",
            CreditLedger.ref_id == share_event_id,
        ).scalar()
        assert total_event_delta == 0
    finally:
        verify_session2.close()


def test_idempotency_key_reuse_for_different_snapshot_returns_409():
    """Reusing the same idempotency key for a different snapshot returns 409 idempotency_key_reuse."""
    client = TestClient(app)
    headers, user_id = _get_admin_headers()

    session = SessionLocal()
    try:
        c = session.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        c.api_credits_total = 100
        c.api_credits_used = 10
        session.commit()

        snap1 = _create_test_snapshot(session, f"snap1-{uuid.uuid4().hex[:6]}.com", f"{random.randint(1000000, 9999999):010d}")
        snap2 = _create_test_snapshot(session, f"snap2-{uuid.uuid4().hex[:6]}.com", f"{random.randint(1000000, 9999999):010d}")

        preview1 = AccountReuseService.generate_preview_token(snap1.id, TEST_CREDIT_SLUG, user_id)
        preview2 = AccountReuseService.generate_preview_token(snap2.id, TEST_CREDIT_SLUG, user_id)

        shared_key = f"shared_key_{uuid.uuid4().hex[:12]}"
    finally:
        session.close()

    # 1. Hydrate snapshot 1 with shared key -> OK
    r1 = client.post(
        "/api/catalog/hydrate",
        headers=headers,
        json={
            "snapshot_id": snap1.id,
            "preview_token": preview1,
            "idempotency_key": shared_key,
        },
    )
    assert r1.status_code == 200, r1.text

    # 2. Hydrate snapshot 2 with SAME key -> 409 Conflict
    r2 = client.post(
        "/api/catalog/hydrate",
        headers=headers,
        json={
            "snapshot_id": snap2.id,
            "preview_token": preview2,
            "idempotency_key": shared_key,
        },
    )
    assert r2.status_code == 409, r2.text
    data = r2.json()
    assert data["detail"]["error"] == "idempotency_key_reuse"


def test_double_click_retry_returns_stored_result_with_single_debit():
    """Double-clicking / retrying with identical parameters returns stored result with only 1 credit debit."""
    client = TestClient(app)
    headers, user_id = _get_admin_headers()

    session = SessionLocal()
    try:
        c = session.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        c.api_credits_total = 100
        c.api_credits_used = 10
        session.commit()

        snap = _create_test_snapshot(session, f"doubleclick-{uuid.uuid4().hex[:6]}.com", f"{random.randint(1000000, 9999999):010d}")
        preview = AccountReuseService.generate_preview_token(snap.id, TEST_CREDIT_SLUG, user_id)
        idem_key = f"doubleclick_{uuid.uuid4().hex[:12]}"
    finally:
        session.close()

    # Click 1
    r1 = client.post(
        "/api/catalog/hydrate",
        headers=headers,
        json={
            "snapshot_id": snap.id,
            "preview_token": preview,
            "idempotency_key": idem_key,
        },
    )
    assert r1.status_code == 200, r1.text
    data1 = r1.json()
    acc_id = data1["account_id"]

    # Click 2 (Retry)
    r2 = client.post(
        "/api/catalog/hydrate",
        headers=headers,
        json={
            "snapshot_id": snap.id,
            "preview_token": preview,
            "idempotency_key": idem_key,
        },
    )
    assert r2.status_code == 200, r2.text
    data2 = r2.json()
    assert data2["idempotent"] is True
    assert data2["account_id"] == acc_id

    # Verify only ONE credit debit in ledger
    verify_session = SessionLocal()
    try:
        c = verify_session.query(Client).filter_by(slug=TEST_CREDIT_SLUG).first()
        assert c.api_credits_used == 11  # Only +1 credit used, not +2!

        ledger_rows = verify_session.query(CreditLedger).filter_by(
            client_id=c.id,
            ref_type="share_event",
            ref_id=data1["share_event_id"],
        ).all()
        assert len(ledger_rows) == 1
    finally:
        verify_session.close()


def test_invalid_idempotency_key_format_returns_400():
    """Caller-supplied idempotency key with invalid characters or too short returns 400."""
    client = TestClient(app)
    headers, user_id = _get_admin_headers()

    session = SessionLocal()
    try:
        snap = _create_test_snapshot(session, f"badkey-{uuid.uuid4().hex[:6]}.com", f"{random.randint(1000000, 9999999):010d}")
        preview = AccountReuseService.generate_preview_token(snap.id, TEST_CREDIT_SLUG, user_id)
    finally:
        session.close()

    # Too short (<8 chars)
    r = client.post(
        "/api/catalog/hydrate",
        headers=headers,
        json={
            "snapshot_id": snap.id,
            "preview_token": preview,
            "idempotency_key": "short",
        },
    )
    assert r.status_code == 400, r.text

    # Invalid characters (spaces)
    r2 = client.post(
        "/api/catalog/hydrate",
        headers=headers,
        json={
            "snapshot_id": snap.id,
            "preview_token": preview,
            "idempotency_key": "invalid key with spaces",
        },
    )
    assert r2.status_code == 400, r2.text
