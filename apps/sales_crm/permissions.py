"""One permission layer for the CRM (apps/sales_crm/README.md §5).

* Account scope (which accounts a user can open) lives in auth.py —
  `auth.account_scope()` / `auth.get_accessible_account_ids()` — because every
  router already imports auth. Sales managers inherit their team's accounts there.
* This module adds what the account scope can't express:
    - `role_guard` middleware: viewers are read-only and partners can only reach
      the partner and auth APIs, across *every* existing endpoint at once;
    - `can(user, action, ...)` for record-level checks in the CRM routers;
    - `require_roles()` / `require_write` FastAPI dependencies.
Only active when AUTH_ENFORCED=true (otherwise every request is the dev admin).
"""

import re
import time
from typing import Dict, Optional, Tuple

import jwt
from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

import auth
from db.connection import get_session
from db.models.user import User

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Paths any logged-in role may call (login/refresh/logout/me, password reset)
ALWAYS_ALLOWED = ("/api/auth/",)
# Viewers may still use their personal copilot workspace (chat, notes, exports are theirs only)
VIEWER_WRITE_ALLOWED = ALWAYS_ALLOWED + ("/api/copilot/",)
# ...and the derived-history syncs the account page fires on view (not user edits)
VIEWER_SYNC_ALLOWED = re.compile(r"^/api/accounts/\d+/(opportunities|weekly-updates)/sync$")
# Partners: introductions portal only (built in M1/M5)
PARTNER_ALLOWED = ALWAYS_ALLOWED + ("/api/partner/",)

_role_cache: Dict[int, Tuple[float, Optional[str]]] = {}
_ROLE_TTL = 30.0


def _role_of(user_id: int) -> Optional[str]:
    hit = _role_cache.get(user_id)
    if hit and time.monotonic() - hit[0] < _ROLE_TTL:
        return hit[1]
    s = get_session()
    try:
        role = s.execute(text("SELECT role FROM users WHERE id = :u AND is_active"), {"u": user_id}).scalar()
    finally:
        s.close()
    _role_cache[user_id] = (time.monotonic(), role)
    return role


def forget_role(user_id: int) -> None:
    """Call after a role change so the guard doesn't wait for the cache to expire."""
    _role_cache.pop(user_id, None)


def blocked_reason(role: Optional[str], method: str, path: str) -> Optional[str]:
    """Pure decision function (unit-tested): None = allowed, else a message."""
    if not path.startswith("/api/") or role in (None, "super_admin", "sales_manager", "user"):
        return None
    if role == "partner":
        return None if path.startswith(PARTNER_ALLOWED) else "Partners can only access their introductions."
    if role == "viewer" and method.upper() not in SAFE_METHODS and not path.startswith(VIEWER_WRITE_ALLOWED) \
            and not VIEWER_SYNC_ALLOWED.match(path):
        return "Your role is read-only."
    return None


async def role_guard(request, call_next):
    if auth.AUTH_ENFORCED and request.url.path.startswith("/api/"):
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            try:
                uid = int(auth.decode_access_token(header[7:].strip())["sub"])
            except (jwt.PyJWTError, KeyError, ValueError):
                uid = None          # the route's own get_current_user returns the 401
            if uid:
                role = await run_in_threadpool(_role_of, uid)
                reason = blocked_reason(role, request.method, request.url.path)
                if reason:
                    return JSONResponse({"detail": reason}, status_code=403)
    return await call_next(request)


# ── Dependencies & record checks ──────────────────────────────────────────────


def is_admin(user) -> bool:
    return not auth.AUTH_ENFORCED or getattr(user, "role", None) == "super_admin"


def require_roles(*roles: str):
    def dep(user: User = Depends(auth.get_current_user)) -> User:
        if auth.AUTH_ENFORCED and user.role != "super_admin" and user.role not in roles:
            raise HTTPException(403, "Not authorized")
        return user
    return dep


def require_write(user: User = Depends(auth.get_current_user)) -> User:
    if auth.AUTH_ENFORCED and user.role in auth.READ_ONLY_ROLES:
        raise HTTPException(403, "Your role is read-only.")
    return user


def can(session, user, action: str, account_id: Optional[int] = None, owner_user_id: Optional[int] = None) -> bool:
    """Record-level rule used by CRM routers.

    read   — account in scope
    edit   — account in scope and role is not read-only (team selling: any rep on the account may edit)
    delete — owner, the owner's manager chain, or admin
    export — same as read, not for partners
    """
    if is_admin(user):
        return True
    role = user.role
    in_scope = account_id is None or account_id in auth.get_accessible_account_ids(session, user.id)
    if action == "read":
        return in_scope
    if action == "export":
        return in_scope and role != "partner"
    if action == "edit":
        return in_scope and role not in auth.READ_ONLY_ROLES
    if action == "delete":
        if role in auth.READ_ONLY_ROLES or not in_scope:
            return False
        return owner_user_id in (None, user.id) or (role == "sales_manager" and owner_user_id in auth.team_user_ids(session, user.id))
    return False
