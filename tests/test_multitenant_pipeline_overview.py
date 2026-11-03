"""Integration and Regression Test Suite for Multi-Tenant Pipeline Overview,
Sidebar Scoping, Client Accounts, and User Access Management.
"""

import sys
import os
from pathlib import Path

# Add project root to path
PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from api import app
from db.connection import engine, SessionLocal
from db.models import User, Client, UserClientAccess, Account, PipelineRun
from db.provisioning import SchemaProvisioningService
import auth

client = TestClient(app)


@pytest.fixture(scope="module")
def admin_headers():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(role="super_admin").first()
        if not user:
            user = User(
                email="admin_overview_test@doshin.internal",
                full_name="Super Admin Overview Tester",
                role="super_admin",
                is_active=True,
            )
            session.add(user)
            session.commit()
            session.refresh(user)

        token = auth.create_access_token(user)
        return {
            "Authorization": f"Bearer {token}",
            "X-Client-Slug": "stradit_india",
        }
    finally:
        session.close()


def test_pipeline_overview_stradit_india(admin_headers):
    """Verifies /api/system/pipeline-overview returns live dynamic client data for Stradit India."""
    res = client.get("/api/system/pipeline-overview", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "success"

    # Client workspace verification
    cw = data["client_workspace"]
    assert cw["client_slug"] == "stradit_india"
    assert "Stradit" in cw["client_name"]
    assert cw["schema_name"] == "client_stradit_india"
    assert cw["accounts_count"] == 6
    assert cw["lobs_count"] >= 390
    assert cw["personas_count"] >= 2240

    # Accounts summary payload verification
    assert "accounts" in cw
    assert len(cw["accounts"]) == 6
    first_acct = cw["accounts"][0]
    assert "id" in first_acct
    assert "display_name" in first_acct
    assert "lobs_count" in first_acct
    assert "personas_count" in first_acct
    assert "completeness_score" in first_acct

    # Available clients contains stradit_india
    avail_slugs = [c["client_slug"] for c in cw["available_clients"]]
    assert "stradit_india" in avail_slugs

    # Dynamic Funnel metrics
    funnel = data["funnel"]
    assert funnel["crm_backlog"] == 6
    assert funnel["tracked_companies"] == 6
    assert funnel["tracked_lobs"] >= 390
    assert funnel["tracked_people"] >= 2240
    assert funnel["posts_stored"] >= 1000

    # 24h Health
    health = data["health_24h"]
    assert "success_rate" in health
    assert "total_runs_evaluated" in health

    # Recent runs & channels
    assert isinstance(data["recent_runs"], list)
    assert len(data["recent_runs"]) > 0
    assert isinstance(data["needs_attention"], list)


def test_pipeline_overview_isolation_on_new_tenant(admin_headers):
    """Verifies that an alternate client workspace returns completely isolated, empty statistics."""
    test_slug = "test_acme_overview"
    test_name = "Acme Test Overview Org"

    session = SessionLocal()
    try:
        existing = session.query(Client).filter_by(slug=test_slug).first()
        if existing:
            session.delete(existing)
            session.commit()
        SchemaProvisioningService.drop_client_schema(engine, test_slug)

        new_client = Client(
            name=test_name,
            slug=test_slug,
            schema_name=f"client_{test_slug}",
            plan_tier="standard",
            max_accounts=15,
            api_credits_total=50000,
            api_credits_used=0,
            is_active=True,
        )
        session.add(new_client)
        session.commit()
        SchemaProvisioningService.create_client_schema(engine, test_slug)

        headers = dict(admin_headers)
        headers["X-Client-Slug"] = test_slug
        res = client.get("/api/system/pipeline-overview", headers=headers)
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["status"] == "success"

        cw = data["client_workspace"]
        assert cw["client_slug"] == test_slug
        assert cw["client_name"] == test_name
        assert cw["schema_name"] == f"client_{test_slug}"
        assert cw["accounts_count"] == 0
        assert cw["lobs_count"] == 0
        assert cw["personas_count"] == 0

        funnel = data["funnel"]
        assert funnel["crm_backlog"] == 0
        assert funnel["tracked_companies"] == 0
        assert funnel["tracked_people"] == 0
        assert funnel["tracked_lobs"] == 0

        crm_res = client.get("/api/system/crm-backlog", headers=headers)
        assert crm_res.status_code == 200
        assert crm_res.json()["total"] == 0

        pd_res = client.get("/api/system/people-directory", headers=headers)
        assert pd_res.status_code == 200
        assert pd_res.json()["total"] == 0

    finally:
        SchemaProvisioningService.drop_client_schema(engine, test_slug)
        del_c = session.query(Client).filter_by(slug=test_slug).first()
        if del_c:
            session.delete(del_c)
            session.commit()
        session.close()


def test_client_accounts_endpoint(admin_headers):
    """Verifies GET /api/clients/{slug}/accounts returns accounts scoped to that schema."""
    res = client.get("/api/clients/stradit_india/accounts", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["client_slug"] == "stradit_india"
    assert data["total"] == 6
    assert len(data["accounts"]) == 6
    names = [a["display_name"] for a in data["accounts"]]
    assert any("BlackRock" in n for n in names)
    assert any("Vanguard" in n for n in names)


def test_client_users_management_workflow(admin_headers):
    """Verifies listing, adding, and revoking client users via /api/clients/{slug}/users."""
    session = SessionLocal()
    test_user = None
    try:
        test_user = User(
            email="temp_user_test@doshin.internal",
            hashed_password="$2b$12$dummyhashfordevtestingonly",
            full_name="Temp Test User",
            role="user",
            is_active=True,
        )
        session.add(test_user)
        session.commit()
        session.refresh(test_user)

        res = client.get("/api/clients/stradit_india/users", headers=admin_headers)
        assert res.status_code == 200
        initial_count = res.json()["total"]

        add_res = client.post(
            "/api/clients/stradit_india/users",
            json={"user_id": test_user.id, "role": "analyst"},
            headers=admin_headers,
        )
        assert add_res.status_code == 200, add_res.text
        assert add_res.json()["ok"] is True

        res2 = client.get("/api/clients/stradit_india/users", headers=admin_headers)
        assert res2.status_code == 200
        assert res2.json()["total"] == initial_count + 1
        member_ids = [m["user_id"] for m in res2.json()["members"]]
        assert test_user.id in member_ids

        del_res = client.delete(
            f"/api/clients/stradit_india/users/{test_user.id}",
            headers=admin_headers,
        )
        assert del_res.status_code == 200
        assert del_res.json()["ok"] is True

        res3 = client.get("/api/clients/stradit_india/users", headers=admin_headers)
        assert res3.json()["total"] == initial_count

    finally:
        if test_user:
            u = session.query(User).filter_by(id=test_user.id).first()
            if u:
                session.delete(u)
                session.commit()
        session.close()


def test_pipeline_run_logger_with_account_and_client_slug():
    """Verifies PipelineRunLogger scopes to client schema, stores account_id, and partitions disk logs."""
    from services.pipeline_run_logger import PipelineRunLogger
    from db.routing import scoped_client_session

    valid_acct_id = None
    with scoped_client_session("stradit_india") as session:
        first_acct = session.query(Account).first()
        assert first_acct is not None, "Expected at least one account in client_stradit_india"
        valid_acct_id = first_acct.id

    run = PipelineRunLogger.log_event(
        company_name="StradIT Automated Testing Corp",
        level="account",
        action="test_run",
        account_id=valid_acct_id,
        client_slug="stradit_india",
        total_credits_used=2,
    )

    assert run is not None
    assert run.company_name == "StradIT Automated Testing Corp"
    assert run.account_id == valid_acct_id

    out_dir = Path("output") / "stradit_india"
    assert out_dir.exists(), f"Expected directory {out_dir} to exist"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main(["-v", __file__]))

