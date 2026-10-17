"""CRM core API, M0 foundations (apps/sales_crm/README.md §2.1, §5, §6).

  /api/crm/meta               roles, business lines, settings the UI needs
  /api/crm/business-lines     list (anyone) · create / update (super_admin)
  /api/crm/team               users with role, manager and business lines (super_admin, sales_manager)
  /api/crm/users/{id}         set manager + business lines (super_admin)
  /api/crm/settings           read (anyone) · update (super_admin)
Roles themselves are still set through the existing /api/admin/users endpoints.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from apps.sales_crm import permissions
from db.connection import engine, get_session
from db.models.user import User

router = APIRouter(prefix="/api/crm", tags=["CRM core"])

SETTING_RULES = {  # key -> validator
    "fiscal_year_start_month": lambda v: isinstance(v, int) and 1 <= v <= 12,
    "default_attribution_pct": lambda v: isinstance(v, (int, float)) and 0 <= v <= 100,
    "stage_probability": lambda v: isinstance(v, dict) and all(isinstance(x, (int, float)) and 0 <= x <= 100 for x in v.values()),
    "capture_provider": lambda v: v in ("microsoft", "google"),
    "capture_allow_bodies": lambda v: isinstance(v, bool),
    "email_notifications_enabled": lambda v: isinstance(v, bool),
}

_schema_ok = False


def ensure_schema() -> None:
    global _schema_ok
    if _schema_ok:
        return
    from db.migrate import schema_managed
    if schema_managed():   # schema changes come from migrations (db/migrate.py)
        _schema_ok = True
        return
    conn = engine.raw_connection()
    try:
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '5s'")
        cur.execute((Path(__file__).parent / "schema.sql").read_text(encoding="utf-8"))
        conn.commit()
        _schema_ok = True
    finally:
        conn.close()


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _q(s, sql: str, **kw) -> List[Dict[str, Any]]:
    return [dict(r) for r in s.execute(text(sql), kw).mappings()]


def settings(s) -> Dict[str, Any]:
    return {r["key"]: r["value"] for r in _q(s, "SELECT key, value FROM crm_settings")}


def business_lines(s, active_only: bool = False) -> List[Dict[str, Any]]:
    return _q(s, "SELECT id, key, name, active, sort FROM business_lines"
                 + (" WHERE active" if active_only else "") + " ORDER BY sort, name")


# ── Meta ──────────────────────────────────────────────────────────────────────


@router.get("/meta")
def meta(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    return {"roles": [{"key": r, "label": auth.ROLE_LABELS[r]} for r in auth.ROLES],
            "business_lines": business_lines(s), "settings": settings(s),
            "me": {"id": user.id, "role": user.role, "read_only": user.role in auth.READ_ONLY_ROLES},
            "auth_enforced": auth.AUTH_ENFORCED}


# ── Business lines ────────────────────────────────────────────────────────────


class BusinessLineIn(BaseModel):
    key: Optional[str] = Field(None, pattern=r"^[a-z0-9_]{2,40}$")
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    active: Optional[bool] = None
    sort: Optional[int] = None


@router.get("/business-lines")
def list_business_lines(active_only: bool = False, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    return business_lines(s, active_only)


@router.post("/business-lines")
def create_business_line(body: BusinessLineIn, user: User = Depends(auth.require_role("super_admin")), s=Depends(_session)):
    if not body.key or not body.name:
        raise HTTPException(400, "key and name are required")
    if s.execute(text("SELECT 1 FROM business_lines WHERE key = :k"), {"k": body.key}).scalar():
        raise HTTPException(409, "A business line with this key already exists")
    bid = s.execute(text("""INSERT INTO business_lines (key, name, active, sort)
                            VALUES (:k, :n, coalesce(:a, true), coalesce(:o, 0)) RETURNING id"""),
                    {"k": body.key, "n": body.name.strip(), "a": body.active, "o": body.sort}).scalar()
    s.commit()
    auth.log_audit(s, user.id, "business_line_created", details={"key": body.key, "name": body.name})
    return _q(s, "SELECT id, key, name, active, sort FROM business_lines WHERE id = :i", i=bid)[0]


@router.patch("/business-lines/{bl_id}")
def update_business_line(bl_id: int, body: BusinessLineIn, user: User = Depends(auth.require_role("super_admin")),
                         s=Depends(_session)):
    fields = {k: v for k, v in body.model_dump(exclude_unset=True).items() if k in ("name", "active", "sort") and v is not None}
    if not fields:
        raise HTTPException(400, "Nothing to update (key cannot be changed)")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    if not s.execute(text(f"UPDATE business_lines SET {sets} WHERE id = :i RETURNING id"), {**fields, "i": bl_id}).scalar():
        raise HTTPException(404, "Business line not found")
    s.commit()
    auth.log_audit(s, user.id, "business_line_updated", details={"id": bl_id, **fields})
    return _q(s, "SELECT id, key, name, active, sort FROM business_lines WHERE id = :i", i=bl_id)[0]


# ── Team ──────────────────────────────────────────────────────────────────────


def team_rows(s, only_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
    rows = _q(s, """
        SELECT u.id, u.email, u.full_name, u.role, u.is_active, u.manager_id,
               coalesce(m.full_name, m.email) AS manager_name,
               coalesce(array_agg(ub.business_line_id) FILTER (WHERE ub.business_line_id IS NOT NULL), '{}') AS business_line_ids
        FROM users u LEFT JOIN users m ON m.id = u.manager_id
        LEFT JOIN user_business_lines ub ON ub.user_id = u.id
        WHERE (CAST(:ids AS int[]) IS NULL OR u.id = ANY(:ids))
        GROUP BY u.id, m.id ORDER BY u.role = 'partner', coalesce(u.full_name, u.email)""", ids=only_ids)
    for r in rows:
        r["role_label"] = auth.ROLE_LABELS.get(r["role"], r["role"])
    return rows


@router.get("/roles")
def roles_guide(user: User = Depends(permissions.require_roles("sales_manager", "user", "viewer"))):
    """What each role is for and what it can reach (Admin → Roles & permissions)."""
    from apps.sales_crm import role_guide
    return role_guide.guide(auth.ROLE_LABELS)


@router.get("/team")
def team(user: User = Depends(permissions.require_roles("sales_manager")), s=Depends(_session)):
    """super_admin: everyone. sales_manager: themself + their reports."""
    if permissions.is_admin(user):
        return team_rows(s)
    return team_rows(s, [user.id] + auth.team_user_ids(s, user.id))


class UserCrmIn(BaseModel):
    manager_id: Optional[int] = None
    clear_manager: bool = False
    business_line_ids: Optional[List[int]] = None


@router.patch("/users/{user_id}")
def update_user_crm(user_id: int, body: UserCrmIn, user: User = Depends(auth.require_role("super_admin")),
                    s=Depends(_session)):
    target = s.execute(text("SELECT id, manager_id FROM users WHERE id = :u"), {"u": user_id}).mappings().fetchone()
    if not target:
        raise HTTPException(404, "User not found")
    details: Dict[str, Any] = {}
    if body.clear_manager or body.manager_id is not None:
        new = None if body.clear_manager else body.manager_id
        if new is not None:
            if new == user_id:
                raise HTTPException(400, "A user cannot be their own manager")
            if not s.execute(text("SELECT 1 FROM users WHERE id = :m"), {"m": new}).scalar():
                raise HTTPException(400, "Manager not found")
            if new in auth.team_user_ids(s, user_id):
                raise HTTPException(400, "That would create a reporting loop")
        s.execute(text("UPDATE users SET manager_id = :m WHERE id = :u"), {"m": new, "u": user_id})
        details["manager_id"] = {"old": target["manager_id"], "new": new}
    if body.business_line_ids is not None:
        valid = {r[0] for r in s.execute(text("SELECT id FROM business_lines"))}
        bad = [b for b in body.business_line_ids if b not in valid]
        if bad:
            raise HTTPException(400, f"Unknown business line id(s): {bad}")
        s.execute(text("DELETE FROM user_business_lines WHERE user_id = :u"), {"u": user_id})
        for b in dict.fromkeys(body.business_line_ids):
            s.execute(text("INSERT INTO user_business_lines (user_id, business_line_id) VALUES (:u, :b)"), {"u": user_id, "b": b})
        details["business_line_ids"] = body.business_line_ids
    s.commit()
    if details:
        auth.log_audit(s, user.id, "user_crm_updated", target_user_id=user_id, details=details)
    return team_rows(s, [user_id])[0]


# ── Settings ──────────────────────────────────────────────────────────────────


class SettingsIn(BaseModel):
    values: Dict[str, Any]


@router.get("/settings")
def get_settings(user: User = Depends(auth.get_current_user), s=Depends(_session)):
    return settings(s)


@router.put("/settings")
def put_settings(body: SettingsIn, user: User = Depends(auth.require_role("super_admin")), s=Depends(_session)):
    for k, v in body.values.items():
        rule = SETTING_RULES.get(k)
        if not rule:
            raise HTTPException(400, f"Unknown setting: {k}")
        if not rule(v):
            raise HTTPException(400, f"Invalid value for {k}")
    for k, v in body.values.items():
        s.execute(text("""INSERT INTO crm_settings (key, value, updated_by, updated_at) VALUES (:k, CAST(:v AS jsonb), :u, now())
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()"""),
                  {"k": k, "v": json.dumps(v), "u": user.id})
    s.commit()
    from apps.sales_crm import forecast
    forecast._prob_cache = (0.0, {})
    auth.log_audit(s, user.id, "crm_settings_updated", details=body.values)
    return settings(s)


def _background() -> bool:
    """CRM_BACKGROUND=0 skips the stale-intro, snapshot and capture workers (tests, one-off scripts)."""
    import os
    return os.getenv("CRM_BACKGROUND", "1") != "0"


def install(app) -> None:
    try:
        ensure_schema()
    except Exception as e:
        print(f"[crm] schema check failed: {e}")
    from apps.sales_crm import activities, forecast, introductions
    app.middleware("http")(permissions.role_guard)
    app.include_router(router)
    app.include_router(introductions.router)
    app.include_router(introductions.partner_router)
    if _background():
        introductions.start_background()
    app.include_router(forecast.router)
    app.include_router(activities.router)
    from apps.sales_crm.capture import api as capture_api, engine as capture_engine
    app.include_router(capture_api.router)
    from apps.sales_crm import notify, records, views
    app.include_router(notify.router)
    app.include_router(records.router)
    app.include_router(views.router)
    if _background():
        notify.start_background()
    if _background():
        capture_engine.start_background()
    if _background():
        forecast.start_background()
    if not auth.AUTH_ENFORCED:
        print("[crm] WARNING: AUTH_ENFORCED is false — every request runs as the super admin and role "
              "rules (viewer read-only, partner isolation, manager scope) are NOT applied. Set AUTH_ENFORCED=true in production.")
