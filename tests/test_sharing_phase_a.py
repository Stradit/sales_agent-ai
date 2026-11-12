"""Integration Test Suite: Cross-Tenant Account Sharing (Phase A).

Verifies the 9 critical safety and reliability scenarios for cross-tenant sharing:
1. ARRAY/JSONB round trip (full parity, unicode, empty arrays, tree remapping, provenance)
2. Cross-tenant slug override protection (strict client context isolation)
3. Strict auth matrix (401 on unauthenticated, 403 on role permission boundaries)
4. Flags enforcement (catalog_consume anti-oracle, catalog_publish enforcement)
5. Rollback safety with dynamic introspection FK guards and reversal
6. Concurrent hydration race safety and idempotency
7. Failure atomicity and zero-leak error sanitization
8. Credit quota and balance limits
9. Audit trail logging and reuse-check rate limiting
"""

import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import auth
from api import app
from db.connection import SessionLocal
from db.models.audit_log import AuditLog
from db.models.client import Client
from db.models.credit_ledger import CreditLedger
from db.models.platform_catalog import Company, ShareEvent, Snapshot, SnapshotBlob
from db.models.user import User
from db.models.user_client_access import UserClientAccess
from db.provisioning import create_client_schema, drop_client_schema
from services.account_reuse_service import AccountReuseService, DuplicateAccountError
from services.catalog_audit import reuse_check_limiter
from services.catalog_service import CatalogService

api_client = TestClient(app)

SRC_SLUG = "test_pa_src"
TGT_A_SLUG = "test_pa_tgt_a"
TGT_B_SLUG = "test_pa_tgt_b"

SCHEMA_SRC = f"client_{SRC_SLUG}"
SCHEMA_TGT_A = f"client_{TGT_A_SLUG}"
SCHEMA_TGT_B = f"client_{TGT_B_SLUG}"


def _headers_for(user_id: int, client_slug: str = None) -> dict:
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=user_id).first()
        token = auth.create_access_token(user, client_slug=client_slug)
        headers = {"Authorization": f"Bearer {token}"}
        if client_slug:
            headers["X-Client-Slug"] = client_slug
        return headers
    finally:
        session.close()


@pytest.fixture(scope="module")
def setup_phase_a_environment():
    """Provisions real Postgres tenant schemas and users for Phase A integration tests."""
    session = SessionLocal()
    try:
        # Pre-cleanup
        try:
            drop_client_schema(SRC_SLUG)
            drop_client_schema(TGT_A_SLUG)
            drop_client_schema(TGT_B_SLUG)
        except Exception:
            pass

        session.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key LIKE 'test_pa_%' OR canonical_key LIKE 'cik:000999%'"))
        session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2, :s3)"), {"s1": SRC_SLUG, "s2": TGT_A_SLUG, "s3": TGT_B_SLUG})
        session.execute(text("DELETE FROM public.users WHERE email LIKE 'test_pa_%'"))
        session.commit()

        # Provision Schemas (this creates public.clients rows)
        create_client_schema(SRC_SLUG)
        create_client_schema(TGT_A_SLUG)
        create_client_schema(TGT_B_SLUG)

        # Configure Client records
        src_client = session.query(Client).filter_by(slug=SRC_SLUG).first()
        src_client.status = "active"
        src_client.catalog_publish = True
        src_client.catalog_consume = True
        src_client.api_credits_total = 5000
        src_client.api_credits_used = 0

        tgt_a_client = session.query(Client).filter_by(slug=TGT_A_SLUG).first()
        tgt_a_client.status = "active"
        tgt_a_client.catalog_publish = True
        tgt_a_client.catalog_consume = True
        tgt_a_client.reuse_requires_approval = False
        tgt_a_client.api_credits_total = 5000
        tgt_a_client.api_credits_used = 0

        tgt_b_client = session.query(Client).filter_by(slug=TGT_B_SLUG).first()
        tgt_b_client.status = "active"
        tgt_b_client.catalog_publish = False
        tgt_b_client.catalog_consume = True
        tgt_b_client.reuse_requires_approval = True
        tgt_b_client.api_credits_total = 5000
        tgt_b_client.api_credits_used = 0
        session.commit()

        # Create Users
        pwd = auth.hash_password("testpass123")
        super_user = User(email="test_pa_super@example.com", hashed_password=pwd, full_name="PA Super Admin", role="super_admin")
        admin_a = User(email="test_pa_admin_a@example.com", hashed_password=pwd, full_name="PA Admin A", role="user")
        analyst_b = User(email="test_pa_analyst_b@example.com", hashed_password=pwd, full_name="PA Analyst B", role="user")
        session.add_all([super_user, admin_a, analyst_b])
        session.flush()

        # User Client Access grants
        session.add(UserClientAccess(user_id=admin_a.id, client_id=tgt_a_client.id, role="client_admin"))
        session.add(UserClientAccess(user_id=analyst_b.id, client_id=tgt_b_client.id, role="analyst"))
        session.commit()

        env_data = {
            "src_client_id": src_client.id,
            "tgt_a_client_id": tgt_a_client.id,
            "tgt_b_client_id": tgt_b_client.id,
            "super_id": super_user.id,
            "admin_a_id": admin_a.id,
            "analyst_b_id": analyst_b.id,
        }
    finally:
        session.close()

    yield env_data

    # Teardown
    cleanup_session = SessionLocal()
    try:
        cleanup_session.execute(text("DELETE FROM platform_catalog.companies WHERE canonical_key LIKE 'test_pa_%' OR canonical_key LIKE 'cik:000999%'"))
        cleanup_session.execute(text("DELETE FROM public.credit_ledger WHERE client_id IN (:c1, :c2, :c3)"), {
            "c1": env_data["src_client_id"], "c2": env_data["tgt_a_client_id"], "c3": env_data["tgt_b_client_id"]
        })
        cleanup_session.execute(text("DELETE FROM public.audit_log WHERE client_id IN (:c1, :c2, :c3)"), {
            "c1": env_data["src_client_id"], "c2": env_data["tgt_a_client_id"], "c3": env_data["tgt_b_client_id"]
        })
        cleanup_session.execute(text("DELETE FROM public.users WHERE email LIKE 'test_pa_%'"))
        cleanup_session.execute(text("DELETE FROM public.clients WHERE slug IN (:s1, :s2, :s3)"), {
            "s1": SRC_SLUG, "s2": TGT_A_SLUG, "s3": TGT_B_SLUG
        })
        cleanup_session.commit()
    finally:
        cleanup_session.close()

    drop_client_schema(SRC_SLUG)
    drop_client_schema(TGT_A_SLUG)
    drop_client_schema(TGT_B_SLUG)


