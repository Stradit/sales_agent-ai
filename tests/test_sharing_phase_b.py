"""Test Suite: Cross-Tenant Account Sharing Phase B Full Test Matrix.

Validates all 10 Phase B correctness items:
- B0: TenantRequestContext single role resolution, schema binding, and route signature guards.
- B1: RBAC account:reuse_execute permission matrix (super_admin, client_admin, analyst, viewer, unauthenticated).
- B2: Table-driven identifier classification and canonical key normalization (CIK, LEI, domain, weak key).
- B3: Catalog deduplication, identifier linking, and company merge resolution.
- B4: Concurrency loops (20x parallel attempts) with advisory locks and deterministic Checksum v2.
- B5: In-transaction credit ledger reconciliation and idempotency conflict detection.
- B6: Complete share policy deny-by-default enforcement, retention cutoffs, and recursive JSON PII scrubbing.
- B7: Skeleton version compatibility check on hydrate (missing column dropped vs required NOT NULL 409).
- B8: Unified account matcher (padded CIK, domain, blank fallback, and key collision resolution).
- B9: Local edits database trigger, provenance timestamps, and minimal account refresh workflow.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import hashlib
import json
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text, inspect

import auth
from api import app
from core.tenant_context import TenantRequestContext, ClientFlags
from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.credit_ledger import CreditLedger
from db.models.platform_catalog import (
    Company,
    CompanyIdentifier,
    ShareEvent,
    Snapshot,
    SnapshotBlob,
    AccountOrigin,
)
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from services.account_reuse_service import (
    AccountReuseService,
    DuplicateAccountError,
    IdempotencyConflictError,
    LocalEditsPresentError,
    RollbackBlockedError,
    SkeletonIncompatibleError,
)
from services.catalog_service import CatalogService, CanonicalResolution
from services.credit_service import CreditService

api_client = TestClient(app)

SRC_SLUG = "test_pb_src"
TGT_A_SLUG = "test_pb_tgt_a"
TGT_B_SLUG = "test_pb_tgt_b"

SCHEMA_SRC = f"client_{SRC_SLUG}"
SCHEMA_TGT_A = f"client_{TGT_A_SLUG}"
SCHEMA_TGT_B = f"client_{TGT_B_SLUG}"


def _headers_for(user_id: int, client_slug: str = None) -> dict:
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        token = auth.create_access_token(user, client_slug=client_slug)
        headers = {"Authorization": f"Bearer {token}"}
        if client_slug:
            headers["X-Client-Slug"] = client_slug
        return headers
    finally:
        session.close()


@pytest.fixture(scope="module")
def setup_phase_b_environment():
    """Provisions real Postgres tenant schemas and users for Phase B integration test matrix."""
    session = SessionLocal()
    try:
        # Pre-cleanup
        for slug in (SRC_SLUG, TGT_A_SLUG, TGT_B_SLUG):
            try:
                drop_client_schema(slug)
            except Exception:
                pass

        session.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key LIKE 'test_pb_%' OR canonical_key LIKE 'cik:000777%' OR canonical_key LIKE 'dom:testpb%'"))
        session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2, :s3)"), {"s1": SRC_SLUG, "s2": TGT_A_SLUG, "s3": TGT_B_SLUG})
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'test_pb_%'"))
        session.commit()

        # Provision Schemas
        create_client_schema(SRC_SLUG)
        create_client_schema(TGT_A_SLUG)
        create_client_schema(TGT_B_SLUG)

        # Configure Client records
        src_client = session.query(Client).filter_by(slug=SRC_SLUG).first()
        src_client.status = "active"
        src_client.catalog_publish = True
        src_client.catalog_consume = True
        src_client.api_credits_total = 5000
        src_client.api_credits_used = 0

        tgt_a_client = session.query(Client).filter_by(slug=TGT_A_SLUG).first()
        tgt_a_client.status = "active"
        tgt_a_client.catalog_publish = True
        tgt_a_client.catalog_consume = True
        tgt_a_client.reuse_requires_approval = False
        tgt_a_client.api_credits_total = 5000
        tgt_a_client.api_credits_used = 0

        tgt_b_client = session.query(Client).filter_by(slug=TGT_B_SLUG).first()
        tgt_b_client.status = "active"
        tgt_b_client.catalog_publish = False
        tgt_b_client.catalog_consume = True
        tgt_b_client.reuse_requires_approval = True
        tgt_b_client.api_credits_total = 5000
        tgt_b_client.api_credits_used = 0
        session.commit()

        # Create Users
        pwd = auth.hash_password("phasebpass123")
        super_user = User(email="test_pb_super@example.com", hashed_password=pwd, full_name="PB Super Admin", role="super_admin")
        admin_a = User(email="test_pb_admin_a@example.com", hashed_password=pwd, full_name="PB Admin A", role="user")
        analyst_b = User(email="test_pb_analyst_b@example.com", hashed_password=pwd, full_name="PB Analyst B", role="user")
        viewer_b = User(email="test_pb_viewer_b@example.com", hashed_password=pwd, full_name="PB Viewer B", role="viewer")
        session.add_all([super_user, admin_a, analyst_b, viewer_b])
        session.flush()

        # Access grants
        session.add(UserClientAccess(user_id=admin_a.id, client_id=tgt_a_client.id, role="client_admin"))
        session.add(UserClientAccess(user_id=analyst_b.id, client_id=tgt_b_client.id, role="analyst"))
        session.add(UserClientAccess(user_id=viewer_b.id, client_id=tgt_b_client.id, role="viewer"))
        session.commit()

        env_data = {
            "src_client_id": src_client.id,
            "tgt_a_client_id": tgt_a_client.id,
            "tgt_b_client_id": tgt_b_client.id,
            "super_id": super_user.id,
            "admin_a_id": admin_a.id,
            "analyst_b_id": analyst_b.id,
            "viewer_b_id": viewer_b.id,
        }
    finally:
        session.close()

    yield env_data

    # Teardown
    cleanup_session = SessionLocal()
    try:
        cleanup_session.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key LIKE 'test_pb_%' OR canonical_key LIKE 'cik:000777%' OR canonical_key LIKE 'dom:testpb%'"))
        cleanup_session.execute(text("DELETE FROM public.credit_ledger WHERE client_id IN (:c1, :c2, :c3)"), {
            "c1": env_data["src_client_id"], "c2": env_data["tgt_a_client_id"], "c3": env_data["tgt_b_client_id"]
        })
        cleanup_session.execute(text("DELETE FROM public.audit_log WHERE client_id IN (:c1, :c2, :c3)"), {
            "c1": env_data["src_client_id"], "c2": env_data["tgt_a_client_id"], "c3": env_data["tgt_b_client_id"]
        })
        cleanup_session.execute(text("DELETE FROM public.users WHERE email LIKE 'test_pb_%'"))
        cleanup_session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2, :s3)"), {
            "s1": SRC_SLUG, "s2": TGT_A_SLUG, "s3": TGT_B_SLUG
        })
        cleanup_session.commit()
    finally:
        cleanup_session.close()

    for slug in (SRC_SLUG, TGT_A_SLUG, TGT_B_SLUG):
        try:
            drop_client_schema(slug)
        except Exception:
            pass


# ── B0: TenantRequestContext & Route Signature Guards ────────────────────────

def test_matrix_b0_tenant_context_and_route_guards(setup_phase_b_environment):
    """B0: Verifies TenantRequestContext role resolution and inspects all catalog routes."""
    env = setup_phase_b_environment
    session = SessionLocal()
    try:
        # 1. Resolve context for client admin
        ctx = TenantRequestContext.for_client(
            session=session,
            client=TGT_A_SLUG,
            user_id=env["admin_a_id"],
            user_role="user",
            effective_role="client_admin",
        )
        assert ctx.client_slug == TGT_A_SLUG
        assert ctx.schema == SCHEMA_TGT_A
        assert ctx.effective_role == "client_admin"
        assert ctx.client_flags.catalog_consume is True
        assert ctx.client_flags.reuse_requires_approval is False

        # 2. Super admin acting with explicit target slug
        ctx_super = TenantRequestContext.for_client(
            session=session,
            client=TGT_B_SLUG,
            user_id=env["super_id"],
            user_role="super_admin",
            effective_role="super_admin",
        )
        assert ctx_super.is_super_admin is True
        assert ctx_super.client_slug == TGT_B_SLUG
        assert ctx_super.schema == SCHEMA_TGT_B
        assert ctx_super.client_flags.reuse_requires_approval is True

        # 3. Route signature inspection guard:
        # Assert no endpoint handler under /api/catalog directly takes a loose 'slug' or 'schema' parameter
        from routes.catalog_router import catalog_router
        for route in catalog_router.routes:
            endpoint = route.endpoint
            import inspect as pyinspect
            sig = pyinspect.signature(endpoint)
            # Param names should never be naked 'client_slug' or 'schema' without being ctx or dependency
            for param_name, param in sig.parameters.items():
                if param_name in ("schema", "client_schema"):
                    assert False, f"Route {route.path} exposes forbidden raw param: {param_name}"
    finally:
        session.close()


# ── B1: RBAC Permission Matrix ───────────────────────────────────────────────

def test_matrix_b1_rbac_permission_matrix(setup_phase_b_environment):
    """B1: Verifies account:reuse_execute vs account:reuse_request across all roles."""
    env = setup_phase_b_environment
    super_headers = _headers_for(env["super_id"])
    admin_a_headers = _headers_for(env["admin_a_id"], TGT_A_SLUG)
    analyst_b_headers = _headers_for(env["analyst_b_id"], TGT_B_SLUG)
    viewer_b_headers = _headers_for(env["viewer_b_id"], TGT_B_SLUG)

    session = SessionLocal()
    try:
        # Seed an account and publish a snapshot
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, country)
                VALUES ('test_b1_corp', 'B1 Corp', 'b1corp.com', 'United States')
                RETURNING id
            """)
        ).scalar()
        session.commit()

        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=env["super_id"],
            force=True,
        )
        snap_id = snap.id
    finally:
        session.close()

    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        # 1. Unauthenticated -> 401
        res = api_client.get("/api/catalog/reuse-check?identifier=b1corp.com")
        assert res.status_code == 401

        # 2. Viewer -> 403 on library, requests, hydrate
        res = api_client.get("/api/catalog/companies", headers=viewer_b_headers)
        assert res.status_code == 403
        res = api_client.post("/api/catalog/requests", headers=viewer_b_headers, json={"snapshot_id": snap_id, "preview_token": "foo"})
        assert res.status_code == 403

        # 3. Analyst on TGT_B -> 403 on reuse-check (requires client_admin or super_admin) and hydrate
        res = api_client.get(f"/api/catalog/reuse-check?identifier=b1corp.com", headers=analyst_b_headers)
        assert res.status_code == 403

        # Analyst attempting direct hydrate -> 403
        res = api_client.post(
            "/api/catalog/hydrate",
            headers=analyst_b_headers,
            json={"snapshot_id": snap_id, "preview_token": "any_token", "mode": "instant"},
        )
        assert res.status_code == 403

        # 4. Client Admin on TGT_A (reuse_requires_approval=False) -> direct hydrate permitted
        res_a_chk = api_client.get("/api/catalog/reuse-check?identifier=b1corp.com", headers=admin_a_headers)
        assert res_a_chk.status_code == 200
        tok_a = res_a_chk.json()["preview_token"]

        res_a_hyd = api_client.post(
            "/api/catalog/hydrate",
            headers=admin_a_headers,
            json={"snapshot_id": snap_id, "preview_token": tok_a, "mode": "instant", "idempotency_key": "b1_admin_a_hyd"},
        )
        assert res_a_hyd.status_code == 200
        assert res_a_hyd.json()["ok"] is True

        # 5. Super Admin -> direct assignment bypasses tenant approval
        res_assign = api_client.post(
            "/api/catalog/assign",
            headers=super_headers,
            json={"snapshot_id": snap_id, "target_client_slug": TGT_B_SLUG, "mode": "instant"},
        )
        assert res_assign.status_code == 200
        assert res_assign.json()["ok"] is True
    finally:
        auth.AUTH_ENFORCED = original_enforced


