"""Deals pipeline API (apps/sales_copilot/README.md §21). Mounted by the main api.py.

Access: a deal is visible/editable to anyone who can open its account (same rule
as the rest of the app — team selling); every change is written to deal_activity.
Nothing here spends LLM quota: health, gaps and nudges are computed from data.
"""

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from apps.sales_copilot import privacy
from apps.sales_crm import permissions
from apps.sales_crm.forecast import stage_probability
from db.connection import engine, get_session
from db.models.user import User

router = APIRouter(prefix="/api/deals", tags=["Deals pipeline"])

STAGES = ["intro", "discovery", "proposal", "pilot", "contract"]
CLOSED = ["won", "lost"]
# A deal can only be marked won once there is a proposal on the table.
WON_FROM = ["proposal", "pilot", "contract"]
STAGE_LABEL = {"intro": "Intro", "discovery": "Discovery", "proposal": "Proposal", "pilot": "Pilot",
               "contract": "Contract", "won": "Won", "lost": "Lost"}
# Why a deal was lost (migrations/versions/0002). A pick-list so losses can be analysed; the
# free-text lost_reason stays as the note. Keep in step with the CHECK constraint in that migration.
LOST_CATEGORIES = {"price": "Price", "timing": "Timing", "competitor": "Lost to a competitor",
                   "no_decision": "No decision", "no_budget": "No budget", "product_fit": "Product fit",
                   "relationship": "Relationship / trust", "other": "Other"}
ROLES = ["champion", "economic_buyer", "technical_evaluator", "influencer", "blocker", "user"]
OFFERINGS = {"ai": "Applied AI", "data": "Data Analytics", "cyber": "Cybersecurity", "cloud": "Cloud & Infrastructure",
             "testing": "Automated AI Testing", "digital_assets": "Digital Assets & Blockchain"}

# README §21.2 — default exit criteria per stage (key, label)
CHECKLIST: Dict[str, List[tuple]] = {
    "intro": [("target_identified", "Target stakeholder identified"),
              ("trigger_noted", "Trigger event noted (why now)"),
              ("meeting_booked", "First meeting booked")],
    "discovery": [("pains_confirmed", "At least 2 pains confirmed"),
                  ("economic_buyer", "Economic buyer identified"),
                  ("champion", "Champion identified"),
                  ("decision_process", "Decision process and timeline noted")],
    "proposal": [("offerings_mapped", "Offering(s) mapped to their priorities"),
                 ("business_case", "Business case drafted"),
                 ("competition", "Competition identified"),
                 ("proposal_sent", "Proposal sent")],
    "pilot": [("success_criteria", "Success criteria agreed"),
              ("pilot_dates", "Pilot start and end dates set"),
              ("checkin_cadence", "Weekly check-in cadence agreed"),
              ("pilot_result", "Pilot result recorded")],
    "contract": [("approval_chain", "Approval chain confirmed"),
                 ("security_review", "Security / procurement review done"),
                 ("contract_sent", "Contract sent"),
                 ("signed", "Signed")],
}
AUTO_CHECK = {("intro", "target_identified"): "has_stakeholder",      # ticked automatically from the committee
              ("discovery", "economic_buyer"): "economic_buyer",
              ("discovery", "champion"): "champion",
              ("proposal", "offerings_mapped"): "has_offerings"}


# ── Schema ────────────────────────────────────────────────────────────────────

_schema_ok = False


def ensure_schema() -> None:
    global _schema_ok
    if _schema_ok:
        return
    from db.migrate import schema_managed
    if schema_managed():   # schema changes come from migrations (db/migrate.py)
        _schema_ok = True
        return
    sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    conn = engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '5s'")
        cur.execute(sql)
        conn.commit()
        _schema_ok = True
    finally:
        conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _acl(s, user) -> List[int]:
    return auth.account_scope(s, user)


def own_deals_only(user) -> bool:
    """Sales reps (role "user") see only the deals they own in every deal list, the digest and
    Insights. Managers and admins see their whole scope. A rep can still open a colleague's deal
    from a direct link (e.g. a task assigned to them on it); edit rights are unchanged."""
    return auth.AUTH_ENFORCED and getattr(user, "role", None) == "user"


def _get_deal_row(s, deal_id: int, user) -> Dict[str, Any]:
    row = s.execute(text("""
        SELECT d.*, a.display_name AS account_name, l.lob_name, u.full_name AS owner_name, u.email AS owner_email,
               bl.name AS business_line_name
        FROM deals d JOIN accounts a ON a.id = d.account_id
        LEFT JOIN lobs l ON l.id = d.lob_id LEFT JOIN users u ON u.id = d.owner_user_id
        LEFT JOIN business_lines bl ON bl.id = d.business_line_id
        WHERE d.id = :id"""), {"id": deal_id}).mappings().fetchone()
    if not row or row["account_id"] not in _acl(s, user):
        raise HTTPException(404, "Deal not found.")
    return dict(row)


def _check_business_line(s, bl_id: Optional[int]) -> None:
    if bl_id is not None and not s.execute(text("SELECT 1 FROM business_lines WHERE id = :b AND active"), {"b": bl_id}).scalar():
        raise HTTPException(400, "Unknown or inactive business line.")


def _log(s, deal_id: int, user_id: Optional[int], kind: str, txt: str) -> None:
    s.execute(text("INSERT INTO deal_activity (deal_id, user_id, kind, text) VALUES (:d, :u, :k, :t)"),
              {"d": deal_id, "u": user_id, "k": kind, "t": txt[:1000]})
    s.execute(text("UPDATE deals SET updated_at = now() WHERE id = :d"), {"d": deal_id})


def _seed_checklist(s, deal_id: int) -> None:
    for stage, items in CHECKLIST.items():
        for i, (key, label) in enumerate(items):
            s.execute(text("""INSERT INTO deal_checklist (deal_id, stage, item_key, label, ordinal)
                              VALUES (:d, :s, :k, :l, :o) ON CONFLICT DO NOTHING"""),
                      {"d": deal_id, "s": stage, "k": key, "l": label, "o": i})


def _sync_auto_checks(s, deal_id: int, user_id: Optional[int]) -> None:
    """Tick checklist items that the data already proves (e.g. an economic buyer is on the committee)."""
    roles = {r[0] for r in s.execute(text("SELECT role FROM deal_stakeholders WHERE deal_id = :d"), {"d": deal_id})}
    offerings = s.execute(text("SELECT cardinality(offerings) FROM deals WHERE id = :d"), {"d": deal_id}).scalar() or 0
    facts = {"has_stakeholder": bool(roles), "economic_buyer": "economic_buyer" in roles,
             "champion": "champion" in roles, "has_offerings": offerings > 0}
    for (stage, key), fact in AUTO_CHECK.items():
        if facts[fact]:
            s.execute(text("""UPDATE deal_checklist SET done = true, done_at = now(), done_by = :u,
                              note = coalesce(note, 'Ticked automatically')
                              WHERE deal_id = :d AND stage = :s AND item_key = :k AND NOT done"""),
                      {"u": user_id, "d": deal_id, "s": stage, "k": key})


