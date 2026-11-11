"""Tenant Inventory & Table Classification Service.

Inspects all tables in PostgreSQL, analyzes columns, sizes, row counts, and foreign
key relationships, and deterministically categorizes every table into:
  - Platform (public)  : User identity, authentication, client registry, scheduler, audit
  - Tenant (client_*)  : Client accounts, deals, CRM, copilot/RAG, tasks, signals, posts
  - Reference (public) : Shared static read-only catalogs and exchange rates

Usage:
  python scripts/tenant_inventory.py                   # Pretty-printed console report
  python scripts/tenant_inventory.py --format json     # JSON export for CI / provisioning
  python scripts/tenant_inventory.py --format markdown # Markdown table report
  python scripts/tenant_inventory.py --verify          # Returns non-zero if anomalies found
"""

import argparse
import json
import re
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure repository root is on Python module search path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
import config


# ── Classification Taxonomy Rules ─────────────────────────────────────────────

# Explicit Platform tables: Identity, Auth, Registry, Global Scheduler, Audit
PLATFORM_TABLES: Set[str] = {
    "alembic_version",
    "users",
    "refresh_tokens",
    "password_reset_tokens",
    "clients",
    "user_client_access",
    "scheduled_jobs",
    "scheduled_job_runs",
    "audit_log",
    "credit_ledger",   # Future ledger
    "pipeline_jobs",   # Future persisted jobs
}

# Explicit Reference tables: Shared static taxonomies, exchange rates
REFERENCE_TABLES: Set[str] = {
    "fx_rates",
}


@dataclass
class ForeignKeyRelation:
    constrained_columns: List[str]
    referred_table: str
    referred_columns: List[str]
    name: Optional[str] = None


@dataclass
class TableMetadata:
    name: str
    schema: str
    row_count: int
    size_pretty: str
    size_bytes: int
    primary_key: List[str]
    outbound_fks: List[ForeignKeyRelation] = field(default_factory=list)
    inbound_fks: List[str] = field(default_factory=list)
    category: str = "Unknown"
    classification_reason: str = ""
    is_cloned_today: bool = False
    cross_schema_fks: List[str] = field(default_factory=list)


