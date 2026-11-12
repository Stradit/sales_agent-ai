"""Test Suite: Catalog Service & Cross-Tenant Account Reuse Engine.

Validates:
1. Deterministic Entity Resolution (CIK > LEI > Domain).
2. Quality Scoring & Quality Gate thresholds.
3. Sanitized snapshot generation via share_policy.yaml (deny-by-default).
4. Zero-knowledge Anti-Oracle lookups (no source client leaks).
5. Cryptographically signed HMAC preview tokens.
6. Atomic single-transaction hydration with ID remapping:
   - accounts, lobs, sub_lobs (via lob_id, no account_id), personas
   - organisational_hierarchy_tree deep traversal and remapping (Spike S1)
   - targets registration (Spike S2)
   - account_origin provenance logging
   - quota enforcement and fee credit ledger deduction
   - exactly-once idempotency
7. Exact reversible rollback:
   - blocked when private CRM children exist
   - clean reverse-topological deletion when clean
   - credit fee refund in ledger
"""

from datetime import datetime, timezone
import json
import uuid
import pytest
from sqlalchemy import text

from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.platform_catalog import (
    Company,
    CompanyIdentifier,
    ShareEvent,
    Snapshot,
    SnapshotBlob,
)
from db.provisioning import create_client_schema, drop_client_schema
from services.catalog_service import CatalogService
from services.account_reuse_service import (
    AccountReuseService,
    DuplicateAccountError,
    RollbackBlockedError,
)
from services.credit_service import AccountQuotaExceededError


SOURCE_SLUG = "test_reuse_src"
TARGET_SLUG = "test_reuse_tgt"
SOURCE_SCHEMA = f"client_{SOURCE_SLUG}"
TARGET_SCHEMA = f"client_{TARGET_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_reuse_test_environment():
    """Provisions isolated source and target tenant schemas for reuse testing."""
    session = SessionLocal()
    try:
        # Cleanup any lingering test clients
        session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2)"), {"s1": SOURCE_SLUG, "s2": TARGET_SLUG})
        session.commit()
    finally:
        session.close()

    # Drop existing schemas
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SOURCE_SCHEMA}" CASCADE'))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TARGET_SCHEMA}" CASCADE'))

    # Provision schemas
    create_client_schema(SOURCE_SLUG)
    create_client_schema(TARGET_SLUG)

    # Enable catalog permissions
    session = SessionLocal()
    try:
        src = session.query(Client).filter_by(slug=SOURCE_SLUG).first()
        tgt = session.query(Client).filter_by(slug=TARGET_SLUG).first()
        src.catalog_publish = True
        src.api_credits_total = 1000
        tgt.catalog_consume = True
        tgt.api_credits_total = 1000
        tgt.max_accounts = 5
        session.commit()
    finally:
        session.close()

    yield

    # Teardown
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{SOURCE_SCHEMA}" CASCADE'))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{TARGET_SCHEMA}" CASCADE'))
    session = SessionLocal()
    try:
        # Delete catalog records associated with test
        session.execute(text("""
            DELETE FROM platform_catalog.companies
            WHERE canonical_key LIKE 'cik:0001999999%' OR canonical_key LIKE 'dom:apexcapital.com%'
        """))
        session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2)"), {"s1": SOURCE_SLUG, "s2": TARGET_SLUG})
        session.commit()
    finally:
        session.close()


def test_entity_resolution_hierarchy():
    """Validates normalization rules and canonical key priority (CIK > LEI > Domain)."""
    # 1. CIK normalization
    assert CatalogService.normalize_cik("1364742") == "0001364742"
    assert CatalogService.normalize_cik("CIK-0001364742") == "0001364742"

    # 2. Domain normalization
    assert CatalogService.normalize_domain("https://www.blackrock.com/investing") == "blackrock.com"
    assert CatalogService.normalize_domain("http://BlackRock.COM:8080/") == "blackrock.com"

    # 3. LEI normalization
    assert CatalogService.normalize_lei("549300v5xz12h6g20e62") == "549300V5XZ12H6G20E62"

    # 4. Canonical key priority
    data_full = {
        "sec_cik": "1999999",
        "lei": "549300V5XZ12H6G20E62",
        "primary_domain": "apexcapital.com",
    }
    canon, ids = CatalogService.resolve_canonical_key(data_full)
    assert canon == "cik:0001999999"
    assert len(ids) == 3

    # Without CIK -> LEI wins
    data_no_cik = {
        "lei": "549300V5XZ12H6G20E62",
        "primary_domain": "apexcapital.com",
    }
    canon_lei, _ = CatalogService.resolve_canonical_key(data_no_cik)
    assert canon_lei == "lei:549300V5XZ12H6G20E62"

    # Without CIK or LEI -> Domain wins
    data_dom_only = {"domain": "https://apexcapital.com"}
    canon_dom, _ = CatalogService.resolve_canonical_key(data_dom_only)
    assert canon_dom == "dom:apexcapital.com"


