"""Test Suite: S-13 Skeleton Version Compatibility on Hydration.

Validates:
1. Target with dropped/missing column: hydrate succeeds and reports column in dropped_columns and audit.
2. Target with new NOT NULL column without default: aborts before writes with 409 skeleton_incompatible.
3. Zero writes guarantee on skeleton incompatibility.
"""

import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
import auth
from api import app
from db.column_types import invalidate_column_kinds_cache
from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.platform_catalog import ShareEvent
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from services.account_reuse_service import AccountReuseService
from services.catalog_service import CatalogService

SRC_SLUG = "test_skel_src_01"
SRC_SCHEMA = f"client_{SRC_SLUG}"
TGT_SLUG = "test_skel_tgt_01"
TGT_SCHEMA = f"client_{TGT_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_skel_env():
    session = SessionLocal()
    try:
        session.execute(text(f"DELETE FROM public.clients WHERE slug IN ('{SRC_SLUG}', '{TGT_SLUG}')"))
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'skel_%@corp.com'"))
        session.commit()

        create_client_schema(SRC_SLUG)
        create_client_schema(TGT_SLUG)

        c_src = session.query(Client).filter_by(slug=SRC_SLUG).first()
        c_src.status = "active"
        c_src.catalog_publish = True
        c_src.catalog_consume = True
        c_src.skeleton_version = "T0002"

        c_tgt = session.query(Client).filter_by(slug=TGT_SLUG).first()
        c_tgt.status = "active"
        c_tgt.catalog_publish = True
        c_tgt.catalog_consume = True
        c_tgt.reuse_requires_approval = False
        c_tgt.api_credits_total = 100
        c_tgt.api_credits_used = 0
        c_tgt.skeleton_version = "T0002"

        pwd = auth.hash_password("testpass123")
        u_super = session.query(User).filter_by(role="super_admin").first()
        if not u_super:
            u_super = User(email="skel_super@corp.com", hashed_password=pwd, role="super_admin", full_name="Super Admin")
            session.add(u_super)
            session.flush()

        u_admin = session.query(User).filter_by(email="skel_admin@corp.com").first()
        if not u_admin:
            u_admin = User(email="skel_admin@corp.com", hashed_password=pwd, role="user", full_name="Target Admin")
            session.add(u_admin)
            session.flush()

        access = session.query(UserClientAccess).filter_by(user_id=u_admin.id, client_id=c_tgt.id).first()
        if not access:
            session.add(UserClientAccess(user_id=u_admin.id, client_id=c_tgt.id, role="client_admin"))

        session.commit()
    finally:
        session.close()

    yield

    engine.dispose()
    drop_client_schema(SRC_SLUG)
    drop_client_schema(TGT_SLUG)
    clean_session = SessionLocal()
    try:
        clean_session.execute(text(f"DELETE FROM public.clients WHERE slug IN ('{SRC_SLUG}', '{TGT_SLUG}')"))
        clean_session.execute(text("DELETE FROM public.users WHERE email LIKE 'skel_%@corp.com'"))
        clean_session.commit()
    finally:
        clean_session.close()


def _get_tgt_admin_token_and_user_id():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(email="skel_admin@corp.com").first()
        token = auth.create_access_token(user, client_slug=TGT_SLUG)
        return token, user.id
    finally:
        session.close()


def _get_super_user_id():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(role="super_admin").first()
        return user.id
    finally:
        session.close()


