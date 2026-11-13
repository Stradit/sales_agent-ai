"""Test Suite: S-17 & S-18 Unified Account Matcher, Duplicate Detection, and Key Collision Resolution.

Validates:
1. Unpadded stored CIK in tenant matches zero-padded catalog CIK.
2. Blank-domain and NULL-domain rows never match.
3. Two different companies with the same base key both hydrate with distinct keys via collision resolution.
4. Hydrating the same company twice aborts with 409 DuplicateAccountError containing existing account ID.
5. Reuse-check returns already_exists=True, existing_account_id, and refresh_available boolean.
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
from db.models.platform_catalog import Company, Snapshot, SnapshotBlob
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from services.account_matcher import AccountMatcher, ExistingAccount
from services.account_reuse_service import AccountReuseService
from services.catalog_service import CatalogService

SRC_SLUG = "test_match_src_01"
SRC_SCHEMA = f"client_{SRC_SLUG}"
TGT_SLUG = "test_match_tgt_01"
TGT_SCHEMA = f"client_{TGT_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_match_env():
    session = SessionLocal()
    try:
        session.execute(text(f"DELETE FROM public.clients WHERE slug IN ('{SRC_SLUG}', '{TGT_SLUG}')"))
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'matcher_%@corp.com'"))
        session.commit()

        create_client_schema(SRC_SLUG)
        create_client_schema(TGT_SLUG)

        c_src = session.query(Client).filter_by(slug=SRC_SLUG).first()
        c_src.status = "active"
        c_src.catalog_publish = True
        c_src.catalog_consume = True

        c_tgt = session.query(Client).filter_by(slug=TGT_SLUG).first()
        c_tgt.status = "active"
        c_tgt.catalog_publish = True
        c_tgt.catalog_consume = True
        c_tgt.reuse_requires_approval = False
        c_tgt.api_credits_total = 100
        c_tgt.api_credits_used = 0

        pwd = auth.hash_password("testpass123")
        u_super = session.query(User).filter_by(role="super_admin").first()
        if not u_super:
            u_super = User(email="matcher_super@corp.com", hashed_password=pwd, role="super_admin", full_name="Super Admin")
            session.add(u_super)
            session.flush()

        u_admin = session.query(User).filter_by(email="matcher_admin@corp.com").first()
        if not u_admin:
            u_admin = User(email="matcher_admin@corp.com", hashed_password=pwd, role="user", full_name="Target Admin")
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
        clean_session.execute(text("DELETE FROM public.users WHERE email LIKE 'matcher_%@corp.com'"))
        clean_session.commit()
    finally:
        clean_session.close()


def _get_tgt_admin_token_and_user_id():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(email="matcher_admin@corp.com").first()
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


def test_unpadded_stored_cik_matches_padded_catalog_cik():
    """Validates that unpadded CIK stored in tenant matches zero-padded 10-digit CIK."""
    session = SessionLocal()
    try:
        # Seed an account in target with UNPADDED CIK '778899'
        unique_key = f"unpadded_co_{uuid.uuid4().hex[:6]}"
        tgt_acct_id = session.execute(
            text(f"""
                INSERT INTO "{TGT_SCHEMA}"."accounts"
                (display_name, domain, key, sec_cik)
                VALUES ('Unpadded Target Corp', 'unpadded-target.com', :k, '778899')
                RETURNING id
            """),
            {"k": unique_key},
        ).scalar()
        session.commit()

        # Match using normalized 10-digit CIK '0000778899'
        match = AccountMatcher.find_existing(
            session=session,
            ctx_or_slug=TGT_SLUG,
            payload_or_company={"sec_cik": "0000778899"},
        )
        assert match is not None
        assert match.account_id == tgt_acct_id
        assert match.match_reason == "sec_cik"
        assert match.sec_cik == "778899"
    finally:
        session.close()


def test_blank_and_null_domains_never_match():
    """Validates that blank domain and NULL domain rows in target never match."""
    session = SessionLocal()
    try:
        # Seed accounts with empty string and NULL domain
        session.execute(
            text(f"""
                INSERT INTO "{TGT_SCHEMA}"."accounts"
                (display_name, domain, key, sec_cik)
                VALUES
                ('Blank Domain Corp', '', :k1, NULL),
                ('Null Domain Corp', NULL, :k2, NULL)
            """),
            {"k1": f"blank_dom_{uuid.uuid4().hex[:6]}", "k2": f"null_dom_{uuid.uuid4().hex[:6]}"},
        )
        session.commit()

        # Match with None / empty
        assert AccountMatcher.find_existing(session, TGT_SLUG, {"domain": ""}) is None
        assert AccountMatcher.find_existing(session, TGT_SLUG, {"domain": None}) is None

        # Match with a real domain that doesn't exist
        assert AccountMatcher.find_existing(session, TGT_SLUG, {"domain": "notfound-unique.com"}) is None
    finally:
        session.close()


def test_key_collision_resolution_different_companies():
    """Validates that two different companies with the same base key hydrate with distinct unique keys."""
    client = TestClient(app)
    admin_token, user_id = _get_tgt_admin_token_and_user_id()
    super_user_id = _get_super_user_id()

    # Company 1
    session = SessionLocal()
    try:
        acct_1 = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts"
                (display_name, domain, key, sec_cik)
                VALUES ('Collision Co One', 'collision-one.com', 'shared_key', '0008881111')
                RETURNING id
            """),
        ).scalar()
        session.commit()

        snap_1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_1,
            published_by_user_id=super_user_id,
            force=True,
        )
        snap_id_1 = snap_1.id
    finally:
        session.close()

    # Company 2 (different CIK & domain, same desired key 'shared_key')
    session = SessionLocal()
    try:
        acct_2 = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts"
                (display_name, domain, key, sec_cik)
                VALUES ('Collision Co Two', 'collision-two.com', 'shared_key', '0008882222')
                RETURNING id
            """),
        ).scalar()
        session.commit()

        snap_2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_2,
            published_by_user_id=super_user_id,
            force=True,
        )
        snap_id_2 = snap_2.id
    finally:
        session.close()

    # Hydrate Company 1
    preview_tok_1 = AccountReuseService.generate_preview_token(snap_id_1, TGT_SLUG, user_id)
    res_1 = client.post(
        "/api/catalog/hydrate",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
        json={
            "snapshot_id": snap_id_1,
            "preview_token": preview_tok_1,
            "mode": "instant",
            "idempotency_key": f"col-1-{uuid.uuid4().hex[:8]}",
        },
    )
    assert res_1.status_code == 200, res_1.text
    data_1 = res_1.json()
    assert data_1["account_key"] == "shared_key"

    # Hydrate Company 2 (collision on 'shared_key') -> MUST NOT be considered duplicate, key is resolved with hash
    preview_tok_2 = AccountReuseService.generate_preview_token(snap_id_2, TGT_SLUG, user_id)
    res_2 = client.post(
        "/api/catalog/hydrate",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
        json={
            "snapshot_id": snap_id_2,
            "preview_token": preview_tok_2,
            "mode": "instant",
            "idempotency_key": f"col-2-{uuid.uuid4().hex[:8]}",
        },
    )
    assert res_2.status_code == 200, res_2.text
    data_2 = res_2.json()
    assert data_2["account_key"].startswith("shared_key_")
    assert data_2["account_key"] != data_1["account_key"]
    assert data_2["account_id"] != data_1["account_id"]


def test_same_company_twice_fails_409_with_existing_id():
    """Validates that hydrating the same company twice aborts with 409 and existing account id."""
    client = TestClient(app)
    admin_token, user_id = _get_tgt_admin_token_and_user_id()
    super_user_id = _get_super_user_id()

    session = SessionLocal()
    try:
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts"
                (display_name, domain, key, sec_cik)
                VALUES ('Acme Corp Dupe Test', 'acmedupe.com', :k, '0009991234')
                RETURNING id
            """),
            {"k": f"acme_dupe_{uuid.uuid4().hex[:6]}"},
        ).scalar()
        session.commit()

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

    # First hydration
    tok_1 = AccountReuseService.generate_preview_token(snap_id, TGT_SLUG, user_id)
    res_1 = client.post(
        "/api/catalog/hydrate",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
        json={
            "snapshot_id": snap_id,
            "preview_token": tok_1,
            "mode": "instant",
            "idempotency_key": f"dupe-1-{uuid.uuid4().hex[:8]}",
        },
    )
    assert res_1.status_code == 200, res_1.text
    first_account_id = res_1.json()["account_id"]

    # Second hydration with different idempotency key -> MUST return 409 DuplicateAccountError with ID
    tok_2 = AccountReuseService.generate_preview_token(snap_id, TGT_SLUG, user_id)
    res_2 = client.post(
        "/api/catalog/hydrate",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
        json={
            "snapshot_id": snap_id,
            "preview_token": tok_2,
            "mode": "instant",
            "idempotency_key": f"dupe-2-{uuid.uuid4().hex[:8]}",
        },
    )
    assert res_2.status_code == 409, res_2.text
    assert f"ID: {first_account_id}" in res_2.text or str(first_account_id) in res_2.text


