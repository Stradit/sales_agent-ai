"""Introductions: connectors, warm intros, conversion to deals, attribution (README §2.2, M1).

Internal API  /api/crm/connectors · /api/crm/introductions[...] · /api/crm/attribution
Partner API   /api/partner/*  — a partner (role 'partner', linked via connectors.user_id)
              only ever sees introductions where they are the connector, and a limited
              view of them: no deal value, no internal notes, no other connectors.

Status flow: proposed → requested → accepted → intro_made → meeting_held → converted
             (+ declined / stale). Manual moves (check_transition): one stage at a time; forward
             needs that stage's criteria (STAGE_NEEDS); back, reopen and stale need a reason.
             'converted' is only reachable through /convert, from meeting_held, which creates or
             links a deal and sets the attribution % (intro → connector → setting, 50).
             Logging a meeting (activities.py) may still move an intro straight to meeting_held:
             a meeting that happened is evidence, not a skipped stage.
Nothing here uses AI requests.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from apps.sales_crm import permissions
from db.connection import engine, get_session
from db.models.user import User

router = APIRouter(prefix="/api/crm", tags=["CRM introductions"])
partner_router = APIRouter(prefix="/api/partner", tags=["Partner portal"])

STATUSES = ["proposed", "requested", "accepted", "intro_made", "meeting_held", "converted", "declined", "stale"]
OPEN_STATUSES = STATUSES[:5]
STATUS_LABEL = {"proposed": "Proposed", "requested": "Requested", "accepted": "Accepted", "intro_made": "Intro made",
                "meeting_held": "Meeting held", "converted": "Converted", "declined": "Declined", "stale": "Stale"}
STATUS_TS = {"requested": "requested_at", "accepted": "accepted_at", "intro_made": "intro_made_at",
             "meeting_held": "meeting_at", "converted": "converted_at"}
CONNECTOR_KINDS = ["partner", "advisor", "employee", "customer", "other"]
STALE_DAYS = 30
# What an introduction needs before it may ENTER a stage (manual moves).
STAGE_NEEDS = {"accepted": ("contact",), "intro_made": ("account", "contact"), "meeting_held": ("account", "contact")}
NEED_LABEL = {"account": "link it to an account", "contact": "choose the contact being introduced"}


def missing_for(target: str, has_account: bool, has_contact: bool) -> List[str]:
    have = {"account": has_account, "contact": has_contact}
    return [NEED_LABEL[n] for n in STAGE_NEEDS.get(target, ()) if not have[n]]


def check_transition(cur: str, new: str, *, has_account: bool, has_contact: bool,
                     reason: Optional[str] = None, reopen_to: Optional[str] = None) -> None:
    """Raise HTTPException(400) unless a MANUAL status change from cur to new is allowed."""
    reason = (reason or "").strip()
    if new not in STATUSES:
        raise HTTPException(400, "Unknown status.")
    if new == "converted":
        raise HTTPException(400, "Use Convert to create or link the deal.")
    if cur == "converted":
        raise HTTPException(400, "A converted introduction can't change status — change the deal instead.")
    if new in ("declined", "stale"):
        if not reason:
            raise HTTPException(400, f"Please give a reason for marking it {STATUS_LABEL[new].lower()}.")
        return
    if cur in ("declined", "stale"):                       # reopening
        if not reason:
            raise HTTPException(400, "Please give a reason for reopening this introduction.")
        expected = reopen_to if reopen_to in OPEN_STATUSES else "proposed"
        if new != expected:
            raise HTTPException(400, f"Reopen it at {STATUS_LABEL[expected]}, the stage it was closed from.")
        return
    step = STATUSES.index(new) - STATUSES.index(cur)
    if abs(step) > 1:
        raise HTTPException(400, "Move one stage at a time.")
    if step == -1 and not reason:
        raise HTTPException(400, "Please give a reason for moving back a stage.")
    if step == 1:
        missing = missing_for(new, has_account, has_contact)
        if missing:
            raise HTTPException(400, f"Before moving to {STATUS_LABEL[new]}: " + " and ".join(missing) + ".")


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _q(s, sql: str, **kw) -> List[Dict[str, Any]]:
    return [dict(r) for r in s.execute(text(sql), kw).mappings()]


def _num(v):
    return float(v) if v is not None else None


def default_attribution(s) -> float:
    v = s.execute(text("SELECT value FROM crm_settings WHERE key = 'default_attribution_pct'")).scalar()
    return float(v) if v is not None else 50.0


# ── Loading & views ───────────────────────────────────────────────────────────

BASE_SQL = """
    SELECT i.*, c.name AS connector_name, c.kind AS connector_kind, c.organisation AS connector_org, c.email AS connector_email,
           c.user_id AS connector_user_id, c.default_attribution_pct AS connector_default_pct,
           a.display_name AS account_name, coalesce(p.full_name, p.display_name) AS persona_name, p.title AS persona_title,
           bl.name AS business_line_name, coalesce(u.full_name, u.email) AS owner_name,
           d.name AS deal_name, d.stage AS deal_stage, d.value_amount AS deal_value, d.currency AS deal_currency
    FROM introductions i JOIN connectors c ON c.id = i.connector_id
    LEFT JOIN accounts a ON a.id = i.account_id LEFT JOIN personas p ON p.id = i.persona_id
    LEFT JOIN business_lines bl ON bl.id = i.business_line_id LEFT JOIN users u ON u.id = i.owner_user_id
    LEFT JOIN deals d ON d.id = i.deal_id"""


def internal_view(r: Dict[str, Any]) -> Dict[str, Any]:
    pct = _num(r["attribution_pct"])
    value = _num(r["deal_value"])
    now = datetime.now(timezone.utc)
    return {
        "id": r["id"], "status": r["status"], "status_label": STATUS_LABEL[r["status"]],
        "connector": {"id": r["connector_id"], "name": r["connector_name"], "kind": r["connector_kind"],
                      "organisation": r["connector_org"], "email": r["connector_email"], "has_login": r["connector_user_id"] is not None,
                      "default_attribution_pct": _num(r["connector_default_pct"])},
        "account_id": r["account_id"], "account_name": r["account_name"], "persona_id": r["persona_id"],
        "persona_name": r["persona_name"], "persona_title": r["persona_title"],
        "submitted": {"account_name": r["submitted_account_name"], "contact_name": r["submitted_contact_name"],
                      "contact_email": r["submitted_contact_email"], "contact_title": r["submitted_contact_title"]},
        "needs_triage": r["account_id"] is None,
        "business_line_id": r["business_line_id"], "business_line_name": r["business_line_name"],
        "owner_user_id": r["owner_user_id"], "owner_name": r["owner_name"],
        "context": r["context"], "next_step": r["next_step"], "closed_reason": r["closed_reason"],
        "requested_at": r["requested_at"], "accepted_at": r["accepted_at"], "intro_made_at": r["intro_made_at"],
        "meeting_at": r["meeting_at"], "converted_at": r["converted_at"],
        "deal": {"id": r["deal_id"], "name": r["deal_name"], "stage": r["deal_stage"], "value_amount": value,
                 "currency": r["deal_currency"]} if r["deal_id"] else None,
        "attribution_pct": pct,
        "attributed_value": round(value * pct / 100, 2) if value is not None and pct is not None else None,
        "days_in_status": (now - r["updated_at"]).days, "created_at": r["created_at"], "updated_at": r["updated_at"],
    }


# Partners may correct what they submitted until StradIT starts acting on it.
PARTNER_EDITABLE = ("proposed", "requested")


def partner_view(r: Dict[str, Any]) -> Dict[str, Any]:
    """Field-level rule (README §5.2): stage + dates only — no value, no attribution, no internal notes.
    A partner's own submission is shown as they typed it, never as matched in StradIT's database."""
    deal_stage = r["deal_stage"]
    submitted = r["submitted_account_name"] is not None
    return {
        "id": r["id"], "status": r["status"], "status_label": STATUS_LABEL[r["status"]],
        "account_name": r["submitted_account_name"] or r["account_name"],
        "contact_name": r["submitted_contact_name"] or r["persona_name"],
        "contact_title": (r["submitted_contact_title"] if submitted else None) or r["persona_title"],
        "contact_email": r["submitted_contact_email"] if submitted else None,
        "editable": submitted and r["status"] in PARTNER_EDITABLE,
        "context": r["context"], "stradit_contact": r["owner_name"],
        "requested_at": r["requested_at"], "intro_made_at": r["intro_made_at"], "meeting_at": r["meeting_at"],
        "converted_at": r["converted_at"], "created_at": r["created_at"], "updated_at": r["updated_at"],
        "opportunity_stage": {"won": "Won", "lost": "Closed"}.get(deal_stage, "In progress") if deal_stage else None,
    }


def _events(s, intro_id: int, partner: bool = False) -> List[Dict[str, Any]]:
    sql = """SELECT e.id, e.kind, e.from_status, e.to_status, e.note, e.partner_visible, e.at,
                    coalesce(u.full_name, u.email) AS by, u.role AS by_role
             FROM introduction_events e LEFT JOIN users u ON u.id = e.by_user
             WHERE e.introduction_id = :i"""
    if partner:
        sql += " AND e.partner_visible"
    rows = _q(s, sql + " ORDER BY e.at, e.id", i=intro_id)
    for e in rows:
        e["from_label"] = STATUS_LABEL.get(e["from_status"]) if e["from_status"] else None
        e["to_label"] = STATUS_LABEL.get(e["to_status"]) if e["to_status"] else None
        if partner:
            e["by"] = "You" if e.pop("by_role") == "partner" else "StradIT"
            e.pop("partner_visible", None)
        else:
            e.pop("by_role", None)
    return rows


def _event(s, intro_id: int, user_id: Optional[int], kind: str, note: Optional[str] = None,
           from_status: Optional[str] = None, to_status: Optional[str] = None, partner_visible: bool = True) -> None:
    eid = s.execute(text("""INSERT INTO introduction_events (introduction_id, kind, from_status, to_status, note, partner_visible, by_user)
                            VALUES (:i, :k, :f, :t, :n, :pv, :u) RETURNING id"""),
                    {"i": intro_id, "k": kind, "f": from_status, "t": to_status, "n": (note or None) and note[:2000],
                     "pv": partner_visible, "u": user_id}).scalar()
    s.execute(text("UPDATE introductions SET updated_at = now() WHERE id = :i"), {"i": intro_id})
    if partner_visible and kind in ("status", "converted", "note"):
        _notify_partner(s, intro_id, eid, user_id, kind, to_status, note)


PARTNER_STATUS_WORDS = {"requested": "StradIT has asked for the introduction", "accepted": "the contact accepted",
                        "intro_made": "the introduction has been made", "meeting_held": "StradIT has met the contact",
                        "converted": "it became an opportunity", "declined": "it won't go ahead", "stale": "it's on hold"}


def _intro_brief(s, intro_id: int):
    return s.execute(text("""SELECT i.id, i.owner_user_id, i.account_id, c.user_id AS partner_user_id, c.name AS connector_name,
                                    coalesce(a.display_name, i.submitted_account_name) AS account,
                                    coalesce(p.full_name, p.display_name, i.submitted_contact_name) AS contact
                             FROM introductions i JOIN connectors c ON c.id = i.connector_id
                             LEFT JOIN accounts a ON a.id = i.account_id LEFT JOIN personas p ON p.id = i.persona_id
                             WHERE i.id = :i"""), {"i": intro_id}).mappings().fetchone()


def _notify_partner(s, intro_id: int, event_id: int, actor: Optional[int], kind: str, to_status: Optional[str], note: Optional[str]) -> None:
    from apps.sales_crm import notify
    b = _intro_brief(s, intro_id)
    if not b or not b["partner_user_id"] or b["partner_user_id"] == actor:
        return
    who = f"{b['contact'] or 'your contact'} at {b['account'] or 'the company'}"
    if kind == "note":
        subject, lines = f"Update on your introduction: {who}", [f"The StradIT team added an update on {who}:", f"“{(note or '')[:500]}”"]
    else:
        subject = f"Your introduction moved on: {who}"
        lines = [f"Good news on {who}: {PARTNER_STATUS_WORDS.get(to_status, 'its status changed')}."]
    notify.enqueue(s, b["partner_user_id"], "partner_intro_update", subject, lines, link="/partner",
                   dedupe_key=f"intro_event:{event_id}", cta_label="Open the partner portal")


def _notify_internal(s, intro_id: int, user_ids, kind: str, subject: str, lines, key: str) -> None:
    from apps.sales_crm import notify
    for uid in dict.fromkeys(u for u in user_ids if u):
        notify.enqueue(s, uid, kind, subject, lines, link=f"/introductions?intro={intro_id}", dedupe_key=f"{key}:{uid}",
                       cta_label="Open the introduction")


def _internal_scope_sql() -> str:
    # Unlinked partner submissions (account_id NULL) are visible to every internal user for triage.
    return "(i.account_id = ANY(:acl) OR i.account_id IS NULL)"


def _load_internal(s, user, intro_id: int) -> Dict[str, Any]:
    if getattr(user, "role", None) == "partner" and auth.AUTH_ENFORCED:
        raise HTTPException(403, "Not authorized")
    rows = _q(s, BASE_SQL + f" WHERE i.id = :i AND {_internal_scope_sql()}", i=intro_id, acl=auth.account_scope(s, user))
    if not rows:
        raise HTTPException(404, "Introduction not found.")
    return rows[0]


# ── Connectors ────────────────────────────────────────────────────────────────


class ConnectorIn(BaseModel):
    kind: Optional[str] = None
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    organisation: Optional[str] = Field(None, max_length=200)
    email: Optional[str] = Field(None, max_length=255)
    default_attribution_pct: Optional[float] = Field(None, ge=0, le=100)
    notes: Optional[str] = Field(None, max_length=2000)
    active: Optional[bool] = None
    user_id: Optional[int] = None          # link a partner login (super_admin only)
    unlink_user: bool = False


def _connector_rows(s, only_id: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = _q(s, """
        SELECT c.*, coalesce(u.full_name, u.email) AS login_name, u.email AS login_email,
               count(i.id) AS intros,
               count(i.id) FILTER (WHERE i.status = 'converted') AS converted,
               count(i.id) FILTER (WHERE i.status = ANY(:open)) AS open
        FROM connectors c LEFT JOIN users u ON u.id = c.user_id LEFT JOIN introductions i ON i.connector_id = c.id
        WHERE (CAST(:cid AS bigint) IS NULL OR c.id = :cid)
        GROUP BY c.id, u.id ORDER BY c.active DESC, c.name""", open=OPEN_STATUSES, cid=only_id)
    for r in rows:
        r["default_attribution_pct"] = _num(r["default_attribution_pct"])
    return rows


def _check_connector_user(s, user, uid: int) -> None:
    if not permissions.is_admin(user):
        raise HTTPException(403, "Only an admin can link a login to a connector.")
    role = s.execute(text("SELECT role FROM users WHERE id = :u"), {"u": uid}).scalar()
    if role != "partner":
        raise HTTPException(400, "Only users with the Partner / Advisor role can be linked to a connector.")
    if s.execute(text("SELECT 1 FROM connectors WHERE user_id = :u"), {"u": uid}).scalar():
        raise HTTPException(409, "That login is already linked to another connector.")


@router.get("/connectors")
def list_connectors(user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")), s=Depends(_session)):
    return {"connectors": _connector_rows(s), "kinds": CONNECTOR_KINDS, "default_attribution_pct": default_attribution(s)}


@router.post("/connectors")
def create_connector(body: ConnectorIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    if not body.name or body.kind not in CONNECTOR_KINDS:
        raise HTTPException(400, "name and a valid kind are required")
    if body.user_id is not None:
        _check_connector_user(s, user, body.user_id)
    cid = s.execute(text("""INSERT INTO connectors (kind, name, organisation, email, user_id, default_attribution_pct, notes, created_by)
                            VALUES (:k, :n, :o, :e, :u, :p, :no, :by) RETURNING id"""),
                    {"k": body.kind, "n": body.name.strip(), "o": body.organisation, "e": body.email, "u": body.user_id,
                     "p": body.default_attribution_pct, "no": body.notes, "by": user.id}).scalar()
    s.commit()
    return _connector_rows(s, cid)[0]


@router.patch("/connectors/{connector_id}")
def update_connector(connector_id: int, body: ConnectorIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    if not s.execute(text("SELECT 1 FROM connectors WHERE id = :c"), {"c": connector_id}).scalar():
        raise HTTPException(404, "Connector not found")
    fields = body.model_dump(exclude_unset=True)
    if "kind" in fields and fields["kind"] not in CONNECTOR_KINDS:
        raise HTTPException(400, "Invalid kind")
    if fields.get("user_id") is not None:
        _check_connector_user(s, user, fields["user_id"])
    if fields.pop("unlink_user", False):
        if not permissions.is_admin(user):
            raise HTTPException(403, "Only an admin can unlink a login.")
        fields["user_id"] = None
    fields = {k: v for k, v in fields.items() if k in ("kind", "name", "organisation", "email", "default_attribution_pct",
                                                        "notes", "active", "user_id")}
    if fields:
        s.execute(text("UPDATE connectors SET " + ", ".join(f"{k} = :{k}" for k in fields) + " WHERE id = :c"),
                  {**fields, "c": connector_id})
        s.commit()
        if "user_id" in fields:
            auth.log_audit(s, user.id, "connector_login_linked", target_user_id=fields["user_id"],
                           details={"connector_id": connector_id})
    return _connector_rows(s, connector_id)[0]


# ── Introductions (internal) ──────────────────────────────────────────────────


class IntroIn(BaseModel):
    connector_id: int
    account_id: int
    persona_id: Optional[int] = None
    business_line_id: Optional[int] = None
    owner_user_id: Optional[int] = None
    status: str = "proposed"
    context: Optional[str] = Field(None, max_length=4000)
    next_step: Optional[str] = Field(None, max_length=500)


class IntroPatch(BaseModel):
    status: Optional[str] = None
    account_id: Optional[int] = None
    persona_id: Optional[int] = None
    business_line_id: Optional[int] = None
    owner_user_id: Optional[int] = None
    context: Optional[str] = Field(None, max_length=4000)
    next_step: Optional[str] = Field(None, max_length=500)
    attribution_pct: Optional[float] = Field(None, ge=0, le=100)
    closed_reason: Optional[str] = Field(None, max_length=500)
    reason: Optional[str] = Field(None, max_length=500)   # moving back, reopening, marking stale, changing the contact


class NoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    partner_visible: bool = False


class EventVisibilityIn(BaseModel):
    partner_visible: bool


class ConvertIn(BaseModel):
    deal_id: Optional[int] = None                 # link an existing deal instead of creating one
    name: Optional[str] = Field(None, max_length=200)
    value_amount: Optional[float] = Field(None, ge=0)
    currency: str = Field("USD", max_length=3)
    expected_close: Optional[str] = None
    business_line_id: Optional[int] = None
    attribution_pct: Optional[float] = Field(None, ge=0, le=100)


def _check_refs(s, user, account_id: Optional[int], persona_id: Optional[int], business_line_id: Optional[int],
                owner_user_id: Optional[int]) -> None:
    if account_id is not None and account_id not in auth.account_scope(s, user):
        raise HTTPException(404, "Account not found.")
    if persona_id is not None:
        pa = s.execute(text("SELECT account_id FROM personas WHERE id = :p"), {"p": persona_id}).scalar()
        if pa is None or (account_id is not None and pa != account_id):
            raise HTTPException(400, "That contact is not at this account.")
    if business_line_id is not None and not s.execute(
            text("SELECT 1 FROM business_lines WHERE id = :b AND active"), {"b": business_line_id}).scalar():
        raise HTTPException(400, "Unknown or inactive business line.")
    if owner_user_id is not None and s.execute(text("SELECT role FROM users WHERE id = :u AND is_active"),
                                               {"u": owner_user_id}).scalar() in (None, "partner", "viewer"):
        raise HTTPException(400, "Owner must be an active StradIT user who can edit.")


@router.get("/people")
def people_picker(account_id: int, q: str = "", user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")),
                  s=Depends(_session)):
    """Contact picker for one account (names and titles only — no contact details)."""
    if account_id not in auth.account_scope(s, user):
        raise HTTPException(404, "Account not found.")
    return _q(s, """SELECT p.id, coalesce(p.full_name, p.display_name) AS name, p.title
                    FROM personas p WHERE p.account_id = :a AND coalesce(p.full_name, p.display_name) ILIKE :q
                    ORDER BY p.hierarchy_level NULLS LAST, 2 LIMIT 20""", a=account_id, q=f"%{q.strip()}%")


# How much a past introduction says about a connector's reach (#7 warm-path finder)
_PATH_WEIGHT = {"converted": 40, "meeting_held": 35, "intro_made": 30, "accepted": 25, "requested": 12,
                "proposed": 8, "stale": 4, "declined": 2}


@router.get("/warm-paths")
def warm_paths(account_id: int, persona_id: Optional[int] = None,
               user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")), s=Depends(_session)):
    """Who can introduce me (#7)? Ranked people with a link to this account (or one contact there):
    connectors who introduced people there before, connectors who work there, and teammates who
    have been talking to people there. Uses only data already in the CRM."""
    _check_refs(s, user, account_id, persona_id, None, None)
    acct = s.execute(text("SELECT display_name, legal_name, coalesce(aliases, '{}') AS aliases FROM accounts WHERE id = :a"),
                     {"a": account_id}).mappings().fetchone()
    paths: Dict[str, Dict[str, Any]] = {}

    def path(key, **kw):
        return paths.setdefault(key, {"score": 0, "reasons": [], **kw})

    # 1. Connectors with introductions at this account
    for r in _q(s, """SELECT c.id, c.name, c.kind, c.organisation, i.status, i.persona_id,
                             coalesce(p.full_name, p.display_name) AS persona_name,
                             greatest(i.updated_at, i.created_at) AS at
                      FROM introductions i JOIN connectors c ON c.id = i.connector_id
                      LEFT JOIN personas p ON p.id = i.persona_id
                      WHERE i.account_id = :a AND c.active ORDER BY at DESC""", a=account_id):
        pth = path(f"c{r['id']}", type="connector", id=r["id"], name=r["name"], kind=r["kind"],
                   organisation=r["organisation"], intros=0, last_at=None)
        pth["intros"] += 1
        pth["last_at"] = pth["last_at"] or r["at"]
        same = persona_id is not None and r["persona_id"] == persona_id
        pth["score"] += _PATH_WEIGHT.get(r["status"], 5) + (30 if same else 0)
        what = ("Introduced this contact before" if same
                else f"Introduced {r['persona_name']}" if r["persona_name"] else "Made an introduction here")
        pth["reasons"].append(f"{what} ({r['status'].replace('_', ' ')})")
    # 2. Connectors whose organisation is this account
    names = [n for n in [acct["display_name"], acct["legal_name"], *(acct["aliases"] or [])] if n and len(n) >= 3]
    if names:
        for r in _q(s, """SELECT id, name, kind, organisation FROM connectors
                          WHERE active AND organisation IS NOT NULL AND lower(organisation) = ANY(:n)""",
                    n=[n.lower() for n in names]):
            pth = path(f"c{r['id']}", type="connector", id=r["id"], name=r["name"], kind=r["kind"],
                       organisation=r["organisation"], intros=0, last_at=None)
            pth["score"] += 25
            pth["reasons"].insert(0, f"Works at {r['organisation']}")
    # 3. Teammates who have been in touch with people there (last 12 months, team-visible activity)
    for r in _q(s, """SELECT u.id, coalesce(u.full_name, u.email) AS name, count(DISTINCT a.id) AS n, max(a.occurred_at) AS last_at,
                             bool_or(l2.object_id IS NOT NULL) AS with_contact
                      FROM activities a
                      JOIN activity_links l ON l.activity_id = a.id AND l.object_type = 'account' AND l.object_id = :a
                      LEFT JOIN activity_links l2 ON l2.activity_id = a.id AND l2.object_type = 'persona' AND l2.object_id = :p
                      JOIN users u ON u.id = a.owner_user_id AND u.is_active
                      WHERE a.type IN ('email', 'meeting', 'call') AND a.visibility = 'team'
                        AND a.occurred_at > now() - interval '365 days' AND u.id <> :me
                      GROUP BY u.id, 2""", a=account_id, p=persona_id, me=user.id):
        pth = path(f"u{r['id']}", type="teammate", id=r["id"], name=r["name"], intros=0, last_at=r["last_at"])
        pth["score"] += min(30, 5 * r["n"]) + (30 if r["with_contact"] else 0)
        pth["reasons"].append(f"{r['n']} email/meeting/call{'s' if r['n'] != 1 else ''} with people there"
                              + (" — including this contact" if r["with_contact"] else ""))
    ranked = sorted(paths.values(), key=lambda x: (-x["score"], str(x["name"])))[:8]
    for x in ranked:
        x["strength"] = "strong" if x["score"] >= 40 else "good" if x["score"] >= 20 else "weak"
        x["reasons"] = x["reasons"][:3]
    return {"account": acct["display_name"], "paths": ranked}


# ── Wanted introductions (#8): the team's wishlist; every partner sees the open items ──────────

class WantedIn(BaseModel):
    account_id: int
    role_wanted: str = Field(..., min_length=2, max_length=200)
    note: Optional[str] = Field(None, max_length=1000)
    business_line_id: Optional[int] = None


class WantedPatch(BaseModel):
    role_wanted: Optional[str] = Field(None, min_length=2, max_length=200)
    note: Optional[str] = Field(None, max_length=1000)
    status: Optional[str] = Field(None, pattern="^(open|closed)$")


WANTED_SQL = """SELECT w.id, w.account_id, a.display_name AS account_name, w.role_wanted, w.note, w.status,
                       w.business_line_id, bl.name AS business_line_name, w.created_at, w.closed_at, w.created_by,
                       coalesce(u.full_name, u.email) AS created_by_name,
                       (SELECT count(*) FROM introductions i WHERE i.wanted_id = w.id) AS claims,
                       (SELECT string_agg(DISTINCT c.name, ', ') FROM introductions i JOIN connectors c ON c.id = i.connector_id
                         WHERE i.wanted_id = w.id) AS claimed_by
                FROM crm_wanted_intros w JOIN accounts a ON a.id = w.account_id
                LEFT JOIN business_lines bl ON bl.id = w.business_line_id LEFT JOIN users u ON u.id = w.created_by"""


@router.get("/wanted")
def wanted_list(status: str = "open", user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")),
                s=Depends(_session)):
    sql = WANTED_SQL + " WHERE w.account_id = ANY(:acl)" + ("" if status == "all" else " AND w.status = :st")
    return {"wanted": _q(s, sql + " ORDER BY w.status DESC, w.created_at DESC", acl=auth.account_scope(s, user), st=status)}


@router.post("/wanted")
def wanted_create(body: WantedIn, user: User = Depends(permissions.require_roles("sales_manager", "user")), s=Depends(_session)):
    _check_refs(s, user, body.account_id, None, body.business_line_id, None)
    wid = s.execute(text("""INSERT INTO crm_wanted_intros (account_id, role_wanted, note, business_line_id, created_by)
                            VALUES (:a, :r, :n, :b, :u) RETURNING id"""),
                    {"a": body.account_id, "r": body.role_wanted.strip(), "n": (body.note or "").strip() or None,
                     "b": body.business_line_id, "u": user.id}).scalar()
    s.commit()
    return _q(s, WANTED_SQL + " WHERE w.id = :i", i=wid)[0]


@router.patch("/wanted/{wanted_id}")
def wanted_update(wanted_id: int, body: WantedPatch, user: User = Depends(permissions.require_roles("sales_manager", "user")),
                  s=Depends(_session)):
    rows = _q(s, WANTED_SQL + " WHERE w.id = :i AND w.account_id = ANY(:acl)", i=wanted_id, acl=auth.account_scope(s, user))
    if not rows:
        raise HTTPException(404, "Wanted introduction not found.")
    if not (permissions.is_admin(user) or user.role == "sales_manager" or rows[0]["created_by"] == user.id):
        raise HTTPException(403, "Only the person who added it, a manager or an admin can change it.")
    changes = body.model_dump(exclude_unset=True)
    if "role_wanted" in changes:
        s.execute(text("UPDATE crm_wanted_intros SET role_wanted = :v WHERE id = :i"),
                  {"v": changes["role_wanted"].strip(), "i": wanted_id})
    if "note" in changes:
        s.execute(text("UPDATE crm_wanted_intros SET note = :v WHERE id = :i"),
                  {"v": (changes["note"] or "").strip() or None, "i": wanted_id})
    if "status" in changes:
        s.execute(text("""UPDATE crm_wanted_intros SET status = :v, closed_at = CASE WHEN :v = 'closed' THEN now() END
                          WHERE id = :i"""), {"v": changes["status"], "i": wanted_id})
    s.commit()
    return _q(s, WANTED_SQL + " WHERE w.id = :i", i=wanted_id)[0]


@router.get("/introductions")
def list_intros(status: Optional[str] = None, connector_id: Optional[int] = None, account_id: Optional[int] = None,
                business_line_id: Optional[int] = None, mine: bool = False, q: Optional[str] = None,
                user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")), s=Depends(_session)):
    sql = BASE_SQL + f" WHERE {_internal_scope_sql()}"
    params: Dict[str, Any] = {"acl": auth.account_scope(s, user)}
    for col, val in (("status", status), ("connector_id", connector_id), ("account_id", account_id),
                     ("business_line_id", business_line_id)):
        if val:
            sql += f" AND i.{col} = :{col}"
            params[col] = val
    if mine:
        sql += " AND i.owner_user_id = :me"; params["me"] = user.id
    if q:
        sql += """ AND (c.name ILIKE :q OR a.display_name ILIKE :q OR coalesce(p.full_name, p.display_name) ILIKE :q
                        OR i.submitted_account_name ILIKE :q OR i.submitted_contact_name ILIKE :q)"""
        params["q"] = f"%{q}%"
    rows = [internal_view(r) for r in _q(s, sql + " ORDER BY i.updated_at DESC LIMIT 1000", **params)]
    summary = {st: sum(1 for r in rows if r["status"] == st) for st in STATUSES}
    return {"introductions": rows, "summary": summary, "statuses": [{"key": k, "label": STATUS_LABEL[k]} for k in STATUSES]}


@router.post("/introductions")
def create_intro(body: IntroIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    if body.status not in OPEN_STATUSES:
        raise HTTPException(400, "A new introduction must start in an open status.")
    missing = missing_for(body.status, body.account_id is not None, body.persona_id is not None)
    if missing:
        raise HTTPException(400, f"To start at {STATUS_LABEL[body.status]}: " + " and ".join(missing) + ".")
    if not s.execute(text("SELECT 1 FROM connectors WHERE id = :c AND active"), {"c": body.connector_id}).scalar():
        raise HTTPException(400, "Unknown or inactive connector.")
    owner = body.owner_user_id or user.id
    _check_refs(s, user, body.account_id, body.persona_id, body.business_line_id, body.owner_user_id)
    try:
        iid = s.execute(text("""
            INSERT INTO introductions (connector_id, account_id, persona_id, business_line_id, owner_user_id, status,
                                       context, next_step, created_by)
            VALUES (:c, :a, :p, :bl, :o, :st, :ctx, :ns, :u) RETURNING id"""),
            {"c": body.connector_id, "a": body.account_id, "p": body.persona_id, "bl": body.business_line_id,
             "o": owner, "st": body.status, "ctx": body.context, "ns": body.next_step, "u": user.id}).scalar()
    except Exception as e:  # unique open path
        s.rollback()
        if "introductions_path" in str(e):
            raise HTTPException(409, "This connector already has an open introduction to this contact.")
        raise
    if body.status in STATUS_TS:
        s.execute(text(f"UPDATE introductions SET {STATUS_TS[body.status]} = now() WHERE id = :i"), {"i": iid})
    _event(s, iid, user.id, "created", to_status=body.status)
    if owner != user.id:
        b = _intro_brief(s, iid)
        _notify_internal(s, iid, [owner], "intro_assigned", f"Introduction assigned to you: {b['contact'] or b['account']}",
                         [f"{user.full_name or user.email} assigned you an introduction from {b['connector_name']} to "
                          f"{b['contact'] or 'a contact'} at {b['account']}."], key=f"intro_assigned:{iid}:{owner}")
    s.commit()
    return get_intro(iid, user, s)


@router.get("/introductions/attribution")
def attribution(business_line_id: Optional[int] = None, since: Optional[str] = None,
                user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")), s=Depends(_session)):
    """Sourced pipeline / won revenue per connector (README §2.2 'Attribution')."""
    # USD value: the stored conversion, else the amount only when the deal is already in USD.
    # Non-USD deals without an exchange rate are left out (and reported), never summed at face value.
    usd = "(CASE WHEN d.amount_usd IS NOT NULL THEN d.amount_usd WHEN coalesce(d.currency, 'USD') = 'USD' THEN d.value_amount END)"
    # A converted intro always stores its %; fall back to the connector, then the org default, so a
    # cleared value can't silently drop the deal from attributed totals.
    pct = "coalesce(i.attribution_pct, c.default_attribution_pct, CAST(:default_pct AS numeric))"
    rows = _q(s, """
        SELECT c.id, c.name, c.kind, c.organisation,
               count(i.id) AS intros,
               count(i.id) FILTER (WHERE i.status = ANY(:open)) AS open,
               count(i.id) FILTER (WHERE i.status IN ('intro_made','meeting_held','converted')) AS intros_made,
               count(i.id) FILTER (WHERE i.status = 'converted') AS converted,
               coalesce(sum({usd}) FILTER (WHERE d.stage NOT IN ('won','lost')), 0) AS sourced_pipeline,
               coalesce(sum({usd}) FILTER (WHERE d.stage = 'won'), 0) AS sourced_won,
               coalesce(sum({usd} * {pct} / 100) FILTER (WHERE d.stage NOT IN ('won','lost')), 0) AS attributed_pipeline,
               coalesce(sum({usd} * {pct} / 100) FILTER (WHERE d.stage = 'won'), 0) AS attributed_won,
               count(d.id) FILTER (WHERE d.value_amount IS NOT NULL AND {usd} IS NULL) AS unconverted_deals,
               coalesce(array_remove(array_agg(DISTINCT d.currency) FILTER (WHERE d.value_amount IS NOT NULL AND {usd} IS NULL), NULL),
                        '{{}}') AS currencies
        FROM connectors c
        JOIN introductions i ON i.connector_id = c.id AND (i.account_id = ANY(:acl) OR i.account_id IS NULL)
             AND (CAST(:bl AS int) IS NULL OR i.business_line_id = :bl)
             AND (CAST(:since AS date) IS NULL OR i.created_at >= CAST(:since AS date))
        LEFT JOIN deals d ON d.id = i.deal_id
        GROUP BY c.id ORDER BY sourced_won DESC, sourced_pipeline DESC, intros DESC""".format(usd=usd, pct=pct),
        open=OPEN_STATUSES, acl=auth.account_scope(s, user), bl=business_line_id, since=since,
        default_pct=default_attribution(s))
    for r in rows:
        for k in ("sourced_pipeline", "sourced_won", "attributed_pipeline", "attributed_won"):
            r[k] = round(float(r[k]), 2)
        r["conversion_rate"] = round(r["converted"] / r["intros"] * 100, 1) if r["intros"] else 0.0
    cur = sorted({c for r in rows for c in r["currencies"]})
    skipped = sum(r.pop("unconverted_deals") for r in rows)
    return {"connectors": rows, "currencies": cur,
            "note": (f"All values in USD. {skipped} deal{'s' if skipped != 1 else ''} in " + ", ".join(cur)
                     + " ha" + ("ve" if skipped != 1 else "s") + " no exchange rate yet and " + ("are" if skipped != 1 else "is")
                     + " not included — add the rate under Forecast → Exchange rates.") if skipped else None}


def _xlsx_response(content: bytes, name: str) -> Response:
    from apps.sales_copilot import exports
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{exports.safe_filename(name, "xlsx")}"'})


@router.get("/introductions/export")
def export_intros(status: Optional[str] = None, connector_id: Optional[int] = None,
                  user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")), s=Depends(_session)):
    from apps.sales_copilot import exports
    data = list_intros(status=status, connector_id=connector_id, account_id=None, business_line_id=None, mine=False,
                       q=None, user=user, s=s)["introductions"]
    rows = [[r["id"], r["status_label"], r["connector"]["name"], r["connector"]["kind"], r["account_name"] or r["submitted"]["account_name"],
             r["persona_name"] or r["submitted"]["contact_name"], r["business_line_name"], r["owner_name"],
             r["deal"]["name"] if r["deal"] else "", r["deal"]["stage"] if r["deal"] else "",
             r["deal"]["value_amount"] if r["deal"] else None, r["attribution_pct"], r["attributed_value"],
             str(r["created_at"])[:10], str(r["updated_at"])[:10], r["context"] or ""] for r in data]
    content = exports.rows_xlsx("Introductions", ["ID", "Status", "Connector", "Kind", "Account", "Contact", "Business line",
                                                  "Owner", "Deal", "Deal stage", "Deal value", "Attribution %",
                                                  "Attributed value", "Created", "Updated", "Context"], rows,
                                [6, 14, 26, 11, 26, 24, 18, 20, 30, 12, 12, 12, 14, 11, 11, 60])
    return _xlsx_response(content, "introductions")


@router.get("/introductions/attribution/export")
def export_attribution(user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")), s=Depends(_session)):
    from apps.sales_copilot import exports
    data = attribution(business_line_id=None, since=None, user=user, s=s)["connectors"]
    rows = [[r["name"], r["kind"], r["organisation"], r["intros"], r["open"], r["intros_made"], r["converted"],
             r["conversion_rate"], r["sourced_pipeline"], r["attributed_pipeline"], r["sourced_won"], r["attributed_won"],
             ", ".join(r["currencies"])] for r in data]
    content = exports.rows_xlsx("Attribution", ["Connector", "Kind", "Organisation", "Intros", "Open", "Intros made",
                                                "Converted", "Conversion %", "Sourced pipeline", "Attributed pipeline",
                                                "Sourced won", "Attributed won", "Currencies"], rows,
                                [28, 11, 24, 8, 8, 11, 10, 12, 16, 18, 14, 16, 12])
    return _xlsx_response(content, "introduction-attribution")


@router.post("/introductions/maintenance")
def run_maintenance(user: User = Depends(auth.require_role("super_admin")), s=Depends(_session)):
    return {"marked_stale": mark_stale(s), "follow_ups": nudge_requested(s), "housekeeping": housekeeping(s)}


@router.get("/introductions/{intro_id}")
def get_intro(intro_id: int, user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer")),
              s=Depends(_session)):
    row = _load_internal(s, user, intro_id)
    out = internal_view(row)
    out["events"] = _events(s, intro_id)
    has_contact = row["persona_id"] is not None or bool(row["submitted_contact_name"])
    nxt = STATUSES[STATUSES.index(row["status"]) + 1] if row["status"] in OPEN_STATUSES[:-1] else None
    out["next_status"] = nxt
    out["next_missing"] = missing_for(nxt, row["account_id"] is not None, has_contact) if nxt else []
    out["can_convert"] = row["status"] == "meeting_held" and row["account_id"] is not None
    out["reopen_to"] = _reopen_target(s, intro_id, row["status"]) if row["status"] in ("declined", "stale") else None
    return out


def _reopen_target(s, intro_id: int, closed_status: str) -> str:
    prev = s.execute(text("""SELECT from_status FROM introduction_events
                             WHERE introduction_id = :i AND kind = 'status' AND to_status = :c
                             ORDER BY at DESC, id DESC LIMIT 1"""), {"i": intro_id, "c": closed_status}).scalar()
    return prev if prev in OPEN_STATUSES else "proposed"


@router.patch("/introductions/{intro_id}")
def update_intro(intro_id: int, body: IntroPatch, user: User = Depends(permissions.require_write), s=Depends(_session)):
    cur = _load_internal(s, user, intro_id)
    ch = body.model_dump(exclude_unset=True)
    new_status = ch.pop("status", None)
    reason = (ch.pop("reason", None) or "").strip() or None
    if new_status and new_status != cur["status"]:      # validate before writing anything
        check_transition(cur["status"], new_status,
                         has_account=cur["account_id"] is not None or ch.get("account_id") is not None,
                         has_contact=(ch["persona_id"] if "persona_id" in ch else cur["persona_id"]) is not None
                         or bool(cur["submitted_contact_name"]),
                         reason=ch.get("closed_reason") if new_status == "declined" else reason,
                         reopen_to=_reopen_target(s, intro_id, cur["status"]) if cur["status"] in ("declined", "stale") else None)
    contact_change = "persona_id" in ch and ch["persona_id"] != cur["persona_id"]
    if contact_change and cur["persona_id"] is not None and cur["status"] not in ("proposed",) and not reason:
        raise HTTPException(400, "Please give a reason for changing the contact on an introduction in progress.")
    _check_refs(s, user, ch.get("account_id"), ch.get("persona_id") if "persona_id" in ch else None,
                ch.get("business_line_id"), ch.get("owner_user_id"))
    if "persona_id" in ch and ch["persona_id"] is not None and "account_id" not in ch:
        pa = s.execute(text("SELECT account_id FROM personas WHERE id = :p"), {"p": ch["persona_id"]}).scalar()
        if cur["account_id"] is not None and pa != cur["account_id"]:
            raise HTTPException(400, "That contact is not at this account.")
    if cur["deal_id"] and "account_id" in ch and ch["account_id"] != cur["account_id"]:
        raise HTTPException(400, "This introduction is already linked to a deal; its account can't change.")
    for col, val in ch.items():
        s.execute(text(f"UPDATE introductions SET {col} = :v, updated_at = now() WHERE id = :i"), {"v": val, "i": intro_id})
    if ch.get("owner_user_id") and ch["owner_user_id"] not in (cur["owner_user_id"], user.id):
        b = _intro_brief(s, intro_id)
        _notify_internal(s, intro_id, [ch["owner_user_id"]], "intro_assigned",
                         f"Introduction assigned to you: {b['contact'] or b['account']}",
                         [f"{user.full_name or user.email} assigned you the introduction from {b['connector_name']} to "
                          f"{b['contact'] or 'a contact'} at {b['account']}."], key=f"intro_assigned:{intro_id}:{ch['owner_user_id']}")
    if "account_id" in ch and cur["account_id"] is None and ch["account_id"] is not None:
        _event(s, intro_id, user.id, "field", note="Linked to an account in StradIT's system", partner_visible=False)
    if "attribution_pct" in ch:
        _event(s, intro_id, user.id, "field", note=f"Attribution set to {ch['attribution_pct']}%", partner_visible=False)
    if contact_change:
        new_name = s.execute(text("SELECT coalesce(full_name, display_name) FROM personas WHERE id = :p"),
                             {"p": ch["persona_id"]}).scalar() if ch["persona_id"] else None
        msg = (f"Contact changed from {cur['persona_name']} to {new_name or 'nobody'}" if cur["persona_id"]
               else f"Contact set to {new_name}")
        _event(s, intro_id, user.id, "field", note=msg + (f" — {reason}" if reason else ""), partner_visible=False)
    if "next_step" in ch and (ch["next_step"] or None) != (cur["next_step"] or None) and ch["next_step"]:
        stage = STATUS_LABEL[new_status or cur["status"]]
        _event(s, intro_id, user.id, "field", note=f"Next step ({stage}): {ch['next_step']}", partner_visible=False)
    if new_status and new_status != cur["status"]:
        s.execute(text("UPDATE introductions SET status = :st, updated_at = now() WHERE id = :i"), {"st": new_status, "i": intro_id})
        if new_status in STATUS_TS:
            s.execute(text(f"UPDATE introductions SET {STATUS_TS[new_status]} = coalesce({STATUS_TS[new_status]}, now()) WHERE id = :i"),
                      {"i": intro_id})
        _event(s, intro_id, user.id, "status", note=ch.get("closed_reason") if new_status == "declined" else None,
               from_status=cur["status"], to_status=new_status)
        if reason and new_status != "declined":
            # Internal: why it went back / was reopened / was parked. Partners only see the status move.
            what = ("Reopened" if cur["status"] in ("declined", "stale") else
                    "Marked stale" if new_status == "stale" else "Moved back")
            _event(s, intro_id, user.id, "note", note=f"{what}: {reason}", partner_visible=False)
        if new_status == "stale":
            s.execute(text("UPDATE introductions SET closed_reason = :r WHERE id = :i"), {"r": reason, "i": intro_id})
        if cur["status"] in ("declined", "stale"):
            s.execute(text("UPDATE introductions SET closed_reason = NULL WHERE id = :i"), {"i": intro_id})
    s.commit()
    return get_intro(intro_id, user, s)


@router.post("/introductions/{intro_id}/notes")
def add_intro_note(intro_id: int, body: NoteIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    _load_internal(s, user, intro_id)
    _event(s, intro_id, user.id, "note", note=body.text.strip(), partner_visible=body.partner_visible)
    s.commit()
    return get_intro(intro_id, user, s)


@router.patch("/introductions/{intro_id}/events/{event_id}")
def set_event_visibility(intro_id: int, event_id: int, body: EventVisibilityIn, user: User = Depends(permissions.require_write),
                         s=Depends(_session)):
    """Share a note with the partner, or make it internal again. Only notes can change; status moves
    and system events keep their visibility."""
    _load_internal(s, user, intro_id)
    kind = s.execute(text("SELECT kind FROM introduction_events WHERE id = :e AND introduction_id = :i"),
                     {"e": event_id, "i": intro_id}).scalar()
    if kind is None:
        raise HTTPException(404, "Event not found.")
    if kind != "note":
        raise HTTPException(400, "Only notes can be shared or made internal.")
    s.execute(text("UPDATE introduction_events SET partner_visible = :v WHERE id = :e"), {"v": body.partner_visible, "e": event_id})
    s.commit()
    return get_intro(intro_id, user, s)


@router.post("/introductions/{intro_id}/convert")
def convert_intro(intro_id: int, body: ConvertIn, user: User = Depends(permissions.require_write), s=Depends(_session)):
    from apps.sales_deals import api as deals_api
    cur = _load_internal(s, user, intro_id)
    if cur["status"] == "converted":
        raise HTTPException(400, "Already converted.")
    if cur["status"] in ("declined", "stale"):
        raise HTTPException(400, "Reopen the introduction before converting it.")
    if cur["status"] != "meeting_held":
        raise HTTPException(400, "Convert an introduction once the meeting has been held.")
    if cur["account_id"] is None:
        raise HTTPException(400, "Link the introduction to an account first.")
    if body.deal_id:
        d = deals_api._get_deal_row(s, body.deal_id, user)
        if d["account_id"] != cur["account_id"]:
            raise HTTPException(400, "That deal is at a different account.")
        if d.get("introduction_id") and d["introduction_id"] != intro_id:
            raise HTTPException(409, "That deal is already sourced by another introduction.")
        deal_id = d["id"]
    else:
        name = (body.name or "").strip() or f"{cur['account_name']} — intro via {cur['connector_name']}"
        created = deals_api.create_deal(deals_api.DealIn(
            account_id=cur["account_id"], name=name[:200], value_amount=body.value_amount, currency=body.currency,
            expected_close=body.expected_close or None, business_line_id=body.business_line_id or cur["business_line_id"],
            next_step=cur["next_step"], stage="intro"), user=user, s=s)
        deal_id = created["id"]
    pct = body.attribution_pct
    if pct is None:
        pct = _num(cur["attribution_pct"])
    if pct is None:
        pct = _num(cur["connector_default_pct"])
    if pct is None:
        pct = default_attribution(s)
    s.execute(text("UPDATE deals SET source = 'introduction', introduction_id = :i, updated_at = now() WHERE id = :d"),
              {"i": intro_id, "d": deal_id})
    if cur["persona_id"]:
        s.execute(text("""INSERT INTO deal_stakeholders (deal_id, persona_id, role) VALUES (:d, :p, 'influencer')
                          ON CONFLICT (deal_id, persona_id) DO NOTHING"""), {"d": deal_id, "p": cur["persona_id"]})
    deals_api._log(s, deal_id, user.id, "created", f"Sourced by an introduction from {cur['connector_name']} ({pct:g}% attribution)")
    s.execute(text("""UPDATE introductions SET status = 'converted', converted_at = now(), deal_id = :d,
                      attribution_pct = :p, updated_at = now() WHERE id = :i"""), {"d": deal_id, "p": pct, "i": intro_id})
    _event(s, intro_id, user.id, "converted", from_status=cur["status"], to_status="converted",
           note="Became an opportunity")
    deals_api._sync_auto_checks(s, deal_id, user.id)
    s.commit()
    out = get_intro(intro_id, user, s)
    out["deal_id"] = deal_id
    return out


# ── Partner portal API ────────────────────────────────────────────────────────


def _connector_for(s, user, preview_connector_id: Optional[int]) -> Dict[str, Any]:
    """The partner's own connector. Admins may preview any connector's view with ?connector_id=."""
    if preview_connector_id and permissions.is_admin(user):
        rows = _q(s, "SELECT id, name, organisation, kind FROM connectors WHERE id = :c", c=preview_connector_id)
    else:
        if auth.AUTH_ENFORCED and user.role != "partner":
            raise HTTPException(403, "The partner portal is for partner logins.")
        rows = _q(s, "SELECT id, name, organisation, kind FROM connectors WHERE user_id = :u AND active", u=user.id)
    if not rows:
        raise HTTPException(403, "Your login isn't linked to a partner record yet — contact your StradIT representative.")
    return rows[0]


class PartnerIntroIn(BaseModel):
    account_name: str = Field(..., min_length=2, max_length=200)
    contact_name: str = Field(..., min_length=2, max_length=200)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_title: Optional[str] = Field(None, max_length=200)
    context: str = Field(..., min_length=10, max_length=4000)
    wanted_id: Optional[int] = None      # answering an item on the "Wanted introductions" list


class PartnerIntroPatch(BaseModel):
    account_name: Optional[str] = Field(None, min_length=2, max_length=200)
    contact_name: Optional[str] = Field(None, min_length=2, max_length=200)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_title: Optional[str] = Field(None, max_length=200)
    context: Optional[str] = Field(None, min_length=10, max_length=4000)


class PartnerNoteIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@partner_router.get("/me")
def partner_me(connector_id: Optional[int] = None, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    c = _connector_for(s, user, connector_id)
    return {"connector": c, "user": {"name": user.full_name or user.email}}


@partner_router.get("/wanted")
def partner_wanted(connector_id: Optional[int] = None, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """The open wishlist, as partners see it: company, the role wanted and the team's note — nothing else."""
    c = _connector_for(s, user, connector_id)
    rows = _q(s, """SELECT w.id, a.display_name AS account_name, w.role_wanted, w.note, bl.name AS business_line_name,
                           w.created_at,
                           EXISTS (SELECT 1 FROM introductions i WHERE i.wanted_id = w.id AND i.connector_id = :c) AS submitted
                    FROM crm_wanted_intros w JOIN accounts a ON a.id = w.account_id
                    LEFT JOIN business_lines bl ON bl.id = w.business_line_id
                    WHERE w.status = 'open' ORDER BY w.created_at DESC LIMIT 100""", c=c["id"])
    return {"wanted": rows}


@partner_router.get("/introductions")
def partner_list(connector_id: Optional[int] = None, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    c = _connector_for(s, user, connector_id)
    rows = [partner_view(r) for r in _q(s, BASE_SQL + " WHERE i.connector_id = :c ORDER BY i.updated_at DESC", c=c["id"])]
    return {"introductions": rows, "statuses": [{"key": k, "label": STATUS_LABEL[k]} for k in STATUSES]}


@partner_router.get("/introductions/{intro_id}")
def partner_get(intro_id: int, connector_id: Optional[int] = None, user: User = Depends(auth.get_current_user),
                s=Depends(_session)):
    c = _connector_for(s, user, connector_id)
    rows = _q(s, BASE_SQL + " WHERE i.id = :i AND i.connector_id = :c", i=intro_id, c=c["id"])
    if not rows:
        raise HTTPException(404, "Introduction not found.")
    out = partner_view(rows[0])
    out["events"] = _events(s, intro_id, partner=True)
    return out


@partner_router.post("/introductions")
def partner_submit(body: PartnerIntroIn, connector_id: Optional[int] = None, user: User = Depends(auth.get_current_user),
                   s=Depends(_session)):
    """A partner proposes an intro by name. We match the account/contact quietly; the partner never
    learns what is in StradIT's database — they only see what they submitted."""
    c = _connector_for(s, user, connector_id)
    name = body.account_name.strip()
    wanted = None
    if body.wanted_id is not None:
        wanted = s.execute(text("SELECT id, account_id, role_wanted FROM crm_wanted_intros WHERE id = :w AND status = 'open'"),
                           {"w": body.wanted_id}).mappings().fetchone()
        if not wanted:
            raise HTTPException(400, "That wanted introduction is no longer open.")
    acct = wanted["account_id"] if wanted else s.execute(text("""SELECT id FROM accounts WHERE lower(display_name) = lower(:n) OR lower(legal_name) = lower(:n)
                             OR lower(:n) = ANY(SELECT lower(x) FROM unnest(coalesce(aliases, '{}')) x) LIMIT 1"""),
                     {"n": name}).scalar()
    persona = None
    if acct and body.contact_email:
        persona = s.execute(text("SELECT id FROM personas WHERE account_id = :a AND lower(email) = lower(:e) LIMIT 1"),
                            {"a": acct, "e": body.contact_email.strip()}).scalar()
    if acct and persona and s.execute(text("""SELECT 1 FROM introductions WHERE connector_id = :c AND account_id = :a
                                              AND persona_id = :p AND status NOT IN ('declined','stale')"""),
                                      {"c": c["id"], "a": acct, "p": persona}).scalar():
        raise HTTPException(409, "You already have an open introduction for this contact.")
    iid = s.execute(text("""
        INSERT INTO introductions (connector_id, account_id, persona_id, submitted_account_name, submitted_contact_name,
                                   submitted_contact_email, submitted_contact_title, status, context, created_by, wanted_id)
        VALUES (:c, :a, :p, :an, :cn, :ce, :ct, 'proposed', :ctx, :u, :w) RETURNING id"""),
        {"c": c["id"], "a": acct, "p": persona, "an": name, "cn": body.contact_name.strip(),
         "ce": (body.contact_email or "").strip() or None, "ct": body.contact_title, "ctx": body.context.strip(),
         "u": user.id, "w": wanted["id"] if wanted else None}).scalar()
    _event(s, iid, user.id, "created", to_status="proposed",
           note="Submitted by partner" + (f" for the wanted introduction: {wanted['role_wanted']}" if wanted else ""))
    from apps.sales_crm import notify
    _notify_internal(s, iid, notify.internal_triagers(s, acct), "triage_needed",
                     f"New partner introduction: {body.contact_name.strip()} at {name}" + ("" if acct else " (needs triage)")
                     + (" — answers your wishlist" if wanted else ""),
                     [f"{c['name']} submitted an introduction to {body.contact_name.strip()} at {name}.",
                      "It isn't linked to an account yet — open it and pick the account." if not acct else
                      "It was matched to the account automatically; assign an owner and move it along.",
                      f"Their note: “{body.context.strip()[:400]}”"], key=f"triage:{iid}")
    s.commit()
    return partner_get(iid, connector_id, user, s)


@partner_router.patch("/introductions/{intro_id}")
def partner_edit(intro_id: int, body: PartnerIntroPatch, connector_id: Optional[int] = None,
                 user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """A partner corrects their own submission while it is still Submitted / Requested. Only the
    submitted_* fields and context change; a link StradIT already made to an account or contact is
    kept, and the team is told what changed so they can re-check it."""
    if connector_id:
        raise HTTPException(403, "Introductions can't be edited in preview.")
    c = _connector_for(s, user, None)
    rows = _q(s, BASE_SQL + " WHERE i.id = :i AND i.connector_id = :c", i=intro_id, c=c["id"])
    if not rows:
        raise HTTPException(404, "Introduction not found.")
    cur = rows[0]
    if cur["submitted_account_name"] is None:
        raise HTTPException(400, "This introduction was logged by the StradIT team — send them an update instead.")
    if cur["status"] not in PARTNER_EDITABLE:
        raise HTTPException(400, "StradIT is already working on this introduction, so its details are locked — send an update instead.")
    fields = {"account_name": "submitted_account_name", "contact_name": "submitted_contact_name",
              "contact_email": "submitted_contact_email", "contact_title": "submitted_contact_title", "context": "context"}
    label = {"account_name": "company", "contact_name": "contact name", "contact_email": "contact email",
             "contact_title": "contact title", "context": "note"}
    new: Dict[str, Any] = {}
    for k, v in body.model_dump(exclude_unset=True).items():
        v = (v or "").strip() or None
        if k in ("account_name", "contact_name", "context") and not v:
            raise HTTPException(400, f"The {label[k]} can't be empty.")
        if v != cur[fields[k]]:
            new[k] = v
    if not new:
        return partner_get(intro_id, None, user, s)
    acct, persona = cur["account_id"], cur["persona_id"]
    if acct is None:   # still in triage: match the edited details quietly, like a new submission
        name = new.get("account_name", cur["submitted_account_name"])
        acct = s.execute(text("""SELECT id FROM accounts WHERE lower(display_name) = lower(:n) OR lower(legal_name) = lower(:n)
                                 OR lower(:n) = ANY(SELECT lower(x) FROM unnest(coalesce(aliases, '{}')) x) LIMIT 1"""),
                         {"n": name}).scalar()
    email = new.get("contact_email", cur["submitted_contact_email"])
    if acct and persona is None and email:
        persona = s.execute(text("SELECT id FROM personas WHERE account_id = :a AND lower(email) = lower(:e) LIMIT 1"),
                            {"a": acct, "e": email}).scalar()
    if persona and persona != cur["persona_id"] and s.execute(text(
            """SELECT 1 FROM introductions WHERE connector_id = :c AND persona_id = :p AND id <> :i
               AND status NOT IN ('declined','stale')"""), {"c": c["id"], "p": persona, "i": intro_id}).scalar():
        raise HTTPException(409, "You already have an open introduction for this contact.")
    sets = ", ".join(f"{fields[k]} = :{k}" for k in new)
    s.execute(text(f"UPDATE introductions SET {sets}, account_id = :acct, persona_id = :per, updated_at = now() WHERE id = :i"),
              {**new, "acct": acct, "per": persona, "i": intro_id})
    changed = [label[k] for k in new]
    _event(s, intro_id, user.id, "field", note="Details updated: " + ", ".join(changed), partner_visible=True)
    linked = cur["account_id"] is not None
    lines = [f"{c['name']} edited their introduction to {cur['submitted_contact_name']} at {cur['submitted_account_name']}:"]
    for k, v in new.items():
        old = cur[fields[k]]
        lines.append(f"{label[k].capitalize()}: “{(old or '—')[:200]}” → “{(v or '—')[:200]}”")
    if linked and ({"account_name", "contact_name", "contact_email"} & new.keys()):
        lines.append("It's already linked in the CRM — check the linked account and contact still match.")
    from apps.sales_crm import notify
    _notify_internal(s, intro_id, [cur["owner_user_id"]] if cur["owner_user_id"] else notify.internal_triagers(s, acct),
                     "partner_note", f"{c['name']} edited an introduction: {new.get('contact_name', cur['submitted_contact_name'])}",
                     lines, key=f"partner_edit:{intro_id}:{s.execute(text('SELECT max(id) FROM introduction_events WHERE introduction_id = :i'), {'i': intro_id}).scalar()}")
    s.commit()
    return partner_get(intro_id, None, user, s)


@partner_router.post("/introductions/{intro_id}/notes")
def partner_note(intro_id: int, body: PartnerNoteIn, connector_id: Optional[int] = None,
                 user: User = Depends(auth.get_current_user), s=Depends(_session)):
    c = _connector_for(s, user, connector_id)
    if not s.execute(text("SELECT 1 FROM introductions WHERE id = :i AND connector_id = :c"), {"i": intro_id, "c": c["id"]}).scalar():
        raise HTTPException(404, "Introduction not found.")
    _event(s, intro_id, user.id, "note", note=body.text.strip(), partner_visible=True)
    from apps.sales_crm import notify
    b = _intro_brief(s, intro_id)
    _notify_internal(s, intro_id, [b["owner_user_id"]] if b["owner_user_id"] else notify.internal_triagers(s, b["account_id"]),
                     "partner_note", f"{c['name']} added an update: {b['contact'] or b['account']}",
                     [f"{c['name']} wrote about {b['contact'] or 'their introduction'} at {b['account']}:",
                      f"“{body.text.strip()[:500]}”"], key=f"partner_note:{intro_id}:{s.execute(text('SELECT max(id) FROM introduction_events WHERE introduction_id = :i'), {'i': intro_id}).scalar()}")
    s.commit()
    return partner_get(intro_id, connector_id, user, s)


# ── Stale job ─────────────────────────────────────────────────────────────────


# Reminders while an intro waits on the connector: well before it goes stale (STALE_DAYS)
FOLLOWUP_DAYS = (5, 10)


def nudge_requested(s) -> int:
    """Email the owner when an introduction has been 'requested' for 5 and again for 10 days.
    Each reminder is sent once (dedupe key includes the request date, so a new request starts over)."""
    rows = s.execute(text("""
        SELECT i.id, i.owner_user_id, floor(extract(epoch FROM now() - i.requested_at) / 86400)::int AS age,
               i.requested_at::date AS asked
        FROM introductions i
        WHERE i.status = 'requested' AND i.owner_user_id IS NOT NULL AND i.requested_at IS NOT NULL
          AND i.requested_at <= now() - make_interval(days => :first)"""), {"first": FOLLOWUP_DAYS[0]}).fetchall()
    sent = 0
    for iid, owner, age, asked in rows:
        step = max(d for d in FOLLOWUP_DAYS if age >= d)          # 5 or 10: only the latest reminder due
        b = _intro_brief(s, iid)
        if not b:
            continue
        who = b["contact"] or "the contact"
        _notify_internal(s, iid, [owner], "intro_followup",
                         f"Follow up with {b['connector_name']}: introduction to {who}",
                         [f"{b['connector_name']} was asked {age} days ago to introduce you to {who} at {b['account']}, "
                          "and it's still waiting.",
                          f"Send {b['connector_name']} a short nudge, or mark the introduction declined or stale "
                          f"if it isn't happening. It goes stale automatically after {STALE_DAYS} days without movement."],
                         key=f"followup:{iid}:{asked.isoformat()}:{step}")
        sent += 1
    s.commit()
    return sent


def mark_stale(s) -> int:
    rows = s.execute(text(f"""
        WITH old AS (SELECT id, status FROM introductions
                     WHERE status = ANY(:open) AND updated_at < now() - interval '{STALE_DAYS} days' FOR UPDATE)
        UPDATE introductions i SET status = 'stale', updated_at = now() FROM old WHERE i.id = old.id
        RETURNING old.id, old.status"""), {"open": OPEN_STATUSES}).fetchall()
    for iid, prev in rows:
        s.execute(text("""INSERT INTO introduction_events (introduction_id, kind, from_status, to_status, note)
                          VALUES (:i, 'status', :f, 'stale', :n)"""),
                  {"i": iid, "f": prev, "n": f"No movement for {STALE_DAYS} days"})
        b = _intro_brief(s, iid)
        if b and b["owner_user_id"]:
            _notify_internal(s, iid, [b["owner_user_id"]], "intro_stale", f"Introduction went stale: {b['contact'] or b['account']}",
                             [f"The introduction from {b['connector_name']} to {b['contact'] or 'a contact'} at {b['account']} "
                              f"had no movement for {STALE_DAYS} days and is now marked stale.",
                              "Reopen it if it's still alive, or decline it with a reason."], key=f"stale:{iid}")
    s.commit()
    return len(rows)


def housekeeping(s) -> dict:
    """Data clean-up that used to run as a side effect of every app start (schema.sql, search
    install). Now it runs here, on the 6-hourly job, and logs what it removed."""
    from apps.sales_search.api import LOG_RETENTION_DAYS
    # Activities whose every linked record was deleted (the links cascade; the activity doesn't).
    # The 1-hour margin keeps it clear of an activity being created right now.
    orphans = s.execute(text("""DELETE FROM activities a WHERE a.created_at < now() - interval '1 hour'
                                  AND NOT EXISTS (SELECT 1 FROM activity_links l WHERE l.activity_id = a.id)""")).rowcount
    old_searches = s.execute(text("DELETE FROM search_log WHERE created_at < now() - make_interval(days => :d)"),
                             {"d": LOG_RETENTION_DAYS}).rowcount
    s.commit()
    return {"orphan_activities": orphans, "old_search_log": old_searches}


def _stale_loop() -> None:
    while True:
        try:
            conn = engine.connect()
            try:
                if conn.execute(text("SELECT pg_try_advisory_lock(84211)")).scalar():
                    s = get_session()
                    try:
                        n = mark_stale(s)
                        if n:
                            print(f"[crm] marked {n} introduction(s) stale")
                        f = nudge_requested(s)
                        if f:
                            print(f"[crm] queued {f} introduction follow-up reminder(s)")
                        h = housekeeping(s)
                        if any(h.values()):
                            print(f"[crm] housekeeping removed {h}")
                    finally:
                        s.close()
                    conn.execute(text("SELECT pg_advisory_unlock(84211)"))
            finally:
                conn.close()
        except Exception as e:
            print(f"[crm] stale job failed: {e}")
        time.sleep(6 * 3600)


def start_background() -> None:
    threading.Thread(target=_stale_loop, name="crm-intro-stale", daemon=True).start()


# ── Partner email preference (the partner can't reach /api/crm/*) ──────────────


class PartnerPrefIn(BaseModel):
    email_updates: bool


@partner_router.get("/notifications")
def partner_notifications(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    from apps.sales_crm import notify
    on = notify._prefs(s, user.id).get("partner_intro_update", True)
    return {"email_updates": on, "email": user.email, "enabled_org_wide": notify.enabled(s)}


@partner_router.put("/notifications")
def set_partner_notifications(body: PartnerPrefIn, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    import json
    s.execute(text("""INSERT INTO crm_notification_prefs (user_id, settings) VALUES (:u, CAST(:v AS jsonb))
                      ON CONFLICT (user_id) DO UPDATE SET settings = crm_notification_prefs.settings || EXCLUDED.settings,
                      updated_at = now()"""), {"u": user.id, "v": json.dumps({"partner_intro_update": body.email_updates})})
    s.commit()
    return partner_notifications(user, s)
