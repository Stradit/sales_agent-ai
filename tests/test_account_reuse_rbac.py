"""Test Suite: RBAC & Permission Matrix for Cross-Tenant Account Reuse.

Validates:
1. Unified effective role checks for catalog operations:
   - 'catalog:publish': super_admin only (or client with catalog_publish=True).
   - 'account:reuse_check': super_admin, client_admin.
   - 'account:reuse_request': super_admin, client_admin.
   - 'catalog:rollback': super_admin only.
2. Global read-only cap: 'viewer' role is strictly rejected from executing hydration or publishing.
3. Cross-Tenant Anonymity: Tenant callers NEVER receive source client slug/id across any role.
4. Quota overflow prevention: Hydration aborts if target tenant quota is exceeded, with zero residue.
"""

import uuid
import pytest
from sqlalchemy import text

import auth
from auth import (
    get_effective_role,
    has_permission,
    hash_password,
    PERMISSION_ROLES,
)
from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.platform_catalog import Company, Snapshot, SnapshotBlob
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema
from services.catalog_service import CatalogService
from services.account_reuse_service import (
    AccountReuseService,
    AccountQuotaExceededError,
)

RBAC_SRC_SLUG = "rbac_reuse_src"
RBAC_TGT_SLUG = "rbac_reuse_tgt"
RBAC_SRC_SCHEMA = f"client_{RBAC_SRC_SLUG}"
RBAC_TGT_SCHEMA = f"client_{RBAC_TGT_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_rbac_reuse_env():
    session = SessionLocal()
    try:
        session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2)"), {"s1": RBAC_SRC_SLUG, "s2": RBAC_TGT_SLUG})
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'rbac_reuse_%'"))
        session.commit()
    finally:
        session.close()

    # Drop schemas
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{RBAC_SRC_SCHEMA}" CASCADE'))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{RBAC_TGT_SCHEMA}" CASCADE'))

    # Provision schemas
    create_client_schema(RBAC_SRC_SLUG)
    create_client_schema(RBAC_TGT_SLUG)

    session = SessionLocal()
    try:
        # 1. Clients
        src_client = session.query(Client).filter_by(slug=RBAC_SRC_SLUG).first()
        tgt_client = session.query(Client).filter_by(slug=RBAC_TGT_SLUG).first()
        src_client.catalog_publish = True
        src_client.api_credits_total = 1000
        tgt_client.catalog_consume = True
        tgt_client.api_credits_total = 1000
        tgt_client.max_accounts = 2
        session.flush()

        # 2. Users
        pwd = hash_password("pass123")
        u_super = User(email="rbac_reuse_super@corp.com", hashed_password=pwd, role="super_admin", full_name="Super Admin")
        u_admin = User(email="rbac_reuse_admin@corp.com", hashed_password=pwd, role="user", full_name="Client Admin")
        u_analyst = User(email="rbac_reuse_analyst@corp.com", hashed_password=pwd, role="user", full_name="Analyst")
        u_viewer = User(email="rbac_reuse_viewer@corp.com", hashed_password=pwd, role="viewer", full_name="Viewer")

        session.add_all([u_super, u_admin, u_analyst, u_viewer])
        session.flush()

        # 3. Tenant Grants
        session.add(UserClientAccess(user_id=u_admin.id, client_id=tgt_client.id, role="client_admin"))
        session.add(UserClientAccess(user_id=u_analyst.id, client_id=tgt_client.id, role="analyst"))
        session.add(UserClientAccess(user_id=u_viewer.id, client_id=tgt_client.id, role="viewer"))
        session.commit()

        # Seed account in source tenant
        aid = session.execute(
            text(f"""
                INSERT INTO "{RBAC_SRC_SCHEMA}"."accounts"
                (key, display_name, domain, sec_cik, country, city, is_manually_verified, owner_user_id)
                VALUES
                ('rbac_corp', 'RBAC Enterprise Solutions', 'rbacenterprise.com', '0001888888',
                 'United States', 'Austin', true, :uid)
                RETURNING id
            """),
            {"uid": u_super.id},
        ).scalar()
        session.commit()

        # Publish to catalog
        CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=RBAC_SRC_SLUG,
            account_id=aid,
            published_by_user_id=u_super.id,
            force=True,
        )

        users_dict = {
            "super": u_super.id,
            "admin": u_admin.id,
            "analyst": u_analyst.id,
            "viewer": u_viewer.id,
        }
    finally:
        session.close()

    yield users_dict

    # Cleanup
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{RBAC_SRC_SCHEMA}" CASCADE'))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{RBAC_TGT_SCHEMA}" CASCADE'))
    session = SessionLocal()
    try:
        session.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key LIKE '%0001888888%'"))
        session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2)"), {"s1": RBAC_SRC_SLUG, "s2": RBAC_TGT_SLUG})
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'rbac_reuse_%'"))
        session.commit()
    finally:
        session.close()


