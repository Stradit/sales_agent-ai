"""Email-notification check — no real email is ever sent.

    python -m apps.sales_crm.notify_selftest

Creates throw-away users (rep, manager, partner) with deliverable-looking addresses, then triggers every
notification hook through the real API code and checks the outbox. The org switch is never touched in the
database: "enabled" is patched *inside this process only*, and the SMTP sender is replaced by a stub that
records what would have gone out. Everything it creates is deleted. Exit 1 on failure.
"""

import secrets
import sys
from datetime import datetime
from typing import List, Tuple

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

import auth
from db.connection import get_session

TAG = secrets.token_hex(3)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from apps.sales_crm import api as crm, introductions, notify, permissions, records
    crm.ensure_schema()

    sent: List[Tuple[str, str]] = []
    notify.suppress()               # nobody may get an email until the test users exist (below)
    notify._sender = lambda to, subject, body, html_body=None: sent.append((to, subject)) or True
    real_enabled = notify.enabled
    auth.AUTH_ENFORCED = True
    app = FastAPI()
    app.middleware("http")(permissions.role_guard)
    for r in (crm.router, introductions.router, introductions.partner_router, records.router, notify.router):
        app.include_router(r)
    c = TestClient(app)

    s = get_session()
    T0 = s.execute(text("SELECT now()")).scalar()
    users, connectors, results = [], [], []

    def mk(role, manager=None):
        uid = s.execute(text("""INSERT INTO users (email, hashed_password, full_name, role, is_active, manager_id, has_dashboard_access,
                                has_command_center_access, has_tasks_access, has_pipeline_access, failed_login_count)
                                VALUES (:e, :h, :n, :r, true, :m, true, true, true, true, 0) RETURNING id"""),
                        {"e": f"notify-selftest-{role}-{TAG}@stradit-selftest.co", "h": auth.hash_password(secrets.token_urlsafe(12)),
                         "n": f"Selftest {role.title()}", "r": role, "m": manager}).scalar()
        users.append(uid)
        return uid

    def tok(uid):
        from db.models.user import User
        return {"Authorization": f"Bearer {auth.create_access_token(s.query(User).filter_by(id=uid).one())}"}

    def outbox(uid, kind=None):
        return s.execute(text("SELECT kind, status, subject FROM crm_notifications WHERE user_id = :u"
                              + (" AND kind = :k" if kind else "") + " ORDER BY id"), {"u": uid, "k": kind}).fetchall()

    def check(label, ok):
        results.append((label, bool(ok)))

    try:
        acct = s.execute(text("SELECT id, display_name FROM accounts ORDER BY id LIMIT 1")).fetchone()
        persona = s.execute(text("SELECT id FROM personas WHERE account_id = :a ORDER BY id LIMIT 1"), {"a": acct[0]}).scalar()
        manager = mk("sales_manager")
        rep = mk("user", manager)
        partner = mk("partner")
        s.execute(text("INSERT INTO user_account_access (user_id, account_id) VALUES (:u, :a)"), {"u": rep, "a": acct[0]})
        s.commit()
        notify.suppress({rep, manager, partner})     # only these throw-away users can ever get a (stub) email
        H = {"rep": tok(rep), "manager": tok(manager), "partner": tok(partner)}

        # 1. While the org switch is off, notifications are recorded as 'held' and nothing is sent
        notify.enabled = lambda _s: False
        cid = c.post("/api/crm/connectors", headers=H["rep"], json={"kind": "partner", "name": f"Selftest Partner {TAG}"}).json()["id"]
        connectors.append(cid)
        s.execute(text("UPDATE connectors SET user_id = :u WHERE id = :c"), {"u": partner, "c": cid}); s.commit()
        iid = c.post("/api/crm/introductions", headers=H["rep"], json={"connector_id": cid, "account_id": acct[0], "persona_id": persona,
                                                                       "owner_user_id": manager}).json()["id"]
        check("assigning an intro to someone else notifies them", [k for k, st, _ in outbox(manager, "intro_assigned")] == ["intro_assigned"])
        check("…held while the org switch is off", all(st == "held" for _, st, _ in outbox(manager, "intro_assigned")))
        notify.send_due()
        check("nothing sent while off", not sent)

        # 2. Switch on (in this process only): partner hears about status changes, notes, conversion
        notify.enabled = lambda _s: True
        c.patch(f"/api/crm/introductions/{iid}", headers=H["rep"], json={"status": "requested"})
        c.post(f"/api/crm/introductions/{iid}/notes", headers=H["rep"], json={"text": "Booked for Tuesday", "partner_visible": True})
        c.post(f"/api/crm/introductions/{iid}/notes", headers=H["rep"], json={"text": "INTERNAL budget note", "partner_visible": False})
        p_out = outbox(partner, "partner_intro_update")
        check("partner notified of status change + visible note (2)", len(p_out) == 2)
        check("internal notes never reach the partner", not any("INTERNAL" in subj for _, _, subj in p_out))
        c.patch(f"/api/crm/introductions/{iid}", headers=H["rep"], json={"status": "requested"})       # no-op
        check("no duplicate for a no-op change", len(outbox(partner, "partner_intro_update")) == 2)
        r = notify.send_due()
        check("queued emails are sent through the sender", r.get("sent", 0) >= 2 and any("stradit-selftest" in to for to, _ in sent))

        # 3. Partner submission → triage for managers/admins; partner note → intro owner
        c.post("/api/partner/introductions", headers=H["partner"], json={"account_name": f"Unknown Co {TAG}", "contact_name": "Pat Doe",
                                                                         "context": "Knows their new CDO from a prior role"})
        check("unmatched partner submission → triage email to managers", len(outbox(manager, "triage_needed")) == 1)
        real = s.execute(text("""SELECT count(*) FROM crm_notifications n JOIN users u ON u.id = n.user_id
                                  WHERE n.subject LIKE :t AND n.status <> 'skipped' AND u.id <> ALL(:ids)"""),
                         {"t": f"%{TAG}%", "ids": [rep, manager, partner]}).scalar()
        check("real managers/admins are suppressed during the test", real == 0)
        c.post(f"/api/partner/introductions/{iid}/notes", headers=H["partner"], json={"text": "They asked for a case study"})
        check("partner note → intro owner", len(outbox(manager, "partner_note")) == 1)
        check("partner's own note doesn't email the partner", len(outbox(partner, "partner_intro_update")) == 2)

        # 4. Preferences: turning a kind off → 'skipped'
        c.put("/api/crm/notifications/prefs", headers=H["manager"], json={"settings": {"partner_note": False}})
        c.post(f"/api/partner/introductions/{iid}/notes", headers=H["partner"], json={"text": "Second update"})
        check("switched-off kind is skipped", [st for _, st, _ in outbox(manager, "partner_note")][-1:] == ["skipped"])
        check("unknown preference refused", c.put("/api/crm/notifications/prefs", headers=H["rep"],
                                                  json={"settings": {"nope": True}}).status_code == 400)
        pp = c.get("/api/partner/notifications", headers=H["partner"]).json()
        c.put("/api/partner/notifications", headers=H["partner"], json={"email_updates": False})
        c.patch(f"/api/crm/introductions/{iid}", headers=H["rep"], json={"status": "accepted"})
        check("partner can opt out of emails", pp.get("email_updates") is True and outbox(partner, "partner_intro_update")[-1][1] == "skipped")

        # 5. Stale intro → owner; account ownership → new owner; test email
        s.execute(text("UPDATE introductions SET updated_at = now() - interval '40 days' WHERE id = :i"), {"i": iid}); s.commit()
        introductions.mark_stale(s)
        check("stale intro → owner", len(outbox(manager, "intro_stale")) == 1)
        orig = s.execute(text("SELECT owner_user_id FROM accounts WHERE id = :a"), {"a": acct[0]}).scalar()
        c.patch(f"/api/crm/accounts/{acct[0]}", headers=H["rep"], json={"owner_user_id": manager})
        check("new account owner notified", len(outbox(manager, "account_assigned")) == 1)
        s.execute(text("UPDATE accounts SET owner_user_id = :o WHERE id = :a"), {"o": orig, "a": acct[0]}); s.commit()
        t = c.post("/api/crm/notifications/test", headers=H["rep"]).json()
        check("test email goes out immediately", t.get("status") == "sent")
        check("partner can't read internal notification settings", c.get("/api/crm/notifications/prefs", headers=H["partner"]).status_code == 403)
        check("rep can't read the admin log", c.get("/api/crm/notifications/admin", headers=H["rep"]).status_code == 403)

        # 6. Weekly pipeline email (idempotent per ISO week)
        did = s.execute(text("INSERT INTO deals (account_id, name, owner_user_id, stage, value_amount, next_step, next_step_due) "
                             "VALUES (:a, 'Selftest weekly deal', :u, 'discovery', 50000, 'Call', current_date - 3) RETURNING id"),
                        {"a": acct[0], "u": rep}).scalar(); s.commit()
        try:
            notify.queue_weekly(datetime(2026, 9, 21, 8, tzinfo=notify.IST), only_user_ids={rep, manager})
            notify.queue_weekly(datetime(2026, 9, 21, 9, tzinfo=notify.IST), only_user_ids={rep, manager})
            w = outbox(rep, "weekly_pipeline")
            check("weekly pipeline email queued once for the rep", len(w) == 1)
            body = s.execute(text("SELECT body FROM crm_notifications WHERE user_id = :u AND kind = 'weekly_pipeline'"), {"u": rep}).scalar() or ""
            check("weekly email lists the overdue deal", "Selftest weekly deal" in body and "Overdue" in body)
            check("manager gets a team email too", len(outbox(manager, "weekly_pipeline")) == 1)
        finally:
            s.execute(text("DELETE FROM deals WHERE id = :d"), {"d": did}); s.commit()
    finally:
        notify.enabled = real_enabled
        notify._sender = None
        notify.suppress()
        s.rollback()
        for cid in connectors:
            s.execute(text("DELETE FROM introductions WHERE connector_id = :c"), {"c": cid})
            s.execute(text("DELETE FROM connectors WHERE id = :c"), {"c": cid})
        s.execute(text("DELETE FROM crm_notifications WHERE error = 'Suppressed: created by a test run' AND created_at >= :t0"), {"t0": T0})
        for u in users:                         # crm_notifications / prefs cascade
            s.execute(text("DELETE FROM users WHERE id = :u"), {"u": u})
        s.commit()
        s.close()

    bad = [r for r in results if not r[1]]
    for label, ok in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + (" - FAILED" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