def health(deal: Dict[str, Any], stakeholders: List[Dict[str, Any]], checklist: List[Dict[str, Any]],
           last_activity: Optional[datetime], open_tasks: int, plan_overdue: int = 0) -> Dict[str, Any]:
    """0-100 score + reasons (README §21.1). Pure data, no LLM.

    gaps / gap_codes are parallel lists: the text is shown as-is (cards, digest, copilot); the code
    lets the deal room offer the matching one-click fix (see GAP_ACTIONS in deals/main.js)."""
    if deal["stage"] in CLOSED:
        return {"score": None, "level": "closed", "reasons": [], "gaps": [], "gap_codes": []}
    now = datetime.now(timezone.utc)
    score, reasons, gaps, codes = 0, [], [], []

    def gap(code: str, text: str) -> None:
        gaps.append(text)
        codes.append(code)

    roles = {x["role"] for x in stakeholders}
    if "champion" in roles:
        score += 20; reasons.append("Champion identified")
    else:
        gap("no_champion", "No champion on the buying committee")
    if "economic_buyer" in roles:
        score += 20; reasons.append("Economic buyer identified")
    elif STAGES.index(deal["stage"]) >= 1:
        gap("no_economic_buyer", "No economic buyer identified")
    if deal.get("next_step"):
        due = deal.get("next_step_due")
        if due and due < date.today():
            gap("next_step_overdue", f"Next step overdue since {due.isoformat()}")
            score += 5
        else:
            score += 20; reasons.append("Next step set")
    else:
        gap("no_next_step", "No next step set")
    if last_activity:
        days = (now - last_activity).days
        if days <= 14:
            score += 20; reasons.append(f"Active {days}d ago")
        elif days <= 30:
            score += 10; gap("quiet", f"Quiet for {days} days")
        else:
            gap("no_activity", f"No activity for {days} days")
    close = deal.get("expected_close")
    if close and close < date.today():
        # Not scored (the close date is often just stale) — but it misleads the forecast, so flag it
        gap("close_date_passed", f"Expected close date passed ({close.isoformat()})")
    stage_items = [c for c in checklist if c["stage"] == deal["stage"]]
    if stage_items:
        pct = sum(1 for c in stage_items if c["done"]) / len(stage_items)
        score += int(20 * pct)
        if pct < 0.5:
            gap("exit_criteria", f"Only {int(pct * 100)}% of {STAGE_LABEL[deal['stage']]} exit criteria done")
    blockers = [x for x in stakeholders if x["role"] == "blocker" or x.get("sentiment") == "negative"]
    if blockers:
        score -= 10; gap("blockers", f"{len(blockers)} blocker/negative stakeholder(s)")
    if plan_overdue:
        score -= 5; gap("plan_overdue", f"Close plan: {plan_overdue} milestone{'s' if plan_overdue != 1 else ''} overdue")
    in_stage = (now - deal["stage_changed_at"]).days
    if in_stage > 45:
        score -= 10; gap("long_in_stage", f"{in_stage} days in {STAGE_LABEL[deal['stage']]}")
    score = max(0, min(100, score))
    level = "good" if score >= 70 else ("watch" if score >= 40 else "risk")
    return {"score": score, "level": level, "reasons": reasons, "gaps": gaps, "gap_codes": codes, "days_in_stage": in_stage}


def _deal_payload(s, deal: Dict[str, Any], full: bool = False) -> Dict[str, Any]:
    did = deal["id"]
    stakeholders = [dict(r) for r in s.execute(text(f"""
        SELECT ds.persona_id, ds.role, ds.sentiment, coalesce(p.full_name, p.display_name) AS name, p.title,
               p.account_id, {privacy.SAFE_EMAIL_SQL} AS email, {privacy.SAFE_PHONE_SQL} AS phone, p.linkedin_url,
               p.decision_authority, p.budget_authority,
               EXISTS (SELECT 1 FROM cxo_movements c WHERE lower(c.person_name) = lower(coalesce(p.full_name, p.display_name))
                       AND c.first_seen > now() - interval '60 days') AS recent_move
        FROM deal_stakeholders ds JOIN personas p ON p.id = ds.persona_id
        WHERE ds.deal_id = :d ORDER BY array_position(ARRAY['economic_buyer','champion','technical_evaluator','influencer','user','blocker'], ds.role)"""),
        {"d": did}).mappings()]
    checklist = [dict(r) for r in s.execute(text("""
        SELECT stage, item_key, label, ordinal, done, note, done_at FROM deal_checklist WHERE deal_id = :d
        ORDER BY array_position(ARRAY['intro','discovery','proposal','pilot','contract'], stage), ordinal"""),
        {"d": did}).mappings()]
    internal_last = s.execute(text("SELECT max(created_at) FROM deal_activity WHERE deal_id = :d"), {"d": did}).scalar()
    customer_touch = deal.get("last_activity_at")          # latest logged/captured customer interaction (CRM activities)
    last_activity = max([x for x in (internal_last, customer_touch) if x is not None], default=None)
    open_tasks = s.execute(text("SELECT count(*) FROM action_items WHERE deal_id = :d AND status IN ('open','in_progress')"),
                           {"d": did}).scalar()
    plan = s.execute(text("""SELECT count(*) AS total, count(*) FILTER (WHERE status = 'done') AS done,
                                    count(*) FILTER (WHERE status <> 'done' AND due_date < current_date) AS overdue
                             FROM deal_close_plan WHERE deal_id = :d"""), {"d": did}).mappings().fetchone()
    h = health(deal, stakeholders, checklist, last_activity, open_tasks, plan["overdue"] if deal["stage"] in STAGES else 0)
    if deal["stage"] in STAGES:
        now = datetime.now(timezone.utc)
        if customer_touch is None and (now - deal["created_at"]).days >= 7:
            h["gaps"].append("No customer interaction logged yet")
        elif customer_touch is not None and (now - customer_touch).days > 21:
            h["gaps"].append(f"No customer contact for {(now - customer_touch).days} days")
    for x in stakeholders:
        if x["recent_move"]:
            h["gaps"].append(f"{x['name']} ({x['role'].replace('_', ' ')}) had a recent leadership change")
    out = {
        "id": did, "name": deal["name"], "account_id": deal["account_id"], "account_name": deal["account_name"],
        "lob_id": deal["lob_id"], "lob_name": deal.get("lob_name"), "stage": deal["stage"],
        "business_line_id": deal.get("business_line_id"), "business_line_name": deal.get("business_line_name"),
        "forecast_category": deal.get("forecast_category") or "pipeline", "probability": deal.get("probability"),
        "stage_probability": stage_probability(s).get(deal["stage"]),
        "amount_usd": float(deal["amount_usd"]) if deal.get("amount_usd") is not None else None,
        "stage_label": STAGE_LABEL[deal["stage"]], "offerings": deal["offerings"] or [],
        "value_amount": float(deal["value_amount"]) if deal["value_amount"] is not None else None,
        "currency": deal["currency"], "expected_close": deal["expected_close"], "next_step": deal["next_step"],
        "next_step_due": deal["next_step_due"], "lost_reason": deal["lost_reason"],
        "lost_category": deal.get("lost_category"), "lost_competitor": deal.get("lost_competitor"),
        "lost_category_label": LOST_CATEGORIES.get(deal.get("lost_category") or "", None),
        "owner": {"id": deal["owner_user_id"], "name": deal.get("owner_name") or deal.get("owner_email")},
        "stage_changed_at": deal["stage_changed_at"], "created_at": deal["created_at"], "updated_at": deal["updated_at"],
        "health": h, "open_tasks": open_tasks, "last_activity": last_activity, "last_customer_touch": customer_touch,
        "stakeholder_count": len(stakeholders),
        "committee_gaps": [r for r in ("champion", "economic_buyer") if r not in {x["role"] for x in stakeholders}],
    }
    stage_items = [c for c in checklist if c["stage"] == deal["stage"]]
    out["stage_progress"] = {"done": sum(1 for c in stage_items if c["done"]), "total": len(stage_items)}
    out["plan_progress"] = {"done": plan["done"], "total": plan["total"], "overdue": plan["overdue"]}
    if deal.get("introduction_id"):
        intro = s.execute(text("""SELECT i.id, i.status, i.attribution_pct, c.name AS connector_name, c.kind AS connector_kind
                                    FROM introductions i JOIN connectors c ON c.id = i.connector_id WHERE i.id = :i"""),
                          {"i": deal["introduction_id"]}).mappings().fetchone()
        out["introduction"] = dict(intro) if intro else None
        if intro and intro["attribution_pct"] is not None:
            out["introduction"]["attribution_pct"] = float(intro["attribution_pct"])
    out["source"] = deal.get("source")
    if full:
        out["qualification"] = deal.get("qualification") or {}
        out["stakeholders"] = stakeholders
        out["checklist"] = checklist
        out["history"] = [dict(r) for r in s.execute(text("""
            SELECT h.from_stage, h.to_stage, h.changed_at, u.full_name AS by FROM deal_stage_history h
            LEFT JOIN users u ON u.id = h.changed_by WHERE h.deal_id = :d ORDER BY h.changed_at"""), {"d": did}).mappings()]
        out["activity"] = [dict(r) for r in s.execute(text("""
            SELECT a.id, a.kind, a.text, a.created_at, coalesce(u.full_name, u.email) AS by FROM deal_activity a
            LEFT JOIN users u ON u.id = a.user_id WHERE a.deal_id = :d ORDER BY a.created_at DESC LIMIT 100"""), {"d": did}).mappings()]
        out["tasks"] = [dict(r) for r in s.execute(text("""
            SELECT t.id, t.title, t.status, t.priority, t.due_date, t.assigned_to_id,
                   coalesce(u.full_name, u.email) AS assignee
            FROM action_items t LEFT JOIN users u ON u.id = t.assigned_to_id WHERE t.deal_id = :d
            ORDER BY (t.status IN ('done','cancelled')), t.due_date NULLS LAST, t.id"""), {"d": did}).mappings()]
        out["close_plan"] = _plan_rows(s, did)
        from apps.sales_deals import next_action
        out["next_action"] = next_action.pick(out)
    return out


