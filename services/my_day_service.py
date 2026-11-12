"""Command Center · My Day — the rep's action-first view at the top of /command-center.

Built from data already in the app (no AI, no scraping), scoped like the Deals page: sales reps see their own
deals, tasks and introductions; managers and admins see everyone's on the accounts they can access.

  headline   overdue items · deals at risk · late-stage deals gone quiet · fresh signals on accounts with deals
  today      one queue, most urgent first: overdue tasks, overdue deal next steps, quiet deals, missing economic
             buyers, stale introductions — each with the actions that resolve it
  quarter    won / commit / best case / target for the current fiscal quarter (apps/sales_crm/forecast.py)
  attention  the open deals with the lowest health score
  reasons    the newest Command Center signals on accounts where an open deal exists, tagged with those deals
             (when the feed has nothing from the last 7 days, the newest signals from the last 30)
  accounts   open pipeline, deals and economic-buyer gaps per account — the cards below My Day (matrix, coverage,
             signal feed, exec moves) use it to say which deals a signal or a gap affects

Served by GET /api/command-center/my-day (api.py); drawn by frontend/js/modules/command-center/my-day.js.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from sqlalchemy import text

QUIET_DAYS = 14                      # same rule as the forecast's "quiet" flag
LATE_STAGES = ("proposal", "pilot", "contract")
INTRO_WAIT_DAYS = 7                  # a requested introduction this old needs a nudge
QUEUE_LIMIT = 40
REASONS_FALLBACK_DAYS = 30


def _day(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _days_since(v, today: date) -> Optional[int]:
    d = _day(v)
    return (today - d).days if d else None


def _short(name: str) -> str:
    return name.replace("Demo · ", "") if name else name


def _copilot(q: str, deal_id: Optional[int] = None) -> str:
    return f"/copilot?q={quote(q)}" + (f"&deal_id={deal_id}" if deal_id else "")


def _deals(s, user, acl: List[int], mine: bool) -> List[Dict[str, Any]]:
    from apps.sales_deals import api as deals_api
    rows = s.execute(text("""
        SELECT d.*, a.display_name AS account_name, l.lob_name, NULL AS owner_name, NULL AS owner_email
        FROM deals d JOIN accounts a ON a.id = d.account_id LEFT JOIN lobs l ON l.id = d.lob_id
        WHERE d.account_id = ANY(:acl) AND d.stage NOT IN ('won', 'lost')
          AND (CAST(:u AS int) IS NULL OR d.owner_user_id = :u)"""), {"acl": acl, "u": user.id if mine else None}).mappings()
    return [deals_api._deal_payload(s, dict(r)) for r in rows]


def _tasks(s, user, acl: List[int], mine: bool, today: date) -> List[Dict[str, Any]]:
    """Open tasks due by the end of today (overdue first)."""
    rows = s.execute(text("""
        SELECT t.id, t.title, t.priority, t.due_date, t.account_id, a.display_name AS account_name, t.deal_id, d.name AS deal_name,
               d.value_amount AS deal_value, d.stage AS deal_stage
        FROM action_items t JOIN accounts a ON a.id = t.account_id LEFT JOIN deals d ON d.id = t.deal_id
        WHERE t.status IN ('open', 'in_progress') AND t.account_id = ANY(:acl) AND t.due_date IS NOT NULL
          AND t.due_date < :tomorrow
          AND (NOT :mine OR t.assigned_to_id = :u OR (t.assigned_to_id IS NULL AND t.created_by_id = :u))
        ORDER BY t.due_date"""), {"acl": acl, "mine": mine, "u": user.id, "tomorrow": today + timedelta(days=1)}).mappings()
    return [dict(r) for r in rows]


def _intros(s, user, mine: bool) -> List[Dict[str, Any]]:
    import auth
    from apps.sales_crm import introductions as intro_api
    sql = intro_api.BASE_SQL + f" WHERE {intro_api._internal_scope_sql()} AND i.status IN ('requested', 'stale')"
    rows = intro_api._q(s, sql, acl=auth.account_scope(s, user))
    out = []
    for r in rows:
        v = intro_api.internal_view(r)
        if mine and v.get("owner_user_id") not in (user.id, None):
            continue
        if v["status"] == "stale" or (v["status"] == "requested" and (v.get("days_in_status") or 0) >= INTRO_WAIT_DAYS):
            out.append(v)
    return out


def _quarter(s, user, mine: bool, today: date) -> Optional[Dict[str, Any]]:
    try:
        from apps.sales_crm import forecast
        sm = int(forecast._setting(s, "fiscal_year_start_month", 1))
        period = forecast.period_of(today, sm)
        t = forecast.rollup(s, user, period, owner_id=user.id if mine else None)["totals"]
        return {"period": period, "won": t["closed"], "commit_forecast": t["commit_forecast"],
                "best_case_forecast": t["best_case_forecast"], "target": t["target"], "attainment_pct": t["attainment_pct"]}
    except Exception as e:            # the rest of My Day still renders
        print(f"[my-day] quarter unavailable: {e}")
        return None


def _deal_item(d: Dict[str, Any], today: date) -> Optional[Dict[str, Any]]:
    """The single most urgent reason this deal needs the rep today, or None."""
    name, did = _short(d["name"]), d["id"]
    value = d.get("value_amount")
    stage_prog = d.get("stage_progress")
    pilot_exit_text = None
    if d.get("stage") == "pilot" and stage_prog:
        done = stage_prog.get("done", 0)
        total = stage_prog.get("total", 4)
        pct = int((done / total) * 100) if total else 0
        pilot_exit_text = f"{pct}% of pilot exit criteria done ({done} of {total})"

    base = {"deal_id": did, "deal": name, "account": d["account_name"], "account_id": d["account_id"], "stage": d["stage_label"],
            "value": value, "health": (d.get("health") or {}).get("score"), "pilot_exit_text": pilot_exit_text,
            "stage_progress": stage_prog, "expected_close": str(d.get("expected_close")) if d.get("expected_close") else None}
    over = _days_since(d.get("next_step_due"), today)
    quiet = _days_since(d.get("last_customer_touch"), today)
    touch = f"last touch {quiet} days ago" if quiet is not None else "no customer contact logged"

    def draft(q):
        return {"label": "Draft email", "href": _copilot(q, did), "primary": True}
    if d.get("next_step") and over is not None and over > 0:
        return {**base, "kind": "deal_next_step", "title": d["next_step"], "overdue_days": over, "extra": touch,
                "why": f"Deal next step · {over} day{'s' if over != 1 else ''} overdue",
                "actions": [draft(f"Draft a short email to move '{d['next_step']}' forward"),
                            {"label": "Reschedule", "deal_action": "reschedule"}]}
    if d["stage"] in LATE_STAGES and quiet is not None and quiet >= QUIET_DAYS:
        due = _day(d.get("next_step_due"))
        step = f'next step "{d["next_step"]}" due {due.strftime("%d %b")}' if d.get("next_step") and due else "no next step set"
        return {**base, "kind": "deal_quiet", "title": f"Re-engage {d['account_name']} — no contact for {quiet} days", "overdue_days": quiet - QUIET_DAYS,
                "why": "Deal gone quiet", "extra": step,
                "actions": [draft("Draft a short check-in email for this deal"),
                            {"label": "Log call", "deal_action": "log_call"}]}
    if d["stage"] in LATE_STAGES and "economic_buyer" in (d.get("committee_gaps") or []):
        return {**base, "kind": "deal_committee", "title": f"Find the economic buyer at {d['account_name']}", "overdue_days": 0,
                "why": "Committee gap · no economic buyer", "extra": touch,
                "actions": [{"label": "Add contact", "href": f"/deals?deal={did}&tab=committee", "primary": True},
                            {"label": "Who can introduce me?", "href": f"/introductions?new=1&account_id={d['account_id']}"}]}
    if not d.get("next_step"):
        return {**base, "kind": "deal_no_step", "title": f"Set a next step for {name}", "overdue_days": 0, "why": "No next step", "extra": touch,
                "actions": [{"label": "Set next step", "deal_action": "set_step", "primary": True},
                            {"label": "Open deal", "href": f"/deals?deal={did}"}]}
    return None


def _short_gap(g: str, today: date) -> str:
    """Deal-health gap text trimmed for a one-line list ("No champion on the buying committee" → "No champion")."""
    g = g.replace(" on the buying committee", "").replace(" identified", "")
    if g.startswith("Next step overdue since "):
        late = _days_since(g.rsplit(" ", 1)[-1], today)
        return f"next step {late} days late" if late else "next step late"
    return g


def _signal_reasons(s, user, deal_names: Dict[int, List[str]]) -> Tuple[List[Dict[str, Any]], int]:
    """Signals on accounts with open deals: the user's latest feed (last 7 days); when that has none, the newest
    from the last REASONS_FALLBACK_DAYS read straight from the source tables. Returns (reasons, window in days)."""
    def shape(sg):
        return {"kind": sg.get("kind"), "category": sg.get("category"), "account": sg.get("account_name"),
                "account_id": sg.get("account_id"), "title": sg.get("title"), "url": sg.get("url"), "score": sg.get("score"),
                "detected_at": sg.get("detected_at"), "deals": deal_names[sg["account_id"]]}
    snap = s.execute(text("""SELECT signals FROM command_center_snapshots WHERE user_id = :u ORDER BY generated_at DESC LIMIT 1"""),
                     {"u": user.id}).scalar() or []
    reasons = [shape(sg) for sg in snap if sg.get("account_id") in deal_names]
    if reasons or not deal_names:
        return reasons, 7
    try:
        from services import command_center_service as ccs
        now = datetime.now(timezone.utc)
        scored = [ccs.score_signal(r, now) for r in ccs.collect_raw_signals(s, list(deal_names), now) if r.get("detected_at")]
        recent = [sg for sg in scored if ccs._age_days(ccs._parse_date(sg["detected_at"]), now) <= REASONS_FALLBACK_DAYS]
        recent.sort(key=lambda sg: str(sg["detected_at"]), reverse=True)
        return [shape(sg) for sg in recent if sg.get("account_id") in deal_names], REASONS_FALLBACK_DAYS
    except Exception as e:             # the rest of My Day still renders
        print(f"[my-day] signal fallback unavailable: {e}")
        return [], 7


def _accounts(s, acl: List[int], deals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Open pipeline per account the user can see (accounts without deals included, at zero)."""
    by_acct: Dict[int, Dict[str, Any]] = {}
    for a in s.execute(text("SELECT id, display_name FROM accounts WHERE id = ANY(:acl) ORDER BY display_name"), {"acl": acl}):
        by_acct[a[0]] = {"account_id": a[0], "name": a[1], "open_count": 0, "open_value": 0, "eb_gaps": 0, "deals": []}
    for d in deals:
        a = by_acct.get(d["account_id"])
        if not a:
            continue
        a["open_count"] += 1
        a["open_value"] += d.get("value_amount") or 0
        a["eb_gaps"] += "economic_buyer" in (d.get("committee_gaps") or [])
        a["deals"].append({"deal_id": d["id"], "deal": _short(d["name"]), "stage": d["stage_label"], "value": d.get("value_amount")})
    return list(by_acct.values())


