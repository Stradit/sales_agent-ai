"""Buyer rooms (#11): a read-only page a rep shares with the customer — the close plan, a welcome
note, useful links and their StradIT contact. Nothing else about the deal ever leaves through it
(no value, stage, health, notes or committee).

Security model
  * The link is a 256-bit random token. Only its sha256 is used for lookup; an encrypted copy
    (capture/crypto.seal) lets the rep copy the same link again. A database leak alone doesn't
    reveal working links unless the encryption key leaks too.
  * One active room per deal; it expires (90 days by default) and can be revoked at any time.
  * Unknown, expired and revoked links all get the same 404. The public endpoint is rate-limited
    per IP, never cached, and asks search engines not to index it.
"""
import hashlib
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

import auth
from apps.sales_crm.capture import crypto
from db.connection import get_session
from db.models.user import User

router = APIRouter(prefix="/api/deals", tags=["Deals pipeline"])
public_router = APIRouter(prefix="/api/room", tags=["Buyer room (public)"])

DEFAULT_DAYS = 90
MAX_RESOURCES = 10
_PRIVATE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer"}
_GONE = "This page isn't available. Ask your StradIT contact for a new link."
SIDE_LABEL = {"us": "StradIT", "customer": "Your team", "both": "Together"}


def _session():
    s = get_session()
    try:
        yield s
    finally:
        s.close()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _base_url(req: Optional[Request] = None) -> str:
    if req is not None:
        return str(req.base_url).rstrip("/")
    return os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")


# ── Rate limit for the public endpoint (per IP, in-process — one app container) ──────────────
_HITS: Dict[str, List[float]] = {}
_HITS_LOCK = threading.Lock()
RATE_LIMIT, RATE_WINDOW = 60, 60.0


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    with _HITS_LOCK:
        hits = [t for t in _HITS.get(ip, []) if now - t < RATE_WINDOW]
        hits.append(now)
        _HITS[ip] = hits
        if len(_HITS) > 5000:                      # forget idle IPs
            for k in [k for k, v in _HITS.items() if not v or now - v[-1] > RATE_WINDOW]:
                _HITS.pop(k, None)
        return len(hits) > RATE_LIMIT


# ── Internal (signed-in) endpoints ────────────────────────────────────────────

