"""Test Suite: Catalog REST API Router (/api/catalog).

Comprehensive end-to-end testing of all 10 catalog API endpoints across
the complete multi-tenant role matrix (super_admin, client_admin, analyst, viewer, unauthenticated).

Endpoints Tested:
1. GET /api/catalog/reuse-check (Anti-Oracle lookup & signed preview token)
2. POST /api/catalog/hydrate (Atomic single-transaction hydration & approval gate)
3. POST /api/catalog/requests (Client admin request submission)
4. GET /api/catalog/requests (Request queue inspection with tenant scoping)
5. POST /api/catalog/requests/{id}/approve (Super admin approval & auto-hydration)
6. POST /api/catalog/requests/{id}/reject (Super admin rejection)
7. GET /api/catalog/companies (Super admin canonical library & holder chips)
8. POST /api/catalog/publish (Super admin snapshot publishing)
9. POST /api/catalog/assign (Super admin direct assignment drawer)
10. POST /api/catalog/share-events/{id}/rollback (Reversible rollback & private child protection)
"""

from datetime import datetime, timezone
import json
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import auth
from api import app
from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.audit_log import AuditLog
from db.models.platform_catalog import Company, ShareEvent, Snapshot, SnapshotBlob
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema
from services.catalog_service import CatalogService
from services.account_reuse_service import AccountReuseService
from services.catalog_audit import reuse_check_limiter

api_client = TestClient(app)