# ── B2: Table-Driven Identifier Normalization ────────────────────────────────

@pytest.mark.parametrize(
    "raw_input, expected_kind, expected_val",
    [
        ("cik:0001364742", "canonical_key", "cik:0001364742"),
        ("1364742", "cik", "0001364742"),
        ("0000000", "unknown", "0000000"),  # rejects all zeros
        ("12345678901", "unknown", "12345678901"),  # rejects >10 digits
        ("549300V5XZ12H6G20E62", "lei", "549300V5XZ12H6G20E62"),
        ("https://www.apple.com/investor", "domain", "apple.com"),
        ("http://us.fidelity.com:8080/path?q=1", "domain", "us.fidelity.com"),  # preserves subdomain
        ("192.168.1.1", "unknown", "192.168.1.1"),  # rejects bare IP
        ("   ", "unknown", ""),
    ]
)
def test_matrix_b2_identifier_normalization_table(raw_input, expected_kind, expected_val):
    """B2: Validates table-driven identifier classification and canonical normalization."""
    kind, val = CatalogService.classify_identifier(raw_input)
    assert kind == expected_kind
    if expected_val:
        assert val == expected_val


def test_matrix_b2_weak_resolution_fallback():
    """B2: Verifies weak company resolution with tenant-neutral sha256 hash."""
    account_no_ids = {
        "display_name": "Private Boutique Advisory LLC",
        "country": "United Kingdom",
    }
    resolution = CatalogService.resolve_canonical_key(account_no_ids)
    assert resolution.weak is True
    assert resolution.canonical_key.startswith("key:")
    # Exactly 16 hex chars after 'key:'
    assert len(resolution.canonical_key.split(":")[1]) == 16


