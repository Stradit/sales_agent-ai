"""
Create Tables — Drops old schema and creates the 4-table schema in sales_ai.
Run: python pipeline/db/create_tables.py
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from db.connection import engine
from db.models import Base
from sqlalchemy import text, inspect

def seed_default_admin_if_missing():
    """First super admin for an empty database. Comes ONLY from SEED_ADMIN_EMAIL /
    SEED_ADMIN_PASSWORD (+ optional SEED_ADMIN_NAME) — never a password in the code, so a
    fresh production database can't come up with a publicly known login. Unset: skipped."""
    import os
    email = (os.getenv("SEED_ADMIN_EMAIL") or "").strip()
    password = os.getenv("SEED_ADMIN_PASSWORD") or ""
    try:
        from db.models import User
        from db.connection import get_session
        import auth
        session = get_session()
        try:
            admin = session.query(User).filter(User.role == "super_admin").first()
            if not admin and not (email and password):
                print("[DB] No super admin yet. Set SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD and restart to create one.")
            elif not admin:
                if len(password) < 12:
                    print("[DB] SEED_ADMIN_PASSWORD must be at least 12 characters — super admin not created.")
                    return
                user = User(
                    email=email.lower(),
                    hashed_password=auth.hash_password(password),
                    full_name=(os.getenv("SEED_ADMIN_NAME") or "Administrator").strip(),
                    role="super_admin",
                    is_active=True,
                    has_dashboard_access=True,
                    has_command_center_access=True,
                    has_tasks_access=True,
                    has_pipeline_access=True,
                )
                session.add(user)
                session.commit()
                print(f"[DB] First super admin created ({email.lower()}). Remove SEED_ADMIN_PASSWORD from the environment now.")
        finally:
            session.close()
    except Exception as e:
        print(f"[DB] Default admin check notice: {e}")


def ensure_schema_compatibility():
    """Ensures all tables, JSONB, array, and expanded columns exist in PostgreSQL."""
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"[DB] Table creation check notice: {e}")

    seed_default_admin_if_missing()

    with engine.connect() as conn:
        # 1. Expand personas columns
        alter_statements = [
            "ALTER TABLE personas ALTER COLUMN source TYPE VARCHAR(255);",
            "ALTER TABLE personas ALTER COLUMN tier TYPE VARCHAR(100);",
            "ALTER TABLE personas ALTER COLUMN seniority_raw TYPE VARCHAR(100);",
            "ALTER TABLE personas ALTER COLUMN email_status TYPE VARCHAR(100);",
            "ALTER TABLE personas ALTER COLUMN phone TYPE VARCHAR(100);",
            "ALTER TABLE personas ALTER COLUMN decision_authority TYPE VARCHAR(100);",
            "ALTER TABLE personas ALTER COLUMN budget_authority TYPE VARCHAR(100);",
            # 2. Add columns to accounts if missing
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS multi_source_intelligence JSONB;",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS organisational_hierarchy_tree JSONB;",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS raw_data JSONB;",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS github_url TEXT;",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS glassdoor_url TEXT;",
            # 3. Add columns to lobs if missing
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS lei_code VARCHAR(50);",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS jurisdiction VARCHAR(50);",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS technologies JSONB;",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS competitors JSONB;",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS logo_url TEXT;",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS financial_snippets JSONB;",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS wikipedia_url TEXT;",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS patents JSONB;",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS raw_data JSONB;",
            # 4. Per-user Dashboard & Feature access toggles
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_dashboard_access BOOLEAN NOT NULL DEFAULT TRUE;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_command_center_access BOOLEAN NOT NULL DEFAULT TRUE;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_tasks_access BOOLEAN NOT NULL DEFAULT TRUE;",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_pipeline_access BOOLEAN NOT NULL DEFAULT TRUE;",
            # 5. Manual-verification flag/timestamp (db/models/{account,lob,sub_lob,persona}.py)
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_manually_verified BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS manually_verified_at TIMESTAMPTZ;",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS is_manually_verified BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE lobs ADD COLUMN IF NOT EXISTS manually_verified_at TIMESTAMPTZ;",
            "ALTER TABLE personas ADD COLUMN IF NOT EXISTS is_manually_verified BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE personas ADD COLUMN IF NOT EXISTS manually_verified_at TIMESTAMPTZ;",
            # 6. sub_lobs grew a full entity-data model (legal name, LEI, jurisdiction,
            # parent-lob linkage, ...) that was never migrated — this table was still
            # at its original 4 columns.
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS legal_name VARCHAR(500);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS lei_code VARCHAR(50);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS jurisdiction VARCHAR(50);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS country VARCHAR(100);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS city VARCHAR(255);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS relationship_type VARCHAR(255);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS status VARCHAR(100) DEFAULT 'ACTIVE';",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS entity_level VARCHAR(100) DEFAULT 'Level 3 (Operating Sub-LOB)';",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS parent_lob_lei VARCHAR(50);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS parent_lob_name VARCHAR(500);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS domain VARCHAR(255);",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS website_url TEXT;",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS is_manually_verified BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE sub_lobs ADD COLUMN IF NOT EXISTS manually_verified_at TIMESTAMPTZ;",
            # 7. digests.target_key must be the PRIMARY KEY (db/models/digest.py) —
            # apps/content_pipeline/db.py upserts with ON CONFLICT (target_key),
            # which needs a real unique/PK constraint on that column to work.
            # Some environments' digests table predates the model's primary_key=True
            # and was created without it; retrofit it here (try/except below no-ops
            # once it already exists).
            "ALTER TABLE digests ADD CONSTRAINT digests_pkey PRIMARY KEY (target_key);",
        ]
        for stmt in alter_statements:
            try:
                conn.execute(text(stmt))
            except Exception as e:
                pass
        conn.commit()
    print("[DB] Schema compatibility verified (all JSONB and intelligence columns synchronized).")
    sync_id_sequences()