def _seed_source_account(display_name: str, domain: str, cik: str = None) -> int:
    session = SessionLocal()
    try:
        res = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts"
                (display_name, domain, key, sec_cik, aliases, industries, keywords)
                VALUES (:name, :dom, :k, :cik, :aliases, :ind, :kw)
                RETURNING id
            """),
            {
                "name": display_name,
                "dom": domain,
                "k": f"skel_{int(session.execute(text('SELECT EXTRACT(EPOCH FROM NOW())*1000')).scalar())}",
                "cik": cik,
                "aliases": ["AliasOne", "AliasTwo"],
                "ind": ["Software", "SaaS"],
                "kw": ["cloud", "enterprise"],
            },
        ).scalar()
        session.commit()
        return res
    finally:
        session.close()


def test_dropped_column_on_older_target():
    """Simulate a target with a dropped/missing column: hydrate succeeds with column reported as dropped."""
    client = TestClient(app)
    admin_token, user_id = _get_tgt_admin_token_and_user_id()
    super_user_id = _get_super_user_id()

    # 1. Seed account in source
    acct_id = _seed_source_account("Skel Dropped Test Corp", "skeldrop.test.com", "0001999001")

    # 2. Publish to catalog
    session = SessionLocal()
    try:
        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=super_user_id,
            force=True,
        )
        snap_id = snap.id
    finally:
        session.close()

    # 3. Simulate target schema missing 'aliases' column by dropping it from target accounts
    session = SessionLocal()
    try:
        session.execute(text(f'ALTER TABLE "{TGT_SCHEMA}"."accounts" DROP COLUMN IF EXISTS "aliases"'))
        session.commit()
    finally:
        session.close()
    invalidate_column_kinds_cache(TGT_SCHEMA, "accounts")

    preview_tok = AccountReuseService.generate_preview_token(snap_id, TGT_SLUG, user_id)

    # 4. Hydrate into target
    hyd_res = client.post(
        "/api/catalog/hydrate",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
        json={
            "snapshot_id": snap_id,
            "preview_token": preview_tok,
            "mode": "instant",
            "idempotency_key": f"skel-drop-idem-{uuid.uuid4().hex[:8]}",
        },
    )
    assert hyd_res.status_code == 200, hyd_res.text
    data = hyd_res.json()
    assert data["ok"] is True
    assert "dropped_columns" in data
    assert "accounts.aliases" in data["dropped_columns"]
    assert data["counts"]["dropped_columns"]["accounts.aliases"] >= 1
    assert data["skeleton_version"] == "T0002"

    # 5. Verify audit log has dropped_columns and skeleton_version
    session = SessionLocal()
    try:
        share_event = session.query(ShareEvent).filter_by(id=data["share_event_id"]).first()
        assert share_event is not None
        assert "dropped_columns" in share_event.counts
        assert "accounts.aliases" in share_event.counts["dropped_columns"]
    finally:
        session.close()


def test_new_not_null_column_without_default_aborts_hydration():
    """Simulate target with new NOT NULL column without default: aborts with 409 skeleton_incompatible and zero writes."""
    client = TestClient(app)
    admin_token, user_id = _get_tgt_admin_token_and_user_id()
    super_user_id = _get_super_user_id()

    # 1. Seed another account in source
    acct_id = _seed_source_account("Skel Incompat Test Corp", "skelincompat.test.com", "0001999002")

    # 2. Publish to catalog
    session = SessionLocal()
    try:
        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=super_user_id,
            force=True,
        )
        snap_id = snap.id
    finally:
        session.close()

    # 3. Add a NOT NULL column without default to target accounts table
    session = SessionLocal()
    try:
        session.execute(text(f'ALTER TABLE "{TGT_SCHEMA}"."accounts" ADD COLUMN IF NOT EXISTS "custom_mandatory_code" text DEFAULT \'temp\' NOT NULL'))
        session.execute(text(f'ALTER TABLE "{TGT_SCHEMA}"."accounts" ALTER COLUMN "custom_mandatory_code" DROP DEFAULT'))
        session.commit()
    finally:
        session.close()
    invalidate_column_kinds_cache(TGT_SCHEMA, "accounts")

    # Record initial counts in target
    session = SessionLocal()
    try:
        initial_count = session.execute(text(f'SELECT COUNT(*) FROM "{TGT_SCHEMA}"."accounts"')).scalar()
        initial_credits_used = session.query(Client).filter_by(slug=TGT_SLUG).first().api_credits_used
    finally:
        session.close()

    preview_tok = AccountReuseService.generate_preview_token(snap_id, TGT_SLUG, user_id)

    # 4. Attempt hydration - MUST fail with 409 skeleton_incompatible
    hyd_res = client.post(
        "/api/catalog/hydrate",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
        json={
            "snapshot_id": snap_id,
            "preview_token": preview_tok,
            "mode": "instant",
            "idempotency_key": f"skel-incompat-idem-{uuid.uuid4().hex[:8]}",
        },
    )
    assert hyd_res.status_code == 409, hyd_res.text
    err_detail = hyd_res.json().get("detail", {})
    assert err_detail.get("error") == "skeleton_incompatible"
    assert "custom_mandatory_code" in err_detail.get("message", "")

    # 5. ZERO WRITES guarantee: verify no account was inserted and no credits were used
    session = SessionLocal()
    try:
        final_count = session.execute(text(f'SELECT COUNT(*) FROM "{TGT_SCHEMA}"."accounts"')).scalar()
        final_credits_used = session.query(Client).filter_by(slug=TGT_SLUG).first().api_credits_used
        assert final_count == initial_count
        assert final_credits_used == initial_credits_used
    finally:
        session.close()

    # 6. Clean up the extra column so it doesn't affect other tests
    session = SessionLocal()
    try:
        session.execute(text(f'ALTER TABLE "{TGT_SCHEMA}"."accounts" DROP COLUMN IF EXISTS "custom_mandatory_code"'))
        session.execute(text(f'ALTER TABLE "{TGT_SCHEMA}"."accounts" ADD COLUMN IF NOT EXISTS "aliases" text[]'))
        session.commit()
    finally:
        session.close()
    invalidate_column_kinds_cache(TGT_SCHEMA, "accounts")
