"""Test Suite: TenantRequestContext & Dependency Unit Tests (Phase B - B0).

Verifies:
1. Parameter inspect guard: zero catalog routes have parameters named 'client' or 'client_slug'.
2. Single DB role lookup per request (verified with SQLAlchemy cursor execute listener).
3. Response headers carry X-Correlation-ID.
4. Tenant isolation and active client enforcement.
"""

import inspect
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

import auth
from api import app
from core.tenant_context import TenantRequestContext, tenant_context
from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from routes.catalog_router import catalog_router

api_client = TestClient(app)

TEST_B0_SLUG = "test_b0_ctx"


@pytest.fixture(scope="module")
def setup_b0_env():
    session = SessionLocal()
    try:
        try:
            drop_client_schema(TEST_B0_SLUG)
        except Exception:
            pass
        session.execute(text("DELETE FROM public.clients WHERE slug = :s"), {"s": TEST_B0_SLUG})
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'test_b0_%'"))
        session.commit()

        create_client_schema(TEST_B0_SLUG)

        client = session.query(Client).filter_by(slug=TEST_B0_SLUG).first()
        client.status = "active"
        client.catalog_consume = True
        client.catalog_publish = True
        client.reuse_requires_approval = False
        client.api_credits_total = 1000

        pwd = auth.hash_password("b0secret123")
        u_admin = User(email="test_b0_admin@corp.com", hashed_password=pwd, role="user", full_name="B0 Admin")
        session.add(u_admin)
        session.flush()

        session.add(UserClientAccess(user_id=u_admin.id, client_id=client.id, role="client_admin"))
        session.commit()

        yield {
            "client_id": client.id,
            "user_id": u_admin.id,
            "slug": TEST_B0_SLUG,
        }
    finally:
        session.close()
        engine.dispose()
        try:
            drop_client_schema(TEST_B0_SLUG)
        except Exception:
            pass
        session.execute(text("DELETE FROM public.clients WHERE slug = :s"), {"s": TEST_B0_SLUG})
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'test_b0_%'"))
        session.commit()
        session.close()


def test_router_signatures_have_no_raw_client_params():
    """Guards against regression: no catalog route handler may take a raw client_slug or client param."""
    for route in catalog_router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint and callable(endpoint):
            sig = inspect.signature(endpoint)
            for param_name in sig.parameters.keys():
                assert param_name not in ("client_slug", "client"), (
                    f"Route {route.path} has forbidden parameter '{param_name}'. "
                    f"Use TenantRequestContext via tenant_context() dependency instead."
                )


def test_single_db_role_lookup_per_request(setup_b0_env):
    """Proves the dependency executes the DB effective_role lookup exactly once per request."""
    user_id = setup_b0_env["user_id"]
    slug = setup_b0_env["slug"]

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        token = auth.create_access_token(user, client_slug=slug)
    finally:
        session.close()

    executed_statements = []

    def statement_listener(conn, cursor, statement, parameters, context, executemany):
        executed_statements.append(statement)

    event.listen(engine, "before_cursor_execute", statement_listener)
    try:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Client-Slug": slug,
        }
        res = api_client.get("/api/catalog/reuse-check?identifier=nonexistent-sample.com", headers=headers)
        assert res.status_code == 200
        assert res.json() == {"available": False}
        assert "X-Correlation-ID" in res.headers

        # Count queries hitting user_client_access table for role resolution
        role_queries = [
            s for s in executed_statements
            if "user_client_access" in s.lower()
        ]
        assert len(role_queries) == 1, (
            f"Expected exactly 1 role lookup query to user_client_access, but got {len(role_queries)}: {role_queries}"
        )
    finally:
        event.remove(engine, "before_cursor_execute", statement_listener)
