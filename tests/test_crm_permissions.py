"""Role-guard decisions (apps/sales_crm/permissions.py) — pure logic, no database."""

import pytest

pytest.importorskip("fastapi")


@pytest.fixture(scope="module")
def blocked():
    try:
        from apps.sales_crm.permissions import blocked_reason
    except RuntimeError as e:          # auth.py refuses to import without JWT_SECRET_KEY
        pytest.skip(str(e))
    return blocked_reason


@pytest.mark.parametrize("role", ["super_admin", "sales_manager", "user"])
@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_internal_roles_are_never_blocked_by_the_guard(blocked, role, method):
    assert blocked(role, method, "/api/deals") is None
    assert blocked(role, method, "/api/accounts/11") is None


def test_viewer_reads_but_cannot_write(blocked):
    assert blocked("viewer", "GET", "/api/deals") is None
    assert blocked("viewer", "HEAD", "/api/deals") is None
    for m in ("POST", "PATCH", "PUT", "DELETE"):
        assert blocked("viewer", m, "/api/deals") == "Your role is read-only."


def test_viewer_keeps_personal_workspace_and_auth(blocked):
    assert blocked("viewer", "POST", "/api/copilot/chat/stream") is None
    assert blocked("viewer", "POST", "/api/auth/logout") is None


def test_partner_only_reaches_partner_and_auth_apis(blocked):
    assert blocked("partner", "GET", "/api/partner/introductions") is None
    assert blocked("partner", "POST", "/api/auth/refresh") is None
    for path in ("/api/deals", "/api/accounts", "/api/copilot/chat", "/api/crm/meta", "/api/personas/1"):
        assert blocked("partner", "GET", path) is not None


def test_pages_and_static_files_are_not_guarded(blocked):
    assert blocked("partner", "GET", "/deals") is None
    assert blocked("viewer", "GET", "/css/deals.css") is None


def test_unknown_or_missing_role_passes_to_route_auth(blocked):
    # No role (inactive/unknown user) → the route's own get_current_user decides (401).
    assert blocked(None, "POST", "/api/deals") is None


def test_viewer_may_trigger_page_view_syncs_only(blocked):
    assert blocked("viewer", "POST", "/api/accounts/11/opportunities/sync") is None
    assert blocked("viewer", "POST", "/api/accounts/11/weekly-updates/sync") is None
    assert blocked("viewer", "POST", "/api/accounts/11/action-items") == "Your role is read-only."
    assert blocked("viewer", "POST", "/api/accounts/11/opportunities/sync/../../x") is not None