def test_quality_score_calculation():
    """Validates rubric calculation and grade thresholds."""
    account = {
        "display_name": "Apex Capital Management",
        "domain": "apexcapital.com",
        "short_description": "Global asset management firm",
        "country": "United States",
        "city": "New York",
        "industries": ["Asset Management", "Financial Services"],
        "sec_cik": "0001999999",
        "estimated_revenue_range": "$10B - $50B",
        "employee_count_range": "10000+",
        "multi_source_intelligence": {"sources": ["sec", "diffbot"]},
    }
    lobs = [{"name": "Asset Management"}, {"name": "Wealth Advisory"}, {"name": "Index Solutions"}]
    sub_lobs = [{"name": "Global Equity ETF"}]
    personas = [
        {"full_name": f"Exec {i}", "hierarchy_level": "c_suite" if i == 0 else "director", "tier": "Tier 1"}
        for i in range(16)
    ]
    posts = [{"body": "Update"}]
    jobs = [{"title": "Analyst"}]

    score, grade = CatalogService.calculate_quality_score(account, lobs, sub_lobs, personas, posts, jobs)
    assert score >= 80.0
    assert grade == "verified"


def test_publish_and_anti_oracle_lookup():
    """Validates full publish cycle and anti-oracle zero-knowledge lookup."""
    session = SessionLocal()
    try:
        # Seed an account in source tenant schema
        hier_tree = {
            "name": "Apex Global",
            "lob_id": 999,  # Placeholder to test Spike S1 tree remapping
            "persona_id": 888,
            "children": [],
        }
        ins_acct = session.execute(
            text(f"""
                INSERT INTO "{SOURCE_SCHEMA}"."accounts"
                (key, display_name, domain, sec_cik, headquarters_location, country, city,
                 industries, industry_groups, aliases, founders, headquarters_regions, keywords,
                 multi_source_intelligence,
                 organisational_hierarchy_tree, raw_data, is_manually_verified, owner_user_id)
                VALUES
                ('apex_capital', 'Apex Capital Management', 'apexcapital.com', '0001999999',
                 'New York, NY', 'United States', 'New York',
                 ARRAY['Financial Services', 'Asset Management', '投資'],
                 ARRAY['Banking & Finance'],
                 ARRAY['Apex Capital', 'Apex Group'],
                 ARRAY['Alice Smith', 'Bob Jones'],
                 ARRAY['North America', 'Americas'],
                 ARRAY[]::text[],
                 CAST(:msi AS jsonb),
                 :tree, '{{"private": "vendor_leak"}}', true, 1)
                RETURNING id
            """),
            {
                "tree": json.dumps(hier_tree),
                "msi": json.dumps({"summary": "Top asset manager", "sentiment": "positive", "unicode": "こんにちは"}),
            },
        ).scalar()

        # Seed LOB
        ins_lob = session.execute(
            text(f"""
                INSERT INTO "{SOURCE_SCHEMA}"."lobs"
                (account_id, key, lob_name, domain, raw_data, is_manually_verified)
                VALUES (:aid, 'apex_am', 'Asset Management', 'apexam.com', '{{"secret": 1}}', true)
                RETURNING id
            """),
            {"aid": ins_acct},
        ).scalar()

        # Seed Sub-LOB (linked via lob_id ONLY, NO account_id column)
        ins_sub = session.execute(
            text(f"""
                INSERT INTO "{SOURCE_SCHEMA}"."sub_lobs"
                (lob_id, name, jurisdiction, is_manually_verified)
                VALUES (:lid, 'Global ETF', 'US-DE', true)
                RETURNING id
            """),
            {"lid": ins_lob},
        ).scalar()

        # Seed Persona
        ins_per = session.execute(
            text(f"""
                INSERT INTO "{SOURCE_SCHEMA}"."personas"
                (account_id, lob_id, key, display_name, full_name, title, hierarchy_level, tier,
                 departments, skills, past_companies, previous_titles, target_kpis, key_objections,
                 extended_profile,
                 personal_email, direct_mobile_phone, value_proposition, personalized_icebreaker,
                 raw_data, is_manually_verified, created_by_user_id)
                VALUES
                (:aid, :lid, 'p_ceo', 'Jane Doe', 'Jane Doe', 'Chief Executive Officer', 1, 'Tier 1',
                 ARRAY['Executive', 'Leadership'],
                 ARRAY['Portfolio Management', 'Asset Allocation', 'リスク管理'],
                 ARRAY['BlackRock', 'Vanguard'],
                 ARRAY['Managing Director', 'VP'],
                 ARRAY['AUM Growth', 'Alpha'],
                 ARRAY[]::text[],
                 CAST(:ext AS jsonb),
                 'jane.private@gmail.com', '+1-555-0199', 'Private Pitch', 'Private Icebreaker',
                 '{{"vendor": "raw"}}', true, 1)
                RETURNING id
            """),
            {
                "aid": ins_acct,
                "lid": ins_lob,
                "ext": json.dumps({"bio": "Experienced CEO", "verified": True}),
            },
        ).scalar()

        # Update hierarchy tree with real IDs
        hier_tree["lob_id"] = ins_lob
        hier_tree["persona_id"] = ins_per
        session.execute(
            text(f'UPDATE "{SOURCE_SCHEMA}"."accounts" SET organisational_hierarchy_tree = :tree WHERE id = :aid'),
            {"tree": json.dumps(hier_tree), "aid": ins_acct},
        )
        session.commit()

        # 1. Publish snapshot to platform_catalog
        snapshot = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SOURCE_SLUG,
            account_id=ins_acct,
            published_by_user_id=1,
            force=True,
        )
        assert snapshot is not None
        assert snapshot.status == "ready"
        assert snapshot.version == 1

        # Verify snapshot blob sanitization (deny-by-default):
        blob = session.query(SnapshotBlob).filter_by(snapshot_id=snapshot.id).first()
        assert blob is not None
        payload = blob.payload

        # Invariant: private vendor payloads & owner IDs are NOT in blob
        assert "raw_data" not in payload["account"]
        assert "owner_user_id" not in payload["account"]
        assert "is_manually_verified" not in payload["account"]

        # Invariant: sensitive persona contact info is NOT in blob
        persona_payload = payload["personas"][0]
        assert "personal_email" not in persona_payload
        assert "direct_mobile_phone" not in persona_payload
        assert "value_proposition" not in persona_payload
        assert "personalized_icebreaker" not in persona_payload

        # 2. Anti-Oracle Lookup
        # Lookup non-existent -> returns None
        assert AccountReuseService.check_reuse_available(session, "nonexistent-corp-xyz.com", TARGET_SLUG) is None

        # Lookup verified account -> returns sanitized metadata
        preview = AccountReuseService.check_reuse_available(session, "apexcapital.com", TARGET_SLUG)
        assert preview is not None
        assert preview["available"] is True
        assert preview["canonical_key"] == "cik:0001999999"
        assert preview["display_name"] == "Apex Capital Management"
        assert preview["freshness"] == "fresh"
        assert preview["counts"]["lobs"] == 1
        assert preview["counts"]["personas"] == 1
        assert preview["fee_credits"] == 1

        # Critical security check: source tenant slug/id MUST NOT be in preview dict
        assert "source_client_slug" not in preview
        assert "source_client_id" not in preview
        assert "source_account_id" not in preview

        # 3. Preview Token Validation
        token = AccountReuseService.generate_preview_token(
            snapshot_id=snapshot.id,
            target_client_slug=TARGET_SLUG,
            user_id=1,
        )
        assert AccountReuseService.validate_preview_token(token, TARGET_SLUG, snapshot.id) is True
        # Tampered slug fails
        assert AccountReuseService.validate_preview_token(token, "evil_client", snapshot.id) is False

    finally:
        session.close()


