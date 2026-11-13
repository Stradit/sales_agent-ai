"""Test Suite: S-7 Catalog Company Deduplication and Merge Workflow.

Validates:
1. Domain-first then CIK publish cleanly merges into one company with upgraded canonical_key.
2. Conflicting CIK for the same domain creates a merge candidate, sets needs_review, and quarantines snapshot.
3. Super-admin merge resolution moves identifiers, moves and renumbers snapshots monotonically, and marks merged_into_id.
4. Lookup by old identifier follows merged_into_id chain to the survivor.
"""

from datetime import datetime, timezone
import random
import uuid
import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient

from api import app
import auth
from db.connection import SessionLocal, engine
from db.models.client import Client
from db.models.user import User
from db.models.platform_catalog import Company, CompanyIdentifier, MergeCandidate, Snapshot
from db.provisioning import create_client_schema, drop_client_schema
from services.catalog_service import CatalogService

TEST_SOURCE_SLUG = "test_src_merge_001"
TEST_SOURCE_SCHEMA = f"client_{TEST_SOURCE_SLUG}"


@pytest.fixture(scope="module", autouse=True)
def setup_merge_env():
    session = SessionLocal()
    try:
        # Create test client
        client = session.query(Client).filter_by(slug=TEST_SOURCE_SLUG).first()
        if not client:
            client = Client(
                name="Merge Test Source Corp",
                slug=TEST_SOURCE_SLUG,
                schema_name=TEST_SOURCE_SCHEMA,
                status="active",
                catalog_publish=True,
                catalog_consume=True,
            )
            session.add(client)
            session.commit()
        create_client_schema(TEST_SOURCE_SLUG)
    finally:
        session.close()

    yield

    # Teardown
    engine.dispose()
    drop_client_schema(TEST_SOURCE_SLUG)
    clean_session = SessionLocal()
    try:
        clean_session.execute(text(f"DELETE FROM public.clients WHERE slug = '{TEST_SOURCE_SLUG}'"))
        clean_session.execute(text("DELETE FROM platform_catalog.company_identifiers WHERE id_value LIKE '%merge%'"))
        clean_session.commit()
    finally:
        clean_session.close()


def test_domain_first_then_cik_merges_and_upgrades_key():
    """Publishing domain first, then CIK upgrades the canonical key and merges identifiers."""
    session = SessionLocal()
    try:
        uid = uuid.uuid4().hex[:6]
        domain = f"upgrade-{uid}.com"
        cik = f"{random.randint(1000000, 9999999):010d}"
        acc_key = f"acc_merge_{uid}"

        # 1. Insert account with domain only
        session.execute(text(f"""
            INSERT INTO "{TEST_SOURCE_SCHEMA}"."accounts" 
            (key, display_name, legal_name, domain)
            VALUES ('{acc_key}', 'Merge Corp', 'Merge Corporation Inc', '{domain}')
        """))
        session.commit()
        acc_id = session.execute(text(f'SELECT id FROM "{TEST_SOURCE_SCHEMA}"."accounts" WHERE key = \'{acc_key}\'')).scalar()

        # Publish 1: domain only
        snap1 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=TEST_SOURCE_SLUG,
            account_id=acc_id,
            force=True,
        )
        assert snap1 is not None
        comp1 = session.query(Company).filter_by(id=snap1.company_id).first()
        assert comp1.canonical_key == f"dom:{domain}"

        # 2. Update account with SEC CIK
        session.execute(text(f"""
            UPDATE "{TEST_SOURCE_SCHEMA}"."accounts"
            SET sec_cik = '{cik}'
            WHERE id = {acc_id}
        """))
        session.commit()

        # Publish 2: CIK included
        snap2 = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=TEST_SOURCE_SLUG,
            account_id=acc_id,
            force=True,
        )
        assert snap2 is not None

        # Verify same company was reused and key was upgraded to CIK
        assert snap2.company_id == comp1.id
        session.refresh(comp1)
        assert comp1.canonical_key == f"cik:{cik}"

        # Verify all identifiers are attached to comp1
        idents = {
            (i.id_type, i.id_value)
            for i in session.query(CompanyIdentifier).filter_by(company_id=comp1.id).all()
        }
        assert ("domain", domain) in idents
        assert ("cik", cik) in idents
        assert ("key", f"dom:{domain}") in idents

        # Lookups by domain, CIK, and old key all return comp1
        assert CatalogService.get_company_by_identifier(session, domain).id == comp1.id
        assert CatalogService.get_company_by_identifier(session, cik).id == comp1.id
        assert CatalogService.get_company_by_identifier(session, f"dom:{domain}").id == comp1.id
    finally:
        session.close()


