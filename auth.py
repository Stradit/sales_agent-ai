"""JWT authentication, password hashing, and role-based authorization.

See AUTH_JWT_IMPLEMENTATION_PLAN.md for the full design. This module holds
the framework-light pieces (hashing, token encode/decode, DB helpers) plus
the two FastAPI dependencies (`get_current_user`, `require_role`) every
protected route uses — the actual `/api/auth/*` HTTP endpoints live in
api.py, same as every other endpoint in this codebase.

Not yet enforced anywhere (AUTH_ENFORCED gate, see api.py) — Phase 1 of the
plan ships this alongside the rest of the app so login/refresh/logout can be
verified end-to-end before anything starts requiring it.
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Set

import jwt
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db.connection import get_session
from db.models import AuditLog, Client, PasswordResetToken, RefreshToken, User, UserAccountAccess, UserClientAccess

# Loaded independently of import order (config.py also loads the same file,
# but auth.py shouldn't rely on having been imported after it).
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── Config ──────────────────────────────────────────────────────
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRE_DAYS", "14"))
RESET_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_RESET_TOKEN_EXPIRE_MINUTES", "45"))
# A new user's invitation link is the same set-password token, but an invite
# may sit unopened until the next working day, so it lives longer.
INVITE_TOKEN_EXPIRE_HOURS = int(os.getenv("JWT_INVITE_TOKEN_EXPIRE_HOURS", "72"))
MAX_FAILED_LOGINS = int(os.getenv("AUTH_MAX_FAILED_LOGINS", "5"))
LOCKOUT_MINUTES = int(os.getenv("AUTH_LOCKOUT_MINUTES", "15"))

# Whether any protected route actually enforces auth yet — see api.py's
# router-level `Depends(get_current_user)`. False during Phase 1 so the
# rest of the app stays reachable while login/refresh/logout are verified.
AUTH_ENFORCED = os.getenv("AUTH_ENFORCED", "false").strip().lower() == "true"

if not JWT_SECRET_KEY:
    # Fail loud, not silent — a blank/default secret would let anyone forge
    # a valid token. Generate one with:
    #   python -c "import secrets; print(secrets.token_urlsafe(64))"
    raise RuntimeError(
        "JWT_SECRET_KEY is not set in .env — required before auth.py can be "
        "imported. See AUTH_JWT_IMPLEMENTATION_PLAN.md §3."
    )

_hasher = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)


# ── Password hashing (argon2) ────────────────────────────────────

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


# ── Opaque tokens (refresh / password-reset) ─────────────────────
# These are high-entropy random strings, not user-chosen secrets, so a fast
# cryptographic hash (not argon2) is the right tool for at-rest storage —
# same reasoning as hashing an API key rather than a password.

def _new_raw_token() -> str:
    return secrets.token_urlsafe(48)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# ── JWT access tokens ─────────────────────────────────────────────

def create_access_token(user: User, client_slug: Optional[str] = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "client_slug": client_slug,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Raises jwt.PyJWTError (expired, bad signature, malformed) on failure."""
    return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])


# ── Refresh tokens (server-side, revocable — see db/models/refresh_token.py) ──

def issue_refresh_token(session, user: User, request: Optional[Request] = None) -> str:
    raw = _new_raw_token()
    row = RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        user_agent=(request.headers.get("user-agent", "")[:500] if request else None),
        ip_address=(request.client.host if request and request.client else None),
    )
    session.add(row)
    session.commit()
    return raw