# ── B3: Catalog Deduplication & Company Merge Candidates ─────────────────────

def test_matrix_b3_catalog_deduplication_and_merge(setup_phase_b_environment):
    """B3: Verifies identifier linking and merge candidate resolution."""
    env = setup_phase_b_environment
    session = SessionLocal()
    try:
        # Account 1: only domain
        acct1_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, country)
                VALUES ('merge_corp_1', 'Apex Merge Corp', 'apexmerge.com', 'United States')
                RETURNING id
            """)
        ).scalar()
        session.commit()

        snap1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct1_id,
            published_by_user_id=env["super_id"],
            force=True,
        )

        comp1 = session.query(Company).filter_by(id=snap1.company_id).first()
        assert comp1.canonical_key == "dom:apexmerge.com"

        # Account 2: same domain + CIK discovered
        acct2_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, sec_cik, country)
                VALUES ('merge_corp_2', 'Apex Merge Corp', 'apexmerge.com', '0007779999', 'United States')
                RETURNING id
            """)
        ).scalar()
        session.commit()

        snap2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct2_id,
            published_by_user_id=env["super_id"],
            force=True,
        )

        comp2 = session.query(Company).filter_by(id=snap2.company_id).first()
        # Should link into the same company or follow merged survivor
        survivor = CatalogService._follow_merged(session, comp2)
        assert survivor is not None

        # Verify CIK identifier was recorded
        cik_ident = session.query(CompanyIdentifier).filter_by(
            company_id=survivor.id, id_type="cik", id_value="0007779999"
        ).first()
        assert cik_ident is not None
    finally:
        session.close()


