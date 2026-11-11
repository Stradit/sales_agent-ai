"""Tenant Context & Session Routing Engine — Fail-Closed Multi-Tenancy.

Provides:
1. Strict tenant context propagation via ContextVars (no fail-open default).
2. Fail-closed schema resolution: raises TenantContextError when tenant context is required but missing.
3. Transaction-scoped search_path isolation via `SET LOCAL search_path = "client_<slug>", public`
   on every SQLAlchemy transaction begin. Safe with PgBouncer transaction pooling.
4. Clean context inheritance for asynchronous code and OS background threads (`spawn_tenant_thread`).
5. Reversible context manager `tenant_scope(slug)` and session generators.
"""

import contextvars
from contextvars import ContextVar, Token
from contextlib import contextmanager
import re
import threading
from typing import Generator, Optional, Any, Set
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from db.connection import engine, SessionLocal


# ── Tenant Slug & Schema Standards ───────────────────────────────────────────

SLUG_REGEX = re.compile(r"^[a-z][a-z0-9_]{1,98}$")

RESERVED_SLUGS: Set[str] = {
    "public", "information_schema", "pg_catalog", "template", "postgres",
    "admin", "root", "system", "auth", "api", "default", "core"
}

DEFAULT_FALLBACK_SLUG = "stradit_india"
DEFAULT_FALLBACK_SCHEMA = f"client_{DEFAULT_FALLBACK_SLUG}"


class TenantContextError(RuntimeError):
    """Raised when an operation requires an active tenant context but none is bound."""
    pass


def validate_slug(slug: str) -> str:
    """Validates that a client slug is safe for use as a PostgreSQL schema identifier.
    
    Raises:
        ValueError: If slug is empty, malformed, or a reserved system keyword.
    """
    if not slug or not isinstance(slug, str):
        raise ValueError("Tenant slug must be a non-empty string.")
    cleaned = slug.strip().lower()
    # Normalize legacy alias
    if cleaned == "stradit":
        cleaned = "stradit_india"
    if not SLUG_REGEX.match(cleaned):
        raise ValueError(
            f"Invalid tenant slug '{cleaned}'. Must start with a letter and contain "
            "only lowercase letters, numbers, and underscores (2-99 characters)."
        )
    if cleaned in RESERVED_SLUGS:
        raise ValueError(f"Tenant slug '{cleaned}' is a reserved system keyword.")
    return cleaned


def schema_for(slug: str) -> str:
    """Returns the PostgreSQL schema name corresponding to a tenant slug."""
    if not slug:
        raise ValueError("Cannot derive schema name for empty slug.")
    clean_slug = slug.strip().lower()
    if clean_slug.startswith("client_"):
        clean_slug = clean_slug[7:]
    return f"client_{validate_slug(clean_slug)}"


def slug_from_schema(schema_name: str) -> str:
    """Extracts the tenant slug from a PostgreSQL schema name."""
    if not schema_name:
        return ""
    s = schema_name.strip().lower()
    return s[7:] if s.startswith("client_") else s


# ── ContextVar (Fail-Closed: default is None) ─────────────────────────────────

_current_tenant_schema: ContextVar[Optional[str]] = ContextVar(
    "current_tenant_schema",
    default=None
)


def set_current_tenant(client_slug: str) -> Token:
    """Binds the active tenant schema to the current context."""
    schema = schema_for(client_slug)
    return _current_tenant_schema.set(schema)


def reset_current_tenant(token: Token) -> None:
    """Resets tenant context to the token's previous state."""
    try:
        _current_tenant_schema.reset(token)
    except (ValueError, RuntimeError):
        pass


def get_current_schema() -> Optional[str]:
    """Returns active tenant schema name (e.g. 'client_stradit_india'), or None if unbound."""
    return _current_tenant_schema.get()


def get_current_slug() -> Optional[str]:
    """Returns active tenant slug (e.g. 'stradit_india'), or None if unbound."""
    schema = get_current_schema()
    return slug_from_schema(schema) if schema else None


