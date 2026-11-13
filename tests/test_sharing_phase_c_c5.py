"""Phase C5 Tests: Credit Transparency, Tenant Ledger, Summaries, CSV Export, Formula Injection Defense, and Reconciliation Guard.

Tests:
1. Tenant isolation: ledger endpoints return strictly the caller's tenant rows.
2. Pagination and keyset cursor stability.
3. Role masking: analyst and viewer see transaction user masked (user: None).
4. Credit summary matches SQL aggregates and returns reuse savings.
5. Savings immutability: savings use stored values from share_events, not recomputed rates.
6. CSV export permission enforcement: client_admin allowed, analyst forbidden (403).
7. Formula injection defense: CSV escapes cells starting with '=', '+', '-', '@', '\\t', '\\r'.
8. Reconciliation script: detect injected ledger drift and repairs it with --fix.
9. Platform summary: super-admin view for multi-tenant totals and single-client views.
"""

import csv
import io
import subprocess
import sys
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from api import app
import auth
from db.connection import SessionLocal, get_session
from db.models.client import Client
from db.models.credit_ledger import CreditLedger
from db.models.platform_catalog import Company, ShareEvent, Snapshot
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from db.tenant import schema_for, validate_slug
from scripts.verify_credit_ledger import verify_client_credits
from services.catalog_presenter import sanitize_csv_cell