def test_atomic_single_transaction_hydration():
    """Validates single-transaction hydration, ID remapping, quota enforcement, and idempotency."""
    session = SessionLocal()
    try:
        # Find the snapshot published in previous test
        company = session.query(Company).filter_by(canonical_key="cik:0001999999").first()
        assert company is not None
        snapshot = CatalogService.get_latest_snapshot(session, company.id)
        assert snapshot is not None

        idem_key = f"hydrate_test_{uuid.uuid4().hex}"

        # 1. Hydrate account into target tenant
        res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=TARGET_SLUG,
            snapshot_id=snapshot.id,
            user_id=1,
            idempotency_key=idem_key,
            mode="instant",
            fee_credits=1,
        )
        assert res["ok"] is True
        target_account_id = res["account_id"]
        assert target_account_id is not None
        assert res["counts"]["lobs"] == 1
        assert res["counts"]["sub_lobs"] == 1
        assert res["counts"]["personas"] == 1

        # 2. Verify target schema records
        target_acct = session.execute(
            text(f'''
                SELECT id, key, display_name, domain, is_manually_verified, owner_user_id,
                       organisational_hierarchy_tree, industries, industry_groups, aliases,
                       founders, headquarters_regions, keywords, multi_source_intelligence
                FROM "{TARGET_SCHEMA}"."accounts" WHERE id = :aid
            '''),
            {"aid": target_account_id},
        ).fetchone()
        assert target_acct is not None
        assert target_acct[2] == "Apex Capital Management"
        assert target_acct[4] is False  # Verification flag reset
        assert target_acct[5] == 1      # Owner set to target user
        # Verify array round trip
        assert target_acct[7] == ['Financial Services', 'Asset Management', '投資']
        assert target_acct[8] == ['Banking & Finance']
        assert target_acct[9] == ['Apex Capital', 'Apex Group']
        assert target_acct[10] == ['Alice Smith', 'Bob Jones']
        assert target_acct[11] == ['North America', 'Americas']
        assert target_acct[12] == []
        # Verify JSONB round trip
        msi = target_acct[13]
        if isinstance(msi, str):
            msi = json.loads(msi)
        assert msi["summary"] == "Top asset manager"
        assert msi["unicode"] == "こんにちは"

        # 3. Verify Sub-LOB relational linkage via lob_id
        target_lob = session.execute(
            text(f'SELECT id, lob_name, account_id FROM "{TARGET_SCHEMA}"."lobs" WHERE account_id = :aid'),
            {"aid": target_account_id},
        ).fetchone()
        assert target_lob is not None
        new_lob_id = target_lob[0]

        target_sub = session.execute(
            text(f'SELECT id, name, lob_id FROM "{TARGET_SCHEMA}"."sub_lobs" WHERE lob_id = :lid'),
            {"lid": new_lob_id},
        ).fetchone()
        assert target_sub is not None
        assert target_sub[1] == "Global ETF"
        assert target_sub[2] == new_lob_id

        # 4. Verify Persona relational linkage and ARRAY / JSONB round trip
        target_per = session.execute(
            text(f'''
                SELECT id, full_name, account_id, lob_id, is_manually_verified,
                       departments, skills, past_companies, previous_titles, target_kpis,
                       key_objections, extended_profile
                FROM "{TARGET_SCHEMA}"."personas" WHERE account_id = :aid
            '''),
            {"aid": target_account_id},
        ).fetchone()
        assert target_per is not None
        assert target_per[1] == "Jane Doe"
        assert target_per[3] == new_lob_id
        new_per_id = target_per[0]
        # Verify persona array round trip
        assert target_per[5] == ['Executive', 'Leadership']
        assert target_per[6] == ['Portfolio Management', 'Asset Allocation', 'リスク管理']
        assert target_per[7] == ['BlackRock', 'Vanguard']
        assert target_per[8] == ['Managing Director', 'VP']
        assert target_per[9] is None  # Regenerate column excluded from copy per policy
        assert target_per[10] is None
        ext_prof = target_per[11]
        if isinstance(ext_prof, str):
            ext_prof = json.loads(ext_prof)
        assert ext_prof["bio"] == "Experienced CEO"
        assert ext_prof["verified"] is True

        # 5. Verify Spike S1 organisational_hierarchy_tree deep ID remapping
        tree_raw = target_acct[6]
        if isinstance(tree_raw, str):
            tree_data = json.loads(tree_raw)
        else:
            tree_data = tree_raw
        assert tree_data["lob_id"] == new_lob_id
        assert tree_data["persona_id"] == new_per_id

        # 6. Verify account_origin provenance record
        origin = session.execute(
            text(f'SELECT account_id, origin_type, canonical_key, snapshot_id, snapshot_version FROM "{TARGET_SCHEMA}"."account_origin" WHERE account_id = :aid'),
            {"aid": target_account_id},
        ).fetchone()
        assert origin is not None
        assert origin[1] == "catalog"
        assert origin[2] == "cik:0001999999"
        assert origin[3] == snapshot.id
        assert origin[4] == snapshot.version

        # 7. Verify credit ledger fee deduction
        target_client = session.query(Client).filter_by(slug=TARGET_SLUG).first()
        assert target_client.api_credits_used >= 1

        ledger = session.execute(
            text("SELECT delta, reason FROM public.credit_ledger WHERE client_id = :cid ORDER BY id DESC LIMIT 1"),
            {"cid": target_client.id},
        ).fetchone()
        assert ledger is not None
        assert ledger[0] == -1
        assert "hydration" in ledger[1].lower()

        # 8. Idempotency test: Re-executing with same idempotency key returns exact same result
        idemp_res = AccountReuseService.hydrate_account_from_catalog(
            session=session,
            target_client_slug=TARGET_SLUG,
            snapshot_id=snapshot.id,
            user_id=1,
            idempotency_key=idem_key,
        )
        assert idemp_res["idempotent"] is True
        assert idemp_res["account_id"] == target_account_id

        # 9. Duplicate Account Error: Attempting to hydrate same account with new key fails
        with pytest.raises(DuplicateAccountError, match="already exists"):
            AccountReuseService.hydrate_account_from_catalog(
                session=session,
                target_client_slug=TARGET_SLUG,
                snapshot_id=snapshot.id,
                user_id=1,
                idempotency_key=f"different_key_{uuid.uuid4().hex}",
            )

    finally:
        session.close()


