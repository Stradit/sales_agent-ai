"""Backfill missing account_origin rows with origin_type='fetched' (C4).

Usage:
    python scripts/backfill_account_origin.py [--dry-run] [--client <slug>] [--batch-size 500]
"""
import argparse
import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from db.connection import SessionLocal
from db.models.client import Client
from db.tenant import schema_for, validate_slug


def backfill_client(session, client_slug: str, dry_run: bool = False, batch_size: int = 500) -> int:
    clean_slug = validate_slug(client_slug)
    tenant_schema = schema_for(clean_slug)

    # Check if table exists in tenant schema
    check_table = session.execute(
        text("""
            SELECT 1 FROM information_schema.tables 
            WHERE table_schema = :schema AND table_name = 'account_origin'
        """),
        {"schema": tenant_schema},
    ).scalar()

    if not check_table:
        print(f"[{clean_slug}] Table '{tenant_schema}.account_origin' does not exist. Skipping.")
        return 0

    # Find accounts without an account_origin row
    find_sql = text(f"""
        SELECT a.id, a.domain, a.key
        FROM "{tenant_schema}"."accounts" a
        LEFT JOIN "{tenant_schema}"."account_origin" o ON a.id = o.account_id
        WHERE o.account_id IS NULL
        ORDER BY a.id ASC
    """)
    rows = session.execute(find_sql).fetchall()
    total_missing = len(rows)

    if total_missing == 0:
        print(f"[{clean_slug}] 0 accounts missing provenance.")
        return 0

    print(f"[{clean_slug}] Found {total_missing} accounts missing provenance.")
    if dry_run:
        print(f"[{clean_slug}] [DRY-RUN] Would backfill {total_missing} accounts with origin_type='fetched'.")
        return total_missing

    inserted = 0
    for i in range(0, total_missing, batch_size):
        chunk = rows[i : i + batch_size]
        params = [
            {"aid": r[0], "ckey": r[1] or r[2]}
            for r in chunk
        ]
        insert_sql = text(f"""
            INSERT INTO "{tenant_schema}"."account_origin" 
                (account_id, origin_type, canonical_key, snapshot_id, snapshot_version, hydrated_at, local_edits)
            VALUES 
                (:aid, 'fetched', :ckey, NULL, NULL, NULL, false)
            ON CONFLICT (account_id) DO NOTHING
        """)
        session.execute(insert_sql, params)
        session.commit()
        inserted += len(chunk)
        print(f"[{clean_slug}] Inserted {inserted}/{total_missing}...")

    print(f"[{clean_slug}] Successfully backfilled {inserted} accounts.")
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Backfill missing account_origin records.")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing")
    parser.add_argument("--client", type=str, help="Target specific client slug")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size for inserts")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        if args.client:
            clients = session.query(Client).filter_by(slug=args.client).all()
            if not clients:
                print(f"Error: Client '{args.client}' not found.")
                sys.exit(1)
        else:
            clients = session.query(Client).filter(Client.is_active == True).all()

        total = 0
        for cl in clients:
            total += backfill_client(session, cl.slug, dry_run=args.dry_run, batch_size=args.batch_size)

        mode_str = "Simulated" if args.dry_run else "Completed"
        print(f"{mode_str} backfill for {len(clients)} clients: {total} total accounts processed.")
    finally:
        session.close()


if __name__ == "__main__":
    main()