@pytest.fixture(scope="module")
def c5_env():
    """Sets up two isolated tenant schemas (test_c5_t1, test_c5_t2) and seeded credit ledgers."""
    slug_t1 = "test_c5_t1"
    slug_t2 = "test_c5_t2"

    drop_client_schema(slug_t1)
    drop_client_schema(slug_t2)
    create_client_schema(slug_t1)
    create_client_schema(slug_t2)

    with get_session() as db:
        # Client T1
        c1 = db.query(Client).filter_by(slug=slug_t1).first()
        c1.api_credits_total = 50000
        c1.api_credits_used = 0
        c1.is_active = True
        c1.status = "active"
        db.flush()

        # Client T2
        c2 = db.query(Client).filter_by(slug=slug_t2).first()
        c2.api_credits_total = 10000
        c2.api_credits_used = 0
        c2.is_active = True
        c2.status = "active"
        db.flush()

        # Users: T1 Admin, T1 Analyst, Super Admin
        admin_user = db.query(User).filter_by(email="admin_c5@test.com").first()
        if not admin_user:
            admin_user = User(
                email="admin_c5@test.com",
                full_name="Alice Admin",
                hashed_password=auth.hash_password("pass123"),
                role="client_admin",
                is_active=True,
            )
            db.add(admin_user)
            db.flush()

        analyst_user = db.query(User).filter_by(email="analyst_c5@test.com").first()
        if not analyst_user:
            analyst_user = User(
                email="analyst_c5@test.com",
                full_name="Bob Analyst",
                hashed_password=auth.hash_password("pass123"),
                role="analyst",
                is_active=True,
            )
            db.add(analyst_user)
            db.flush()

        super_user = db.query(User).filter_by(email="super_c5@test.com").first()
        if not super_user:
            super_user = User(
                email="super_c5@test.com",
                full_name="Sam Super",
                hashed_password=auth.hash_password("pass123"),
                role="super_admin",
                is_active=True,
            )
            db.add(super_user)
            db.flush()

        # Access grants
        db.query(UserClientAccess).filter(UserClientAccess.user_id.in_([admin_user.id, analyst_user.id])).delete()
        db.add(UserClientAccess(user_id=admin_user.id, client_id=c1.id, role="client_admin"))
        db.add(UserClientAccess(user_id=analyst_user.id, client_id=c1.id, role="analyst"))
        db.flush()

        # Seed catalog company & snapshot
        comp = db.query(Company).filter_by(canonical_key="c5test.com").first()
        if not comp:
            comp = Company(canonical_key="c5test.com", display_name="C5 Enterprise Inc")
            db.add(comp)
            db.flush()

        snap = db.query(Snapshot).filter_by(company_id=comp.id, version=1).first()
        if not snap:
            snap = Snapshot(
                company_id=comp.id,
                version=1,
                status="ready",
                quality_score=92.0,
                quality_grade="A",
                counts={"accounts": 1},
                checksum="chk_c5_v1",
                taken_at=datetime.now(timezone.utc),
            )
            db.add(snap)
            db.flush()

        # Clean up any leftover share events for c1
        db.query(ShareEvent).filter_by(target_client_id=c1.id).delete()
        db.flush()

        # Seed share event with stored savings
        import uuid
        idem_key = f"idem_c5_{uuid.uuid4().hex[:8]}"
        share_event = ShareEvent(
            target_client_id=c1.id,
            snapshot_id=snap.id,
            status="done",
            mode="hydrate",
            idempotency_key=idem_key,
            fee_credits=15,
            estimated_credits_saved=85,
            requested_by=admin_user.id,
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
            completed_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        db.add(share_event)
        db.flush()

        # Seed T1 ledger rows:
        # 1. Catalog reuse fee (-15)
        # 2. Pipeline run (-100)
        # 3. Credit refund (+20)
        # 4. Dangerous injection row for CSV formula testing
        db.query(CreditLedger).filter(CreditLedger.client_id.in_([c1.id, c2.id])).delete()

        t1_rows = [
            CreditLedger(
                client_id=c1.id,
                delta=-15,
                balance_after=49985,
                reason="catalog_reuse:hydrate",
                ref_type="share_event",
                ref_id=share_event.id,
                created_at=datetime.now(timezone.utc) - timedelta(days=2),
            ),
            CreditLedger(
                client_id=c1.id,
                delta=-100,
                balance_after=49885,
                reason="reserve:run:enrich_org",
                created_at=datetime.now(timezone.utc) - timedelta(days=1),
            ),
            CreditLedger(
                client_id=c1.id,
                delta=20,
                balance_after=49905,
                reason="settle:refund:unused_tokens",
                created_at=datetime.now(timezone.utc) - timedelta(hours=12),
            ),
            CreditLedger(
                client_id=c1.id,
                delta=-5,
                balance_after=49900,
                reason="=cmd|' /C calc'!A0 injection attack",
                created_at=datetime.now(timezone.utc) - timedelta(hours=1),
            ),
        ]
        db.add_all(t1_rows)

        # Seed T2 ledger rows (must NEVER leak into T1)
        t2_rows = [
            CreditLedger(
                client_id=c2.id,
                delta=-300,
                balance_after=9700,
                reason="reserve:run:t2_secret",
                created_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        ]
        db.add_all(t2_rows)

        # Update client recorded used
        c1.api_credits_used = 15 + 100 - 20 + 5  # 100
        c2.api_credits_used = 300

        # Generate tokens before session closes to avoid DetachedInstanceError
        admin_token = auth.create_access_token(admin_user, client_slug=slug_t1)
        analyst_token = auth.create_access_token(analyst_user, client_slug=slug_t1)
        super_token = auth.create_access_token(super_user)

        c1_id = c1.id
        c2_id = c2.id
        share_event_id = share_event.id
        db.commit()

    client = TestClient(app)

    yield {
        "slug_t1": slug_t1,
        "slug_t2": slug_t2,
        "c1_id": c1_id,
        "c2_id": c2_id,
        "admin_token": admin_token,
        "analyst_token": analyst_token,
        "super_token": super_token,
        "client": client,
        "share_event_id": share_event_id,
    }

    drop_client_schema(slug_t1)
    drop_client_schema(slug_t2)


def test_credit_ledger_tenant_isolation(c5_env):
    """Ensures ledger transactions for tenant T1 never leak rows from tenant T2."""
    client = c5_env["client"]
    token = c5_env["admin_token"]
    slug = c5_env["slug_t1"]

    res = client.get(
        "/api/credits/ledger",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res.status_code == 200, res.text
    data = res.json()

    assert "items" in data
    assert data["total"] == 4
    assert len(data["items"]) == 4

    # Assert none of the items contain t2 data
    descriptions = [item["description"] for item in data["items"]]
    assert not any("t2_secret" in d for d in descriptions)

    # Assert deltas and types are present
    types = {item["type"] for item in data["items"]}
    assert "reuse_fee" in types
    assert "pipeline_run" in types
    assert "refund" in types


def test_credit_ledger_keyset_and_offset_pagination(c5_env):
    """Verifies page/page_size and keyset cursor stability."""
    client = c5_env["client"]
    token = c5_env["admin_token"]
    slug = c5_env["slug_t1"]

    # Offset pagination
    res_p1 = client.get(
        "/api/credits/ledger?page=1&page_size=2",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res_p1.status_code == 200
    data_p1 = res_p1.json()
    assert len(data_p1["items"]) == 2
    assert data_p1["total"] == 4
    assert data_p1["next_cursor"] is not None

    res_p2 = client.get(
        "/api/credits/ledger?page=2&page_size=2",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res_p2.status_code == 200
    data_p2 = res_p2.json()
    assert len(data_p2["items"]) == 2

    ids_p1 = [item["id"] for item in data_p1["items"]]
    ids_p2 = [item["id"] for item in data_p2["items"]]
    assert set(ids_p1).isdisjoint(set(ids_p2))

    # Keyset pagination using cursor
    cursor = data_p1["next_cursor"]
    res_cursor = client.get(
        f"/api/credits/ledger?cursor={cursor}&page_size=2",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res_cursor.status_code == 200
    data_cursor = res_cursor.json()
    assert len(data_cursor["items"]) == 2
    ids_cursor = [item["id"] for item in data_cursor["items"]]
    assert ids_cursor == ids_p2


def test_credit_ledger_analyst_user_masking(c5_env):
    """Analyst and Viewer roles must see transactions with user identities masked."""
    client = c5_env["client"]
    slug = c5_env["slug_t1"]

    # 1. Client Admin sees authoring user
    res_admin = client.get(
        "/api/credits/ledger",
        headers={"Authorization": f"Bearer {c5_env['admin_token']}", "X-Client-Slug": slug},
    )
    assert res_admin.status_code == 200
    admin_items = res_admin.json()["items"]
    reuse_admin = next(item for item in admin_items if item["type"] == "reuse_fee")
    assert reuse_admin["user"] is not None
    assert reuse_admin["user"]["name"] == "Alice Admin"

    # 2. Analyst sees masked user (None)
    res_analyst = client.get(
        "/api/credits/ledger",
        headers={"Authorization": f"Bearer {c5_env['analyst_token']}", "X-Client-Slug": slug},
    )
    assert res_analyst.status_code == 200
    analyst_items = res_analyst.json()["items"]
    for item in analyst_items:
        assert item["user"] is None


def test_credit_summary_aggregations(c5_env):
    """Credit summary correctly aggregates balance, spend by type, reuse savings, and refunds."""
    client = c5_env["client"]
    token = c5_env["admin_token"]
    slug = c5_env["slug_t1"]

    res = client.get(
        "/api/credits/summary?period=all",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res.status_code == 200, res.text
    data = res.json()

    # Balance
    assert data["total"] == 50000
    assert data["used"] == 100
    assert data["balance"] == 49900

    # Reuse savings
    reuse = data["reuse"]
    assert reuse["count"] == 1
    assert reuse["fees_paid"] == 15
    assert reuse["estimated_saved"] == 85
    assert reuse["net_savings"] == 70  # 85 - 15

    # Refunds
    assert data["refunds"] == 20

    # Spend by type
    spent_map = {item["type"]: item["total_spent"] for item in data["spent_by_type"]}
    assert spent_map.get("reuse_fee") == 15
    assert spent_map.get("pipeline_run") == 100

    # Daily series
    assert len(data["daily_series"]) > 0


def test_credit_summary_stored_savings_immutable(c5_env):
    """Savings come from the stored values on share events (C0), never recomputed."""
    client = c5_env["client"]
    token = c5_env["admin_token"]
    slug = c5_env["slug_t1"]

    # Assert current stored savings is 85
    res1 = client.get(
        "/api/credits/summary?period=all",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res1.json()["reuse"]["estimated_saved"] == 85

    # Manually modify share event stored savings in DB to verify it reflects stored value
    with get_session() as db:
        se = db.query(ShareEvent).filter_by(id=c5_env["share_event_id"]).first()
        se.estimated_credits_saved = 120
        db.commit()

    res2 = client.get(
        "/api/credits/summary?period=all",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res2.json()["reuse"]["estimated_saved"] == 120

    # Restore
    with get_session() as db:
        se = db.query(ShareEvent).filter_by(id=c5_env["share_event_id"]).first()
        se.estimated_credits_saved = 85
        db.commit()


def test_credit_ledger_export_csv_auth_and_format(c5_env):
    """CSV export requires client_admin (403 for analyst) and returns streaming text/csv."""
    client = c5_env["client"]
    slug = c5_env["slug_t1"]

    # 1. Analyst is forbidden from exporting CSV
    res_analyst = client.get(
        "/api/credits/ledger/export.csv",
        headers={"Authorization": f"Bearer {c5_env['analyst_token']}", "X-Client-Slug": slug},
    )
    assert res_analyst.status_code == 403

    # 2. Client Admin gets streaming CSV
    res_admin = client.get(
        "/api/credits/ledger/export.csv",
        headers={"Authorization": f"Bearer {c5_env['admin_token']}", "X-Client-Slug": slug},
    )
    assert res_admin.status_code == 200
    assert "text/csv" in res_admin.headers["content-type"]
    assert "attachment; filename=\"credits-ledger-" in res_admin.headers["content-disposition"]

    # Parse CSV content
    reader = csv.reader(io.StringIO(res_admin.text))
    rows = list(reader)
    assert len(rows) >= 5  # 1 header + 4 rows
    assert rows[0][0] == "Transaction ID"
    assert rows[0][1] == "Timestamp"
    assert rows[0][2] == "Type"


def test_csv_formula_injection_defense(c5_env):
    """Prefixes unsafe leading characters (=, +, -, @, \\t, \\r) with a single quote."""
    client = c5_env["client"]
    token = c5_env["admin_token"]
    slug = c5_env["slug_t1"]

    # Unit test helper
    assert sanitize_csv_cell("=1+1") == "'=1+1"
    assert sanitize_csv_cell("+44123") == "'+44123"
    assert sanitize_csv_cell("-100") == "'-100"
    assert sanitize_csv_cell("@SUM(A1:B1)") == "'@SUM(A1:B1)"
    assert sanitize_csv_cell("\tmalicious") == "'\tmalicious"
    assert sanitize_csv_cell("Normal Text") == "Normal Text"
    assert sanitize_csv_cell(123) == "123"
    assert sanitize_csv_cell(None) == ""

    # Live CSV export integration test
    res = client.get(
        "/api/credits/ledger/export.csv",
        headers={"Authorization": f"Bearer {token}", "X-Client-Slug": slug},
    )
    assert res.status_code == 200

    # Ensure dangerous injection payload was escaped with leading single quote
    csv_text = res.text
    assert "'=cmd|' /C calc'!A0 injection attack" in csv_text or "'=cmd|" in csv_text


def test_verify_credit_ledger_reconciliation_script(c5_env):
    """Management script detects ledger drift and repairs it with --fix."""
    slug = c5_env["slug_t1"]

    with get_session() as db:
        c1 = db.query(Client).filter_by(slug=slug).first()
        cid = c1.id

        # 1. Verify clean state (no drift)
        is_drift, rec_used, exp_used, drift = verify_client_credits(db, cid, slug)
        assert not is_drift
        assert drift == 0

        # 2. Inject intentional drift into clients.api_credits_used
        c1.api_credits_used = 9999
        db.commit()

        # 3. Check that verification detects drift
        is_drift, rec_used, exp_used, drift = verify_client_credits(db, cid, slug)
        assert is_drift
        assert drift == (9999 - exp_used)

    # 4. Run scripts/verify_credit_ledger.py CLI without --fix -> should exit 1
    res_fail = subprocess.run(
        [sys.executable, "scripts/verify_credit_ledger.py", "--client", slug],
        capture_output=True,
        text=True,
    )
    assert res_fail.returncode == 1
    assert "Ledger drift detected" in res_fail.stdout

    # 5. Run scripts/verify_credit_ledger.py CLI with --fix -> should exit 0 and repair
    res_fix = subprocess.run(
        [sys.executable, "scripts/verify_credit_ledger.py", "--client", slug, "--fix"],
        capture_output=True,
        text=True,
    )
    assert res_fix.returncode == 0
    assert "All credit ledgers reconciled successfully" in res_fix.stdout

    # 6. Verify database is now repaired
    with get_session() as db:
        c1 = db.query(Client).filter_by(slug=slug).first()
        is_drift, rec_used, exp_used, drift = verify_client_credits(db, cid, slug)
        assert not is_drift
        assert drift == 0
        assert c1.api_credits_used == exp_used


def test_platform_summary_super_admin_only(c5_env):
    """GET /api/credits/platform-summary requires super_admin and returns platform totals."""
    client = c5_env["client"]

    # Non-super admin is forbidden
    res_admin = client.get(
        "/api/credits/platform-summary",
        headers={"Authorization": f"Bearer {c5_env['admin_token']}"},
    )
    assert res_admin.status_code == 403

    # Super admin receives global metrics
    res_super = client.get(
        "/api/credits/platform-summary",
        headers={"Authorization": f"Bearer {c5_env['super_token']}"},
    )
    assert res_super.status_code == 200
    data = res_super.json()
    assert "total_allocated" in data
    assert "total_used" in data
    assert "total_balance" in data
    assert "clients" in data
    assert len(data["clients"]) > 0

    # Single client filter
    res_single = client.get(
        f"/api/credits/platform-summary?client={c5_env['slug_t1']}",
        headers={"Authorization": f"Bearer {c5_env['super_token']}"},
    )
    assert res_single.status_code == 200
    single_data = res_single.json()
    assert single_data["client_slug"] == c5_env["slug_t1"]
    assert "balance" in single_data
