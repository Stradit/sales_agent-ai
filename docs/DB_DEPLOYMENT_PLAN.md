# Database migration & deployment — current state and enterprise plan

Status (2026-10-17): **Phase 1 built, Phase 2–3 partly built** (see §7). How-to: [MIGRATIONS.md](MIGRATIONS.md). Scope: the main app (`api.py` + `apps/sales_*`) and its PostgreSQL
database on RDS. `apps/content_pipeline` is out of scope.

---

## 1. How it works today

### Schema changes — "self-healing on boot"
There is no migration tool and no record of which schema version a database is at. Instead, every
time the app process starts, five independent code paths reshape the database:

| Path | What it runs on **every** startup |
|---|---|
| `db/create_tables.py` → `ensure_schema_compatibility()` (from `api.py` startup) | `Base.metadata.create_all` for 40 ORM models + **46 `ALTER` statements** (incl. `ALTER COLUMN … TYPE`) + seed admin |
| `apps/sales_crm/schema.sql` (via `install()`) | 37 `CREATE` / 13 `ALTER` + triggers + **`DELETE FROM activities …` (line 255)** |
| `apps/sales_deals/schema.sql` | 10 `CREATE` / 2 `ALTER` |
| `apps/sales_copilot/schema.sql` | 32 `CREATE`, drops/re-creates triggers |
| `apps/sales_search/api.py` `_ensure_schema()` | inline `CREATE` + **`DELETE FROM search_log …`** retention |

### Data moves
Manual: `pg_dump` on a laptop → `scp` to EC2 → `deploy/restore_db.sh` (now with orphan checks and
row-count verification). Dumps containing all customer data sit on laptops and in `/tmp`.

### Deployment
GitHub Actions `deploy.yml` (manual) → build → ECR → SSH → `remote_deploy.sh` → container start →
health check → **image-only** rollback. Single EC2, single container, single RDS instance; one
environment (production). All config, incl. the DB password, lives in one GitHub secret (`ENV_FILE`).

## 2. Why that isn't enterprise-grade

| # | Problem | Consequence |
|---|---|---|
| P1 | No versioning: nobody can say which schema a database has | Drift between dev / prod goes unnoticed; restores and new environments are guesswork |
| P2 | DDL runs inside the web process at boot, while serving traffic | Long `ALTER`s lock tables during startup; two processes starting together race each other |
| P3 | Failures are swallowed (`print("schema check failed")`) and a 5 s `lock_timeout` skips work silently | The app runs on a half-migrated schema — seen twice on 2026-10-16 (`LockNotAvailable`) |
| P4 | Data-changing statements run on every boot (`DELETE FROM activities`, `search_log`) | Data is altered as a side effect of a restart, with no audit trail |
| P5 | Rollback reverts the image, not the schema | An old image can meet a newer schema it doesn't understand |
| P6 | No review or test of schema changes before production; no staging | The first time a change meets production data is in production |
| P7 | No snapshot before a deploy; restores are manual and untested | Recovery time is unknown |
| P8 | One DB user owns everything; credentials in a CI secret and shared in chat | A leaked app credential can drop tables; no rotation |
| P9 | Customer data dumps on laptops / `/tmp` | Data-protection exposure |

## 3. Target process

```
 developer ─▶ PR (code + migration file) ─▶ CI: lint migration · upgrade a fresh DB ·
              upgrade a copy of prod schema · drift check · tests
           ─▶ merge ─▶ deploy pipeline:
                 1. build image (once)            4. snapshot prod RDS
                 2. migrate STAGING → deploy → smoke tests
                 3. approval (GitHub environment)  5. migrate PROD (one-off job, migration role)
                                                   6. deploy app → health → auto-rollback app
```
Principles: **migrations are versioned files, reviewed like code, run once by the pipeline (never by
the app at boot), and always backward-compatible with the previous app version** (expand → migrate →
contract), so rolling the app back never needs a schema rollback.

## 4. Plan

### Phase 1 — Versioned migrations (foundation) · ~1 week
1. Adopt **Alembic** (SQLAlchemy is already used; one tool for ORM and raw SQL).
2. **Baseline**: `pg_dump --schema-only` of production → `0001_baseline` migration (raw SQL). Existing
   databases are *stamped* at `0001` (no change applied); a new database is built by running it.
3. Move every startup DDL path (§1 table) into migrations; delete them from `install()` / startup.
4. Move the two boot-time `DELETE`s into scheduled maintenance jobs (logged, not on boot).
5. App startup **only checks** the schema version: if the DB is behind, `/api/health` reports
   `schema: behind` (503) and the deploy stops — no silent half-state.
6. `alembic` settings: `lock_timeout = 5s`, `statement_timeout`, one transaction per migration, fail loudly.

