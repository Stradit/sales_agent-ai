"""Versioned schema migrations — the one way the schema changes (docs/DB_DEPLOYMENT_PLAN.md).

    python -m db.migrate status            # where is this database vs the code?
    python -m db.migrate upgrade           # apply pending migrations (deploy pipeline runs this)
    python -m db.migrate stamp-baseline    # put an EXISTING database under migration control (once)
    python -m db.migrate new "add x to y"  # create the next migration file

The database is the one in DATABASE_URL (a real environment variable wins over .env).

Two modes, decided by the database itself:
  * managed  — it has an alembic_version table. The app never changes the schema at startup; it only
               checks the version, and /api/health reports 503 if the database is behind the code.
  * legacy   — not stamped yet. The old startup schema code still runs, exactly as before, so nothing
               changes for a database until someone deliberately runs stamp-baseline on it.
"""
import argparse
import sys
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, inspect, pool, text

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "migrations" / "sql" / "0001_baseline.columns.txt"
BASELINE = "0001"


def _config():
    from alembic.config import Config
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def _engine():
    import config
    return create_engine(config.DATABASE_URL, poolclass=pool.NullPool)


def head_revision() -> str:
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(_config()).get_current_head()


def db_revision(conn):
    if not inspect(conn).has_table("alembic_version"):
        return None
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def state() -> dict:
    """{'mode': managed|legacy|empty, 'current', 'head', 'behind'}"""
    eng = _engine()
    try:
        with eng.connect() as c:
            cur = db_revision(c)
            tables = [t for t in inspect(c).get_table_names() if t != "alembic_version"]
    finally:
        eng.dispose()
    head = head_revision()
    mode = "managed" if cur else ("legacy" if tables else "empty")
    # An unstamped database is behind too once migrations beyond the baseline exist
    behind = (mode == "managed" and cur != head) or (mode == "legacy" and head != BASELINE)
    return {"mode": mode, "current": cur, "head": head, "behind": behind}


@lru_cache(maxsize=1)
def schema_managed() -> bool:
    """True once the database is stamped. Startup schema code checks this and stands down.
    Cached for the process: stamping happens offline, and the next deploy restarts the app."""
    try:
        eng = _engine()
        try:
            with eng.connect() as c:
                return inspect(c).has_table("alembic_version")
        finally:
            eng.dispose()
    except Exception as e:  # can't reach the DB → behave as before; the health check reports the DB anyway
        print(f"[DB] migration state unknown ({type(e).__name__}); using startup schema code")
        return False


def _columns(conn) -> set:
    rows = conn.execute(text("SELECT table_name||'.'||column_name||' '||data_type FROM information_schema.columns "
                             "WHERE table_schema = 'public'")).scalars()
    return set(rows)


def cmd_status(_args) -> int:
    s = state()
    print(f"mode: {s['mode']}  current: {s['current'] or '-'}  head: {s['head']}")
    if s["mode"] == "legacy":
        print("Not under migration control yet: run `python -m db.migrate stamp-baseline` once.")
    elif s["mode"] == "empty":
        print("Empty database: `python -m db.migrate upgrade` builds the whole schema.")
    elif s["behind"]:
        print("Behind the code: run `python -m db.migrate upgrade`.")
    else:
        print("Up to date.")
    return 0


def cmd_upgrade(args) -> int:
    from alembic import command
    s = state()
    if s["mode"] == "legacy":
        if s["head"] == BASELINE:
            # Nothing beyond the baseline yet: the app's startup code still owns this database.
            print("Database not under migration control yet (legacy mode) - nothing to apply.")
            return 0
        # Migrations beyond the baseline exist: switch the database over first - but only if it
        # really has the baseline schema (otherwise stop; nothing is changed).
        print("Database not under migration control yet: checking it against the baseline before stamping")
        if not _verify_baseline():
            return 1
        if args.sql:
            print("(--sql: would stamp 0001 here, then apply the migrations below)")
        else:
            command.stamp(_config(), BASELINE)
            print(f"Stamped at {BASELINE}.")
        s["current"] = BASELINE
    print(f"upgrading {s['current'] or '(empty)'} -> {s['head']}")
    command.upgrade(_config(), "head", sql=args.sql)
    if not args.sql:
        print(f"now at {state()['current']}")
    return 0


