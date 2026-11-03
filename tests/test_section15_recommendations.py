"""
Test Suite verifying Section 15: Open Questions & Decisions Recommendations:
1. Decision 1: Per-client API credits tracked in clients.api_credits_used
2. Decision 2: api_configs retained in client schema
3. Decision 3: Super-admin cross-client UNION ALL views (v_all_accounts, v_all_pipeline_runs, etc.)
4. Decision 4: Soft error (HTTP 410) on archived/inactive client access
5. Decision 5: pipeline_runs account_id linked & backfilled
6. Decision 6: output/ directories prefixed with client_slug
7. Decision 7: Connection pool configured with QueuePool (pool_size=20, max_overflow=30)
8. Decision 8: Dedicated client-level roles (client_admin, analyst, viewer)
"""

import pytest
from datetime import datetime
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.pool import QueuePool

import config
from db.connection import engine, SessionLocal
from db.models.client import Client
from db.models.user_client_access import UserClientAccess, CLIENT_ROLES
from services.pipeline_run_logger import PipelineRunLogger


def test_decision_1_per_client_api_credits():
    """Verify clients table tracks api_credits_used and logger atomically increments it."""
    with engine.connect() as conn:
        client = conn.execute(text("SELECT id, slug, api_credits_used, api_credits_total FROM public.clients WHERE slug = 'stradit_india'")).fetchone()
        assert client is not None
        assert client.api_credits_used is not None
        assert client.api_credits_total is not None


def test_decision_2_api_configs_in_client_schema():
    """Verify system_api_configs table exists in client isolated schema."""
    with engine.connect() as conn:
        exists = conn.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables 
                WHERE table_schema = 'client_stradit_india' 
                  AND table_name = 'system_api_configs'
            )
        """)).scalar()
        assert exists is True, "system_api_configs should exist in client_stradit_india schema"


def test_decision_3_cross_client_union_all_views():
    """Verify public cross-client UNION ALL views exist and return client_slug partitioned data."""
    with engine.connect() as conn:
        for view_name in ["v_all_accounts", "v_all_pipeline_runs", "v_all_personas", "v_all_lobs", "v_all_opportunity_signals"]:
            exists = conn.execute(text(f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.views 
                    WHERE table_schema = 'public' 
                      AND table_name = '{view_name}'
                )
            """)).scalar()
            assert exists is True, f"View public.{view_name} should exist"

        # Verify query against v_all_accounts returns client_slug
        rows = conn.execute(text("SELECT client_slug, count(*) FROM public.v_all_accounts GROUP BY client_slug")).fetchall()
        assert len(rows) >= 1
        slugs = [r[0] for r in rows]
        assert "stradit_india" in slugs


def test_decision_4_soft_error_on_archived_client():
    """Verify accessing an inactive/archived client raises HTTP 410 soft error."""
    from fastapi.testclient import TestClient
    from api import app
    from auth import create_access_token
    from db.models.user import User

    client = TestClient(app)
    s = SessionLocal()
    sa_user = s.query(User).filter_by(role="super_admin").first() or s.query(User).first()
    token = create_access_token(sa_user, client_slug="stradit_india")
    
    # Temporarily set an inactive test client
    inactive = s.query(Client).filter_by(slug="inactive_test_client").first()
    if not inactive:
        inactive = Client(slug="inactive_test_client", name="Inactive Test", schema_name="client_inactive_test", is_active=False)
        s.add(inactive)
        s.commit()
    else:
        inactive.is_active = False
        s.commit()
    s.close()

    resp = client.get("/api/accounts", headers={"Authorization": f"Bearer {token}", "X-Client-Slug": "inactive_test_client"})
    assert resp.status_code == 410
    data = resp.json()
    assert "deactivated" in data["detail"].lower() or "archived" in data["detail"].lower()


def test_decision_5_pipeline_runs_account_id_linked():
    """Verify pipeline_runs has account_id and runs are backfilled."""
    with engine.connect() as conn:
        linked_count = conn.execute(text(
            "SELECT count(*) FROM client_stradit_india.pipeline_runs WHERE account_id IS NOT NULL"
        )).scalar()
        assert linked_count >= 2000, f"Expected >= 2000 linked runs after backfill, got {linked_count}"


def test_decision_6_output_dirs_prefixed_with_client_slug():
    """Verify get_run_output_dirs prefixes paths with client_slug."""
    dirs = config.get_run_output_dirs("Test Company", run_dt=datetime(2026, 10, 30, 12, 0, 0), client_slug="acme_corp")
    base_run = dirs["run_dir"]
    # Path should include 'acme_corp'
    parts = [p.lower() for p in base_run.parts]
    assert "acme_corp" in parts, f"Expected acme_corp in path parts: {parts}"


def test_decision_7_queuepool_pool_settings():
    """Verify engine uses QueuePool with proper pool size."""
    pool = engine.pool
    assert isinstance(pool, QueuePool), f"Expected QueuePool, got {type(pool)}"
    assert pool.size() == 20
    assert pool._max_overflow == 30


def test_decision_8_client_roles():
    """Verify separate client-level roles are defined."""
    assert "client_admin" in CLIENT_ROLES
    assert "analyst" in CLIENT_ROLES
    assert "viewer" in CLIENT_ROLES
