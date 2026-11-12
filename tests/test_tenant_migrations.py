"""Phase 3 Test Suite: Tenant Manifest, Schema Lifecycle, Drift Detection & Migration Commands.

Validates:
1. TenantManifest accurately parses manifest.yaml, table definitions, primary keys, and foreign keys.
2. TenantManifest.verify_schema_against_manifest checks schema compliance and flags missing tables, PKs, FKs.
3. Client model supports all lifecycle columns (status, skeleton_version, provisioned_at, suspended_at, etc.).
4. SchemaProvisioningService manages the client provisioning state machine (pending -> provisioning -> active/failed).
5. Provisioning failures record last_error, mark status='failed', and preserve the schema for diagnostics.
6. db.migrate tenants status and upgrade commands execute cleanly with support for --dry-run and --only.
"""

import argparse
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
from unittest.mock import patch

from config import DATABASE_URL
from db.connection import engine, SessionLocal
from db.models.client import Client
from db.manifest import TenantManifest
from db.provisioning import (
    CLIENT_TABLES_ORDERED,
    create_client_schema,
    repair_client_schema,
    drop_client_schema,
    SchemaProvisioningService,
)
from db.migrate import cmd_tenants_status, cmd_tenants_upgrade


TEST_SLUG_CLEAN = "test_p3_clean"
TEST_SCHEMA_CLEAN = f"client_{TEST_SLUG_CLEAN}"

TEST_SLUG_DRIFT = "test_p3_drift"
TEST_SCHEMA_DRIFT = f"client_{TEST_SLUG_DRIFT}"

TEST_SLUG_FAIL = "test_p3_fail"
TEST_SCHEMA_FAIL = f"client_{TEST_SLUG_FAIL}"


@pytest.fixture(scope="module", autouse=True)
def setup_teardown_phase3_environment():
    """Sets up isolated schemas in test DB and cleans them up after the module finishes."""
    test_schemas = [TEST_SCHEMA_CLEAN, TEST_SCHEMA_DRIFT, TEST_SCHEMA_FAIL]
    test_slugs = [TEST_SLUG_CLEAN, TEST_SLUG_DRIFT, TEST_SLUG_FAIL]

    with engine.begin() as conn:
        for s in test_schemas:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{s}" CASCADE'))
        for sl in test_slugs:
            conn.execute(text("DELETE FROM public.clients WHERE slug = :sl"), {"sl": sl})

    # Upgrade any baseline tenants in test DB to ensure consistent state
    cmd_tenants_upgrade(argparse.Namespace(only=None, dry_run=False))

    yield

    with engine.begin() as conn:
        for s in test_schemas:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{s}" CASCADE'))
        for sl in test_slugs:
            conn.execute(text("DELETE FROM public.clients WHERE slug = :sl"), {"sl": sl})


class TestTenantManifest:
    """Verifies TenantManifest contract loading and drift inspection."""

    def test_manifest_loads_canonical_contract(self):
        manifest = TenantManifest.load()
        assert manifest is not None
        assert manifest.skeleton_version == TenantManifest.get_skeleton_version()
        assert len(manifest.tables) == len(CLIENT_TABLES_ORDERED)
        assert len(manifest.tables) == manifest.total_tables

        # Verify key domain tables are present in the manifest
        for critical_tbl in ["accounts", "personas", "pipeline_runs", "targets", "business_lines"]:
            assert critical_tbl in manifest.tables
            tbl_def = manifest.tables[critical_tbl]
            assert "primary_key" in tbl_def
            assert tbl_def["primary_key"] is not None

    def test_verify_clean_schema_is_compliant(self):
        # Provision clean schema
        create_client_schema(TEST_SLUG_CLEAN)

        report = TenantManifest.verify_schema_against_manifest(engine, TEST_SCHEMA_CLEAN)
        assert report["is_compliant"] is True
        assert report["drift_detected"] is False
        assert report["tables_found"] == len(CLIENT_TABLES_ORDERED)
        assert report["tables_expected"] == len(CLIENT_TABLES_ORDERED)
        assert len(report["missing_tables"]) == 0
        assert len(report["pk_mismatches"]) == 0
        assert len(report["fk_mismatches"]) == 0

    def test_manifest_detects_missing_table_drift(self):
        # Provision drift test schema
        create_client_schema(TEST_SLUG_DRIFT)

        # Intentionally drop a table
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{TEST_SCHEMA_DRIFT}".business_lines CASCADE'))

        report = TenantManifest.verify_schema_against_manifest(engine, TEST_SCHEMA_DRIFT)
        assert report["is_compliant"] is False
        assert report["drift_detected"] is True
        assert "business_lines" in report["missing_tables"]
        assert report["tables_found"] == len(CLIENT_TABLES_ORDERED) - 1