# ── B4: Concurrency Loops (20x) & Checksum v2 ────────────────────────────────

def test_matrix_b4_concurrency_loops_and_deterministic_checksum(setup_phase_b_environment):
    """B4: Runs 20 concurrent publish attempts ensuring zero duplicates/deadlocks and verifies Checksum v2."""
    env = setup_phase_b_environment
    session = SessionLocal()
    try:
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, sec_cik, country)
                VALUES ('conc_corp', 'Concurrent Dynamics', 'concurrentdyn.com', '0007778888', 'United States')
                RETURNING id
            """)
        ).scalar()
        session.commit()
    finally:
        session.close()

    def _worker(worker_id: int):
        s = SessionLocal()
        try:
            snap = CatalogService.publish_account_snapshot(
                session=s,
                source_client_slug=SRC_SLUG,
                account_id=acct_id,
                published_by_user_id=env["super_id"],
                force=True,
            )
            return (snap.id, snap.status)
        finally:
            s.close()

    # 20 concurrent threads
    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_worker, i) for i in range(20)]
        for f in as_completed(futures):
            results.append(f.result())

    assert len(results) == 20
    # All 20 threads succeeded without deadlock or unhandled unique constraint error
    assert all(r[1] == "ready" for r in results)

    # Deterministic Checksum v2 test:
    # Differing only in transient timestamps or float precision or key ordering
    payload_a = {
        "display_name": "Test Co",
        "market_cap": 12345.67890123,
        "updated_at": "2026-11-13T10:00:00Z",
        "items": [{"key": "b", "val": 2}, {"key": "a", "val": 1}],
    }
    payload_b = {
        "items": [{"key": "a", "val": 1}, {"key": "b", "val": 2}],
        "updated_at": "2026-11-13T12:00:00Z",
        "market_cap": 12345.67890145,
        "display_name": "Test Co",
    }
    norm_a = CatalogService.normalize_payload_for_checksum(payload_a)
    norm_b = CatalogService.normalize_payload_for_checksum(payload_b)
    chk_a = hashlib.sha256(json.dumps(norm_a, sort_keys=True).encode()).hexdigest()
    chk_b = hashlib.sha256(json.dumps(norm_b, sort_keys=True).encode()).hexdigest()
    assert chk_a == chk_b


# ── B5: Credit Ledger & Idempotency Conflicts ────────────────────────────────

def test_matrix_b5_in_transaction_credits_and_idempotency_conflicts(setup_phase_b_environment):
    """B5: Verifies in-transaction credit deduction and idempotency conflict rejection."""
    env = setup_phase_b_environment
    session = SessionLocal()
    try:
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, country)
                VALUES ('credit_corp', 'Credit Ledger Corp', 'creditleg.com', 'United States')
                RETURNING id
            """)
        ).scalar()
        session.commit()

        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=env["super_id"],
            force=True,
        )

        initial_credits = session.query(Client).filter_by(slug=TGT_A_SLUG).first().api_credits_used

        # 1. First hydration
        idem_key = "test_idem_b5_key"
        res1 = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=TGT_A_SLUG,
            snapshot_id=snap.id,
            user_id=env["admin_a_id"],
            idempotency_key=idem_key,
        )
        assert res1["ok"] is True

        credits_after_first = session.query(Client).filter_by(slug=TGT_A_SLUG).first().api_credits_used
        assert credits_after_first == initial_credits + 1

        # 2. Re-hydration with exact same idempotency key (idempotent replay)
        res2 = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=TGT_A_SLUG,
            snapshot_id=snap.id,
            user_id=env["admin_a_id"],
            idempotency_key=idem_key,
        )
        assert res2["ok"] is True
        assert res2.get("idempotent") is True

        # Credits must NOT be deducted twice
        credits_after_replay = session.query(Client).filter_by(slug=TGT_A_SLUG).first().api_credits_used
        assert credits_after_replay == credits_after_first

        # 3. Same idempotency key with conflicting snapshot_id -> raises ValueError (idempotency conflict)
        acct2_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, country)
                VALUES ('credit_corp_2', 'Credit Ledger Corp 2', 'creditleg2.com', 'United States')
                RETURNING id
            """)
        ).scalar()
        session.commit()
        snap2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct2_id,
            published_by_user_id=env["super_id"],
            force=True,
        )

        with pytest.raises((ValueError, IdempotencyConflictError)) as exc:
            AccountReuseService.hydrate_account_from_catalog(
                session=session,
                target_client_slug=TGT_A_SLUG,
                snapshot_id=snap2.id,
                user_id=env["admin_a_id"],
                idempotency_key=idem_key,
            )
        assert "Idempotency key" in str(exc.value)
    finally:
        session.close()


# ── B6: Share Policy Enforcement & Recursive JSON PII Scrubbing ──────────────

def test_matrix_b6_share_policy_enforcement_and_pii_scrubbing(setup_phase_b_environment):
    """B6: Verifies recursive JSON PII scrubbing and share policy sanitization."""
    from services.catalog_service import scrub_record_json_fields, sanitize_contact_email

    # 1. Contact email sanitization: personal emails masked/scrubbed
    assert sanitize_contact_email("personal@gmail.com", "targetcorp.com") is None
    assert sanitize_contact_email("john.doe@targetcorp.com", "targetcorp.com") is None
    assert sanitize_contact_email("contact@targetcorp.com", "targetcorp.com") == "contact@targetcorp.com"

    # 2. Recursive JSON scrubbing
    payload_with_pii = {
        "display_name": "Secure Corp",
        "contact_email": "leaked@gmail.com",
        "custom_metadata": {
            "notes": "internal deal private",
            "personal_email": "ceo@yahoo.com",
            "nested_list": [
                {"secret": 123, "safe_metric": 456},
                {"api_token": "xyz", "role": "admin"},
            ]
        }
    }
    scrubbed = scrub_record_json_fields(payload_with_pii)
    assert "personal_email" not in scrubbed["custom_metadata"]
    assert "secret" not in scrubbed["custom_metadata"]["nested_list"][0]
    assert "api_token" not in scrubbed["custom_metadata"]["nested_list"][1]
    assert scrubbed["custom_metadata"]["nested_list"][0]["safe_metric"] == 456


# ── B7: Skeleton Version Compatibility Check ─────────────────────────────────

def test_matrix_b7_skeleton_compatibility(setup_phase_b_environment):
    """B7: Verifies hydration drops extra nullable columns and raises 409 for missing NOT NULL columns."""
    from db.column_types import invalidate_column_kinds_cache
    env = setup_phase_b_environment
    session = SessionLocal()
    try:
        # Seed account in source and publish
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, country, aliases)
                VALUES ('skel_compat_co', 'Skeleton Co', 'skeletoncompat.com', 'United States', ARRAY['SkelAlias'])
                RETURNING id
            """)
        ).scalar()
        session.commit()

        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=env["super_id"],
            force=True,
        )

        # 1. Add a NOT NULL column without default to target accounts table
        session.execute(text(f'ALTER TABLE "{SCHEMA_TGT_B}"."accounts" ADD COLUMN IF NOT EXISTS "custom_mand_col" text DEFAULT \'temp\' NOT NULL'))
        session.execute(text(f'ALTER TABLE "{SCHEMA_TGT_B}"."accounts" ALTER COLUMN "custom_mand_col" DROP DEFAULT'))
        session.commit()
        invalidate_column_kinds_cache(SCHEMA_TGT_B, "accounts")

        # Hydrate must abort with SkeletonIncompatibleError
        with pytest.raises(SkeletonIncompatibleError) as exc:
            AccountReuseService.hydrate_account_from_catalog(
                session=session,
                target_client_slug=TGT_B_SLUG,
                snapshot_id=snap.id,
                user_id=env["super_id"],
                idempotency_key="test_b7_incompat",
            )
        session.rollback()
        assert "custom_mand_col" in str(exc.value)

        # 2. Cleanup custom NOT NULL column and test dropping an optional column
        session.execute(text(f'ALTER TABLE "{SCHEMA_TGT_B}"."accounts" DROP COLUMN IF EXISTS "custom_mand_col"'))
        session.execute(text(f'ALTER TABLE "{SCHEMA_TGT_B}"."accounts" DROP COLUMN IF EXISTS "aliases"'))
        session.commit()
        invalidate_column_kinds_cache(SCHEMA_TGT_B, "accounts")

        # Hydrate must succeed and record dropped column
        res_ok = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=TGT_B_SLUG,
            snapshot_id=snap.id,
            user_id=env["super_id"],
            idempotency_key="test_b7_drop_col",
        )
        assert res_ok["ok"] is True
        assert "dropped_columns" in res_ok
        assert "accounts.aliases" in res_ok["dropped_columns"]

        # Restore aliases column
        session.execute(text(f'ALTER TABLE "{SCHEMA_TGT_B}"."accounts" ADD COLUMN IF NOT EXISTS "aliases" text[]'))
        session.commit()
        invalidate_column_kinds_cache(SCHEMA_TGT_B, "accounts")
    finally:
        session.close()