### Phase 2 — Safety rails in CI · ~3 days
1. Migration lint (e.g. **squawk**) blocks risky DDL: table rewrites, non-concurrent index builds,
   `NOT NULL` without default on big tables, renames/drops without a contract phase.
2. CI job: Postgres 18 service → `alembic upgrade head` on an empty DB **and** on the latest
   schema-only prod snapshot → app smoke test (extends `docker-build.yml`).
3. **Drift check**: `alembic check` (models vs migrations) and a schema diff of prod vs `head`
   in the deploy pipeline (read-only).
4. PR template: "migration is backward-compatible with the running app?" checkbox; CODEOWNERS for
   `migrations/`.

### Phase 3 — Deployment pipeline with staging · ~1 week
1. **Staging** environment: small EC2 + RDS (or a second database on the same RDS to save cost),
   refreshed from a prod snapshot (with personal data masked) on demand.
2. `deploy.yml` becomes: build once → migrate+deploy staging → smoke tests → approval →
   **RDS manual snapshot** → migrate prod as a **separate one-off container** (`alembic upgrade head`)
   → deploy app → health → automatic app rollback.
3. Tag images and migrations with the same commit; release notes list the migrations applied.

### Phase 4 — Data operations & recovery · ~3 days
1. RDS automated backups **14–35 days + point-in-time recovery**; pre-deploy snapshot kept 30 days.
2. Written restore runbook + a **quarterly restore drill** into staging (measure RTO/RPO).
3. Bulk data loads (like `merged (2).dump`) go through staging first, then prod via the checked
   `restore_db.sh`; dumps travel via an encrypted, private S3 bucket with a 7-day lifecycle —
   never laptops / `/tmp`.

### Phase 5 — Security & operations · ~1 week
1. **Secrets in AWS Secrets Manager / SSM Parameter Store** (DB URL, API keys), read at container
   start via the instance role; `ENV_FILE` keeps only non-secret settings. Rotate the RDS password
   now (it has been exposed) and then on a schedule.
2. **Separate DB roles**: `migrator` (owns schema, used only by the migration job), `app`
   (read/write data, no DDL), `readonly` (reporting/support).
3. RDS: Multi-AZ (when uptime matters), deletion protection, encryption, Performance Insights,
   CloudWatch alarms (CPU, connections, free storage, slow queries), `log_min_duration_statement`.

### Phase 6 — Zero-downtime (later)
Two app containers behind an ALB for rolling deploys. Prerequisites in the app: Chroma in server mode
(not embedded) and research-job state in the database instead of process memory (DEPLOYMENT_REVIEW.md B3).

## 5. Decisions needed
1. Migration tool: **Alembic** (recommended) vs plain numbered SQL files.
2. Staging: separate RDS (≈ +$25–40/month, cleanest) vs second database on the existing RDS (≈ $0).
3. Secrets: **Secrets Manager** (rotation built-in, ≈ $0.40/secret/month) vs SSM Parameter Store (free tier).
4. Multi-AZ RDS now or later (doubles the RDS cost).

## 6. First steps (Phase 1 in practice)
1. Freeze schema changes outside migrations from today.
2. Generate the baseline from production's schema; verify it rebuilds an empty database to the same
   schema (schema diff = empty).
3. Stamp production and dev at `0001`; switch startup to "check only"; ship behind the pipeline.

## 7. Progress

| Item | State |
|---|---|
| Alembic + `python -m db.migrate` (status / upgrade / check / stamp-baseline / new) | done |
| Baseline `0001` generated from the startup code (57 tables, 744 columns), reproducible (`scripts/build_schema_baseline.py --check`) | done |
| Managed databases: app startup changes nothing (verified by schema diff); health = 503 when behind | done |
| Legacy databases (not stamped) behave exactly as before | done |
| Deploy: check → RDS snapshot → migrate (one-off container) → start → health → image rollback | done (`remote_deploy.sh`) |
| CI: build empty DB from migrations, re-run no-op, baseline drift check, downgrade/re-upgrade, image migrates + boots | done (`docker-build.yml`, called by `deploy.yml`) |
| Unit tests: linear numbered history, downgrades present, baseline clean | done (`tests/test_migrations.py`) |
| `.env` no longer overrides real environment variables (a migration aimed at staging can't hit `.env`'s DB) | done |
| Stamp dev and production databases | **to do** (MIGRATIONS.md → "Switching an existing database over") |
| `RDS_INSTANCE_ID` in server `.env` + snapshot policy on `salesai-ec2-role` | **to do** (AWS_SETUP.md Step 4) |
| Boot-time `DELETE`s (activities orphans, search_log retention) → 6-hourly CRM housekeeping job | done (managed mode; legacy keeps the old behaviour) |
| Migration lint (squawk), staging, Secrets Manager, DB roles, backups/PITR, monitoring | Phases 2–5 |