# ── Models ────────────────────────────────────────────────────────────────────


class DealIn(BaseModel):
    account_id: int
    name: str = Field(..., min_length=1, max_length=200)
    lob_id: Optional[int] = None
    business_line_id: Optional[int] = None
    value_amount: Optional[float] = Field(None, ge=0)
    currency: str = Field("USD", max_length=3)
    expected_close: Optional[date] = None
    offerings: List[str] = Field(default_factory=list)
    next_step: Optional[str] = Field(None, max_length=500)
    next_step_due: Optional[date] = None
    stage: str = "intro"


class DealPatch(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    lob_id: Optional[int] = None
    business_line_id: Optional[int] = None
    forecast_category: Optional[str] = None
    probability: Optional[int] = Field(None, ge=0, le=100)
    clear_probability: bool = False
    value_amount: Optional[float] = Field(None, ge=0)
    currency: Optional[str] = Field(None, max_length=3)
    expected_close: Optional[date] = None
    offerings: Optional[List[str]] = None
    next_step: Optional[str] = Field(None, max_length=500)
    next_step_due: Optional[date] = None
    stage: Optional[str] = None
    lost_reason: Optional[str] = Field(None, max_length=500)
    lost_category: Optional[str] = None
    lost_competitor: Optional[str] = Field(None, max_length=120)
    reason: Optional[str] = Field(None, max_length=500)


class StakeholderIn(BaseModel):
    persona_id: int
    role: str
    sentiment: Optional[str] = None


class CheckIn(BaseModel):
    done: bool
    note: Optional[str] = Field(None, max_length=500)


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class TaskIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    due_date: Optional[date] = None
    priority: str = "medium"
    persona_id: Optional[int] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get("/meta")
def meta(s=Depends(_session)):
    bls = [dict(r) for r in s.execute(text("SELECT id, key, name FROM business_lines WHERE active ORDER BY sort, name")).mappings()]
    return {"stages": [{"key": k, "label": STAGE_LABEL[k]} for k in STAGES + CLOSED], "roles": ROLES,
            "won_from": WON_FROM, "business_lines": bls,
            "offerings": [{"key": k, "label": v} for k, v in OFFERINGS.items()],
            "checklist": {k: [{"key": a, "label": b} for a, b in v] for k, v in CHECKLIST.items()}}


@router.get("")
def list_deals(account_id: Optional[int] = None, business_line_id: Optional[int] = None, mine: bool = False, q: Optional[str] = None,
               include_closed: bool = True, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    acl = _acl(s, user)
    mine = mine or own_deals_only(user)
    sql = """SELECT d.*, a.display_name AS account_name, l.lob_name, u.full_name AS owner_name, u.email AS owner_email,
                    bl.name AS business_line_name
             FROM deals d JOIN accounts a ON a.id = d.account_id LEFT JOIN lobs l ON l.id = d.lob_id
             LEFT JOIN users u ON u.id = d.owner_user_id LEFT JOIN business_lines bl ON bl.id = d.business_line_id
             WHERE d.account_id = ANY(:acl)"""
    params: Dict[str, Any] = {"acl": acl}
    if account_id:
        sql += " AND d.account_id = :a"; params["a"] = account_id
    if business_line_id:
        sql += " AND d.business_line_id = :bl"; params["bl"] = business_line_id
    if mine:
        sql += " AND d.owner_user_id = :u"; params["u"] = user.id
    if q:
        sql += " AND (d.name ILIKE :q OR a.display_name ILIKE :q)"; params["q"] = f"%{q}%"
    if not include_closed:
        sql += " AND d.stage NOT IN ('won','lost')"
    sql += " ORDER BY d.updated_at DESC LIMIT 500"
    deals = [_deal_payload(s, dict(r)) for r in s.execute(text(sql), params).mappings()]
    summary = {}
    for st in STAGES + CLOSED:
        ds = [d for d in deals if d["stage"] == st]
        summary[st] = {"count": len(ds), "value": sum(d["value_amount"] or 0 for d in ds)}
    by_reason: Dict[str, int] = {}
    for d in deals:
        if d["stage"] == "lost":
            k = d.get("lost_category") or "uncategorised"
            by_reason[k] = by_reason.get(k, 0) + 1
    summary["lost"]["by_reason"] = [{"key": k, "label": LOST_CATEGORIES.get(k, "Not categorised"), "count": n}
                                    for k, n in sorted(by_reason.items(), key=lambda kv: -kv[1])]
    return {"deals": deals, "summary": summary, "lost_categories": LOST_CATEGORIES, "own_only": own_deals_only(user)}


@router.get("/analytics")
def analytics(days: int = 365, business_line_id: Optional[int] = None, mine: bool = False,
              user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Win/loss and stage velocity (#9) over the deals this user can see, closed or created in the
    last `days`: win rate, loss reasons, competitors, stage conversion, days per stage, and how
    introduction-sourced deals do against the rest."""
    days = max(7, min(days, 1825))
    mine = mine or own_deals_only(user)
    where = "d.account_id = ANY(:acl)"
    params: Dict[str, Any] = {"acl": _acl(s, user), "days": days}
    if business_line_id:
        where += " AND d.business_line_id = :bl"; params["bl"] = business_line_id
    if mine:
        where += " AND d.owner_user_id = :u"; params["u"] = user.id
    since = "now() - make_interval(days => :days)"
    amount = "coalesce(d.amount_usd, d.value_amount, 0)"

    closed = s.execute(text(f"""
        SELECT d.id, d.stage, {amount} AS amount, d.lost_category, nullif(trim(d.lost_competitor), '') AS competitor,
               d.introduction_id IS NOT NULL AS sourced, c.id AS connector_id, c.name AS connector_name,
               extract(epoch FROM d.closed_at - d.created_at) / 86400 AS cycle_days
        FROM deals d LEFT JOIN introductions i ON i.id = d.introduction_id LEFT JOIN connectors c ON c.id = i.connector_id
        WHERE {where} AND d.stage IN ('won','lost') AND d.closed_at >= {since}"""), params).mappings().all()

    def rate(rows):
        won = sum(1 for r in rows if r["stage"] == "won")
        return {"won": won, "lost": len(rows) - won, "win_rate": round(100 * won / len(rows)) if rows else None,
                "won_value": round(sum(float(r["amount"]) for r in rows if r["stage"] == "won"), 2)}

    won_rows = [r for r in closed if r["stage"] == "won"]
    totals = {**rate(closed),
              "lost_value": round(sum(float(r["amount"]) for r in closed if r["stage"] == "lost"), 2),
              "avg_won_size": round(sum(float(r["amount"]) for r in won_rows) / len(won_rows), 2) if won_rows else None,
              "avg_cycle_days": round(sum(float(r["cycle_days"] or 0) for r in won_rows) / len(won_rows)) if won_rows else None}

    reasons: Dict[str, int] = {}
    competitors: Dict[str, Dict[str, Any]] = {}
    for r in closed:
        if r["stage"] != "lost":
            continue
        k = r["lost_category"] or "uncategorised"
        reasons[k] = reasons.get(k, 0) + 1
        if r["competitor"]:
            c = competitors.setdefault(r["competitor"].lower(), {"name": r["competitor"], "count": 0, "value": 0.0})
            c["count"] += 1; c["value"] += float(r["amount"])
    loss_reasons = [{"key": k, "label": LOST_CATEGORIES.get(k, "Not categorised"), "count": n}
                    for k, n in sorted(reasons.items(), key=lambda kv: -kv[1])]

    connectors: Dict[int, Dict[str, Any]] = {}
    for r in won_rows:
        if r["connector_id"]:
            c = connectors.setdefault(r["connector_id"], {"id": r["connector_id"], "name": r["connector_name"], "won": 0, "value": 0.0})
            c["won"] += 1; c["value"] += float(r["amount"])

    # Stage history of deals created in the window (the cohort) -> conversion and time per stage
    hist = s.execute(text(f"""
        SELECT h.deal_id, h.to_stage, h.changed_at, d.stage AS current_stage
        FROM deal_stage_history h JOIN deals d ON d.id = h.deal_id
        WHERE {where} AND d.created_at >= {since}
        ORDER BY h.deal_id, h.changed_at, h.id"""), params).mappings().all()
    order = STAGES + ["won"]
    reached = {st: 0 for st in order}
    spans: Dict[str, List[float]] = {st: [] for st in STAGES}
    by_deal: Dict[int, List[Any]] = {}
    for h in hist:
        by_deal.setdefault(h["deal_id"], []).append(h)
    for rows in by_deal.values():
        top = max((order.index(h["to_stage"]) for h in rows if h["to_stage"] in order), default=-1)
        for i in range(top + 1):
            reached[order[i]] += 1
        for cur, nxt in zip(rows, rows[1:] + [None]):
            if cur["to_stage"] in spans and nxt is not None:      # completed time in a stage only
                spans[cur["to_stage"]].append((nxt["changed_at"] - cur["changed_at"]).total_seconds() / 86400)
    in_stage_now = {st: [] for st in STAGES}
    for r in s.execute(text(f"""SELECT d.stage, extract(epoch FROM now() - d.stage_changed_at) / 86400 AS days
                                FROM deals d WHERE {where} AND d.stage = ANY(:open)"""),
                       {**params, "open": STAGES}).mappings():
        in_stage_now[r["stage"]].append(float(r["days"] or 0))
    funnel = []
    for i, st in enumerate(order):
        nxt = reached[order[i + 1]] if i + 1 < len(order) else None
        funnel.append({"stage": st, "label": STAGE_LABEL[st], "reached": reached[st],
                       "to_next_pct": round(100 * nxt / reached[st]) if nxt is not None and reached[st] else None,
                       "avg_days": round(sum(spans[st]) / len(spans[st]), 1) if spans.get(st) else None,
                       "open_now": len(in_stage_now.get(st, [])),
                       "open_avg_days": round(sum(in_stage_now[st]) / len(in_stage_now[st]), 1) if in_stage_now.get(st) else None})

    return {"days": days, "totals": totals, "loss_reasons": loss_reasons,
            "competitors": sorted(competitors.values(), key=lambda c: (-c["count"], -c["value"]))[:8],
            "by_source": {"introduction": rate([r for r in closed if r["sourced"]]),
                          "other": rate([r for r in closed if not r["sourced"]])},
            "top_connectors": sorted(connectors.values(), key=lambda c: -c["value"])[:8],
            "funnel": funnel, "cohort_size": len(by_deal)}


@router.post("")
def create_deal(body: DealIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    if body.account_id not in _acl(s, user):
        raise HTTPException(404, "Account not found.")
    if body.stage not in STAGES:
        raise HTTPException(400, "stage must be one of " + ", ".join(STAGES))
    offerings = [o for o in body.offerings if o in OFFERINGS]
    _check_business_line(s, body.business_line_id)
    did = s.execute(text("""
        INSERT INTO deals (account_id, lob_id, business_line_id, name, owner_user_id, stage, offerings, value_amount, currency,
                           expected_close, next_step, next_step_due)
        VALUES (:a, :l, :bl, :n, :u, :st, :o, :v, :c, :ec, :ns, :nd) RETURNING id"""),
        {"a": body.account_id, "l": body.lob_id, "bl": body.business_line_id, "n": body.name.strip(), "u": user.id, "st": body.stage,
         "o": offerings, "v": body.value_amount, "c": body.currency.upper(), "ec": body.expected_close,
         "ns": body.next_step, "nd": body.next_step_due}).scalar()
    _seed_checklist(s, did)
    s.execute(text("INSERT INTO deal_stage_history (deal_id, from_stage, to_stage, changed_by) VALUES (:d, NULL, :t, :u)"),
              {"d": did, "t": body.stage, "u": user.id})
    _log(s, did, user.id, "created", f"Deal created in {STAGE_LABEL[body.stage]}")
    _sync_auto_checks(s, did, user.id)
    s.commit()
    return _deal_payload(s, _get_deal_row(s, did, user), full=True)




def digest_data(s, acl: List[int], owner_id: Optional[int] = None, days: int = 7) -> Dict[str, Any]:
    """Weekly pipeline digest (README §21.5, phase D4) — pure SQL + health(), no LLM."""
    sql = """SELECT d.*, a.display_name AS account_name, l.lob_name, u.full_name AS owner_name, u.email AS owner_email,
                    bl.name AS business_line_name
             FROM deals d JOIN accounts a ON a.id = d.account_id LEFT JOIN lobs l ON l.id = d.lob_id
             LEFT JOIN users u ON u.id = d.owner_user_id LEFT JOIN business_lines bl ON bl.id = d.business_line_id
             WHERE d.account_id = ANY(:acl)"""
    params: Dict[str, Any] = {"acl": acl}
    if owner_id:
        sql += " AND d.owner_user_id = :u"; params["u"] = owner_id
    deals = [_deal_payload(s, dict(r)) for r in s.execute(text(sql), params).mappings()]
    open_ = [d for d in deals if d["stage"] in STAGES]
    since = datetime.now(timezone.utc).timestamp() - days * 86400
    moves = [dict(r) for r in s.execute(text("""
        SELECT h.deal_id, d.name, a.display_name AS account_name, h.from_stage, h.to_stage, h.changed_at
        FROM deal_stage_history h JOIN deals d ON d.id = h.deal_id JOIN accounts a ON a.id = d.account_id
        WHERE d.account_id = ANY(:acl) AND h.from_stage IS NOT NULL AND h.changed_at > now() - make_interval(days => :days)
          AND (:u IS NULL OR d.owner_user_id = :u)
        ORDER BY h.changed_at DESC LIMIT 30"""), {"acl": acl, "days": days, "u": owner_id}).mappings()]
    today = date.today()

    def brief(d, why=""):
        return {"id": d["id"], "name": d["name"], "account_name": d["account_name"], "stage": d["stage"],
                "stage_label": d["stage_label"], "value_amount": d["value_amount"], "currency": d["currency"],
                "health": d["health"].get("score"), "level": d["health"]["level"], "why": why}

    at_risk = [brief(d, "; ".join(d["health"]["gaps"][:2])) for d in open_ if d["health"]["level"] == "risk"]
    stuck = [brief(d, f"{d['health'].get('days_in_stage', 0)} days in {d['stage_label']}")
             for d in open_ if d["health"].get("days_in_stage", 0) > 30]
    overdue = [brief(d, f"'{d['next_step']}' was due {d['next_step_due']}") for d in open_
               if d["next_step"] and d["next_step_due"] and d["next_step_due"] < today]
    closing = [brief(d, f"expected close {d['expected_close']}") for d in open_
               if d["expected_close"] and 0 <= (d["expected_close"] - today).days <= 30]
    closed = [brief(d, " — ".join(x for x in (d.get("lost_category_label"), d["lost_reason"]) if x)) for d in deals if d["stage"] in CLOSED
              and d["stage_changed_at"] and d["stage_changed_at"].timestamp() > since]
    probs = stage_probability(s)

    def usd(d):  # USD via fx_rates (deals.amount_usd); falls back to the raw value if no rate exists
        return d["amount_usd"] if d.get("amount_usd") is not None else (d["value_amount"] or 0)
    total = sum(usd(d) for d in open_ if d["forecast_category"] != "omitted")
    weighted = sum(usd(d) * (d["probability"] if d["probability"] is not None else probs.get(d["stage"], 0)) / 100
                   for d in open_ if d["forecast_category"] != "omitted")
    return {"days": days, "generated_at": datetime.now(timezone.utc),
            "totals": {"open": len(open_), "value": total, "weighted": round(weighted, 2),
                       "by_stage": {st: sum(1 for d in open_ if d["stage"] == st) for st in STAGES},
                       "avg_health": round(sum(d["health"]["score"] or 0 for d in open_) / len(open_)) if open_ else None},
            "moves": moves, "at_risk": at_risk, "stuck": stuck, "overdue": overdue, "closing_soon": closing, "closed": closed}


@router.get("/pipeline/digest")
def digest(mine: bool = False, days: int = 7, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    return digest_data(s, _acl(s, user), user.id if (mine or own_deals_only(user)) else None, max(1, min(days, 90)))


@router.get("/{deal_id}/toolkit")
def toolkit(deal_id: int, stage: Optional[str] = None, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Stage toolkit (README §21.1, D3): why-now, question bank, MEDDICC, value map, battlecard, pilot plan …"""
    from apps.sales_deals import toolkit as tk
    deal = _get_deal_row(s, deal_id, user)
    st = stage or (deal["stage"] if deal["stage"] in STAGES else "contract")
    if st not in tk.BUILDERS:
        raise HTTPException(400, "stage must be one of " + ", ".join(STAGES))
    return {"stage": st, "stage_label": STAGE_LABEL[st], "data": tk.BUILDERS[st](s, deal)}


class QualificationIn(BaseModel):
    values: Dict[str, str]


@router.patch("/{deal_id}/qualification")
def set_qualification(deal_id: int, body: QualificationIn, user: User = Depends(auth.get_current_user),
                      s=Depends(_session)):
    from apps.sales_deals.toolkit import MEDDICC
    _get_deal_row(s, deal_id, user)
    labels = {k: label for k, label, _ in MEDDICC}
    vals = {k: (v or "").strip()[:1000] for k, v in body.values.items() if k in labels}
    if not vals:
        raise HTTPException(400, "Nothing to update.")
    import json
    s.execute(text("UPDATE deals SET qualification = qualification || CAST(:v AS jsonb) WHERE id = :d"),
              {"v": json.dumps(vals), "d": deal_id})
    _log(s, deal_id, user.id, "field", "MEDDICC updated: " + ", ".join(labels[k] for k in vals))
    s.commit()
    return {"qualification": s.execute(text("SELECT qualification FROM deals WHERE id = :d"), {"d": deal_id}).scalar()}


@router.get("/{deal_id}")
def get_deal(deal_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    return _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)


@router.patch("/{deal_id}")
def update_deal(deal_id: int, body: DealPatch, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    deal = _get_deal_row(s, deal_id, user)
    changes = body.model_dump(exclude_unset=True)
    warnings: List[str] = []
    new_stage = changes.pop("stage", None)
    reason = (changes.pop("reason", None) or "").strip()
    if "offerings" in changes:
        changes["offerings"] = [o for o in changes["offerings"] or [] if o in OFFERINGS]
    if "currency" in changes and changes["currency"]:
        changes["currency"] = changes["currency"].upper()
    if "business_line_id" in changes:
        _check_business_line(s, changes["business_line_id"])
    if changes.pop("clear_probability", False):
        changes["probability"] = None
    if changes.get("lost_category") is not None and changes["lost_category"] not in LOST_CATEGORIES:
        raise HTTPException(400, "lost_category must be one of: " + ", ".join(LOST_CATEGORIES))
    for k in ("lost_reason", "lost_competitor"):
        if k in changes and changes[k] is not None:
            changes[k] = changes[k].strip() or None
    if new_stage == "lost" and new_stage != deal["stage"]:
        cat = changes.get("lost_category") or deal.get("lost_category")
        note = changes.get("lost_reason") or deal.get("lost_reason")
        if not cat and note:            # older clients send only a text reason
            cat = changes["lost_category"] = "other"
        if not cat:
            raise HTTPException(400, "Please pick why the deal was lost.")
        if cat == "other" and not note:
            raise HTTPException(400, "Please add a short note when the reason is Other.")
        if cat != "competitor" and "lost_competitor" not in changes:
            changes["lost_competitor"] = None
    if "forecast_category" in changes:
        if changes["forecast_category"] not in ("pipeline", "best_case", "commit", "omitted"):
            raise HTTPException(400, "forecast_category must be pipeline, best_case, commit or omitted.")
        if deal["stage"] in CLOSED:
            raise HTTPException(400, "Won and lost deals are categorised automatically.")
    for col, val in changes.items():
        s.execute(text(f"UPDATE deals SET {col} = :v, updated_at = now() WHERE id = :d"), {"v": val, "d": deal_id})
        if col in ("next_step", "value_amount", "expected_close", "name"):
            _log(s, deal_id, user.id, "field", f"{col.replace('_', ' ').capitalize()} → {val if val is not None else '—'}")
        elif col in ("forecast_category", "probability"):
            label = {"forecast_category": "Forecast category", "probability": "Probability"}[col]
            shown = (str(val).replace("_", " ") + ("%" if col == "probability" else "")) if val is not None else "stage default"
            _log(s, deal_id, user.id, "field", f"{label} → {shown}")
        elif col == "business_line_id":
            bl = s.execute(text("SELECT name FROM business_lines WHERE id = :b"), {"b": val}).scalar() if val else None
            _log(s, deal_id, user.id, "field", f"Business line → {bl or '—'}")
    if new_stage and new_stage != deal["stage"]:
        if new_stage not in STAGES + CLOSED:
            raise HTTPException(400, "Unknown stage.")
        if new_stage == "won" and deal["stage"] not in WON_FROM:
            raise HTTPException(400, "Move the deal to Proposal or later before marking it won.")
        if new_stage in STAGES and deal["stage"] in STAGES:
            step = STAGES.index(new_stage) - STAGES.index(deal["stage"])
            if abs(step) > 1:
                raise HTTPException(400, "Move one stage at a time.")
            if step == -1 and not reason:
                raise HTTPException(400, "Please give a reason for moving back a stage.")
            if step > 0:
                open_items = s.execute(text("""SELECT label FROM deal_checklist WHERE deal_id = :d AND stage = :s AND NOT done
                                               ORDER BY ordinal"""), {"d": deal_id, "s": deal["stage"]}).scalars().all()
                if open_items:
                    warnings.append(f"Moved on with {len(open_items)} open {STAGE_LABEL[deal['stage']]} item(s): " + "; ".join(open_items))
        s.execute(text("UPDATE deals SET stage = :st, stage_changed_at = now(), updated_at = now() WHERE id = :d"),
                  {"st": new_stage, "d": deal_id})
        if deal["stage"] == "lost":   # reopened: the old loss reason stays in the activity log only
            s.execute(text("UPDATE deals SET lost_category = NULL, lost_competitor = NULL, lost_reason = NULL WHERE id = :d"),
                      {"d": deal_id})
        s.execute(text("INSERT INTO deal_stage_history (deal_id, from_stage, to_stage, changed_by) VALUES (:d, :f, :t, :u)"),
                  {"d": deal_id, "f": deal["stage"], "t": new_stage, "u": user.id})
        if new_stage == "lost":
            cat = changes.get("lost_category") or deal.get("lost_category")
            comp = changes.get("lost_competitor") if "lost_competitor" in changes else deal.get("lost_competitor")
            note = changes.get("lost_reason") or deal.get("lost_reason")
            stage_reason = " — ".join(x for x in (LOST_CATEGORIES.get(cat, cat) + (f": {comp}" if comp else ""), note) if x)
        else:
            stage_reason = reason
        _log(s, deal_id, user.id, "stage", f"{STAGE_LABEL[deal['stage']]} → {STAGE_LABEL[new_stage]}"
             + (f" (reason: {stage_reason})" if stage_reason else ""))
    _sync_auto_checks(s, deal_id, user.id)
    s.commit()
    out = _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)
    out["warnings"] = warnings
    return out


@router.delete("/{deal_id}")
def delete_deal(deal_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    deal = _get_deal_row(s, deal_id, user)
    if not permissions.can(s, user, "delete", deal["account_id"], deal["owner_user_id"]):
        raise HTTPException(403, "Only the deal owner, their manager or an admin can delete a deal.")
    s.execute(text("DELETE FROM deals WHERE id = :d"), {"d": deal_id})
    s.commit()
    return {"ok": True}


@router.post("/{deal_id}/stakeholders")
def add_stakeholder(deal_id: int, body: StakeholderIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    deal = _get_deal_row(s, deal_id, user)
    if body.role not in ROLES:
        raise HTTPException(400, "role must be one of " + ", ".join(ROLES))
    if body.sentiment and body.sentiment not in ("positive", "neutral", "negative"):
        raise HTTPException(400, "sentiment must be positive, neutral or negative")
    p = s.execute(text("SELECT coalesce(full_name, display_name), account_id FROM personas WHERE id = :p"),
                  {"p": body.persona_id}).fetchone()
    if not p or p[1] != deal["account_id"]:
        raise HTTPException(400, "That person isn't at this deal's account.")
    s.execute(text("""INSERT INTO deal_stakeholders (deal_id, persona_id, role, sentiment) VALUES (:d, :p, :r, :se)
                      ON CONFLICT (deal_id, persona_id) DO UPDATE SET role = EXCLUDED.role, sentiment = EXCLUDED.sentiment"""),
              {"d": deal_id, "p": body.persona_id, "r": body.role, "se": body.sentiment})
    _log(s, deal_id, user.id, "stakeholder", f"{p[0]} → {body.role.replace('_', ' ')}")
    _sync_auto_checks(s, deal_id, user.id)
    s.commit()
    return _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)


@router.delete("/{deal_id}/stakeholders/{persona_id}")
def remove_stakeholder(deal_id: int, persona_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    _get_deal_row(s, deal_id, user)
    name = s.execute(text("SELECT coalesce(full_name, display_name) FROM personas WHERE id = :p"), {"p": persona_id}).scalar()
    s.execute(text("DELETE FROM deal_stakeholders WHERE deal_id = :d AND persona_id = :p"), {"d": deal_id, "p": persona_id})
    _log(s, deal_id, user.id, "stakeholder", f"{name or 'Contact'} removed from the committee")
    s.commit()
    return _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)


@router.patch("/{deal_id}/checklist/{stage}/{item_key}")
def check_item(deal_id: int, stage: str, item_key: str, body: CheckIn,
               user: User = Depends(auth.get_current_user), s=Depends(_session)):
    _get_deal_row(s, deal_id, user)
    label = s.execute(text("""UPDATE deal_checklist SET done = :done, note = coalesce(:note, note),
                              done_by = CASE WHEN :done THEN :u END, done_at = CASE WHEN :done THEN now() END
                              WHERE deal_id = :d AND stage = :s AND item_key = :k RETURNING label"""),
                      {"done": body.done, "note": body.note, "u": user.id, "d": deal_id, "s": stage, "k": item_key}).scalar()
    if not label:
        raise HTTPException(404, "Checklist item not found.")
    _log(s, deal_id, user.id, "checklist", f"{'✓' if body.done else '○'} {label}")
    s.commit()
    return _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)


@router.post("/{deal_id}/notes")
def add_note(deal_id: int, body: NoteIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    _get_deal_row(s, deal_id, user)
    _log(s, deal_id, user.id, "note", body.text.strip())
    s.commit()
    return _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)


# ── Next best action (#12): AI draft for the one suggested action ───────────

def _clean_email_subject(subj: str, default_title: str = "") -> str:
    """Cleans an email subject line so it is a natural, professional customer-facing subject.
    Removes internal CRM prefixes like 'Next step:', 'Do or reschedule:', 'Action:', etc.
    """
    import re
    if not subj:
        subj = default_title or "Following up"
    s = subj.strip()
    for _ in range(3):
        cleaned = re.sub(
            r"^(?:(?:Our\s+)?Next\s+step(?:\s+is)?\s*:\s*|Do\s+or\s+reschedule\s*:\s*|Confirm\s+(?:the\s+)?next\s+step(?:\s+with\s+[^:]+)?\s*:\s*|Action\s*:\s*|Goal\s*:\s*|Subject\s*:\s*)",
            "", s, flags=re.IGNORECASE
        ).strip()
        if cleaned == s:
            break
        s = cleaned
    s = re.sub(r"^(?:Next\s+step\s*[-–—]\s*)", "", s, flags=re.IGNORECASE).strip()
    if not s:
        s = default_title or "Following up"
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s[:200]


@router.post("/{deal_id}/next-action/draft")
def draft_next_action(deal_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Drafts the email for the deal's current next best action (one LLM request, shared team budget)."""
    import json as _json
    import re
    from apps.sales_copilot import llm
    from apps.sales_deals import next_action
    d = _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)
    action = d.get("next_action")
    if not action or not action.get("can_draft"):
        raise HTTPException(400, "There's no email to draft for this deal's next action.")
    prompt = next_action.draft_prompt(d, action, user.full_name or user.email)
    try:
        raw, info = llm.chat(s, feature="deals", user_id=user.id, system=prompt["system"], user=prompt["user"], max_tokens=500)
    except llm.QuotaExceeded as e:
        raise HTTPException(429, str(e))
    except llm.LLMError:
        raise HTTPException(502, "The AI service didn't answer — try again in a minute.")
    m = re.search(r"\{.*\}", raw, re.S)
    subject, body = action["title"], raw.strip()
    if m:
        try:
            j = _json.loads(m.group(0))
            subject = str(j.get("subject") or subject).strip()[:200]
            body = str(j.get("body") or body).strip()
        except ValueError:
            pass
    subject = _clean_email_subject(subject, default_title=action.get("title") or "")
    email = None
    target_name = (action.get("target") or {}).get("name")
    pid = (action.get("target") or {}).get("persona_id")
    if pid:
        email = next((p.get("email") for p in d["stakeholders"] if p["persona_id"] == pid), None)
    draft_row = s.execute(text("""
        INSERT INTO deal_email_drafts (deal_id, user_id, action_code, stage, recipient_name, recipient_email, subject, body, model)
        VALUES (:deal_id, :user_id, :action_code, :stage, :recipient_name, :recipient_email, :subject, :body, :model)
        RETURNING id, created_at
    """), {
        "deal_id": deal_id,
        "user_id": user.id,
        "action_code": action["code"],
        "stage": d.get("stage"),
        "recipient_name": target_name,
        "recipient_email": email,
        "subject": subject,
        "body": body,
        "model": info.get("model")
    }).mappings().fetchone()
    _log(s, deal_id, user.id, "field", f"Next best action: drafted an email ({action['title']})")
    s.commit()
    return {
        "id": draft_row["id"],
        "action": action["code"],
        "subject": subject,
        "body": body,
        "to": email,
        "recipient_name": target_name,
        "created_at": draft_row["created_at"].isoformat() if draft_row and draft_row.get("created_at") else None,
        "model": info.get("model"),
    }


@router.get("/{deal_id}/email-drafts")
def list_email_drafts(deal_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Returns the history of AI-generated email drafts for this deal, newest first."""
    _get_deal_row(s, deal_id, user)
    rows = s.execute(text("""
        SELECT ed.id, ed.deal_id, ed.user_id, ed.action_code, ed.stage, ed.recipient_name, ed.recipient_email,
               ed.subject, ed.body, ed.model, ed.created_at, u.full_name AS user_name, u.email AS user_email
        FROM deal_email_drafts ed
        LEFT JOIN users u ON u.id = ed.user_id
        WHERE ed.deal_id = :deal_id
        ORDER BY ed.created_at DESC
    """), {"deal_id": deal_id}).mappings().fetchall()
    return [{
        "id": r["id"],
        "deal_id": r["deal_id"],
        "user_id": r["user_id"],
        "user_name": r["user_name"] or r["user_email"] or "Unknown",
        "action_code": r["action_code"],
        "stage": r["stage"],
        "recipient_name": r["recipient_name"],
        "recipient_email": r["recipient_email"],
        "subject": _clean_email_subject(r["subject"]),
        "body": r["body"],
        "model": r["model"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in rows]


# ── Close plan (#10): dated milestones both sides agree on ───────────────────

PLAN_SIDES = ("us", "customer", "both")
PLAN_STATUSES = ("todo", "done", "blocked")
# The standard plan for getting a signature: (title, side, days before expected close)
PLAN_TEMPLATE = [
    ("Confirm the decision process and who signs", "customer", 28),
    ("Security / IT review complete", "customer", 21),
    ("Commercial proposal agreed", "both", 14),
    ("Legal review of the contract (MSA / SOW)", "customer", 10),
    ("Procurement / vendor onboarding done", "customer", 7),
    ("Contract signed", "both", 0),
    ("Kick-off meeting held", "us", -7),
]


def _plan_rows(s, deal_id: int) -> List[Dict[str, Any]]:
    return [dict(r) for r in s.execute(text("""
        SELECT p.id, p.title, p.side, p.owner_name, p.due_date, p.status, p.note, p.ordinal, p.done_at,
               (p.status <> 'done' AND p.due_date < current_date) AS overdue
        FROM deal_close_plan p WHERE p.deal_id = :d ORDER BY p.ordinal, p.due_date NULLS LAST, p.id"""),
        {"d": deal_id}).mappings()]


class PlanItemIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    side: str = "us"
    owner_name: Optional[str] = Field(None, max_length=120)
    due_date: Optional[date] = None
    note: Optional[str] = Field(None, max_length=1000)


class PlanItemPatch(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    side: Optional[str] = None
    owner_name: Optional[str] = Field(None, max_length=120)
    due_date: Optional[date] = None
    status: Optional[str] = None
    note: Optional[str] = Field(None, max_length=1000)
    ordinal: Optional[int] = Field(None, ge=0, le=1000)


def _plan_check(body) -> None:
    if getattr(body, "side", None) is not None and body.side not in PLAN_SIDES:
        raise HTTPException(400, "side must be us, customer or both.")
    if getattr(body, "status", None) is not None and body.status not in PLAN_STATUSES:
        raise HTTPException(400, "status must be todo, done or blocked.")


@router.post("/{deal_id}/plan")
def add_plan_item(deal_id: int, body: PlanItemIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    _get_deal_row(s, deal_id, user)
    _plan_check(body)
    s.execute(text("""INSERT INTO deal_close_plan (deal_id, title, side, owner_name, due_date, note, ordinal, created_by)
                      VALUES (:d, :t, :sd, :o, :due, :n,
                              (SELECT coalesce(max(ordinal), 0) + 1 FROM deal_close_plan WHERE deal_id = :d), :u)"""),
              {"d": deal_id, "t": body.title.strip(), "sd": body.side, "o": (body.owner_name or "").strip() or None,
               "due": body.due_date, "n": (body.note or "").strip() or None, "u": user.id})
    _log(s, deal_id, user.id, "field", f"Close plan: added \u201c{body.title.strip()}\u201d")
    s.commit()
    return {"close_plan": _plan_rows(s, deal_id)}


@router.post("/{deal_id}/plan/template")
def add_plan_template(deal_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Adds the standard steps to signature, dated back from the expected close (or 30 days out)."""
    deal = _get_deal_row(s, deal_id, user)
    if s.execute(text("SELECT 1 FROM deal_close_plan WHERE deal_id = :d LIMIT 1"), {"d": deal_id}).scalar():
        raise HTTPException(400, "This deal already has a close plan — add steps one by one instead.")
    from datetime import timedelta
    close = deal["expected_close"] or (date.today() + timedelta(days=30))
    for i, (title, side, before) in enumerate(PLAN_TEMPLATE, start=1):
        due = max(date.today(), close - timedelta(days=before)) if before >= 0 else close - timedelta(days=before)
        s.execute(text("""INSERT INTO deal_close_plan (deal_id, title, side, due_date, ordinal, created_by)
                          VALUES (:d, :t, :sd, :due, :o, :u)"""),
                  {"d": deal_id, "t": title, "sd": side, "due": due, "o": i, "u": user.id})
    _log(s, deal_id, user.id, "field", "Close plan: added the standard steps to signature")
    s.commit()
    return {"close_plan": _plan_rows(s, deal_id)}


@router.patch("/{deal_id}/plan/{item_id}")
def update_plan_item(deal_id: int, item_id: int, body: PlanItemPatch, user: User = Depends(auth.get_current_user),
                     s=Depends(_session)):
    _get_deal_row(s, deal_id, user)
    _plan_check(body)
    item = s.execute(text("SELECT title, status FROM deal_close_plan WHERE id = :i AND deal_id = :d"),
                     {"i": item_id, "d": deal_id}).mappings().fetchone()
    if not item:
        raise HTTPException(404, "Milestone not found.")
    changes = body.model_dump(exclude_unset=True)
    for col in ("title", "owner_name", "note"):
        if col in changes and changes[col] is not None:
            changes[col] = changes[col].strip() or None
    if changes.get("title", "") is None:
        raise HTTPException(400, "A milestone needs a title.")
    for col, val in changes.items():
        s.execute(text(f"UPDATE deal_close_plan SET {col} = :v, updated_at = now() WHERE id = :i"), {"v": val, "i": item_id})
    if "status" in changes and changes["status"] != item["status"]:
        s.execute(text("UPDATE deal_close_plan SET done_at = CASE WHEN :st = 'done' THEN now() END WHERE id = :i"),
                  {"st": changes["status"], "i": item_id})
        label = {"done": "done", "blocked": "blocked", "todo": "reopened"}[changes["status"]]
        _log(s, deal_id, user.id, "field", f"Close plan: \u201c{changes.get('title') or item['title']}\u201d {label}")
    s.commit()
    return {"close_plan": _plan_rows(s, deal_id)}


@router.delete("/{deal_id}/plan/{item_id}")
def delete_plan_item(deal_id: int, item_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    _get_deal_row(s, deal_id, user)
    title = s.execute(text("DELETE FROM deal_close_plan WHERE id = :i AND deal_id = :d RETURNING title"),
                      {"i": item_id, "d": deal_id}).scalar()
    if title is None:
        raise HTTPException(404, "Milestone not found.")
    _log(s, deal_id, user.id, "field", f"Close plan: removed \u201c{title}\u201d")
    s.commit()
    return {"close_plan": _plan_rows(s, deal_id)}


@router.post("/{deal_id}/tasks")
def add_task(deal_id: int, body: TaskIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Creates a normal action item linked to the deal — it shows on My Tasks too."""
    deal = _get_deal_row(s, deal_id, user)
    if body.priority not in ("high", "medium", "low"):
        raise HTTPException(400, "priority must be high, medium or low")
    # Created assigned to the author; the page reassigns through PATCH /api/action-items/{id},
    # which owns the "assigned to you" email.
    task_id = s.execute(text("""INSERT INTO action_items (account_id, persona_id, title, status, priority, due_date,
                                                assigned_to_id, created_by_id, source, source_ref_id, deal_id, created_at, updated_at)
                      VALUES (:a, :p, :t, 'open', :pr, :due, :u, :u, 'deal', :d, :d, now(), now()) RETURNING id"""),
              {"a": deal["account_id"], "p": body.persona_id, "t": body.title.strip(), "pr": body.priority,
               "due": body.due_date, "u": user.id, "d": deal_id}).scalar()
    _log(s, deal_id, user.id, "field", f"Task added: {body.title.strip()}")
    s.commit()
    out = _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)
    out["task_id"] = task_id
    return out


@router.get("/{deal_id}/candidates")
def committee_candidates(deal_id: int, q: str = "", user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """People at the deal's account to add to the buying committee, best decision-makers first."""
    deal = _get_deal_row(s, deal_id, user)
    rows = s.execute(text("""
        SELECT p.id, coalesce(p.full_name, p.display_name) AS name, p.title, p.decision_authority, p.budget_authority,
               p.tier, (p.value_proposition IS NOT NULL) AS has_callprep
        FROM personas p WHERE p.account_id = :a
          AND NOT EXISTS (SELECT 1 FROM deal_stakeholders ds WHERE ds.deal_id = :d AND ds.persona_id = p.id)
          AND (:q = '' OR coalesce(p.full_name, p.display_name) ILIKE :like OR p.title ILIKE :like)
        ORDER BY (p.decision_authority ILIKE '%final%') DESC, p.hierarchy_level NULLS LAST, 2 LIMIT 25"""),
        {"a": deal["account_id"], "d": deal_id, "q": q.strip(), "like": f"%{q.strip()}%"}).mappings()
    return [dict(r) for r in rows]


@router.get("/{deal_id}/export")
def export_deal(deal_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Deal summary as Excel: overview, committee, checklist, activity, tasks."""
    from apps.sales_copilot import exports
    from openpyxl import Workbook
    d = _deal_payload(s, _get_deal_row(s, deal_id, user), full=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Overview"
    exports._sheet(ws, ["Field", "Value"], [
        ["Deal", d["name"]], ["Account", d["account_name"]], ["Stage", d["stage_label"]],
        ["Value", f"{d['value_amount']:,.0f} {d['currency']}" if d["value_amount"] is not None else ""],
        ["Expected close", d["expected_close"]], ["Next step", d["next_step"]], ["Next step due", d["next_step_due"]],
        ["Offerings", ", ".join(OFFERINGS.get(o, o) for o in d["offerings"])],
        ["Health", f"{d['health']['score']} ({d['health']['level']})" if d["health"]["score"] is not None else "closed"],
        ["Gaps", "; ".join(d["health"]["gaps"])], ["Owner", d["owner"]["name"]], ["Exported", exports._now()]], [20, 90])
    exports._sheet(wb.create_sheet("Buying committee"), ["Name", "Title", "Role", "Sentiment", "Work email", "Phone"],
                   [[x["name"], x["title"], x["role"].replace("_", " "), x["sentiment"], x["email"], x["phone"]]
                    for x in d["stakeholders"]], [26, 44, 20, 12, 30, 18])
    exports._sheet(wb.create_sheet("Checklist"), ["Stage", "Item", "Done", "Note"],
                   [[STAGE_LABEL[c["stage"]], c["label"], "yes" if c["done"] else "", c["note"]] for c in d["checklist"]],
                   [12, 44, 8, 40])
    exports._sheet(wb.create_sheet("Activity"), ["When", "Who", "Type", "What"],
                   [[str(a["created_at"])[:16], a["by"], a["kind"], a["text"]] for a in d["activity"]], [18, 20, 12, 80])
    exports._sheet(wb.create_sheet("Tasks"), ["Task", "Status", "Priority", "Due"],
                   [[t["title"], t["status"], t["priority"], str(t["due_date"] or "")[:10]] for t in d["tasks"]], [50, 12, 10, 12])
    content = exports._xlsx(wb)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{exports.safe_filename(d["name"], "xlsx")}"'})


def install(app) -> None:
    try:
        ensure_schema()
    except Exception as e:
        print(f"[deals] schema check failed: {e}")
    from apps.sales_deals import buyer_room
    app.include_router(buyer_room.router)        # before router: /{deal_id}/room is more specific
    app.include_router(buyer_room.public_router)
    app.include_router(router)