def verify_and_rotate_refresh_token(session, raw_token: str, request: Optional[Request] = None) -> tuple[str, User]:
    """Validates a refresh token, revokes it, and issues a replacement.

    Rotation means a stolen-and-reused *old* token becomes detectable (it
    will already be revoked) instead of remaining silently valid for its
    whole lifetime. Raises HTTPException(401) on any failure.
    """
    token_hash = _hash_token(raw_token)
    row = session.query(RefreshToken).filter_by(token_hash=token_hash).first()
    if not row or row.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    if row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Refresh token expired")

    user = session.query(User).filter_by(id=row.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Account is inactive")

    row.revoked_at = datetime.now(timezone.utc)
    session.commit()
    new_raw = issue_refresh_token(session, user, request)
    return new_raw, user


def revoke_refresh_token(session, raw_token: str) -> None:
    token_hash = _hash_token(raw_token)
    row = session.query(RefreshToken).filter_by(token_hash=token_hash).first()
    if row and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        session.commit()


def revoke_all_refresh_tokens_for_user(session, user_id: int) -> None:
    """Ends every session for a user — used on password reset/change and
    available for a super admin's "log out everywhere" action."""
    now = datetime.now(timezone.utc)
    (session.query(RefreshToken)
     .filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
     .update({RefreshToken.revoked_at: now}))
    session.commit()


# ── Password reset tokens ─────────────────────────────────────────

def issue_password_reset_token(session, user: User, expire_minutes: int = None) -> str:
    raw = _new_raw_token()
    row = PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=expire_minutes or RESET_TOKEN_EXPIRE_MINUTES),
    )
    session.add(row)
    session.commit()
    return raw


def consume_password_reset_token(session, raw_token: str) -> User:
    """Validates a reset token and marks it used. Raises HTTPException(400)
    on any failure (unknown/expired/already-used) — deliberately generic so
    a caller can't distinguish "expired" from "never existed"."""
    token_hash = _hash_token(raw_token)
    row = session.query(PasswordResetToken).filter_by(token_hash=token_hash).first()
    if (
        not row
        or row.used_at is not None
        or row.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    user = session.query(User).filter_by(id=row.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="Invalid or expired reset link")

    row.used_at = datetime.now(timezone.utc)
    session.commit()
    return user


# ── Login lockout ──────────────────────────────────────────────────

def register_failed_login(session, user: User) -> None:
    user.failed_login_count = (user.failed_login_count or 0) + 1
    if user.failed_login_count >= MAX_FAILED_LOGINS:
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
    session.commit()


def register_successful_login(session, user: User) -> None:
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)
    session.commit()


def is_locked_out(user: User) -> bool:
    return bool(user.locked_until and user.locked_until.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))


# ── Per-user account access ─────────────────────────────────────────
# A user with zero grants has no accounts once this is enforced (Phase 3) —
# "super admin grants access" is literal: a new user starts with nothing.
# super_admin bypasses this table entirely (sees everything) — callers
# should check `user.role == "super_admin"` before consulting these.

#
# CRM roles (apps/sales_crm/README.md §5). 'user' is the sales rep — the value
# is kept so existing rows and checks keep working.
ROLES = ("super_admin", "sales_manager", "user", "viewer", "partner")
ROLE_LABELS = {
    "super_admin": "Super Admin",
    "sales_manager": "Sales Manager",
    "user": "Sales Rep",
    "viewer": "Viewer",
    "partner": "Partner / Advisor",
}
CLIENT_ROLES = ("client_admin", "analyst", "viewer")
READ_ONLY_ROLES = ("viewer", "partner")

# Unified Permission Matrix (Specification v2 §8)
PERMISSION_ROLES: Dict[str, Set[str]] = {
    # Platform catalog admin operations (super_admin only)
    "catalog:view_library": {"super_admin"},
    "catalog:publish": {"super_admin"},
    "catalog:assign": {"super_admin"},
    "catalog:approve": {"super_admin"},
    "catalog:rollback": {"super_admin"},

    # Account reuse & client management (super_admin and client_admin)
    "account:reuse_check": {"super_admin", "client_admin"},
    "account:reuse_request": {"super_admin", "client_admin"},
    "client:manage": {"super_admin", "client_admin"},

    # Pipeline execution and writes (super_admin, client_admin, analyst)
    "account:write": {"super_admin", "client_admin", "analyst"},
    "auto_pipeline:run": {"super_admin", "client_admin", "analyst"},

    # Read-only operations
    "account:read": {"super_admin", "client_admin", "analyst", "viewer"},
}


def get_granted_account_ids(session, user_id: int) -> list:
    """The user's own explicit grants only (what the admin picker shows)."""
    rows = session.query(UserAccountAccess.account_id).filter_by(user_id=user_id).all()
    return [r[0] for r in rows]