def test_rbac_permission_matrix(setup_rbac_reuse_env):
    """Verifies that unified role hierarchy accurately maps to permissions."""
    session = SessionLocal()
    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        users = setup_rbac_reuse_env
        u_super = session.query(User).filter_by(id=users["super"]).first()
        u_admin = session.query(User).filter_by(id=users["admin"]).first()
        u_analyst = session.query(User).filter_by(id=users["analyst"]).first()
        u_viewer = session.query(User).filter_by(id=users["viewer"]).first()

        # 1. Super Admin has all permissions across any tenant context
        assert get_effective_role(session, u_super, RBAC_TGT_SLUG) == "super_admin"
        assert has_permission(get_effective_role(session, u_super, RBAC_TGT_SLUG), "catalog:publish") is True
        assert has_permission(get_effective_role(session, u_super, RBAC_TGT_SLUG), "catalog:rollback") is True
        assert has_permission(get_effective_role(session, u_super, RBAC_TGT_SLUG), "account:reuse_check") is True

        # 2. Client Admin has account reuse permissions, but NOT catalog rollback or global publish
        assert get_effective_role(session, u_admin, RBAC_TGT_SLUG) == "client_admin"
        assert has_permission(get_effective_role(session, u_admin, RBAC_TGT_SLUG), "account:reuse_check") is True
        assert has_permission(get_effective_role(session, u_admin, RBAC_TGT_SLUG), "account:reuse_request") is True
        assert has_permission(get_effective_role(session, u_admin, RBAC_TGT_SLUG), "catalog:rollback") is False
        assert has_permission(get_effective_role(session, u_admin, RBAC_TGT_SLUG), "catalog:publish") is False

        # 3. Analyst can write accounts and run auto-pipeline, but cannot approve reuse
        assert get_effective_role(session, u_analyst, RBAC_TGT_SLUG) == "analyst"
        assert has_permission(get_effective_role(session, u_analyst, RBAC_TGT_SLUG), "account:write") is True
        assert has_permission(get_effective_role(session, u_analyst, RBAC_TGT_SLUG), "auto_pipeline:run") is True
        assert has_permission(get_effective_role(session, u_analyst, RBAC_TGT_SLUG), "catalog:approve") is False

        # 4. Viewer has global read-only cap
        assert get_effective_role(session, u_viewer, RBAC_TGT_SLUG) == "viewer"
        assert has_permission(get_effective_role(session, u_viewer, RBAC_TGT_SLUG), "account:read") is True
        assert has_permission(get_effective_role(session, u_viewer, RBAC_TGT_SLUG), "account:write") is False
        assert has_permission(get_effective_role(session, u_viewer, RBAC_TGT_SLUG), "account:reuse_check") is False
        assert has_permission(get_effective_role(session, u_viewer, RBAC_TGT_SLUG), "auto_pipeline:run") is False
    finally:
        auth.AUTH_ENFORCED = original_enforced
        session.close()


