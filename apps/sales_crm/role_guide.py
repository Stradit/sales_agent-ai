"""Roles & permissions guide shown on the Admin page (GET /api/crm/roles).

Plain-language description of what each role is for and what it can reach. It is
documentation, not enforcement: the rules live in auth.py (account scope),
apps/sales_crm/permissions.py (role guard, can()) and the routers. tests/test_role_guide.py
checks this table against those rules so the two can't drift apart silently.

Access values: "yes" · "no" · "scoped" (accounts granted to them) · "team" (their
accounts + their reports') · "own" (records they own / created) · "read" (view only).
"""

ROLE_ORDER = ["super_admin", "sales_manager", "user", "viewer", "partner"]

ROLES = [
    {
        "key": "super_admin", "icon": "fa-shield-halved",
        "summary": "Runs the platform. Sees every account and controls who can access what.",
        "data_scope": "All accounts, all users, all records.",
        "responsibilities": [
            "Create users, set their role and manager, and grant account access",
            "Configure API keys, AI models and system settings",
            "Run the account data pipeline and copilot pipeline",
            "Link partner logins to connectors and preview the partner portal",
            "Review the security and audit trail",
        ],
        "limits": ["Can't be assigned tasks — work is assigned to the sales team"],
    },
    {
        "key": "sales_manager", "icon": "fa-user-tie",
        "summary": "Leads a sales team. Works their own deals and oversees their reports'.",
        "data_scope": "Accounts granted to them plus every account granted to people who report to them.",
        "responsibilities": [
            "Review the team's pipeline, deal health and forecast",
            "Set sales targets for the people who report to them",
            "Triage partner introductions and assign owners",
            "Coach reps: keep stages, next steps and buying committees current",
        ],
        "limits": ["Can't manage users, API settings or data pipelines",
                   "Can't set business-line targets or exchange rates"],
    },
    {
        "key": "user", "icon": "fa-briefcase",
        "summary": "Sales rep. Owns deals and relationships on the accounts they're given.",
        "data_scope": "Only the accounts an admin has granted them.",
        "responsibilities": [
            "Run deals from Intro to Contract and keep the next step current",
            "Map the buying committee and log customer activity",
            "Work warm introductions from partners through to a meeting",
            "Complete the tasks assigned to them",
        ],
        "limits": ["Sees only the deals they own (pipeline, account pages, digest, insights)",
                   "Can delete only deals they own", "Can't set targets"],
    },
    {
        "key": "viewer", "icon": "fa-eye",
        "summary": "Read-only stakeholder — e.g. leadership or delivery — who needs visibility, not edits.",
        "data_scope": "Only the accounts an admin has granted them, read-only.",
        "responsibilities": [
            "Follow account intelligence, pipeline and introductions",
            "Use the Sales Copilot for research (their chats and notes are private to them)",
        ],
        "limits": ["Can't create, edit or delete any record", "Can't be relied on to complete tasks (read-only)"],
    },
    {
        "key": "partner", "icon": "fa-handshake",
        "summary": "External partner or advisor who introduces StradIT to their contacts.",
        "data_scope": "Only the introductions linked to their own connector, in the partner portal.",
        "responsibilities": [
            "Submit warm introductions with context",
            "Correct a submission while it is still Submitted or Requested",
            "Send updates and follow each introduction's progress",
        ],
        "limits": ["No access to accounts, deals, values, attribution or internal notes",
                   "Only the partner portal (/partner) — every other page and API is blocked"],
    },
]