def cmd_stamp_baseline(args) -> int:
    """Record that this database already has the baseline schema — after checking that it does."""
    from alembic import command
    s = state()
    if s["mode"] == "managed":
        print(f"Already under migration control (at {s['current']}).")
        return 0
    if s["mode"] == "empty":
        print("Empty database - use `upgrade` to build it instead of stamping.")
        return 1
    if not _verify_baseline():
        return 1
    if not args.yes:
        print("Matches the baseline. Re-run with --yes to stamp it at 0001.")
        return 0
    command.stamp(_config(), BASELINE)
    print(f"Stamped at {BASELINE}. From now on this database changes only through migrations.")
    return 0


def _verify_baseline() -> bool:
    """True if every baseline column exists with the baseline type; prints what doesn't match."""
    expected = set(MANIFEST.read_text(encoding="utf-8").splitlines()) - {""}
    eng = _engine()
    try:
        with eng.connect() as c:
            actual = _columns(c)
    finally:
        eng.dispose()
    missing = sorted(expected - actual)
    if missing:
        # Type differences show up as a missing "table.column type" line
        print(f"This database does NOT match the baseline: {len(missing)} column(s) missing or of a different type:")
        for line in missing[:40]:
            print("   ", line)
        print("Start the app version from before migrations (2026-10-17) against it once - its startup code "
              "brings it up to the baseline - then try again. Nothing was changed.")
        return False
    extra = sorted(c for c in actual - expected if c.split(".", 1)[0] not in {"targets", "run_history"})
    if extra:
        print(f"Note: {len(extra)} column(s) exist here that the code does not create (left untouched):")
        for line in extra[:20]:
            print("   ", line)
    return True


def cmd_check(_args) -> int:
    """Exit 1 if `upgrade` has something to do (for the deploy script and CI)."""
    s = state()
    print(f"mode: {s['mode']}  current: {s['current'] or '-'}  head: {s['head']}")
    return 1 if s["behind"] or s["mode"] == "empty" else 0


def cmd_new(args) -> int:
    from alembic import command
    from alembic.script import ScriptDirectory
    heads = ScriptDirectory.from_config(_config()).get_heads()
    nxt = f"{max(int(h) for h in heads) + 1:04d}"
    command.revision(_config(), message=args.message, rev_id=nxt)
    return 0


def cmd_tenants_status(_args) -> int:
    """Inspects all tenant schemas in public.clients and reports drift against manifest."""
    from db.manifest import TenantManifest
    eng = _engine()
    head_ver = TenantManifest.get_skeleton_version()
    try:
        with eng.connect() as conn:
            clients = conn.execute(text("""
                SELECT slug, schema_name, status, skeleton_version, is_active
                FROM public.clients
                ORDER BY slug
            """)).fetchall()

        if not clients:
            print("No clients registered in public.clients.")
            return 0

        print(f"\n{'SLUG':<20} {'SCHEMA':<26} {'STATUS':<12} {'VERSION':<10} {'DRIFT':<8} {'TABLES'}")
        print("─" * 85)

        drift_found = False
        for slug, schema_name, status, skel_ver, is_active in clients:
            report = TenantManifest.verify_schema_against_manifest(eng, schema_name)
            drift_str = "YES ⚠️" if report["drift_detected"] else "NO ✔"
            if report["drift_detected"]:
                drift_found = True
            tables_str = f"{report['tables_found']}/{report['tables_expected']}"
            ver_str = skel_ver or "-"
            if ver_str != head_ver:
                ver_str = f"{ver_str} < {head_ver}"
            print(f"{slug:<20} {schema_name:<26} {status:<12} {ver_str:<10} {drift_str:<8} {tables_str}")

            if report["missing_tables"]:
                print(f"   Missing tables ({len(report['missing_tables'])}): {', '.join(report['missing_tables'][:5])}")
            if report["pk_mismatches"]:
                print(f"   PK mismatches: {len(report['pk_mismatches'])}")
            if report["fk_mismatches"]:
                print(f"   FK mismatches: {len(report['fk_mismatches'])}")

        print("─" * 85)
        print(f"Total: {len(clients)} client(s) inspected. Target skeleton head: {head_ver}\n")
        return 1 if drift_found else 0
    finally:
        eng.dispose()


