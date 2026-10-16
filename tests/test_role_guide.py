"""The Admin page's roles guide must match the permission rules it describes (pure — no database)."""
import auth
from apps.sales_crm import permissions
from apps.sales_crm.role_guide import ACCESS_LABEL, CAPABILITIES, ROLE_ORDER, ROLES, guide

ROWS = {row["key"]: row for g in CAPABILITIES for row in g["items"]}


def test_every_role_described_once_with_real_labels():
    assert ROLE_ORDER == list(auth.ROLES)
    assert [r["key"] for r in ROLES] == ROLE_ORDER
    labels = {r["key"]: r["label"] for r in guide(auth.ROLE_LABELS)["roles"]}
    assert labels == auth.ROLE_LABELS


def test_every_capability_covers_every_role_with_a_known_value():
    for row in ROWS.values():
        assert set(row["access"]) == set(ROLE_ORDER), row["key"]
        assert set(row["access"].values()) <= set(ACCESS_LABEL), row["key"]
        assert set(row.get("notes", {})) <= set(ROLE_ORDER), row["key"]


def test_read_only_roles_never_shown_with_write_access():
    write_rows = ["manage_accounts", "pipelines", "edit_records", "delete_deals", "edit_activities",
                  "link_partner", "targets", "fx", "manage_users", "settings"]
    for role in auth.READ_ONLY_ROLES:
        for key in write_rows:
            assert ROWS[key]["access"][role] == "no", (role, key)
        # ...and the guard really blocks their writes
        assert permissions.blocked_reason(role, "POST", "/api/deals") is not None
        assert permissions.blocked_reason(role, "PATCH", "/api/crm/introductions/1") is not None


def test_partner_is_portal_only():
    for key, row in ROWS.items():
        assert row["access"]["partner"] == ("own" if key == "partner_portal" else "no"), key
    assert permissions.blocked_reason("partner", "GET", "/api/deals") is not None
    assert permissions.blocked_reason("partner", "GET", "/api/partner/introductions") is None


def test_viewer_can_use_copilot_but_not_edit():
    assert ROWS["copilot"]["access"]["viewer"] == "yes"
    assert permissions.blocked_reason("viewer", "POST", "/api/copilot/chat") is None


def test_internal_roles_are_not_blocked_by_the_guard():
    for role in ("super_admin", "sales_manager", "user"):
        assert permissions.blocked_reason(role, "POST", "/api/deals") is None
        assert ROWS["edit_records"]["access"][role] != "no"


def test_admin_only_rows():
    for key in ("manage_accounts", "pipelines", "link_partner", "fx", "manage_users", "settings"):
        assert [r for r, v in ROWS[key]["access"].items() if v != "no"] == ["super_admin"], key