class Resource(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    url: str = Field(..., min_length=8, max_length=1000)


class RoomIn(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=160)
    welcome_note: Optional[str] = Field(None, max_length=2000)
    resources: Optional[List[Resource]] = None
    days: int = Field(DEFAULT_DAYS, ge=1, le=365)


def _check_resources(resources: Optional[List[Resource]]) -> List[Dict[str, str]]:
    out = []
    for r in resources or []:
        u = urlparse(r.url.strip())
        # https only, and nothing that could break out of an HTML attribute on the public page
        if u.scheme != "https" or not u.netloc or any(ch in r.url for ch in ' "\'<>\\`\t\n\r'):
            raise HTTPException(400, f"Links must start with https:// and contain no spaces or quotes ({r.title}).")
        out.append({"title": r.title.strip(), "url": r.url.strip()})
    if len(out) > MAX_RESOURCES:
        raise HTTPException(400, f"Up to {MAX_RESOURCES} links.")
    return out


def _deal(s, deal_id: int, user) -> Dict[str, Any]:
    from apps.sales_deals.api import _get_deal_row        # same access rule as the deal room
    return _get_deal_row(s, deal_id, user)


def _room_out(s, row, req: Optional[Request] = None) -> Dict[str, Any]:
    if not row:
        return {"room": None}
    try:
        link = f"{_base_url(req)}/room/{crypto.unseal(row['token_enc'])['t']}"
    except Exception:                                    # key rotated: the link can't be shown any more
        link = None
    now = datetime.now(timezone.utc)
    return {"room": {"id": row["id"], "title": row["title"], "welcome_note": row["welcome_note"],
                     "resources": row["resources"] or [], "link": link, "created_at": row["created_at"],
                     "expires_at": row["expires_at"], "expired": row["expires_at"] <= now,
                     "view_count": row["view_count"], "first_viewed_at": row["first_viewed_at"],
                     "last_viewed_at": row["last_viewed_at"]}}


def _active(s, deal_id: int):
    return s.execute(text("SELECT * FROM deal_buyer_rooms WHERE deal_id = :d AND revoked_at IS NULL"),
                     {"d": deal_id}).mappings().fetchone()


@router.get("/{deal_id}/room")
def get_room(deal_id: int, request: Request, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    _deal(s, deal_id, user)
    return _room_out(s, _active(s, deal_id), req=request)


@router.post("/{deal_id}/room")
def create_room(deal_id: int, body: RoomIn, request: Request, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Creates the buyer room — or replaces its link: the old link stops working immediately."""
    from apps.sales_deals.api import _log
    deal = _deal(s, deal_id, user)
    old = _active(s, deal_id)
    resources = _check_resources(body.resources) if body.resources is not None else (old["resources"] if old else [])
    title = (body.title or (old["title"] if old else None) or f"{deal['account_name']} & StradIT").strip()
    note = body.welcome_note if body.welcome_note is not None else (old["welcome_note"] if old else None)
    if old:
        s.execute(text("UPDATE deal_buyer_rooms SET revoked_at = now() WHERE id = :i"), {"i": old["id"]})
    token = secrets.token_urlsafe(32)
    import json
    s.execute(text("""INSERT INTO deal_buyer_rooms (deal_id, token_hash, token_enc, title, welcome_note, resources,
                                                    created_by, expires_at)
                      VALUES (:d, :h, :e, :t, :n, CAST(:r AS jsonb), :u, now() + make_interval(days => :days))"""),
              {"d": deal_id, "h": _hash(token), "e": crypto.seal({"t": token}), "t": title,
               "n": (note or "").strip() or None, "r": json.dumps(resources), "u": user.id, "days": body.days})
    _log(s, deal_id, user.id, "field", "Buyer room: new link created" + (" (the old link no longer works)" if old else ""))
    s.commit()
    return _room_out(s, _active(s, deal_id), req=request)


@router.patch("/{deal_id}/room")
def update_room(deal_id: int, body: RoomIn, request: Request, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    """Changes what the room shows; the link stays the same."""
    _deal(s, deal_id, user)
    room = _active(s, deal_id)
    if not room:
        raise HTTPException(404, "This deal has no buyer room yet.")
    import json
    sets, params = [], {"i": room["id"]}
    if body.title is not None:
        sets.append("title = :t"); params["t"] = body.title.strip()
    if body.welcome_note is not None:
        sets.append("welcome_note = :n"); params["n"] = body.welcome_note.strip() or None
    if body.resources is not None:
        sets.append("resources = CAST(:r AS jsonb)"); params["r"] = json.dumps(_check_resources(body.resources))
    if "days" in body.model_fields_set:
        sets.append("expires_at = now() + make_interval(days => :days)"); params["days"] = body.days
    if sets:
        s.execute(text(f"UPDATE deal_buyer_rooms SET {', '.join(sets)} WHERE id = :i"), params)
        s.commit()
    return _room_out(s, _active(s, deal_id), req=request)


@router.delete("/{deal_id}/room")
def revoke_room(deal_id: int, user: User = Depends(auth.get_current_user), s=Depends(_session)):
    from apps.sales_deals.api import _log
    _deal(s, deal_id, user)
    n = s.execute(text("UPDATE deal_buyer_rooms SET revoked_at = now() WHERE deal_id = :d AND revoked_at IS NULL"),
                  {"d": deal_id}).rowcount
    if not n:
        raise HTTPException(404, "This deal has no buyer room.")
    _log(s, deal_id, user.id, "field", "Buyer room: link turned off")
    s.commit()
    return {"room": None}


# ── Public (no sign-in) ───────────────────────────────────────────────────────

@public_router.get("/{token}")
def public_room(token: str, request: Request, s=Depends(_session)):
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        return JSONResponse({"detail": "Too many requests — try again in a minute."}, status_code=429, headers=_PRIVATE)
    if not (20 <= len(token) <= 100):
        return JSONResponse({"detail": _GONE}, status_code=404, headers=_PRIVATE)
    room = s.execute(text("""SELECT r.*, d.name AS deal_name, d.stage, a.display_name AS account_name,
                                    coalesce(u.full_name, u.email) AS owner_name, u.email AS owner_email
                             FROM deal_buyer_rooms r JOIN deals d ON d.id = r.deal_id JOIN accounts a ON a.id = d.account_id
                             LEFT JOIN users u ON u.id = d.owner_user_id
                             WHERE r.token_hash = :h AND r.revoked_at IS NULL AND r.expires_at > now()"""),
                     {"h": _hash(token)}).mappings().fetchone()
    if not room:
        return JSONResponse({"detail": _GONE}, status_code=404, headers=_PRIVATE)
    first = room["first_viewed_at"] is None
    s.execute(text("""UPDATE deal_buyer_rooms SET view_count = view_count + 1, last_viewed_at = now(),
                      first_viewed_at = coalesce(first_viewed_at, now()) WHERE id = :i"""), {"i": room["id"]})
    if first:
        from apps.sales_deals.api import _log
        _log(s, room["deal_id"], None, "field", "Buyer room: the customer opened it for the first time")
    s.commit()
    plan = [dict(r) for r in s.execute(text("""
        SELECT title, side, owner_name, due_date, status FROM deal_close_plan WHERE deal_id = :d
        ORDER BY ordinal, due_date NULLS LAST, id"""), {"d": room["deal_id"]}).mappings()]
    for p in plan:
        p["side_label"] = SIDE_LABEL.get(p["side"], p["side"])
    body = {"title": room["title"], "account_name": room["account_name"], "welcome_note": room["welcome_note"],
            "resources": room["resources"] or [], "plan": plan,
            "contact": {"name": room["owner_name"], "email": room["owner_email"]},
            "closed": room["stage"] in ("won", "lost"), "expires_at": room["expires_at"].isoformat()}
    from fastapi.encoders import jsonable_encoder
    return JSONResponse(jsonable_encoder(body), headers=_PRIVATE)
