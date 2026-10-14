"""Permission-matrix gate (apps/sales_crm/README.md §7 "Testing"). Needs the real DB.

    python -m apps.sales_crm.permission_matrix

Creates throw-away users (rep, manager of that rep, viewer, partner, outsider rep) with
real JWTs, forces AUTH_ENFORCED=True, calls the deals / CRM / copilot routers plus the
role-guard middleware through TestClient, checks every expected status code, then deletes
everything it created. Exit code 1 on any mismatch.
"""

import secrets
import sys
from typing import List, Tuple

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

import auth
from db.connection import get_session

PREFIX = "crm-matrix-"


def _mk_user(s, role: str, manager_id=None) -> int:
    uid = s.execute(text("""INSERT INTO users (email, hashed_password, full_name, role, is_active, manager_id,
                                               has_dashboard_access, has_command_center_access, has_tasks_access,
                                               has_pipeline_access, failed_login_count)
                            VALUES (:e, :h, :n, :r, true, :m, true, true, true, true, 0) RETURNING id"""),
                    {"e": f"{PREFIX}{role}-{secrets.token_hex(3)}@example.invalid", "h": auth.hash_password(secrets.token_urlsafe(16)),
                     "n": f"Matrix {role}", "r": role, "m": manager_id}).scalar()
    return uid