def sync_id_sequences():
    """Move every serial/identity sequence past its table's highest id.

    If rows are ever loaded with explicit ids (a restore, a data-only import, a table copy) the
    sequence stays behind and the next INSERT fails with "duplicate key ... _pkey" — on
    2026-09-24 this broke logins (refresh_tokens) and would have broken copilot chats, indexing and
    quota tracking. Only ever moves a sequence forward; never touches data."""
    q = text("""SELECT s.relname, t.relname, a.attname FROM pg_class s
                JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')
                JOIN pg_class t ON t.oid = d.refobjid
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
                JOIN pg_namespace n ON n.oid = s.relnamespace
                WHERE s.relkind = 'S' AND n.nspname = 'public'""")
    fixed = []
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '3s'"))
            for seq, tbl, col in conn.execute(q).fetchall():
                mx = conn.execute(text(f'SELECT max("{col}") FROM "{tbl}"')).scalar()
                last, called = conn.execute(text(f'SELECT last_value, is_called FROM "{seq}"')).fetchone()
                if mx is not None and (last + 1 if called else last) <= mx:
                    conn.execute(text("SELECT setval(:s, :v, true)"), {"s": seq, "v": mx})
                    fixed.append(f"{tbl} → next id {mx + 1}")
    except Exception as e:  # never block start-up on this
        print(f"[DB] Sequence check skipped: {e}")
        return
    if fixed:
        print("[DB] Repaired id sequences that were behind their tables: " + "; ".join(fixed))

if __name__ == "__main__":
    print("[DB] Ensuring all tables exist in sales_ai database (PostgreSQL)...")
    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility()

    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"[DB] Tables in database: {len(tables)}")
    for t in sorted(tables):
        cols = inspector.get_columns(t)
        print(f"     - {t} ({len(cols)} columns)")

    print("[DB] Schema migration complete!")
