"""Test Suite: Deny-by-Default Share Policy Enforcement.

Validates that config/tenant_skeleton/share_policy.yaml strictly satisfies
all non-negotiable architectural principles of Specification v2:
1. Every table in the tenant skeleton is classified.
2. Every column across all tenant tables is classified into exactly one of:
   'copy', 'regenerate', 'sensitive', or 'never'.
3. Zero duplicate column classifications.
4. Zero unclassified columns (deny-by-default).
5. Strict security and privacy invariants (CRM, raw vendor data, owner IDs, verification flags).
"""

from pathlib import Path
import pytest
import yaml
from sqlalchemy import inspect
from db.connection import engine

POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "tenant_skeleton" / "share_policy.yaml"
MANIFEST_PATH = Path(__file__).resolve().parent.parent / "config" / "tenant_skeleton" / "manifest.yaml"


@pytest.fixture(scope="module")
def share_policy() -> dict:
    assert POLICY_PATH.exists(), f"share_policy.yaml missing at {POLICY_PATH}"
    with open(POLICY_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST_PATH.exists(), f"manifest.yaml missing at {MANIFEST_PATH}"
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_share_policy_structure(share_policy):
    """Verifies the root metadata of the share policy."""
    assert "policy_version" in share_policy
    assert "skeleton_version" in share_policy
    assert "rules" in share_policy
    assert share_policy["rules"].get("default_action") == "deny"
    assert "tables" in share_policy
    assert len(share_policy["tables"]) >= 58


def test_manifest_tables_coverage(share_policy, manifest):
    """Verifies that 100% of tables defined in manifest.yaml exist in share_policy.yaml."""
    manifest_tables = {t["name"] for t in manifest.get("tables", [])}
    policy_tables = set(share_policy["tables"].keys())

    missing_tables = manifest_tables - policy_tables
    assert not missing_tables, f"Manifest tables missing from share_policy.yaml: {missing_tables}"


def test_live_schema_columns_classified(share_policy):
    """Verifies every column in the live PostgreSQL schema is classified with zero unclassified columns."""
    insp = inspect(engine)
    live_public_tables = set(insp.get_table_names(schema="public"))
    categories = ["copy", "regenerate", "sensitive", "never"]

    for table_name, table_def in share_policy["tables"].items():
        if table_name not in live_public_tables:
            # If table hasn't been migrated yet (e.g. account_origin before migration 0013), skip live db check
            continue

        db_columns = {col["name"] for col in insp.get_columns(table_name, schema="public")}
        
        # Collect classified columns
        all_classified = []
        for cat in categories:
            cols = table_def.get(cat, [])
            assert isinstance(cols, list), f"Table {table_name}.{cat} must be a list"
            all_classified.extend(cols)

        classified_set = set(all_classified)

        # Check for duplicates across categories
        assert len(all_classified) == len(classified_set), (
            f"Table {table_name} has duplicate column classifications: "
            f"{[c for c in classified_set if all_classified.count(c) > 1]}"
        )

        # Check for unclassified columns in live DB
        unclassified = db_columns - classified_set
        assert not unclassified, f"Table {table_name} has UNCLASSIFIED columns: {unclassified}"

        # Check for non-existent columns in policy
        non_existent = classified_set - db_columns
        assert not non_existent, f"Table {table_name} policy specifies non-existent columns: {non_existent}"


def test_security_and_privacy_invariants(share_policy):
    """Enforces non-negotiable security boundaries for CRM and sensitive data."""
    tables = share_policy["tables"]

    # 1. Accounts invariants
    acc = tables["accounts"]
    never_acc = set(acc["never"])
    assert "owner_user_id" in never_acc, "accounts.owner_user_id MUST be 'never'"
    assert "primary_business_line_id" in never_acc, "accounts.primary_business_line_id MUST be 'never'"
    assert "is_manually_verified" in never_acc, "accounts.is_manually_verified MUST be 'never'"
    assert "manually_verified_at" in never_acc, "accounts.manually_verified_at MUST be 'never'"
    assert "raw_data" in never_acc, "accounts.raw_data MUST be 'never'"
    assert "osint_feed_manifest" in never_acc, "accounts.osint_feed_manifest MUST be 'never'"
    assert "api_credits_total" in never_acc, "accounts.api_credits_total MUST be 'never'"
    assert "api_credits_used" in never_acc, "accounts.api_credits_used MUST be 'never'"

    # 2. Personas invariants
    per = tables["personas"]
    never_per = set(per["never"])
    sens_per = set(per["sensitive"])
    regen_per = set(per["regenerate"])
    assert "created_by_user_id" in never_per, "personas.created_by_user_id MUST be 'never'"
    assert "is_manually_verified" in never_per, "personas.is_manually_verified MUST be 'never'"
    assert "personal_email" in sens_per, "personas.personal_email MUST be 'sensitive'"
    assert "direct_mobile_phone" in sens_per, "personas.direct_mobile_phone MUST be 'sensitive'"
    assert "phone" in sens_per, "personas.phone MUST be 'sensitive'"
    assert "email" in sens_per, "personas.email MUST be 'sensitive'"
    assert "value_proposition" in regen_per, "personas.value_proposition MUST be 'regenerate'"
    assert "personalized_icebreaker" in regen_per, "personas.personalized_icebreaker MUST be 'regenerate'"

    # 3. Relational integrity: sub_lobs has NO account_id
    sub_lobs_all = (
        tables["sub_lobs"]["copy"]
        + tables["sub_lobs"]["regenerate"]
        + tables["sub_lobs"]["sensitive"]
        + tables["sub_lobs"]["never"]
    )
    assert "account_id" not in sub_lobs_all, "sub_lobs MUST NOT contain account_id column!"

    # 4. Strict isolation of private CRM & Copilot & RAG tables
    crm_and_private_tables = [
        "deals", "deal_stakeholders", "deal_stage_history", "deal_checklist",
        "deal_activity", "deal_close_plan", "deal_buyer_rooms", "deal_email_drafts",
        "activities", "activity_links", "introductions", "introduction_events",
        "crm_wanted_intros", "action_items", "action_item_reminders",
        "copilot_sessions", "copilot_messages", "copilot_memories",
        "rag_documents", "rag_document_chunks", "rag_document_sources",
        "rag_document_entities", "rag_index_entries", "rag_pipeline_runs",
        "pipeline_runs", "user_account_access"
    ]
    for tbl_name in crm_and_private_tables:
        tbl = tables.get(tbl_name)
        assert tbl is not None, f"Private table {tbl_name} missing from policy"
        assert tbl.get("classification") == "never", f"Table {tbl_name} classification MUST be 'never'"
        assert len(tbl.get("copy", [])) == 0, f"Table {tbl_name} MUST NOT have any copy columns"
        assert len(tbl.get("sensitive", [])) == 0, f"Table {tbl_name} MUST NOT have sensitive columns"