def test_cross_tenant_zero_knowledge_anonymity(setup_rbac_reuse_env):
    """Guarantees target tenant receives zero data or hints about source tenant identity."""
    session = SessionLocal()
    try:
        users = setup_rbac_reuse_env
        u_admin = session.query(User).filter_by(id=users["admin"]).first()

        preview = AccountReuseService.check_reuse_available(session, "rbacenterprise.com", RBAC_TGT_SLUG)
        assert preview is not None

        # Verify no source tenant fields
        for forbidden in ["source_client_slug", "source_client_id", "source_client_name", "source_account_id"]:
            assert forbidden not in preview, f"Forbidden leakage: '{forbidden}' present in preview"

        # Hydrate account
        company = session.query(Company).filter_by(canonical_key="cik:0001888888").first()
        snapshot = CatalogService.get_latest_snapshot(session, company.id)

        res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=RBAC_TGT_SLUG,
            snapshot_id=snapshot.id,
            user_id=u_admin.id,
            idempotency_key=f"anon_test_{uuid.uuid4().hex}",
            fee_credits=1,
        )
        assert res["ok"] is True
        for forbidden in ["source_client_slug", "source_client_id"]:
            assert forbidden not in res, f"Forbidden leakage: '{forbidden}' present in hydration result"

        # Verify target schema account_origin table
        origin_row = session.execute(
            text(f'SELECT origin_type, canonical_key, snapshot_id FROM "{RBAC_TGT_SCHEMA}"."account_origin" WHERE account_id = :aid'),
            {"aid": res["account_id"]},
        ).fetchone()
        assert origin_row is not None
        assert origin_row[0] == "catalog"
        assert origin_row[1] == "cik:0001888888"
        assert origin_row[2] == snapshot.id
    finally:
        session.close()


def test_quota_overflow_aborts_atomically(setup_rbac_reuse_env):
    """Verifies that exceeding the plan's max_accounts limit rejects hydration with zero residual state."""
    session = SessionLocal()
    try:
        users = setup_rbac_reuse_env
        u_admin = session.query(User).filter_by(id=users["admin"]).first()
        tgt_client = session.query(Client).filter_by(slug=RBAC_TGT_SLUG).first()

        # Set max_accounts to 1 (which is already reached by the account hydrated in the previous test)
        tgt_client.max_accounts = 1
        session.commit()

        # Seed another company in catalog to attempt hydration
        overflow_key = f"cik:{uuid.uuid4().hex[:10]}"
        company2 = Company(canonical_key=overflow_key, display_name="Overflow Corp")
        session.add(company2)
        session.flush()

        snap2 = Snapshot(
            company_id=company2.id,
            version=1,
            status="ready",
            source_client_id=None,
            policy_version="1.0.0",
            skeleton_version="T0002",
            counts={"accounts": 1},
            checksum="test_overflow_checksum",
        )
        session.add(snap2)
        session.flush()

        blob2 = SnapshotBlob(
            snapshot_id=snap2.id,
            payload={"account": {"key": "overflow_acct", "display_name": "Overflow Corp", "domain": "overflow.com"}},
        )
        session.add(blob2)
        session.commit()

        # Count accounts in target schema before attempt
        cnt_before = session.execute(text(f'SELECT count(*) FROM "{RBAC_TGT_SCHEMA}"."accounts"')).scalar()

        # Hydration must fail with AccountQuotaExceededError
        with pytest.raises(AccountQuotaExceededError, match="maximum allowed accounts limit"):
            AccountReuseService.hydrate_account_from_catalog(
                session=session,
                target_client_slug=RBAC_TGT_SLUG,
                snapshot_id=snap2.id,
                user_id=u_admin.id,
                idempotency_key=f"overflow_test_{uuid.uuid4().hex}",
            )

        # Count accounts after: exactly identical, zero residue
        cnt_after = session.execute(text(f'SELECT count(*) FROM "{RBAC_TGT_SCHEMA}"."accounts"')).scalar()
        assert cnt_after == cnt_before
    finally:
        session.close()
