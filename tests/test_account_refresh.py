"""Test Suite: S-19 Local Edits Trigger, Provenance Timestamps, and Account Refresh Workflow (Phase B B9).

Validates:
1. Edit a persona in tenant (UPDATE personas SET display_name='Edited' WHERE id=:pid) -> trigger fires and sets account_origin.local_edits = true.
2. Normal hydration with SET LOCAL app.catalog_hydrating = 'on' leaves account_origin.local_edits = false.
3. Refresh with no local edits updates intelligence and keeps existing tenant IDs stable (persona.id and lob.id unchanged).
4. Refresh with local edits (local_edits = true) aborts with 409 local_edits_present and makes zero writes.
5. Refresh never deletes a vanished persona that has an introduction link in introductions.persona_id.
6. Rollback on a refresh event (mode == 'refresh') fails with 400/409 RollbackBlockedError ("Rollback of catalog refresh operations is not supported.").
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
from db.models.platform_catalog import Company, ShareEvent, Snapshot, SnapshotBlob
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from services.account_matcher import AccountMatcher
from services.account_reuse_service import (
    AccountReuseService,
    LocalEditsPresentError,
    RollbackBlockedError,
)
from services.catalog_service import CatalogService

SRC_SLUG = "test_refresh_src_01"
SRC_SCHEMA = f"client_{SRC_SLUG}"
TGT_SLUG = "test_refresh_tgt_01"
TGT_SCHEMA = f"client_{TGT_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_refresh_env():
    session = SessionLocal()
    try:
        session.execute(text(f"DELETE FROM public.clients WHERE slug IN ('{SRC_SLUG}', '{TGT_SLUG}')"))
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'refresh_%@corp.com'"))
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
            u_super = User(email="refresh_super@corp.com", hashed_password=pwd, role="super_admin", full_name="Super Admin")
            session.add(u_super)
            session.flush()

        u_admin = session.query(User).filter_by(email="refresh_admin@corp.com").first()
        if not u_admin:
            u_admin = User(email="refresh_admin@corp.com", hashed_password=pwd, role="user", full_name="Target Admin")
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
        clean_session.execute(text("DELETE FROM public.users WHERE email LIKE 'refresh_%@corp.com'"))
        clean_session.commit()
    finally:
        clean_session.close()


def _get_tgt_admin_token_and_user_id():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(email="refresh_admin@corp.com").first()
        token = auth.create_access_token(user, client_slug=TGT_SLUG)
        return token, user.id
    finally:
        session.close()


def _get_super_token_and_user_id():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(role="super_admin").first()
        token = auth.create_access_token(user)
        return token, user.id
    finally:
        session.close()


def test_1_and_2_normal_hydration_and_local_edits_trigger():
    """Validates that:
    1. Normal hydration leaves account_origin.local_edits = false.
    2. Editing a persona in tenant fires the trigger and sets account_origin.local_edits = true.
    """
    session = SessionLocal()
    try:
        domain = f"refresh-trigger-{uuid.uuid4().hex[:6]}.com"
        acct_key = f"acct_trg_{uuid.uuid4().hex[:6]}"
        lob_key = f"lob_trg_{uuid.uuid4().hex[:6]}"
        per_key = f"per_trg_{uuid.uuid4().hex[:6]}"

        # 1. Seed in source tenant
        src_aid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts" (key, display_name, domain, extracted_at, created_at, updated_at)
                VALUES (:k, 'Trigger Test Corp', :d, NOW() - INTERVAL '5 days', NOW(), NOW())
                RETURNING id
            """),
            {"k": acct_key, "d": domain},
        ).scalar()

        src_lid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."lobs" (account_id, key, lob_name)
                VALUES (:aid, :k, 'Cloud Division')
                RETURNING id
            """),
            {"aid": src_aid, "k": lob_key},
        ).scalar()

        src_pid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."personas" (account_id, lob_id, key, first_name, last_name, display_name, headline)
                VALUES (:aid, :lid, :k, 'Sarah', 'Connor', 'Sarah Connor', 'VP Engineering')
                RETURNING id
            """),
            {"aid": src_aid, "lid": src_lid, "k": per_key},
        ).scalar()
        session.commit()

        # 2. Publish to platform catalog
        _, super_uid = _get_super_token_and_user_id()
        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()

        # 3. Test 2: Normal hydration leaves local_edits = false
        _, admin_uid = _get_tgt_admin_token_and_user_id()
        hyd_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=TGT_SLUG,
            snapshot_id=snap.id,
            user_id=admin_uid,
            mode="instant",
            fee_credits=0,
        )
        session.commit()

        tgt_aid = hyd_res["account_id"]
        origin_row = session.execute(
            text(f'SELECT local_edits, snapshot_version, source_extracted_at FROM "{TGT_SCHEMA}"."account_origin" WHERE account_id = :aid'),
            {"aid": tgt_aid},
        ).fetchone()

        assert origin_row is not None
        assert origin_row[0] is False, "Normal hydration must leave local_edits as false"
        assert origin_row[1] == 1, "Snapshot version should be 1"
        assert origin_row[2] is not None, "source_extracted_at must be populated from snapshot"

        # 4. Test 1: Edit persona in tenant -> trigger fires and sets local_edits = true
        tgt_pid = session.execute(
            text(f'SELECT id FROM "{TGT_SCHEMA}"."personas" WHERE account_id = :aid AND key = :k'),
            {"aid": tgt_aid, "k": per_key},
        ).scalar()
        assert tgt_pid is not None

        session.execute(
            text(f'UPDATE "{TGT_SCHEMA}"."personas" SET display_name = :n WHERE id = :pid'),
            {"n": "Sarah Connor (Updated Locally)", "pid": tgt_pid},
        )
        session.commit()

        local_edits_val = session.execute(
            text(f'SELECT local_edits FROM "{TGT_SCHEMA}"."account_origin" WHERE account_id = :aid'),
            {"aid": tgt_aid},
        ).scalar()

        assert local_edits_val is True, "Updating a persona must cause trigger to flip local_edits to true"
    finally:
        session.close()