def team_user_ids(session, manager_id: int) -> list:
    """Everyone reporting to this user, directly or indirectly (users.manager_id)."""
    rows = session.execute(text("""
        WITH RECURSIVE team AS (
            SELECT id FROM users WHERE manager_id = :m
            UNION SELECT u.id FROM users u JOIN team t ON u.manager_id = t.id)
        SELECT id FROM team"""), {"m": manager_id}).fetchall()
    return [r[0] for r in rows]


def get_accessible_account_ids(session, user_id: int) -> list:
    """Accounts this (non-super-admin) user may open.

    rep / viewer → their own grants; sales_manager → own grants + their team's
    grants; partner → none (partners only see their introductions, via the
    partner endpoints)."""
    role = session.execute(text("SELECT role FROM users WHERE id = :u"), {"u": user_id}).scalar()
    if role == "partner":
        return []
    ids = set(get_granted_account_ids(session, user_id))
    if role == "sales_manager":
        team = team_user_ids(session, user_id)
        if team:
            ids.update(r[0] for r in session.query(UserAccountAccess.account_id)
                       .filter(UserAccountAccess.user_id.in_(team)).all())
    return sorted(ids)


def account_scope(session, user) -> list:
    """Account ids a request may touch — every account for super_admin (or when
    auth is not enforced), otherwise get_accessible_account_ids()."""
    if not AUTH_ENFORCED or getattr(user, "role", None) in ("super_admin", "client_admin"):
        return [r[0] for r in session.execute(text("SELECT id FROM accounts")).fetchall()]
    return get_accessible_account_ids(session, user.id)


def _has_account(session, user, account_id: int) -> bool:
    return account_id in get_accessible_account_ids(session, user.id)


def grant_account_access(session, user_id: int, account_id: int, granted_by_id: int) -> bool:
    """Returns False if the grant already existed (idempotent, not an error)."""
    existing = session.query(UserAccountAccess).filter_by(user_id=user_id, account_id=account_id).first()
    if existing:
        return False
    session.add(UserAccountAccess(user_id=user_id, account_id=account_id, granted_by_id=granted_by_id))
    session.commit()
    return True


def revoke_account_access(session, user_id: int, account_id: int) -> bool:
    row = session.query(UserAccountAccess).filter_by(user_id=user_id, account_id=account_id).first()
    if not row:
        return False
    session.delete(row)
    session.commit()
    return True


# ── Audit log ────────────────────────────────────────────────────

def log_audit(session, actor_user_id: Optional[int], action: str,
              target_user_id: Optional[int] = None, details: Optional[dict] = None) -> None:
    """Insert an audit-log row.

    Automatically heals a stale primary-key sequence (UniqueViolation) by
    rolling back, resetting the sequence to MAX(id), and retrying once.
    """
    entry = AuditLog(
        actor_user_id=actor_user_id, action=action,
        target_user_id=target_user_id, details=details or {},
    )
    session.add(entry)
    try:
        session.commit()
    except IntegrityError as exc:
        # psycopg2.errors.UniqueViolation on audit_log_pkey → stale sequence
        session.rollback()
        # Reset the sequence so the next nextval() is safe
        session.execute(
            text("SELECT setval('audit_log_id_seq', (SELECT MAX(id) FROM audit_log), true)")
        )
        session.commit()
        # Retry the insert once with the corrected sequence
        session.add(AuditLog(
            actor_user_id=actor_user_id, action=action,
            target_user_id=target_user_id, details=details or {},
        ))
        session.commit()



# ── FastAPI dependencies ───────────────────────────────────────────