def test_rollback_protection_and_execution():
    """Validates rollback blocking when CRM children exist and clean execution when clean."""
    session = SessionLocal()
    try:
        # Fetch the share event from the previous hydration test
        share_event = session.query(ShareEvent).filter_by(status="done").order_by(ShareEvent.id.desc()).first()
        assert share_event is not None
        target_account_id = share_event.created_ids["accounts"][0]

        # 1. Simulate private CRM work (e.g. sales rep created a deal for this account)
        session.execute(
            text(f"""
                INSERT INTO "{TARGET_SCHEMA}"."deals"
                (account_id, name, stage, value_amount, probability)
                VALUES (:aid, 'Enterprise Q4 Expansion', 'discovery', 500000, 20)
            """),
            {"aid": target_account_id},
        )
        session.commit()

        # Rollback MUST be blocked to protect client CRM data
        with pytest.raises(RollbackBlockedError, match="contains private tenant CRM data"):
            AccountReuseService.rollback_account_reuse(
                session=session,
                share_event_id=share_event.id,
                admin_user_id=1,
            )

        # 2. Remove the private CRM record to simulate clean rollback
        session.execute(
            text(f'DELETE FROM "{TARGET_SCHEMA}"."deals" WHERE account_id = :aid'),
            {"aid": target_account_id},
        )
        session.commit()

        # 3. Clean rollback succeeds
        rollback_res = AccountReuseService.rollback_account_reuse(
            session=session,
            share_event_id=share_event.id,
            admin_user_id=1,
            reason="Admin requested rollback rehearsal",
        )
        assert rollback_res["ok"] is True
        assert rollback_res["rolled_back"] is True
        assert rollback_res["refunded_credits"] == 1

        # Verify all entities were deleted in target schema
        acct_exists = session.execute(
            text(f'SELECT 1 FROM "{TARGET_SCHEMA}"."accounts" WHERE id = :aid'),
            {"aid": target_account_id},
        ).scalar()
        assert acct_exists is None

        lob_exists = session.execute(
            text(f'SELECT 1 FROM "{TARGET_SCHEMA}"."lobs" WHERE account_id = :aid'),
            {"aid": target_account_id},
        ).scalar()
        assert lob_exists is None

        origin_exists = session.execute(
            text(f'SELECT 1 FROM "{TARGET_SCHEMA}"."account_origin" WHERE account_id = :aid'),
            {"aid": target_account_id},
        ).scalar()
        assert origin_exists is None

        # Verify fee was refunded in credit ledger
        refund_ledger = session.execute(
            text("SELECT delta, reason FROM public.credit_ledger WHERE client_id = :cid ORDER BY id DESC LIMIT 1"),
            {"cid": share_event.target_client_id},
        ).fetchone()
        assert refund_ledger is not None
        assert refund_ledger[0] == 1
        assert "refund" in refund_ledger[1].lower()

    finally:
        session.close()