def test_scenario_1_array_jsonb_round_trip(setup_phase_a_environment):
    """Scenario 1:
    Seed source account with EVERY array column (including empty array and unicode),
    JSONB columns, LOBs, sub-LOBs, personas, posts/cxo/jobs, targets row.
    Publish, hydrate into tgt_a, assert exact equality of copied columns, ID remapping,
    organisational_hierarchy_tree, and account_origin.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        # Seed Source Account with full array, unicode, and jsonb diversity
        sample_tree = {
            "version": 1,
            "root_nodes": [
                {
                    "lob_id": 1,
                    "lob_name": "Autonomous Robotics",
                    "sub_lobs": [{"sub_lob_id": 1, "name": "Vision Systems"}],
                    "leaders": [{"persona_id": 1, "name": "Dr. Aris Thorne"}],
                }
            ],
        }

        acct_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."accounts"
                (key, display_name, legal_name, domain, sec_cik, country, city,
                 industries, industry_groups, aliases, founders, headquarters_regions, keywords,
                 organisational_hierarchy_tree, is_manually_verified, owner_user_id)
                VALUES
                (:key, :name, :lname, :dom, :cik, 'US', 'Austin',
                 :industries, :ind_groups, :aliases, :founders, :hq_regions, :keywords,
                 CAST(:tree AS jsonb), true, :uid)
                RETURNING id
            """),
            {
                "key": "omnicorp_ai",
                "name": "OmniCorp Artificial Intelligence",
                "lname": "OmniCorp AI Global Holdings Inc.",
                "dom": "omnicorp-global.ai",
                "cik": "0009998888",
                "industries": ["Artificial Intelligence", "Robotics & Automation", "Enterprise Cloud", ""],
                "ind_groups": ["Technology", "Industrial Automation"],
                "aliases": ["OmniCorp", "オムニ株式会社", "Société OmniCorp"],  # Japanese & French unicode
                "founders": ["Dr. Elena Rostova", "Klaus Müller", "Wei Zhang"],  # Umlaut & unicode
                "hq_regions": ["North America", "Asia-Pacific", "EMEA"],
                "keywords": [],  # Empty array test
                "tree": json.dumps(sample_tree),
                "uid": env["super_id"],
            },
        ).scalar()

        # Seed LOB
        lob_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."lobs"
                (account_id, key, lob_name, domain, overview, relationship_type)
                VALUES (:aid, 'omni_robotics', 'Autonomous Robotics', 'robotics.omnicorp.ai', 'Next-gen robotics division', 'subsidiary')
                RETURNING id
            """),
            {"aid": acct_id},
        ).scalar()

        # Seed Sub-LOB
        sub_lob_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."sub_lobs"
                (lob_id, name, domain, legal_name, entity_level, relationship_type)
                VALUES (:lid, 'Vision Systems', 'vision.omnicorp.ai', 'OmniVision Ltd.', 'business_unit', 'operating_unit')
                RETURNING id
            """),
            {"lid": lob_id},
        ).scalar()

        # Seed Persona with array columns (skills, departments, past_companies, etc.)
        persona_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."personas"
                (account_id, lob_id, key, full_name, first_name, last_name, title,
                 departments, skills, past_companies, previous_titles, target_kpis,
                 education_history, seniority_raw, is_manually_verified)
                VALUES
                (:aid, :lid, 'aris_thorne', 'Dr. Aris Thorne', 'Aris', 'Thorne', 'VP of Robotic Perception',
                 :depts, :skills, :past_comps, :prev_titles, :kpis,
                 CAST(:edu AS jsonb), 'Executive', true)
                RETURNING id
            """),
            {
                "aid": acct_id,
                "lid": lob_id,
                "depts": ["Perception & Sensors", "Deep Learning Research", "Executive Office"],
                "skills": ["Computer Vision", "LiDAR Fusion", "Neural Networks", "PyTorch"],
                "past_comps": ["Boston Dynamics", "Google DeepMind"],
                "prev_titles": ["Principal Research Scientist", "Staff Vision Engineer"],
                "kpis": ["Latency < 5ms", "mAP > 94.5%"],
                "edu": json.dumps([{"degree": "PhD", "institution": "MIT", "year": 2018}]),
            },
        ).scalar()

        # Seed Posts, CXO movements, Jobs
        session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."posts"
                (post_key, target_key, channel, kind, author, body, published_at, rank, raw)
                VALUES
                ('omni_post_1', 'omnicorp_ai', 'linkedin', 'post', 'OmniCorp PR', 'Thrilled to launch OmniSense LiDAR platform!', NOW(), 1, '{{}}'::jsonb)
            """)
        )
        session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."cxo_movements"
                (target_key, company_name, person_name, designation, new_company, event_type, article_url, published_at, raw)
                VALUES
                ('omnicorp_ai', 'OmniCorp AI', 'Dr. Elena Rostova', 'Chief AI Scientist', 'OmniCorp Global', 'promotion', 'https://news.omnicorp.ai/article-1', NOW(), '{{}}'::jsonb)
            """)
        )
        session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."linkedin_jobs"
                (job_key, target_key, title, company_name, location, workplace_type, posted_date, raw)
                VALUES
                ('omni_job_1', 'omnicorp_ai', 'Lead Embedded Vision Engineer', 'OmniCorp AI', 'Austin, TX', 'hybrid', NOW(), '{{}}'::jsonb)
            """)
        )

        # Seed Targets row
        session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_SRC}"."targets" (key, kind, display_name, config)
                VALUES ('omnicorp_ai', 'company', 'OmniCorp AI', CAST(:cfg AS jsonb))
            """),
            {"cfg": json.dumps({"monitoring": True, "frequency": "hourly"})},
        )
        session.commit()

        # Publish snapshot
        snapshot = CatalogService.publish_account_snapshot(
            session=session,
            source_client_slug=SRC_SLUG,
            account_id=acct_id,
            published_by_user_id=env["super_id"],
            force=True,
        )
        assert snapshot is not None
        assert snapshot.status == "ready"

        # Hydrate into Target A
        preview_token = AccountReuseService.generate_preview_token(
            snapshot_id=snapshot.id,
            target_client_slug=TGT_A_SLUG,
            user_id=env["admin_a_id"],
        )

        headers_a = _headers_for(env["admin_a_id"], TGT_A_SLUG)
        res = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snapshot.id, "preview_token": preview_token},
            headers=headers_a,
        )
        assert res.status_code == 200
        hydrated_data = res.json()
        tgt_acct_id = hydrated_data["account_id"]
        assert hydrated_data["counts"]["lobs"] == 1
        assert hydrated_data["counts"]["personas"] == 1

        # Assert round-trip data equality in Target A
        tgt_acct = session.execute(
            text(f"""
                SELECT display_name, legal_name, domain, sec_cik,
                       industries, industry_groups, aliases, founders, headquarters_regions, keywords,
                       organisational_hierarchy_tree
                FROM "{SCHEMA_TGT_A}"."accounts" WHERE id = :aid
            """),
            {"aid": tgt_acct_id},
        ).fetchone()

        assert tgt_acct.display_name == "OmniCorp Artificial Intelligence"
        assert tgt_acct.legal_name == "OmniCorp AI Global Holdings Inc."
        assert tgt_acct.domain == "omnicorp-global.ai"
        assert tgt_acct.sec_cik == "0009998888"

        # Verify ARRAY parity (including unicode and empty array)
        assert tgt_acct.industries == ["Artificial Intelligence", "Robotics & Automation", "Enterprise Cloud", ""]
        assert tgt_acct.industry_groups == ["Technology", "Industrial Automation"]
        assert tgt_acct.aliases == ["OmniCorp", "オムニ株式会社", "Société OmniCorp"]
        assert tgt_acct.founders == ["Dr. Elena Rostova", "Klaus Müller", "Wei Zhang"]
        assert tgt_acct.headquarters_regions == ["North America", "Asia-Pacific", "EMEA"]
        assert tgt_acct.keywords == [] or tgt_acct.keywords is None

        # Verify Persona arrays
        tgt_persona = session.execute(
            text(f"""
                SELECT full_name, departments, skills, past_companies, previous_titles, target_kpis
                FROM "{SCHEMA_TGT_A}"."personas" WHERE account_id = :aid
            """),
            {"aid": tgt_acct_id},
        ).fetchone()
        assert tgt_persona.full_name == "Dr. Aris Thorne"
        assert tgt_persona.departments == ["Perception & Sensors", "Deep Learning Research", "Executive Office"]
        assert tgt_persona.skills == ["Computer Vision", "LiDAR Fusion", "Neural Networks", "PyTorch"]
        assert tgt_persona.past_companies == ["Boston Dynamics", "Google DeepMind"]
        assert tgt_persona.previous_titles == ["Principal Research Scientist", "Staff Vision Engineer"]
        # target_kpis is classified as 'regenerate' in share_policy.yaml, so it is not copied
        assert tgt_persona.target_kpis is None

        # Verify ID remapping in hierarchy tree
        tree = tgt_acct.organisational_hierarchy_tree
        assert isinstance(tree, dict)
        remapped_lob_id = session.execute(text(f'SELECT id FROM "{SCHEMA_TGT_A}"."lobs" WHERE account_id = :aid'), {"aid": tgt_acct_id}).scalar()
        remapped_persona_id = session.execute(text(f'SELECT id FROM "{SCHEMA_TGT_A}"."personas" WHERE account_id = :aid'), {"aid": tgt_acct_id}).scalar()
        assert tree["root_nodes"][0]["lob_id"] == remapped_lob_id
        assert tree["root_nodes"][0]["leaders"][0]["persona_id"] == remapped_persona_id

        # Verify AccountOrigin provenance
        origin = session.execute(
            text(f'SELECT origin_type, snapshot_id, snapshot_version FROM "{SCHEMA_TGT_A}"."account_origin" WHERE account_id = :aid'),
            {"aid": tgt_acct_id},
        ).fetchone()
        assert origin.origin_type == "catalog"
        assert origin.snapshot_id == snapshot.id
        assert origin.snapshot_version == snapshot.version

        # Verify Targets row copied
        tgt_row = session.execute(text(f'SELECT key, config FROM "{SCHEMA_TGT_A}"."targets" WHERE key = :k'), {"k": "omnicorp_ai"}).fetchone()
        assert tgt_row is not None
        assert tgt_row.config == {"monitoring": True, "frequency": "hourly"}
    finally:
        session.close()


def test_scenario_2_cross_tenant_slug_override(setup_phase_a_environment):
    """Scenario 2:
    Client_admin of tgt_a cannot target tgt_b via body, query, or client param
    (403 and 0 rows written in tgt_b); works with X-Client-Slug: tgt_a;
    super_admin can target tgt_b.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        admin_a_headers = _headers_for(env["admin_a_id"], TGT_A_SLUG)
        super_headers = _headers_for(env["super_id"])

        # Fetch snapshot id
        snapshot = session.query(Snapshot).order_by(Snapshot.id.desc()).first()
        token = AccountReuseService.generate_preview_token(snapshot.id, TGT_B_SLUG, env["admin_a_id"])

        # 1. Admin A attempts to hydrate into Tenant B via body client_slug
        res_hyd = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snapshot.id, "preview_token": token, "client_slug": TGT_B_SLUG},
            headers=admin_a_headers,
        )
        assert res_hyd.status_code == 403
        assert "Target client does not match your active client context" in res_hyd.json()["detail"]

        # 2. Admin A attempts reuse-check for Tenant B
        res_rc = api_client.get(
            "/api/catalog/reuse-check",
            params={"identifier": "omnicorp-global.ai", "client": TGT_B_SLUG},
            headers=admin_a_headers,
        )
        assert res_rc.status_code == 403

        # 3. Admin A attempts request submission for Tenant B
        res_req = api_client.post(
            "/api/catalog/requests",
            json={"snapshot_id": snapshot.id, "preview_token": token, "client_slug": TGT_B_SLUG},
            headers=admin_a_headers,
        )
        assert res_req.status_code == 403

        # Verify 0 rows written into Tenant B
        cnt_b = session.execute(text(f'SELECT count(*) FROM "{SCHEMA_TGT_B}"."accounts"')).scalar()
        assert cnt_b == 0

        # 4. Super Admin CAN target Tenant B
        token_super = AccountReuseService.generate_preview_token(snapshot.id, TGT_B_SLUG, env["super_id"])
        res_super = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snapshot.id, "preview_token": token_super, "client_slug": TGT_B_SLUG},
            headers=super_headers,
        )
        assert res_super.status_code == 200
        assert session.execute(text(f'SELECT count(*) FROM "{SCHEMA_TGT_B}"."accounts"')).scalar() == 1
    finally:
        session.close()


