"""Database Session Routing — Dynamic multi-tenant PostgreSQL schema resolution.

Backward-compatibility facade over `db.tenant`.
Provides ContextVar-backed per-request client schema isolation, session generators,
and scoped session context managers.
"""

from typing import Generator, Optional
from contextvars import Token
from sqlalchemy.orm import Session

from db.connection import SessionLocal
import db.tenant as tenant
from db.tenant import (
    DEFAULT_FALLBACK_SLUG as DEFAULT_CLIENT_SLUG,
    DEFAULT_FALLBACK_SCHEMA as DEFAULT_CLIENT_SCHEMA,
    TenantContextError,
    validate_slug,
    schema_for,
    slug_from_schema,
    tenant_scope,
    spawn_tenant_thread,
    get_public_session,
)


def set_client_schema(schema_name: str) -> Token:
    """Sets the active PostgreSQL schema for the current request context."""
    if not schema_name or schema_name == "public":
        # Clear or set to public
        return tenant._current_tenant_schema.set("public")
    slug = slug_from_schema(schema_name)
    return tenant.set_current_tenant(slug)


def reset_client_schema(token: Token) -> None:
    """Resets the active schema to previous state."""
    tenant.reset_current_tenant(token)


def get_client_schema() -> str:
    """Returns the active schema name (e.g. 'client_stradit_india' or 'public')."""
    return tenant.get_current_schema() or DEFAULT_CLIENT_SCHEMA


def get_client_slug() -> str:
    """Returns the current client slug without 'client_' prefix."""
    s = get_client_schema()
    return slug_from_schema(s)


def get_client_session(client_slug: Optional[str] = None) -> Generator[Session, None, None]:
    """FastAPI dependency yielding a Session scoped to client_<slug>."""
    slug = client_slug or get_client_slug() or DEFAULT_CLIENT_SLUG
    yield from tenant.get_client_session(slug)


def scoped_client_session(client_slug: str):
    """Context manager for background tasks, automation engines, and scripts.
    
    Usage:
        with scoped_client_session("stradit_india") as session:
            accounts = session.query(Account).all()
    """
    slug = client_slug or DEFAULT_CLIENT_SLUG
    return tenant.scoped_client_session(slug)
