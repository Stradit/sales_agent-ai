#!/usr/bin/env python3
"""Reconciliation Guard — Audit and verify credit ledger integrity against client balances.

Checks that public.clients.api_credits_used matches the net sum of ledger transactions
for each client, and detects any drift.

Usage:
    python scripts/verify_credit_ledger.py [--client <slug>] [--fix] [--verbose]
"""

import argparse
import sys
from typing import Dict, List, Tuple
from sqlalchemy import text

# Ensure project root is in sys.path
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from db.connection import SessionLocal
from db.tenant import validate_slug


def verify_client_credits(session, client_id: int, slug: str, verbose: bool = False) -> Tuple[bool, int, int, int]:
    """Computes expected credits used from credit_ledger and compares with client row.
    
    Returns: (is_drift, recorded_used, expected_used, drift_amount)
    """
    # 1. Get recorded values from public.clients
    client_row = session.execute(
        text("SELECT id, slug, api_credits_total, api_credits_used FROM public.clients WHERE id = :cid"),
        {"cid": client_id},
    ).fetchone()

    if not client_row:
        raise ValueError(f"Client ID {client_id} not found")

    recorded_total = int(client_row[2] or 0)
    recorded_used = int(client_row[3] or 0)

    # 2. Compute expected used from non-topup transactions in public.credit_ledger
    # debits are negative (-15), refunds are positive (+15)
    # expected used = sum(-delta)
    ledger_row = session.execute(
        text("""
            SELECT COALESCE(SUM(-delta), 0) AS expected_used,
                   COUNT(*) AS tx_count
            FROM public.credit_ledger
            WHERE client_id = :cid
              AND (reason NOT ILIKE 'topup%' AND reason NOT ILIKE 'top_up%')
        """),
        {"cid": client_id},
    ).fetchone()

    expected_used = max(0, int(ledger_row[0] or 0))
    tx_count = int(ledger_row[1] or 0)

    drift = recorded_used - expected_used
    is_drift = (drift != 0)

    if verbose or is_drift:
        status_icon = "⚠️ DRIFT" if is_drift else "✓ OK"
        print(f"[{status_icon}] Client '{slug}' (ID {client_id}):")
        print(f"       Allocated: {recorded_total:,} cr")
        print(f"       Recorded Used: {recorded_used:,} cr")
        print(f"       Ledger Expected: {expected_used:,} cr ({tx_count} transactions)")
        if is_drift:
            print(f"       Discrepancy (drift): {drift:+,} cr")

    return is_drift, recorded_used, expected_used, drift


def main():
    parser = argparse.ArgumentParser(description="Reconcile and verify credit ledger integrity.")
    parser.add_argument("--client", "-c", type=str, help="Specific client slug to audit (optional)")
    parser.add_argument("--fix", action="store_true", help="Automatically repair detected drift by syncing client.api_credits_used to ledger")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print detailed breakdown for all audited clients")
    args = parser.parse_args()

    session = SessionLocal()
    total_audited = 0
    drift_count = 0
    fixed_count = 0

    try:
        if args.client:
            clean_slug = validate_slug(args.client)
            client_rows = session.execute(
                text("SELECT id, slug FROM public.clients WHERE slug = :s"),
                {"s": clean_slug},
            ).fetchall()
            if not client_rows:
                print(f"Error: Client '{clean_slug}' not found.")
                sys.exit(1)
        else:
            client_rows = session.execute(
                text("SELECT id, slug FROM public.clients ORDER BY slug ASC")
            ).fetchall()

        print(f"[*] Auditing credit ledger integrity for {len(client_rows)} client(s)...")

        for cid, slug in client_rows:
            total_audited += 1
            is_drift, recorded_used, expected_used, drift = verify_client_credits(
                session, cid, slug, verbose=args.verbose
            )

            if is_drift:
                drift_count += 1
                if args.fix:
                    print(f"    [FIX] Repairing '{slug}': Updating api_credits_used {recorded_used} -> {expected_used}...")
                    session.execute(
                        text("""
                            UPDATE public.clients
                            SET api_credits_used = :exp,
                                updated_at = NOW()
                            WHERE id = :cid
                        """),
                        {"cid": cid, "exp": expected_used},
                    )
                    session.commit()
                    fixed_count += 1

        print("\n" + "=" * 60)
        print(f"Reconciliation Audit Summary:")
        print(f"  Clients Audited : {total_audited}")
        print(f"  Drifts Detected : {drift_count}")
        if args.fix:
            print(f"  Drifts Repaired : {fixed_count}")
        print("=" * 60)

        if drift_count > 0 and not args.fix:
            print("[!] Ledger drift detected! Run with --fix to reconcile database.")
            sys.exit(1)
        else:
            print("[✓] All credit ledgers reconciled successfully.")
            sys.exit(0)

    except Exception as e:
        print(f"Error during reconciliation audit: {e}")
        session.rollback()
        sys.exit(2)
    finally:
        session.close()


if __name__ == "__main__":
    main()