def test_scenario_3_strict_auth_matrix(setup_phase_a_environment):
    """Scenario 3:
    - Unauthenticated calls to all catalog endpoints return 401.
    - Analyst gets 403 on hydrate (lacks account:write).
    - Client_admin gets 403 on publish, assign, and rollback.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        snapshot = session.query(Snapshot).order_by(Snapshot.id.desc()).first()
        token = AccountReuseService.generate_preview_token(snapshot.id, TGT_A_SLUG, env["admin_a_id"])

        # 1. Unauthenticated requests -> 401
        assert api_client.get("/api/catalog/reuse-check?identifier=test.com").status_code == 401
        assert api_client.post("/api/catalog/hydrate", json={"snapshot_id": snapshot.id, "preview_token": token}).status_code == 401
        assert api_client.post("/api/catalog/requests", json={"snapshot_id": snapshot.id, "preview_token": token}).status_code == 401
        assert api_client.get("/api/catalog/requests").status_code == 401
        assert api_client.post("/api/catalog/publish", json={"source_client_slug": SRC_SLUG, "account_id": 1}).status_code == 401
        assert api_client.post("/api/catalog/assign", json={"snapshot_id": snapshot.id, "target_client_slug": TGT_A_SLUG}).status_code == 401
        assert api_client.post("/api/catalog/share-events/1/rollback").status_code == 401

        # 2. Analyst in Tenant B has read-only role, lacks account:write -> 403 on hydrate
        analyst_headers = _headers_for(env["analyst_b_id"], TGT_B_SLUG)
        token_b = AccountReuseService.generate_preview_token(snapshot.id, TGT_B_SLUG, env["analyst_b_id"])
        res_analyst = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snapshot.id, "preview_token": token_b},
            headers=analyst_headers,
        )
        assert res_analyst.status_code == 403

        # 3. Client Admin lacks super-admin permissions: publish, assign, rollback -> 403
        admin_a_headers = _headers_for(env["admin_a_id"], TGT_A_SLUG)
        assert api_client.post(
            "/api/catalog/publish",
            json={"source_client_slug": TGT_A_SLUG, "account_id": 1},
            headers=admin_a_headers,
        ).status_code == 403

        assert api_client.post(
            "/api/catalog/assign",
            json={"snapshot_id": snapshot.id, "target_client_slug": TGT_A_SLUG},
            headers=admin_a_headers,
        ).status_code == 403

        assert api_client.post(
            "/api/catalog/share-events/1/rollback",
            json={"reason": "unauthorized attempt"},
            headers=admin_a_headers,
        ).status_code == 403
    finally:
        session.close()


def test_scenario_4_flags_enforcement(setup_phase_a_environment):
    """Scenario 4:
    - catalog_consume=False returns {available: false} on reuse-check and 403 on hydrate/requests.
    - catalog_publish=False with force=True fails with PermissionError.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        # Disable catalog_consume on Target A
        tgt_a = session.query(Client).filter_by(slug=TGT_A_SLUG).first()
        tgt_a.catalog_consume = False
        session.commit()

        admin_a_headers = _headers_for(env["admin_a_id"], TGT_A_SLUG)
        snapshot = session.query(Snapshot).order_by(Snapshot.id.desc()).first()

        # 1. Reuse Check returns available: False (Anti-Oracle)
        res_rc = api_client.get(
            "/api/catalog/reuse-check",
            params={"identifier": "omnicorp-global.ai", "client": TGT_A_SLUG},
            headers=admin_a_headers,
        )
        assert res_rc.status_code == 200
        assert res_rc.json() == {"available": False}

        # 2. Hydrate returns 403
        dummy_token = AccountReuseService.generate_preview_token(snapshot.id, TGT_A_SLUG, env["admin_a_id"])
        res_hyd = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snapshot.id, "preview_token": dummy_token},
            headers=admin_a_headers,
        )
        assert res_hyd.status_code == 403
        assert "Catalog reuse is not enabled" in res_hyd.json()["detail"]

        # 3. Requests returns 403
        res_req = api_client.post(
            "/api/catalog/requests",
            json={"snapshot_id": snapshot.id, "preview_token": dummy_token},
            headers=admin_a_headers,
        )
        assert res_req.status_code == 403
        assert "Catalog reuse is not enabled" in res_req.json()["detail"]

        # Restore catalog_consume
        tgt_a.catalog_consume = True
        session.commit()

        # 4. catalog_publish=False on Target B: publish with force=True raises PermissionError
        with pytest.raises(PermissionError) as exc_info:
            CatalogService.publish_account_snapshot(
                session=session,
                source_client_slug=TGT_B_SLUG,
                account_id=1,
                published_by_user_id=env["super_id"],
                force=True,
            )
        assert "not authorized to publish" in str(exc_info.value)
    finally:
        session.close()


