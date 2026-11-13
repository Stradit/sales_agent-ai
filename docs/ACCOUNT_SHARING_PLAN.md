# Cross-Tenant Account Reuse & Verified Intelligence Catalog Architecture
**Specification v2 (Merged) & Implementation Blueprint**
*Document Version: 2.0.0 • Date: November 2026 • Status: Approved Engineering Plan*
*Companion to Specification v2: supersedes v1.0.0 (`docs/CROSS_TENANT_ACCOUNT_ASSIGNMENT_PLAN.md`)*

---

## 1. Executive Summary & Problem Formulation

In our multi-tenant PostgreSQL architecture, each client operates within an isolated PostgreSQL schema (e.g., `client_stradit_india`, `client_acme_corp`). Each schema maintains a canonical 58-table structure (skeleton `T0001`) housing accounts, lines of business (LOBs), sub-LOBs, personas, and intelligence feeds.

### The Business & Technical Problem
When Client A (`stradit_india`) has enriched enterprise accounts such as **BlackRock** (consisting of 101 firmographic attributes, 298 LOBs, 385 Sub-LOBs, 704 Personas, and 793 Posts), and Client B (`acme_corp`) targets the same enterprise:
1. **Redundant Scraping & Heavy Credit Burn**: A fresh scrape requires 5–15 minutes and burns dozens of external API credits (ProxyCrawl, Diffbot, Serper, Apify, Firecrawl, FullEnrich, plus LLM inference).
2. **Anti-Bot Friction**: Redundantly crawling identical public corporate websites risks rate limits and CAPTCHA friction.
3. **Cross-Tenant Data Privacy Boundaries**: In a multi-tenant SaaS architecture, Client B must **never** see Client A’s private data (deals, pipeline stages, private sales notes, buyer room links, user assignments).
4. **Tenant Anonymity**: Client B must **never** learn that Client A holds or enriched the account (Zero-Knowledge Source Principle).

### The Solution: Platform-Mediated Verified Catalog (Specification v2)
Instead of direct tenant-to-tenant copying, we introduce an isolated `platform_catalog` schema that acts as a secure platform-level intelligence clearinghouse:
* **Publish (Client -> Catalog)**: Sanitized public facts are extracted via a strict deny-by-default share policy and stored as immutable, versioned snapshots.
* **Hydrate (Catalog -> Client)**: Target clients preview and import snapshots in a single atomic transaction with foreign key remapping, quota enforcement, and exact provenance tracking (`account_origin`).

---

## 2. Industry Research & Architectural Benchmarks

To establish a world-class architecture, we researched existing enterprise patterns for cross-tenant intelligence sharing:

| Architecture Pattern | Industry Example | How It Informs Our Design |
|---|---|---|
| **Global Master Data Management (MDM)** | *ZoomInfo, Apollo.io* | Centralized public company repository. When a tenant adds an account, they get a tenant-local copy. Tenant-specific CRM overlays remain strictly isolated in the tenant's workspace. |
| **Secure Data Sharing & Clean Rooms** | *Snowflake, Databricks Delta Sharing* | Zero cross-tenant schema access. Data is published to a platform catalog, and consumers materialize snapshots through platform mediation. Source tenant identity is never exposed. |
| **Strict Entity Resolution** | *Dun & Bradstreet, OpenCorporates, GLEIF* | Matches must use deterministic global identifiers (SEC CIK, LEI, Registrable Domain) rather than fuzzy text queries to eliminate false merges. |
| **Atomic Quota & Idempotent Ledger** | *Stripe Metering, AWS Marketplace* | Uses transactional advisory locks (`pg_advisory_xact_lock`) to prevent race conditions during concurrent account provisioning, backed by an immutable ledger. |

---

## 3. Non-Negotiable Architectural Principles

1. **No Tenant-to-Tenant Reads**: Tenant search paths and roles cannot read `platform_catalog` or other tenant schemas. All sharing flows through platform-mediated services.
2. **Deny-by-Default Share Policy**: Stored in `config/tenant_skeleton/share_policy.yaml`. Any table or column not explicitly marked `copy` is excluded.
3. **Source Client Anonymity**: The source client slug/id is never returned in client API responses, headers, error messages, or target schema records.
4. **Single-Transaction Atomicity**: Hydration executes within a single target-schema transaction. If any constraint fails, the entire import aborts with zero residual state.
5. **Server-Side Authorization**: UI hiding is cosmetic; all endpoints enforce permissions via unified effective roles.
6. **Auditable & Reversible**: Every hydration records an exact `created_ids` manifest in `platform_catalog.share_events`. Rollback cleanly reverses creations without touching tenant CRM data.
7. **Client Data Ownership**: Hydrated accounts become independent copies owned by the target client. There is no silent synchronization.