def get_current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> User:
    # Phase 1: auth endpoints exist but nothing requires them yet.
    # When AUTH_ENFORCED=false, unauthenticated requests get a synthetic
    # super_admin bypass user so every route stays reachable during dev.
    if creds is None:
        if not AUTH_ENFORCED:
            session = get_session()
            try:
                user = session.query(User).filter_by(role="super_admin").first() or session.query(User).first()
                if user:
                    session.expunge(user)
                    return user
                return User(
                    id=1, email="admin@local", role="super_admin", is_active=True,
                    has_dashboard_access=True, has_command_center_access=True,
                    has_tasks_access=True, has_pipeline_access=True
                )
            finally:
                session.close()
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_access_token(creds.credentials)
    except jwt.ExpiredSignatureError:
        if not AUTH_ENFORCED:
            session = get_session()
            try:
                user = session.query(User).filter_by(role="super_admin").first() or session.query(User).first()
                if user:
                    session.expunge(user)
                    return user
            finally:
                session.close()
        raise HTTPException(status_code=401, detail="Access token expired")
    except jwt.PyJWTError:
        if not AUTH_ENFORCED:
            session = get_session()
            try:
                user = session.query(User).filter_by(role="super_admin").first() or session.query(User).first()
                if user:
                    session.expunge(user)
                    return user
            finally:
                session.close()
        raise HTTPException(status_code=401, detail="Invalid access token")

    session = get_session()
    try:
        user = session.query(User).filter_by(id=int(payload["sub"])).first()
        if not user or not user.is_active:
            if not AUTH_ENFORCED:
                admin_user = session.query(User).filter_by(role="super_admin").first() or session.query(User).first()
                if admin_user:
                    session.expunge(admin_user)
                    return admin_user
            raise HTTPException(status_code=401, detail="Account is inactive")
        # Detach from the session before closing it so the caller can still
        # read attributes (SQLAlchemy would otherwise lazy-reload on access
        # and hit a closed session) — the same object, not a live query.
        session.expunge(user)
        user.token_client_slug = payload.get("client_slug")
        return user
    finally:
        session.close()


def require_role(*roles: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if not AUTH_ENFORCED:
            return user
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Not authorized")
        return user
    return dependency


def require_account_access(account_id: int, user: User = Depends(get_current_user)) -> User:
    """Gate for any route with an `account_id` path parameter — FastAPI
    matches this dependency's `account_id` argument to that path param
    automatically. super_admin always passes; anyone else needs an explicit
    UserAccountAccess grant for this specific account (see
    db/models/user_account_access.py)."""
    if not AUTH_ENFORCED or user.role in ("super_admin", "client_admin"):
        return user
    session = get_session()
    try:
        if not _has_account(session, user, account_id):
            raise HTTPException(status_code=403, detail="You do not have access to this account")
        return user
    finally:
        session.close()


def require_persona_account_access(persona_id: int, user: User = Depends(get_current_user)) -> User:
    """Same as require_account_access, but for a route keyed by persona_id —
    resolves the persona's account first, then applies the same check."""
    if not AUTH_ENFORCED or user.role in ("super_admin", "client_admin"):
        return user
    session = get_session()
    try:
        from db.models import Persona
        persona = session.query(Persona).filter_by(id=persona_id).first()
        if not persona:
            raise HTTPException(status_code=404, detail="Persona not found")
        if not _has_account(session, user, persona.account_id):
            raise HTTPException(status_code=403, detail="You do not have access to this account")
        return user
    finally:
        session.close()


def require_action_item_account_access(item_id: int, user: User = Depends(get_current_user)) -> User:
    """Same as require_account_access, but for a route keyed by an
    action_items id — resolves the item's account first, then applies the
    same check. See ACTION_ITEMS_IMPLEMENTATION_PLAN.md §2."""
    if not AUTH_ENFORCED or user.role in ("super_admin", "client_admin"):
        return user
    session = get_session()
    try:
        from db.models import ActionItem
        item = session.query(ActionItem).filter_by(id=item_id).first()
        if not item:
            raise HTTPException(status_code=404, detail="Action item not found")
        if not _has_account(session, user, item.account_id):
            raise HTTPException(status_code=403, detail="You do not have access to this account")
        return user
    finally:
        session.close()


# ── CRM hardening (apps/sales_crm/README.md §5.2, M5) ─────────────────────────

def require_editor(user: User = Depends(get_current_user)) -> User:
    """Rejects read-only roles (viewer, partner) on write endpoints."""
    if AUTH_ENFORCED and user.role in READ_ONLY_ROLES:
        raise HTTPException(status_code=403, detail="Your role is read-only.")
    return user


def _require_account_of(sql: str, key: int, user: User, missing: str) -> User:
    if not AUTH_ENFORCED or user.role in ("super_admin", "client_admin"):
        return user
    session = get_session()
    try:
        account_id = session.execute(text(sql), {"k": key}).scalar()
        if account_id is None:
            raise HTTPException(status_code=404, detail=missing)
        if not _has_account(session, user, account_id):
            raise HTTPException(status_code=403, detail="You do not have access to this account")
        return user
    finally:
        session.close()


def require_lob_account_access(lob_id: int, user: User = Depends(get_current_user)) -> User:
    return _require_account_of("SELECT account_id FROM lobs WHERE id = :k", lob_id, user, "LOB not found")


def require_sub_lob_account_access(sub_lob_id: int, user: User = Depends(get_current_user)) -> User:
    return _require_account_of("SELECT l.account_id FROM sub_lobs s JOIN lobs l ON l.id = s.lob_id WHERE s.id = :k",
                               sub_lob_id, user, "Sub-LOB not found")


def require_entity_account_access(entity_type: str, entity_id: int, user: User = Depends(get_current_user)) -> User:
    """For /api/verify/{entity_type}/{entity_id}: account, lob, sub_lob or persona."""
    kind = entity_type.lower().replace("-", "_").rstrip("s")
    kind = {"person": "persona", "sublob": "sub_lob"}.get(kind, kind)
    sql = {"account": "SELECT id FROM accounts WHERE id = :k",
           "lob": "SELECT account_id FROM lobs WHERE id = :k",
           "sub_lob": "SELECT l.account_id FROM sub_lobs s JOIN lobs l ON l.id = s.lob_id WHERE s.id = :k",
           "persona": "SELECT account_id FROM personas WHERE id = :k"}
    if kind not in sql:
        raise HTTPException(status_code=400, detail="Unknown entity type")
    return _require_account_of(sql[kind], entity_id, user, "Record not found")


# ── Multi-Tenant Client Helpers ─────────────────────────────────────

def get_user_client_grants(session, user_id: int) -> list:
    """Returns all active clients accessible by this user with their role in each."""
    rows = (
        session.query(Client, UserClientAccess.role)
        .join(UserClientAccess, UserClientAccess.client_id == Client.id)
        .filter(UserClientAccess.user_id == user_id, Client.is_active == True)
        .all()
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "slug": c.slug,
            "schema_name": c.schema_name,
            "plan_tier": c.plan_tier,
            "role": role,
            "api_credits_total": c.api_credits_total,
            "api_credits_used": c.api_credits_used,
            "max_accounts": c.max_accounts,
        }
        for c, role in rows
    ]