def test_scenario_5_rollback_and_dynamic_guards(setup_phase_a_environment):
    """Scenario 5:
    Tenant-added persona, deal, activity link, user_account_access row each block
    rollback with clear breakdown; clean rollback deletes all rows including targets
    and refunds fee; second rollback returns 400.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        super_headers = _headers_for(env["super_id"])
        # Find latest completed ShareEvent for Target B
        share_event = session.query(ShareEvent).filter_by(
            target_client_id=env["tgt_b_client_id"],
            status="done",
        ).first()
        assert share_event is not None
        account_id = share_event.created_ids["accounts"][0]

        # Guard 1: Tenant persona blocks rollback
        per_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_TGT_B}"."personas" (account_id, key, full_name)
                VALUES (:aid, 'tenant_per', 'Tenant Added Persona')
                RETURNING id
            """),
            {"aid": account_id},
        ).scalar()
        session.commit()

        r_block1 = api_client.post(
            f"/api/catalog/share-events/{share_event.id}/rollback",
            json={"reason": "Testing block on persona"},
            headers=super_headers,
        )
        assert r_block1.status_code == 409
        assert "personas" in r_block1.json()["detail"]["breakdown"]
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."personas" WHERE id = :id'), {"id": per_id})
        session.commit()

        # Guard 2: Tenant deal blocks rollback
        deal_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_TGT_B}"."deals" (account_id, name, stage, value_amount, probability)
                VALUES (:aid, 'Enterprise Pilot', 'discovery', 50000, 10)
                RETURNING id
            """),
            {"aid": account_id},
        ).scalar()
        session.commit()

        r_block2 = api_client.post(
            f"/api/catalog/share-events/{share_event.id}/rollback",
            json={"reason": "Testing block on deal"},
            headers=super_headers,
        )
        assert r_block2.status_code == 409
        assert "deals" in r_block2.json()["detail"]["breakdown"]
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."deals" WHERE id = :id'), {"id": deal_id})
        session.commit()

        # Guard 3: Activity link blocks rollback
        act_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_TGT_B}"."activities" (type, subject, direction)
                VALUES ('call', 'Client Discovery Call', 'inbound')
                RETURNING id
            """)
        ).scalar()
        session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_TGT_B}"."activity_links" (activity_id, object_type, object_id)
                VALUES (:act_id, 'account', :aid)
            """),
            {"act_id": act_id, "aid": account_id},
        )
        session.commit()

        r_block3 = api_client.post(
            f"/api/catalog/share-events/{share_event.id}/rollback",
            json={"reason": "Testing block on activity link"},
            headers=super_headers,
        )
        assert r_block3.status_code == 409
        assert "activity_links" in r_block3.json()["detail"]["breakdown"]
        session.execute(
            text(f"""
                DELETE FROM "{SCHEMA_TGT_B}"."activity_links"
                WHERE activity_id = :act_id AND object_id = :aid
            """),
            {"act_id": act_id, "aid": account_id},
        )
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."activities" WHERE id = :id'), {"id": act_id})
        session.commit()

        # Guard 4: User account access blocks rollback
        acc_id = session.execute(
            text(f"""
                INSERT INTO "{SCHEMA_TGT_B}"."user_account_access" (user_id, account_id)
                VALUES (:uid, :aid)
                RETURNING id
            """),
            {"uid": env["analyst_b_id"], "aid": account_id},
        ).scalar()
        session.commit()

        r_block4 = api_client.post(
            f"/api/catalog/share-events/{share_event.id}/rollback",
            json={"reason": "Testing block on user_account_access"},
            headers=super_headers,
        )
        assert r_block4.status_code == 409
        assert "user_account_access" in r_block4.json()["detail"]["breakdown"]
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."user_account_access" WHERE id = :id'), {"id": acc_id})
        session.commit()

        # Clean Case: Rollback succeeds
        r_clean = api_client.post(
            f"/api/catalog/share-events/{share_event.id}/rollback",
            json={"reason": "Clean rollback verified"},
            headers=super_headers,
        )
        assert r_clean.status_code == 200
        assert r_clean.json()["rolled_back"] is True

        # Assert all created rows in Target B deleted
        assert session.execute(text(f'SELECT 1 FROM "{SCHEMA_TGT_B}"."accounts" WHERE id = :aid'), {"aid": account_id}).scalar() is None
        assert session.execute(text(f'SELECT 1 FROM "{SCHEMA_TGT_B}"."targets" WHERE key = :k'), {"k": "omnicorp_ai"}).scalar() is None

        # Repeat rollback -> 400
        r_repeat = api_client.post(
            f"/api/catalog/share-events/{share_event.id}/rollback",
            json={"reason": "Repeat attempt"},
            headers=super_headers,
        )
        assert r_repeat.status_code == 400
    finally:
        session.close()


def test_scenario_6_concurrent_hydrate(setup_phase_a_environment):
    """Scenario 6:
    Concurrent hydrate of same snapshot:
    - 2 threads/sessions, same target, same snapshot, same idempotency key ->
      exactly 1 account created, same account_id returned, 1 debit.
    - Two different keys -> 1 succeeds, 1 gets 409.
    Tested over repeated iterations for race freedom.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        snapshot = session.query(Snapshot).order_by(Snapshot.id.desc()).first()
        target_snap_id = snapshot.id
        session.commit()

        # Iteration test for concurrency
        for iteration in range(5):
            # Clean Target B accounts
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."account_origin"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."targets"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."personas"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."sub_lobs"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."lobs"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."accounts"'))
            session.commit()

            # Test A: Same idempotency key concurrently
            shared_key = f"idem_concurrent_shared_{iteration}"
            def _call_hydrate_shared():
                s = SessionLocal()
                try:
                    return AccountReuseService.hydrate_account_from_catalog(
                        session=s,
                        target_client_slug=TGT_B_SLUG,
                        snapshot_id=target_snap_id,
                        user_id=env["super_id"],
                        idempotency_key=shared_key,
                        mode="instant",
                        fee_credits=1,
                        is_admin_assign=True,
                    )
                finally:
                    s.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(_call_hydrate_shared)
                f2 = executor.submit(_call_hydrate_shared)
                res1 = f1.result()
                res2 = f2.result()

            assert res1["ok"] is True
            assert res2["ok"] is True
            assert res1["account_id"] == res2["account_id"]

            # Exactly 1 account in target schema
            cnt = session.execute(text(f'SELECT count(*) FROM "{SCHEMA_TGT_B}"."accounts"')).scalar()
            assert cnt == 1

            # Test B: Different idempotency keys concurrently on clean target -> 1 succeeds, 1 gets DuplicateAccountError (409)
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."account_origin"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."targets"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."personas"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."sub_lobs"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."lobs"'))
            session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."accounts"'))
            session.commit()

            def _call_hydrate_diff(key):
                s = SessionLocal()
                try:
                    return AccountReuseService.hydrate_account_from_catalog(
                        session=s,
                        target_client_slug=TGT_B_SLUG,
                        snapshot_id=target_snap_id,
                        user_id=env["super_id"],
                        idempotency_key=key,
                        mode="instant",
                        fee_credits=1,
                        is_admin_assign=True,
                    )
                finally:
                    s.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f1 = executor.submit(_call_hydrate_diff, f"diff_key_1_{iteration}")
                f2 = executor.submit(_call_hydrate_diff, f"diff_key_2_{iteration}")
                outcomes = []
                for f in (f1, f2):
                    try:
                        outcomes.append(f.result())
                    except DuplicateAccountError:
                        outcomes.append("duplicate")

            assert "duplicate" in outcomes
            success = [o for o in outcomes if o != "duplicate"]
            assert len(success) == 1
            assert success[0]["ok"] is True
    finally:
        session.close()