---

## 4. Empirical Spikes (Validated on Live Production DB)

Prior to finalizing this plan, seven empirical spikes were executed against the live PostgreSQL database:

* **Spike S1 (`organisational_hierarchy_tree`)**: Confirmed `accounts.organisational_hierarchy_tree` contains a 96 KB JSON tree with embedded `lob_id` and `persona_id` attributes. The remapping engine must walk this tree and translate all IDs.
* **Spike S2 (`targets` table)**: Confirmed schema is `(key, kind, display_name, ticker, config, updated_at)`. Target registration is required to resume continuous content monitoring.
* **Spike S3 (Enrichment artifacts)**: Legacy enrichment files in root and `output/` will be organized under tenant-scoped directories (`output/<slug>/`).
* **Spike S4 (RAG Isolation)**: Confirmed `rag_chunks`, `rag_documents`, and `rag_outbox` are tenant-scoped tables. Hydration enqueues background RAG embedding for the target schema.
* **Spike S5 (`opportunity_signals`)**: Confirmed signals contain generic SEC/news facts. Persona-specific generated fields (`value_proposition`, `personalized_icebreaker`) are marked `regenerate`.
* **Spike S6 (BlackRock Real-World Scale)**: BlackRock contains **298 LOBs, 385 Sub-LOBs, 704 Personas, 18 Signals, 793 Posts, 291 Jobs**. The single-transaction engine must handle ~2,500 rows in batches under statement timeouts.
* **Spike S7 (`sub_lobs` Foreign Key)**: Confirmed `sub_lobs` contains columns `['id', 'lob_id', 'name', ...]` and **NO `account_id` column**. Foreign key remapping must link strictly via `new_lob_id`.

---

## 5. Four Iterations of Architectural Refinement

### Iteration 1: Security & Identity Resolution (Anti-Oracle & Deny-by-Default)
* **Anti-Oracle Protection**: Looking up an account requires an exact identifier (SEC CIK, LEI, or normalized lowercase domain). Fuzzy queries return candidates requiring admin confirmation. If no snapshot exists or if the client is unauthorized, the API returns an identical 404 response to eliminate catalog probing.
* **Deny-by-Default Policy (`share_policy.yaml`)**:
  - `copy`: Canonical firmographics, financials, LOBs, Sub-LOBs, verified Personas, social Posts (last 90 days), CXO movements, Jobs, Targets.
  - `regenerate`: Account-specific LLM value propositions, icebreakers, digests, and action items.
  - `sensitive`: Personal emails, direct mobile phones, home addresses (excluded by default).
  - `never`: CRM deals, private notes, owner user IDs, verification flags, pipeline runs, user account grants, raw API payloads.

### Iteration 2: RBAC & Permission Unification
* **Root Cause of Dead Guards**: In `client_router.py`, `require_role("super_admin", "client_admin")` checked `User.role` (global), which never matches `client_admin` (stored in `UserClientAccess.role`).
* **Unified Effective Role Engine**:
  ```python
  def get_effective_role(session, user: User, client_slug: Optional[str] = None) -> Optional[str]:
      if not auth.AUTH_ENFORCED or user.role == "super_admin":
          return "super_admin"
      if user.role in auth.READ_ONLY_ROLES:
          return "viewer"  # Global read-only cap
      if client_slug:
          grant = session.query(UserClientAccess).join(Client).filter(...).first()
          return grant.role if grant else None
      return user.role
  ```
* **Granular Permission Mapping**:
  - `catalog:view_library`, `catalog:publish`, `catalog:assign`, `catalog:approve`, `catalog:rollback` -> `{"super_admin"}`
  - `account:reuse_check`, `account:reuse_request`, `account:reuse_execute`, `client:manage` -> `{"super_admin", "client_admin"}`
  - `account:write`, `auto_pipeline:run` -> `{"super_admin", "client_admin", "analyst"}`
* **Securing Telemetry & Auto-Pipeline**:
  - Guard `/api/system/*` with `auth.get_current_user`.
  - Guard the 5 auto-pipeline endpoints with `require_permission("auto_pipeline:run")`, `require_editor`, and `require_account_access`.
  - Eliminate the dangerous fallback `or "stradit_india"` in `require_client_role`.

