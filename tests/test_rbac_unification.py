"""Comprehensive Unit & Integration Test Suite for Unified RBAC & Permission Enforcement.

Tests the implementation of:
1. `get_effective_role(session, user, client_slug)`:
   - super_admin omnipotence
   - global read-only cap (viewer, partner)
   - tenant-scoped UserClientAccess.role resolution
2. `require_permission(permission)`:
   - role hierarchy (super_admin > client_admin > analyst > viewer)
   - client:manage enforcement
   - auto_pipeline:run enforcement
   - catalog:view_library platform isolation
3. `require_client_role(*roles)`:
   - rejection on missing client context (no default stradit fallback)
   - tenant isolation across client boundaries
4. Guarded auto-pipeline & telemetry endpoints in `api.py`
"""

import sys
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

import auth
from api import app
from db.connection import SessionLocal
from db.models import User, Client, UserClientAccess

test_http_client = TestClient(app)


@pytest.fixture(scope="module")
def setup_rbac_test_entities():
    """Sets up deterministic test users and clients in the PostgreSQL test database."""
    session = SessionLocal()
    try:
        # 1. Super admin user
        super_admin = session.query(User).filter_by(email="rbac_super_admin@test.local").first()
        if not super_admin:
            super_admin = User(
                email="rbac_super_admin@test.local",
                hashed_password=auth.hash_password("testpass123"),
                full_name="RBAC Super Admin",
                role="super_admin",
                is_active=True,
            )
            session.add(super_admin)

        # 2. Regular user (sales rep) who will be client_admin for client A
        rep_admin = session.query(User).filter_by(email="rbac_client_admin@test.local").first()
        if not rep_admin:
            rep_admin = User(
                email="rbac_client_admin@test.local",
                hashed_password=auth.hash_password("testpass123"),
                full_name="RBAC Client Admin User",
                role="user",
                is_active=True,
            )
            session.add(rep_admin)

        # 3. Regular user who will be analyst for client A
        rep_analyst = session.query(User).filter_by(email="rbac_analyst@test.local").first()
        if not rep_analyst:
            rep_analyst = User(
                email="rbac_analyst@test.local",
                hashed_password=auth.hash_password("testpass123"),
                full_name="RBAC Analyst User",
                role="user",
                is_active=True,
            )
            session.add(rep_analyst)

        # 4. Global viewer who attempts to escalate
        global_viewer = session.query(User).filter_by(email="rbac_global_viewer@test.local").first()
        if not global_viewer:
            global_viewer = User(
                email="rbac_global_viewer@test.local",
                hashed_password=auth.hash_password("testpass123"),
                full_name="RBAC Global Viewer",
                role="viewer",
                is_active=True,
            )
            session.add(global_viewer)

        # 5. Client A & Client B
        client_a = session.query(Client).filter_by(slug="rbac_test_client_a").first()
        if not client_a:
            client_a = Client(
                name="RBAC Test Client A",
                slug="rbac_test_client_a",
                schema_name="client_rbac_test_a",
                is_active=True,
                status="active",
            )
            session.add(client_a)

        client_b = session.query(Client).filter_by(slug="rbac_test_client_b").first()
        if not client_b:
            client_b = Client(
                name="RBAC Test Client B",
                slug="rbac_test_client_b",
                schema_name="client_rbac_test_b",
                is_active=True,
                status="active",
            )
            session.add(client_b)

        session.commit()
        session.refresh(super_admin)
        session.refresh(rep_admin)
        session.refresh(rep_analyst)
        session.refresh(global_viewer)
        session.refresh(client_a)
        session.refresh(client_b)

        # 6. Assign UserClientAccess grants
        # rep_admin -> client_admin for client A ONLY
        grant1 = session.query(UserClientAccess).filter_by(user_id=rep_admin.id, client_id=client_a.id).first()
        if not grant1:
            session.add(UserClientAccess(user_id=rep_admin.id, client_id=client_a.id, role="client_admin"))

        # rep_analyst -> analyst for client A ONLY
        grant2 = session.query(UserClientAccess).filter_by(user_id=rep_analyst.id, client_id=client_a.id).first()
        if not grant2:
            session.add(UserClientAccess(user_id=rep_analyst.id, client_id=client_a.id, role="analyst"))

        # global_viewer -> granted client_admin in client A (to test that global cap wins)
        grant3 = session.query(UserClientAccess).filter_by(user_id=global_viewer.id, client_id=client_a.id).first()
        if not grant3:
            session.add(UserClientAccess(user_id=global_viewer.id, client_id=client_a.id, role="client_admin"))

        # Grant rep_admin access to stradit_india for live telemetry test
        stradit_client = session.query(Client).filter_by(slug="stradit_india").first()
        if stradit_client:
            g_stradit = session.query(UserClientAccess).filter_by(user_id=rep_admin.id, client_id=stradit_client.id).first()
            if not g_stradit:
                session.add(UserClientAccess(user_id=rep_admin.id, client_id=stradit_client.id, role="client_admin"))

        session.commit()

        yield {
            "super_admin_id": super_admin.id,
            "client_admin_id": rep_admin.id,
            "analyst_id": rep_analyst.id,
            "global_viewer_id": global_viewer.id,
            "client_a_slug": client_a.slug,
            "client_b_slug": client_b.slug,
        }
    finally:
        session.close()


