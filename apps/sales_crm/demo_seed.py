"""Demo data for the whole CRM flow: connectors → introductions → deals → forecast, plus activities.

    python -m apps.sales_crm.demo_seed status     # what demo data exists
    python -m apps.sales_crm.demo_seed seed       # create it (refuses if it already exists)
    python -m apps.sales_crm.demo_seed reset      # remove + seed again (dates are relative to today)
    python -m apps.sales_crm.demo_seed remove     # delete every demo row, nothing else

Every row created is recorded in `crm_demo_rows`, so `remove` deletes exactly the demo data and never
touches real records. Demo deals and activity subjects start with "Demo ·" and connectors are
fictional. Contacts are the real people already in the database (introductions and deals need them),
so the notes describe generic, plausible sales conversations. No AI requests, no emails sent.

What you get (dates relative to today, fiscal quarters from crm_settings):
  • 5 connectors (partner, advisors, employee, customer); one linked to the demo partner login if created
  • 12 introductions covering every status (proposed … converted, declined, stale, one unmatched partner submission)
  • 14 deals across BNY, BlackRock, DTCC, Northern Trust and Vanguard: every stage, won and lost, USD/EUR/GBP,
    all forecast categories, some sourced by introductions (with attribution)
  • buying committees, exit-criteria ticks, stage history, next steps, MEDDICC notes, tasks
  • ~20 activities (meetings, calls, emails, notes, LinkedIn, one transcript with action items, one upcoming meeting)
  • quarterly targets for the reps and each business line (current + next quarter)
  • a forecast snapshot dated this Monday with different values, so "What moved" has content
"""

import json
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from db.connection import get_session

REGISTRY_DDL = """CREATE TABLE IF NOT EXISTS crm_demo_rows (
  table_name text NOT NULL, row_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (table_name, row_id))"""
# Delete order (children first). Rows in other tables cascade from these.
REMOVE_ORDER = ["activities", "forecast_snapshots", "sales_targets", "action_items", "introductions", "deals",
                "connectors", "users"]
NOW = datetime.now(timezone.utc)
TODAY = NOW.date()


def days(n: int) -> datetime:
    return NOW + timedelta(days=n)


class Seeder:
    def __init__(self, s):
        self.s = s
        self.n: Dict[str, int] = {}

    def track(self, table: str, row_id: Any) -> None:
        self.s.execute(text("INSERT INTO crm_demo_rows (table_name, row_id) VALUES (:t, :r) ON CONFLICT DO NOTHING"),
                       {"t": table, "r": str(row_id)})
        self.n[table] = self.n.get(table, 0) + 1

    def q1(self, sql: str, **kw):
        return self.s.execute(text(sql), kw).scalar()

    def rows(self, sql: str, **kw) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.s.execute(text(sql), kw).mappings()]


# ── helpers ───────────────────────────────────────────────────────────────────


def _people(sd: Seeder, account_id: int, n: int) -> List[Dict[str, Any]]:
    return sd.rows("""SELECT id, coalesce(full_name, display_name) AS name, title, email FROM personas
                      WHERE account_id = :a AND coalesce(full_name, display_name) IS NOT NULL
                      ORDER BY hierarchy_level NULLS LAST, (email IS NULL), id LIMIT :n""", a=account_id, n=n)


def _period_of(d: date, start_month: int) -> str:
    months_in = (d.month - start_month) % 12
    fy_start_year = d.year if d.month >= start_month else d.year - 1
    fy = fy_start_year if start_month == 1 else fy_start_year + 1
    return f"FY{fy}-Q{months_in // 3 + 1}"


def _quarter_bounds(start_month: int):
    from apps.sales_crm.forecast import period_bounds
    cur = _period_of(TODAY, start_month)
    a, b = period_bounds(cur, start_month)
    nxt_start = b + timedelta(days=1)
    nxt = _period_of(nxt_start, start_month)
    na, nb = period_bounds(nxt, start_month)
    return cur, a, b, nxt, na, nb


# ── seed ──────────────────────────────────────────────────────────────────────


