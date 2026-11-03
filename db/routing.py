"""Database Session Routing — Dynamic multi-tenant PostgreSQL schema resolution.

Handles:
1. ContextVar for per-request client schema isolation across async & sync code.
2. Connection pool checkout event listener guaranteeing active schema configuration per checkout.
3. Dependency-injectable session generators: get_client_session(), get_public_session().
4. Context manager scoped_client_session() for scripts and background jobs.
"""

from contextvars import ContextVar, Token
from typing import Generator, Optional
from contextlib import contextmanager
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from db.connection import engine, SessionLocal

DEFAULT_CLIENT_SLUG = "stradit_india"
DEFAULT_CLIENT_SCHEMA = f"client_{DEFAULT_CLIENT_SLUG}"

_current_client_schema: ContextVar[str] = ContextVar(
    "current_client_schema",
    default=DEFAULT_CLIENT_SCHEMA
)


def set_client_schema(schema_name: str) -> Token:
    """Sets the active PostgreSQL schema for the current request context."""
    return _current_client_schema.set(schema_name)


def reset_client_schema(token: Token) -> None:
    """Resets the active schema to previous state."""
    try:
        _current_client_schema.reset(token)
    except (ValueError, RuntimeError):
        pass


def get_client_schema() -> str:
    """Returns the active schema name (e.g. 'client_stradit_india' or 'public')."""
    return _current_client_schema.get()


def get_client_slug() -> str:
    """Returns the current client slug without 'client_' prefix."""
    s = get_client_schema()
    return s.replace("client_", "") if s.startswith("client_") else s


def get_client_session(client_slug: Optional[str] = None) -> Generator[Session, None, None]:
    """FastAPI dependency yielding a Session scoped to client_<slug>."""
    slug = client_slug or get_client_slug() or DEFAULT_CLIENT_SLUG
    schema_name = f"client_{slug}" if not slug.startswith("client_") else slug
    token = set_client_schema(schema_name)
    session = SessionLocal()
    try:
        session.execute(text(f'SET search_path = "{schema_name}", public'))
        yield session
    finally:
        session.close()
        reset_client_schema(token)


def get_public_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a Session explicitly scoped to public."""
    token = set_client_schema("public")
    session = SessionLocal()
    try:
        session.execute(text('SET search_path = public'))
        yield session
    finally:
        session.close()
        reset_client_schema(token)


@contextmanager
def scoped_client_session(client_slug: str):
    """Context manager for background tasks, automation engines, and scripts.
    
    Usage:
        with scoped_client_session("stradit_india") as session:
            accounts = session.query(Account).all()
    """
    slug = client_slug or DEFAULT_CLIENT_SLUG
    schema_name = f"client_{slug}" if not slug.startswith("client_") else slug
    token = set_client_schema(schema_name)
    session = SessionLocal()
    try:
        session.execute(text(f'SET search_path = "{schema_name}", public'))
        yield session
    finally:
        session.close()
        reset_client_schema(token)


# ── Connection Pool Hook ─────────────────────────────────────────────────────
# On EVERY connection checkout from the SQLAlchemy pool, inspect the current
# ContextVar and set search_path accordingly. This guarantees that:
# 1. Any lazy reload, refresh, or subsequent transaction on this connection uses the right schema.
# 2. No connection leaks search_path to another thread or request.

@event.listens_for(engine, "checkout")
def _set_search_path_on_checkout(dbapi_conn, connection_record, connection_proxy):
    try:
        schema = _current_client_schema.get()
        cursor = dbapi_conn.cursor()
        if schema and schema != "public":
            cursor.execute(f'SET search_path = "{schema}", public')
        else:
            cursor.execute("SET search_path = public")
        cursor.close()
    except Exception:
        pass