class TestClientLifecycleModel:
    """Verifies the extended Client model fields and database persistence."""

    def test_client_model_persists_lifecycle_fields(self):
        session = SessionLocal()
        try:
            client = session.query(Client).filter_by(slug=TEST_SLUG_CLEAN).first()
            assert client is not None
            assert client.status == "active"
            assert client.skeleton_version == TenantManifest.get_skeleton_version()
            assert client.provisioned_at is not None
            assert client.refresh_cron == "0 0 * * *"
            assert client.max_concurrent_jobs == 3
            assert client.last_error is None

            # Test updating lifecycle fields
            client.status = "suspended"
            client.suspended_at = datetime.now(timezone.utc)
            session.commit()

            reloaded = session.query(Client).filter_by(slug=TEST_SLUG_CLEAN).first()
            assert reloaded.status == "suspended"
            assert reloaded.suspended_at is not None

            # Restore active
            reloaded.status = "active"
            reloaded.suspended_at = None
            session.commit()
        finally:
            session.close()


class TestSchemaProvisioningLifecycle:
    """Verifies the state machine in SchemaProvisioningService."""

    def test_provisioning_failure_records_error_and_preserves_schema(self):
        # Insert client record in public.clients with status='pending'
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO public.clients (slug, name, schema_name, status, skeleton_version, is_active)
                VALUES (:s, :n, :sc, 'pending', :ver, true)
                ON CONFLICT (slug) DO UPDATE SET status = 'pending', last_error = NULL
            """), {"s": TEST_SLUG_FAIL, "n": "Test Fail Client", "sc": TEST_SCHEMA_FAIL, "ver": TenantManifest.get_skeleton_version()})

        # Simulate failure during table provisioning
        with patch.object(SchemaProvisioningService, "_ensure_tenant_foreign_keys", side_effect=RuntimeError("Simulated FK failure")):
            with pytest.raises(RuntimeError, match="Simulated FK failure"):
                create_client_schema(TEST_SLUG_FAIL)

        # Verify client status in database is 'failed' and last_error is populated
        with engine.connect() as conn:
            row = conn.execute(text("SELECT status, last_error FROM public.clients WHERE slug = :s"),
                               {"s": TEST_SLUG_FAIL}).fetchone()
            assert row is not None
            assert row[0] == "failed"
            assert "Simulated FK failure" in row[1]

            # Verify schema was NOT dropped
            schemas = [r[0] for r in conn.execute(text("SELECT schema_name FROM information_schema.schemata")).fetchall()]
            assert TEST_SCHEMA_FAIL in schemas


class TestTenantMigrationCommands:
    """Verifies CLI migration runner commands (tenants status & tenants upgrade)."""

    def test_cmd_tenants_status_reports_clean_tenants(self):
        # Clean up any failed test records first so status can be verified cleanly
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM public.clients WHERE slug = :s"), {"s": TEST_SLUG_FAIL})

        # Upgrade drift schema via runner to ensure it is compliant
        cmd_tenants_upgrade(argparse.Namespace(only=TEST_SLUG_DRIFT, dry_run=False))

        args = argparse.Namespace()
        ret = cmd_tenants_status(args)
        assert ret == 0

    def test_cmd_tenants_upgrade_dry_run_and_execution(self):
        # Drop a table in drift schema
        with engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{TEST_SCHEMA_DRIFT}".crm_settings CASCADE'))

        # Dry run on only drift slug
        dry_args = argparse.Namespace(only=TEST_SLUG_DRIFT, dry_run=True)
        ret_dry = cmd_tenants_upgrade(dry_args)
        assert ret_dry == 0

        # Verify table was NOT yet repaired by dry run
        rep_before = TenantManifest.verify_schema_against_manifest(engine, TEST_SCHEMA_DRIFT)
        assert "crm_settings" in rep_before["missing_tables"]

        # Run actual upgrade on drift slug
        upgrade_args = argparse.Namespace(only=TEST_SLUG_DRIFT, dry_run=False)
        ret_upgrade = cmd_tenants_upgrade(upgrade_args)
        assert ret_upgrade == 0

        # Verify table IS now repaired and schema is 100% compliant
        rep_after = TenantManifest.verify_schema_against_manifest(engine, TEST_SCHEMA_DRIFT)
        assert rep_after["is_compliant"] is True
        assert "crm_settings" not in rep_after["missing_tables"]