def _urgency(item: Dict[str, Any]) -> float:
    weight = {"task": 30, "deal_next_step": 28, "deal_quiet": 22, "intro": 14, "deal_committee": 12, "deal_no_step": 8}[item["kind"]]
    value = (item.get("value") or 0) / 100000          # $100k ≈ one point
    pri = 10 if item.get("priority") == "high" else 0
    return weight + min(item.get("overdue_days") or 0, 30) + min(value, 10) + pri


def build(s, user, today: Optional[date] = None) -> Dict[str, Any]:
    from apps.sales_deals import api as deals_api
    today = today or date.today()
    mine = deals_api.own_deals_only(user)
    acl = deals_api._acl(s, user)
    deals = _deals(s, user, acl, mine)
    tasks = _tasks(s, user, acl, mine, today)
    intros = _intros(s, user, mine)

    queue: List[Dict[str, Any]] = []
    for t in tasks:
        over = _days_since(t["due_date"], today) or 0
        queue.append({"kind": "task", "task_id": t["id"], "title": _short(t["title"]), "overdue_days": max(over, 0), "priority": t["priority"],
                      "why": f"Task · {over} day{'s' if over != 1 else ''} overdue" if over > 0 else "Task · due today",
                      "account": t["account_name"], "account_id": t["account_id"], "deal_id": t["deal_id"],
                      "deal": _short(t["deal_name"]) if t["deal_name"] else None, "value": t["deal_value"],
                      "stage": (t["deal_stage"] or "").replace("_", " ").title() or None,
                      "actions": [{"label": "Mark done", "task_action": "complete", "primary": True},
                                  {"label": "Snooze", "task_action": "snooze"}]})
    for d in deals:
        item = _deal_item(d, today)
        if item:
            queue.append(item)
    for v in intros:
        contact = v.get("persona_name") or (v.get("submitted") or {}).get("contact_name") or "a contact"
        email = (v.get("connector") or {}).get("email")
        nudge = (f"mailto:{email}?subject={quote('Following up: introduction to ' + contact)}" if email
                 else f"/introductions?intro={v['id']}")
        queue.append({"kind": "intro", "intro_id": v["id"], "overdue_days": v.get("days_in_status") or 0,
                      "title": f"Nudge {v['connector']['name']} — intro to {contact} is {'stale' if v['status'] == 'stale' else 'waiting'}",
                      "why": f"Introduction · {v['status_label']} for {v.get('days_in_status') or 0} days",
                      "account": v.get("account_name"), "account_id": v.get("account_id"), "extra": "waiting on the connector",
                      "actions": [{"label": "Nudge", "href": nudge, "primary": True},
                                  {"label": "Mark declined", "intro_action": "decline"}]})
    for q in queue:
        q["urgency"] = round(_urgency(q), 1)
        q["tone"] = "red" if (q.get("overdue_days") or 0) >= 7 and q["kind"] in ("task", "deal_next_step") \
            else "blue" if q["kind"] == "intro" else "amber"
    queue.sort(key=lambda q: -q["urgency"])

    open_value = sum(d.get("value_amount") or 0 for d in deals)
    at_risk = [d for d in deals if (d.get("health") or {}).get("level") == "risk"]
    quiet = [d for d in deals if d["stage"] in LATE_STAGES and (_days_since(d.get("last_customer_touch"), today) or 0) >= QUIET_DAYS]
    overdue_tasks = sum(1 for t in tasks if (_days_since(t["due_date"], today) or 0) > 0)
    overdue_steps = sum(1 for d in deals if d.get("next_step") and (_days_since(d.get("next_step_due"), today) or 0) > 0)

    # Reasons to reach out: Command Center signals on accounts that have an open deal
    deal_names: Dict[int, List[str]] = {}
    for d in deals:
        deal_names.setdefault(d["account_id"], []).append(_short(d["name"]))
    reasons, reasons_days = _signal_reasons(s, user, deal_names)
    refreshed = s.execute(text("SELECT max(generated_at) FROM command_center_snapshots WHERE user_id = :u"), {"u": user.id}).scalar()
    accounts = _accounts(s, acl, deals)

    attention = sorted(deals, key=lambda d: ((d.get("health") or {}).get("score") if (d.get("health") or {}).get("score") is not None else 999))[:3]
    return {
        "scope": "mine" if mine else "team",
        "today": today.isoformat(),
        "account_count": len(accounts),
        "refreshed_at": refreshed.isoformat() if refreshed else None,
        "headline": {
            "overdue": overdue_tasks + overdue_steps, "overdue_tasks": overdue_tasks, "overdue_steps": overdue_steps,
            "at_risk_count": len(at_risk), "at_risk_value": sum(d.get("value_amount") or 0 for d in at_risk),
            "open_count": len(deals), "open_value": open_value,
            "quiet_count": len(quiet), "quiet_value": sum(d.get("value_amount") or 0 for d in quiet),
            "reasons": len(reasons),
        },
        "today_items": queue[:QUEUE_LIMIT],
        "today_total": len(queue),
        "quarter": _quarter(s, user, mine, today),
        "attention": [{"deal_id": d["id"], "deal": _short(d["name"]), "account": d["account_name"], "value": d.get("value_amount"),
                       "health": (d.get("health") or {}).get("score"), "level": (d.get("health") or {}).get("level"),
                       "stage": d.get("stage"), "stage_label": d.get("stage_label"),
                       "pilot_exit_text": f"{int((d.get('stage_progress', {}).get('done', 0) / (d.get('stage_progress', {}).get('total') or 1)) * 100)}% of pilot exit criteria done" if d.get("stage") == "pilot" and d.get("stage_progress") else None,
                       "why": " · ".join(_short_gap(g, today) for g in ((d.get("health") or {}).get("gaps") or [])[:2]).capitalize()} for d in attention],
        "reasons": reasons[:5],
        "reasons_window_days": reasons_days,
        "accounts": accounts,
        "eb_gap_deals": sum(a["eb_gaps"] for a in accounts),
    }