def test_3_refresh_with_no_local_edits_keeps_stable_ids():
    """Validates that:
    Refresh with no local edits updates intelligence and keeps existing tenant IDs stable
    (persona.id and lob.id unchanged).
    """
    session = SessionLocal()
    try:
        domain = f"refresh-stable-{uuid.uuid4().hex[:6]}.com"
        acct_key = f"acct_stb_{uuid.uuid4().hex[:6]}"
        lob_key = f"lob_stb_{uuid.uuid4().hex[:6]}"
        per_key = f"per_stb_{uuid.uuid4().hex[:6]}"

        # 1. Seed in source tenant
        src_aid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts" (key, display_name, domain, extracted_at, created_at, updated_at)
                VALUES (:k, 'Stable Test Corp', :d, NOW() - INTERVAL '10 days', NOW(), NOW())
                RETURNING id
            """),
            {"k": acct_key, "d": domain},
        ).scalar()

        src_lid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."lobs" (account_id, key, lob_name, overview)
                VALUES (:aid, :k, 'AI Platforms', 'Old Overview')
                RETURNING id
            """),
            {"aid": src_aid, "k": lob_key},
        ).scalar()

        src_pid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."personas" (account_id, lob_id, key, first_name, last_name, display_name, headline)
                VALUES (:aid, :lid, :k, 'Alex', 'Murphy', 'Alex Murphy', 'Lead Architect')
                RETURNING id
            """),
            {"aid": src_aid, "lid": src_lid, "k": per_key},
        ).scalar()
        session.commit()

        # 2. Publish snapshot v1
        _, super_uid = _get_super_token_and_user_id()
        snap_v1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()

        # 3. Hydrate into target tenant
        _, admin_uid = _get_tgt_admin_token_and_user_id()
        hyd_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=TGT_SLUG,
            snapshot_id=snap_v1.id,
            user_id=admin_uid,
            mode="instant",
            fee_credits=0,
        )
        session.commit()

        tgt_aid = hyd_res["account_id"]
        tgt_lid_before = session.execute(
            text(f'SELECT id FROM "{TGT_SCHEMA}"."lobs" WHERE account_id = :aid AND key = :k'),
            {"aid": tgt_aid, "k": lob_key},
        ).scalar()
        tgt_pid_before = session.execute(
            text(f'SELECT id, headline FROM "{TGT_SCHEMA}"."personas" WHERE account_id = :aid AND key = :k'),
            {"aid": tgt_aid, "k": per_key},
        ).fetchone()

        assert tgt_lid_before is not None
        assert tgt_pid_before is not None
        assert tgt_pid_before[1] == "Lead Architect"

        # 4. Source tenant updates persona headline and lob overview, then publishes v2
        session.execute(
            text(f"""
                UPDATE "{SRC_SCHEMA}"."personas"
                SET headline = 'Chief Technology Officer'
                WHERE id = :pid
            """),
            {"pid": src_pid},
        )
        session.execute(
            text(f"""
                UPDATE "{SRC_SCHEMA}"."lobs"
                SET overview = 'Updated AI Platform Division'
                WHERE id = :lid
            """),
            {"lid": src_lid},
        )
        session.commit()

        snap_v2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()
        assert snap_v2.version > snap_v1.version

        # 5. Target tenant refreshes to v2
        ref_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=TGT_SLUG,
            snapshot_id=snap_v2.id,
            user_id=admin_uid,
            mode="refresh",
            fee_credits=0,
        )
        session.commit()

        assert ref_res["ok"] is True
        assert ref_res["mode"] == "refresh"
        assert ref_res["account_id"] == tgt_aid

        # 6. Verify tenant IDs remain stable and intelligence is updated
        tgt_lid_after = session.execute(
            text(f'SELECT id, overview FROM "{TGT_SCHEMA}"."lobs" WHERE account_id = :aid AND key = :k'),
            {"aid": tgt_aid, "k": lob_key},
        ).fetchone()
        tgt_pid_after = session.execute(
            text(f'SELECT id, headline FROM "{TGT_SCHEMA}"."personas" WHERE account_id = :aid AND key = :k'),
            {"aid": tgt_aid, "k": per_key},
        ).fetchone()

        assert tgt_lid_after[0] == tgt_lid_before, "LOB ID must remain unchanged across refresh"
        assert tgt_lid_after[1] == "Updated AI Platform Division", "LOB overview must be refreshed"

        assert tgt_pid_after[0] == tgt_pid_before[0], "Persona ID must remain unchanged across refresh"
        assert tgt_pid_after[1] == "Chief Technology Officer", "Persona headline must be refreshed"

        origin_after = session.execute(
            text(f'SELECT snapshot_version, local_edits FROM "{TGT_SCHEMA}"."account_origin" WHERE account_id = :aid'),
            {"aid": tgt_aid},
        ).fetchone()
        assert origin_after[0] == snap_v2.version
        assert origin_after[1] is False
    finally:
        session.close()


def test_4_refresh_with_local_edits_aborts_with_409():
    """Validates that:
    Refresh with local edits (local_edits = true) aborts with 409 local_edits_present and makes zero writes.
    """
    session = SessionLocal()
    try:
        domain = f"refresh-edits-{uuid.uuid4().hex[:6]}.com"
        acct_key = f"acct_edt_{uuid.uuid4().hex[:6]}"

        # 1. Seed in source tenant
        src_aid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts" (key, display_name, domain, extracted_at, created_at, updated_at)
                VALUES (:k, 'Edits Test Corp', :d, NOW(), NOW(), NOW())
                RETURNING id
            """),
            {"k": acct_key, "d": domain},
        ).scalar()
        session.commit()

        # 2. Publish snapshot v1
        _, super_uid = _get_super_token_and_user_id()
        snap_v1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()

        # 3. Hydrate into target
        _, admin_uid = _get_tgt_admin_token_and_user_id()
        hyd_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=TGT_SLUG,
            snapshot_id=snap_v1.id,
            user_id=admin_uid,
            mode="instant",
            fee_credits=0,
        )
        session.commit()
        tgt_aid = hyd_res["account_id"]

        # 4. Modify account locally in target tenant
        session.execute(
            text(f"""
                UPDATE "{TGT_SCHEMA}"."accounts"
                SET display_name = 'Locally Customized Brand'
                WHERE id = :aid
            """),
            {"aid": tgt_aid},
        )
        session.commit()

        origin = session.execute(
            text(f'SELECT local_edits FROM "{TGT_SCHEMA}"."account_origin" WHERE account_id = :aid'),
            {"aid": tgt_aid},
        ).scalar()
        assert origin is True, "Trigger should have marked local_edits as true"

        # 5. Create newer snapshot v2
        session.execute(
            text(f"""
                UPDATE "{SRC_SCHEMA}"."accounts"
                SET display_name = 'Upstream Brand v2'
                WHERE id = :aid
            """),
            {"aid": src_aid},
        )
        session.commit()
        snap_v2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()

        # 6. Attempt refresh via AccountReuseService directly -> raises LocalEditsPresentError
        with pytest.raises(LocalEditsPresentError) as exc_info:
            AccountReuseService.hydrate_account_from_catalog(
                session=session,
                ctx=TGT_SLUG,
                snapshot_id=snap_v2.id,
                user_id=admin_uid,
                mode="refresh",
            )
        session.rollback()
        assert "local tenant modifications" in str(exc_info.value).lower()

        # 7. Attempt refresh via HTTP endpoint -> returns 409 local_edits_present
        admin_token, _ = _get_tgt_admin_token_and_user_id()
        client = TestClient(app)
        resp = client.post(
            f"/api/catalog/accounts/{snap_v2.company_id}/refresh",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"snapshot_id": snap_v2.id},
        )
        assert resp.status_code == 409
        body = resp.json()
        detail = body.get("detail", {})
        assert detail.get("error") == "local_edits_present"

        # 8. Verify ZERO writes were made to target tenant
        name_after = session.execute(
            text(f'SELECT display_name FROM "{TGT_SCHEMA}"."accounts" WHERE id = :aid'),
            {"aid": tgt_aid},
        ).scalar()
        assert name_after == "Locally Customized Brand", "Zero writes must occur on 409 rejection"
    finally:
        session.close()