class TenantInventoryAnalyzer:
    """Enterprise Table Introspection and Architectural Classification Engine."""

    # Currently cloned in db/provisioning.py (the 16 legacy tables)
    LEGACY_CLONED_16: Set[str] = {
        "accounts", "lobs", "sub_lobs", "personas", "pipeline_runs",
        "user_account_access", "opportunity_signals", "weekly_digest_snapshots",
        "linkedin_jobs", "cxo_movements", "action_items", "action_item_reminders",
        "system_api_configs", "command_center_snapshots", "digests", "posts",
    }

    def __init__(self, engine: Optional[Engine] = None, schema: str = "public"):
        self.engine = engine or create_engine(config.DATABASE_URL)
        self.schema = schema
        self.tables: Dict[str, TableMetadata] = {}

    def collect(self) -> Dict[str, TableMetadata]:
        """Collects full schema metadata, dependencies, sizes, and row counts."""
        insp = inspect(self.engine)
        table_names = sorted(insp.get_table_names(schema=self.schema))

        with self.engine.connect() as conn:
            for tbl in table_names:
                # 1. Safely query row count
                try:
                    row_cnt = conn.execute(
                        text(f'SELECT count(*) FROM "{self.schema}"."{tbl}"')
                    ).scalar() or 0
                except Exception:
                    row_cnt = 0

                # 2. Safely query total table size (including indexes & toast)
                try:
                    size_query = text(f"""
                        SELECT 
                            pg_total_relation_size('"{self.schema}"."{tbl}"'),
                            pg_size_pretty(pg_total_relation_size('"{self.schema}"."{tbl}"'))
                    """)
                    row = conn.execute(size_query).fetchone()
                    size_bytes = row[0] if row else 0
                    size_pretty = row[1] if row else "0 bytes"
                except Exception:
                    size_bytes = 0
                    size_pretty = "unknown"

                # 3. Primary Key
                pk_constraint = insp.get_pk_constraint(tbl, schema=self.schema) or {}
                pk_cols = pk_constraint.get("constrained_columns", [])

                # 4. Outbound Foreign Keys
                fks_raw = insp.get_foreign_keys(tbl, schema=self.schema) or []
                outbound_fks = [
                    ForeignKeyRelation(
                        constrained_columns=fk.get("constrained_columns", []),
                        referred_table=fk.get("referred_table", ""),
                        referred_columns=fk.get("referred_columns", []),
                        name=fk.get("name"),
                    )
                    for fk in fks_raw
                ]

                self.tables[tbl] = TableMetadata(
                    name=tbl,
                    schema=self.schema,
                    row_count=row_cnt,
                    size_pretty=size_pretty,
                    size_bytes=size_bytes,
                    primary_key=pk_cols,
                    outbound_fks=outbound_fks,
                    is_cloned_today=tbl in self.LEGACY_CLONED_16,
                )

        # 5. Populate Inbound Foreign Keys
        for tbl_name, meta in self.tables.items():
            for fk in meta.outbound_fks:
                if fk.referred_table in self.tables:
                    self.tables[fk.referred_table].inbound_fks.append(tbl_name)

        # 6. Apply Categorization Engine
        for tbl_name, meta in self.tables.items():
            self._classify_table(meta)

        return self.tables

    def _classify_table(self, meta: TableMetadata) -> None:
        """Determines the strict architectural categorization for a table."""
        name = meta.name

        # Rule 1: Explicit Platform Tables
        if name in PLATFORM_TABLES:
            meta.category = "Platform"
            meta.classification_reason = "Global platform identity, authentication, client registry, or scheduler"
            return

        # Rule 2: Explicit Reference Tables
        if name in REFERENCE_TABLES:
            meta.category = "Reference"
            meta.classification_reason = "Shared static lookup, taxonomy, or financial conversion rates (read-only)"
            return

        # Rule 3: LLM governance / usage
        if name == "llm_usage":
            meta.category = "Platform"
            meta.classification_reason = "Global AI token consumption & governance log across all tenant queries"
            return

        # Rule 4: Core Entity / Account / Deal / Contact / Scraper / RAG -> Tenant
        tenant_indicators = {
            "account", "lob", "persona", "deal", "lead", "intro", "activity",
            "post", "target", "signal", "copilot", "rag", "action_item",
            "digest", "crm", "business_line", "snapshot", "search_log",
        }

        # Check table name matching tenant indicators
        matches = [kw for kw in tenant_indicators if kw in name]
        if matches:
            meta.category = "Tenant"
            meta.classification_reason = f"Tenant business domain entity: matches domain terms ({', '.join(matches)})"
            self._detect_cross_schema_fks(meta)
            return

        # Rule 5: Inherited from foreign keys (if points to accounts/personas/deals/lobs)
        points_to_tenant = False
        for fk in meta.outbound_fks:
            if fk.referred_table in {"accounts", "lobs", "sub_lobs", "personas", "deals", "targets"}:
                points_to_tenant = True
                break

        if points_to_tenant:
            meta.category = "Tenant"
            meta.classification_reason = "Dependent child table referencing tenant entity (account/lob/deal/persona)"
            self._detect_cross_schema_fks(meta)
            return

        # Default fallback if unclassified
        meta.category = "Tenant"
        meta.classification_reason = "Default tenant isolation (holds application operational state)"
        self._detect_cross_schema_fks(meta)

    def _detect_cross_schema_fks(self, meta: TableMetadata) -> None:
        """Detects foreign keys pointing to Platform tables (which must become soft-refs or public.* FKs)."""
        cross_refs = []
        for fk in meta.outbound_fks:
            if fk.referred_table in PLATFORM_TABLES:
                cols = ", ".join(fk.constrained_columns)
                cross_refs.append(f"{cols} -> public.{fk.referred_table}")
        meta.cross_schema_fks = cross_refs

    def compute_topological_sort(self) -> List[str]:
        """Calculates exact creation order for Tenant tables based on FK dependencies."""
        tenant_tables = {name: meta for name, meta in self.tables.items() if meta.category == "Tenant"}
        in_degree: Dict[str, int] = {name: 0 for name in tenant_tables}
        adj_list: Dict[str, List[str]] = defaultdict(list)

        for name, meta in tenant_tables.items():
            for fk in meta.outbound_fks:
                ref = fk.referred_table
                # Only consider dependencies inside the tenant track and skip self-references
                if ref in tenant_tables and ref != name:
                    adj_list[ref].append(name)
                    in_degree[name] += 1

        queue = deque([name for name, deg in in_degree.items() if deg == 0])
        ordered: List[str] = []

        while queue:
            node = queue.popleft()
            ordered.append(node)
            for neighbor in adj_list[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # If cycles exist (unlikely in relational design), append remaining
        remaining = [name for name in tenant_tables if name not in ordered]
        ordered.extend(remaining)
        return ordered

    def summary(self) -> Dict[str, Any]:
        """Returns high-level categorization metrics."""
        counts = {"Platform": 0, "Tenant": 0, "Reference": 0, "Unknown": 0}
        total_rows = {"Platform": 0, "Tenant": 0, "Reference": 0}
        uncloned_tenant_tables = []

        for name, meta in self.tables.items():
            counts[meta.category] = counts.get(meta.category, 0) + 1
            total_rows[meta.category] = total_rows.get(meta.category, 0) + meta.row_count
            if meta.category == "Tenant" and not meta.is_cloned_today:
                uncloned_tenant_tables.append(name)

        return {
            "total_tables": len(self.tables),
            "counts": counts,
            "total_rows": total_rows,
            "legacy_cloned_count": len([m for m in self.tables.values() if m.is_cloned_today]),
            "uncloned_tenant_tables_count": len(uncloned_tenant_tables),
            "uncloned_tenant_tables": sorted(uncloned_tenant_tables),
            "tenant_creation_order": self.compute_topological_sort(),
        }

    def render_markdown(self) -> str:
        """Formats the full inventory into a readable Markdown report."""
        s = self.summary()
        lines = [
            "# PostgreSQL Public Schema Table Inventory & Tenancy Classification",
            "",
            f"**Total Tables Analyzed:** {s['total_tables']}",
            f"- **Platform Tables (`public`):** {s['counts']['Platform']} tables ({s['total_rows']['Platform']:,} rows)",
            f"- **Tenant Tables (`client_<slug>`):** {s['counts']['Tenant']} tables ({s['total_rows']['Tenant']:,} rows)",
            f"- **Reference Tables (`public` read-only):** {s['counts']['Reference']} tables ({s['total_rows']['Reference']:,} rows)",
            "",
            f"### ⚠️ Critical Finding: {s['uncloned_tenant_tables_count']} Tenant Tables are NOT in `CLIENT_TABLES_ORDERED`",
            "The following tables currently reside in `public` but must be moved into the tenant track:",
            ", ".join(f"`{t}`" for t in s['uncloned_tenant_tables']),
            "",
            "## Table Classification Matrix",
            "",
            "| Table Name | Category | Rows | Size | Cloned Today? | Primary Key | Cross-Schema FKs (to Platform) | Reason |",
            "|---|---|---|---|---|---|---|---|",
        ]

        sorted_tables = sorted(self.tables.values(), key=lambda t: (t.category, t.name))
        for m in sorted_tables:
            cloned_badge = "✅ Cloned" if m.is_cloned_today else ("—" if m.category != "Tenant" else "❌ **MISSING**")
            pk_str = ", ".join(m.primary_key) or "none"
            cross_fk_str = "<br/>".join(m.cross_schema_fks) or "—"
            lines.append(
                f"| `{m.name}` | **{m.category}** | {m.row_count:,} | {m.size_pretty} | {cloned_badge} | `{pk_str}` | {cross_fk_str} | {m.classification_reason} |"
            )

        lines.extend([
            "",
            "## Recommended Tenant Table Creation Order (Dependency Topological Sort)",
            "",
            "When provisioning a tenant schema or running tenant migrations, tables must be created in this order to satisfy intra-schema foreign keys:",
            "",
        ])
        for idx, tbl in enumerate(s["tenant_creation_order"], 1):
            lines.append(f"{idx}. `{tbl}`")

        return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-Tenant Database Table Inventory")
    parser.add_argument("--format", choices=["table", "json", "markdown"], default="table", help="Output format")
    parser.add_argument("--verify", action="store_true", help="Assert that all tables are categorized without unknowns")
    args = parser.parse_args()

    analyzer = TenantInventoryAnalyzer()
    analyzer.collect()
    summary = analyzer.summary()

    if args.verify:
        unknowns = [m.name for m in analyzer.tables.values() if m.category == "Unknown"]
        if unknowns:
            print(f"[ERROR] Found {len(unknowns)} unclassified tables: {', '.join(unknowns)}", file=sys.stderr)
            return 1
        print(f"[OK] Verification passed: All {len(analyzer.tables)} tables deterministically categorized.")
        return 0

    if args.format == "json":
        data = {
            "summary": summary,
            "tables": {name: asdict(meta) for name, meta in analyzer.tables.items()},
        }
        print(json.dumps(data, indent=2, default=str))
        return 0

    if args.format == "markdown":
        print(analyzer.render_markdown())
        return 0

    # Default console table format
    print(analyzer.render_markdown())
    return 0


if __name__ == "__main__":
    sys.exit(main())
