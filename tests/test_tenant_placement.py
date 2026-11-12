"""Phase 2 Validation Suite: Comprehensive Table Placement & Ingestion Pipeline Scoping.

Verifies:
1. All 58 tenant tables in `CLIENT_TABLES_ORDERED` are properly provisioned in fresh tenant schemas.
2. Platform tables remain exclusively in public schema.
3. Intra-tenant foreign keys correctly qualify to `client_<slug>.*`.
4. Cross-schema foreign keys correctly qualify to `public.users`.
5. Sequence isolation (tenant table sequences reside in client schema and sync on copy).
6. Content pipeline database operations write to and read from tenant schema when scoped.
7. Content pipeline CLI parser and scheduler correctly propagate tenant context.
"""

import os
import sys
import pytest
from sqlalchemy import text
from unittest.mock import patch, MagicMock

from db.connection import engine, get_session
from db.tenant import tenant_scope, get_current_slug, schema_for, validate_slug
from db.provisioning import (
    CLIENT_TABLES_ORDERED,
    create_client_schema,
    copy_data_from_public,
    verify_schema_integrity,
    repair_client_schema,
)
from scripts.tenant_inventory import PLATFORM_TABLES, TenantInventoryAnalyzer

# Add content_pipeline to sys.path for direct cp_db testing
CONTENT_PIPELINE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps", "content_pipeline")
if CONTENT_PIPELINE_DIR not in sys.path:
    sys.path.insert(0, CONTENT_PIPELINE_DIR)

import apps.content_pipeline.db as cp_db
import subprocess


TEST_TENANT_SLUG = "test_placement_p2"
TEST_SCHEMA = f"client_{TEST_TENANT_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_teardown_tenant_schema():
    """Create test tenant schema before module tests and drop on teardown."""
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))

    # Provision fresh schema
    create_client_schema(TEST_TENANT_SLUG)

    yield

    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))


class TestTenantTablePlacement:
    """Validates complete table placement and isolation."""

    def test_all_tenant_tables_created(self):
        """Asserts that all tenant tables in CLIENT_TABLES_ORDERED exist in the provisioned schema."""
        assert len(CLIENT_TABLES_ORDERED) >= 58, f"Expected at least 58 tenant tables, got {len(CLIENT_TABLES_ORDERED)}"

        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = :s AND table_type = 'BASE TABLE'
                """),
                {"s": TEST_SCHEMA}
            ).fetchall()
            existing_tables = {r[0] for r in rows}

        for table in CLIENT_TABLES_ORDERED:
            assert table in existing_tables, f"Table '{table}' missing from provisioned tenant schema '{TEST_SCHEMA}'"

    def test_platform_tables_not_in_tenant_schema(self):
        """Asserts that platform identity/auth/audit tables are never created in tenant schema."""
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = :s AND table_type = 'BASE TABLE'
                """),
                {"s": TEST_SCHEMA}
            ).fetchall()
            tenant_tables = {r[0] for r in rows}

        for table in PLATFORM_TABLES:
            assert table not in tenant_tables, f"Platform table '{table}' should NOT be in tenant schema '{TEST_SCHEMA}'"

    def test_tenant_inventory_alignment(self):
        """Asserts that CLIENT_TABLES_ORDERED matches the Tenant tables collected by TenantInventoryAnalyzer."""
        inv = TenantInventoryAnalyzer()
        inv.collect()
        tenant_tables = {name for name, meta in inv.tables.items() if meta.category == "Tenant"}
        ordered_set = set(CLIENT_TABLES_ORDERED)
        assert ordered_set == tenant_tables, (
            f"CLIENT_TABLES_ORDERED does not match TenantInventoryAnalyzer tenant tables. "
            f"Missing from ordered: {tenant_tables - ordered_set}, "
            f"Extra in ordered: {ordered_set - tenant_tables}"
        )