def require_current_schema() -> str:
    """Returns active tenant schema name; raises TenantContextError if unbound."""
    schema = get_current_schema()
    if not schema:
        raise TenantContextError(
            "Operation requires an active tenant context, but no tenant is bound. "
            "Ensure X-Client-Slug header is provided or wrap execution in tenant_scope()."
        )
    return schema


def require_current_slug() -> str:
    """Returns active tenant slug; raises TenantContextError if unbound."""
    schema = require_current_schema()
    return slug_from_schema(schema)


# Aliases aligning with specification §5.1
current_schema = require_current_schema
current_slug = require_current_slug


@contextmanager
def tenant_scope(client_slug: str):
    """Context manager for executing a block within a specific tenant context.
    
    Usage:
        with tenant_scope("acme_corp"):
            session = SessionLocal()
            # all queries automatically scoped to client_acme_corp
    """
    token = set_current_tenant(client_slug)
    try:
        yield get_current_schema()
    finally:
        reset_current_tenant(token)


# ── Thread Context Propagation ───────────────────────────────────────────────

def spawn_tenant_thread(
    target,
    args: tuple = (),
    kwargs: Optional[dict] = None,
    name: Optional[str] = None,
    daemon: bool = False,
) -> threading.Thread:
    """Spawns an OS background thread with a complete snapshot of parent ContextVars.
    
    Ensures that background tasks never lose tenant context and never fail open.
    """
    ctx = contextvars.copy_context()
    thread = threading.Thread(
        target=ctx.run,
        args=(target, *args),
        kwargs=kwargs or {},
        name=name,
        daemon=daemon,
    )
    thread.start()
    return thread


# ── Session & Transaction Event Listener (SET LOCAL) ─────────────────────────

@event.listens_for(SessionLocal, "after_begin")
def _bind_tenant_search_path(session, transaction, connection):
    """Binds active tenant schema per transaction via SET LOCAL.
    
    Guarantees:
    1. Search path is set inside the transaction block; reverts cleanly on commit or rollback.
    2. Connection pool safe: search path never leaks to subsequent checkouts (PgBouncer safe).
    3. If session explicitly specifies `session.info['schema']`, that schema is used.
    4. Otherwise, uses active tenant ContextVar if set; falls back to public for platform sessions.
    """
    explicit_schema = session.info.get("schema")
    active_schema = explicit_schema or get_current_schema()

    if active_schema and active_schema != "public":
        # Tenant transaction: search active tenant schema first, then public platform
        connection.exec_driver_sql(f'SET LOCAL search_path = "{active_schema}", public')
    else:
        # Platform transaction: explicitly scope to public only
        connection.exec_driver_sql('SET LOCAL search_path = public')


# ── Session Generators & Helpers ─────────────────────────────────────────────

def get_client_session(client_slug: Optional[str] = None) -> Generator[Session, None, None]:
    """FastAPI dependency yielding an isolated Session scoped to client_<slug>.
    
    Fails closed if client_slug is omitted and no tenant is bound to context.
    """
    slug = client_slug or require_current_slug()
    schema = schema_for(slug)
    with tenant_scope(slug):
        session = SessionLocal()
        session.info["schema"] = schema
        try:
            yield session
        finally:
            session.close()


def get_public_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a Session explicitly scoped to public platform schema."""
    session = SessionLocal()
    session.info["schema"] = "public"
    try:
        yield session
    finally:
        session.close()


@contextmanager
def scoped_client_session(client_slug: str):
    """Context manager for scripts, automation engines, and background tasks.
    
    Usage:
        with scoped_client_session("stradit_india") as session:
            accounts = session.query(Account).all()
    """
    if not client_slug:
        raise ValueError("client_slug is required for scoped_client_session")
    slug = client_slug
    schema = schema_for(slug)
    with tenant_scope(slug):
        session = SessionLocal()
        session.info["schema"] = schema
        try:
            yield session
        finally:
            session.close()