def test_reuse_check_surfaces_already_exists_and_refresh_available():
    """Validates that check_reuse_available surfaces already_exists, existing_account_id, and refresh_available."""
    client = TestClient(app)
    admin_token, user_id = _get_tgt_admin_token_and_user_id()
    super_user_id = _get_super_user_id()

    session = SessionLocal()
    try:
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts"
                (display_name, domain, key, sec_cik)
                VALUES ('Refresh Check Corp', 'refreshcheck.com', :k, '0009995555')
                RETURNING id
            """),
            {"k": f"ref_chk_{uuid.uuid4().hex[:6]}"},
        ).scalar()
        session.commit()

        snap_v1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=super_user_id,
            force=True,
        )
        snap_id_1 = snap_v1.id
    finally:
        session.close()

    # Before hydration: already_exists=False, refresh_available=False
    chk_before = client.get(
        "/api/catalog/reuse-check?identifier=refreshcheck.com",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
    )
    assert chk_before.status_code == 200, chk_before.text
    data_before = chk_before.json()
    assert data_before["available"] is True
    assert data_before["already_exists"] is False
    assert data_before.get("refresh_available") is False

    # Hydrate V1
    tok = AccountReuseService.generate_preview_token(snap_id_1, TGT_SLUG, user_id)
    hyd = client.post(
        "/api/catalog/hydrate",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
        json={
            "snapshot_id": snap_id_1,
            "preview_token": tok,
            "mode": "instant",
            "idempotency_key": f"chk-ref-{uuid.uuid4().hex[:8]}",
        },
    )
    assert hyd.status_code == 200, hyd.text
    tgt_acct_id = hyd.json()["account_id"]

    # After hydration (version 1 == version 1): already_exists=True, refresh_available=False
    chk_after_v1 = client.get(
        "/api/catalog/reuse-check?identifier=refreshcheck.com",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
    )
    assert chk_after_v1.status_code == 200, chk_after_v1.text
    data_v1 = chk_after_v1.json()
    assert data_v1["already_exists"] is True
    assert data_v1["existing_account_id"] == tgt_acct_id
    assert data_v1["refresh_available"] is False

    # Publish V2 of snapshot from source
    session = SessionLocal()
    try:
        session.execute(
            text(f'UPDATE "{SRC_SCHEMA}"."accounts" SET display_name = \'Refresh Check Corp Updated\' WHERE id = :aid'),
            {"aid": acct_id},
        )
        session.commit()
        snap_v2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=super_user_id,
            force=True,
        )
        assert snap_v2.version > snap_v1.version
    finally:
        session.close()

    # After V2 published: snapshot.version (2) > account_origin.snapshot_version (1) -> refresh_available=True!
    chk_after_v2 = client.get(
        "/api/catalog/reuse-check?identifier=refreshcheck.com",
        headers={"Authorization": f"Bearer {admin_token}", "X-Client-Slug": TGT_SLUG},
    )
    assert chk_after_v2.status_code == 200, chk_after_v2.text
    data_v2 = chk_after_v2.json()
    assert data_v2["already_exists"] is True
    assert data_v2["existing_account_id"] == tgt_acct_id
    assert data_v2["refresh_available"] is True
