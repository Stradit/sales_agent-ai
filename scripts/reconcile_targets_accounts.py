"""Reconciliation check: apps/content_pipeline's `targets` table vs this
app's `accounts` table.

MERGE_PLAN.md (S7.3) splits ownership by responsibility instead of merging
the two lists: `accounts` is the registry of which companies exist at all;
`targets` is content-pipeline-specific scrape config (LinkedIn URL, SEC CIK,
etc.) for a subset of them. The two live in genuinely separate databases
today (see Phase 1) and nothing enforces the "every targets.key needs a
matching accounts.key" rule automatically, so this is a manual check to run
periodically rather than a live constraint.

Reports two kinds of drift:
  - orphaned target config: a targets.key with no matching accounts.key
    (scrape config for a company sales_agent-ai doesn't know about)
  - unmonitored accounts: an accounts.key with no targets entry (a company
    that's trackable in sales_agent-ai but has no content monitoring set up)

Usage: python scripts/reconcile_targets_accounts.py
Reads TARGETS_DATABASE_URL / ACCOUNTS_DATABASE_URL from the environment,
falling back to sensible local-dev defaults for each app (matching
apps/content_pipeline/config.py's DATABASE_URL_NEON default and this app's
own DATABASE_URL) if unset — override either when checking a different
pair of databases.
"""

import importlib.util
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

import psycopg2


def _load_config(module_name: str, config_path: Path):
    """Loads a config.py by file path (not via sys.path/import), since both
    apps have a module literally named `config` — importing by name would
    silently resolve to whichever one happened to be first on sys.path.
    Running each one's own dotenv-loading config.py is what actually
    fulfills the docstring's promise of "sensible local-dev defaults" —
    a bare os.getenv() here would miss anything only set in .env.
    """
    spec = importlib.util.spec_from_file_location(module_name, config_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fetch_target_keys(database_url: str) -> set:
    if not database_url:
        print("[!] No targets database URL configured — skipping targets side.")
        return set()
    try:
        conn = psycopg2.connect(database_url)
    except Exception as e:
        print(f"[!] Could not connect to targets database ({e}) — skipping targets side.")
        return set()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM targets")
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        print(f"[!] Could not read targets table ({e}) — skipping targets side.")
        return set()
    finally:
        conn.close()


def _fetch_account_keys(database_url: str) -> set:
    if not database_url:
        print("[!] No accounts database URL configured — skipping accounts side.")
        return set()
    try:
        conn = psycopg2.connect(database_url)
    except Exception as e:
        print(f"[!] Could not connect to accounts database ({e}) — skipping accounts side.")
        return set()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM accounts")
            return {row[0] for row in cur.fetchall()}
    except Exception as e:
        print(f"[!] Could not read accounts table ({e}) — skipping accounts side.")
        return set()
    finally:
        conn.close()


def main():
    targets_url = os.getenv("TARGETS_DATABASE_URL")
    if not targets_url:
        content_pipeline_config = _load_config(
            "content_pipeline_config", REPO_ROOT / "apps" / "content_pipeline" / "config.py"
        )
        targets_url = content_pipeline_config.DATABASE_URL  # respects that app's Neon/local toggle

    accounts_url = os.getenv("ACCOUNTS_DATABASE_URL")
    if not accounts_url:
        sales_ai_config = _load_config("sales_ai_config", REPO_ROOT / "config.py")
        # config names the SQLAlchemy driver ("postgresql+psycopg2://"); psycopg2.connect wants the plain form
        accounts_url = sales_ai_config.DATABASE_URL.replace("postgresql+psycopg2://", "postgresql://", 1)

    target_keys = _fetch_target_keys(targets_url)
    account_keys = _fetch_account_keys(accounts_url)

    if not target_keys and not account_keys:
        print("[!] Nothing to compare — check TARGETS_DATABASE_URL / ACCOUNTS_DATABASE_URL.")
        sys.exit(1)

    orphaned_targets = sorted(target_keys - account_keys)
    unmonitored_accounts = sorted(account_keys - target_keys)

    print(f"targets: {len(target_keys)}, accounts: {len(account_keys)}\n")

    if orphaned_targets:
        print(f"[!] {len(orphaned_targets)} target(s) with no matching account (orphaned scrape config):")
        for key in orphaned_targets:
            print(f"    - {key}")
    else:
        print("[OK] Every target has a matching account.")

    print()

    if unmonitored_accounts:
        print(f"[i] {len(unmonitored_accounts)} account(s) with no content monitoring set up:")
        for key in unmonitored_accounts:
            print(f"    - {key}")
    else:
        print("[OK] Every account has content monitoring configured.")


if __name__ == "__main__":
    main()