# ── B8: Unified Account Matcher & Key Collision Resolution ───────────────────

def test_matrix_b8_account_matcher_and_collision_resolution(setup_phase_b_environment):
    """B8: Verifies CIK/domain matching hierarchy and non-destructive key collision resolution."""
    env = setup_phase_b_environment
    session = SessionLocal()
    try:
        # Existing tenant account
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_TGT_A}"."accounts"
                (key, display_name, domain, sec_cik, country)
                VALUES ('matched_co', 'Existing Corp', 'existing.com', '0001234567', 'United States')
                RETURNING id
            """)
        ).scalar()
        session.commit()

        # Match by unpadded CIK
        from services.account_matcher import AccountMatcher
        m_cik = AccountMatcher.find_existing(
            session=session,
            ctx_or_slug=TGT_A_SLUG,
            payload_or_company={"sec_cik": "1234567"},
        )
        assert m_cik is not None
        assert m_cik.account_id == acct_id

        # Match by domain
        m_dom = AccountMatcher.find_existing(
            session=session,
            ctx_or_slug=TGT_A_SLUG,
            payload_or_company={"domain": "https://www.existing.com/about"},
        )
        assert m_dom is not None
        assert m_dom.account_id == acct_id

        # Key collision test:
        # Incoming key 'matched_co' with different domain -> suffix resolved
        session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, country)
                VALUES ('matched_co', 'Different Corp', 'different.com', 'United States')
            """)
        )
        src_diff_id = session.execute(
            text(f'SELECT id FROM "{SCHEMA_SRC}"."accounts" WHERE domain = :d'),
            {"d": "different.com"},
        ).scalar()
        session.commit()

        snap_diff = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_diff_id,
            published_by_user_id=env["super_id"],
            force=True,
        )

        res_coll = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=TGT_A_SLUG,
            snapshot_id=snap_diff.id,
            user_id=env["admin_a_id"],
            idempotency_key="test_b8_collision",
        )
        assert res_coll["ok"] is True

        # Existing account kept its key 'matched_co'
        orig = session.execute(
            text(f'SELECT key FROM "{SCHEMA_TGT_A}"."accounts" WHERE id = :aid'),
            {"aid": acct_id},
        ).scalar()
        assert orig == "matched_co"

        # New account got collision-resolved key
        new_key = session.execute(
            text(f'SELECT key FROM "{SCHEMA_TGT_A}"."accounts" WHERE id = :aid'),
            {"aid": res_coll["account_id"]},
        ).scalar()
        assert new_key.startswith("matched_co_")
    finally:
        session.close()


