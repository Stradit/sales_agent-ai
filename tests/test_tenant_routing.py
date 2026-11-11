"""Hermetic Test Suite for Phase 1: Fail-Closed Routing & Tenant Context Engine.

Verifies:
1. Strict fail-closed ContextVar behavior (no silent platform fallback).
2. Slug validation, normalization, and reserved word prevention.
3. Context lifecycle with tenant_scope() and nested scopes.
4. Thread context propagation via spawn_tenant_thread().
5. Per-transaction SET LOCAL search_path isolation and rollback safety.
6. Date-partitioned per-tenant output directory isolation in config.py.
7. Cross-tenant pipeline job polling isolation (boundary checks).
8. Production environment startup assertion (AUTH_ENFORCED guard).
"""

import os
import threading
import pytest
from pathlib import Path
from sqlalchemy import text
from fastapi.testclient import TestClient

from db.tenant import (
    validate_slug,
    schema_for,
    slug_from_schema,
    require_current_schema,
    require_current_slug,
    get_current_schema,
    get_current_slug,
    current_schema,
    current_slug,
    tenant_scope,
    spawn_tenant_thread,
    TenantContextError,
    get_client_session,
    get_public_session,
    scoped_client_session,
    SessionLocal,
)
from db.connection import get_session
import config
from api import app, TYPE_ACCOUNT
from services.job_registry import create_job, get_job, list_jobs
import auth


test_client = TestClient(app)


# ── 1. Fail-Closed Unbound Context ───────────────────────────────────────────

def test_fail_closed_unbound_context():
    """Operations requiring tenant context must raise TenantContextError when unbound."""
    # Ensure clean unbound state
    assert get_current_schema() is None
    assert get_current_slug() is None

    with pytest.raises(TenantContextError) as exc_schema:
        require_current_schema()
    assert "No tenant bound to this context" in str(exc_schema.value) or "Operation requires an active tenant context" in str(exc_schema.value)

    with pytest.raises(TenantContextError) as exc_slug:
        require_current_slug()
    assert "Operation requires an active tenant context" in str(exc_slug.value)

    # Aliases per spec §5.1
    with pytest.raises(TenantContextError):
        current_schema()

    with pytest.raises(TenantContextError):
        current_slug()


# ── 2. Slug Validation & Normalization ───────────────────────────────────────

def test_slug_validation_and_normalization():
    """Validates slug sanitization, legacy aliases, and rejects dangerous/reserved inputs."""
    # Valid slugs
    assert validate_slug("stradit_india") == "stradit_india"
    assert validate_slug("acme_corp_42") == "acme_corp_42"
    assert validate_slug("  CLIENT_TEST  ") == "client_test"

    # Normalization of legacy alias
    assert validate_slug("stradit") == "stradit_india"

    # Schema conversion helpers
    assert schema_for("acme_corp") == "client_acme_corp"
    assert schema_for("client_acme_corp") == "client_acme_corp"  # idempotent
    assert slug_from_schema("client_acme_corp") == "acme_corp"
    assert slug_from_schema("acme_corp") == "acme_corp"

    # Invalid slug structures
    invalid_slugs = [
        "",
        "   ",
        "123abc",       # starts with digit
        "_invalid",     # starts with underscore
        "a",            # too short (< 2 chars)
        "bad-slug",     # hyphens disallowed
        "bad$slug",     # special characters disallowed
        "a" * 105,      # exceeds 99 characters
    ]
    for bad in invalid_slugs:
        with pytest.raises(ValueError):
            validate_slug(bad)

    # Reserved keywords
    reserved_slugs = ["public", "information_schema", "pg_catalog", "template", "postgres", "admin", "system"]
    for r in reserved_slugs:
        with pytest.raises(ValueError) as exc:
            validate_slug(r)
        assert "reserved" in str(exc.value).lower()


# ── 3. Tenant Scope Lifecycle & Nested Scopes ────────────────────────────────

