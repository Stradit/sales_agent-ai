"""/api/crm/capture/* — connect a Microsoft 365 mailbox + calendar, per-user settings, admin view.

  GET    /status                       provider configured? my connection, settings, org policy
  POST   /connect/microsoft            → {auth_url}; the browser then goes to Microsoft
  GET    /callback/microsoft           OAuth redirect target (no bearer header; authenticated by the sealed state)
  PATCH  /settings                     my capture settings
  POST   /sync                         sync my connection now
  DELETE /microsoft?purge=true|false   disconnect (tokens deleted); purge also removes what was captured
  GET    /admin                        every connection's status (super_admin)
"""

import json
import os
import secrets
import threading
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from apps.sales_crm import permissions
from apps.sales_crm.capture import crypto, engine as cap_engine, microsoft
from db.connection import get_session
from db.models.user import User

router = APIRouter(prefix="/api/crm/capture", tags=["CRM activity capture"])
STATE_TTL = 600
INTERNAL = ("sales_manager", "user")


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _conn(s, user_id: int):
    r = s.execute(text("""SELECT id, provider, account_email, status, error, settings, last_sync_at, last_stats, next_sync_at, created_at
                          FROM capture_connections WHERE user_id = :u AND provider = 'microsoft'"""), {"u": user_id}).mappings().fetchone()
    return dict(r) if r else None


def _allow_bodies(s) -> bool:
    return bool(s.execute(text("SELECT value FROM crm_settings WHERE key = 'capture_allow_bodies'")).scalar())


@router.get("/status")
def status(user: User = Depends(permissions.require_roles(*INTERNAL)), s=Depends(_session)):
    c = _conn(s, user.id)
    captured = s.execute(text("""SELECT count(*) FILTER (WHERE type = 'email'), count(*) FILTER (WHERE type = 'meeting')
                                 FROM activities WHERE owner_user_id = :u AND source IN ('outlook','outlook_calendar')"""),
                         {"u": user.id}).fetchone()
    return {"provider": "microsoft", "configured": microsoft.configured(), "connection": c,
            "captured": {"emails": captured[0], "meetings": captured[1]},
            "org": {"allow_bodies": _allow_bodies(s)}, "redirect_uri": microsoft.config()["redirect_uri"],
            "is_admin": permissions.is_admin(user)}


@router.post("/connect/microsoft")
def connect(user: User = Depends(permissions.require_roles(*INTERNAL)), s=Depends(_session)):
    if not microsoft.configured():
        raise HTTPException(503, "Microsoft 365 isn't set up yet — an admin needs to add MS_CLIENT_ID, MS_CLIENT_SECRET and MS_TENANT_ID.")
    verifier, challenge = microsoft.new_pkce()
    state = crypto.seal_text(json.dumps({"u": user.id, "v": verifier, "n": secrets.token_hex(8)}))
    return {"auth_url": microsoft.authorize_url(state, challenge, login_hint=user.email)}


def _back(result: str, msg: str = "") -> RedirectResponse:
    from urllib.parse import quote
    return RedirectResponse(f"/email-sync?result={result}" + (f"&msg={quote(msg[:200])}" if msg else ""), status_code=303)


@router.get("/callback/microsoft", include_in_schema=False)
def callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None,
             error_description: Optional[str] = None):
    if error:
        return _back("error", error_description or error)
    if not code or not state:
        return _back("error", "Microsoft didn't return a sign-in code.")
    try:
        st = json.loads(crypto.unseal_text(state, ttl=STATE_TTL))
    except Exception:
        return _back("error", "The sign-in link expired — please try again.")
    s = get_session()
    try:
        user_row = s.execute(text("SELECT id, role, is_active FROM users WHERE id = :u"), {"u": st["u"]}).fetchone()
        if not user_row or not user_row[2] or (auth.AUTH_ENFORCED and user_row[1] not in ("super_admin",) + INTERNAL):
            return _back("error", "This account can't connect a mailbox.")
        try:
            tokens = microsoft.exchange_code(code, st["v"])
            g = microsoft.Graph(tokens)
            try:
                me = g.me()
            finally:
                g.close()
        except Exception as e:
            return _back("error", f"Microsoft sign-in failed: {e}")
        mailbox = (me.get("mail") or me.get("userPrincipalName") or "").lower()
        s.execute(text("""
            INSERT INTO capture_connections (user_id, provider, account_email, scopes, token_encrypted, status, error, next_sync_at)
            VALUES (:u, 'microsoft', :m, :sc, :t, 'active', NULL, now())
            ON CONFLICT (user_id, provider) DO UPDATE SET account_email = EXCLUDED.account_email, scopes = EXCLUDED.scopes,
              token_encrypted = EXCLUDED.token_encrypted, status = 'active', error = NULL, next_sync_at = now(),
              cursors = CASE WHEN capture_connections.account_email = EXCLUDED.account_email THEN capture_connections.cursors ELSE '{}' END"""),
            {"u": st["u"], "m": mailbox, "sc": tokens.get("scope", "").split(), "t": crypto.seal(tokens)})
        s.commit()
        auth.log_audit(s, st["u"], "capture_connected", details={"provider": "microsoft", "mailbox": mailbox})
        cid = s.execute(text("SELECT id FROM capture_connections WHERE user_id = :u AND provider = 'microsoft'"), {"u": st["u"]}).scalar()
    finally:
        s.close()
    threading.Thread(target=cap_engine.sync_connection, args=(cid,), name="capture-first-sync", daemon=True).start()
    return _back("connected")