def test_5_refresh_never_deletes_referenced_vanished_persona():
    """Validates that:
    Refresh never deletes a vanished persona that has an introduction link in introductions.persona_id.
    """
    session = SessionLocal()
    try:
        domain = f"refresh-ref-{uuid.uuid4().hex[:6]}.com"
        acct_key = f"acct_ref_{uuid.uuid4().hex[:6]}"
        lob_key = f"lob_ref_{uuid.uuid4().hex[:6]}"
        per1_key = f"per_van_{uuid.uuid4().hex[:6]}"
        per2_key = f"per_ret_{uuid.uuid4().hex[:6]}"

        # 1. Seed in source tenant with 2 personas
        src_aid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts" (key, display_name, domain, extracted_at, created_at, updated_at)
                VALUES (:k, 'Referenced Test Corp', :d, NOW(), NOW(), NOW())
                RETURNING id
            """),
            {"k": acct_key, "d": domain},
        ).scalar()

        src_lid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."lobs" (account_id, key, lob_name)
                VALUES (:aid, :k, 'Operations')
                RETURNING id
            """),
            {"aid": src_aid, "k": lob_key},
        ).scalar()

        src_p1 = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."personas" (account_id, lob_id, key, first_name, last_name, display_name)
                VALUES (:aid, :lid, :k, 'John', 'Doe', 'John Doe')
                RETURNING id
            """),
            {"aid": src_aid, "lid": src_lid, "k": per1_key},
        ).scalar()

        src_p2 = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."personas" (account_id, lob_id, key, first_name, last_name, display_name)
                VALUES (:aid, :lid, :k, 'Jane', 'Smith', 'Jane Smith')
                RETURNING id
            """),
            {"aid": src_aid, "lid": src_lid, "k": per2_key},
        ).scalar()
        session.commit()

        # 2. Publish snapshot v1
        _, super_uid = _get_super_token_and_user_id()
        snap_v1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()

        # 3. Hydrate into target
        _, admin_uid = _get_tgt_admin_token_and_user_id()
        hyd_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=TGT_SLUG,
            snapshot_id=snap_v1.id,
            user_id=admin_uid,
            mode="instant",
            fee_credits=0,
        )
        session.commit()
        tgt_aid = hyd_res["account_id"]

        tgt_p1 = session.execute(
            text(f'SELECT id FROM "{TGT_SCHEMA}"."personas" WHERE account_id = :aid AND key = :k'),
            {"aid": tgt_aid, "k": per1_key},
        ).scalar()
        assert tgt_p1 is not None

        # 4. Link an introduction to persona 1 in target tenant
        conn_id = session.execute(
            text(f'INSERT INTO "{TGT_SCHEMA}"."connectors" (name, kind, active) VALUES (\'Test Connector\', \'partner\', true) RETURNING id')
        ).scalar()
        session.execute(
            text(f"""
                INSERT INTO "{TGT_SCHEMA}"."introductions" (connector_id, account_id, persona_id, status)
                VALUES (:cid, :aid, :pid, 'accepted')
            """),
            {"cid": conn_id, "aid": tgt_aid, "pid": tgt_p1},
        )
        # Ensure local_edits remains false for this account_origin
        session.execute(
            text(f'UPDATE "{TGT_SCHEMA}"."account_origin" SET local_edits = false WHERE account_id = :aid'),
            {"aid": tgt_aid},
        )
        session.commit()

        # 5. In source tenant, remove persona 1 and publish snapshot v2
        session.execute(text(f'DELETE FROM "{SRC_SCHEMA}"."personas" WHERE id = :pid'), {"pid": src_p1})
        session.commit()

        snap_v2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()

        # 6. Target refreshes to v2
        ref_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=TGT_SLUG,
            snapshot_id=snap_v2.id,
            user_id=admin_uid,
            mode="refresh",
            fee_credits=0,
        )
        session.commit()

        assert ref_res["ok"] is True
        manifest = ref_res["manifest"]
        assert tgt_p1 in manifest["retained"]["personas"], "Referenced persona must be retained"

        # 7. Verify persona 1 is STILL in database
        check_p1 = session.execute(
            text(f'SELECT id FROM "{TGT_SCHEMA}"."personas" WHERE id = :pid'),
            {"pid": tgt_p1},
        ).scalar()
        assert check_p1 == tgt_p1, "Vanished persona referenced by introductions must NOT be deleted"
    finally:
        session.close()


