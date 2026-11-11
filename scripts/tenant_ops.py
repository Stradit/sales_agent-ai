"""Phase 7 Tenant Operations, Telemetry, Export & Offboarding CLI.

Provides platform administrators with commands for:
1. Per-tenant disk usage, row counts, credit telemetry & health metrics.
2. Complete per-tenant data & metadata export (JSON or SQL).
3. Secure tenant offboarding & schema purge with audit trail logging.

Usage:
  python scripts/tenant_ops.py metrics [--slug <slug>]
  python scripts/tenant_ops.py export <slug> [--out <output_file>]
  python scripts/tenant_ops.py offboard <slug> [--force] [--drop-permanently]
"""

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

import config
from db.connection import engine as default_engine, SessionLocal
from db.models import Client, UserClientAccess
from db.provisioning import CLIENT_TABLES_ORDERED
from db.tenant import validate_slug
from services.audit_service import AuditService

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("tenant_ops")


class TenantOpsManager:
    """Manages Phase 7 operational tooling: metrics, export, and offboarding."""

    def __init__(self, engine: Optional[Engine] = None):
        self.engine = engine or default_engine

    def get_tenant_metrics(self, slug: Optional[str] = None) -> List[Dict[str, Any]]:
        """Collects telemetry for one or all registered tenants."""
        results = []
        with self.engine.connect() as conn:
            # Query registered clients
            query = "SELECT id, name, slug, plan_tier, max_accounts, api_credits_total, api_credits_used, status, skeleton_version, is_active FROM public.clients"
            params = {}
            if slug:
                clean_slug = validate_slug(slug)
                query += " WHERE slug = :s"
                params["s"] = clean_slug
            query += " ORDER BY id ASC"

            clients = conn.execute(text(query), params).mappings().fetchall()

            for c in clients:
                c_slug = c["slug"]
                schema_name = f"client_{c_slug}"

                # Check schema exists
                exists = conn.execute(
                    text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :s"),
                    {"s": schema_name}
                ).scalar()

                if not exists:
                    results.append({
                        "slug": c_slug,
                        "name": c["name"],
                        "schema_name": schema_name,
                        "exists": False,
                        "status": c["status"],
                        "total_size_bytes": 0,
                        "total_size_pretty": "0 kB",
                        "total_rows": 0,
                        "accounts_count": 0,
                        "credits_used": c["api_credits_used"] or 0,
                        "credits_total": c["api_credits_total"] or 0,
                    })
                    continue

                # Query total schema size
                size_bytes = conn.execute(text("""
                    SELECT COALESCE(SUM(pg_total_relation_size(quote_ident(table_schema) || '.' || quote_ident(table_name))), 0)
                    FROM information_schema.tables
                    WHERE table_schema = :s AND table_type = 'BASE TABLE'
                """), {"s": schema_name}).scalar() or 0

                size_pretty = conn.execute(text("SELECT pg_size_pretty(CAST(:sz AS bigint))"), {"sz": int(size_bytes)}).scalar() or "0 kB"

                # Query total rows across canonical tables
                total_rows = 0
                accounts_count = 0
                for t in CLIENT_TABLES_ORDERED:
                    t_exists = conn.execute(text("""
                        SELECT 1 FROM information_schema.tables WHERE table_schema = :s AND table_name = :t
                    """), {"s": schema_name, "t": t}).scalar()
                    if t_exists:
                        cnt = conn.execute(text(f'SELECT count(*) FROM "{schema_name}"."{t}"')).scalar() or 0
                        total_rows += cnt
                        if t == "accounts":
                            accounts_count = cnt

                # Query user count
                users_cnt = conn.execute(text("""
                    SELECT count(*) FROM public.user_client_access WHERE client_id = :cid
                """), {"cid": c["id"]}).scalar() or 0

                results.append({
                    "id": c["id"],
                    "slug": c_slug,
                    "name": c["name"],
                    "schema_name": schema_name,
                    "exists": True,
                    "status": c["status"] or "active",
                    "plan_tier": c["plan_tier"] or "enterprise",
                    "skeleton_version": c["skeleton_version"] or "T0001",
                    "total_size_bytes": size_bytes,
                    "total_size_pretty": size_pretty,
                    "total_rows": total_rows,
                    "accounts_count": accounts_count,
                    "max_accounts": c["max_accounts"] or 30,
                    "users_count": users_cnt,
                    "credits_used": c["api_credits_used"] or 0,
                    "credits_total": c["api_credits_total"] or 0,
                })

        return results

    def export_tenant_data(self, slug: str) -> Dict[str, Any]:
        """Exports all schema tables and platform metadata for a tenant as structured JSON."""
        clean_slug = validate_slug(slug)
        schema_name = f"client_{clean_slug}"

        with self.engine.connect() as conn:
            client_row = conn.execute(
                text("SELECT * FROM public.clients WHERE slug = :s"),
                {"s": clean_slug}
            ).mappings().first()

            if not client_row:
                raise ValueError(f"Client workspace '{clean_slug}' not found.")

            client_meta = dict(client_row)
            for k, v in client_meta.items():
                if hasattr(v, "isoformat"):
                    client_meta[k] = v.isoformat()

            # Query user access grants
            user_grants = conn.execute(
                text("SELECT user_id, role, granted_at FROM public.user_client_access WHERE client_id = :cid"),
                {"cid": client_meta["id"]}
            ).mappings().fetchall()
            grants_list = []
            for g in user_grants:
                item = dict(g)
                if hasattr(item.get("granted_at"), "isoformat"):
                    item["granted_at"] = item["granted_at"].isoformat()
                grants_list.append(item)

            # Query credit ledger
            credit_rows = conn.execute(
                text("SELECT id, run_id, delta, reason, created_at FROM public.credit_ledger WHERE client_id = :cid ORDER BY created_at ASC"),
                {"cid": client_meta["id"]}
            ).mappings().fetchall()
            credits_list = []
            for r in credit_rows:
                citem = dict(r)
                if hasattr(citem.get("created_at"), "isoformat"):
                    citem["created_at"] = citem["created_at"].isoformat()
                credits_list.append(citem)

            # Export data from all tenant tables
            tables_data = {}
            for t in CLIENT_TABLES_ORDERED:
                t_exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.tables WHERE table_schema = :s AND table_name = :t
                """), {"s": schema_name, "t": t}).scalar()

                if not t_exists:
                    continue

                rows = conn.execute(text(f'SELECT * FROM "{schema_name}"."{t}"')).mappings().fetchall()
                serialized_rows = []
                for row in rows:
                    rdict = dict(row)
                    for k, v in rdict.items():
                        if hasattr(v, "isoformat"):
                            rdict[k] = v.isoformat()
                    serialized_rows.append(rdict)

                tables_data[t] = {
                    "count": len(serialized_rows),
                    "rows": serialized_rows,
                }

        export_manifest = {
            "version": "1.0",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "client": client_meta,
            "user_grants": grants_list,
            "credit_ledger": credits_list,
            "tables": tables_data,
        }

        return export_manifest

    def offboard_tenant(self, slug: str, force: bool = False, drop_permanently: bool = False) -> Dict[str, Any]:
        """Safely offboards a tenant workspace, archives/drops its schema, and records audit trail."""
        clean_slug = validate_slug(slug)
        if clean_slug == "stradit_india":
            raise ValueError("The primary platform tenant ('stradit_india') cannot be offboarded.")

        schema_name = f"client_{clean_slug}"

        with self.engine.connect() as conn:
            client_row = conn.execute(
                text("SELECT id, status, is_active FROM public.clients WHERE slug = :s"),
                {"s": clean_slug}
            ).mappings().first()

        if not client_row:
            raise ValueError(f"Client workspace '{clean_slug}' not found.")

        if client_row["status"] == "active" and not force:
            raise ValueError(
                f"Client '{clean_slug}' is currently ACTIVE. Suspend the client first or pass --force to offboard."
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        archived_schema_name = None

        with self.engine.begin() as conn:
            # 1. Archive or drop the PostgreSQL schema
            if drop_permanently:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
            else:
                archived_schema_name = f"purged_{clean_slug}_{timestamp}"
                conn.execute(text(f'ALTER SCHEMA "{schema_name}" RENAME TO "{archived_schema_name}"'))

            # 2. Update client record status to deleted
            conn.execute(text("""
                UPDATE public.clients 
                SET status = 'deleted', is_active = false, deleted_at = NOW()
                WHERE id = :cid
            """), {"cid": client_row["id"]})

            # 3. Revoke all user access permissions
            conn.execute(text("DELETE FROM public.user_client_access WHERE client_id = :cid"), {"cid": client_row["id"]})

        AuditService.log_event(
            action="client.offboarded",
            actor_user_id=None,
            client_slug=clean_slug,
            details={
                "slug": clean_slug,
                "schema_action": "dropped" if drop_permanently else f"archived_to_{archived_schema_name}",
                "timestamp": timestamp,
            },
            engine=self.engine,
        )

        return {
            "ok": True,
            "slug": clean_slug,
            "schema_name": schema_name,
            "status": "deleted",
            "archived_schema": archived_schema_name,
            "dropped_permanently": drop_permanently,
        }


def print_metrics_table(metrics: List[Dict[str, Any]]):
    print("\n" + "=" * 90)
    print(f"{'SLUG':<20} {'PLAN':<10} {'STATUS':<10} {'ACCOUNTS':<12} {'ROWS':<10} {'DISK SIZE':<12} {'CREDITS':<15}")
    print("-" * 90)
    for m in metrics:
        accts = f"{m.get('accounts_count', 0)}/{m.get('max_accounts', 30)}"
        credits_str = f"{m.get('credits_used', 0):,}/{m.get('credits_total', 0):,}"
        print(f"{m['slug']:<20} {m.get('plan_tier', 'enterprise'):<10} {m.get('status', 'active'):<10} {accts:<12} {m.get('total_rows', 0):<10} {m.get('total_size_pretty', '0 kB'):<12} {credits_str:<15}")
    print("=" * 90 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Phase 7 Multi-Tenant Operations & Lifecycle Management")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: metrics
    p_metrics = subparsers.add_parser("metrics", help="Show disk, row count, and quota telemetry per tenant")
    p_metrics.add_argument("--slug", help="Filter by specific tenant slug")
    p_metrics.add_argument("--json", action="store_true", help="Output as JSON")

    # Subcommand: export
    p_export = subparsers.add_parser("export", help="Export full tenant database tables and metadata")
    p_export.add_argument("slug", help="Client tenant slug to export")
    p_export.add_argument("--out", help="Path to write JSON export file")

    # Subcommand: offboard
    p_offboard = subparsers.add_parser("offboard", help="Offboard and archive/purge tenant schema")
    p_offboard.add_argument("slug", help="Client tenant slug to offboard")
    p_offboard.add_argument("--force", action="store_true", help="Force offboarding even if tenant is active")
    p_offboard.add_argument("--drop-permanently", action="store_true", help="Permanently drop schema instead of archiving")

    args = parser.parse_args()
    mgr = TenantOpsManager()

    if args.command == "metrics":
        data = mgr.get_tenant_metrics(slug=args.slug)
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print_metrics_table(data)

    elif args.command == "export":
        data = mgr.export_tenant_data(args.slug)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"✔ Tenant '{args.slug}' exported successfully to {out_path} ({len(data['tables'])} tables).")
        else:
            print(json.dumps(data, indent=2))

    elif args.command == "offboard":
        res = mgr.offboard_tenant(args.slug, force=args.force, drop_permanently=args.drop_permanently)
        print(f"✔ Tenant '{args.slug}' offboarded successfully: {res}")


if __name__ == "__main__":
    main()