def normalize_client_slug(slug: Optional[str]) -> Optional[str]:
    """Strips leading 'client_' schema prefix if present, and canonicalizes 'stradit' -> 'stradit_india'."""
    if not slug:
        return None
    s = str(slug).strip().lower()
    if s.startswith("client_"):
        s = s[7:]
    return "stradit_india" if s == "stradit" else s


def resolve_request_client_slug(request: Request, user: Optional[User] = None) -> Optional[str]:
    """Resolves client slug from request path params, headers, query params, or user token."""
    # 1. Path parameters (e.g. /api/clients/{slug}/... or /{client_slug}/...)
    if hasattr(request, "path_params") and request.path_params:
        slug = request.path_params.get("slug") or request.path_params.get("client_slug")
        if slug:
            return normalize_client_slug(slug)

    # 2. Header
    header_slug = request.headers.get("X-Client-Slug")
    if header_slug:
        return normalize_client_slug(header_slug)

    # 3. Query params
    if hasattr(request, "query_params") and request.query_params:
        q_slug = request.query_params.get("client_slug") or request.query_params.get("slug")
        if q_slug:
            return normalize_client_slug(q_slug)

    # 4. Token client slug
    if user and getattr(user, "token_client_slug", None):
        return normalize_client_slug(getattr(user, "token_client_slug"))

    return None


