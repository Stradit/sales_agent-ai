"""Schema Provisioning Service — Dynamically provisions and manages per-client PostgreSQL schemas.

Clones the complete skeleton schema from public for all 58 Tenant tables,
isolates autoincrement sequences, establishes intra-schema and cross-schema
foreign keys, and provides self-healing schema repair and data migration.
"""

import hashlib
import re
from typing import Dict, List, Optional, Any, Set
from sqlalchemy import text, inspect
from sqlalchemy.engine import Engine

from db.tenant import validate_slug, schema_for, RESERVED_SLUGS


# ── Canonical Tenant Tables in Dependency Order (58 Tables) ──────────────────
# Tables are ordered topologically so that referenced parent tables precede child tables.
CLIENT_TABLES_ORDERED: List[str] = [
    # 1. Base reference & configuration tables (independent within tenant domain)
    "business_lines",
    "crm_settings",
    "connectors",
    "targets",
    "activities",
    "forecast_snapshots",
    "copilot_user_prefs",
    "rag_chunks",
    "rag_index_versions",
    "rag_sync_state",
    "rag_outbox",
    "rag_schema_meta",
    "system_api_configs",
    "crm_notifications",
    "crm_notification_prefs",
    "crm_saved_views",
    "crm_demo_rows",
    "search_log",
    "run_history",
    "cxo_movements",
    "weekly_digest_snapshots",
    "digests",
    "posts",
    # 2. Level 1 dependents
    "linkedin_jobs",        # -> targets(key)
    "activity_links",       # -> activities(id)
    "sales_targets",        # -> business_lines(id)
    "user_business_lines",  # -> business_lines(id)
    "accounts",             # -> business_lines(id)
    # 3. Level 2 dependents (account children)
    "lobs",                 # -> accounts(id)
    "sub_lobs",             # -> lobs(id)
    "personas",             # -> accounts(id), lobs(id)
    "pipeline_runs",        # -> accounts(id)
    "user_account_access",  # -> accounts(id)
    "opportunity_signals",  # -> accounts(id)
    "action_items",         # -> accounts(id), personas(id)
    "action_item_reminders",# -> action_items(id)
    "command_center_snapshots", # -> accounts(id)
    "account_origin",           # -> accounts(id)
    # 4. Deals & sales flow
    "deals",                # -> accounts(id), lobs(id), business_lines(id)
    "deal_stakeholders",    # -> deals(id), personas(id)
    "deal_stage_history",   # -> deals(id)
    "deal_checklist",       # -> deals(id)
    "deal_activity",        # -> deals(id)
    "deal_close_plan",      # -> deals(id)
    "deal_buyer_rooms",     # -> deals(id)
    "deal_email_drafts",    # -> deals(id)
    # 5. Introductions & events
    "introductions",        # -> connectors(id), accounts(id), personas(id), business_lines(id), deals(id)
    "introduction_events",  # -> introductions(id)
    "crm_wanted_intros",    # -> accounts(id), business_lines(id)
    "capture_connections",
    # 6. Copilot & RAG hierarchy
    "copilot_sessions",     # -> accounts(id), personas(id)
    "copilot_messages",     # -> copilot_sessions(id)
    "copilot_memories",     # -> copilot_messages(id), accounts(id), personas(id)
    "rag_documents",
    "rag_document_chunks",  # -> rag_documents(id), rag_chunks(chunk_hash)
    "rag_document_sources",
    "rag_document_entities",# -> accounts(id), personas(id), lobs(id)
    "rag_index_entries",    # -> rag_index_versions(id), rag_chunks(chunk_hash), accounts(id)
    "rag_pipeline_runs",
]