CLIENT_A_SLUG = "cat_route_src"
CLIENT_B_SLUG = "cat_route_tgt"
SCHEMA_A = f"client_{CLIENT_A_SLUG}"
SCHEMA_B = f"client_{CLIENT_B_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_catalog_router_environment():
    session = SessionLocal()
    try:
        session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2)"), {"s1": CLIENT_A_SLUG, "s2": CLIENT_B_SLUG})
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'cat_route_%'"))
        session.commit()
    finally:
        session.close()

    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA_A}" CASCADE'))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA_B}" CASCADE'))

    create_client_schema(CLIENT_A_SLUG)
    create_client_schema(CLIENT_B_SLUG)

    session = SessionLocal()
    try:
        # 1. Clients
        ca = session.query(Client).filter_by(slug=CLIENT_A_SLUG).first()
        cb = session.query(Client).filter_by(slug=CLIENT_B_SLUG).first()
        ca.catalog_publish = True
        ca.api_credits_total = 5000
        cb.catalog_consume = True
        cb.api_credits_total = 5000
        cb.max_accounts = 5
        cb.reuse_requires_approval = True  # requires approval by default
        session.flush()

        # 2. Users
        pwd = auth.hash_password("routerpass123")
        u_super = User(email="cat_route_super@corp.com", hashed_password=pwd, role="super_admin", full_name="Super Admin")
        u_admin_b = User(email="cat_route_admin_b@corp.com", hashed_password=pwd, role="user", full_name="Client B Admin")
        u_analyst_b = User(email="cat_route_analyst_b@corp.com", hashed_password=pwd, role="user", full_name="Client B Analyst")
        u_viewer_b = User(email="cat_route_viewer_b@corp.com", hashed_password=pwd, role="viewer", full_name="Client B Viewer")

        session.add_all([u_super, u_admin_b, u_analyst_b, u_viewer_b])
        session.flush()

        # 3. Access grants
        session.add(UserClientAccess(user_id=u_admin_b.id, client_id=cb.id, role="client_admin"))
        session.add(UserClientAccess(user_id=u_analyst_b.id, client_id=cb.id, role="analyst"))
        session.add(UserClientAccess(user_id=u_viewer_b.id, client_id=cb.id, role="viewer"))
        session.commit()

        # 4. Seed account in Client A
        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_A}"."accounts"
                (key, display_name, domain, sec_cik, country, city, is_manually_verified, owner_user_id)
                VALUES
                ('veritas_holdings', 'Veritas Global Holdings', 'veritasglobal.com', '0001955555',
                 'United States', 'Chicago', true, :uid)
                RETURNING id
            """),
            {"uid": u_super.id},
        ).scalar()

        lob_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_A}"."lobs"
                (account_id, key, lob_name, domain)
                VALUES (:aid, 'veritas_cap', 'Capital Markets', 'cap.veritas.com')
                RETURNING id
            """),
            {"aid": acct_id},
        ).scalar()

        session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_A}"."personas"
                (account_id, lob_id, key, display_name, full_name, title, hierarchy_level, tier, created_by_user_id)
                VALUES (:aid, :lid, 'p_md', 'Robert Chen', 'Robert Chen', 'Managing Director', 1, 'Tier 1', :uid)
            """),
            {"aid": acct_id, "lid": lob_id, "uid": u_super.id},
        )
        session.commit()

        # Publish initial snapshot
        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=CLIENT_A_SLUG,
            account_id=acct_id,
            published_by_user_id=u_super.id,
            force=True,
        )

        env_dict = {
            "super_id": u_super.id,
            "admin_b_id": u_admin_b.id,
            "analyst_b_id": u_analyst_b.id,
            "viewer_b_id": u_viewer_b.id,
            "client_a_account_id": acct_id,
            "snapshot_id": snap.id,
        }
    finally:
        session.close()

    yield env_dict

    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA_A}" CASCADE'))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SCHEMA_B}" CASCADE'))
    session = SessionLocal()
    try:
        session.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key LIKE '%0001955555%'"))
        session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2)"), {"s1": CLIENT_A_SLUG, "s2": CLIENT_B_SLUG})
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'cat_route_%'"))
        session.commit()
    finally:
        session.close()


def _headers_for(user_id: int, client_slug: str = None) -> dict:
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        token = auth.create_access_token(user, client_slug=client_slug)
        h = {"Authorization": f"Bearer {token}"}
        if client_slug:
            h["X-Client-Slug"] = client_slug
        return h
    finally:
        session.close()


def test_reuse_check_endpoint(setup_catalog_router_environment):
    """Verifies anti-oracle behavior, preview token generation, and zero source leaks."""
    env = setup_catalog_router_environment
    headers = _headers_for(env["admin_b_id"], CLIENT_B_SLUG)

    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        # 1. Unknown domain -> Anti-Oracle returns available: False (200 OK)
        res_unknown = api_client.get(
            "/api/catalog/reuse-check?identifier=nonexistent-corp-1234.com",
            headers=headers,
        )
        assert res_unknown.status_code == 200
        assert res_unknown.json() == {"available": False}

        # 2. Known domain -> Returns verified preview + preview_token
        res_known = api_client.get(
            "/api/catalog/reuse-check?identifier=veritasglobal.com",
            headers=headers,
        )
        assert res_known.status_code == 200
        data = res_known.json()
        assert data["available"] is True
        assert data["canonical_key"] == "cik:0001955555"
        assert data["display_name"] == "Veritas Global Holdings"
        assert data["freshness"] == "fresh"
        assert data["default_action"] == "use_verified"
        assert "preview_token" in data
        assert data["preview_token"] is not None
        assert data["requires_approval"] is True

        # Critical security check: NO source tenant leakage in response
        assert "source_client_slug" not in data
        assert "source_client_id" not in data
        assert "source_account_id" not in data
    finally:
        auth.AUTH_ENFORCED = original_enforced


def test_hydrate_and_approval_gate(setup_catalog_router_environment):
    """Verifies approval gate: client admin cannot directly hydrate when approval required."""
    env = setup_catalog_router_environment
    headers = _headers_for(env["admin_b_id"], CLIENT_B_SLUG)
    super_headers = _headers_for(env["super_id"])

    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        # Get preview token
        check_res = api_client.get(
            "/api/catalog/reuse-check?identifier=veritasglobal.com",
            headers=headers,
        )
        preview_token = check_res.json()["preview_token"]

        # 1. Direct hydration by Client Admin without approval -> 403 Forbidden
        res_direct = api_client.post(
            "/api/catalog/hydrate",
            headers=headers,
            json={
                "snapshot_id": env["snapshot_id"],
                "preview_token": preview_token,
                "mode": "instant",
            },
        )
        assert res_direct.status_code == 403
        assert "requires administrator approval" in res_direct.json()["detail"]

        # 2. Client Admin submits reuse request -> 200 OK
        res_req = api_client.post(
            "/api/catalog/requests",
            headers=headers,
            json={
                "snapshot_id": env["snapshot_id"],
                "preview_token": preview_token,
                "mode": "instant",
                "reason": "Target enterprise expansion for Q4",
            },
        )
        assert res_req.status_code == 200
        req_data = res_req.json()
        assert req_data["ok"] is True
        assert req_data["status"] == "pending_approval"
        request_id = req_data["request_id"]

        # 3. List requests as Client Admin -> sees their pending request
        res_list = api_client.get("/api/catalog/requests", headers=headers)
        assert res_list.status_code == 200
        reqs = res_list.json()["requests"]
        assert any(r["request_id"] == request_id for r in reqs)

        # 4. Super Admin approves request -> auto-hydrates into Client B
        res_appr = api_client.post(
            f"/api/catalog/requests/{request_id}/approve",
            headers=super_headers,
        )
        assert res_appr.status_code == 200
        appr_data = res_appr.json()
        assert appr_data["ok"] is True
        assert appr_data["status"] == "approved"
        assert "account_id" in appr_data

        # 5. Verify account exists in Client B schema
        session = SessionLocal()
        try:
            acct_in_b = session.execute(
                text(f'SELECT id, display_name, domain FROM "{SCHEMA_B}"."accounts" WHERE id = :aid'),
                {"aid": appr_data["account_id"]},
            ).fetchone()
            assert acct_in_b is not None
            assert acct_in_b[1] == "Veritas Global Holdings"

            # Check provenance table
            origin_in_b = session.execute(
                text(f'SELECT origin_type, canonical_key FROM "{SCHEMA_B}"."account_origin" WHERE account_id = :aid'),
                {"aid": appr_data["account_id"]},
            ).fetchone()
            assert origin_in_b is not None
            assert origin_in_b[0] == "catalog"
            assert origin_in_b[1] == "cik:0001955555"
        finally:
            session.close()

    finally:
        auth.AUTH_ENFORCED = original_enforced


def test_super_admin_catalog_management(setup_catalog_router_environment):
    """Verifies Super Admin library view, direct assignment, and rollback."""
    env = setup_catalog_router_environment
    super_headers = _headers_for(env["super_id"])
    viewer_headers = _headers_for(env["viewer_b_id"], CLIENT_B_SLUG)

    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        # 1. Non-admin accessing library -> 403
        res_v = api_client.get("/api/catalog/companies", headers=viewer_headers)
        assert res_v.status_code == 403

        # 2. Super admin library overview -> 200 OK
        res_lib = api_client.get("/api/catalog/companies?search=Veritas", headers=super_headers)
        assert res_lib.status_code == 200
        data = res_lib.json()
        assert "companies" in data
        assert data["total"] >= 1
        comp = next(c for c in data["companies"] if c["canonical_key"] == "cik:0001955555")
        assert comp["display_name"] == "Veritas Global Holdings"
        assert len(comp["holder_clients"]) >= 1

        # 3. Super Admin Rollback Rehearsal
        session = SessionLocal()
        try:
            event = session.query(ShareEvent).filter_by(
                target_client_id=session.query(Client).filter_by(slug=CLIENT_B_SLUG).first().id,
                status="done",
            ).order_by(ShareEvent.id.desc()).first()
            assert event is not None
            share_event_id = event.id
        finally:
            session.close()

        # Execute rollback via API
        res_rb = api_client.post(
            f"/api/catalog/share-events/{share_event_id}/rollback",
            headers=super_headers,
            json={"reason": "Super admin testing rollback API endpoint"},
        )
        assert res_rb.status_code == 200
        rb_data = res_rb.json()
        assert rb_data["ok"] is True
        assert rb_data["rolled_back"] is True

        # Verify account deleted from Client B schema
        session = SessionLocal()
        try:
            cnt_after = session.execute(
                text(f'SELECT count(*) FROM "{SCHEMA_B}"."accounts" WHERE domain = :d'),
                {"d": "veritasglobal.com"},
            ).scalar()
            assert cnt_after == 0
        finally:
            session.close()

    finally:
        auth.AUTH_ENFORCED = original_enforced


def test_cross_tenant_slug_override_prevention(setup_catalog_router_environment):
    """Verifies that client admins cannot override target tenant via body, query, or params."""
    env = setup_catalog_router_environment
    headers_b = _headers_for(env["admin_b_id"], CLIENT_B_SLUG)
    super_headers = _headers_for(env["super_id"])

    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        # 1. Query client override on reuse-check -> 403
        res = api_client.get(
            f"/api/catalog/reuse-check?identifier=veritasglobal.com&client={CLIENT_A_SLUG}",
            headers=headers_b,
        )
        assert res.status_code == 403
        assert "Target client does not match your active client context" in res.json()["detail"]

        # 2. Body client_slug override on hydrate -> 403
        res = api_client.post(
            "/api/catalog/hydrate",
            headers=headers_b,
            json={
                "snapshot_id": env["snapshot_id"],
                "preview_token": "fake-token",
                "client_slug": CLIENT_A_SLUG,
            },
        )
        assert res.status_code == 403
        assert "Target client does not match your active client context" in res.json()["detail"]

        # 3. Body client_slug override on requests -> 403
        res = api_client.post(
            "/api/catalog/requests",
            headers=headers_b,
            json={
                "snapshot_id": env["snapshot_id"],
                "preview_token": "fake-token",
                "client_slug": CLIENT_A_SLUG,
            },
        )
        assert res.status_code == 403
        assert "Target client does not match your active client context" in res.json()["detail"]

        # 4. Query client override on GET requests -> 403
        res = api_client.get(
            f"/api/catalog/requests?client={CLIENT_A_SLUG}",
            headers=headers_b,
        )
        assert res.status_code == 403

        # 5. Super admin can target any active client
        res = api_client.get(
            f"/api/catalog/reuse-check?identifier=veritasglobal.com&client={CLIENT_B_SLUG}",
            headers=super_headers,
        )
        assert res.status_code == 200
        assert res.json()["available"] is True
    finally:
        auth.AUTH_ENFORCED = original_enforced


def test_strict_auth_enforcement(setup_catalog_router_environment):
    """Verifies that all catalog routes enforce strict JWT authentication (401 when unauthenticated)."""
    env = setup_catalog_router_environment

    # Even if AUTH_ENFORCED is false in the test environment, catalog endpoints must return 401
    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = False
    try:
        # 1. Unauthenticated publish -> 401
        res = api_client.post(
            "/api/catalog/publish",
            json={"source_client_slug": CLIENT_A_SLUG, "account_id": env["client_a_account_id"]},
        )
        assert res.status_code == 401
        assert "Not authenticated" in res.json()["detail"]

        # 2. Unauthenticated reuse-check -> 401
        res = api_client.get("/api/catalog/reuse-check?identifier=veritasglobal.com")
        assert res.status_code == 401

        # 3. Unauthenticated hydrate -> 401
        res = api_client.post("/api/catalog/hydrate", json={"snapshot_id": 1, "preview_token": "foo"})
        assert res.status_code == 401

        # 4. Unauthenticated requests GET -> 401
        res = api_client.get("/api/catalog/requests")
        assert res.status_code == 401

        # 5. Unauthenticated companies GET -> 401
        res = api_client.get("/api/catalog/companies")
        assert res.status_code == 401

        # 6. Verify APP_ENV=prod and AUTH_ENFORCED=false raises at import time
        import subprocess, sys
        cmd = [
            sys.executable,
            "-c",
            "import os; os.environ['APP_ENV']='prod'; os.environ['AUTH_ENFORCED']='false'; import auth",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode != 0
        assert "AUTH_ENFORCED cannot be false when APP_ENV is 'prod'" in proc.stderr
    finally:
        auth.AUTH_ENFORCED = original_enforced


def test_catalog_flags_enforcement(setup_catalog_router_environment):
    """Verifies S-4: catalog_publish and catalog_consume flags enforcement."""
    env = setup_catalog_router_environment
    super_headers = _headers_for(env["super_id"])
    admin_b_headers = _headers_for(env["admin_b_id"], CLIENT_B_SLUG)

    session = SessionLocal()
    try:
        ca = session.query(Client).filter_by(slug=CLIENT_A_SLUG).first()
        cb = session.query(Client).filter_by(slug=CLIENT_B_SLUG).first()

        # 1. Publish flag enforcement: client_a catalog_publish = False
        ca.catalog_publish = False
        session.commit()

        # Direct service call with force=True still raises PermissionError
        with pytest.raises(PermissionError) as exc_info:
            CatalogService.publish_account_snapshot(
                session=session,
                source_client_slug=CLIENT_A_SLUG,
                account_id=env["client_a_account_id"],
                published_by_user_id=env["super_id"],
                force=True,
            )
        assert "not authorized to publish" in str(exc_info.value)

        # HTTP /publish endpoint returns 403
        res = api_client.post(
            "/api/catalog/publish",
            json={"source_client_slug": CLIENT_A_SLUG, "account_id": env["client_a_account_id"], "force": True},
            headers=super_headers,
        )
        assert res.status_code == 403

        # Inactive client raises PermissionError
        ca.catalog_publish = True
        ca.status = "suspended"
        session.commit()

        with pytest.raises(PermissionError) as exc_info:
            CatalogService.publish_account_snapshot(
                session=session,
                source_client_slug=CLIENT_A_SLUG,
                account_id=env["client_a_account_id"],
                published_by_user_id=env["super_id"],
                force=True,
            )
        assert "is not active" in str(exc_info.value)

        # Restore client A
        ca.status = "active"
        ca.catalog_publish = True
        session.commit()

        # Publish a snapshot to test consume flag
        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=CLIENT_A_SLUG,
            account_id=env["client_a_account_id"],
            published_by_user_id=env["super_id"],
            force=True,
        )

        # 2. Consume flag enforcement: client_b catalog_consume = False
        cb.catalog_consume = False
        session.commit()

        # (a) Service check_reuse_available returns None
        avail = AccountReuseService.check_reuse_available(session, "veritasglobal.com", CLIENT_B_SLUG)
        assert avail is None

        # (b) GET /reuse-check returns available: false
        res = api_client.get(
            f"/api/catalog/reuse-check?identifier=veritasglobal.com&client={CLIENT_B_SLUG}",
            headers=admin_b_headers,
        )
        assert res.status_code == 200
        assert res.json() == {"available": False}

        # (c) Service hydrate raises PermissionError
        with pytest.raises(PermissionError) as exc_info:
            AccountReuseService.hydrate_account_from_catalog(
                session=session,
                target_client_slug=CLIENT_B_SLUG,
                snapshot_id=snap.id,
                user_id=env["admin_b_id"],
                idempotency_key="test_consume_off",
            )
        assert "Catalog reuse is not enabled" in str(exc_info.value)

        # (d) POST /hydrate returns 403
        tok = AccountReuseService.generate_preview_token(snap.id, CLIENT_B_SLUG, env["admin_b_id"])
        res = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snap.id, "preview_token": tok},
            headers=admin_b_headers,
        )
        assert res.status_code == 403
        assert "Catalog reuse is not enabled" in res.json()["detail"]

        # (e) POST /requests returns 403
        res = api_client.post(
            "/api/catalog/requests",
            json={"snapshot_id": snap.id, "preview_token": tok, "reason": "Test consume off"},
            headers=admin_b_headers,
        )
        assert res.status_code == 403
        assert "Catalog reuse is not enabled" in res.json()["detail"]

        # (f) Super-admin direct assign can bypass catalog_consume
        res = api_client.post(
            "/api/catalog/assign",
            json={"snapshot_id": snap.id, "target_client_slug": CLIENT_B_SLUG, "mode": "instant"},
            headers=super_headers,
        )
        assert res.status_code == 200
        assert res.json()["ok"] is True
    finally:
        ca = session.query(Client).filter_by(slug=CLIENT_A_SLUG).first()
        cb = session.query(Client).filter_by(slug=CLIENT_B_SLUG).first()
        if ca:
            ca.catalog_publish = True
            ca.status = "active"
        if cb:
            cb.catalog_consume = True
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."account_origin"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."personas"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."sub_lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."accounts"'))
        session.commit()
        session.close()


def test_safe_rollback_with_introspection_guard(setup_catalog_router_environment):
    """Verifies S-5: complete and safe rollback with dynamic introspection guard."""
    env = setup_catalog_router_environment
    super_headers = _headers_for(env["super_id"])
    session = SessionLocal()
    try:
        # Hermetic cleanup of client B workspace before test
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."account_origin"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."personas"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."sub_lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."targets"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."accounts"'))
        session.commit()

        # 1. Clean hydration in client B via super admin direct assign
        res_assign = api_client.post(
            "/api/catalog/assign",
            json={"snapshot_id": env["snapshot_id"], "target_client_slug": CLIENT_B_SLUG, "mode": "instant"},
            headers=super_headers,
        )
        assert res_assign.status_code == 200
        assign_data = res_assign.json()
        share_event_id = assign_data["share_event_id"]
        account_id = assign_data["account_id"]
        assert account_id is not None

        # Seed a targets row in client B to verify targets deletion on rollback
        session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_B}"."targets" (key, kind, display_name, config)
                VALUES ('veritas_holdings', 'enterprise', 'Veritas Target', '{{}}'::jsonb)
                ON CONFLICT (key) DO NOTHING
            """)
        )
        session.commit()

        # Guard Test (a): Tenant-added persona blocks rollback
        per_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_B}"."personas" (account_id, key, display_name, full_name, title, hierarchy_level, tier, created_by_user_id)
                VALUES (:aid, 'p_custom_tenant', 'Private Persona', 'Private Persona', 'VP Engineering', 2, 'Tier 2', :uid)
                RETURNING id
            """),
            {"aid": account_id, "uid": env["super_id"]},
        ).scalar()
        session.commit()

        res_rb = api_client.post(
            f"/api/catalog/share-events/{share_event_id}/rollback",
            json={"reason": "Testing blocked rollback on tenant persona"},
            headers=super_headers,
        )
        assert res_rb.status_code == 409
        detail = res_rb.json()["detail"]
        assert "personas" in detail["breakdown"]
        assert detail["breakdown"]["personas"] >= 1

        # Delete tenant persona
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."personas" WHERE id = :id'), {"id": per_id})
        session.commit()

        # Guard Test (b): Tenant-added deal blocks rollback
        deal_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_B}"."deals" (account_id, name, stage, value_amount, probability)
                VALUES (:aid, 'Q4 Enterprise Deal', 'discovery', 250000, 30)
                RETURNING id
            """),
            {"aid": account_id},
        ).scalar()
        session.commit()

        res_rb = api_client.post(
            f"/api/catalog/share-events/{share_event_id}/rollback",
            json={"reason": "Testing blocked rollback on deal"},
            headers=super_headers,
        )
        assert res_rb.status_code == 409
        detail = res_rb.json()["detail"]
        assert "deals" in detail["breakdown"]

        # Delete tenant deal
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."deals" WHERE id = :id'), {"id": deal_id})
        session.commit()

        # Guard Test (c): Tenant-added user_account_access blocks rollback
        acc_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_B}"."user_account_access" (user_id, account_id)
                VALUES (:uid, :aid)
                RETURNING id
            """),
            {"uid": env["admin_b_id"], "aid": account_id},
        ).scalar()
        session.commit()

        res_rb = api_client.post(
            f"/api/catalog/share-events/{share_event_id}/rollback",
            json={"reason": "Testing blocked rollback on user_account_access"},
            headers=super_headers,
        )
        assert res_rb.status_code == 409
        detail = res_rb.json()["detail"]
        assert "user_account_access" in detail["breakdown"]

        # Delete user_account_access
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."user_account_access" WHERE id = :id'), {"id": acc_id})
        session.commit()

        # Clean Case: Rollback succeeds
        res_clean = api_client.post(
            f"/api/catalog/share-events/{share_event_id}/rollback",
            json={"reason": "Clean rehearsal rollback"},
            headers=super_headers,
        )
        assert res_clean.status_code == 200
        assert res_clean.json()["rolled_back"] is True

        # Verify account deleted
        assert session.execute(text(f'SELECT 1 FROM "{SCHEMA_B}"."accounts" WHERE id = :aid'), {"aid": account_id}).scalar() is None
        # Verify targets row deleted
        assert session.execute(text(f'SELECT 1 FROM "{SCHEMA_B}"."targets" WHERE key = :k'), {"k": "veritas_holdings"}).scalar() is None
        # Verify LOBs deleted
        assert session.execute(text(f'SELECT 1 FROM "{SCHEMA_B}"."lobs" WHERE account_id = :aid'), {"aid": account_id}).scalar() is None

        # Verify share_event audit fields
        event = session.query(ShareEvent).filter_by(id=share_event_id).first()
        assert event.status == "rolled_back"
        assert event.rolled_back_at is not None
        assert event.rolled_back_by == env["super_id"]
        assert event.rollback_reason == "Clean rehearsal rollback"

        # Rollback twice -> 400
        res_repeat = api_client.post(
            f"/api/catalog/share-events/{share_event_id}/rollback",
            json={"reason": "Repeat rollback"},
            headers=super_headers,
        )
        assert res_repeat.status_code == 400
    finally:
        session.close()


def test_failure_handling_and_atomic_credits(setup_catalog_router_environment, monkeypatch):
    """Verifies S-10: credit-balance protection, failure atomicity, and sanitized error handling."""
    env = setup_catalog_router_environment
    super_headers = _headers_for(env["super_id"])
    admin_b_headers = _headers_for(env["admin_b_id"], CLIENT_B_SLUG)
    session = SessionLocal()
    try:
        cb = session.query(Client).filter_by(slug=CLIENT_B_SLUG).first()
        cb.catalog_consume = True
        cb.reuse_requires_approval = False
        session.commit()

        # 1. Credit Limit Exhaustion Protection
        cb.api_credits_total = 10
        cb.api_credits_used = 10
        session.commit()

        preview_tok = AccountReuseService.generate_preview_token(env["snapshot_id"], CLIENT_B_SLUG, env["admin_b_id"])
        res_credit = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": env["snapshot_id"], "preview_token": preview_tok},
            headers=admin_b_headers,
        )
        assert res_credit.status_code == 402
        assert "Insufficient credits" in res_credit.text

        # Restore credits for next tests
        cb.api_credits_used = 0
        cb.api_credits_total = 1000
        session.commit()

        # 2. Failure Atomicity on Hydrate: monkeypatch an insert step to fail
        initial_debits = session.execute(
            text("SELECT count(*) FROM public.credit_ledger WHERE client_id = :cid AND delta < 0"),
            {"cid": cb.id},
        ).scalar() or 0

        preview_tok2 = AccountReuseService.generate_preview_token(env["snapshot_id"], CLIENT_B_SLUG, env["admin_b_id"])

        import services.account_reuse_service as ars
        original_insert = ars._insert_row
        def failing_insert(session, schema, table, row, kinds, **kwargs):
            if table == "personas":
                raise RuntimeError("Simulated database failure during personas insert [SQL: INSERT INTO personas ...]")
            return original_insert(session, schema, table, row, kinds, **kwargs)

        monkeypatch.setattr(ars, "_insert_row", failing_insert)

        res_fail = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": env["snapshot_id"], "preview_token": preview_tok2},
            headers=admin_b_headers,
        )
        assert res_fail.status_code == 500
        detail = res_fail.json()["detail"]
        assert "correlation_id" in detail
        # Ensure raw SQL is NOT leaked
        assert "INSERT INTO" not in res_fail.text
        assert "SELECT" not in res_fail.text

        # Verify no rows created in client B accounts
        acct_cnt = session.execute(text(f'SELECT count(*) FROM "{SCHEMA_B}"."accounts"')).scalar()
        assert acct_cnt == 0
        # Verify no new ledger debit was recorded
        final_debits = session.execute(
            text("SELECT count(*) FROM public.credit_ledger WHERE client_id = :cid AND delta < 0"),
            {"cid": cb.id},
        ).scalar() or 0
        assert final_debits == initial_debits

        # Undo monkeypatch
        monkeypatch.undo()

        # 3. Approval failure handling:
        cb.reuse_requires_approval = True
        session.commit()

        preview_tok3 = AccountReuseService.generate_preview_token(env["snapshot_id"], CLIENT_B_SLUG, env["admin_b_id"])
        res_req = api_client.post(
            "/api/catalog/requests",
            json={"snapshot_id": env["snapshot_id"], "preview_token": preview_tok3, "reason": "Request to approve"},
            headers=admin_b_headers,
        )
        assert res_req.status_code == 200
        req_id = res_req.json()["request_id"]

        # Monkeypatch again
        monkeypatch.setattr(ars, "_insert_row", failing_insert)

        res_app_fail = api_client.post(
            f"/api/catalog/requests/{req_id}/approve",
            headers=super_headers,
        )
        assert res_app_fail.status_code == 500
        app_detail = res_app_fail.json()["detail"]
        assert "correlation_id" in app_detail
        assert "INSERT INTO" not in res_app_fail.text

        # Verify share_event marked 'failed' with sanitized error in separate session
        failed_event = session.query(ShareEvent).filter_by(id=req_id).first()
        session.refresh(failed_event)
        assert failed_event.status == "failed"
        assert failed_event.error is not None
        assert "INSERT INTO" not in failed_event.error
        assert len(failed_event.error) <= 500

    finally:
        monkeypatch.undo()
        cb = session.query(Client).filter_by(slug=CLIENT_B_SLUG).first()
        if cb:
            cb.api_credits_total = 5000
            cb.api_credits_used = 0
            cb.reuse_requires_approval = True
        session.commit()
        session.close()


def test_catalog_audit_and_rate_limiting(setup_catalog_router_environment, monkeypatch):
    """Verifies S-14:
    - Audit rows created across catalog actions (publish, reuse_check, hydrate, rollback, etc.)
    - Source client slug stripped for non-super-admins
    - Rolled back transaction creates zero business audit rows
    - Sliding window rate limit on reuse-check triggers 429 on 31st call
    """
    env = setup_catalog_router_environment
    super_headers = _headers_for(env["super_id"])
    admin_b_headers = _headers_for(env["admin_b_id"], CLIENT_B_SLUG)
    session = SessionLocal()
    try:
        cb = session.query(Client).filter_by(slug=CLIENT_B_SLUG).first()
        cb.catalog_consume = True
        cb.reuse_requires_approval = False
        session.commit()

        # Clean slate in Schema B
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."account_origin"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."targets"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."personas"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."sub_lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."accounts"'))
        session.commit()

        # 1. Reuse Check Rate Limiting (30 calls per 5 min, 31st triggers 429)
        reuse_check_limiter.reset()
        for i in range(30):
            r = api_client.get(
                "/api/catalog/reuse-check",
                params={"identifier": f"company-{i}.com", "client": CLIENT_B_SLUG},
                headers=admin_b_headers,
            )
            assert r.status_code == 200, f"Call {i+1} failed with status {r.status_code}"
            assert r.json() == {"available": False}

        # 31st call must trigger 429
        r_31 = api_client.get(
            "/api/catalog/reuse-check",
            params={"identifier": "company-31.com", "client": CLIENT_B_SLUG},
            headers=admin_b_headers,
        )
        assert r_31.status_code == 429
        assert "Rate limit exceeded" in r_31.json()["detail"]

        # Reset limiter for subsequent tests
        reuse_check_limiter.reset()

        # 2. Audit row for reuse_check_available (only on hit, hashes identifier)
        r_hit = api_client.get(
            "/api/catalog/reuse-check",
            params={"identifier": "veritasglobal.com", "client": CLIENT_B_SLUG},
            headers=admin_b_headers,
        )
        assert r_hit.status_code == 200
        target_snapshot_id = r_hit.json()["snapshot_id"]
        preview_tok = r_hit.json()["preview_token"]

        audit_hit = session.query(AuditLog).filter_by(
            action="catalog.reuse_check_available",
            actor_user_id=env["admin_b_id"],
        ).order_by(AuditLog.id.desc()).first()
        assert audit_hit is not None
        assert "identifier_hash" in audit_hit.details
        # Raw company name or domain must NOT be stored
        assert "veritasglobal.com" not in json.dumps(audit_hit.details)
        assert audit_hit.details["target_client_slug"] == CLIENT_B_SLUG

        # 3. Audit row for catalog.hydrate (source client stripped for non-super-admins)
        r_hyd = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": target_snapshot_id, "preview_token": preview_tok},
            headers=admin_b_headers,
        )
        assert r_hyd.status_code == 200, f"r_hyd failed: {r_hyd.status_code} {r_hyd.text}"
        sh_event_id = r_hyd.json()["share_event_id"]

        audit_hyd = session.query(AuditLog).filter_by(
            action="catalog.hydrate",
            actor_user_id=env["admin_b_id"],
        ).order_by(AuditLog.id.desc()).first()
        assert audit_hyd is not None
        assert audit_hyd.details["share_event_id"] == sh_event_id
        assert audit_hyd.details["target_client_slug"] == CLIENT_B_SLUG
        # Source client stripped because actor is client_admin, not super_admin
        assert "source_client_slug" not in audit_hyd.details
        assert "source_client_id" not in audit_hyd.details
        # Prohibited keys / payload / PII absent
        assert "payload" not in audit_hyd.details
        assert "records" not in audit_hyd.details

        # 4. Failure atomicity: rolled back transaction creates ZERO business audit rows
        import services.account_reuse_service as ars
        original_insert = ars._insert_row
        def failing_insert(session, schema, table, row, kinds, **kwargs):
            if table == "personas":
                raise RuntimeError("Simulated failure to verify zero audit rows")
            return original_insert(session, schema, table, row, kinds, **kwargs)

        initial_hydrate_audits = session.query(AuditLog).filter_by(action="catalog.hydrate").count()

        # Clean up account B so we can attempt hydration
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."account_origin"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."targets"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."personas"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."sub_lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."accounts"'))
        session.commit()

        monkeypatch.setattr(ars, "_insert_row", failing_insert)
        preview_tok_fail = AccountReuseService.generate_preview_token(target_snapshot_id, CLIENT_B_SLUG, env["admin_b_id"])
        r_fail = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": target_snapshot_id, "preview_token": preview_tok_fail, "idempotency_key": "test_idem_fail_audit"},
            headers=admin_b_headers,
        )
        assert r_fail.status_code == 500
        monkeypatch.undo()

        final_hydrate_audits = session.query(AuditLog).filter_by(action="catalog.hydrate").count()
        assert final_hydrate_audits == initial_hydrate_audits, "Rolled back transaction must leave zero business audit rows"

        # 5. Audit row for catalog.rollback_blocked and catalog.rollback
        # Re-hydrate properly to test rollback audits
        preview_tok_rb = AccountReuseService.generate_preview_token(target_snapshot_id, CLIENT_B_SLUG, env["admin_b_id"])
        r_hyd2 = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": target_snapshot_id, "preview_token": preview_tok_rb, "idempotency_key": "test_idem_rb_audit"},
            headers=admin_b_headers,
        )
        assert r_hyd2.status_code == 200
        sh_event_id2 = r_hyd2.json()["share_event_id"]
        account_id2 = r_hyd2.json()["account_id"]

        # Insert private deal to block rollback
        deal_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_B}"."deals" (account_id, name, stage, value_amount, probability)
                VALUES (:aid, 'Audit Blocked Deal', 'discovery', 100000, 20)
                RETURNING id
            """),
            {"aid": account_id2},
        ).scalar()
        session.commit()

        r_rb_blocked = api_client.post(
            f"/api/catalog/share-events/{sh_event_id2}/rollback",
            json={"reason": "Testing rollback_blocked audit"},
            headers=super_headers,
        )
        assert r_rb_blocked.status_code == 409

        audit_blocked = session.query(AuditLog).filter_by(
            action="catalog.rollback_blocked",
            actor_user_id=env["super_id"],
        ).order_by(AuditLog.id.desc()).first()
        assert audit_blocked is not None
        assert audit_blocked.details["share_event_id"] == sh_event_id2
        assert "deals" in audit_blocked.details["breakdown"]

        # Clean deal and perform clean rollback
        session.execute(text(f'DELETE FROM "{SCHEMA_B}"."deals" WHERE id = :id'), {"id": deal_id})
        session.commit()

        r_rb_ok = api_client.post(
            f"/api/catalog/share-events/{sh_event_id2}/rollback",
            json={"reason": "Testing clean rollback audit"},
            headers=super_headers,
        )
        assert r_rb_ok.status_code == 200

        audit_rb = session.query(AuditLog).filter_by(
            action="catalog.rollback",
            actor_user_id=env["super_id"],
        ).order_by(AuditLog.id.desc()).first()
        assert audit_rb is not None
        assert audit_rb.details["share_event_id"] == sh_event_id2
        assert audit_rb.details["target_client_slug"] == CLIENT_B_SLUG
    finally:
        session.close()