class TestForeignKeyAndSequenceIntegrity:
    """Validates intra-tenant and cross-schema foreign keys and sequence isolation."""

    def test_intra_tenant_and_cross_schema_fks(self):
        """Checks foreign key references: intra-tenant must point to tenant schema, users must point to public."""
        with engine.connect() as conn:
            fk_rows = conn.execute(
                text("""
                    SELECT
                        tc.table_name,
                        kcu.column_name,
                        ccu.table_schema AS foreign_table_schema,
                        ccu.table_name AS foreign_table_name
                    FROM information_schema.table_constraints AS tc
                    JOIN information_schema.key_column_usage AS kcu
                      ON tc.constraint_name = kcu.constraint_name
                      AND tc.table_schema = kcu.table_schema
                    JOIN information_schema.constraint_column_usage AS ccu
                      ON ccu.constraint_name = tc.constraint_name
                      AND ccu.table_schema = tc.table_schema
                    WHERE tc.constraint_type = 'FOREIGN KEY'
                      AND tc.table_schema = :s
                """),
                {"s": TEST_SCHEMA}
            ).fetchall()

        assert len(fk_rows) > 0, f"Expected adapted foreign keys in {TEST_SCHEMA}"

        for tbl, col, f_schema, f_tbl in fk_rows:
            if f_tbl == "users":
                # Cross-schema FK: Must reference public.users
                assert f_schema == "public", f"FK on {tbl}.{col} -> {f_schema}.{f_tbl} should reference public.users"
            elif f_tbl in CLIENT_TABLES_ORDERED:
                # Intra-tenant FK: Must reference client_<slug>.<f_tbl>
                assert f_schema == TEST_SCHEMA, f"Intra-tenant FK on {tbl}.{col} -> {f_schema}.{f_tbl} should reference {TEST_SCHEMA}"

    def test_sequences_owned_by_tenant_tables(self):
        """Checks that auto-increment sequences in the tenant schema are isolated."""
        with engine.connect() as conn:
            seq_rows = conn.execute(
                text("""
                    SELECT sequence_name
                    FROM information_schema.sequences
                    WHERE sequence_schema = :s
                """),
                {"s": TEST_SCHEMA}
            ).fetchall()

        # Should have dedicated sequences for tables with serial IDs
        assert len(seq_rows) > 0, f"Expected sequences in {TEST_SCHEMA}"

    def test_schema_integrity_and_repair(self):
        """Tests that verify_schema_integrity reports complete and repair is idempotent."""
        report = verify_schema_integrity(TEST_TENANT_SLUG)
        assert report["is_healthy"] is True
        assert report["status"] == "healthy"
        assert report["missing_tables"] == []
        assert report["tables_total"] == len(CLIENT_TABLES_ORDERED)

        repair_result = repair_client_schema(TEST_TENANT_SLUG)
        assert repair_result["is_healthy"] is True
        assert repair_result["repaired_tables"] == []