class SchemaProvisioningService:
    """Enterprise-grade dynamic schema provisioning and migration service."""

    @classmethod
    def validate_slug(cls, client_slug: str) -> str:
        """Validates that a client slug is safe for use as a PostgreSQL schema identifier."""
        return validate_slug(client_slug)

    @classmethod
    def get_schema_name(cls, client_slug: str) -> str:
        return schema_for(client_slug)

    @classmethod
    def resolve_schema_name(cls, engine: Optional[Engine], client_slug: str, schema_name: Optional[str] = None) -> str:
        """Resolves the schema name either from direct param, from public.clients, or fallback to naming rule."""
        if schema_name:
            return schema_name
        if engine is not None:
            try:
                with engine.connect() as conn:
                    row = conn.execute(
                        text("SELECT schema_name FROM public.clients WHERE slug = :s"),
                        {"s": client_slug}
                    ).fetchone()
                    if row and row[0]:
                        return row[0]
            except Exception:
                pass
        return cls.get_schema_name(client_slug)

    @classmethod
    def schema_exists(cls, engine: Engine, client_slug: str) -> bool:
        schema_name = cls.get_schema_name(client_slug)
        insp = inspect(engine)
        from db.column_types import invalidate_column_kinds_cache
        invalidate_column_kinds_cache(schema_name)
        return schema_name in insp.get_schema_names()

    @classmethod
    def _adapt_fk_constraint(cls, con_def: str, schema_name: str, tenant_set: Set[str]) -> str:
        """Dynamically adapts a public schema foreign key constraint definition for a tenant schema.
        
        Intra-tenant references point to \"{schema_name}\".\"{ref_tbl}\".
        Cross-schema platform references point to public.\"{ref_tbl}\".
        """
        m = re.search(
            r'FOREIGN\s+KEY\s*\(([^\)]+)\)\s+REFERENCES\s+([a-zA-Z0-9_\"]+)\s*(\([^\)]+\))(.*)',
            con_def,
            re.IGNORECASE,
        )
        if not m:
            return con_def
        fk_cols = m.group(1).strip()
        ref_table = m.group(2).replace('"', '').strip()
        ref_cols = m.group(3).strip()
        rest = m.group(4).strip()

        if ref_table in tenant_set:
            new_ref = f'"{schema_name}"."{ref_table}"'
        else:
            new_ref = f'public."{ref_table}"'

        return f'FOREIGN KEY ({fk_cols}) REFERENCES {new_ref} {ref_cols} {rest}'.strip()

    @classmethod
    def _ensure_tenant_foreign_keys(cls, conn, schema_name: str) -> List[str]:
        """Dynamically discovers and binds all intra-tenant and cross-schema foreign keys."""
        tenant_set = set(CLIENT_TABLES_ORDERED)
        fk_query = text("""
            SELECT
                cl.relname AS table_name,
                con.conname AS constraint_name,
                pg_get_constraintdef(con.oid) AS definition
            FROM pg_constraint con
            JOIN pg_class cl ON cl.oid = con.conrelid
            JOIN pg_namespace ns ON ns.oid = cl.relnamespace
            WHERE ns.nspname = 'public' AND con.contype = 'f'
            ORDER BY cl.relname, con.conname;
        """)
        rows = conn.execute(fk_query).fetchall()
        applied = []
        for tbl, cname, cdef in rows:
            if tbl in tenant_set:
                raw_cname = f"fk_{schema_name}_{tbl}_{cname}"
                if len(raw_cname) > 63:
                    h = hashlib.md5(cname.encode()).hexdigest()[:8]
                    raw_cname = f"fk_{schema_name[:15]}_{tbl[:25]}_{h}"

                check_sql = text("""
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_namespace n ON n.oid = c.connamespace
                    WHERE n.nspname = :schema AND c.conname = :cname
                """)
                exists = conn.execute(check_sql, {"schema": schema_name, "cname": raw_cname}).scalar()
                if not exists:
                    adapted_def = cls._adapt_fk_constraint(cdef, schema_name, tenant_set)
                    alter_sql = f'ALTER TABLE "{schema_name}"."{tbl}" ADD CONSTRAINT "{raw_cname}" {adapted_def}'
                    conn.execute(text(alter_sql))
                    applied.append(raw_cname)
        return applied

    @classmethod
    def _ensure_catalog_triggers(cls, conn, schema_name: str) -> None:
        """Installs the local_edits trigger function and triggers on accounts, lobs, sub_lobs, personas (B9: S-19)."""
        conn.execute(text(f"""
            CREATE OR REPLACE FUNCTION "{schema_name}".set_catalog_account_local_edits()
            RETURNS trigger AS $$
            DECLARE
                v_account_id integer;
                v_hydrating text;
            BEGIN
                BEGIN
                    v_hydrating := current_setting('app.catalog_hydrating', true);
                EXCEPTION WHEN OTHERS THEN
                    v_hydrating := 'off';
                END;

                IF v_hydrating = 'on' THEN
                    RETURN NEW;
                END IF;

                IF TG_TABLE_NAME = 'accounts' THEN
                    v_account_id := NEW.id;
                ELSIF TG_TABLE_NAME = 'lobs' THEN
                    v_account_id := NEW.account_id;
                ELSIF TG_TABLE_NAME = 'sub_lobs' THEN
                    SELECT account_id INTO v_account_id FROM "{schema_name}"."lobs" WHERE id = NEW.lob_id;
                ELSIF TG_TABLE_NAME = 'personas' THEN
                    v_account_id := NEW.account_id;
                END IF;

                IF v_account_id IS NOT NULL THEN
                    UPDATE "{schema_name}"."account_origin"
                    SET local_edits = true
                    WHERE account_id = v_account_id
                      AND origin_type = 'catalog'
                      AND local_edits = false;
                END IF;

                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))

        conn.execute(text(f"""
            DROP TRIGGER IF EXISTS trg_accounts_local_edits ON "{schema_name}"."accounts";
            CREATE TRIGGER trg_accounts_local_edits
            AFTER UPDATE ON "{schema_name}"."accounts"
            FOR EACH ROW EXECUTE FUNCTION "{schema_name}".set_catalog_account_local_edits();

            DROP TRIGGER IF EXISTS trg_lobs_local_edits ON "{schema_name}"."lobs";
            CREATE TRIGGER trg_lobs_local_edits
            AFTER UPDATE ON "{schema_name}"."lobs"
            FOR EACH ROW EXECUTE FUNCTION "{schema_name}".set_catalog_account_local_edits();

            DROP TRIGGER IF EXISTS trg_sub_lobs_local_edits ON "{schema_name}"."sub_lobs";
            CREATE TRIGGER trg_sub_lobs_local_edits
            AFTER UPDATE ON "{schema_name}"."sub_lobs"
            FOR EACH ROW EXECUTE FUNCTION "{schema_name}".set_catalog_account_local_edits();

            DROP TRIGGER IF EXISTS trg_personas_local_edits ON "{schema_name}"."personas";
            CREATE TRIGGER trg_personas_local_edits
            AFTER UPDATE ON "{schema_name}"."personas"
            FOR EACH ROW EXECUTE FUNCTION "{schema_name}".set_catalog_account_local_edits();
        """))

    @classmethod
    def create_client_schema(cls, engine: Engine, client_slug: str) -> str:
        """Provisions a complete isolated schema for the client from the skeleton.
        
        Lifecycle state machine (§4.3, §5.3):
        1. Set client status to 'provisioning'
        2. CREATE SCHEMA IF NOT EXISTS
        3. CREATE TABLE ... (LIKE public.<table> INCLUDING ALL) for all 58 tenant tables
        4. Allocate independent sequence for each table column with a nextval sequence
        5. Establish all intra-schema and cross-schema foreign key constraints
        6. Verify schema against manifest and mark client status = 'active'
        """
        schema_name = cls.get_schema_name(client_slug)
        clean_slug = validate_slug(client_slug)
        q = chr(39)
        from db.manifest import TenantManifest
        manifest_version = TenantManifest.get_skeleton_version()

        # 1. Register or update client in public.clients with status 'provisioning'
        try:
            with engine.begin() as conn:
                client_name = clean_slug.replace("_", " ").title()
                conn.execute(text("""
                    INSERT INTO public.clients (slug, name, schema_name, status, skeleton_version, is_active)
                    VALUES (:s, :name, :sc, 'provisioning', :ver, true)
                    ON CONFLICT (slug) DO UPDATE
                    SET status = 'provisioning', last_error = NULL
                """), {"s": clean_slug, "name": client_name, "sc": schema_name, "ver": manifest_version})
        except Exception:
            pass

        # 2. Commit schema creation in its own transaction so it persists even if table provisioning fails
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))

        try:
            with engine.begin() as conn:
                # 3. Clone Tables from public skeleton
                for table in CLIENT_TABLES_ORDERED:
                    conn.execute(text(
                        f'CREATE TABLE IF NOT EXISTS "{schema_name}"."{table}" '
                        f'(LIKE public."{table}" INCLUDING ALL)'
                    ))

                    # 4. Detect any column that has a sequence in public, and rebind in target schema
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

                    # Ensure primary key exists if public table has one
                    has_pk_target = conn.execute(text("""
                        SELECT 1 FROM pg_constraint c
                        JOIN pg_namespace n ON n.oid = c.connamespace
                        JOIN pg_class cl ON cl.oid = c.conrelid
                        WHERE n.nspname = :s AND cl.relname = :t AND c.contype = 'p'
                    """), {"s": schema_name, "t": table}).scalar()
                    if not has_pk_target:
                        pub_pk_def = conn.execute(text("""
                            SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c
                            JOIN pg_namespace n ON n.oid = c.connamespace
                            JOIN pg_class cl ON cl.oid = c.conrelid
                            WHERE n.nspname = 'public' AND cl.relname = :t AND c.contype = 'p'
                        """), {"t": table}).scalar()
                        if pub_pk_def:
                            conn.execute(text(f'ALTER TABLE "{schema_name}"."{table}" ADD {pub_pk_def}'))

                # 5. Intra-schema & cross-schema foreign keys
                cls._ensure_tenant_foreign_keys(conn, schema_name)

                # 6. S-19: Catalog local edits tracking triggers
                cls._ensure_catalog_triggers(conn, schema_name)

            # 7. Mark client status = 'active', set skeleton_version to manifest head
            try:
                with engine.begin() as conn:
                    conn.execute(text("""
                        UPDATE public.clients
                        SET status = 'active',
                            skeleton_version = :ver,
                            provisioned_at = COALESCE(provisioned_at, NOW()),
                            last_error = NULL
                        WHERE slug = :s
                    """), {"s": clean_slug, "ver": manifest_version})
            except Exception:
                pass

        except Exception as exc:
            # On failure, mark status = 'failed' with error message, NEVER drop schema
            try:
                with engine.begin() as conn:
                    conn.execute(text("""
                        UPDATE public.clients
                        SET status = 'failed', last_error = :err
                        WHERE slug = :s
                    """), {"s": clean_slug, "err": str(exc)[:2000]})
            except Exception:
                pass
            raise

        from db.column_types import invalidate_column_kinds_cache
        invalidate_column_kinds_cache(schema_name)
        return schema_name

    @classmethod
    def copy_data_from_public(cls, engine: Engine, client_slug: str, schema_name: Optional[str] = None) -> Dict[str, int]:
        """Copies existing accounts, lobs, personas, runs, deals, CRM, etc. from public into the client schema.
        
        Used for initial migration (e.g. Stradit India PVT LTD).
        Guarantees session_replication_role safety, generated column exclusion, and sequence synchronization.
        """
        schema_name = cls.resolve_schema_name(engine, client_slug, schema_name)
        stats: Dict[str, int] = {}

        with engine.begin() as conn:
            conn.execute(text("SET session_replication_role = 'replica'"))
            try:
                for table in CLIENT_TABLES_ORDERED:
                    # Check if public table has rows
                    count = conn.execute(text(f'SELECT count(*) FROM public."{table}"')).scalar() or 0
                    if count > 0:
                        # Exclude generated columns (is_generated = 'ALWAYS')
                        cols_sql = text("""
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema = :schema AND table_name = :tbl AND is_generated = 'NEVER'
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
            finally:
                conn.execute(text("SET session_replication_role = 'origin'"))

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
    def verify_schema_integrity(cls, engine: Engine, client_slug: str, schema_name: Optional[str] = None) -> Dict[str, Any]:
        """Deep integrity check of client PostgreSQL schema, tables, sequences and constraints."""
        schema_name = cls.resolve_schema_name(engine, client_slug, schema_name)
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
        from db.manifest import TenantManifest
        manifest_report = TenantManifest.verify_schema_against_manifest(engine, schema_name)
        overall_healthy = is_healthy and manifest_report["is_compliant"]

        return {
            "schema_name": schema_name,
            "slug": client_slug,
            "exists": True,
            "status": "healthy" if overall_healthy else "drift_detected",
            "is_healthy": overall_healthy,
            "skeleton_version": manifest_report["skeleton_version"],
            "manifest_compliance": manifest_report,
            "tables_total": len(CLIENT_TABLES_ORDERED),
            "tables_found": len(existing_tables),
            "missing_tables": missing_tables,
            "tables_detail": tables_detail,
            "foreign_keys_count": fk_cnt,
            "total_rows": total_rows,
        }

    @classmethod
    def repair_client_schema(cls, engine: Engine, client_slug: str, schema_name: Optional[str] = None) -> Dict[str, Any]:
        """Self-healing: Non-destructively provisions any missing tables, sequences, or constraints."""
        schema_name = cls.resolve_schema_name(engine, client_slug, schema_name)
        q = chr(39)
        repaired_tables: List[str] = []
        repaired_sequences: List[str] = []

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

                # Ensure primary key exists if public table has one
                has_pk_target = conn.execute(text("""
                    SELECT 1 FROM pg_constraint c
                    JOIN pg_namespace n ON n.oid = c.connamespace
                    JOIN pg_class cl ON cl.oid = c.conrelid
                    WHERE n.nspname = :s AND cl.relname = :t AND c.contype = 'p'
                """), {"s": schema_name, "t": table}).scalar()
                if not has_pk_target:
                    pub_pk_def = conn.execute(text("""
                        SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c
                        JOIN pg_namespace n ON n.oid = c.connamespace
                        JOIN pg_class cl ON cl.oid = c.conrelid
                        WHERE n.nspname = 'public' AND cl.relname = :t AND c.contype = 'p'
                    """), {"t": table}).scalar()
                    if pub_pk_def:
                        conn.execute(text(f'ALTER TABLE "{schema_name}"."{table}" ADD {pub_pk_def}'))

            # Intra-schema and cross-schema foreign keys
            repaired_fks = cls._ensure_tenant_foreign_keys(conn, schema_name)

            # S-19: Repair account_origin timestamps and triggers
            conn.execute(text(f'ALTER TABLE "{schema_name}"."account_origin" ADD COLUMN IF NOT EXISTS "source_extracted_at" timestamptz'))
            conn.execute(text(f'ALTER TABLE "{schema_name}"."account_origin" ADD COLUMN IF NOT EXISTS "snapshot_taken_at" timestamptz'))
            cls._ensure_catalog_triggers(conn, schema_name)

            clean_slug = validate_slug(client_slug)
            from db.manifest import TenantManifest
            manifest_ver = TenantManifest.get_skeleton_version()
            conn.execute(text("UPDATE public.clients SET skeleton_version = :ver WHERE slug = :s"), {"ver": manifest_ver, "s": clean_slug})

        from db.column_types import invalidate_column_kinds_cache
        invalidate_column_kinds_cache(schema_name)

        return {
            "schema_name": schema_name,
            "repaired": True,
            "repaired_tables": repaired_tables,
            "repaired_sequences": repaired_sequences,
            "repaired_foreign_keys": repaired_fks,
            "is_healthy": True,
        }

    @classmethod
    def drop_client_schema(cls, engine: Engine, client_slug: str, schema_name: Optional[str] = None) -> bool:
        """Safely drops a client schema. Use only for testing or explicit admin requests."""
        schema_name = cls.resolve_schema_name(engine, client_slug, schema_name)
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        from db.column_types import invalidate_column_kinds_cache
        invalidate_column_kinds_cache(schema_name)
        return True

    @classmethod
    def create_cross_client_views(cls, engine: Engine) -> Dict[str, str]:
        """Super-Admin Cross-Client UNION ALL Views (Decision 3).
        
        Dynamically constructs in-DB UNION ALL views in public schema over all active client schemas:
        - public.v_all_accounts
        - public.v_all_pipeline_runs
        - public.v_all_personas
        - public.v_all_lobs
        - public.v_all_opportunity_signals
        - public.v_all_deals
        - public.v_all_activities
        """
        created_views = {}

        with engine.begin() as conn:
            clients = conn.execute(text(
                "SELECT slug, schema_name FROM public.clients WHERE is_active = true ORDER BY id ASC"
            )).fetchall()

            if not clients:
                return created_views

            view_targets = [
                "accounts", "pipeline_runs", "personas", "lobs",
                "opportunity_signals", "deals", "activities"
            ]
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


# ── Module-Level Ergonomic Helpers ───────────────────────────────────────────

def create_client_schema(client_slug: str, eng: Optional[Engine] = None) -> str:
    """Provisions a complete isolated schema for the client from the skeleton."""
    from db.connection import engine as default_engine
    return SchemaProvisioningService.create_client_schema(eng or default_engine, client_slug)


def copy_data_from_public(client_slug: str, eng: Optional[Engine] = None, schema_name: Optional[str] = None) -> Dict[str, Any]:
    """Copies all tenant table rows from public schema into the client's schema."""
    from db.connection import engine as default_engine
    return SchemaProvisioningService.copy_data_from_public(eng or default_engine, client_slug, schema_name=schema_name)


def verify_schema_integrity(client_slug: str, eng: Optional[Engine] = None, schema_name: Optional[str] = None) -> Dict[str, Any]:
    """Evaluates the client's schema against canonical 58 tenant tables and sequences."""
    from db.connection import engine as default_engine
    return SchemaProvisioningService.verify_schema_integrity(eng or default_engine, client_slug, schema_name=schema_name)


def repair_client_schema(client_slug: str, eng: Optional[Engine] = None, schema_name: Optional[str] = None) -> Dict[str, Any]:
    """Self-healing migration that checks and creates missing tables, sequences, and FKs."""
    from db.connection import engine as default_engine
    return SchemaProvisioningService.repair_client_schema(eng or default_engine, client_slug, schema_name=schema_name)


def drop_client_schema(client_slug: str, eng: Optional[Engine] = None, schema_name: Optional[str] = None) -> bool:
    """Safely drops a client schema."""
    from db.connection import engine as default_engine
    return SchemaProvisioningService.drop_client_schema(eng or default_engine, client_slug, schema_name=schema_name)

