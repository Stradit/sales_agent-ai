# DEPLOYMENT.md — review and step-by-step implementation plan

Reviewed against the code on `feature/crm-core-phase1` (2026-10-16). DEPLOYMENT.md's overall shape
(Docker image → ECR → one EC2 behind nginx → RDS Postgres, deployed by GitHub Actions) is sound and
fits this app. But as written it would **not** produce a working or safe deployment. Below: what is
wrong, then the plan we'll work through, phase by phase.

---

## Part 1 — Verification

### Blockers (the deploy fails or is unsafe)

| # | Finding | Evidence | Fix |
|---|---|---|---|
| B1 | **The image can't start.** `.dockerignore` excludes all of `apps/`, but this app's code lives there (`apps/sales_copilot`, `sales_crm`, `sales_deals`, `sales_search`). `api.py` imports them at startup. | `.dockerignore`: `apps/` (meant only for `apps/content_pipeline`) | Exclude `apps/content_pipeline/` only |
| B2 | **No Celery worker exists.** The plan's `worker` container runs `celery -A celery_app` — `celery_app.py` doesn't exist (and is in `.dockerignore`). All background work runs as threads inside the web process. Redis is therefore unused. | `docker-compose.yml` comment, `services/job_registry.py` | Drop `worker` and `redis` containers (and `celery`/`redis` from requirements later) |
| B3 | **`--workers 2` breaks the app.** (a) The embedded Chroma vector store must be owned by one process (`apps/sales_copilot/settings.py`). (b) Research-job status (`/api/pipeline/job/{id}`) lives in one process's memory, so a job started on one worker isn't found when polled on the other. | `settings.py` L25-27, `services/job_registry.py` | Run **1 uvicorn worker** (fine for this team size), or move Chroma to its own container *and* jobs to the DB later |
| B4 | **`AUTH_ENFORCED` is missing from the production env.** It defaults off, which makes every request a super admin — roles, account scope and partner isolation all switch off. | `auth.py`, Admin page warning banner | `AUTH_ENFORCED=true` |
| B5 | **Saved API keys become unreadable.** Keys saved on the Admin page are encrypted with `CONFIG_ENCRYPTION_KEY`, falling back to `JWT_SECRET_KEY`. The plan generates a new JWT secret, so keys copied from the current DB can't be decrypted. | `services/config_service.py` L36 | Set `CONFIG_ENCRYPTION_KEY` to the value the current DB was encrypted with; never change it afterwards |
| B6 | **No HTTPS.** Port 443 is opened but nginx only listens on 80; no certificate step. Login cookies are `Secure` only when `COOKIE_SECURE=true`, which the plan never sets. | `deploy/nginx` section, `api.py` L148 | certbot step + `COOKIE_SECURE=true` |
| B8 | **A known admin login in the code.** On startup, an empty database gets a super admin whose email and password were written in `db/create_tables.py` — a public login on a fresh production DB. | `db/create_tables.py` `seed_default_admin_if_missing` | ✅ Fixed: the first admin now comes only from `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` (12+ chars); unset = skipped |
| B7 | **No data migration.** The current DB (`DATABASE_URL` in `.env` → local PostgreSQL 18.4, 268 MB, 59 tables) holds the accounts, personas, CRM data and RAG ledger. The plan creates an empty RDS. | `config.py`, `.env` | `pg_dump` → `pg_restore` step, then Chroma rebuild |

### Wrong or misleading (would cause confusion or silent problems)

| # | Finding | Fix |
|---|---|---|
| W1 | `deploy/`, `deploy.yml`, `setup_server.sh`, `remote_deploy.sh`, nginx conf **don't exist yet** — the plan describes them as if they do. Only `.github/workflows/code-quality.yml` exists. | We write them (Phases 1–4) |
| W2 | Step 9 / Troubleshooting: `cli rebuild-index` **doesn't exist** (CLI has init, sync, maintain, stats, search, ask, models, eval). And `docker exec … cli sync` against the embedded store **while the server runs** is unsafe — the CLI docstring says to use the Admin "Sync now" instead. | Rewrite the ops section |
| W3 | Env template: `COPILOT_LLM_MODELS`, `S3_MEDIA_BUCKET`, `AWS_REGION`, `SEC_USER_AGENT` — **S3/region/SEC vars aren't read by the code**; S3 (Step 4) isn't used today. | Drop S3 for now; keep the IAM role for later |
| W4 | Env template **misses** variables the code reads: `AUTH_ENFORCED`, `COOKIE_SECURE`, `CONFIG_ENCRYPTION_KEY`, `SITE_URL`, `CORS_ALLOWED_ORIGINS`, `SMTP_HOST/PORT/USER/PASSWORD/FROM`, `MS_REDIRECT_URI`, `LLM_API_KEY`/`LLM_MODEL` (the HeyRoute gateway), `CALLPREP_LLM_MODEL`, `FORECAST_ENABLED`, `AUTH_MAX_FAILED_LOGINS`, `AUTH_LOCKOUT_MINUTES`. | New template (Phase 2) |
| W5 | Health check uses `/login` — only proves the HTML renders, not that the DB works. `/api/health` is referenced in `route_audit.py` but **no such route exists**. | Add `/api/health` that pings the DB |
| W6 | uvicorn behind nginx needs `--proxy-headers --forwarded-allow-ips` or the app sees every request as plain-http from 127.0.0.1 (wrong client IPs in the audit log and login lockouts). | Add to the command |
| W7 | SSH (22) open to `0.0.0.0/0`. GitHub runners have no fixed IPs, so this is the usual shortcut, but it's the weakest point. | Prefer **AWS SSM Run Command** from Actions (no port 22), or restrict 22 to your office IP + use SSM |
| W8 | The FastEmbed model (~130 MB) downloads on first use **inside the container** and again after every redeploy. | Bake it into the image at build time |
| W9 | `docker-compose` publishes `8000:8000` on all interfaces. The SG blocks it today, but one SG change exposes the app without TLS. | Bind `127.0.0.1:8000:8000` |
| W10 | Diagram/table mention Celery "15-day refresh cycles", OpenRouter-only LLM, Flask comparisons, local Windows `file:///` links. | Clean up the doc at the end (Phase 7) |
| W11 | Dev runs Python **3.14**; the image uses **3.12**. Probably fine, but untested. | Build and smoke-test the image locally first (Phase 1) |
| W12 | `apps/content_pipeline` is a separate app with its own Dockerfile — the plan doesn't say whether it's deployed. | Decide (out of scope unless you say so) |