def test_conflicting_cik_creates_merge_candidate_and_quarantines():
    """Conflicting strong identifiers creates a merge candidate, sets needs_review, and quarantines snapshot."""
    session = SessionLocal()
    try:
        uid = uuid.uuid4().hex[:6]
        domain = f"conflict-{uid}.com"
        cik_a = f"{random.randint(1000000, 4999999):010d}"
        cik_b = f"{random.randint(5000000, 9999999):010d}"
        acc_key = f"acc_conflict_{uid}"

        # 1. Company A with cik_a and domain
        comp_a = Company(
            canonical_key=f"cik:{cik_a}",
            legal_name="Conflict Corp A",
            display_name="Conflict Corp A",
        )
        session.add(comp_a)
        session.flush()
        session.add(CompanyIdentifier(company_id=comp_a.id, id_type="cik", id_value=cik_a))
        session.add(CompanyIdentifier(company_id=comp_a.id, id_type="domain", id_value=domain))
        session.commit()

        # 2. Incoming account with same domain but conflicting CIK (cik_b)
        session.execute(text(f"""
            INSERT INTO "{TEST_SOURCE_SCHEMA}"."accounts" 
            (key, display_name, legal_name, domain, sec_cik)
            VALUES ('{acc_key}', 'Conflict Corp B', 'Conflict Corp B LLC', '{domain}', '{cik_b}')
        """))
        session.commit()
        acc_id = session.execute(text(f'SELECT id FROM "{TEST_SOURCE_SCHEMA}"."accounts" WHERE key = \'{acc_key}\'')).scalar()

        # 3. Publish account snapshot
        snap = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=TEST_SOURCE_SLUG,
            account_id=acc_id,
        )

        # Snapshot MUST be quarantined
        assert snap.status == "quarantined"

        # Verify merge candidate created
        candidate = session.query(MergeCandidate).filter(
            MergeCandidate.company_a_id == comp_a.id,
            MergeCandidate.status == "pending",
        ).first()
        assert candidate is not None
        assert "Conflicting cik" in candidate.reason

        # Both companies flagged needs_review
        session.refresh(comp_a)
        assert comp_a.needs_review is True
        comp_b = session.query(Company).filter_by(id=candidate.company_b_id).first()
        assert comp_b is not None
        assert comp_b.needs_review is True
    finally:
        session.close()


