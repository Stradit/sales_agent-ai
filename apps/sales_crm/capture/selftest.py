"""End-to-end capture check against a fake Microsoft Graph — no credentials, no network.

    python -m apps.sales_crm.capture.selftest

Creates two throw-away reps with access to one account, seals fake tokens (already expired, so the
refresh path runs), serves Inbox / Sent / Calendar delta pages from httpx.MockTransport, runs the real
sync engine twice (initial + delta), and checks matching, privacy, dedupe, deal / intro linking,
removals, throttling and revoked-consent handling. Everything it creates is deleted. Exit 1 on failure.
"""

import json
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

import httpx
from sqlalchemy import text

import auth
from apps.sales_crm.capture import crypto, engine as cap
from db.connection import get_session

PREFIX = "capture-selftest-"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from apps.sales_crm import notify as _notify
    _notify.suppress()          # tests never email real people (see notify.suppress)
    from apps.sales_crm import api as crm
    crm.ensure_schema()
    s = get_session()
    results: List[Tuple[str, bool]] = []
    users, deals, connectors = [], [], []
    now = datetime.now(timezone.utc)
    try:
        acct = s.execute(text("SELECT id, domain FROM accounts WHERE domain IS NOT NULL AND domain <> '' ORDER BY id LIMIT 1")).fetchone()
        aid, adom = acct[0], acct[1]
        prow = s.execute(text("""SELECT id, lower(email), coalesce(full_name, display_name) FROM personas
                                 WHERE account_id = :a AND email ILIKE :d ORDER BY id LIMIT 2"""), {"a": aid, "d": f"%@{adom}"}).fetchall()
        (pid, pemail, pname), (pid2, pemail2, _) = prow[0], prow[1]
        other_acct = s.execute(text("SELECT id FROM accounts WHERE id <> :a ORDER BY id LIMIT 1"), {"a": aid}).scalar()
        other_dom = s.execute(text("SELECT domain FROM accounts WHERE id = :a"), {"a": other_acct}).scalar() or "example-other.com"

        def mk_user(tag):
            uid = s.execute(text("""INSERT INTO users (email, hashed_password, full_name, role, is_active, has_dashboard_access,
                                    has_command_center_access, has_tasks_access, has_pipeline_access, failed_login_count)
                                    VALUES (:e, :h, :n, 'user', true, true, true, true, true, 0) RETURNING id"""),
                            {"e": f"{PREFIX}{tag}-{secrets.token_hex(3)}@stradit.com", "h": auth.hash_password(secrets.token_urlsafe(12)),
                             "n": f"Selftest {tag}"}).scalar()
            s.execute(text("INSERT INTO user_account_access (user_id, account_id) VALUES (:u, :a)"), {"u": uid, "a": aid})
            users.append(uid)
            return uid

        rep, rep2 = mk_user("a"), mk_user("b")
        mailbox = s.execute(text("SELECT email FROM users WHERE id = :u"), {"u": rep}).scalar()
        mailbox2 = s.execute(text("SELECT email FROM users WHERE id = :u"), {"u": rep2}).scalar()
        # deal with the contact on the committee, and an introduction at "intro made"
        did = s.execute(text("INSERT INTO deals (account_id, name, owner_user_id, stage) VALUES (:a, 'Selftest deal', :u, 'discovery') RETURNING id"),
                        {"a": aid, "u": rep}).scalar()
        deals.append(did)
        s.execute(text("INSERT INTO deal_stakeholders (deal_id, persona_id, role) VALUES (:d, :p, 'champion')"), {"d": did, "p": pid})
        cid = s.execute(text("INSERT INTO connectors (kind, name) VALUES ('advisor', 'Selftest advisor') RETURNING id")).scalar()
        connectors.append(cid)
        iid = s.execute(text("""INSERT INTO introductions (connector_id, account_id, persona_id, status, intro_made_at)
                                VALUES (:c, :a, :p, 'intro_made', now()) RETURNING id"""), {"c": cid, "a": aid, "p": pid}).scalar()
        s.commit()

        expired = {"access_token": "old", "refresh_token": "r1", "expires_at": int(time.time()) - 10}
        conn_ids = {}
        for uid, mb in ((rep, mailbox), (rep2, mailbox2)):
            conn_ids[uid] = s.execute(text("""INSERT INTO capture_connections (user_id, provider, account_email, token_encrypted, settings)
                VALUES (:u, 'microsoft', :m, :t, CAST(:st AS jsonb)) RETURNING id"""),
                {"u": uid, "m": mb, "t": crypto.seal(expired),
                 "st": json.dumps({"capture_email": True, "capture_calendar": True, "store_bodies": False,
                                   "exclude_internal_only": True, "exclude_domains": ["blocked-example.com"]})}).scalar()
        s.commit()

        def msg(mid, frm, to, subject, when, draft=False):
            return {"id": mid, "internetMessageId": f"<{mid}@test>", "subject": subject, "isDraft": draft,
                    "from": {"emailAddress": {"address": frm, "name": frm.split("@")[0]}},
                    "toRecipients": [{"emailAddress": {"address": x, "name": x.split("@")[0]}} for x in to],
                    "ccRecipients": [], "receivedDateTime": iso(when) + "Z", "sentDateTime": iso(when) + "Z",
                    "bodyPreview": f"Preview of {subject}", "conversationId": f"conv-{mid}"}

        past = now - timedelta(days=2)
        inbox = [msg("m1", pemail, [mailbox], "Re: governance pilot", past),
                 msg("m2", f"someone.new@{adom}", [mailbox], "Intro from a new BNY contact", past),
                 msg("m3", "friend@gmail.com", [mailbox], "Personal note", past),
                 msg("m4", "colleague@stradit.com", [mailbox], "Internal sync", past),
                 msg("m5", pemail, [mailbox], "Draft", past, draft=True),
                 msg("m6", "x@blocked-example.com", [mailbox, pemail], "Blocked domain", past)]
        if other_dom:
            inbox.append(msg("m7", f"someone@{other_dom}", [mailbox], "Other account (no access)", past))
        sent = [msg("s1", mailbox, [pemail, "friend@gmail.com"], "Follow-up: pilot scope", past + timedelta(hours=1))]
        event = {"id": "e1", "iCalUId": "ical-e1", "type": "singleInstance", "subject": "Discovery meeting",
                 "start": {"dateTime": iso(past), "timeZone": "UTC"}, "end": {"dateTime": iso(past + timedelta(minutes=45)), "timeZone": "UTC"},
                 "attendees": [{"emailAddress": {"address": pemail2, "name": "Second contact"}, "type": "required"},
                               {"emailAddress": {"address": pemail, "name": pname}, "type": "required"}],
                 "organizer": {"emailAddress": {"address": mailbox, "name": "Rep"}}, "bodyPreview": "Agenda", "isCancelled": False,
                 "isOnlineMeeting": True}
        future = dict(event, id="e2", iCalUId="ical-e2", subject="Pilot kickoff (future)",
                      start={"dateTime": iso(now + timedelta(days=5))}, end={"dateTime": iso(now + timedelta(days=5, hours=1))})

        state = {"round": 1, "refreshes": 0, "mode": "ok"}

        def handler(req: httpx.Request) -> httpx.Response:
            url = str(req.url)
            if "login.microsoftonline.com" in url:
                state["refreshes"] += 1
                if state["mode"] == "revoked":
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS70000: consent revoked"})
                return httpx.Response(200, json={"access_token": f"new-{state['refreshes']}", "refresh_token": "r2", "expires_in": 3600})
            if state["mode"] == "throttle":
                return httpx.Response(429, headers={"Retry-After": "120"}, json={})
            if "deltatoken=" in url:          # second round: changes only
                if "inbox" in url:
                    return httpx.Response(200, json={"value": [{"id": "m1", "@removed": {"reason": "deleted"}}],
                                                     "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=i2"})
                if "calendarView" in url:
                    return httpx.Response(200, json={"value": [dict(event, subject="Discovery meeting (renamed)")],
                                                     "@odata.deltaLink": "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=c2"})
                return httpx.Response(200, json={"value": [], "@odata.deltaLink": url})
            if "/mailFolders/inbox/messages/delta" in url:
                if "skiptoken=p2" in url:
                    return httpx.Response(200, json={"value": inbox[3:], "@odata.deltaLink":
                                                     "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=i1"})
                return httpx.Response(200, json={"value": inbox[:3], "@odata.nextLink":
                                                 "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$skiptoken=p2"})
            if "/mailFolders/sentitems/messages/delta" in url:
                return httpx.Response(200, json={"value": sent, "@odata.deltaLink":
                                                 "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages/delta?$deltatoken=s1"})
            if "/calendarView/delta" in url:
                return httpx.Response(200, json={"value": [event, future], "@odata.deltaLink":
                                                 "https://graph.microsoft.com/v1.0/me/calendarView/delta?$deltatoken=c1"})
            return httpx.Response(404, json={"error": {"message": url}})

        t = httpx.MockTransport(handler)
        r1 = cap.sync_connection(conn_ids[rep], transport=t)
        print("round 1:", r1)

        def acts(uid):
            return {r[0]: r for r in s.execute(text("""SELECT external_id, id, type, direction, summary, body, participants
                                                        FROM activities WHERE owner_user_id = :u"""), {"u": uid}).fetchall()}

        def links(activity_id):
            return {(r[0], r[1]) for r in s.execute(text("SELECT object_type, object_id FROM activity_links WHERE activity_id = :a"),
                                                   {"a": activity_id})}

        s.expire_all()
        a = acts(rep)
        results.append(("round 1 status active", r1.get("status") == "active"))
        results.append(("expired token refreshed + re-sealed", state["refreshes"] >= 1 and
                        crypto.unseal(s.execute(text("SELECT token_encrypted FROM capture_connections WHERE id = :i"),
                                                {"i": conn_ids[rep]}).scalar())["access_token"].startswith("new-")))
        results.append(("paged inbox followed nextLink", r1.get("fetched_inbox") == len(inbox)))
        results.append(("exact contact email → contact + account + deal",
                        "<m1@test>" in a and {("persona", pid), ("account", aid), ("deal", did)} <= links(a["<m1@test>"][1])))
        results.append(("domain only → account link only", "<m2@test>" in a and links(a["<m2@test>"][1]) == {("account", aid)}))
        results.append(("free-mail sender not stored", "<m3@test>" not in a))
        results.append(("internal-only thread not stored", "<m4@test>" not in a))
        results.append(("draft ignored", "<m5@test>" not in a))
        results.append(("excluded domain not stored", "<m6@test>" not in a))
        results.append(("account without access not linked", "<m7@test>" not in a))
        s1 = a.get("<s1@test>")
        results.append(("sent mail stored as outbound", bool(s1) and s1[3] == "outbound"))
        results.append(("personal address blanked in participants",
                        bool(s1) and all(p.get("email") != "friend@gmail.com" for p in s1[6])))
        results.append(("no bodies stored (default)", all(r[5] is None for r in a.values())))
        results.append(("meeting stored with both contacts",
                        "ical-e1" in a and {("persona", pid), ("persona", pid2)} <= links(a["ical-e1"][1])))
        results.append(("future meeting stored", "ical-e2" in a))
        ist = s.execute(text("SELECT status FROM introductions WHERE id = :i"), {"i": iid}).scalar()
        results.append(("past meeting moved intro to meeting_held", ist == "meeting_held"))
        last = s.execute(text("SELECT last_activity_at FROM deals WHERE id = :d"), {"d": did}).scalar()
        results.append(("deal.last_activity_at set", last is not None))

        # colleague's mailbox sees the same message + meeting → stored once
        r2 = cap.sync_connection(conn_ids[rep2], transport=t)
        results.append(("same mail/meeting from colleague deduplicated", r2.get("duplicate", 0) >= 3 and not acts(rep2).get("<m1@test>")))

        # round 2: deltas
        r3 = cap.sync_connection(conn_ids[rep], transport=t)
        print("round 2:", r3)
        s.expire_all()
        a = acts(rep)
        results.append(("delta removal deletes activity", "<m1@test>" not in a and r3.get("removed") == 1))
        subj = s.execute(text("SELECT subject FROM activities WHERE external_id = 'ical-e1' AND owner_user_id = :u"), {"u": rep}).scalar()
        results.append(("delta update renames meeting", subj == "Discovery meeting (renamed)"))
        results.append(("re-sync idempotent (no duplicates)", s.execute(text(
            "SELECT count(*) FROM activities WHERE owner_user_id = :u AND external_id = 'ical-e1'"), {"u": rep}).scalar() == 1))

        # throttling and revoked consent
        state["mode"] = "throttle"
        s.execute(text("UPDATE capture_connections SET cursors = '{}' WHERE id = :i"), {"i": conn_ids[rep]}); s.commit()
        r4 = cap.sync_connection(conn_ids[rep], transport=t)
        nxt = s.execute(text("SELECT next_sync_at FROM capture_connections WHERE id = :i"), {"i": conn_ids[rep]}).scalar()
        results.append(("429 → back off, stay active", r4.get("status") == "active" and nxt > datetime.now(timezone.utc) + timedelta(seconds=100)))
        state["mode"] = "revoked"
        s.execute(text("UPDATE capture_connections SET token_encrypted = :t WHERE id = :i"), {"t": crypto.seal(expired), "i": conn_ids[rep]}); s.commit()
        r5 = cap.sync_connection(conn_ids[rep], transport=t)
        results.append(("revoked consent → needs_reauth", r5.get("status") == "needs_reauth"))

        # OAuth callback: sealed state, code exchange (mocked), connection stored encrypted, bad state rejected
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from apps.sales_crm.capture import api as cap_api, microsoft
        s.execute(text("DELETE FROM capture_connections WHERE user_id = :u"), {"u": rep2}); s.commit()
        orig = (microsoft.exchange_code, microsoft.Graph.me, cap_api.cap_engine.sync_connection)
        microsoft.exchange_code = lambda code, verifier, client=None: {"access_token": "at", "refresh_token": "PLAIN-REFRESH-TOKEN-MARKER", "expires_at": int(time.time()) + 3000,
                                                                      "scope": "Mail.Read Calendars.Read", "_v": verifier}
        microsoft.Graph.me = lambda self: {"mail": mailbox2.upper()}
        cap_api.cap_engine.sync_connection = lambda cid, transport=None: {}
        try:
            app = FastAPI(); app.include_router(cap_api.router)
            tc = TestClient(app)
            good = crypto.seal_text(json.dumps({"u": rep2, "v": "verifier-123", "n": "x"}))
            r = tc.get("/api/crm/capture/callback/microsoft", params={"code": "abc", "state": good}, follow_redirects=False)
            row = s.execute(text("SELECT account_email, token_encrypted, status FROM capture_connections WHERE user_id = :u"), {"u": rep2}).fetchone()
            results.append(("callback stores connection (mailbox lower-cased)", r.status_code == 303 and "result=connected" in r.headers["location"]
                            and row is not None and row[0] == mailbox2.lower() and row[2] == "active"))
            results.append(("tokens stored encrypted, not plain", row is not None and b"PLAIN-REFRESH-TOKEN-MARKER" not in bytes(row[1])
                            and crypto.unseal(row[1])["refresh_token"] == "PLAIN-REFRESH-TOKEN-MARKER"))
            r = tc.get("/api/crm/capture/callback/microsoft", params={"code": "abc", "state": good[:-4] + "AAAA"}, follow_redirects=False)
            results.append(("tampered state rejected", "result=error" in r.headers.get("location", "")))
            from cryptography.fernet import Fernet
            old = crypto._get().encrypt_at_time(json.dumps({"u": rep2, "v": "v", "n": "y"}).encode(), int(time.time()) - 3600).decode()
            r = tc.get("/api/crm/capture/callback/microsoft", params={"code": "abc", "state": old}, follow_redirects=False)
            results.append(("expired state (>10 min) rejected", "result=error" in r.headers.get("location", "")))
            del Fernet
        finally:
            microsoft.exchange_code, microsoft.Graph.me, cap_api.cap_engine.sync_connection = orig
    finally:
        s.rollback()
        if users:
            s.execute(text("DELETE FROM activities WHERE owner_user_id = ANY(:u)"), {"u": users})
        for c in connectors:
            s.execute(text("DELETE FROM introductions WHERE connector_id = :c"), {"c": c})
            s.execute(text("DELETE FROM connectors WHERE id = :c"), {"c": c})
        for d in deals:
            s.execute(text("DELETE FROM deals WHERE id = :d"), {"d": d})
        for u in users:
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