def test_tenant_scope_lifecycle_and_nesting():
    """Verifies tenant_scope establishes, restores, and isolates context across blocks."""
    assert get_current_slug() is None

    with tenant_scope("alpha_corp"):
        assert get_current_slug() == "alpha_corp"
        assert get_current_schema() == "client_alpha_corp"
        assert require_current_slug() == "alpha_corp"

        # Nested scope
        with tenant_scope("beta_corp"):
            assert get_current_slug() == "beta_corp"
            assert get_current_schema() == "client_beta_corp"

        # Restored to outer scope
        assert get_current_slug() == "alpha_corp"
        assert get_current_schema() == "client_alpha_corp"

    # Fully unbound outside context manager
    assert get_current_slug() is None
    assert get_current_schema() is None


def test_tenant_scope_exception_safety():
    """ContextVar must be reliably reset even if an exception occurs inside the scope."""
    assert get_current_slug() is None

    try:
        with tenant_scope("error_tenant"):
            assert get_current_slug() == "error_tenant"
            raise ZeroDivisionError("Simulated failure")
    except ZeroDivisionError:
        pass

    assert get_current_slug() is None
    assert get_current_schema() is None


# ── 4. Thread Context Propagation (spawn_tenant_thread) ─────────────────────

def test_spawn_tenant_thread_context_propagation():
    """Verifies background threads retain the parent's tenant context."""
    results = {}

    def thread_worker(key: str):
        results[key] = {
            "slug": get_current_slug(),
            "schema": get_current_schema(),
        }

    with tenant_scope("thread_test_tenant"):
        th = spawn_tenant_thread(target=thread_worker, args=("inherited",), name="test-inherited")
        th.join(timeout=5.0)

    assert results.get("inherited") == {
        "slug": "thread_test_tenant",
        "schema": "client_thread_test_tenant",
    }

    # Verify standard threading.Thread without context copy loses context
    with tenant_scope("thread_raw_tenant"):
        raw_th = threading.Thread(target=thread_worker, args=("raw",), name="test-raw")
        raw_th.start()
        raw_th.join(timeout=5.0)

    # Standard thread did NOT inherit ContextVar
    assert results.get("raw") == {
        "slug": None,
        "schema": None,
    }


# ── 5. SET LOCAL search_path per-Transaction & Rollback Safety ───────────────

def test_set_local_search_path_isolation_and_rollback():
    """Verifies SET LOCAL search_path binds active tenant schema per transaction and reverts on rollback."""
    # 1. Unbound session defaults to public
    session_pub = SessionLocal()
    try:
        sp = session_pub.execute(text("SHOW search_path")).scalar()
        assert "public" in sp
        assert "client_" not in sp
    finally:
        session_pub.close()

    # 2. Session with explicit tenant_scope
    with tenant_scope("stradit_india"):
        session_tenant = SessionLocal()
        try:
            sp = session_tenant.execute(text("SHOW search_path")).scalar()
            assert sp.startswith('"client_stradit_india"') or sp.startswith("client_stradit_india")
            assert "public" in sp
        finally:
            session_tenant.close()

    # 3. SET LOCAL transaction isolation: reverts cleanly upon rollback
    from db.connection import engine
    with engine.connect() as conn:
        trans = conn.begin()
        conn.exec_driver_sql('SET LOCAL search_path = "client_stradit_india", public')
        sp_in_txn = conn.exec_driver_sql("SHOW search_path").scalar()
        assert "client_stradit_india" in sp_in_txn
        trans.rollback()
        sp_after_rollback = conn.exec_driver_sql("SHOW search_path").scalar()
        # Under Postgres, SET LOCAL reverted automatically upon transaction rollback
        assert "client_stradit_india" not in sp_after_rollback


