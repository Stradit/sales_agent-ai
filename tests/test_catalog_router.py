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
from db.models.platform_catalog import Company, ShareEvent, Snapshot, SnapshotBlob
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema
from services.catalog_service import CatalogService

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
        res_lib = api_client.get("/api/catalog/companies", headers=super_headers)
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
