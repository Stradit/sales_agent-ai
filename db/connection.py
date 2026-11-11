"""
Database Connection — Engine and Session factory for the local PostgreSQL sales_ai database.
Reads DATABASE_URL from the pipeline .env configuration.
"""

import sys
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool
from sqlalchemy.orm import sessionmaker, Session

# Ensure pipeline root is on path for config import
PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))
import config

engine = create_engine(
    config.DATABASE_URL,
    poolclass=QueuePool,
    pool_size=20,
    max_overflow=30,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    echo=False
)


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session() -> Session:
    """Returns a new database session, automatically scoped to current client schema if set."""
    session = SessionLocal()
    try:
        from db.tenant import get_current_schema
        schema = get_current_schema()
        if schema and schema != "public":
            session.info["schema"] = schema
    except Exception:
        pass
    return session


# ── JSONB Auto-Flag Listener (inline — replaces a separate listeners.py) ────
# Registers a before_flush event that auto-calls flag_modified on all JSONB
# columns of dirty Persona objects, preventing silent missed mutations.

_PERSONA_JSONB_COLS = {
    "raw_data", "osint_feed_manifest", "extended_profile",
    "employment_history", "education_history", "skills",
    "past_companies", "previous_titles", "target_kpis",
    "operational_pain_points", "key_objections", "departments",
}


def _auto_flag_jsonb(session, flush_context, instances=None, *args, **kwargs) -> None:
    """Before every flush, flag all JSONB columns on dirty Persona objects."""
    try:
        from db.models.persona import Persona
        from sqlalchemy.orm.attributes import flag_modified
    except ImportError:
        return

    for obj in session.dirty:
        if not isinstance(obj, Persona):
            continue
        for col in _PERSONA_JSONB_COLS:
            try:
                if getattr(obj, col, None) is not None:
                    flag_modified(obj, col)
            except Exception:
                pass


from sqlalchemy import event as _sa_event
_sa_event.listen(SessionLocal, "before_flush", _auto_flag_jsonb)