### Iteration 3: Atomic Single-Transaction Hydration & ID Remapping
* **Concurrency Lock**: `pg_advisory_xact_lock(hashtext('catalog:hydrate:<target_slug>:<canonical_key>'))`.
* **Advisory Quota Check**: Inside the transaction, query `SELECT count(*) FROM target.accounts` under `pg_advisory_xact_lock(hashtext('accounts:<target_slug>'))`.
* **Topological Relational Insert**:
  1. `accounts`: Generate new ID. Suffix key if duplicate.
  2. `lobs`: Insert rows with `account_id = new_account_id`. Build `old_lob_id -> new_lob_id`.
  3. `sub_lobs`: Insert rows with `lob_id = new_lob_id` (**no `account_id`**).
  4. `personas`: Insert rows with `account_id = new_account_id`, `lob_id = new_lob_id`. Build `old_persona_id -> new_persona_id`. Remap hierarchy fields (`reports_to_persona_id`).
  5. `organisational_hierarchy_tree`: Deep-walk JSON and translate all embedded LOB and Persona IDs.
  6. `posts`, `cxo_movements`, `linkedin_jobs`: Rewrite `target_key` to target account key (`ON CONFLICT DO NOTHING`).
  7. `targets`: Register account key for continuous monitoring.
  8. `account_origin`: Record provenance (`origin_type = 'catalog'`, `snapshot_id`, `version`).

### Iteration 4: Credit Metering, Rollback Manifest, & Dual-Mode UI
* **Two-Phase Idempotent Metering**:
  - Settle flat fee (default 1 credit) via `CreditService` with idempotency key.
* **Exact Rollback Manifest**:
  - Records all created IDs in `platform_catalog.share_events.created_ids`.
  - Rollback checks if any private children exist (deals, action items). If private children exist, rollback is blocked to prevent data loss (admin can detach provenance instead). If clean, reverses insertions in reverse topological order.
* **Dual-Mode UI in `#addAccountModal`**:
  - Debounced (300ms) check on domain/CIK.
  - Displays Verified Intelligence Badge when a snapshot is available:
    - ⚡ **Verified Intelligence Available**: Updated 6 days ago (Fresh) • 4 LOBs • 24 Contacts • Est. 4,300 credits saved.
    - Mode 1: **[⚡ Use Verified Intelligence (Instant • 1 Credit)]**
    - Mode 2: **[⚡ Use Verified, Then Refresh]**
    - Mode 3: **[🌐 Fetch Fresh (Standard Crawl • 25 Credits)]**
  - Approval flow: If `client.reuse_requires_approval = True`, client admins submit a request for super admin review.

---

## 6. Database Schemas

### Platform Schema (`migrations/versions/0013_platform_catalog.py`)
```sql
CREATE SCHEMA platform_catalog;

CREATE TABLE platform_catalog.companies (
  id bigserial PRIMARY KEY,
  canonical_key text UNIQUE NOT NULL,   -- cik:… | lei:… | dom:…
  legal_name text,
  display_name text,
  country text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE platform_catalog.company_identifiers (
  company_id bigint REFERENCES platform_catalog.companies(id) ON DELETE CASCADE,
  id_type text NOT NULL,                -- cik | lei | domain
  id_value text NOT NULL,
  UNIQUE (id_type, id_value)
);

CREATE TABLE platform_catalog.snapshots (
  id bigserial PRIMARY KEY,
  company_id bigint NOT NULL REFERENCES platform_catalog.companies(id),
  version int NOT NULL,
  status text NOT NULL,                 -- building | ready | failed | quarantined | superseded
  source_client_id int REFERENCES public.clients(id) ON DELETE SET NULL,
  source_account_id int,
  policy_version text NOT NULL,
  skeleton_version text NOT NULL,
  quality_score numeric(5,2),
  quality_grade text,
  source_extracted_at timestamptz,
  taken_at timestamptz DEFAULT now(),
  counts jsonb NOT NULL,
  checksum text NOT NULL,
  UNIQUE (company_id, version)
);

CREATE TABLE platform_catalog.snapshot_blobs (
  snapshot_id bigint PRIMARY KEY REFERENCES platform_catalog.snapshots(id) ON DELETE CASCADE,
  payload jsonb NOT NULL
);

CREATE TABLE platform_catalog.share_events (
  id bigserial PRIMARY KEY,
  snapshot_id bigint NOT NULL REFERENCES platform_catalog.snapshots(id),
  target_client_id int NOT NULL REFERENCES public.clients(id),
  mode text NOT NULL,                   -- push | request | refresh_pull
  requested_by int,
  approved_by int,
  reason text,
  status text NOT NULL,                 -- previewed | pending_approval | queued | running | done | failed | rolled_back
  idempotency_key text UNIQUE NOT NULL,
  fee_credits int NOT NULL DEFAULT 0,
  created_ids jsonb,
  counts jsonb,
  error text,
  created_at timestamptz DEFAULT now(),
  completed_at timestamptz,
  rolled_back_at timestamptz
);

-- Client settings for catalog sharing
ALTER TABLE public.clients
  ADD COLUMN catalog_publish boolean NOT NULL DEFAULT false,
  ADD COLUMN catalog_consume boolean NOT NULL DEFAULT false,
  ADD COLUMN reuse_requires_approval boolean NOT NULL DEFAULT true;

-- Public template table for tenant cloning
CREATE TABLE public.account_origin (
  account_id int PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
  origin_type text NOT NULL,            -- fetched | catalog
  canonical_key text,
  snapshot_id bigint,
  snapshot_version int,
  hydrated_at timestamptz,
  hydrated_by int,
  local_edits boolean NOT NULL DEFAULT false
);
```