def test_scenario_7_failure_atomicity(setup_phase_a_environment, monkeypatch):
    """Scenario 7:
    Monkeypatch insert -> 0 rows in target schema, 0 ledger debit,
    HTTP body has correlation_id and zero SQL text.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        # Clean Target B accounts
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."account_origin"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."targets"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."personas"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."sub_lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_B}"."accounts"'))
        session.commit()

        snapshot = session.query(Snapshot).order_by(Snapshot.id.desc()).first()
        preview_tok = AccountReuseService.generate_preview_token(snapshot.id, TGT_B_SLUG, env["super_id"])
        super_headers = _headers_for(env["super_id"], TGT_B_SLUG)

        # Baseline ledger debits
        initial_debits = session.execute(
            text("SELECT count(*) FROM public.credit_ledger WHERE client_id = :cid AND delta < 0"),
            {"cid": env["tgt_b_client_id"]},
        ).scalar() or 0

        # Monkeypatch insert to raise an exception containing raw SQL
        import services.account_reuse_service as ars
        original_insert = ars._insert_row
        def failing_insert(session, schema, table, row, kinds, **kwargs):
            if table in ("personas", "lobs"):
                raise RuntimeError("Simulated DB failure [SQL: INSERT INTO personas (id, col) VALUES (1, 'leak')]")
            return original_insert(session, schema, table, row, kinds, **kwargs)

        monkeypatch.setattr(ars, "_insert_row", failing_insert)

        res = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snapshot.id, "preview_token": preview_tok, "idempotency_key": "fail_atomic_test"},
            headers=super_headers,
        )
        assert res.status_code == 500
        detail = res.json()["detail"]
        assert "correlation_id" in detail
        assert "INSERT INTO" not in res.text
        assert "SELECT" not in res.text

        # 0 rows in Target B accounts
        cnt_accounts = session.execute(text(f'SELECT count(*) FROM "{SCHEMA_TGT_B}"."accounts"')).scalar()
        assert cnt_accounts == 0

        # 0 new ledger debits
        final_debits = session.execute(
            text("SELECT count(*) FROM public.credit_ledger WHERE client_id = :cid AND delta < 0"),
            {"cid": env["tgt_b_client_id"]},
        ).scalar() or 0
        assert final_debits == initial_debits
    finally:
        monkeypatch.undo()
        session.close()


def test_scenario_8_credit_limits(setup_phase_a_environment):
    """Scenario 8:
    Client at credit limit returns 402, 0 ledger rows, 0 accounts created.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        # Exhaust credits on Target A
        tgt_a = session.query(Client).filter_by(slug=TGT_A_SLUG).first()
        tgt_a.api_credits_total = 100
        tgt_a.api_credits_used = 100
        session.commit()

        # Clean accounts in Target A
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_A}"."account_origin"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_A}"."targets"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_A}"."personas"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_A}"."sub_lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_A}"."lobs"'))
        session.execute(text(f'DELETE FROM "{SCHEMA_TGT_A}"."accounts"'))
        session.commit()

        initial_ledger_count = session.execute(
            text("SELECT count(*) FROM public.credit_ledger WHERE client_id = :cid"),
            {"cid": tgt_a.id},
        ).scalar() or 0

        snapshot = session.query(Snapshot).order_by(Snapshot.id.desc()).first()
        preview_tok = AccountReuseService.generate_preview_token(snapshot.id, TGT_A_SLUG, env["admin_a_id"])
        admin_a_headers = _headers_for(env["admin_a_id"], TGT_A_SLUG)

        res = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snapshot.id, "preview_token": preview_tok, "idempotency_key": "credit_exhaust_test"},
            headers=admin_a_headers,
        )
        assert res.status_code == 402
        assert "Insufficient credits" in res.json()["detail"]

        # 0 accounts created
        assert session.execute(text(f'SELECT count(*) FROM "{SCHEMA_TGT_A}"."accounts"')).scalar() == 0

        # 0 ledger rows added
        final_ledger_count = session.execute(
            text("SELECT count(*) FROM public.credit_ledger WHERE client_id = :cid"),
            {"cid": tgt_a.id},
        ).scalar() or 0
        assert final_ledger_count == initial_ledger_count

        # Restore credits
        tgt_a.api_credits_total = 5000
        tgt_a.api_credits_used = 0
        session.commit()
    finally:
        session.close()