def test_effective_role_super_admin(setup_rbac_test_entities):
    """super_admin always yields 'super_admin' across any client context."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=setup_rbac_test_entities["super_admin_id"]).first()
        original_enforced = auth.AUTH_ENFORCED
        auth.AUTH_ENFORCED = True
        try:
            assert auth.get_effective_role(session, user) == "super_admin"
            assert auth.get_effective_role(session, user, "rbac_test_client_a") == "super_admin"
            assert auth.get_effective_role(session, user, "rbac_test_client_b") == "super_admin"
            assert auth.get_effective_role(session, user, "non_existent_client") == "super_admin"
        finally:
            auth.AUTH_ENFORCED = original_enforced
    finally:
        session.close()


def test_effective_role_client_admin(setup_rbac_test_entities):
    """client_admin role resolves properly for granted client, but is None for ungranted client."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=setup_rbac_test_entities["client_admin_id"]).first()
        original_enforced = auth.AUTH_ENFORCED
        auth.AUTH_ENFORCED = True
        try:
            # For client_a: granted as client_admin
            role_a = auth.get_effective_role(session, user, "rbac_test_client_a")
            assert role_a == "client_admin", f"Expected client_admin, got {role_a}"

            # For client_b: has NO grant
            role_b = auth.get_effective_role(session, user, "rbac_test_client_b")
            assert role_b is None, f"Expected None for unauthorized client, got {role_b}"
        finally:
            auth.AUTH_ENFORCED = original_enforced
    finally:
        session.close()