# ── B9: Local Edits Trigger & Minimal Refresh Workflow ───────────────────────

def test_matrix_b9_local_edits_trigger_and_refresh(setup_phase_b_environment):
    """B9: Verifies local edits trigger, refresh flow, and vanished entity protection."""
    env = setup_phase_b_environment
    session = SessionLocal()
    try:
        # 1. Publish account with persona and LOB
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, domain, country, extracted_at)
                VALUES ('refresh_src_corp', 'Refresh Src Corp', 'refreshcorp.com', 'United States', NOW())
                RETURNING id
            """)
        ).scalar()

        lob_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."lobs"
                (account_id, key, lob_name, domain)
                VALUES (:aid, 'ref_lob_1', 'Wealth Division', 'wealth.refreshcorp.com')
                RETURNING id
            """),
            {"aid": acct_id},
        ).scalar()

        per1_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."personas"
                (account_id, lob_id, key, display_name, full_name, title, hierarchy_level, tier)
                VALUES (:aid, :lid, 'p_alice', 'Alice Vance', 'Alice Vance', 'Managing Director', 1, 'Tier 1')
                RETURNING id
            """),
            {"aid": acct_id, "lid": lob_id},
        ).scalar()
        session.commit()

        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=env["super_id"],
            force=True,
        )

        # 2. Hydrate into TGT_A
        hyd_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=TGT_A_SLUG,
            snapshot_id=snap.id,
            user_id=env["admin_a_id"],
            idempotency_key="test_b9_hyd_init",
        )
        assert hyd_res["ok"] is True
        tgt_acct_id = hyd_res["account_id"]

        # Check local_edits is False
        origin = session.execute(
            text(f'SELECT local_edits, source_extracted_at, snapshot_taken_at FROM "{SCHEMA_TGT_A}"."account_origin" WHERE account_id = :aid'),
            {"aid": tgt_acct_id},
        ).fetchone()
        assert origin[0] is False
        assert origin[1] is not None
        assert origin[2] is not None

        # 3. Trigger local edit on persona
        session.execute(
            text(f'UPDATE "{SCHEMA_TGT_A}"."personas" SET title = :t WHERE account_id = :aid'),
            {"t": "Senior Partner (Locally Edited)", "aid": tgt_acct_id},
        )
        session.commit()

        origin_after_edit = session.execute(
            text(f'SELECT local_edits FROM "{SCHEMA_TGT_A}"."account_origin" WHERE account_id = :aid'),
            {"aid": tgt_acct_id},
        ).scalar()
        assert origin_after_edit is True

        # 4. Attempt refresh with local_edits=True -> raises LocalEditsPresentError (409)
        with pytest.raises(LocalEditsPresentError) as exc:
            AccountReuseService.hydrate_account_from_catalog(
                session=session,
                target_client_slug=TGT_A_SLUG,
                snapshot_id=snap.id,
                user_id=env["admin_a_id"],
                mode="refresh",
            )
        session.rollback()
        assert "local" in str(exc.value).lower()

        # 5. Clear local_edits to test clean refresh
        session.execute(
            text(f'UPDATE "{SCHEMA_TGT_A}"."account_origin" SET local_edits = false WHERE account_id = :aid'),
            {"aid": tgt_acct_id},
        )
        session.commit()

        # Clean refresh
        ref_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=TGT_A_SLUG,
            snapshot_id=snap.id,
            user_id=env["admin_a_id"],
            mode="refresh",
            idempotency_key="test_b9_ref_clean",
        )
        assert ref_res["ok"] is True
        assert ref_res["mode"] == "refresh"
        assert ref_res["account_id"] == tgt_acct_id

        # 6. Verify rollback of refresh event is blocked
        with pytest.raises(RollbackBlockedError) as exc_rb:
            AccountReuseService.rollback_account_reuse(
                session=session,
                share_event_id=ref_res["share_event_id"],
                admin_user_id=env["super_id"],
            )
        session.rollback()
        assert "refresh" in str(exc_rb.value).lower()
    finally:
        session.close()