# Each row: what it is, then access per role in ROLE_ORDER, plus an optional note per role.
CAPABILITIES = [
    {"group": "Accounts & data", "items": [
        {"key": "open_accounts", "label": "Open accounts and account intelligence",
         "access": {"super_admin": "yes", "sales_manager": "team", "user": "scoped", "viewer": "read", "partner": "no"}},
        {"key": "manage_accounts", "label": "Create, fetch, edit or delete accounts",
         "access": {"super_admin": "yes", "sales_manager": "no", "user": "no", "viewer": "no", "partner": "no"}},
        {"key": "pipelines", "label": "Run data and copilot pipelines, download the database",
         "access": {"super_admin": "yes", "sales_manager": "no", "user": "no", "viewer": "no", "partner": "no"}},
    ]},
    {"group": "Sales work", "items": [
        {"key": "see_deals", "label": "See deals in the pipeline, account pages, digest and insights",
         "access": {"super_admin": "yes", "sales_manager": "team", "user": "own", "viewer": "read", "partner": "no"},
         "notes": {"sales_manager": "Every deal on their team's accounts", "user": "Only deals they own",
                   "viewer": "Every deal on the accounts they can see, read-only"}},
        {"key": "edit_records", "label": "Create and edit deals, contacts, activities and tasks",
         "access": {"super_admin": "yes", "sales_manager": "team", "user": "scoped", "viewer": "no", "partner": "no"}},
        {"key": "delete_deals", "label": "Delete deals",
         "access": {"super_admin": "yes", "sales_manager": "team", "user": "own", "viewer": "no", "partner": "no"},
         "notes": {"sales_manager": "Their own and their reports' deals", "user": "Deals they own"}},
        {"key": "edit_activities", "label": "Change or delete a logged activity",
         "access": {"super_admin": "yes", "sales_manager": "own", "user": "own", "viewer": "no", "partner": "no"},
         "notes": {"sales_manager": "Activities they logged", "user": "Activities they logged"}},
        {"key": "assigned_tasks", "label": "Be assigned tasks",
         "access": {"super_admin": "no", "sales_manager": "yes", "user": "yes", "viewer": "no", "partner": "no"}},
        {"key": "copilot", "label": "Use the Sales Copilot",
         "access": {"super_admin": "yes", "sales_manager": "yes", "user": "yes", "viewer": "yes", "partner": "no"}},
    ]},
    {"group": "Introductions", "items": [
        {"key": "intro_board", "label": "Work the introductions board",
         "access": {"super_admin": "yes", "sales_manager": "team", "user": "scoped", "viewer": "read", "partner": "no"}},
        {"key": "link_partner", "label": "Link partner logins to connectors",
         "access": {"super_admin": "yes", "sales_manager": "no", "user": "no", "viewer": "no", "partner": "no"}},
        {"key": "partner_portal", "label": "Submit and follow introductions in the partner portal",
         "access": {"super_admin": "read", "sales_manager": "no", "user": "no", "viewer": "no", "partner": "own"},
         "notes": {"super_admin": "Preview only", "partner": "Their own introductions"}},
    ]},
    {"group": "Forecast", "items": [
        {"key": "forecast_view", "label": "View the forecast",
         "access": {"super_admin": "yes", "sales_manager": "team", "user": "own", "viewer": "own", "partner": "no"},
         "notes": {"viewer": "Opens, but shows only deals they own — viewers own none"}},
        {"key": "targets", "label": "Set sales targets",
         "access": {"super_admin": "yes", "sales_manager": "team", "user": "no", "viewer": "no", "partner": "no"},
         "notes": {"sales_manager": "For their reports; not business-line targets"}},
        {"key": "fx", "label": "Edit exchange rates",
         "access": {"super_admin": "yes", "sales_manager": "no", "user": "no", "viewer": "no", "partner": "no"}},
    ]},
    {"group": "Administration", "items": [
        {"key": "manage_users", "label": "Manage users, roles and account access",
         "access": {"super_admin": "yes", "sales_manager": "no", "user": "no", "viewer": "no", "partner": "no"}},
        {"key": "see_team", "label": "See the team list",
         "access": {"super_admin": "yes", "sales_manager": "team", "user": "no", "viewer": "no", "partner": "no"},
         "notes": {"sales_manager": "Themself and their reports"}},
        {"key": "settings", "label": "API, model and system settings; audit trail",
         "access": {"super_admin": "yes", "sales_manager": "no", "user": "no", "viewer": "no", "partner": "no"}},
    ]},
]

ACCESS_LABEL = {"yes": "Full", "no": "No access", "scoped": "Granted accounts", "team": "Own + team",
                "own": "Own only", "read": "View only"}


def guide(labels: dict) -> dict:
    return {"roles": [{**r, "label": labels.get(r["key"], r["key"])} for r in ROLES],
            "order": ROLE_ORDER, "capabilities": CAPABILITIES, "access_labels": ACCESS_LABEL}
