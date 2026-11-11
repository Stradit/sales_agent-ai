"""Tests for Phase 7 Multi-Tenant Operational Tooling (Telemetry, Export, Offboarding)."""

import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from sqlalchemy import text

from db.connection import engine as default_engine
from db.provisioning import create_client_schema
from scripts.tenant_ops import TenantOpsManager


@pytest.fixture(scope="module")
def ops_mgr():
    return TenantOpsManager()


def test_tenant_metrics_collection_all(ops_mgr):
    metrics = ops_mgr.get_tenant_metrics()
    assert len(metrics) >= 3
    slugs = [m["slug"] for m in metrics]
    assert "stradit_india" in slugs
    assert "acme_corp" in slugs

    stradit = next(m for m in metrics if m["slug"] == "stradit_india")
    assert stradit["exists"] is True
    assert stradit["accounts_count"] == 6
    assert stradit["total_size_bytes"] > 0
    assert "MB" in stradit["total_size_pretty"]


def test_tenant_metrics_collection_single(ops_mgr):
    metrics = ops_mgr.get_tenant_metrics(slug="stradit_india")
    assert len(metrics) == 1
    assert metrics[0]["slug"] == "stradit_india"


def test_tenant_data_export_structure(ops_mgr):
    export = ops_mgr.export_tenant_data(slug="acme_corp")
    assert "version" in export
    assert "client" in export
    assert export["client"]["slug"] == "acme_corp"
    assert "user_grants" in export
    assert "credit_ledger" in export
    assert "tables" in export
    assert "accounts" in export["tables"]
    assert export["tables"]["accounts"]["count"] == 1


def test_offboard_primary_tenant_forbidden(ops_mgr):
    with pytest.raises(ValueError, match="primary platform tenant"):
        ops_mgr.offboard_tenant(slug="stradit_india")


def test_offboard_active_tenant_without_force_fails(ops_mgr):
    with pytest.raises(ValueError, match="is currently ACTIVE"):
        ops_mgr.offboard_tenant(slug="acme_corp", force=False)


def test_offboard_and_archive_lifecycle():
    """Hermetic test of tenant provisioning, offboarding, and schema archiving."""
    slug = "tmp_offboard_test"
    schema_name = f"client_{slug}"

    # Provision temporary tenant
    create_client_schema(slug)
    with default_engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO public.clients (name, slug, schema_name, plan_tier, is_active, status)
            VALUES ('Offboard Test Org', :s, :sch, 'starter', true, 'active')
            ON CONFLICT (slug) DO UPDATE SET status = 'active', is_active = true
        """), {"s": slug, "sch": schema_name})

    mgr = TenantOpsManager()

    try:
        # Offboard with force
        res = mgr.offboard_tenant(slug, force=True, drop_permanently=False)
        assert res["ok"] is True
        assert res["status"] == "deleted"
        archived_schema = res["archived_schema"]
        assert archived_schema.startswith(f"purged_{slug}_")

        # Verify client status is deleted in public.clients
        with default_engine.connect() as conn:
            c_status = conn.execute(text("SELECT status, is_active FROM public.clients WHERE slug = :s"), {"s": slug}).first()
            assert c_status[0] == "deleted"
            assert c_status[1] is False

            # Verify original schema no longer exists, but archive schema does
            orig_exists = conn.execute(text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"), {"s": schema_name}).scalar()
            arch_exists = conn.execute(text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"), {"s": archived_schema}).scalar()
            assert not orig_exists
            assert arch_exists

    finally:
        # Clean up temporary test client and schemas
        with default_engine.begin() as conn:
            conn.execute(text("DELETE FROM public.clients WHERE slug = :s"), {"s": slug})
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
            if 'archived_schema' in locals() and archived_schema:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{archived_schema}" CASCADE'))
