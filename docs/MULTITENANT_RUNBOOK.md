# Multi-Tenant Dynamic Database Production Runbook & Operations Manual

This runbook provides platform engineers, site reliability engineers (SREs), and database administrators (DBAs) with standardized, end-to-end operational procedures for managing the dynamic multi-tenant database architecture in the Sales AI Agent pipeline.

---

## 1. Architecture Overview

### Isolation Model
The platform implements a **hybrid multi-tenant database architecture**:
- **Platform Layer (`public` schema)**: Holds global identity, platform user authentication, tenant registry, user-tenant workspace access grants, atomic credit balance ledgers, persistent Celery pipeline job definitions, and immutable security audit logs.
- **Tenant Layer (`client_<slug>` schema)**: Complete, physically isolated PostgreSQL schema container per tenant organization. Contains all 58 canonical business tables (Accounts, LOBs, Personas, Deals, Pipeline Runs, CRM entities, Copilot RAG vectors/chunks, Signals, Digests, Posts).
- **Reference Layer (`public` schema, read-only)**: Shared static taxonomies (e.g. `fx_rates`).

### Dynamic Routing & Fail-Closed Security
- **Context Engine (`db/tenant.py`)**: Uses Python `ContextVar[Optional[str]]` (`_current_tenant_schema`). In production (`AUTH_ENFORCED=True`), requests without an active tenant context immediately fail closed.
- **Transaction Search Path Binding**: SQLAlchemy `@event.listens_for(SessionLocal, "after_begin")` automatically binds `SET LOCAL search_path = "client_<slug>", public` to every transaction, preventing cross-tenant leakage across connection pool checkouts.
- **Background Task Propagation**: `tasks/base.py` (`TenantTask`) and `db/routing.py` (`spawn_tenant_thread`) automatically capture and propagate the tenant slug across Celery task workers and multi-threaded executions.

---

## 2. Tenant Provisioning Runbook

### Methods of Provisioning
1. **Interactive UI Wizard**:
   - Navigate to Pipeline Explorer $\to$ Click `Add New Client` from topbar or manage panel.
   - **Step 1: Organization**: Input organization name and slug (e.g. `acme_financial`).
   - **Step 2: Plan & Limits**: Choose plan tier (`enterprise`, `professional`, `starter`), max account quota, credit budget, automated refresh cron, and assign initial client admin.
   - **Step 3: Provision & Verify**: Live 6-step progress pipeline automates schema creation, table cloning, sequence allocation, FK adaptation, and manifest compliance verification.
2. **REST API**:
   ```http
   POST /api/clients
   Authorization: Bearer <super_admin_jwt>
   Content-Type: application/json

   {
     "name": "Acme Financial Holdings",
     "slug": "acme_financial",
     "legal_name": "Acme Financial Holdings Inc.",
     "plan_tier": "enterprise",
     "max_accounts": 50,
     "api_credits_total": 100000,
     "refresh_cron": "0 0 * * *"
   }
   ```
3. **CLI / Python Scripting**:
   ```bash
   python -c "from db.provisioning import create_client_schema; create_client_schema('acme_financial')"
   ```

### Provisioning Guarantees
- **Non-Destructive Failure Preservation**: If table cloning or FK adaptation fails, schema creation commits in its own block so the schema is preserved for debugging; it is **never** silently dropped.
- **Deterministic ID Sequence Allocation**: Every tenant table receives dedicated sequences owned by its own columns, eliminating cross-tenant sequence contention.

---

## 3. Schema Evolution & Migration Fan-Out

### Dual Migration Track
1. **Platform Migration Track (Alembic)**:
   - Manages all `public` tables (`0001_initial` through `0012_credit_ledger_pipeline_jobs_and_audit`).
   - Run platform migrations:
     ```bash
     alembic upgrade head
     ```
   - Check platform migration status:
     ```bash
     python -m db.migrate status
     ```
2. **Tenant Migration Track (Canonical Skeleton Manifest)**:
   - Single source of truth: `config/tenant_skeleton/manifest.yaml` (Head: `T0001`).
   - Defines all 58 canonical tables, primary keys, intra-tenant foreign keys, and platform foreign keys.

### Fan-Out CLI Commands
- **Inspect All Tenants for Drift**:
  ```bash
  python -m db.migrate tenants status
  ```
  *Output table displays each tenant's schema, status, current version, drift status (`NO ✔` or `DRIFT ⚠`), and table count.*
- **Dry-Run Upgrade Simulation**:
  ```bash
  python -m db.migrate tenants upgrade --dry-run
  ```
- **Execute Upgrade Fan-Out (All Tenants)**:
  ```bash
  python -m db.migrate tenants upgrade
  ```
- **Targeted Single-Tenant Upgrade**:
  ```bash
  python -m db.migrate tenants upgrade --only acme_corp
  ```

---

## 4. Stradit Cutover & Public Table Archiving

The production cutover script (`scripts/stradit_cutover.py`) provides automated verification, data parity checks, and transactional public table archiving.

### Step 1: Pre-Cutover Verification
Run the verification suite:
```bash
python scripts/stradit_cutover.py --verify
```
*Validates:*
- All 58 canonical tables present in `client_stradit_india`.
- Manifest compliance at head `T0001`.
- Row count and ID checksum parity against `public`.
- Zero orphan foreign key records.
- Sequence values synchronized with max table IDs.