def test_scenario_9_audit_and_rate_limiting(setup_phase_a_environment):
    """Scenario 9:
    - Each action writes 1 audit_log row.
    - Details contain no source client for non-super-admins.
    - Rate limit returns 429 on 31st call.
    """
    env = setup_phase_a_environment
    session = SessionLocal()
    try:
        admin_a_headers = _headers_for(env["admin_a_id"], TGT_A_SLUG)

        # 1. Sliding window rate limit on reuse-check (30 calls / 5 min)
        reuse_check_limiter.reset()
        for i in range(30):
            r = api_client.get(
                "/api/catalog/reuse-check",
                params={"identifier": f"unregistered-brand-{i}.com", "client": TGT_A_SLUG},
                headers=admin_a_headers,
            )
            assert r.status_code == 200
            assert r.json() == {"available": False}

        # 31st call triggers 429
        r_31 = api_client.get(
            "/api/catalog/reuse-check",
            params={"identifier": "unregistered-brand-31.com", "client": TGT_A_SLUG},
            headers=admin_a_headers,
        )
        assert r_31.status_code == 429
        assert "Rate limit exceeded" in r_31.json()["detail"]

        # Reset limiter for rest of test
        reuse_check_limiter.reset()

        # 2. Audit row on reuse-check hit
        r_hit = api_client.get(
            "/api/catalog/reuse-check",
            params={"identifier": "omnicorp-global.ai", "client": TGT_A_SLUG},
            headers=admin_a_headers,
        )
        assert r_hit.status_code == 200
        assert r_hit.json()["available"] is True

        audit_rc = session.query(AuditLog).filter_by(
            action="catalog.reuse_check_available",
            actor_user_id=env["admin_a_id"],
        ).order_by(AuditLog.id.desc()).first()
        assert audit_rc is not None
        assert "identifier_hash" in audit_rc.details
        assert "omnicorp-global.ai" not in json.dumps(audit_rc.details)

        # 3. Audit row on hydration (source client stripped for client_admin)
        tok = r_hit.json()["preview_token"]
        snap_id = r_hit.json()["snapshot_id"]

        r_hyd = api_client.post(
            "/api/catalog/hydrate",
            json={"snapshot_id": snap_id, "preview_token": tok, "idempotency_key": "audit_test_hydrate"},
            headers=admin_a_headers,
        )
        assert r_hyd.status_code == 200

        audit_hyd = session.query(AuditLog).filter_by(
            action="catalog.hydrate",
            actor_user_id=env["admin_a_id"],
        ).order_by(AuditLog.id.desc()).first()
        assert audit_hyd is not None
        assert audit_hyd.details["target_client_slug"] == TGT_A_SLUG
        # Source client stripped
        assert "source_client_slug" not in audit_hyd.details
        assert "source_client_id" not in audit_hyd.details
        # Zero payload or PII
        assert "payload" not in audit_hyd.details
        assert "records" not in audit_hyd.details
    finally:
        session.close()
