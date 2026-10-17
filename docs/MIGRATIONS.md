# Database migrations — how to

Why and the overall plan: [DB_DEPLOYMENT_PLAN.md](DB_DEPLOYMENT_PLAN.md). Tool: Alembic, wrapped by
`python -m db.migrate`. The database is the one in `DATABASE_URL` (a real environment variable wins
over `.env`).

## Commands
| Command | What it does |
|---|---|
| `python -m db.migrate status` | mode (`managed` / `legacy` / `empty`), current version, code version |
| `python -m db.migrate upgrade` | apply pending migrations (the deploy runs this; `--sql` prints the SQL instead) |
| `python -m db.migrate check` | exit 1 if the database is behind the code |
| `python -m db.migrate stamp-baseline [--yes]` | put an existing database under migration control (once) |
| `python -m db.migrate new "add x to deals"` | create the next numbered migration file |

## Changing the schema (developers)
1. `python -m db.migrate new "add renewal_date to deals"` → edit `migrations/versions/000N_….py`.
2. Write `upgrade()` **and** `downgrade()` by hand (`op.add_column`, `op.create_index`, `op.execute("…")`).
   Also update the ORM model in `db/models.py` if the table has one. **Never** edit `schema.sql`,
   `db/create_tables.py` or `migrations/sql/0001_baseline.sql` — CI fails if they drift from the baseline.
3. `python -m db.migrate upgrade` on your dev database, run the app and tests.
4. Open a PR. CI builds an empty database from all migrations, downgrades/re-applies the new ones and
   smoke-tests the image.

### The one rule: the previous app version must still work after your migration
The deploy migrates **before** the new app starts, and a failed deploy rolls back the app image but
never the schema. So every migration is *additive* (expand → migrate → contract):

| Change | Do it as |
|---|---|
| Add a column | nullable, or with a constant `server_default` — one migration |
| Make a column `NOT NULL` | 1) add nullable + backfill in batches · 2) next release: `SET NOT NULL` |
| Rename a column / table | 1) add the new one, write both, backfill · 2) read the new one · 3) later release: drop the old |
| Drop a column / table | 1) release code that no longer uses it · 2) next release: drop it |
| Add an index on a big table | its own migration: `with op.get_context().autocommit_block(): op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS …")` |
| Change a column's type | new column + backfill + switch over (a type change rewrites the table and locks it) |
| Data fixes / backfills | a migration with `UPDATE … WHERE id BETWEEN …` in batches, idempotent |

Migrations run with `lock_timeout = 10s`: if a table is busy the migration stops cleanly (nothing
half-applied, each migration is its own transaction) and the next deploy retries it.

## How a deploy applies migrations
`remote_deploy.sh` on the server, with the new image, while the old app keeps serving:
`db.migrate check` → if behind: **RDS snapshot** `pre-migrate-<time>-<sha>` → `db.migrate upgrade`
→ start the new app → `/api/health` (503 if the schema is behind) → image rollback if unhealthy.
A failed migration stops the deploy before the app is touched. Needs `RDS_INSTANCE_ID` in the
server `.env` and the snapshot policy on the instance role (deploy/AWS_SETUP.md, Step 4).

## Switching an existing database over (once per database)
Databases built before 2026-10-17 start in **legacy** mode. **This now happens automatically:** as soon
as there is a migration after the baseline (0002 = structured lost reasons), `upgrade` — and so the next
deploy — verifies the database against the baseline, stamps it at 0001 and applies the rest. If the
check fails it stops and changes nothing. To do it by hand instead:
```bash
python -m db.migrate stamp-baseline        # verifies every baseline column exists; changes nothing
python -m db.migrate stamp-baseline --yes  # records version 0001
```
Production (on the server, after a healthy deploy of this version):
```bash
docker exec salesai-web python -m db.migrate stamp-baseline
docker exec salesai-web python -m db.migrate stamp-baseline --yes
docker restart salesai-web                  # the app now runs in managed mode
```
If verification lists missing columns, start the current app version against that database once
(its startup code adds them), then verify again. Undo a stamp (back to legacy):
`DROP TABLE alembic_version;` and restart.

## When something goes wrong
- **Deploy stopped at "Migration failed"** — the old version is still serving and the failed migration
  left nothing behind. Read the error in the Actions log, fix the migration, deploy again.
- **A migration ran but was wrong** — write a new migration that corrects it (preferred), or
  restore the `pre-migrate-…` snapshot: RDS → Snapshots → Restore → new instance → point
  `DATABASE_URL` at it (data written since the snapshot is lost — that's why fixing forward is preferred).
- **Health says `"schema": "behind"`** — the app is newer than the database: run
  `docker exec salesai-web python -m db.migrate upgrade`, then `docker restart salesai-web`.