def get_effective_role(session, user: User, client_slug: Optional[str] = None) -> Optional[str]:
    """Returns the effective role for a user in the given client context.
    
    Rules (§8 Specification v2):
    1. super_admin is always super_admin across all platform contexts.
    2. Global READ_ONLY_ROLES (viewer, partner) are capped to 'viewer' regardless of tenant grants.
    3. If client_slug is specified, resolves UserClientAccess.role for that client.
    4. If client_slug is not specified, returns client_admin if user has that grant in any active client,
       otherwise returns user.role.
    """
    if not AUTH_ENFORCED or getattr(user, "role", None) == "super_admin":
        return "super_admin"

    # Global read-only roles cap to 'viewer'
    if getattr(user, "role", None) in READ_ONLY_ROLES:
        return "viewer"

    if client_slug:
        clean_slug = normalize_client_slug(client_slug)
        grant = (
            session.query(UserClientAccess.role)
            .join(Client, Client.id == UserClientAccess.client_id)
            .filter(
                UserClientAccess.user_id == user.id,
                Client.slug == clean_slug,
                Client.is_active == True,
            )
            .scalar()
        )
        return grant

    # When no client_slug is specified, check if user has client_admin grant in any active client
    has_client_admin = (
        session.query(UserClientAccess.id)
        .join(Client, Client.id == UserClientAccess.client_id)
        .filter(
            UserClientAccess.user_id == user.id,
            UserClientAccess.role == "client_admin",
            Client.is_active == True,
        )
        .first()
    )
    if has_client_admin:
        return "client_admin"

    return getattr(user, "role", None)


def has_permission(effective_role: Optional[str], permission: str) -> bool:
    """Evaluates whether an effective role satisfies the requested permission according to role hierarchy."""
    if not effective_role:
        return False
    if effective_role == "super_admin":
        return True
    allowed_roles = PERMISSION_ROLES.get(permission)
    if not allowed_roles:
        return False
    if effective_role in allowed_roles:
        return True
    if effective_role == "client_admin" and any(r in ("analyst", "viewer") for r in allowed_roles):
        return True
    if effective_role == "analyst" and "viewer" in allowed_roles:
        return True
    return False


def require_permission(permission: str, require_client_context: bool = False):
    """FastAPI dependency verifying the user has the required permission in the active context."""
    def dependency(request: Request, user: User = Depends(get_current_user)) -> User:
        if not AUTH_ENFORCED or getattr(user, "role", None) == "super_admin":
            return user

        allowed_roles = PERMISSION_ROLES.get(permission)
        if allowed_roles is None:
            raise HTTPException(status_code=500, detail=f"Unknown permission '{permission}'")

        client_slug = resolve_request_client_slug(request, user)
        if require_client_context and not client_slug:
            raise HTTPException(
                status_code=400,
                detail="Client context required (pass X-Client-Slug header or client parameter)",
            )

        session = get_session()
        try:
            effective = get_effective_role(session, user, client_slug)
            if not effective or not has_permission(effective, permission):
                raise HTTPException(status_code=403, detail="Not authorized")

            user.effective_role = effective
            return user
        finally:
            session.close()
    return dependency


def user_has_client_access(session, user: User, client_slug: str) -> bool:
    """Returns True if user has access to client_slug."""
    if not AUTH_ENFORCED or getattr(user, "role", None) == "super_admin":
        return True
    access = (
        session.query(UserClientAccess)
        .join(Client, Client.id == UserClientAccess.client_id)
        .filter(UserClientAccess.user_id == user.id, Client.slug == client_slug, Client.is_active == True)
        .first()
    )
    return access is not None


def require_client_role(*roles: str):
    """Dependency verifying user has one of the allowed roles for the active client."""
    def dependency(request: Request, user: User = Depends(get_current_user)) -> User:
        if not AUTH_ENFORCED or getattr(user, "role", None) == "super_admin":
            return user
        client_slug = resolve_request_client_slug(request, user)
        if not client_slug:
            raise HTTPException(status_code=400, detail="Client context required (pass X-Client-Slug header or client parameter)")
        session = get_session()
        try:
            grant = (
                session.query(UserClientAccess)
                .join(Client, Client.id == UserClientAccess.client_id)
                .filter(UserClientAccess.user_id == user.id, Client.slug == client_slug, Client.is_active == True)
                .first()
            )
            if not grant:
                raise HTTPException(status_code=403, detail="Not authorized for this client")
            if grant.role in roles or (grant.role == "client_admin" and any(r in ("client_admin", "analyst", "viewer") for r in roles)):
                return user
            raise HTTPException(status_code=403, detail="Not authorized for this client")
        finally:
            session.close()
    return dependency
