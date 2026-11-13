"""
Scheduler & Task CLI — Command-line interface for Celery background jobs.
Allows triggering 15-day refreshes on-demand for all or selected accounts.

Usage Examples:
    # 1. Refresh all accounts in database
    python tasks/scheduler_cli.py --refresh-all

    # 2. Refresh only specific selected accounts
    python tasks/scheduler_cli.py --refresh "bny,blackrock"

    # 3. List all registered accounts in DB
    python tasks/scheduler_cli.py --list

    # 4. Trigger full onboarding run in background
    python tasks/scheduler_cli.py --full "BlackRock" --url "https://www.blackrock.com"
"""

import sys
import argparse
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPELINE_ROOT))

from db.connection import get_session
from db.models import Account
from tasks.refresh_tasks import (
    task_refresh_account_hierarchy_and_social,
    task_scheduled_15day_refresh
)
from tasks.pipeline_tasks import task_full_company_pipeline


def list_accounts():
    """Lists all accounts currently stored in PostgreSQL."""
    session = get_session()
    try:
        accounts = session.query(Account).order_by(Account.id).all()
        print("=" * 65)
        print(f"REGISTERED ACCOUNTS IN DATABASE ({len(accounts)} total)")
        print("=" * 65)
        for a in accounts:
            print(f"  [{a.id:>2}] Key: {a.key:18s} | Name: {str(a.legal_name or a.display_name or 'N/A')[:30]:30s} | Last Extracted: {a.extracted_at}")
        print("=" * 65)
    finally:
        session.close()


def refresh_accounts(account_keys: list):
    """Enqueues refresh tasks for specific account keys."""
    print(f"[*] Dispatching 15-Day Refresh for {len(account_keys)} account(s): {account_keys}")
    for key in account_keys:
        k = key.strip()
        job = task_refresh_account_hierarchy_and_social.delay(account_key=k)
        print(f"  [+] Dispatched refresh job for '{k}' -> Celery Task ID: {job.id}")


def refresh_all():
    """Enqueues refresh tasks for all registered accounts in DB."""
    session = get_session()
    try:
        accounts = session.query(Account.key).all()
        target_keys = [a.key for a in accounts if a.key]
        if not target_keys:
            print("[!] No accounts found in database to refresh.")
            return

        print(f"[*] Dispatching 15-Day Refresh for ALL {len(target_keys)} accounts in DB...")
        job = task_scheduled_15day_refresh.delay(account_keys=None)
        print(f"  [+] Dispatched master refresh batch -> Celery Task ID: {job.id}")
    finally:
        session.close()


def run_full(name: str, url: str = None):
    """Enqueues a full onboarding run."""
    print(f"[*] Dispatching full onboarding pipeline for: '{name}' (URL: {url})...")
    job = task_full_company_pipeline.delay(company_name=name, target_url=url)
    print(f"  [+] Dispatched full pipeline job -> Celery Task ID: {job.id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sales AI Pipeline — Celery Task & Refresh Scheduler")
    parser.add_argument("--list", action="store_true", help="List all accounts in DB available for refresh")
    parser.add_argument("--refresh-all", action="store_true", help="Trigger 15-day refresh for ALL accounts in DB")
    parser.add_argument("--refresh", type=str, help="Comma-separated list of account keys to refresh (e.g. 'bny,blackrock')")
    parser.add_argument("--full", type=str, help="Run full pipeline for a company name")
    parser.add_argument("--url", type=str, help="Target website URL (used with --full)")

    args = parser.parse_args()

    if args.list:
        list_accounts()
    elif args.refresh_all:
        refresh_all()
    elif args.refresh:
        keys = [k.strip() for k in args.refresh.split(",") if k.strip()]
        refresh_accounts(keys)
    elif args.full:
        run_full(args.full, args.url)
    else:
        parser.print_help()