def _token(s, uid: int) -> dict:
    from db.models.user import User
    u = s.query(User).filter_by(id=uid).one()
    return {"Authorization": f"Bearer {auth.create_access_token(u)}"}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")   # Windows consoles (cp1252) can't print arrows
    from apps.sales_crm import notify as _notify
    _notify.suppress()          # tests never email real people (see notify.suppress)
    from apps.sales_crm import api as crm
    from apps.sales_crm import activities, forecast, introductions, permissions
    from apps.sales_deals import api as deals
    crm.ensure_schema()
    deals.ensure_schema()

    auth.AUTH_ENFORCED = True
    app = FastAPI()
    app.middleware("http")(permissions.role_guard)
    app.include_router(crm.router)
    app.include_router(deals.router)
    app.include_router(introductions.router)
    app.include_router(introductions.partner_router)
    app.include_router(forecast.router)
    app.include_router(activities.router)
    c = TestClient(app)

    s = get_session()
    T0 = s.execute(text("SELECT now()")).scalar()
    created_users: List[int] = []
    created_deals: List[int] = []
    created_connectors: List[int] = []
    results: List[Tuple[str, int, int]] = []
    try:
        a_in, a_out = [r[0] for r in s.execute(text("SELECT id FROM accounts ORDER BY id LIMIT 2"))]
        admin = s.execute(text("SELECT id FROM users WHERE role = 'super_admin' AND is_active ORDER BY id LIMIT 1")).scalar()
        manager = _mk_user(s, "sales_manager")
        rep = _mk_user(s, "user", manager)
        viewer = _mk_user(s, "viewer")
        partner = _mk_user(s, "partner")
        outsider = _mk_user(s, "user")
        created_users += [rep, viewer, partner, outsider, manager]      # rep before manager (FK)
        for u in (rep, viewer, partner):                                  # partner grant must be ignored
            s.execute(text("INSERT INTO user_account_access (user_id, account_id) VALUES (:u, :a)"), {"u": u, "a": a_in})
        s.execute(text("INSERT INTO user_account_access (user_id, account_id) VALUES (:u, :a)"), {"u": outsider, "a": a_out})
        s.commit()
        H = {name: _token(s, uid) for name, uid in
             [("admin", admin), ("manager", manager), ("rep", rep), ("viewer", viewer), ("partner", partner), ("outsider", outsider)]}

        def check(label, resp, expected):
            results.append((label, resp.status_code, expected))
            return resp

        # create: rep in own account ok, rep outside 404, viewer 403 (guard), partner 403 (guard)
        r = check("rep creates deal in granted account", c.post("/api/deals", headers=H["rep"], json={"account_id": a_in, "name": "Matrix deal"}), 200)
        did = r.json().get("id")
        if did:
            created_deals.append(did)
        check("rep creates deal in other account", c.post("/api/deals", headers=H["rep"], json={"account_id": a_out, "name": "x"}), 404)
        check("viewer creates deal", c.post("/api/deals", headers=H["viewer"], json={"account_id": a_in, "name": "x"}), 403)
        check("partner creates deal", c.post("/api/deals", headers=H["partner"], json={"account_id": a_in, "name": "x"}), 403)

        # read
        check("rep reads own deal", c.get(f"/api/deals/{did}", headers=H["rep"]), 200)
        check("manager reads report's deal (team scope)", c.get(f"/api/deals/{did}", headers=H["manager"]), 200)
        check("viewer reads deal", c.get(f"/api/deals/{did}", headers=H["viewer"]), 200)
        check("outsider reads deal", c.get(f"/api/deals/{did}", headers=H["outsider"]), 404)
        check("partner reads deal", c.get(f"/api/deals/{did}", headers=H["partner"]), 403)
        check("partner lists deals", c.get("/api/deals", headers=H["partner"]), 403)
        lst = c.get("/api/deals", headers=H["outsider"]).json().get("deals", [])
        results.append(("outsider list excludes deal", 0 if any(d["id"] == did for d in lst) else 1, 1))

        # edit
        check("viewer edits deal", c.patch(f"/api/deals/{did}", headers=H["viewer"], json={"next_step": "x"}), 403)
        check("manager edits report's deal", c.patch(f"/api/deals/{did}", headers=H["manager"], json={"next_step": "Call"}), 200)
        bl = s.execute(text("SELECT id FROM business_lines WHERE key = 'fs'")).scalar()
        check("rep sets business line", c.patch(f"/api/deals/{did}", headers=H["rep"], json={"business_line_id": bl}), 200)
        check("rep sets unknown business line", c.patch(f"/api/deals/{did}", headers=H["rep"], json={"business_line_id": 999999}), 400)

        # CRM admin surfaces
        check("rep creates business line", c.post("/api/crm/business-lines", headers=H["rep"], json={"key": "zz", "name": "ZZ"}), 403)
        check("rep changes settings", c.put("/api/crm/settings", headers=H["rep"], json={"values": {"default_attribution_pct": 10}}), 403)
        check("rep sets a manager", c.patch(f"/api/crm/users/{rep}", headers=H["rep"], json={"clear_manager": True}), 403)
        check("rep reads team", c.get("/api/crm/team", headers=H["rep"]), 403)
        t = check("manager reads team", c.get("/api/crm/team", headers=H["manager"]), 200).json()
        results.append(("manager team = self + report only", 1 if sorted(x["id"] for x in t) == sorted([manager, rep]) else 0, 1))
        check("admin reads team", c.get("/api/crm/team", headers=H["admin"]), 200)
        check("admin creates loop manager", c.patch(f"/api/crm/users/{manager}", headers=H["admin"], json={"manager_id": rep}), 400)
        check("admin bad setting value", c.put("/api/crm/settings", headers=H["admin"], json={"values": {"fiscal_year_start_month": 13}}), 400)
        check("viewer reads meta", c.get("/api/crm/meta", headers=H["viewer"]), 200)
        check("partner reads meta", c.get("/api/crm/meta", headers=H["partner"]), 403)

        # delete: outsider no, viewer no (guard), manager yes (team owner)
        check("outsider deletes deal", c.delete(f"/api/deals/{did}", headers=H["outsider"]), 404)
        check("viewer deletes deal", c.delete(f"/api/deals/{did}", headers=H["viewer"]), 403)
        check("manager deletes report's deal", c.delete(f"/api/deals/{did}", headers=H["manager"]), 200)
        created_deals.clear()

        # ── M1 introductions ──
        persona = s.execute(text("SELECT id FROM personas WHERE account_id = :a ORDER BY id LIMIT 1"), {"a": a_in}).scalar()
        acct_name = s.execute(text("SELECT display_name FROM accounts WHERE id = :a"), {"a": a_in}).scalar()
        r = check("rep creates connector", c.post("/api/crm/connectors", headers=H["rep"],
                                                   json={"kind": "partner", "name": "Matrix Partners LLP"}), 200)
        conn_id = r.json()["id"]
        created_connectors.append(conn_id)
        check("rep links a login to connector", c.patch(f"/api/crm/connectors/{conn_id}", headers=H["rep"], json={"user_id": partner}), 403)
        check("admin links non-partner login", c.patch(f"/api/crm/connectors/{conn_id}", headers=H["admin"], json={"user_id": rep}), 400)
        check("admin links partner login", c.patch(f"/api/crm/connectors/{conn_id}", headers=H["admin"], json={"user_id": partner}), 200)
        check("viewer creates intro", c.post("/api/crm/introductions", headers=H["viewer"],
                                             json={"connector_id": conn_id, "account_id": a_in}), 403)
        check("rep creates intro in other account", c.post("/api/crm/introductions", headers=H["rep"],
                                                            json={"connector_id": conn_id, "account_id": a_out}), 404)
        r = check("rep creates intro", c.post("/api/crm/introductions", headers=H["rep"],
                                              json={"connector_id": conn_id, "account_id": a_in, "persona_id": persona,
                                                    "context": "Met at a conference"}), 200)
        iid = r.json()["id"]
        check("duplicate open intro", c.post("/api/crm/introductions", headers=H["rep"],
                                             json={"connector_id": conn_id, "account_id": a_in, "persona_id": persona}), 409)
        check("outsider reads intro", c.get(f"/api/crm/introductions/{iid}", headers=H["outsider"]), 404)
        check("manager reads report's intro", c.get(f"/api/crm/introductions/{iid}", headers=H["manager"]), 200)
        check("viewer reads intro", c.get(f"/api/crm/introductions/{iid}", headers=H["viewer"]), 200)
        check("viewer moves intro", c.patch(f"/api/crm/introductions/{iid}", headers=H["viewer"], json={"status": "requested"}), 403)
        check("partner uses internal intro API", c.get("/api/crm/introductions", headers=H["partner"]), 403)
        check("rep declines without reason", c.patch(f"/api/crm/introductions/{iid}", headers=H["rep"], json={"status": "declined"}), 400)
        check("rep sets converted directly", c.patch(f"/api/crm/introductions/{iid}", headers=H["rep"], json={"status": "converted"}), 400)
        mv = lambda status, **kw: c.patch(f"/api/crm/introductions/{iid}", headers=H["rep"], json={"status": status, **kw})  # noqa: E731
        check("rep skips a stage", mv("intro_made"), 400)
        check("rep moves to requested", mv("requested"), 200)
        check("rep moves to accepted", mv("accepted"), 200)
        check("rep converts before meeting held", c.post(f"/api/crm/introductions/{iid}/convert", headers=H["rep"], json={}), 400)
        check("rep moves back without reason", mv("requested"), 400)
        check("rep moves back with reason", mv("requested", reason="Contact asked to wait"), 200)
        check("rep moves forward again", mv("accepted"), 200)
        check("rep moves to intro made", mv("intro_made"), 200)
        check("rep moves to meeting held", mv("meeting_held"), 200)
        r = check("rep adds internal note", c.post(f"/api/crm/introductions/{iid}/notes", headers=H["rep"],
                                                   json={"text": "INTERNAL-ONLY budget is tight", "partner_visible": False}), 200)
        note_id = max(e["id"] for e in r.json()["events"] if e["kind"] == "note")
        status_ev = next(e["id"] for e in r.json()["events"] if e["kind"] == "status")
        check("viewer shares a note", c.patch(f"/api/crm/introductions/{iid}/events/{note_id}", headers=H["viewer"],
                                              json={"partner_visible": True}), 403)
        check("rep toggles a status event", c.patch(f"/api/crm/introductions/{iid}/events/{status_ev}", headers=H["rep"],
                                                    json={"partner_visible": False}), 400)
        check("rep shares then hides a note", c.patch(f"/api/crm/introductions/{iid}/events/{note_id}", headers=H["rep"],
                                                      json={"partner_visible": True}), 200)
        check("rep makes the note internal again", c.patch(f"/api/crm/introductions/{iid}/events/{note_id}", headers=H["rep"],
                                                           json={"partner_visible": False}), 200)
        r = check("rep converts intro", c.post(f"/api/crm/introductions/{iid}/convert", headers=H["rep"],
                                               json={"value_amount": 200000, "name": "Matrix intro deal"}), 200)
        conv = r.json()
        if conv.get("deal_id"):
            created_deals.append(conv["deal_id"])
        results.append(("default attribution is 50%", 1 if conv.get("attribution_pct") == 50.0 else 0, 1))
        results.append(("attributed value = 100,000", 1 if conv.get("attributed_value") == 100000.0 else 0, 1))
        dd = c.get(f"/api/deals/{conv.get('deal_id')}", headers=H["rep"]).json()
        results.append(("deal sourced by intro", 1 if dd.get("source") == "introduction" and (dd.get("introduction") or {}).get("id") == iid else 0, 1))
        results.append(("contact added to committee", 1 if any(x["persona_id"] == persona for x in dd.get("stakeholders", [])) else 0, 1))
        check("convert twice", c.post(f"/api/crm/introductions/{iid}/convert", headers=H["rep"], json={}), 400)
        check("converted intro moves back", mv("meeting_held", reason="oops"), 400)
        check("deal marked won at intro stage", c.patch(f"/api/deals/{conv.get('deal_id')}", headers=H["rep"], json={"stage": "won"}), 400)
        att = check("attribution report", c.get("/api/crm/introductions/attribution", headers=H["rep"]), 200).json()
        row = next((x for x in att["connectors"] if x["id"] == conn_id), {})
        results.append(("attributed pipeline = 100,000", 1 if row.get("attributed_pipeline") == 100000.0 else 0, 1))

        # partner portal: own intros only, limited fields, no internal notes
        pl = check("partner lists own intros", c.get("/api/partner/introductions", headers=H["partner"]), 200).json()["introductions"]
        results.append(("partner sees exactly their intro", 1 if [x["id"] for x in pl] == [iid] else 0, 1))
        leaked = [k for k in ("attribution_pct", "attributed_value", "deal", "value_amount", "connector") if pl and k in pl[0]]
        results.append(("partner view hides value/attribution", 0 if leaked else 1, 1))
        pd = check("partner reads own intro", c.get(f"/api/partner/introductions/{iid}", headers=H["partner"]), 200).json()
        results.append(("partner can't see internal note", 0 if "INTERNAL-ONLY" in str(pd) else 1, 1))
        check("rep uses partner portal", c.get("/api/partner/introductions", headers=H["rep"]), 403)
        r = check("partner submits intro by name", c.post("/api/partner/introductions", headers=H["partner"],
                  json={"account_name": acct_name, "contact_name": "Jane Example", "context": "Knows their CIO well from a prior role"}), 200)
        sub = r.json()
        results.append(("submission matched account quietly", 1 if s.execute(text("SELECT account_id FROM introductions WHERE id = :i"),
                                                                             {"i": sub.get("id")}).scalar() == a_in else 0, 1))
        r = check("partner submits unknown company", c.post("/api/partner/introductions", headers=H["partner"],
                  json={"account_name": "Nonexistent Holdings 9x", "contact_name": "Sam Doe", "context": "Warm lead from an advisory board"}), 200)
        tri = c.get(f"/api/crm/introductions/{r.json().get('id')}", headers=H["outsider"]).json()
        results.append(("unmatched submission in triage for all reps", 1 if tri.get("needs_triage") else 0, 1))
        check("partner reads other partner's intro id", c.get(f"/api/partner/introductions/{iid + 100000}", headers=H["partner"]), 404)
        # partner edits: own submission while Submitted/Requested only; never staff-logged or later-stage intros
        sid = sub.get("id")
        r = check("partner edits own submission", c.patch(f"/api/partner/introductions/{sid}", headers=H["partner"],
                  json={"contact_title": "Chief Data Officer", "context": "Knows their CIO well — met again last week"}), 200)
        ed = r.json()
        results.append(("edit saved and shown as typed", 1 if ed.get("contact_title") == "Chief Data Officer"
                        and ed.get("account_name") == acct_name and ed.get("editable") else 0, 1))
        results.append(("edit logged for partner", 1 if any(e["kind"] == "field" and "contact title" in (e["note"] or "")
                                                            for e in ed.get("events", [])) else 0, 1))
        check("partner blanks the company", c.patch(f"/api/partner/introductions/{sid}", headers=H["partner"], json={"account_name": ""}), 422)
        check("partner edits staff-logged intro", c.patch(f"/api/partner/introductions/{iid}", headers=H["partner"], json={"context": "x" * 20}), 400)
        check("rep uses partner edit", c.patch(f"/api/partner/introductions/{sid}", headers=H["rep"], json={"context": "x" * 20}), 403)
        check("partner edits other partner's intro id", c.patch(f"/api/partner/introductions/{sid + 100000}", headers=H["partner"],
                                                                json={"context": "x" * 20}), 404)
        s.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": sid}); s.commit()
        check("partner edits after Accepted", c.patch(f"/api/partner/introductions/{sid}", headers=H["partner"], json={"context": "x" * 20}), 400)

        # ── M2 forecasting ──
        from datetime import date, timedelta
        per = c.get("/api/forecast/meta", headers=H["rep"]).json()["current"]
        today = date.today().isoformat()
        eur_rate = float(s.execute(text("SELECT usd_rate FROM fx_rates WHERE currency = 'EUR'")).scalar())
        r = check("rep creates EUR deal closing this quarter", c.post("/api/deals", headers=H["rep"], json={
            "account_id": a_in, "name": "Matrix forecast deal", "value_amount": 100000, "currency": "EUR",
            "expected_close": today}), 200)
        fdid = r.json()["id"]
        created_deals.append(fdid)
        results.append(("amount_usd from fx rate", 1 if r.json().get("amount_usd") == round(100000 * eur_rate, 2) else 0, 1))
        check("rep sets category commit", c.patch(f"/api/deals/{fdid}", headers=H["rep"], json={"forecast_category": "commit"}), 200)
        check("rep sets category closed on open deal", c.patch(f"/api/deals/{fdid}", headers=H["rep"], json={"forecast_category": "closed"}), 400)
        check("viewer sets category", c.patch(f"/api/deals/{fdid}", headers=H["viewer"], json={"forecast_category": "best_case"}), 403)
        f = check("rep reads forecast", c.get("/api/forecast", headers=H["rep"], params={"period": per, "owner_id": rep}), 200).json()
        results.append(("commit bucket = converted EUR amount", 1 if f["totals"]["commit"] == round(100000 * eur_rate, 2) else 0, 1))
        check("viewer reads forecast", c.get("/api/forecast", headers=H["viewer"], params={"period": per}), 200)
        check("partner reads forecast", c.get("/api/forecast", headers=H["partner"], params={"period": per}), 403)
        fo = c.get("/api/forecast", headers=H["outsider"], params={"period": per}).json()
        results.append(("outsider forecast excludes deal", 0 if any(d["id"] == fdid for d in fo["deals"]) else 1, 1))
        check("bad period", c.get("/api/forecast", headers=H["rep"], params={"period": "2026Q3"}), 400)
        # targets
        check("rep sets own target", c.put("/api/forecast/targets", headers=H["rep"], json={"period": per, "user_id": rep, "amount_usd": 1}), 403)
        check("manager sets report's target", c.put("/api/forecast/targets", headers=H["manager"],
                                                    json={"period": per, "user_id": rep, "amount_usd": 400000}), 200)
        check("manager sets outsider's target", c.put("/api/forecast/targets", headers=H["manager"],
                                                      json={"period": per, "user_id": outsider, "amount_usd": 1}), 403)
        check("manager sets business-line target", c.put("/api/forecast/targets", headers=H["manager"],
                                                         json={"period": "FY2099-Q1", "business_line_id": bl, "amount_usd": 1}), 403)
        check("target needs exactly one owner", c.put("/api/forecast/targets", headers=H["admin"],
                                                      json={"period": "FY2099-Q1", "amount_usd": 1}), 400)
        check("admin sets business-line target", c.put("/api/forecast/targets", headers=H["admin"],
                                                       json={"period": "FY2099-Q1", "business_line_id": bl, "amount_usd": 5}), 200)
        s.execute(text("DELETE FROM sales_targets WHERE period = 'FY2099-Q1'")); s.commit()
        f = c.get("/api/forecast", headers=H["manager"], params={"period": per, "owner_id": rep}).json()["totals"]
        results.append(("target 400,000 and gap shown", 1 if f["target"] == 400000 and f["gap"] == 400000 else 0, 1))
        check("rep edits fx rate", c.put("/api/forecast/fx", headers=H["rep"], json={"currency": "EUR", "usd_rate": 2}), 403)
        # changes vs a snapshot
        s.execute(text("""INSERT INTO forecast_snapshots (snapshot_date, deal_id, name, account_id, owner_user_id, stage,
                          forecast_category, amount_usd, expected_close)
                          VALUES (:d, :id, 'Matrix forecast deal', :a, :u, 'intro', 'pipeline', 1, :ec)"""),
                  {"d": date.today() - timedelta(days=1), "id": fdid, "a": a_in, "u": rep, "ec": date.today()})
        s.commit()
        check("rep skips a deal stage", c.patch(f"/api/deals/{fdid}", headers=H["rep"], json={"stage": "proposal"}), 400)
        check("rep advances deal to discovery", c.patch(f"/api/deals/{fdid}", headers=H["rep"], json={"stage": "discovery"}), 200)
        check("rep advances deal", c.patch(f"/api/deals/{fdid}", headers=H["rep"], json={"stage": "proposal"}), 200)
        ch = check("rep reads changes", c.get("/api/forecast/changes", headers=H["rep"], params={"period": per}), 200).json()
        kinds = {x["type"] for x in ch["changes"] if x["deal_id"] == fdid}
        results.append(("changes: advanced + category + amount", 1 if {"advanced", "category", "amount"} <= kinds else 0, 1))
        # won → closed category, closed_at, counted as closed
        check("rep marks deal won", c.patch(f"/api/deals/{fdid}", headers=H["rep"], json={"stage": "won"}), 200)
        w = s.execute(text("SELECT forecast_category, closed_at IS NOT NULL FROM deals WHERE id = :d"), {"d": fdid}).fetchone()
        results.append(("won → category closed + closed_at", 1 if tuple(w) == ("closed", True) else 0, 1))
        f = c.get("/api/forecast", headers=H["rep"], params={"period": per, "owner_id": rep}).json()["totals"]
        results.append(("won counted as closed", 1 if f["closed"] == round(100000 * eur_rate, 2) and f["commit"] == 0 else 0, 1))
        check("rep reopens deal", c.patch(f"/api/deals/{fdid}", headers=H["rep"], json={"stage": "contract"}), 200)
        w = s.execute(text("SELECT forecast_category, closed_at FROM deals WHERE id = :d"), {"d": fdid}).fetchone()
        results.append(("reopen → pipeline, closed_at cleared", 1 if tuple(w) == ("pipeline", None) else 0, 1))
        check("forecast export", c.get("/api/forecast/export", headers=H["rep"], params={"period": per}), 200)

        # ── M3 activities ──
        import base64
        r = check("rep creates M3 deal", c.post("/api/deals", headers=H["rep"], json={"account_id": a_in, "name": "Matrix M3 deal"}), 200)
        adid = r.json()["id"]
        created_deals.append(adid)
        link = [{"object_type": "deal", "object_id": adid}]
        check("viewer logs activity", c.post("/api/crm/activities", headers=H["viewer"], json={"type": "call", "subject": "x", "links": link}), 403)
        check("partner reads timeline", c.get("/api/crm/activities", headers=H["partner"], params={"object_type": "deal", "object_id": adid}), 403)
        check("activity without links", c.post("/api/crm/activities", headers=H["rep"], json={"type": "call", "subject": "x", "links": []}), 400)
        check("rep logs on other account", c.post("/api/crm/activities", headers=H["rep"], json={
            "type": "call", "subject": "x", "links": [{"object_type": "account", "object_id": a_out}]}), 404)
        r = check("rep logs call on deal", c.post("/api/crm/activities", headers=H["rep"], json={
            "type": "call", "subject": "Discovery call", "summary": "Budget approved for Q4", "links": link,
            "participant_persona_ids": [persona]}), 200)
        act = r.json()
        kinds = {(l["object_type"], l["object_id"]) for l in act["links"]}
        results.append(("links: deal + contact + derived account", 1 if {("deal", adid), ("persona", persona), ("account", a_in)} <= kinds else 0, 1))
        r = check("rep logs private note", c.post("/api/crm/activities", headers=H["rep"], json={
            "type": "note", "subject": "PRIVATE-NOTE", "links": link, "visibility": "private"}), 200)
        priv_id = r.json()["id"]
        tl = check("manager reads deal timeline", c.get("/api/crm/activities", headers=H["manager"],
                                                        params={"object_type": "deal", "object_id": adid}), 200).json()
        results.append(("manager can't see rep's private note", 0 if any(a["id"] == priv_id for a in tl["activities"]) else 1, 1))
        tl = c.get("/api/crm/activities", headers=H["rep"], params={"object_type": "account", "object_id": a_in}).json()
        results.append(("account timeline includes deal call", 1 if any(a["id"] == act["id"] for a in tl["activities"]) else 0, 1))
        check("outsider reads deal timeline", c.get("/api/crm/activities", headers=H["outsider"], params={"object_type": "deal", "object_id": adid}), 404)
        check("outsider reads activity", c.get(f"/api/crm/activities/{act['id']}", headers=H["outsider"]), 404)
        check("manager reads private note by id", c.get(f"/api/crm/activities/{priv_id}", headers=H["manager"]), 404)
        check("manager deletes rep's activity", c.delete(f"/api/crm/activities/{act['id']}", headers=H["manager"]), 403)
        check("viewer edits activity", c.patch(f"/api/crm/activities/{act['id']}", headers=H["viewer"], json={"subject": "y"}), 403)
        dl = s.execute(text("SELECT last_activity_at IS NOT NULL FROM deals WHERE id = :d"), {"d": adid}).scalar()
        results.append(("deal.last_activity_at set", 1 if dl else 0, 1))
        dd = c.get(f"/api/deals/{adid}", headers=H["rep"]).json()
        results.append(("deal payload has customer touch", 1 if dd.get("last_customer_touch") else 0, 1))
        check("rep deletes own private note", c.delete(f"/api/crm/activities/{priv_id}", headers=H["rep"]), 200)

        # meeting logged with an introduced contact → intro moves to meeting_held
        r = c.post("/api/crm/connectors", headers=H["rep"], json={"kind": "advisor", "name": "Matrix Advisor M3"})
        created_connectors.append(r.json()["id"])
        pname = s.execute(text("SELECT coalesce(full_name, display_name) FROM personas WHERE id = :p"), {"p": persona}).scalar()
        persona2 = s.execute(text("SELECT id FROM personas WHERE account_id = :a AND id <> :p ORDER BY id LIMIT 1"), {"a": a_in, "p": persona}).scalar()
        r = c.post("/api/crm/introductions", headers=H["rep"], json={"connector_id": created_connectors[-1], "account_id": a_in,
                                                                     "persona_id": persona2, "status": "intro_made"})
        iid2 = r.json()["id"]
        c.post("/api/crm/activities", headers=H["rep"], json={"type": "meeting", "subject": "Intro meeting",
                                                              "links": [{"object_type": "persona", "object_id": persona2}]})
        st2 = c.get(f"/api/crm/introductions/{iid2}", headers=H["rep"]).json()["status"]
        results.append(("meeting auto-moves intro to meeting_held", 1 if st2 == "meeting_held" else 0, 1))

        # transcript upload
        vtt = (f"WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n<v {pname}>Our priority this quarter is cutting reconciliation time by 30 percent.</v>\n\n"
               f"00:00:06.000 --> 00:00:09.000\n<v Someone Unknown>Understood. I will send over the governance one-pager by Friday.</v>\n\n"
               f"00:00:10.000 --> 00:00:14.000\n<v {pname}>The budget decision sits with our CFO and security approval is needed before any pilot.</v>\n")
        b64 = base64.b64encode(vtt.encode()).decode()
        check("viewer uploads transcript", c.post("/api/crm/activities/transcript", headers=H["viewer"], json={
            "file_name": "call.vtt", "content_base64": b64, "account_id": a_in}), 403)
        check("transcript bad extension", c.post("/api/crm/activities/transcript", headers=H["rep"], json={
            "file_name": "call.pdf", "content_base64": b64, "account_id": a_in}), 400)
        check("transcript deal at other account", c.post("/api/crm/activities/transcript", headers=H["rep"], json={
            "file_name": "call.vtt", "content_base64": b64, "account_id": a_out, "deal_id": adid}), 404)
        tr = check("rep uploads transcript", c.post("/api/crm/activities/transcript", headers=H["rep"], json={
            "file_name": "call.vtt", "content_base64": b64, "account_id": a_in, "deal_id": adid}), 200).json()
        results.append(("transcript speaker matched to contact", 1 if tr.get("matched_contacts") == 1 else 0, 1))
        results.append(("transcript action item found", 1 if any("one-pager" in x["text"] for x in tr.get("action_items", [])) else 0, 1))
        results.append(("transcript summary has budget point", 1 if "budget" in (tr.get("summary") or "").lower() else 0, 1))

        # ── M4 capture ──
        from apps.sales_crm.capture import api as cap_api
        app.include_router(cap_api.router)
        check("rep reads capture status", c.get("/api/crm/capture/status", headers=H["rep"]), 200)
        check("viewer reads capture status", c.get("/api/crm/capture/status", headers=H["viewer"]), 403)
        check("partner reads capture status", c.get("/api/crm/capture/status", headers=H["partner"]), 403)
        check("rep reads capture admin", c.get("/api/crm/capture/admin", headers=H["rep"]), 403)
        check("rep changes settings w/o connection", c.patch("/api/crm/capture/settings", headers=H["rep"], json={"capture_email": False}), 404)
        check("rep sets org body policy", c.put("/api/crm/settings", headers=H["rep"], json={"values": {"capture_allow_bodies": True}}), 403)

        # ── Phase 1.1: contacts by hand, account owner / business line ──
        from apps.sales_crm import records
        app.include_router(records.router)
        r = check("rep adds contact", c.post("/api/crm/contacts", headers=H["rep"], json={
            "account_id": a_in, "full_name": "Matrix Contact Person", "title": "CDO", "email": "matrix.contact@example-corp.com"}), 200)
        mcid = r.json().get("id")
        check("contact with personal email refused", c.post("/api/crm/contacts", headers=H["rep"], json={
            "account_id": a_in, "full_name": "Matrix Other", "email": "someone@gmail.com"}), 400)
        check("duplicate contact refused", c.post("/api/crm/contacts", headers=H["rep"], json={
            "account_id": a_in, "full_name": "Matrix Contact Person"}), 409)
        check("rep adds contact at other account", c.post("/api/crm/contacts", headers=H["rep"], json={
            "account_id": a_out, "full_name": "Matrix X"}), 404)
        check("viewer adds contact", c.post("/api/crm/contacts", headers=H["viewer"], json={
            "account_id": a_in, "full_name": "Matrix Y"}), 403)
        check("manager edits report's contact", c.patch(f"/api/crm/contacts/{mcid}", headers=H["manager"], json={"title": "Chief Data Officer"}), 200)
        check("outsider edits contact", c.patch(f"/api/crm/contacts/{mcid}", headers=H["outsider"], json={"title": "x"}), 404)
        check("manager deletes rep's hand-added contact", c.delete(f"/api/crm/contacts/{mcid}", headers=H["manager"]), 403)
        check("pipeline contact can't be deleted", c.delete(f"/api/crm/contacts/{persona}", headers=H["rep"]), 400)
        check("rep deletes own contact", c.delete(f"/api/crm/contacts/{mcid}", headers=H["rep"]), 200)
        cr = check("rep reads account owner info", c.get(f"/api/crm/accounts/{a_in}/crm", headers=H["rep"]), 200).json()
        results.append(("assignable includes rep + manager, not outsider",
                        1 if {rep, manager} <= {u["id"] for u in cr.get("assignable", [])} and outsider not in {u["id"] for u in cr.get("assignable", [])} else 0, 1))
        orig_owner = s.execute(text("SELECT owner_user_id, primary_business_line_id FROM accounts WHERE id = :a"), {"a": a_in}).fetchone()
        check("owner without account access refused", c.patch(f"/api/crm/accounts/{a_in}", headers=H["rep"], json={"owner_user_id": outsider}), 400)
        check("viewer sets account owner", c.patch(f"/api/crm/accounts/{a_in}", headers=H["viewer"], json={"owner_user_id": rep}), 403)
        check("rep sets manager as owner + business line", c.patch(f"/api/crm/accounts/{a_in}", headers=H["rep"],
                                                                   json={"owner_user_id": manager, "primary_business_line_id": bl}), 200)
        n = s.execute(text("SELECT count(*) FROM crm_notifications WHERE user_id = :u AND kind = 'account_assigned'"), {"u": manager}).scalar()
        results.append(("new owner gets an account_assigned notification", 1 if n == 1 else 0, 1))
        s.execute(text("UPDATE accounts SET owner_user_id = :o, primary_business_line_id = :b WHERE id = :a"),
                  {"o": orig_owner[0], "b": orig_owner[1], "a": a_in})
        s.commit()

        # account scope used by the main app's require_account_access
        results.append(("manager inherits report's account", int(a_in in auth.get_accessible_account_ids(s, manager)), 1))
        results.append(("partner grant ignored", int(auth.get_accessible_account_ids(s, partner) == []), 1))
    finally:
        s.rollback()
        for cid in created_connectors:
            s.execute(text("DELETE FROM introductions WHERE connector_id = :c"), {"c": cid})
        for d in created_deals:
            s.execute(text("DELETE FROM forecast_snapshots WHERE deal_id = :d"), {"d": d})
            s.execute(text("DELETE FROM deals WHERE id = :d"), {"d": d})
        for cid in created_connectors:
            s.execute(text("DELETE FROM connectors WHERE id = :c"), {"c": cid})
        s.execute(text("DELETE FROM crm_notifications WHERE error = 'Suppressed: created by a test run' AND created_at >= :t0"), {"t0": T0})
        s.execute(text("DELETE FROM activities WHERE owner_user_id = ANY(:u)"), {"u": created_users})
        s.execute(text("DELETE FROM personas WHERE created_by_user_id = ANY(:u)"), {"u": created_users})
        for u in created_users:
            s.execute(text("DELETE FROM users WHERE id = :u"), {"u": u})
        s.commit()
        s.close()

    bad = [r for r in results if r[1] != r[2]]
    for label, got, exp in results:
        print(f"  {'ok  ' if got == exp else 'FAIL'} {label:<48} got {got} expected {exp}")
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (" — GATE FAILED" if bad else " — gate passed"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
