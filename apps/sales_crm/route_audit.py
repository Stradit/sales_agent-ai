"""Auth hardening gate (apps/sales_crm/README.md §5.2, M5). Needs the DB (the app builds on import).

    python -m apps.sales_crm.route_audit

1. Static: every /api route of the real app must have `auth.get_current_user` somewhere in its
   dependency tree (directly or through require_role / require_roles / require_write / require_*_access),
   except the explicit PUBLIC allow-list (login, refresh, password reset, OAuth callback …).
2. Dynamic: with AUTH_ENFORCED forced on, an unauthenticated GET to every /api GET route must not
   return 200 (path parameters are filled with 1). Only GET is sent, so nothing is changed.
Background workers are switched off for the run (COPILOT_AUTOSYNC=0, CRM_BACKGROUND=0). Exit 1 on failure.
"""

import os
import re
import sys

os.environ.setdefault("COPILOT_AUTOSYNC", "0")
os.environ.setdefault("CRM_BACKGROUND", "0")
os.environ["CRM_NOTIFY_SUPPRESS"] = "1"        # never email real people from a test run

PUBLIC = {
    ("POST", "/api/auth/login"), ("POST", "/api/auth/refresh"), ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/forgot-password"), ("POST", "/api/auth/reset-password"), ("GET", "/api/auth/reset-password/validate"),
    ("GET", "/api/crm/capture/callback/microsoft"),      # authenticated by the sealed OAuth state instead
    ("GET", "/api/health"), ("GET", "/api/crm/deals/meta"), ("GET", "/api/deals/meta"),
    ("GET", "/api/room/{token}"),                         # buyer room: the unguessable token is the access (#11)
}


def _has_auth(dependant, target) -> bool:
    if dependant.call is target:
        return True
    return any(_has_auth(d, target) for d in dependant.dependencies)


def _login_smoke(client):
    """Partner / viewer / rep sign in for real and hit representative endpoints."""
    import secrets
    from sqlalchemy import text
    import auth
    from db.connection import get_session
    s = get_session()
    made, conn_id, out = [], None, []
    pw = secrets.token_urlsafe(14)
    try:
        acct = s.execute(text("SELECT id FROM accounts ORDER BY id LIMIT 1")).scalar()
        for role in ("partner", "viewer", "user"):
            uid = s.execute(text("""INSERT INTO users (email, hashed_password, full_name, role, is_active, has_dashboard_access,
                                    has_command_center_access, has_tasks_access, has_pipeline_access, failed_login_count)
                                    VALUES (:e, :h, :n, :r, true, true, true, true, true, 0) RETURNING id"""),
                            {"e": f"route-audit-{role}-{secrets.token_hex(3)}@example.invalid", "h": auth.hash_password(pw),
                             "n": f"Audit {role}", "r": role}).scalar()
            made.append((role, uid))
            if role != "partner":
                s.execute(text("INSERT INTO user_account_access (user_id, account_id) VALUES (:u, :a)"), {"u": uid, "a": acct})
        conn_id = s.execute(text("INSERT INTO connectors (kind, name, user_id) VALUES ('partner', 'Audit partner', :u) RETURNING id"),
                            {"u": made[0][1]}).scalar()
        s.commit()
        tokens = {}
        for role, uid in made:
            email = s.execute(text("SELECT email FROM users WHERE id = :u"), {"u": uid}).scalar()
            r = client.post("/api/auth/login", json={"email": email, "password": pw})
            out.append((f"{role} logs in", r.status_code, 200))
            tokens[role] = {"Authorization": f"Bearer {r.json().get('access_token', '')}"}
        checks = [("anonymous: introductions", None, "GET", "/api/crm/introductions", 401),
                  ("anonymous: database download", None, "GET", "/api/database/download", 401),
                  ("partner: own introductions", "partner", "GET", "/api/partner/introductions", 200),
                  ("partner: accounts list", "partner", "GET", "/api/accounts", 403),
                  ("partner: copilot", "partner", "POST", "/api/copilot/chat", 403),
                  ("partner: database download", "partner", "GET", "/api/database/download", 403),
                  ("viewer: deals list", "viewer", "GET", "/api/deals", 200),
                  ("viewer: create deal", "viewer", "POST", "/api/deals", 403),
                  ("viewer: edit account", "viewer", "PATCH", f"/api/accounts/{acct}", 403),
                  ("viewer: pipeline run", "viewer", "POST", "/api/pipeline/run", 403),
                  ("rep: deals list", "user", "GET", "/api/deals", 200),
                  ("rep: database download", "user", "GET", "/api/database/download", 403),
                  ("rep: pipeline runs", "user", "GET", "/api/pipeline/runs", 403),
                  ("rep: partner portal", "user", "GET", "/api/partner/introductions", 403),
                  ("rep: paid data fetch (Diffbot)", "user", "POST", "/api/account/diffbot", 403),
                  ("rep: data fetch (GLEIF)", "user", "POST", "/api/account/gleif", 403),
                  ("rep: team list", "user", "GET", "/api/crm/team", 403),
                  ("rep: roles guide", "user", "GET", "/api/crm/roles", 200),
                  ("partner: roles guide", "partner", "GET", "/api/crm/roles", 403)]
        for label, role, method, path, want in checks:
            r = client.request(method, path, headers=tokens.get(role, {}), json={} if method != "GET" else None)
            out.append((label, r.status_code, want))
    finally:
        s.rollback()
        if conn_id:
            s.execute(text("DELETE FROM connectors WHERE id = :c"), {"c": conn_id})
        for _, uid in made:
            s.execute(text("DELETE FROM refresh_tokens WHERE user_id = :u"), {"u": uid})
            s.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        s.commit()
        s.close()
    return out


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    import auth
    import api as main_api
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient

    app = main_api.app

    def flatten(items):
        """FastAPI ≥0.14x keeps included routers as nested objects; walk their effective routes too."""
        for r in items:
            if isinstance(r, APIRoute):
                yield r
            elif hasattr(r, "effective_candidates"):
                ec = r.effective_candidates
                yield from flatten(list(ec() if callable(ec) else ec))
            elif hasattr(r, "dependant") and hasattr(r, "methods") and hasattr(r, "path"):
                yield r                                   # an effective route context

    routes = [r for r in flatten(app.router.routes) if r.path.startswith("/api")]
    unguarded, public_seen = [], []
    for r in routes:
        for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
            key = (m, r.path)
            if _has_auth(r.dependant, auth.get_current_user):
                continue
            (public_seen if key in PUBLIC else unguarded).append(key)

    auth.AUTH_ENFORCED = True
    client = TestClient(app, raise_server_exceptions=False)
    leaks = []
    probed = 0
    for r in routes:
        if "GET" not in r.methods or ("GET", r.path) in PUBLIC:
            continue
        path = re.sub(r"\{[^}]+\}", "1", r.path)
        resp = client.get(path)
        probed += 1
        if resp.status_code == 200:
            leaks.append((path, resp.status_code))

    # 3. Real logins through /api/auth/login, whole app, AUTH_ENFORCED on
    smoke = _login_smoke(client)

    print(f"API routes: {len(routes)} · public by design: {len(public_seen)} · unauthenticated GET probes: {probed}")
    for label, got, want in smoke:
        print(f"  {'ok  ' if got == want else 'FAIL'} {label:<52} got {got} expected {want}")
    for m, p in unguarded:
        print(f"  FAIL no auth dependency: {m} {p}")
    for p, code in leaks:
        print(f"  FAIL anonymous GET returned {code}: {p}")
    ok = not unguarded and not leaks and all(g == w for _, g, w in smoke)
    print("gate passed" if ok else "GATE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