### Step 2: Dry-Run Public Table Archive
```bash
python scripts/stradit_cutover.py --archive-public --dry-run
```
*Simulates moving 58 tenant tables from `public` to `legacy_archive_<timestamp>` without modifying schemas.*

### Step 3: Execute Public Table Archive
```bash
python scripts/stradit_cutover.py --archive-public
```
*Effect:*
- Creates schema `legacy_archive_<timestamp>`.
- Atomically moves all 58 tenant tables out of `public` into the archive schema.
- Leaves **only platform tables** in `public` (`users`, `clients`, `user_client_access`, `credit_ledger`, `pipeline_jobs`, `audit_log`, `alembic_version`).
- Emits audit log entry `platform.public_schema_archive`.

### Rollback Procedure
If rollback is required, restore tables back to `public`:
```bash
python scripts/stradit_cutover.py --restore-public legacy_archive_<timestamp>
```

---

## 5. Credit Governance & Execution Control

### Atomic Credit Engine (`services/credit_service.py`)
- **Balance Invariants**: Ensures `credits_used <= credits_total`.
- **Atomic Operations**: Uses PostgreSQL transaction advisory locks (`pg_advisory_xact_lock(hashtext('accounts:<slug>'))`) combined with atomic `UPDATE ... RETURNING` queries.
- **Double-Entry Ledger (`public.credit_ledger`)**: Every transaction (reservation, settlement, refund, top-up) writes an immutable record with delta, context run ID, and reason.

### Concurrency & Quota Limits
- **Max Accounts Quota**: Checked on every account creation (`AccountQuotaBlockedError` if account count $\ge$ quota).
- **Per-Tenant Celery Concurrency**: `tasks/base.py` limits simultaneous jobs per client using `max_concurrent_jobs` configured on the client record.

---

## 6. Operational Telemetry & Per-Tenant Tooling

### Telemetry CLI (`scripts/tenant_ops.py`)
- **Display Multi-Tenant Metrics Table**:
  ```bash
  python scripts/tenant_ops.py metrics
  ```
  *Outputs table with Slug, Plan, Status, Accounts usage, Total rows, Disk size (pretty), and Credit consumption.*
- **Single Tenant Metrics (JSON)**:
  ```bash
  python scripts/tenant_ops.py metrics --slug stradit_india --json
  ```

### Tenant Data Export
Exports all 58 tables, platform client metadata, user access grants, and credit ledger history to structured JSON:
```bash
python scripts/tenant_ops.py export <slug> --out exports/<slug>_backup.json
```

### Tenant Offboarding & Purge
- **Suspension (Safe Lockout)**:
  ```http
  POST /api/clients/{slug}/suspend
  ```
  *Blocks all dynamic DB queries, API calls, and pipeline runs for the tenant.*
- **Offboarding CLI**:
  ```bash
  python scripts/tenant_ops.py offboard <slug>
  ```
  *Renames `client_<slug>` to `purged_<slug>_<timestamp>`, soft-deletes client row (`status = 'deleted'`), revokes all user permissions, and records audit trail.*
- **Permanent Purge (with irreversible deletion)**:
  ```bash
  python scripts/tenant_ops.py offboard <slug> --force --drop-permanently
  ```

---

## 7. Incident Response & Troubleshooting Playbooks

### Playbook A: Schema Drift Alert (`DRIFT ⚠`)
**Symptoms**: UI displays `⚠ v0 < head T0001` or `python -m db.migrate tenants status` flags drift.
**Procedure**:
1. Run diagnostics:
   ```bash
   curl -H "Authorization: Bearer <token>" http://localhost:8000/api/clients/<slug>/health
   ```
2. Trigger self-healing repair via API, UI, or CLI:
   ```bash
   python -m db.migrate tenants upgrade --only <slug>
   ```
3. Verify table count restored to 58/58.

### Playbook B: Credit Exhaustion (`InsufficientCreditsError`)
**Symptoms**: Pipeline runs fail with credit exhaustion error.
**Procedure**:
1. Inspect tenant ledger:
   ```bash
   curl -H "Authorization: Bearer <token>" http://localhost:8000/api/clients/<slug>/credits/ledger
   ```
2. Top up credits via administrative API:
   ```http
   POST /api/clients/{slug}/credits/adjust
   {"amount": 50000, "action": "add", "reason": "Operational quota increase"}
   ```

### Playbook C: Primary Client Suspension Blocked
**Rule**: `stradit_india` is protected by system constraints and cannot be suspended or offboarded (`HTTP 400`). To perform maintenance on Stradit India, schedule a maintenance window and halt worker processes.

---

## 8. Summary of Verification Status
- **Test Database**: `sales_ai_test` (Hermetic isolated PostgreSQL database).
- **Core Multi-Tenant Test Suites**: 100% Passing (87/87 tests across all test modules).
- **Alembic Revision Head**: `0012` (single clean head, linear history).
- **Canonical Skeleton Version**: `T0001` (58 canonical tenant tables, 0 drift across all tenants).