def test_resolve_merge_candidate_flow():
    """Admin resolves merge candidate: moves identifiers, renumbers snapshots monotonically, marks merged_into_id."""
    session = SessionLocal()
    try:
        uid = uuid.uuid4().hex[:6]
        cik_s = f"{random.randint(1000000, 4999999):010d}"
        cik_l = f"{random.randint(5000000, 9999999):010d}"
        dom_s = f"survivor-{uid}.com"
        dom_l = f"loser-{uid}.com"

        # Create candidate with two companies
        comp_survivor = Company(canonical_key=f"cik:{cik_s}", legal_name="Survivor Corp")
        comp_loser = Company(canonical_key=f"cik:{cik_l}", legal_name="Loser Corp")
        session.add_all([comp_survivor, comp_loser])
        session.flush()

        session.add(CompanyIdentifier(company_id=comp_survivor.id, id_type="cik", id_value=cik_s))
        session.add(CompanyIdentifier(company_id=comp_survivor.id, id_type="domain", id_value=dom_s))
        session.add(CompanyIdentifier(company_id=comp_loser.id, id_type="cik", id_value=cik_l))
        session.add(CompanyIdentifier(company_id=comp_loser.id, id_type="domain", id_value=dom_l))

        # Add snapshots to both
        s1 = Snapshot(
            company_id=comp_survivor.id,
            version=1,
            status="ready",
            source_client_id=1,
            source_account_id=1,
            policy_version="1.0.0",
            skeleton_version="T0002",
            checksum=f"chk1_{uid}",
            taken_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        s2 = Snapshot(
            company_id=comp_loser.id,
            version=1,
            status="ready",
            source_client_id=1,
            source_account_id=2,
            policy_version="1.0.0",
            skeleton_version="T0002",
            checksum=f"chk2_{uid}",
            taken_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        session.add_all([s1, s2])

        candidate = MergeCandidate(
            company_a_id=comp_survivor.id,
            company_b_id=comp_loser.id,
            reason="Duplicate test",
            evidence={},
            status="pending",
        )
        session.add(candidate)
        session.commit()

        # Resolve merge candidate via CatalogService
        res = CatalogService.resolve_merge_candidate(
            session=session,
            candidate_id=candidate.id,
            action="merge",
            keep_company_id=comp_survivor.id,
            admin_user_id=1,
        )
        assert res.status == "merged"

        # Verify loser marked merged_into_id
        session.refresh(comp_loser)
        assert comp_loser.merged_into_id == comp_survivor.id
        assert comp_loser.needs_review is False

        session.refresh(comp_survivor)
        assert comp_survivor.needs_review is False

        # Verify loser's domain identifier was moved to survivor
        loser_domain_ident = session.query(CompanyIdentifier).filter_by(
            id_type="domain", id_value=dom_l
        ).first()
        assert loser_domain_ident.company_id == comp_survivor.id

        # Verify snapshots were moved and renumbered monotonically
        surv_snaps = session.query(Snapshot).filter_by(company_id=comp_survivor.id).order_by(Snapshot.version).all()
        assert len(surv_snaps) == 2
        assert [s.version for s in surv_snaps] == [1, 2]

        # Verify lookups by loser's identifiers now follow merged_into_id to survivor
        found = CatalogService.get_company_by_identifier(session, dom_l)
        assert found is not None
        assert found.id == comp_survivor.id

        found_by_cik = CatalogService.get_company_by_identifier(session, cik_l)
        assert found_by_cik is not None
        assert found_by_cik.id == comp_survivor.id

        # Latest snapshot for loser_id returns the survivor's latest snapshot
        latest = CatalogService.get_latest_snapshot(session, comp_loser.id)
        assert latest is not None
        assert latest.company_id == comp_survivor.id
        assert latest.version == 2
    finally:
        session.close()


def test_api_resolve_merge_candidate_endpoint():
    """Tests the super-admin API endpoint for resolving merge candidates."""
    client = TestClient(app)
    session = SessionLocal()
    try:
        uid = uuid.uuid4().hex[:6]
        admin_user = session.query(User).filter_by(role="super_admin").first()
        if not admin_user:
            admin_user = User(
                email=f"superadmin_{uid}@test.com",
                role="super_admin",
                is_active=True,
            )
            session.add(admin_user)
            session.commit()
        token = auth.create_access_token(admin_user)
        headers = {"Authorization": f"Bearer {token}"}

        cik_1 = f"{random.randint(1000000, 4999999):010d}"
        cik_2 = f"{random.randint(5000000, 9999999):010d}"
        c1 = Company(canonical_key=f"cik:{cik_1}", legal_name="API Merge 1")
        c2 = Company(canonical_key=f"cik:{cik_2}", legal_name="API Merge 2")
        session.add_all([c1, c2])
        session.flush()

        candidate = MergeCandidate(
            company_a_id=c1.id,
            company_b_id=c2.id,
            reason="API merge test",
            evidence={},
            status="pending",
        )
        session.add(candidate)
        session.commit()
        cand_id = candidate.id
    finally:
        session.close()

    resp = client.post(
        f"/api/catalog/merge-candidates/{cand_id}/resolve",
        headers=headers,
        json={"action": "dismiss"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["success"] is True
    assert data["status"] == "dismissed"
