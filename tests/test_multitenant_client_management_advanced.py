"""Test Suite for Advanced Multi-Tenant Client Management:
KPIs, Schema Health Verification, Self-Healing Repair, Credit Adjustments,
User Role Updates, and Per-Account Metrics.
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

import pytest
from fastapi.testclient import TestClient

from api import app
from db.connection import SessionLocal
from db.models import User, Client, UserClientAccess
import auth

client = TestClient(app)


@pytest.fixture(scope="module")
def admin_headers():
    session = SessionLocal()
    try:
        user = session.query(User).filter_by(role="super_admin").first()
        if not user:
            user = User(
                email="superadmin_test_mgr@internal.doshin",
                full_name="Super Admin Manager Test",
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


def test_overview_kpis_endpoint(admin_headers):
    res = client.get("/api/clients/overview-kpis", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert "total_clients" in data
    assert "active_clients" in data
    assert "total_accounts" in data
    assert "total_credits_allocated" in data
    assert data["total_clients"] >= 1
    assert data["total_accounts"] >= 5


def test_platform_users_endpoint(admin_headers):
    res = client.get("/api/clients/platform-users", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert "users" in data
    assert len(data["users"]) >= 1
    assert any(u["role"] == "super_admin" for u in data["users"])


def test_schema_health_and_repair_workflow(admin_headers):
    # 1. Check health of stradit_india
    res = client.get("/api/clients/stradit_india/health", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["slug"] == "stradit_india"
    assert data["exists"] is True
    assert data["is_healthy"] is True
    from db.provisioning import CLIENT_TABLES_ORDERED
    assert data["tables_found"] == len(CLIENT_TABLES_ORDERED)
    assert data["tables_total"] == len(CLIENT_TABLES_ORDERED)
    assert data["total_rows"] > 0

    # 2. Trigger self-healing repair (idempotent, does not drop tables)
    repair_res = client.post("/api/clients/stradit_india/repair", headers=admin_headers)
    assert repair_res.status_code == 200, repair_res.text
    rdata = repair_res.json()
    assert rdata["ok"] is True
    assert rdata["repair"]["is_healthy"] is True


def test_atomic_credit_adjustment(admin_headers):
    # Get current client credits
    c_res = client.get("/api/clients/stradit_india", headers=admin_headers)
    assert c_res.status_code == 200
    current_credits = c_res.json()["api_credits_total"]

    # Add 25,000 credits
    adjust_res = client.post(
        "/api/clients/stradit_india/credits/adjust",
        headers=admin_headers,
        json={"amount": 25000, "action": "add", "reason": "Automated pipeline top-up test"}
    )
    assert adjust_res.status_code == 200, adjust_res.text
    assert adjust_res.json()["api_credits_total"] == current_credits + 25000

    # Reset back to original
    reset_res = client.post(
        "/api/clients/stradit_india/credits/adjust",
        headers=admin_headers,
        json={"amount": current_credits, "action": "set", "reason": "Reverting test top-up"}
    )
    assert reset_res.status_code == 200
    assert reset_res.json()["api_credits_total"] == current_credits


def test_client_user_role_update_workflow(admin_headers):
    session = SessionLocal()
    try:
        # Create a test analyst user
        test_user = session.query(User).filter_by(email="analyst_test_user@internal.doshin").first()
        if not test_user:
            test_user = User(
                email="analyst_test_user@internal.doshin",
                full_name="Analyst Tester",
                hashed_password="mock_password_hash_12345",
                role="user",
                is_active=True,
            )
            session.add(test_user)
            session.commit()
            session.refresh(test_user)
        test_user_id = test_user.id
    finally:
        session.close()

    # 1. Assign user as viewer
    add_res = client.post(
        "/api/clients/stradit_india/users",
        headers=admin_headers,
        json={"user_id": test_user_id, "role": "viewer"}
    )
    assert add_res.status_code == 200

    # 2. Update role to client_admin
    patch_res = client.patch(
        f"/api/clients/stradit_india/users/{test_user_id}",
        headers=admin_headers,
        json={"role": "client_admin"}
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["role"] == "client_admin"

    # 3. Clean up by revoking
    del_res = client.delete(f"/api/clients/stradit_india/users/{test_user_id}", headers=admin_headers)
    assert del_res.status_code == 200


def test_client_accounts_drilldown_metrics(admin_headers):
    res = client.get("/api/clients/stradit_india/accounts", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert "accounts" in data
    assert data["total"] >= 5
    for a in data["accounts"]:
        assert "display_name" in a
        assert "primary_domain" in a
        assert "lobs_count" in a
        assert "personas_count" in a
        assert isinstance(a["lobs_count"], int)
        assert isinstance(a["personas_count"], int)


def test_client_serialization_lifecycle_and_skeleton_fields(admin_headers):
    res = client.get("/api/clients/stradit_india", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert "status" in data
    assert data["status"] in ("active", "suspended", "deleted")
    assert "skeleton_version" in data
    assert "skeleton_head" in data
    assert "is_behind" in data
    assert "refresh_cron" in data
    assert "max_concurrent_jobs" in data
    assert data["skeleton_version"] == "T0001"
    assert data["skeleton_head"] == "T0001"
    assert data["is_behind"] is False


def test_suspend_and_activate_lifecycle(admin_headers):
    # 1. Primary platform client cannot be suspended
    res_primary = client.post("/api/clients/stradit_india/suspend", headers=admin_headers)
    assert res_primary.status_code == 400
    assert "Cannot suspend the primary platform client" in res_primary.json()["detail"]

    # 2. Suspend a non-primary client (e.g. acme_corp)
    res = client.post("/api/clients/acme_corp/suspend", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["ok"] is True
    assert data["status"] == "suspended"

    # Verify GET returns suspended
    c_res = client.get("/api/clients/acme_corp", headers=admin_headers)
    assert c_res.json()["status"] == "suspended"
    assert c_res.json()["is_active"] is False

    # 3. Reactivate acme_corp
    act_res = client.post("/api/clients/acme_corp/activate", headers=admin_headers)
    assert act_res.status_code == 200, act_res.text
    act_data = act_res.json()
    assert act_data["ok"] is True
    assert act_data["status"] == "active"

    # Verify GET returns active
    c2_res = client.get("/api/clients/acme_corp", headers=admin_headers)
    assert c2_res.json()["status"] == "active"
    assert c2_res.json()["is_active"] is True


def test_credit_ledger_endpoint(admin_headers):
    res = client.get("/api/clients/stradit_india/credits/ledger", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["client_slug"] == "stradit_india"
    assert "ledger" in data
    assert "total" in data
    assert isinstance(data["ledger"], list)


def test_activity_log_endpoint(admin_headers):
    res = client.get("/api/clients/stradit_india/activity", headers=admin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["client_slug"] == "stradit_india"
    assert "events" in data
    assert "total" in data
    assert isinstance(data["events"], list)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__]))