### Correct as written ✅
RDS Postgres private + SG reference — but **version 18, not 16** (the source DB is PostgreSQL 18.4); `pg_trgm` is the only extension the app needs (`vector` is installed locally but unused and left out of the dump); t3.medium sizing
(once there's 1 worker and no Celery/Redis); persisting `/app/output` (Chroma lives in `output/chroma`);
nginx `proxy_buffering off` for streaming — the stream route is `/api/copilot/chat/stream`, which the
`location /api/copilot/chat` prefix does match; background loops (sync, stale intros, forecast snapshot,
notifications, M365 capture) already use Postgres advisory locks, so restarts and overlaps are safe.

---

## Part 2 — Implementation plan

Each phase ends with something we can check. We do them in order; nothing touches AWS until Phase 3.

### Phase 0 — Decisions (you)
- [x] Domain: **doshin.stradit.com** → Elastic IP 54.166.60.149 (DNS verified 2026-10-16)
- [x] AWS region: **us-east-1 (N. Virginia)**
- [x] Data: **copy the current database** (the one in today's `.env`) — downtime window still to agree
- [x] Deploy access: **SSH key** (user `deploy`; restrict port 22 as far as practical)
- [ ] Is `apps/content_pipeline` deployed too? (default: no)

### Phase 1 — Make the image correct (local, no AWS)
- [x] Fix `.dockerignore` (B1); bake the FastEmbed model into the image (W8)
- [x] Dockerfile: non-root user, `--proxy-headers`, 1 worker, `HEALTHCHECK` (B3, W6)
- [x] First admin from env only, no password in code (B8)
- [x] Add `GET /api/health` (DB ping) — already on route_audit's public list (W5)
- [ ] Build the image and run it against a database with `AUTH_ENFORCED=true` (W11) — runs in GitHub Actions: `.github/workflows/docker-build.yml` (WSL isn't installed locally)

### Phase 2 — Production config files (in repo)
- [x] `deploy/docker-compose.prod.yml` — one `web` service, `127.0.0.1:8000`, `/opt/salesai/data/output` volume (B2, W9)
- [x] `deploy/.env.production.example` — complete variable list, no secrets (B4, B5, W4)
- [x] `deploy/nginx/salesai.conf` — HTTP→HTTPS redirect, TLS server, streaming location (B6)
- [x] `deploy/setup_server.sh` — Docker, nginx, certbot, AWS CLI, swap, data dirs, `deploy` user (idempotent)
- [x] `deploy/remote_deploy.sh` — ECR login, pull, start, health check, roll back to the previous image on failure
- [x] `.gitattributes` — LF line endings for scripts, Dockerfile and deploy files

### Phase 3 — AWS foundation (console, guided)
- [ ] Security groups (W7), RDS **PG18** + `pg_trgm`, EC2 t3.medium + Elastic IP, ECR repo
- [ ] GitHub OIDC role (ECR push + SSM send-command), EC2 instance role (SSM)
- [ ] DNS A record → Elastic IP

### Phase 4 — CI/CD
- [x] `.github/workflows/deploy.yml`: tests → build → push to ECR (tag = commit SHA) → deploy via SSH → health check
- [ ] GitHub `production` environment: 4 variables, 3 secrets, required reviewer (`deploy/AWS_SETUP.md`, Phase 4)

### Phase 5 — Data migration + first deploy
- [x] Scripts: `deploy/dump_local_db.sh` (this PC) and `deploy/restore_db.sh` (EC2, before the first deploy) with row-count checks
- [ ] Run them (B7). Production reads only `DATABASE_URL` → RDS: no Neon or local-database settings
- [ ] First deploy; certbot certificate; `COOKIE_SECURE=true`
- [ ] Rebuild Chroma: Admin → Copilot pipeline → **Full re-embed** (a normal sync trusts the copied ledger and would leave the new store empty)

### Phase 6 — Go-live checks
- [ ] Log in as each role (route_audit-style checks against prod), partner portal, SEO (`/robots.txt`, `/sitemap.xml`, social preview)
- [ ] SMTP test email from Admin, then switch notifications on
- [ ] Backups: RDS automated backups + snapshot before each deploy; EBS snapshot of `/opt/salesai/data`

### Phase 7 — Fix DEPLOYMENT.md
- [ ] Rewrite it to match what we actually built (W2, W3, W10)