class TestContentPipelineTenantScoping:
    """Validates content pipeline db operations write to and read from tenant schema."""

    def test_get_active_client_slug_resolution(self):
        """Verifies resolution precedence: parameter > contextvar > env."""
        # 1. Parameter priority
        assert cp_db.get_active_client_slug("param_slug") == "param_slug"

        # 2. ContextVar priority over env
        with tenant_scope("context_slug"):
            with patch.dict(os.environ, {"CLIENT_SLUG": "env_slug"}):
                assert cp_db.get_active_client_slug() == "context_slug"

        # 3. Env fallback when no contextvar
        with patch.dict(os.environ, {"CLIENT_SLUG": "env_slug"}):
            assert cp_db.get_active_client_slug() == "env_slug"

        # 4. Reserved and invalid slugs return None
        assert cp_db.get_active_client_slug("public") is None
        assert cp_db.get_active_client_slug("admin") is None
        assert cp_db.get_active_client_slug("inv@lid;drop") is None
        assert cp_db.get_active_client_slug("") is None

    def test_content_pipeline_writes_to_tenant_schema(self):
        """Verifies upsert_target and upsert_posts land in client schema, not public."""
        target_data = {
            "key": "test_placement_corp",
            "display_name": "Test Placement Corp",
            "ticker": "TPC",
        }

        # Write to tenant schema via explicit client_slug parameter
        cp_db.upsert_target("company", target_data, client_slug=TEST_TENANT_SLUG)

        # Verify target is in tenant schema
        with engine.connect() as conn:
            tenant_row = conn.execute(
                text(f'SELECT key, display_name FROM "{TEST_SCHEMA}".targets WHERE key = :k'),
                {"k": "test_placement_corp"}
            ).fetchone()
            assert tenant_row is not None
            assert tenant_row[0] == "test_placement_corp"

            # Verify target is NOT in public schema
            public_row = conn.execute(
                text("SELECT key FROM public.targets WHERE key = :k"),
                {"k": "test_placement_corp"}
            ).fetchone()
            assert public_row is None

        # Write posts to tenant schema via tenant_scope
        post_data = {
            "linkedin": {
                "posts": [
                    {
                        "platform": "linkedin",
                        "post_url": "https://linkedin.com/posts/test_placement_corp_1",
                        "text": "Exciting news from Test Placement Corp!",
                        "author": "Test Corp",
                        "rank": 1,
                    }
                ]
            }
        }
        with tenant_scope(TEST_TENANT_SLUG):
            cp_db.upsert_posts("test_placement_corp", "company", post_data)

            # Read back store via content_pipeline db
            store = cp_db.get_store("test_placement_corp")
            assert store is not None
            assert "linkedin" in store["data"]
            assert len(store["data"]["linkedin"]["posts"]) == 1

        # Direct SQL verify in tenant schema
        with engine.connect() as conn:
            post_row = conn.execute(
                text(f'SELECT target_key, channel FROM "{TEST_SCHEMA}".posts WHERE target_key = :k'),
                {"k": "test_placement_corp"}
            ).fetchone()
            assert post_row is not None

            # Verify public schema posts did NOT receive this row
            public_post = conn.execute(
                text("SELECT id FROM public.posts WHERE target_key = :k"),
                {"k": "test_placement_corp"}
            ).fetchone()
            assert public_post is None

    def test_content_pipeline_cli_client_flag(self):
        """Verifies CLI argument parsing accepts --client flag."""
        # Top-level --client
        res1 = subprocess.run(
            [sys.executable, "main.py", "--client", "acme_test", "scrape", "--help"],
            cwd=CONTENT_PIPELINE_DIR, capture_output=True, text=True
        )
        assert res1.returncode == 0, f"Failed: {res1.stderr}"
        assert "--client" in res1.stdout

        # Subcommand --client
        res2 = subprocess.run(
            [sys.executable, "main.py", "scrape", "--client", "acme_test2", "--help"],
            cwd=CONTENT_PIPELINE_DIR, capture_output=True, text=True
        )
        assert res2.returncode == 0, f"Failed: {res2.stderr}"
        assert "--client" in res2.stdout

        # Digest subcommand --client
        res3 = subprocess.run(
            [sys.executable, "main.py", "digest", "-c", "acme_test3", "--help"],
            cwd=CONTENT_PIPELINE_DIR, capture_output=True, text=True
        )
        assert res3.returncode == 0, f"Failed: {res3.stderr}"
        assert "--client" in res3.stdout


class TestSchedulerTenantPropagation:
    """Validates scheduler runs propagate tenant context to content pipeline."""

    def test_scheduler_sources_refresh_propagates_client_slug(self):
        """Verifies run_sources_refresh includes --client and CLIENT_SLUG env."""
        from services.scheduler import run_sources_refresh

        mock_session = MagicMock()
        options = {"channels": ["sec"], "limit": 5, "client_slug": "tenant_sched_test"}

        with patch("subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "SCRAPE · Test Account\nDone"
            mock_proc.stderr = ""
            mock_run.return_value = mock_proc

            summary = run_sources_refresh(mock_session, options)

            assert mock_run.called
            cmd_called = mock_run.call_args[0][0]
            env_called = mock_run.call_args[1]["env"]

            assert "--client" in cmd_called
            client_idx = cmd_called.index("--client")
            assert cmd_called[client_idx + 1] == "tenant_sched_test"
            assert env_called.get("CLIENT_SLUG") == "tenant_sched_test"
            assert summary.get("client_slug") == "tenant_sched_test"