def seed(with_users: bool = True) -> Dict[str, Any]:
    from apps.sales_crm import activities as acts
    from apps.sales_crm import api as crm
    from apps.sales_deals import api as deals_api
    import auth

    from apps.sales_crm import notify
    notify.suppress()               # seeding never emails anyone
    crm.ensure_schema()
    deals_api.ensure_schema()
    s = get_session()
    sd = Seeder(s)
    out: Dict[str, Any] = {}
    try:
        s.execute(text(REGISTRY_DDL))
        if sd.q1("SELECT count(*) FROM crm_demo_rows"):
            raise SystemExit("Demo data already exists — run `reset` or `remove` first.")

        # People: reps own deals; the super admin owns a couple too.
        reps = sd.rows("SELECT id, coalesce(full_name, email) AS name FROM users WHERE role IN ('user','sales_manager') AND is_active ORDER BY id")
        admin = sd.rows("SELECT id, coalesce(full_name, email) AS name FROM users WHERE role = 'super_admin' AND is_active ORDER BY id LIMIT 1")
        owners = (reps + admin) or admin
        if not owners:
            raise SystemExit("No active users to own demo deals.")
        o = lambda i: owners[i % len(owners)]["id"]          # noqa: E731

        bl = {r["key"]: r["id"] for r in sd.rows("SELECT id, key FROM business_lines")}
        acct = {r["name"]: r["id"] for r in sd.rows("SELECT id, display_name AS name FROM accounts")}
        need = ["BNY", "BlackRock", "Depository Trust & Clearing"]
        missing = [a for a in need if a not in acct]
        if missing:
            raise SystemExit(f"Accounts missing for the demo: {missing}")
        bny, blk, dtcc = acct["BNY"], acct["BlackRock"], acct["Depository Trust & Clearing"]
        nt, vg = acct.get("Northern Trust"), acct.get("The Vanguard Group")
        P = {"bny": _people(sd, bny, 8), "blk": _people(sd, blk, 8), "dtcc": _people(sd, dtcc, 6)}
        start_month = int(sd.q1("SELECT value FROM crm_settings WHERE key = 'fiscal_year_start_month'") or 1)
        cur_q, q_start, q_end, next_q, nq_start, nq_end = _quarter_bounds(start_month)
        # close dates inside the current / next quarter, relative to today
        in_q = lambda frac: q_start + timedelta(days=int((q_end - q_start).days * frac))        # noqa: E731
        in_nq = lambda frac: nq_start + timedelta(days=int((nq_end - nq_start).days * frac))    # noqa: E731
        soon = lambda n: min(max(TODAY + timedelta(days=n), q_start), q_end)                    # noqa: E731

        # ── optional demo partner login (so /partner can be tried) ──
        partner_uid, partner_pw = None, None
        if with_users:
            partner_pw = secrets.token_urlsafe(10)
            partner_uid = sd.q1("""INSERT INTO users (email, hashed_password, full_name, role, is_active, has_dashboard_access,
                                   has_command_center_access, has_tasks_access, has_pipeline_access, failed_login_count)
                                   VALUES ('demo.partner@example.com', :h, 'Priya Nair (demo partner)', 'partner', true,
                                           false, false, false, false, 0)
                                   ON CONFLICT (email) DO NOTHING RETURNING id""", h=auth.hash_password(partner_pw))
            if partner_uid:
                sd.track("users", partner_uid)
            else:
                partner_pw = None

        # ── connectors ──
        C = {}
        for key, kind, name, org, pct, uid in [
            ("northbridge", "partner", "Priya Nair", "Northbridge Advisory Partners", 50, partner_uid),
            ("meridian", "advisor", "James Whitfield", "Meridian Capital Advisors (ex-CIO)", 40, None),
            ("hollis", "advisor", "Dr. Elena Hollis", "Independent board advisor", 30, None),
            ("employee", "employee", "Rahul Mehta", "StradIT delivery lead", 10, None),
            ("customer", "customer", "Sarah Collins", "Reference customer — Atlas Bank", 20, None),
        ]:
            cid = sd.q1("""INSERT INTO connectors (kind, name, organisation, default_attribution_pct, user_id, notes, created_by)
                           VALUES (:k, :n, :o, :p, :u, 'Demo data', :by) RETURNING id""",
                        k=kind, n=name, o=org, p=pct, u=uid, by=owners[0]["id"])
            sd.track("connectors", cid)
            C[key] = cid

        # ── deals ──
        def deal(name, account_id, owner, stage, value, currency="USD", close=None, category="pipeline", prob=None,
                 bline=None, next_step=None, due=None, source="outbound", created_ago=40, stage_ago=10, qualification=None):
            did = sd.q1("""
                INSERT INTO deals (account_id, name, owner_user_id, stage, offerings, value_amount, currency, expected_close,
                                   next_step, next_step_due, business_line_id, forecast_category, probability, source,
                                   qualification, created_at, stage_changed_at, closed_at, lost_reason)
                VALUES (:a, :n, :o, :st, :off, :v, :c, :ec, :ns, :nd, :bl, :fc, :pr, :src, CAST(:qual AS jsonb),
                        :created, :changed, :closed, :lost)
                RETURNING id""",
                a=account_id, n=f"Demo · {name}", o=owner, st=stage, off=["ai", "data"] if "AI" in name else ["data", "cloud"],
                v=value, c=currency, ec=close, ns=next_step, nd=due, bl=bline, fc=category, pr=prob, src=source,
                qual=json.dumps(qualification or {}), created=days(-created_ago), changed=days(-stage_ago),
                closed=days(-stage_ago) if stage in ("won", "lost") else None,
                lost="Chose incumbent vendor on price" if stage == "lost" else None)
            sd.track("deals", did)
            deals_api._seed_checklist(s, did)
            order = ["intro", "discovery", "proposal", "pilot", "contract"]
            path = order[: (order.index(stage) + 1 if stage in order else len(order))] + ([stage] if stage in ("won", "lost") else [])
            t0 = days(-created_ago)
            step = max(1, (created_ago - stage_ago) // max(1, len(path) - 1)) if len(path) > 1 else 0
            prev = None
            for i, st in enumerate(path):
                at = t0 + timedelta(days=step * i) if i < len(path) - 1 else days(-stage_ago)
                s.execute(text("""INSERT INTO deal_stage_history (deal_id, from_stage, to_stage, changed_by, changed_at)
                                  VALUES (:d, :f, :t, :u, :at)"""), {"d": did, "f": prev, "t": st, "u": owner, "at": at})
                prev = st
            # tick the exit criteria of every stage already passed
            done_stages = path[:-1] if stage not in ("won", "lost") else order
            s.execute(text("""UPDATE deal_checklist SET done = true, done_at = :at, done_by = :u, note = 'Demo'
                              WHERE deal_id = :d AND stage = ANY(:st)"""), {"at": days(-stage_ago), "u": owner, "d": did, "st": done_stages})
            s.execute(text("""INSERT INTO deal_activity (deal_id, user_id, kind, text, created_at)
                              VALUES (:d, :u, 'created', 'Deal created (demo data)', :at)"""), {"d": did, "u": owner, "at": t0})
            return did

        def committee(did, people, roles):
            for p, (role, sentiment) in zip(people, roles):
                s.execute(text("""INSERT INTO deal_stakeholders (deal_id, persona_id, role, sentiment) VALUES (:d, :p, :r, :s)
                                  ON CONFLICT DO NOTHING"""), {"d": did, "p": p["id"], "r": role, "s": sentiment})
            deals_api._sync_auto_checks(s, did, None)

        fs, fed, trn = bl.get("fs"), bl.get("federal"), bl.get("training")
        D = {}
        D["bny_ai"] = deal("BNY — Applied AI governance pilot", bny, o(0), "pilot", 320000, close=in_q(0.85), category="commit",
                           bline=fs, next_step="Pilot results readout with CIO", due=soon(5), source="introduction",
                           created_ago=75, stage_ago=12,
                           qualification={"metrics": "Cut model validation from 6 weeks to 1", "economic_buyer": "CIO",
                                          "decision_process": "Pilot → steering committee → procurement",
                                          "identify_pain": "Manual model validation and AI governance gaps"})
        committee(D["bny_ai"], P["bny"][:4], [("economic_buyer", "positive"), ("champion", "positive"),
                                              ("technical_evaluator", "neutral"), ("influencer", "neutral")])
        D["bny_data"] = deal("BNY — Fund accounting data quality", bny, o(1), "proposal", 180000, close=in_q(0.95),
                             category="best_case", bline=fs, next_step="Send revised proposal with phased pricing", due=soon(3),
                             created_ago=50, stage_ago=9)
        committee(D["bny_data"], P["bny"][4:6], [("champion", "positive"), ("user", "neutral")])
        D["bny_cyber"] = deal("BNY — Cyber resilience assessment", bny, o(0), "discovery", 95000, close=in_nq(0.4),
                              bline=fs, next_step="Discovery workshop with CISO team", due=soon(8), created_ago=20, stage_ago=6)
        committee(D["bny_cyber"], P["bny"][6:7], [("influencer", "neutral")])
        D["bny_train"] = deal("BNY — GenAI enablement training", bny, o(1), "won", 60000, close=in_q(0.5), bline=trn,
                              source="introduction", created_ago=70, stage_ago=18)
        committee(D["bny_train"], P["bny"][1:2], [("economic_buyer", "positive")])

        D["blk_ai"] = deal("BlackRock — AI agent testing framework", blk, o(2), "contract", 450000, currency="USD",
                           close=in_q(0.9), category="commit", prob=90, bline=fs, next_step="Legal redlines on MSA", due=soon(4),
                           created_ago=95, stage_ago=8,
                           qualification={"metrics": "Regression suite runtime -70%", "champion": "Head of QA Engineering",
                                          "competition": "In-house tooling, a Big-4 firm"})
        committee(D["blk_ai"], P["blk"][:4], [("economic_buyer", "positive"), ("champion", "positive"),
                                              ("technical_evaluator", "positive"), ("blocker", "negative")])
        D["blk_cloud"] = deal("BlackRock — Cloud cost optimisation (EMEA)", blk, o(0), "proposal", 210000, currency="EUR",
                              close=in_nq(0.3), category="best_case", bline=fs, next_step="Workshop with EMEA infra leads",
                              due=soon(10), source="introduction", created_ago=45, stage_ago=14)
        committee(D["blk_cloud"], P["blk"][4:6], [("champion", "neutral"), ("influencer", "neutral")])
        D["blk_data"] = deal("BlackRock — Aladdin data integration", blk, o(1), "intro", 150000, close=in_nq(0.7),
                             bline=fs, created_ago=6, stage_ago=6)
        D["blk_lost"] = deal("BlackRock — Legacy test migration", blk, o(2), "lost", 120000, close=in_q(0.4), bline=fs,
                             created_ago=80, stage_ago=25)

        D["dtcc_ai"] = deal("DTCC — AI-assisted reconciliation", dtcc, o(0), "discovery", 275000, currency="USD",
                            close=in_q(0.97), category="pipeline", bline=fs, next_step="Map reconciliation exceptions with ops",
                            due=days(-3).date(), created_ago=35, stage_ago=33)          # overdue + stuck → shows in digest
        committee(D["dtcc_ai"], P["dtcc"][:2], [("champion", "neutral"), ("user", "neutral")])
        D["dtcc_digital"] = deal("DTCC — Digital assets settlement pilot", dtcc, o(1), "pilot", 380000, currency="GBP",
                                 close=in_nq(0.5), category="best_case", bline=fs, next_step="Agree pilot success criteria",
                                 due=soon(6), source="introduction", created_ago=60, stage_ago=11)
        committee(D["dtcc_digital"], P["dtcc"][2:5], [("economic_buyer", "neutral"), ("champion", "positive"),
                                                      ("technical_evaluator", "neutral")])
        D["dtcc_won"] = deal("DTCC — Data lineage accelerator", dtcc, o(2), "won", 140000, close=in_q(0.3), bline=fs,
                             created_ago=90, stage_ago=30)

        if nt:
            D["nt_fed"] = deal("Northern Trust — Secure cloud landing zone", nt, o(0), "proposal", 230000, close=in_nq(0.6),
                               category="pipeline", bline=fed, next_step="Security review questionnaire", due=soon(12),
                               created_ago=30, stage_ago=7)
        if vg:
            D["vg_train"] = deal("Vanguard — Data literacy programme", vg, o(1), "intro", 45000, close=None, bline=trn,
                                 created_ago=4, stage_ago=4)       # no close date → "undated" list in the forecast
            D["vg_omit"] = deal("Vanguard — Analytics platform refresh", vg, o(2), "discovery", 90000, close=in_q(0.8),
                                category="omitted", bline=fs, created_ago=40, stage_ago=20)

        # ── introductions ──
        def intro(connector, account_id, persona, status, *, bline=fs, owner=None, context="", deal_id=None, pct=None,
                  ago=20, submitted=None, reason=None):
            steps = ["proposed", "requested", "accepted", "intro_made", "meeting_held", "converted"]
            ts_col = {"requested": "requested_at", "accepted": "accepted_at", "intro_made": "intro_made_at",
                      "meeting_held": "meeting_at", "converted": "converted_at"}
            path = steps[: steps.index(status) + 1] if status in steps else ["proposed", "requested", status]
            t0 = days(-ago)
            stamps = {ts_col[st]: t0 + timedelta(days=i * max(1, ago // max(1, len(path)))) for i, st in enumerate(path) if st in ts_col}
            updated = days(-min(ago, 35)) if status == "stale" else days(-max(1, ago // max(1, len(path))))
            iid = sd.q1(f"""
                INSERT INTO introductions (connector_id, account_id, persona_id, business_line_id, owner_user_id, status, context,
                                           deal_id, attribution_pct, closed_reason, submitted_account_name, submitted_contact_name,
                                           submitted_contact_email, created_by, created_at, updated_at
                                           {''.join(', ' + k for k in stamps)})
                VALUES (:c, :a, :p, :bl, :o, :st, :ctx, :d, :pct, :why, :san, :scn, :sce, :by, :t0, :upd
                        {''.join(', :' + k for k in stamps)})
                RETURNING id""",
                c=connector, a=account_id, p=persona["id"] if persona else None, bl=bline, o=owner, st=status, ctx=context,
                d=deal_id, pct=pct, why=reason, san=(submitted or {}).get("account"), scn=(submitted or {}).get("contact"),
                sce=(submitted or {}).get("email"), by=partner_uid if submitted and partner_uid else (owner or owners[0]["id"]),
                t0=t0, upd=updated, **stamps)
            sd.track("introductions", iid)
            prev = None
            for i, st in enumerate(path):
                s.execute(text("""INSERT INTO introduction_events (introduction_id, kind, from_status, to_status, note, by_user, at)
                                  VALUES (:i, :k, :f, :t, :n, :u, :at)"""),
                          {"i": iid, "k": "created" if i == 0 else ("converted" if st == "converted" else "status"), "f": prev, "t": st,
                           "n": "Submitted by partner" if (i == 0 and submitted) else (reason if st == "declined" else None),
                           "u": owner or owners[0]["id"], "at": t0 + timedelta(days=i * max(1, ago // max(1, len(path))))})
                prev = st
            if deal_id:
                s.execute(text("UPDATE deals SET introduction_id = :i, source = 'introduction' WHERE id = :d"), {"i": iid, "d": deal_id})
            return iid

        I = {}
        I["bny_ai"] = intro(C["meridian"], bny, P["bny"][0], "converted", owner=o(0), deal_id=D["bny_ai"], pct=40, ago=80,
                            context="James sat on the BNY technology advisory panel and introduced us to the CIO office about AI governance.")
        I["bny_train"] = intro(C["employee"], bny, P["bny"][1], "converted", bline=trn, owner=o(1), deal_id=D["bny_train"], pct=10,
                               ago=72, context="Rahul met their L&D lead at a client workshop; they want GenAI enablement for analysts.")
        I["blk_cloud"] = intro(C["northbridge"], blk, P["blk"][4], "converted", owner=o(0), deal_id=D["blk_cloud"], pct=50, ago=50,
                               context="Priya's firm runs BlackRock's EMEA infra vendor panel; cloud spend is a board-level topic.")
        I["dtcc_digital"] = intro(C["hollis"], dtcc, P["dtcc"][3], "converted", owner=o(1), deal_id=D["dtcc_digital"], pct=30, ago=65,
                                  context="Elena advises DTCC's digital assets committee; they are evaluating settlement pilots.")
        I["bny_meet"] = intro(C["customer"], bny, P["bny"][2], "meeting_held", owner=o(0), ago=18,
                              context="Sarah (Atlas Bank) offered to introduce us after our data-quality project went live there.")
        I["blk_made"] = intro(C["northbridge"], blk, P["blk"][5], "intro_made", owner=o(2), ago=12,
                              context="Warm email intro sent by Priya; contact leads model risk for iShares.")
        I["blk_acc"] = intro(C["meridian"], blk, P["blk"][6], "accepted", owner=o(0), ago=9,
                             context="Contact agreed to a 30-minute call about testing AI agents.")
        I["dtcc_req"] = intro(C["hollis"], dtcc, P["dtcc"][5] if len(P["dtcc"]) > 5 else P["dtcc"][0], "requested", owner=o(1), ago=5,
                              context="Asked Elena to introduce us to the head of clearing operations.")
        I["bny_prop"] = intro(C["northbridge"], bny, P["bny"][7] if len(P["bny"]) > 7 else P["bny"][3], "proposed", owner=None, ago=2,
                              context="Priya suggested this contact at a partner event; not yet actioned.")
        I["blk_decl"] = intro(C["customer"], blk, P["blk"][7] if len(P["blk"]) > 7 else P["blk"][1], "declined", owner=o(2), ago=40,
                              reason="Contact moved to a non-technology role", context="Offered intro to a former project sponsor.")
        I["dtcc_stale"] = intro(C["meridian"], dtcc, P["dtcc"][1], "stale", owner=o(0), ago=60,
                                context="Intro requested but no reply after several follow-ups.")
        I["triage"] = intro(C["northbridge"], None, None, "proposed", owner=None, ago=1,
                            submitted={"account": "Fidelity Investments", "contact": "Morgan Lee", "email": None},
                            context="Priya knows their new Chief Data Officer from a previous role and can make a warm intro.")
        # partner-visible note on one intro
        s.execute(text("""INSERT INTO introduction_events (introduction_id, kind, note, partner_visible, by_user, at)
                          VALUES (:i, 'note', 'Thanks Priya — the workshop is booked for next week.', true, :u, :at)"""),
                  {"i": I["blk_cloud"], "u": o(0), "at": days(-20)})

        # ── activities (timeline, deal health, copilot) ──
        def activity(typ, subject, summary, when, owner, links, direction=None, duration=None, meta=None, body=None, participants=None):
            aid = sd.q1("""INSERT INTO activities (type, direction, subject, summary, body, occurred_at, duration_min, owner_user_id,
                                                   source, visibility, participants, metadata)
                           VALUES (:t, :d, :su, :sm, :b, :o, :du, :u, 'manual', 'team', CAST(:p AS jsonb), CAST(:m AS jsonb)) RETURNING id""",
                        t=typ, d=direction, su=f"Demo · {subject}", sm=summary, b=body, o=when, du=duration, u=owner,
                        p=json.dumps(participants or []), m=json.dumps({"demo": True, **(meta or {})}))
            sd.track("activities", aid)
            acts._add_links(s, aid, links)
            acts._after_write(s, aid, owner)
            return aid

        def L(*pairs):
            return [(t, i, "manual") for t, i in pairs]

        bp, kp, dp = P["bny"], P["blk"], P["dtcc"]
        activity("meeting", "Discovery: AI governance priorities", "CIO office confirmed model validation takes ~6 weeks per model; "
                 "wants a governance framework before scaling GenAI. Budget exists for a pilot this quarter.",
                 days(-60), o(0), L(("deal", D["bny_ai"]), ("persona", bp[0]["id"]), ("introduction", I["bny_ai"])), duration=45)
        activity("email", "Pilot scope and success criteria", "Sent pilot scope: 3 models, validation time and audit trail as success metrics.",
                 days(-40), o(0), L(("deal", D["bny_ai"]), ("persona", bp[1]["id"])), direction="outbound")
        activity("call", "Pilot mid-point check-in", "Two of three models through the new validation flow; champion happy with audit trail.",
                 days(-9), o(0), L(("deal", D["bny_ai"]), ("persona", bp[1]["id"])), direction="outbound", duration=30)
        transcript = (f"{bp[0]['name']}: The pilot results look promising, validation dropped from six weeks to about eight days.\n"
                      f"Rep: That's in line with what we saw elsewhere. What would you need to take this to production?\n"
                      f"{bp[0]['name']}: Security approval and a budget decision from our CFO before the end of the quarter.\n"
                      f"Rep: I will send over the security pack and a phased pricing proposal by Friday.\n"
                      f"{bp[1]['name']}: Let's schedule a readout with the steering committee next week.")
        activity("transcript", "Pilot readout prep — transcript", f"- {bp[0]['name']}: validation dropped from six weeks to about eight days.\n"
                 f"- {bp[0]['name']}: needs security approval and a CFO budget decision before quarter end.",
                 days(-3), o(0), L(("deal", D["bny_ai"]), ("persona", bp[0]["id"]), ("persona", bp[1]["id"])), duration=35, body=transcript,
                 meta={"file_name": "pilot-readout-prep.vtt", "action_items": [
                     {"speaker": "Rep", "text": "I will send over the security pack and a phased pricing proposal by Friday."},
                     {"speaker": bp[1]["name"], "text": "Let's schedule a readout with the steering committee next week."}],
                     "speakers": [{"name": bp[0]["name"], "share_pct": 45, "persona_id": bp[0]["id"], "is_internal": False},
                                  {"name": "Rep", "share_pct": 35, "is_internal": True},
                                  {"name": bp[1]["name"], "share_pct": 20, "persona_id": bp[1]["id"], "is_internal": False}]})
        activity("meeting", "Data quality walkthrough", "Walked through reconciliation break reports; they want phased pricing.",
                 days(-12), o(1), L(("deal", D["bny_data"]), ("persona", bp[4]["id"])), duration=60)
        activity("email", "Revised proposal questions", "Procurement asked for a per-fund pricing option.", days(-4), o(1),
                 L(("deal", D["bny_data"]), ("persona", bp[5]["id"])), direction="inbound")
        activity("meeting", "Intro call via Atlas Bank reference", "Good first call; interested in the data-quality case study.",
                 days(-6), o(0), L(("persona", bp[2]["id"]), ("introduction", I["bny_meet"])), duration=30)
        activity("linkedin", "Connection accepted", "Accepted connection and replied to the CISO resilience post.", days(-7), o(0),
                 L(("deal", D["bny_cyber"]), ("persona", bp[6]["id"])), direction="outbound")
        activity("meeting", "Enablement programme kickoff", "Kickoff done for the GenAI training cohort (40 analysts).", days(-15), o(1),
                 L(("deal", D["bny_train"]), ("persona", bp[1]["id"])), duration=60)

        activity("meeting", "Steering committee: framework demo", "Demoed agent regression suite; economic buyer asked for MSA redlines.",
                 days(-10), o(2), L(("deal", D["blk_ai"]), ("persona", kp[0]["id"]), ("persona", kp[1]["id"])), duration=60)
        activity("email", "MSA redlines received", "Legal returned redlines on liability cap and data residency.", days(-2), o(2),
                 L(("deal", D["blk_ai"]), ("persona", kp[1]["id"])), direction="inbound")
        activity("note", "Blocker concern", "Platform lead worried about overlap with in-house tooling; plan a joint architecture session.",
                 days(-5), o(2), L(("deal", D["blk_ai"]), ("persona", kp[3]["id"])))
        activity("call", "EMEA cloud spend discovery", "Cloud bill grew 38% YoY; FinOps team is two people.", days(-25), o(0),
                 L(("deal", D["blk_cloud"]), ("persona", kp[4]["id"])), direction="outbound", duration=40)
        activity("meeting", "Intro meeting (Northbridge)", "First meeting after Priya's intro; model risk team open to a demo.",
                 days(-4), o(2), L(("persona", kp[5]["id"]), ("introduction", I["blk_made"])), duration=30)
        activity("email", "Aladdin integration question", "Inbound question about integrating Aladdin exports with the data platform.",
                 days(-5), o(1), L(("deal", D["blk_data"])), direction="inbound")

        activity("meeting", "Reconciliation exceptions review", "Mapped top 5 exception types; ops want a proof of value.",
                 days(-34), o(0), L(("deal", D["dtcc_ai"]), ("persona", dp[0]["id"])), duration=45)
        activity("meeting", "Digital assets settlement workshop", "Agreed pilot scope: tokenised collateral settlement in a sandbox.",
                 days(-14), o(1), L(("deal", D["dtcc_digital"]), ("persona", dp[2]["id"]), ("persona", dp[3]["id"])), duration=90)
        activity("email", "Pilot success criteria draft", "Sent draft success criteria: settlement time, reconciliation breaks, audit.",
                 days(-8), o(1), L(("deal", D["dtcc_digital"]), ("persona", dp[3]["id"])), direction="outbound")
        activity("task_done", "Data lineage go-live", "Accelerator live in production; lineage coverage 92% of critical reports.",
                 days(-30), o(2), L(("deal", D["dtcc_won"])))
        if nt:
            activity("call", "Security questionnaire walkthrough", "Walked through 120-question security review.", days(-6), o(0),
                     L(("deal", D["nt_fed"])), direction="outbound", duration=45)
        # an upcoming meeting (shows in the future on timelines; doesn't move intros)
        activity("meeting", "Steering committee readout", "Pilot results and production proposal.", days(6), o(0),
                 L(("deal", D["bny_ai"]), ("persona", bp[0]["id"])), duration=60)

        # a couple of deal tasks
        for did, title, due, prio in [(D["bny_ai"], "Send security pack and phased pricing", soon(2), "high"),
                                      (D["blk_ai"], "Return MSA redlines to BlackRock legal", soon(3), "high"),
                                      (D["dtcc_ai"], "Book proof-of-value scoping session", days(-2).date(), "medium")]:
            owner_id = sd.q1("SELECT owner_user_id FROM deals WHERE id = :d", d=did)
            acc = sd.q1("SELECT account_id FROM deals WHERE id = :d", d=did)
            tid = sd.q1("""INSERT INTO action_items (account_id, title, status, priority, due_date, assigned_to_id, created_by_id, source,
                                                     deal_id, created_at, updated_at)
                           VALUES (:a, :t, 'open', :p, :due, :u, :u, 'manual', :d, now(), now()) RETURNING id""",
                        a=acc, t=f"Demo · {title}", p=prio, due=datetime.combine(due, datetime.min.time(), timezone.utc), u=owner_id, d=did)
            sd.track("action_items", tid)

        # ── targets (current + next quarter), skipping any real target already set ──
        for per, mult in ((cur_q, 1.0), (next_q, 1.15)):
            for i, u in enumerate(owners[:3]):
                if not sd.q1("SELECT 1 FROM sales_targets WHERE period = :p AND user_id = :u", p=per, u=u["id"]):
                    tid = sd.q1("""INSERT INTO sales_targets (period, user_id, amount_usd, updated_by) VALUES (:p, :u, :a, :u) RETURNING id""",
                                p=per, u=u["id"], a=round([600000, 450000, 500000][i % 3] * mult, -3))
                    sd.track("sales_targets", tid)
            for key, amt in (("fs", 1500000), ("federal", 300000), ("training", 150000)):
                if bl.get(key) and not sd.q1("SELECT 1 FROM sales_targets WHERE period = :p AND business_line_id = :b", p=per, b=bl[key]):
                    tid = sd.q1("""INSERT INTO sales_targets (period, business_line_id, amount_usd, updated_by) VALUES (:p, :b, :a, :u) RETURNING id""",
                                p=per, b=bl[key], a=round(amt * mult, -3), u=owners[0]["id"])
                    sd.track("sales_targets", tid)

        # ── forecast snapshot at the start of this week (the weekly job then treats the week as done), with
        #    differences so "What moved" has content. If today is Monday, use last Monday. ──
        snap_day = TODAY - timedelta(days=TODAY.weekday() or 7)
        cur_rows = sd.rows("""SELECT id, name, account_id, owner_user_id, business_line_id, stage, forecast_category, amount_usd,
                                     probability, expected_close FROM deals WHERE id = ANY(:ids)""", ids=list(D.values()))
        changes = {D["bny_ai"]: {"stage": "proposal", "forecast_category": "best_case"},           # advanced + category
                   D["bny_data"]: {"amount_usd": 150000},                                          # amount up
                   D["blk_ai"]: {"stage": "pilot"},                                                # advanced
                   D["dtcc_digital"]: {"expected_close": in_q(0.9)},                               # slipped out of quarter
                   D["bny_train"]: {"stage": "contract", "forecast_category": "commit"}}           # won since
        new_since = {D["blk_data"]} | ({D["vg_train"]} if "vg_train" in D else set())
        for r in cur_rows:
            if r["id"] in new_since:
                continue
            r.update(changes.get(r["id"], {}))
            s.execute(text("""INSERT INTO forecast_snapshots (snapshot_date, deal_id, name, account_id, owner_user_id, business_line_id,
                                                             stage, forecast_category, amount_usd, probability, expected_close)
                              VALUES (:sd, :id, :name, :account_id, :owner_user_id, :business_line_id, :stage, :forecast_category,
                                      :amount_usd, :probability, :expected_close)
                              ON CONFLICT (snapshot_date, deal_id) DO UPDATE SET stage = EXCLUDED.stage,
                                forecast_category = EXCLUDED.forecast_category, amount_usd = EXCLUDED.amount_usd,
                                expected_close = EXCLUDED.expected_close"""), {"sd": snap_day, **r})
            sd.track("forecast_snapshots", f"{snap_day}:{r['id']}")

        s.commit()
        out = {"created": sd.n, "current_quarter": cur_q, "next_quarter": next_q}
        if partner_pw:
            out["partner_login"] = {"email": "demo.partner@example.com", "password": partner_pw,
                                    "note": "Shown once. Log in at /login to see /partner (Northbridge Advisory Partners)."}
        return out
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


# ── remove / status ───────────────────────────────────────────────────────────


def remove() -> Dict[str, int]:
    s = get_session()
    removed: Dict[str, int] = {}
    try:
        s.execute(text(REGISTRY_DDL))
        for table in REMOVE_ORDER:
            ids = [r[0] for r in s.execute(text("SELECT row_id FROM crm_demo_rows WHERE table_name = :t"), {"t": table})]
            if not ids:
                continue
            if table == "forecast_snapshots":
                n = 0
                for rid in ids:
                    d, deal_id = rid.split(":")
                    n += s.execute(text("DELETE FROM forecast_snapshots WHERE snapshot_date = :d AND deal_id = :i"),
                                   {"d": d, "i": int(deal_id)}).rowcount
            elif table == "users":
                s.execute(text("DELETE FROM refresh_tokens WHERE user_id = ANY(:i)"), {"i": [int(x) for x in ids]})
                s.execute(text("UPDATE connectors SET user_id = NULL WHERE user_id = ANY(:i)"), {"i": [int(x) for x in ids]})
                n = s.execute(text("DELETE FROM users WHERE id = ANY(:i)"), {"i": [int(x) for x in ids]}).rowcount
            else:
                if table == "deals":       # snapshots of demo deals taken by the weekly job are demo data too
                    s.execute(text("DELETE FROM forecast_snapshots WHERE deal_id = ANY(:i)"), {"i": [int(x) for x in ids]})
                n = s.execute(text(f"DELETE FROM {table} WHERE id = ANY(:i)"), {"i": [int(x) for x in ids]}).rowcount
            removed[table] = n
            s.execute(text("DELETE FROM crm_demo_rows WHERE table_name = :t"), {"t": table})
        s.commit()
        return removed
    finally:
        s.close()


def status() -> Dict[str, int]:
    s = get_session()
    try:
        s.execute(text(REGISTRY_DDL))
        s.commit()
        return {r[0]: r[1] for r in s.execute(text("SELECT table_name, count(*) FROM crm_demo_rows GROUP BY 1 ORDER BY 1"))}
    finally:
        s.close()


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    cmd = (argv or sys.argv[1:] or ["status"])[0]
    no_users = "--no-users" in (argv or sys.argv[1:])
    if cmd == "status":
        st = status()
        print("Demo rows:", st or "none")
    elif cmd == "seed":
        print(json.dumps(seed(with_users=not no_users), indent=2, default=str))
    elif cmd == "remove":
        print("Removed:", remove())
    elif cmd == "reset":
        print("Removed:", remove())
        print(json.dumps(seed(with_users=not no_users), indent=2, default=str))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