def test_6_rollback_on_refresh_event_is_blocked():
    """Validates that:
    Rollback on a refresh share event (mode == 'refresh') fails with RollbackBlockedError.
    """
    session = SessionLocal()
    try:
        domain = f"refresh-rollback-{uuid.uuid4().hex[:6]}.com"
        acct_key = f"acct_rlb_{uuid.uuid4().hex[:6]}"

        # 1. Seed in source
        src_aid = session.execute(
            text(f"""
                INSERT INTO "{SRC_SCHEMA}"."accounts" (key, display_name, domain, extracted_at, created_at, updated_at)
                VALUES (:k, 'Rollback Blocked Corp', :d, NOW(), NOW(), NOW())
                RETURNING id
            """),
            {"k": acct_key, "d": domain},
        ).scalar()
        session.commit()

        # 2. Publish snapshot v1
        _, super_uid = _get_super_token_and_user_id()
        snap_v1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()

        # 3. Hydrate into target
        _, admin_uid = _get_tgt_admin_token_and_user_id()
        AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=TGT_SLUG,
            snapshot_id=snap_v1.id,
            user_id=admin_uid,
            mode="instant",
            fee_credits=0,
        )
        session.commit()

        # 4. Publish v2 and refresh
        snap_v2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=src_aid,
            published_by_user_id=super_uid,
            force=True,
        )
        session.commit()

        ref_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            ctx=TGT_SLUG,
            snapshot_id=snap_v2.id,
            user_id=admin_uid,
            mode="refresh",
            fee_credits=0,
        )
        session.commit()
        refresh_event_id = ref_res["share_event_id"]

        # 5. Direct service call raises RollbackBlockedError
        with pytest.raises(RollbackBlockedError) as exc_info:
            AccountReuseService.rollback_account_reuse(
                session=session,
                share_event_id=refresh_event_id,
                admin_user_id=super_uid,
            )
        session.rollback()
        assert "refresh operations is not supported" in str(exc_info.value).lower()

        # 6. HTTP endpoint returns 400
        super_token, _ = _get_super_token_and_user_id()
        client = TestClient(app)
        resp = client.post(
            f"/api/catalog/share-events/{refresh_event_id}/rollback",
            headers={"Authorization": f"Bearer {super_token}"},
            json={"reason": "Testing rollback rejection on refresh"},
        )
        assert resp.status_code == 400
        assert "refresh operations is not supported" in resp.json().get("detail", {}).get("message", "").lower()
    finally:
        session.close()
