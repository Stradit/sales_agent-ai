"""Schema Provisioning Service — Dynamically provisions and manages per-client PostgreSQL schemas.

Clones the skeleton schema from public, isolates sequences, establishes
intra-schema foreign keys, and provides data migration capabilities.
"""

import re
from typing import Dict, List, Optional, Any
from sqlalchemy import text, inspect
from sqlalchemy.engine import Engine


RESERVED_SLUGS = {
    "public", "information_schema", "pg_catalog", "template", "postgres",
    "admin", "root", "system", "auth", "api", "default", "core"
}

CLIENT_TABLES_ORDERED = [
    "accounts",
    "lobs",
    "sub_lobs",
    "personas",
    "pipeline_runs",
    "user_account_access",
    "opportunity_signals",
    "weekly_digest_snapshots",
    "linkedin_jobs",
    "cxo_movements",
    "action_items",
    "action_item_reminders",
    "system_api_configs",
    "command_center_snapshots",
    "digests",
    "posts",
]


class SchemaProvisioningService:
    @classmethod
    def create_cross_client_views(cls, engine: Engine) -> Dict[str, str]:
        """
        Super-Admin Cross-Client UNION ALL Views (Decision 3).
        Dynamically constructs in-DB UNION ALL views in public schema over all active client schemas:
        - public.v_all_accounts
        - public.v_all_pipeline_runs
        - public.v_all_personas
        - public.v_all_lobs
        - public.v_all_opportunity_signals
        """
        from sqlalchemy import inspect
        created_views = {}

        with engine.begin() as conn:
            clients = conn.execute(text(
                "SELECT slug, schema_name FROM public.clients WHERE is_active = true ORDER BY id ASC"
            )).fetchall()

            if not clients:
                return created_views

            view_targets = ["accounts", "pipeline_runs", "personas", "lobs", "opportunity_signals"]
            inspector = inspect(engine)

            for target in view_targets:
                parts = []
                for slug, schema in clients:
                    tables = inspector.get_table_names(schema=schema)
                    if target in tables:
                        parts.append(f"SELECT '{slug}'::text AS client_slug, * FROM \"{schema}\".\"{target}\"")

                if parts:
                    view_name = f"v_all_{target}"
                    union_sql = f"CREATE OR REPLACE VIEW public.{view_name} AS " + " UNION ALL ".join(parts)
                    conn.execute(text(union_sql))
                    created_views[view_name] = f"UNION ALL across {len(parts)} tenant schemas"

        return created_views

    """Enterprise-grade dynamic schema provisioning and migration service."""

    @staticmethod
    def validate_slug(client_slug: str) -> str:
        """Validates that a client slug is safe for use as a PostgreSQL schema identifier."""
        if not client_slug:
            raise ValueError("Client slug cannot be empty.")
        slug = client_slug.strip().lower()
        if not re.match(r"^[a-z][a-z0-9_]{1,98}$", slug):
            raise ValueError(
                f"Invalid client slug '{slug}'. Must start with a letter and contain "
                "only lowercase letters, numbers, and underscores (2-99 characters)."
            )
        if slug in RESERVED_SLUGS:
            raise ValueError(f"Client slug '{slug}' is a reserved system name.")
        return slug

    @classmethod
    def get_schema_name(cls, client_slug: str) -> str:
        slug = cls.validate_slug(client_slug)
        return f"client_{slug}"

    @classmethod
    def schema_exists(cls, engine: Engine, client_slug: str) -> bool:
        schema_name = cls.get_schema_name(client_slug)
        insp = inspect(engine)
        return schema_name in insp.get_schema_names()

    @classmethod
    def create_client_schema(cls, engine: Engine, client_slug: str) -> str:
        """Provisions a complete isolated schema for the client from the skeleton.
        
        Steps:
        1. CREATE SCHEMA IF NOT EXISTS
        2. CREATE TABLE ... (LIKE public.<table> INCLUDING ALL) for all 16 client tables
        3. Allocate independent sequence for each table column with a nextval sequence
        4. Establish intra-schema foreign key constraints
        """
        schema_name = cls.get_schema_name(client_slug)
        q = chr(39)

        with engine.begin() as conn:
            # 1. Create Schema
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))

            # 2. Clone Tables from public skeleton
            for table in CLIENT_TABLES_ORDERED:
                conn.execute(text(
                    f'CREATE TABLE IF NOT EXISTS "{schema_name}"."{table}" '
                    f'(LIKE public."{table}" INCLUDING ALL)'
                ))

                # 3. Detect any column that has a sequence in public, and rebind in target schema
                seq_cols = conn.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :tbl AND column_default LIKE '%nextval%'
                """), {"tbl": table}).fetchall()

                for (col_name,) in seq_cols:
                    seq_name = f"{table}_{col_name}_seq"
                    conn.execute(text(
                        f'CREATE SEQUENCE IF NOT EXISTS "{schema_name}"."{seq_name}"'
                    ))
                    conn.execute(text(
                        f'ALTER TABLE "{schema_name}"."{table}" '
                        f'ALTER COLUMN "{col_name}" SET DEFAULT nextval({q}"{schema_name}"."{seq_name}"{q}::regclass)'
                    ))
                    conn.execute(text(
                        f'ALTER SEQUENCE "{schema_name}"."{seq_name}" '
                        f'OWNED BY "{schema_name}"."{table}"."{col_name}"'
                    ))

            # 4. Intra-schema Foreign Keys
            def _add_fk(tbl: str, constraint_name: str, fk_col: str, ref_tbl: str, ref_col: str = "id", on_delete: str = "CASCADE"):
                check_sql = text("""
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_namespace n ON n.oid = c.connamespace
                    WHERE n.nspname = :schema AND c.conname = :cname
                """)
                exists = conn.execute(check_sql, {"schema": schema_name, "cname": constraint_name}).scalar()
                if not exists:
                    conn.execute(text(
                        f'ALTER TABLE "{schema_name}"."{tbl}" '
                        f'ADD CONSTRAINT "{constraint_name}" '
                        f'FOREIGN KEY ({fk_col}) REFERENCES "{schema_name}"."{ref_tbl}"({ref_col}) '
                        f'ON DELETE {on_delete}'
                    ))

            _add_fk("lobs", f"fk_{schema_name}_lobs_account", "account_id", "accounts", on_delete="CASCADE")
            _add_fk("sub_lobs", f"fk_{schema_name}_sublobs_lob", "lob_id", "lobs", on_delete="CASCADE")
            _add_fk("personas", f"fk_{schema_name}_personas_account", "account_id", "accounts", on_delete="CASCADE")
            _add_fk("personas", f"fk_{schema_name}_personas_lob", "lob_id", "lobs", on_delete="SET NULL")
            _add_fk("pipeline_runs", f"fk_{schema_name}_runs_account", "account_id", "accounts", on_delete="SET NULL")
            _add_fk("user_account_access", f"fk_{schema_name}_uaa_account", "account_id", "accounts", on_delete="CASCADE")
            _add_fk("opportunity_signals", f"fk_{schema_name}_opp_account", "account_id", "accounts", on_delete="CASCADE")
            _add_fk("weekly_digest_snapshots", f"fk_{schema_name}_wds_account", "account_id", "accounts", on_delete="CASCADE")
            _add_fk("action_items", f"fk_{schema_name}_ai_account", "account_id", "accounts", on_delete="CASCADE")
            _add_fk("action_items", f"fk_{schema_name}_ai_persona", "persona_id", "personas", on_delete="SET NULL")
            _add_fk("action_item_reminders", f"fk_{schema_name}_air_item", "action_item_id", "action_items", on_delete="CASCADE")

        return schema_name

    @classmethod
    def copy_data_from_public(cls, engine: Engine, client_slug: str) -> Dict[str, int]:
        """Copies existing accounts, lobs, personas, runs, etc. from public into the client schema.
        
        Used for initial migration (e.g. Stradit India PVT LTD).
        Updates sequence values afterwards so future inserts continue seamlessly.
        """
        schema_name = cls.get_schema_name(client_slug)
        stats: Dict[str, int] = {}

        with engine.begin() as conn:
            for table in CLIENT_TABLES_ORDERED:
                # Check if public table has rows
                count = conn.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar() or 0
                if count > 0:
                    # Get common columns between public and client schema table
                    cols_sql = text("""
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = :schema AND table_name = :tbl
                        ORDER BY ordinal_position
                    """)
                    target_cols = [r[0] for r in conn.execute(cols_sql, {"schema": schema_name, "tbl": table}).fetchall()]
                    pub_cols = [r[0] for r in conn.execute(cols_sql, {"schema": "public", "tbl": table}).fetchall()]
                    common_cols = [c for c in target_cols if c in pub_cols]

                    if common_cols:
                        cols_str = ", ".join(f'"{c}"' for c in common_cols)
                        where_clause = ""
                        if "account_id" in common_cols and table != "accounts":
                            where_clause = f' WHERE account_id IS NULL OR account_id IN (SELECT id FROM "{schema_name}"."accounts")'
                        conn.execute(text(
                            f'INSERT INTO "{schema_name}"."{table}" ({cols_str}) '
                            f'SELECT {cols_str} FROM public."{table}"{where_clause} '
                            f'ON CONFLICT DO NOTHING'
                        ))

                # Rebind sequence values to max value
                seq_cols = conn.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :tbl AND column_default LIKE '%nextval%'
                """), {"tbl": table}).fetchall()

                for (col_name,) in seq_cols:
                    seq_name = f"{table}_{col_name}_seq"
                    conn.execute(text(f"""
                        SELECT setval(
                            '"{schema_name}"."{seq_name}"',
                            COALESCE((SELECT MAX("{col_name}") FROM "{schema_name}"."{table}"), 1),
                            (SELECT MAX("{col_name}") IS NOT NULL FROM "{schema_name}"."{table}")
                        )
                    """))

                # Record copied count
                schema_count = conn.execute(text(f'SELECT count(*) FROM "{schema_name}"."{table}"')).scalar() or 0
                stats[table] = schema_count

        return stats

    @classmethod
    def verify_client_schema(cls, engine: Engine, client_slug: str) -> Dict[str, Any]:
        """Returns verification status and row counts for all tables in the client schema."""
        schema_name = cls.get_schema_name(client_slug)
        report: Dict[str, Any] = {"schema": schema_name, "tables": {}}

        with engine.connect() as conn:
            for table in CLIENT_TABLES_ORDERED:
                schema_count = conn.execute(text(f'SELECT count(*) FROM "{schema_name}"."{table}"')).scalar() or 0
                pub_count = conn.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar() or 0
                report["tables"][table] = {
                    "schema_count": schema_count,
                    "public_count": pub_count,
                    "matches": schema_count == pub_count
                }

        return report

    @classmethod
    def verify_schema_integrity(cls, engine: Engine, client_slug: str) -> Dict[str, Any]:
        """Deep integrity check of client PostgreSQL schema, tables, sequences and constraints."""
        schema_name = cls.get_schema_name(client_slug)
        insp = inspect(engine)
        if schema_name not in insp.get_schema_names():
            return {
                "schema_name": schema_name,
                "slug": client_slug,
                "exists": False,
                "status": "missing",
                "is_healthy": False,
                "tables_total": len(CLIENT_TABLES_ORDERED),
                "tables_found": 0,
                "missing_tables": CLIENT_TABLES_ORDERED.copy(),
                "tables_detail": {},
                "total_rows": 0,
            }

        existing_tables = set(insp.get_table_names(schema=schema_name))
        missing_tables = [t for t in CLIENT_TABLES_ORDERED if t not in existing_tables]
        tables_detail: Dict[str, Any] = {}
        total_rows = 0

        with engine.connect() as conn:
            for tbl in CLIENT_TABLES_ORDERED:
                if tbl in existing_tables:
                    try:
                        row_cnt = conn.execute(text(f'SELECT count(*) FROM "{schema_name}"."{tbl}"')).scalar() or 0
                        size_str = conn.execute(text(
                            f"SELECT pg_size_pretty(pg_total_relation_size('\"{schema_name}\".\"{tbl}\"'))"
                        )).scalar() or "0 kB"
                    except Exception:
                        row_cnt = 0
                        size_str = "unknown"

                    total_rows += row_cnt
                    tables_detail[tbl] = {
                        "exists": True,
                        "rows": row_cnt,
                        "size": size_str,
                    }
                else:
                    tables_detail[tbl] = {
                        "exists": False,
                        "rows": 0,
                        "size": "0 kB",
                    }

            # Check foreign keys
            fk_cnt = conn.execute(text("""
                SELECT count(*) FROM pg_constraint c
                JOIN pg_namespace n ON n.oid = c.connamespace
                WHERE n.nspname = :schema AND c.contype = 'f'
            """), {"schema": schema_name}).scalar() or 0

        is_healthy = len(missing_tables) == 0
        return {
            "schema_name": schema_name,
            "slug": client_slug,
            "exists": True,
            "status": "healthy" if is_healthy else "drift_detected",
            "is_healthy": is_healthy,
            "tables_total": len(CLIENT_TABLES_ORDERED),
            "tables_found": len(existing_tables),
            "missing_tables": missing_tables,
            "tables_detail": tables_detail,
            "foreign_keys_count": fk_cnt,
            "total_rows": total_rows,
        }

    @classmethod
    def repair_client_schema(cls, engine: Engine, client_slug: str) -> Dict[str, Any]:
        """Self-healing: Non-destructively provisions any missing tables, sequences, or constraints."""
        schema_name = cls.get_schema_name(client_slug)
        q = chr(39)
        repaired_tables: List[str] = []
        repaired_sequences: List[str] = []
        repaired_fks: List[str] = []

        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
            existing = set(inspect(engine).get_table_names(schema=schema_name))

            for table in CLIENT_TABLES_ORDERED:
                if table not in existing:
                    conn.execute(text(
                        f'CREATE TABLE IF NOT EXISTS "{schema_name}"."{table}" '
                        f'(LIKE public."{table}" INCLUDING ALL)'
                    ))
                    repaired_tables.append(table)

                # Sequences check
                seq_cols = conn.execute(text("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = :tbl AND column_default LIKE '%nextval%'
                """), {"tbl": table}).fetchall()

                for (col_name,) in seq_cols:
                    seq_name = f"{table}_{col_name}_seq"
                    seq_exists = conn.execute(text("""
                        SELECT 1 FROM information_schema.sequences
                        WHERE sequence_schema = :schema AND sequence_name = :seq
                    """), {"schema": schema_name, "seq": seq_name}).scalar()

                    if not seq_exists:
                        conn.execute(text(f'CREATE SEQUENCE IF NOT EXISTS "{schema_name}"."{seq_name}"'))
                        conn.execute(text(
                            f'ALTER TABLE "{schema_name}"."{table}" '
                            f'ALTER COLUMN "{col_name}" SET DEFAULT nextval({q}"{schema_name}"."{seq_name}"{q}::regclass)'
                        ))
                        repaired_sequences.append(seq_name)

            # Intra-schema FKs
            def _ensure_fk(tbl: str, constraint_name: str, fk_col: str, ref_tbl: str, ref_col: str = "id", on_delete: str = "CASCADE"):
                exists = conn.execute(text("""
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_namespace n ON n.oid = c.connamespace
                    WHERE n.nspname = :schema AND c.conname = :cname
                """), {"schema": schema_name, "cname": constraint_name}).scalar()
                if not exists:
                    conn.execute(text(
                        f'ALTER TABLE "{schema_name}"."{tbl}" '
                        f'ADD CONSTRAINT "{constraint_name}" '
                        f'FOREIGN KEY ({fk_col}) REFERENCES "{schema_name}"."{ref_tbl}"({ref_col}) '
                        f'ON DELETE {on_delete}'
                    ))
                    repaired_fks.append(constraint_name)

            _ensure_fk("lobs", f"fk_{schema_name}_lobs_account", "account_id", "accounts", on_delete="CASCADE")
            _ensure_fk("sub_lobs", f"fk_{schema_name}_sublobs_lob", "lob_id", "lobs", on_delete="CASCADE")
            _ensure_fk("personas", f"fk_{schema_name}_personas_account", "account_id", "accounts", on_delete="CASCADE")
            _ensure_fk("personas", f"fk_{schema_name}_personas_lob", "lob_id", "lobs", on_delete="SET NULL")
            _ensure_fk("pipeline_runs", f"fk_{schema_name}_runs_account", "account_id", "accounts", on_delete="SET NULL")
            _ensure_fk("user_account_access", f"fk_{schema_name}_uaa_account", "account_id", "accounts", on_delete="CASCADE")
            _ensure_fk("opportunity_signals", f"fk_{schema_name}_opp_account", "account_id", "accounts", on_delete="CASCADE")
            _ensure_fk("weekly_digest_snapshots", f"fk_{schema_name}_wds_account", "account_id", "accounts", on_delete="CASCADE")
            _ensure_fk("action_items", f"fk_{schema_name}_ai_account", "account_id", "accounts", on_delete="CASCADE")
            _ensure_fk("action_items", f"fk_{schema_name}_ai_persona", "persona_id", "personas", on_delete="SET NULL")
            _ensure_fk("action_item_reminders", f"fk_{schema_name}_air_item", "action_item_id", "action_items", on_delete="CASCADE")

        return {
            "schema_name": schema_name,
            "repaired": True,
            "repaired_tables": repaired_tables,
            "repaired_sequences": repaired_sequences,
            "repaired_foreign_keys": repaired_fks,
            "is_healthy": True,
        }

    @classmethod
    def drop_client_schema(cls, engine: Engine, client_slug: str) -> bool:
        """Safely drops a client schema. Use only for testing or explicit admin requests."""
        schema_name = cls.get_schema_name(client_slug)
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        return True