def test_effective_role_global_read_only_cap(setup_rbac_test_entities):
    """Global viewer/partner cap strictly enforces 'viewer' even if tenant grant says client_admin."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=setup_rbac_test_entities["global_viewer_id"]).first()
        original_enforced = auth.AUTH_ENFORCED
        auth.AUTH_ENFORCED = True
        try:
            # Even though UserClientAccess for client_a has role="client_admin",
            # the user's global role is "viewer", which MUST cap effective role to "viewer".
            role_a = auth.get_effective_role(session, user, "rbac_test_client_a")
            assert role_a == "viewer", f"Expected global cap 'viewer', got {role_a}"
        finally:
            auth.AUTH_ENFORCED = original_enforced
    finally:
        session.close()


def test_require_permission_hierarchy():
    """Verify that permission role hierarchy works:
    - client_admin satisfies analyst and viewer permissions
    - analyst satisfies viewer permission
    """
    assert "super_admin" in auth.PERMISSION_ROLES["catalog:view_library"]
    assert "client_admin" not in auth.PERMISSION_ROLES["catalog:view_library"]

    assert "client_admin" in auth.PERMISSION_ROLES["client:manage"]
    assert "super_admin" in auth.PERMISSION_ROLES["client:manage"]
    assert "analyst" not in auth.PERMISSION_ROLES["client:manage"]

    assert "analyst" in auth.PERMISSION_ROLES["auto_pipeline:run"]
    assert "client_admin" in auth.PERMISSION_ROLES["auto_pipeline:run"]


def test_require_client_role_no_default_stradit_fallback():
    """require_client_role must reject with 400 when client context is missing entirely."""
    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        from unittest.mock import MagicMock
        req = MagicMock()
        req.headers = {}
        req.path_params = {}
        req.query_params = {}

        dummy_user = User(id=999, role="user", email="dummy@test.com")
        dummy_user.token_client_slug = None

        dep = auth.require_client_role("client_admin", "analyst")
        with pytest.raises(HTTPException) as exc_info:
            dep(req, dummy_user)

        assert exc_info.value.status_code == 400
        assert "Client context required" in exc_info.value.detail
    finally:
        auth.AUTH_ENFORCED = original_enforced


def test_client_admin_can_manage_own_client(setup_rbac_test_entities):
    """client_admin of client_a CAN manage users in client_a via client_router."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=setup_rbac_test_entities["client_admin_id"]).first()
        token = auth.create_access_token(user)
    finally:
        session.close()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Slug": "rbac_test_client_a",
    }

    # Calling /platform-users to see registered users for assignment
    res = test_http_client.get("/api/clients/platform-users", headers=headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert "users" in data


def test_client_admin_cannot_manage_foreign_client(setup_rbac_test_entities):
    """client_admin of client_a is rejected with 403 when attempting to manage client_b."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=setup_rbac_test_entities["client_admin_id"]).first()
        analyst_user = session.query(User).filter_by(id=setup_rbac_test_entities["analyst_id"]).first()
        token = auth.create_access_token(user)
    finally:
        session.close()

    # Attempting to add a user to client_b
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Slug": "rbac_test_client_b",
    }

    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        res = test_http_client.post(
            "/api/clients/rbac_test_client_b/users",
            json={"user_id": analyst_user.id, "role": "analyst"},
            headers=headers,
        )
        assert res.status_code == 403, f"Expected 403 Forbidden, got {res.status_code}: {res.text}"
    finally:
        auth.AUTH_ENFORCED = original_enforced


def test_system_telemetry_guarded(setup_rbac_test_entities):
    """System telemetry endpoints require authentication when AUTH_ENFORCED=True."""
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=setup_rbac_test_entities["client_admin_id"]).first()
        token = auth.create_access_token(user)
    finally:
        session.close()

    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        # Without auth header -> 401
        res = test_http_client.get("/api/system/pipeline-overview")
        assert res.status_code == 401, f"Expected 401 Unauthorized without token, got {res.status_code}"

        # With auth header and client slug -> 200
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Client-Slug": "stradit_india",
        }
        res_auth = test_http_client.get("/api/system/pipeline-overview", headers=headers)
        assert res_auth.status_code == 200, f"Expected 200 with valid token, got {res_auth.status_code}: {res_auth.text}"
    finally:
        auth.AUTH_ENFORCED = original_enforced


def test_auto_pipeline_blocks_viewer_role(setup_rbac_test_entities):
    """Auto pipeline endpoints reject users with read-only viewer role (403)."""
    session = SessionLocal()
    try:
        viewer = session.query(User).filter_by(id=setup_rbac_test_entities["global_viewer_id"]).first()
        token = auth.create_access_token(viewer)
    finally:
        session.close()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Client-Slug": "rbac_test_client_a",
    }

    original_enforced = auth.AUTH_ENFORCED
    auth.AUTH_ENFORCED = True
    try:
        res = test_http_client.post(
            "/api/accounts/1/auto-pipeline",
            headers=headers,
        )
        assert res.status_code == 403, f"Expected 403 Forbidden for viewer, got {res.status_code}: {res.text}"
    finally:
        auth.AUTH_ENFORCED = original_enforced