def test_scoped_client_session_helper():
    """Verifies scoped_client_session context manager sets schema on session."""
    with scoped_client_session("stradit_india") as s:
        assert s.info.get("schema") == "client_stradit_india"
        sp = s.execute(text("SHOW search_path")).scalar()
        assert "client_stradit_india" in sp


# ── 6. Output Directory Partitioning in config.py ───────────────────────────

def test_config_output_dir_partitioning():
    """Verifies get_run_output_dirs dynamically partitions into output/<tenant_slug>/..."""
    # 1. Within tenant_scope
    with tenant_scope("acme_holdings"):
        paths = config.get_run_output_dirs("Acme Corp")
        assert "acme_holdings" in str(paths["client_dir"])
        assert "acme_holdings" in str(paths["run_dir"])
        assert str(paths["run_dir"]).startswith(str(config.OUTPUT_DIR / "acme_holdings"))

    # 2. With explicit client_slug parameter override
    paths_explicit = config.get_run_output_dirs("Global Logistics", client_slug="logistics_intl")
    assert "logistics_intl" in str(paths_explicit["client_dir"])
    assert "logistics_intl" in str(paths_explicit["run_dir"])
    assert str(paths_explicit["run_dir"]).startswith(str(config.OUTPUT_DIR / "logistics_intl"))


# ── 7. Cross-Tenant Job Isolation Boundaries ────────────────────────────────

def test_cross_tenant_job_isolation():
    """Verifies that jobs belonging to tenant A cannot be polled or accessed by tenant B."""
    # Create a job under tenant_a
    job_id = create_job(
        job_type=TYPE_ACCOUNT,
        meta={"company_name": "Tenant A Secret Corp"},
        client_slug="tenant_alpha",
    )
    job_info = get_job(job_id)
    assert job_info is not None
    assert job_info["client_slug"] == "tenant_alpha"

    # Poll under tenant_beta (must return 404)
    res_b = test_client.get(
        f"/api/pipeline/job/{job_id}",
        headers={"X-Client-Slug": "tenant_beta"}
    )
    assert res_b.status_code == 404
    assert "not found" in res_b.json().get("detail", "").lower()

    # Poll under tenant_alpha (must return 200)
    res_a = test_client.get(
        f"/api/pipeline/job/{job_id}",
        headers={"X-Client-Slug": "tenant_alpha"}
    )
    assert res_a.status_code == 200
    assert res_a.json()["job_id"] == job_id

    # List jobs under tenant_beta -> tenant_alpha's job must not appear
    res_list_b = test_client.get(
        "/api/pipeline/jobs",
        headers={"X-Client-Slug": "tenant_beta"}
    )
    assert res_list_b.status_code == 200
    b_job_ids = [j["job_id"] for j in res_list_b.json().get("jobs", [])]
    assert job_id not in b_job_ids

    # List jobs under tenant_alpha -> tenant_alpha's job must appear
    res_list_a = test_client.get(
        "/api/pipeline/jobs",
        headers={"X-Client-Slug": "tenant_alpha"}
    )
    assert res_list_a.status_code == 200
    a_job_ids = [j["job_id"] for j in res_list_a.json().get("jobs", [])]
    assert job_id in a_job_ids


# ── 8. Production Security Assertion (AUTH_ENFORCED Guard) ───────────────────

def test_production_auth_enforced_startup_guard(monkeypatch):
    """Verifies that on_startup() halts application boot if ENV=production and AUTH_ENFORCED=false."""
    from api import on_startup

    # Case 1: ENV=production and AUTH_ENFORCED=False -> Must raise RuntimeError
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setattr(auth, "AUTH_ENFORCED", False)

    with pytest.raises(RuntimeError) as exc:
        on_startup()
    assert "FATAL SECURITY MISCONFIGURATION" in str(exc.value)
    assert "AUTH_ENFORCED=false" in str(exc.value)

    # Case 2: ENV=development and AUTH_ENFORCED=False -> Allowed
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setattr(auth, "AUTH_ENFORCED", False)
    # Should not raise
    on_startup()
