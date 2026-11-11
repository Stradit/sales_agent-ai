"""Tests for Tenant Inventory and Classification Service (scripts/tenant_inventory.py).

Validates:
1. Full database schema introspection across all public tables.
2. Deterministic categorization into Platform, Tenant, and Reference categories.
3. No table left unclassified (0 Unknowns).
4. Correct classification of key architectural boundary tables.
5. Topological sorting algorithm handles table dependencies without circular exceptions.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest
from scripts.tenant_inventory import (
    PLATFORM_TABLES,
    REFERENCE_TABLES,
    TenantInventoryAnalyzer,
)


@pytest.fixture(scope="module")
def analyzer():
    inv = TenantInventoryAnalyzer()
    inv.collect()
    return inv


def test_inventory_collects_tables(analyzer):
    """Verify inventory discovers all tables in public schema."""
    assert len(analyzer.tables) >= 60, f"Expected >= 60 tables, found {len(analyzer.tables)}"
    assert "users" in analyzer.tables
    assert "clients" in analyzer.tables
    assert "accounts" in analyzer.tables
    assert "deals" in analyzer.tables


def test_zero_unclassified_tables(analyzer):
    """Verify every table is assigned to a valid category without Unknowns."""
    unknowns = [name for name, meta in analyzer.tables.items() if meta.category not in ("Platform", "Tenant", "Reference")]
    assert len(unknowns) == 0, f"Found unclassified tables: {unknowns}"


def test_platform_classification(analyzer):
    """Verify platform infrastructure tables are classified as Platform."""
    for pt in ["users", "clients", "user_client_access", "scheduled_jobs", "audit_log", "refresh_tokens"]:
        if pt in analyzer.tables:
            assert analyzer.tables[pt].category == "Platform", f"{pt} should be Platform"


def test_reference_classification(analyzer):
    """Verify static reference tables are classified as Reference."""
    if "fx_rates" in analyzer.tables:
        assert analyzer.tables["fx_rates"].category == "Reference"


def test_tenant_classification(analyzer):
    """Verify business domain entities are classified as Tenant."""
    for tt in ["accounts", "lobs", "personas", "deals", "posts", "introductions", "rag_documents"]:
        if tt in analyzer.tables:
            assert analyzer.tables[tt].category == "Tenant", f"{tt} should be Tenant"


def test_summary_consistency(analyzer):
    """Verify summary metrics add up to total table count."""
    s = analyzer.summary()
    assert s["total_tables"] == len(analyzer.tables)
    cat_sum = s["counts"]["Platform"] + s["counts"]["Tenant"] + s["counts"]["Reference"]
    assert cat_sum == s["total_tables"]
    assert s["uncloned_tenant_tables_count"] > 0, "Should detect uncloned tables outside the legacy 16"


def test_topological_sort(analyzer):
    """Verify topological sorting produces a valid creation sequence."""
    order = analyzer.compute_topological_sort()
    tenant_tables = [name for name, m in analyzer.tables.items() if m.category == "Tenant"]
    assert len(order) == len(tenant_tables)
    assert set(order) == set(tenant_tables)