### Tenant Skeleton `T0002`
- Added `account_origin` to `config/tenant_skeleton/manifest.yaml` (total tables: 59).
- Added `account_origin` to `db/provisioning.py:CLIENT_TABLES_ORDERED`.
- Tenant schemas receive `account_origin` via `python -m db.migrate tenants-upgrade`.

---

## 7. Phased Implementation Roadmap

### Phase 0: Security & RBAC Foundations (Immediate Pre-requisites)
1. Unify RBAC in `auth.py`: implement `get_effective_role` and `require_permission`.
2. Replace dead `require_role("super_admin", "client_admin")` in `client_router.py`.
3. Remove `"stradit_india"` default fallback in `require_client_role`.
4. Secure `/api/system/*` routes and 5 auto-pipeline routes in `api.py`.
5. Unit test RBAC unification with `tests/test_rbac_unification.py`.

### Phase 1: Platform Catalog Core & Schema Migration
1. Create `config/tenant_skeleton/share_policy.yaml` and verify completeness with `tests/test_share_policy.py`.
2. Create migration `migrations/versions/0013_platform_catalog.py`.
3. Update `db/models/client.py` and create `db/models/platform_catalog.py`.
4. Advance skeleton to `T0002` in `config/tenant_skeleton/manifest.yaml` and update `db/provisioning.py`.
5. Run `python -m db.migrate upgrade` and `python -m db.migrate tenants-upgrade`.

### Phase 2: Service Layer (Publish, Preview, Hydrate, Rollback)
1. Implement `services/catalog_service.py` (`publish`, identity resolution, quality gate).
2. Implement `services/account_reuse_service.py` (`preview`, `hydrate`, `rollback`).
3. Wire transaction timeouts, advisory locks, and credit ledger deduction.
4. Comprehensive testing: `tests/test_account_reuse_service.py` and `tests/test_account_reuse_rbac.py`.

### Phase 3: REST API Routers
1. Create `routes/catalog_router.py` (Platform routes for super admin, Tenant routes for reuse check and execution).
2. Register `catalog_router` in `api.py`.
3. Test all API routes across the complete role matrix (`super_admin`, `client_admin`, `analyst`, `viewer`, unauthorized).

### Phase 4: Frontend Dual-Mode Integration
1. Update `#addAccountModal` in `frontend/pipline/`:
   - Debounced domain/CIK lookup.
   - Verified Intelligence Badge & Action cards.
   - Client Admin request submission state.
2. Add Catalog Provenance Badge to Account Detail view.
3. Verify with JavaScript syntax checking and end-to-end browser smoke test.

---

## 8. Verification & Quality Gates

Every phase must pass 100% of the following verification checks:
1. `pytest -s tests/test_copilot_*.py`: 166 passing unit tests.
2. `pytest -s tests/test_multitenant_*.py`: 15+ multi-tenant client tests.
3. `pytest -s tests/test_rbac_unification.py`: 100% coverage of role transitions.
4. `pytest -s tests/test_share_policy.py`: 0 unclassified columns across all 59 tables.
5. `pytest -s tests/test_account_reuse_rbac.py`: Full verification of cross-tenant cloning, zero source leaks, quota enforcement, and rollback.
6. `node -c frontend/pipline/js/app.js`: Clean JavaScript syntax verification.
