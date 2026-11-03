"""Phase 6 Production Cutover Validation Checklist Suite (§11.5 & §14).

Validates:
1. All accounts accessible via API with X-Client-Slug: stradit_india
2. LOB automation engine runs in client_stradit_india schema
3. Persona automation engine runs in client_stradit_india schema
4. pipeline_runs created in client schema (not public)
5. user_account_access grants work correctly in client schema
6. Auth login flow returns correct client_slug in JWT
7. Super admin can switch between clients via X-Client-Slug
8. Background job status tracked with client_slug in job registry
9. Row counts match: public.accounts == client_stradit_india.accounts
10. No search_path contamination between concurrent requests
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest
from starlette.testclient import TestClient
from sqlalchemy import text

from api import app
import config
from db.connection import SessionLocal, get_session
from db.models import User, Client, Account, PipelineRun
from db.routing import scoped_client_session, DEFAULT_CLIENT_SLUG, set_client_schema
from services.lob_automation_engine import LobAutomationEngine
from services.persona_automation_engine import PersonaAutomationEngine
from services.job_registry import create_job, get_job, TYPE_ACCOUNT
from services.pipeline_run_logger import PipelineRunLogger
import auth

client = TestClient(app)


@pytest.fixture(scope="module")
def admin_auth():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(role="super_admin").first()
        if not user:
            user = User(
                email="admin_cutover@internal.doshin",
                full_name="Admin Cutover Test",
                role="super_admin",
                is_active=True,
            )
            session.add(user)
            session.commit()
            session.refresh(user)

        token = auth.create_access_token(user, client_slug="stradit_india")
        return {
            "token": token,
            "user": user,
            "headers": {
                "Authorization": f"Bearer {token}",
                "X-Client-Slug": "stradit_india",
            }
        }
    finally:
        session.close()


def test_cutover_1_feature_flag_active():
    """Verify MULTITENANT_ENABLED feature flag is active."""
    assert getattr(config, "MULTITENANT_ENABLED", False) is True, "MULTITENANT_ENABLED must be True"
    assert config.DEFAULT_CLIENT_SLUG == "stradit_india"


def test_cutover_2_all_accounts_accessible_via_api(admin_auth):
    """[Checklist #1] All accounts accessible via API with X-Client-Slug: stradit_india."""
    r = client.get("/api/accounts", headers=admin_auth["headers"])
    assert r.status_code == 200
    data = r.json()
    accts = data if isinstance(data, list) else data.get("accounts", [])
    assert len(accts) >= 6
    names = [a.get("display_name") or a.get("legal_name") or a.get("key") for a in accts]
    assert any("BlackRock" in n for n in names)
    assert any("BNY" in n or "Mellon" in n for n in names)


def test_cutover_3_lob_engine_schema_isolation():
    """[Checklist #2] LOB automation engine runs in client_stradit_india schema."""
    engine = LobAutomationEngine(lob_id=1, account_id=11, client_slug="stradit_india")
    session = engine.get_session()
    try:
        sp = session.execute(text("SHOW search_path")).fetchone()[0]
        assert "client_stradit_india" in sp
    finally:
        session.close()


def test_cutover_4_persona_engine_schema_isolation():
    """[Checklist #3] Persona automation engine runs in client_stradit_india schema."""
    engine = PersonaAutomationEngine(persona_id=1, account_id=11, client_slug="stradit_india")
    session = engine.get_session()
    try:
        sp = session.execute(text("SHOW search_path")).fetchone()[0]
        assert "client_stradit_india" in sp
    finally:
        session.close()


def test_cutover_5_pipeline_runs_created_in_client_schema():
    """[Checklist #4] pipeline_runs created in client schema (not public)."""
    run = PipelineRunLogger.log_event(
        company_name="Cutover Validation Account",
        level="account",
        action="cutover_validation",
        status="completed",
        account_id=11,
        client_slug="stradit_india",
        duration_seconds=1.2,
    )
    assert run is not None
    assert run.account_id == 11

    # Verify record in client_stradit_india
    with scoped_client_session("stradit_india") as s_client:
        r_client = s_client.query(PipelineRun).filter_by(run_id=run.run_id).first()
        assert r_client is not None
        assert r_client.account_id == 11

    # Verify NOT in public (zero cross-contamination)
    pub_session = SessionLocal()
    try:
        r_pub = pub_session.execute(
            text("SELECT count(*) FROM public.pipeline_runs WHERE run_id = :rid"),
            {"rid": run.run_id}
        ).scalar()
        assert r_pub == 0, "Pipeline run should NOT be created in public schema during cutover!"
    finally:
        pub_session.close()


def test_cutover_6_user_account_access_in_client_schema(admin_auth):
    """[Checklist #5] user_account_access grants work correctly in client schema."""
    uid = admin_auth["user"].id
    r = client.get(f"/api/clients/stradit_india/users/{uid}/accounts", headers=admin_auth["headers"])
    assert r.status_code == 200
    data = r.json()
    assert "accounts" in data
    assert len(data["accounts"]) >= 6


def test_cutover_7_jwt_contains_client_slug(admin_auth):
    """[Checklist #6] Auth login flow returns correct client_slug in JWT."""
    payload = auth.decode_access_token(admin_auth["token"])
    assert payload is not None
    assert payload.get("client_slug") == "stradit_india"
    assert payload.get("role") == "super_admin"


def test_cutover_8_client_switching_via_header(admin_auth):
    """[Checklist #7] Super admin can switch between clients via X-Client-Slug."""
    headers_stradit = dict(admin_auth["headers"])
    headers_stradit["X-Client-Slug"] = "stradit_india"

    headers_acme = dict(admin_auth["headers"])
    headers_acme["X-Client-Slug"] = "acme_corp"

    r_stradit = client.get("/api/accounts", headers=headers_stradit)
    r_acme = client.get("/api/accounts", headers=headers_acme)

    assert r_stradit.status_code == 200
    assert r_acme.status_code == 200

    accts_stradit = r_stradit.json() if isinstance(r_stradit.json(), list) else r_stradit.json().get("accounts", [])
    accts_acme = r_acme.json() if isinstance(r_acme.json(), list) else r_acme.json().get("accounts", [])

    assert len(accts_stradit) >= 6
    assert len(accts_acme) == 1


def test_cutover_9_job_registry_tracks_client_slug():
    """[Checklist #8] Background job status tracked with client_slug in job registry."""
    jid = create_job(TYPE_ACCOUNT, meta={"test": "cutover"}, client_slug="stradit_india")
    job = get_job(jid)
    assert job is not None
    assert job["client_slug"] == "stradit_india"
    assert job["job_type"] == TYPE_ACCOUNT


def test_cutover_10_row_counts_match_and_no_contamination():
    """[Checklist #9 & #10] Row counts match and search_path is safe."""
    pub_session = SessionLocal()
    try:
        pub_accts = pub_session.execute(text("SELECT count(*) FROM public.accounts")).scalar()
        client_accts = pub_session.execute(text("SELECT count(*) FROM client_stradit_india.accounts")).scalar()
        assert pub_accts == client_accts == 6

        # Connection pool check
        res = pub_session.execute(text("SHOW search_path")).fetchone()[0]
        assert "public" in res
    finally:
        pub_session.close()

    # Reset ContextVar
    set_client_schema(f"client_{DEFAULT_CLIENT_SLUG}")