class CaptureSettings(BaseModel):
    capture_email: Optional[bool] = None
    capture_calendar: Optional[bool] = None
    store_bodies: Optional[bool] = None
    exclude_internal_only: Optional[bool] = None
    exclude_domains: Optional[List[str]] = Field(None, max_length=100)


@router.patch("/settings")
def update_settings(body: CaptureSettings, user: User = Depends(permissions.require_roles(*INTERNAL)), s=Depends(_session)):
    c = _conn(s, user.id)
    if not c:
        raise HTTPException(404, "Connect your mailbox first.")
    ch = body.model_dump(exclude_unset=True)
    if ch.get("store_bodies") and not _allow_bodies(s):
        raise HTTPException(403, "Your organisation doesn't allow storing full email bodies.")
    if "exclude_domains" in ch:
        doms = []
        for d in ch["exclude_domains"] or []:
            d = d.strip().lower().lstrip("@")
            if d and not all(part.replace("-", "").isalnum() for part in d.split(".")):
                raise HTTPException(400, f"Not a domain: {d}")
            if d:
                doms.append(d)
        ch["exclude_domains"] = sorted(set(doms))
    s.execute(text("UPDATE capture_connections SET settings = settings || CAST(:v AS jsonb) WHERE id = :i"),
              {"v": json.dumps(ch), "i": c["id"]})
    if "capture_email" in ch or "capture_calendar" in ch:
        s.execute(text("UPDATE capture_connections SET next_sync_at = now() WHERE id = :i"), {"i": c["id"]})
    s.commit()
    return _conn(s, user.id)


@router.post("/sync")
def sync_now(user: User = Depends(permissions.require_roles(*INTERNAL)), s=Depends(_session)):
    c = _conn(s, user.id)
    if not c:
        raise HTTPException(404, "Connect your mailbox first.")
    if c["status"] == "needs_reauth":
        raise HTTPException(409, "Reconnect your mailbox first.")
    if not microsoft.configured():
        raise HTTPException(503, "Microsoft 365 isn't configured on the server.")
    return cap_engine.sync_connection(c["id"])


@router.delete("/microsoft")
def disconnect(purge: bool = False, user: User = Depends(permissions.require_roles(*INTERNAL)), s=Depends(_session)):
    c = _conn(s, user.id)
    if not c:
        raise HTTPException(404, "No mailbox connected.")
    s.execute(text("DELETE FROM capture_connections WHERE id = :i"), {"i": c["id"]})
    removed = 0
    if purge:
        removed = s.execute(text("DELETE FROM activities WHERE owner_user_id = :u AND source IN ('outlook','outlook_calendar')"),
                            {"u": user.id}).rowcount
    s.commit()
    auth.log_audit(s, user.id, "capture_disconnected", details={"provider": "microsoft", "purged_activities": removed})
    return {"ok": True, "purged": removed,
            "note": "Also remove the app's access in your Microsoft account (myaccount.microsoft.com → Privacy → Apps) if you no longer use it."}


@router.get("/admin")
def admin(user: User = Depends(auth.require_role("super_admin")), s=Depends(_session)):
    rows = [dict(r) for r in s.execute(text("""
        SELECT c.id, c.user_id, coalesce(u.full_name, u.email) AS user_name, c.provider, c.account_email, c.status, c.error,
               c.last_sync_at, c.next_sync_at, c.last_stats, c.settings,
               (SELECT count(*) FROM activities a WHERE a.owner_user_id = c.user_id AND a.source IN ('outlook','outlook_calendar')) AS captured
        FROM capture_connections c JOIN users u ON u.id = c.user_id ORDER BY c.status <> 'active', user_name""")).mappings()]
    return {"configured": microsoft.configured(), "connections": rows, "allow_bodies": _allow_bodies(s),
            "token_key_set": bool(os.getenv("CAPTURE_TOKEN_KEY", "").strip())}