def cmd_tenants_upgrade(args) -> int:
    """Fans out schema migration/repair across tenant schemas safely."""
    from db.manifest import TenantManifest
    from db.provisioning import repair_client_schema
    eng = _engine()
    head_ver = TenantManifest.get_skeleton_version()
    only_slug = getattr(args, "only", None)
    dry_run = getattr(args, "dry_run", False)

    try:
        with eng.connect() as conn:
            query = "SELECT slug, schema_name, status, skeleton_version FROM public.clients"
            params = {}
            if only_slug:
                query += " WHERE slug = :s"
                params["s"] = only_slug.strip().lower()
            query += " ORDER BY slug"
            clients = conn.execute(text(query), params).fetchall()

        if not clients:
            print(f"No matching clients found{' for slug ' + only_slug if only_slug else ''}.")
            return 0

        print(f"\nRunning tenant migration fan-out (Head: {head_ver}, Dry-Run: {dry_run})")
        print("─" * 70)

        failures = 0
        for slug, schema_name, status, skel_ver in clients:
            print(f"Tenant '{slug}' ({schema_name}) [Current: {skel_ver or 'none'}]...")
            if dry_run:
                report = TenantManifest.verify_schema_against_manifest(eng, schema_name)
                if report["drift_detected"]:
                    print(f"  [DRY-RUN] Drift detected. Missing tables: {len(report['missing_tables'])}, FKs: {len(report['fk_mismatches'])}. Would repair.")
                else:
                    print(f"  [DRY-RUN] Schema compliant. Would update skeleton_version to {head_ver}.")
                continue

            try:
                repair_res = repair_client_schema(slug, eng, schema_name=schema_name)
                with eng.begin() as conn:
                    conn.execute(text("""
                        UPDATE public.clients
                        SET skeleton_version = :ver,
                            status = CASE WHEN status = 'failed' THEN 'active' ELSE status END,
                            last_error = NULL
                        WHERE slug = :s
                    """), {"ver": head_ver, "s": slug})
                repaired_summary = f"Repaired {len(repair_res.get('repaired_tables', []))} tables, {len(repair_res.get('repaired_foreign_keys', []))} FKs."
                print(f"  ✔ Upgraded to {head_ver}. {repaired_summary}")
            except Exception as e:
                print(f"  ❌ Migration failed: {e}")
                failures += 1
                try:
                    with eng.begin() as conn:
                        conn.execute(text("UPDATE public.clients SET last_error = :err WHERE slug = :s"),
                                     {"err": str(e)[:2000], "s": slug})
                except Exception:
                    pass

        print("─" * 70)
        print(f"Finished fan-out: {len(clients) - failures} succeeded, {failures} failed.\n")
        return 1 if failures > 0 else 0
    finally:
        eng.dispose()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m db.migrate", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("upgrade")
    p.add_argument("--sql", action="store_true", help="print the SQL instead of running it")
    p.set_defaults(fn=cmd_upgrade)
    p = sub.add_parser("stamp-baseline")
    p.add_argument("--yes", action="store_true", help="really stamp (default: verify only)")
    p.set_defaults(fn=cmd_stamp_baseline)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    p = sub.add_parser("new")
    p.add_argument("message")
    p.set_defaults(fn=cmd_new)

    # Platform alias
    p_plat = sub.add_parser("platform", help="Platform schema migrations")
    plat_sub = p_plat.add_subparsers(dest="platform_cmd", required=True)
    plat_sub.add_parser("status").set_defaults(fn=cmd_status)
    p_pl_up = plat_sub.add_parser("upgrade")
    p_pl_up.add_argument("--sql", action="store_true", help="print SQL instead of running")
    p_pl_up.set_defaults(fn=cmd_upgrade)

    # Multi-tenant migration fan-out & drift commands
    p_tenants = sub.add_parser("tenants", help="Multi-tenant schema migrations & drift")
    tenants_sub = p_tenants.add_subparsers(dest="tenant_cmd", required=True)
    tenants_sub.add_parser("status").set_defaults(fn=cmd_tenants_status)
    p_t_up = tenants_sub.add_parser("upgrade")
    p_t_up.add_argument("--only", help="Run only on specified client slug")
    p_t_up.add_argument("--dry-run", action="store_true", help="Inspect and simulate without applying changes")
    p_t_up.set_defaults(fn=cmd_tenants_upgrade)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
