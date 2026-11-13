# Verified Intelligence Account Reuse — User & Administrator Guide

> **Version:** 2.0 (Phase C Enterprise Release)  
> **Audience:** Tenant Users, Client Administrators, and Platform Super-Administrators  
> **Core Guarantee:** Strict Multi-Tenant Isolation with Zero Source Client Oracle Leaks

---

## 1. Overview & Architecture

The **Verified Intelligence Catalog** enables organizations to reuse high-fidelity, pre-validated enterprise accounts, Lines of Business (LOBs), and executive personas without incurring expensive multi-vendor API costs or multi-minute pipeline crawl latencies.

### Key Value Propositions
- **Instant Hydration:** Import a complete enterprise hierarchy (Company profile, LOBs, Sub-LOBs, Executive personas, OSINT feeds) in `< 250 milliseconds` instead of running 2-5 minute autonomous web crawls.
- **Credit Efficiency:** Verified catalog reuse charges a nominal fee (e.g. 15 credits) compared to 100+ credits for full vendor API harvesting, saving up to **85%+ credits per account**.
- **Local-Edit Protection:** Tenant edits are never silently overwritten. Local modifications are tracked with millisecond timestamps and safeguarded during refresh operations.
- **Anti-Oracle Guarantee:** Responses and exports never disclose the originating client, foreign tenant presence, or external database identifiers.

---

## 2. Tenant End-User & Client Admin Workflows

### 2.1 Discovering & Previewing Verified Intelligence (C1)
When creating a new account in **Add Account**:
1. Type a company name or website domain (e.g. `acme.com`).
2. The real-time gatekeeper checks the platform catalog in the background without exposing catalog internals.
3. If verified intelligence is found, an **Intelligence Match Card** appears showing:
   - **Quality Score & Grade:** Overall completeness score (e.g., `94/100 · Grade A`).
   - **Coverage Breakdown:** Verified counts of Lines of Business, Sub-LOBs, and Executive Personas.
   - **Freshness Indicator:** Days since extraction (`Fresh` ≤ 15 days, `Stale` 16–45 days, `Old` > 45 days).
   - **Pricing Transparency:** Transparent fee (e.g. `15 credits`) vs. fresh fetch estimate (e.g. `100 credits`), and your current available balance.
4. Choose between:
   - **Import Verified Intelligence:** Instantly hydrates the account.
   - **Run Fresh Pipeline:** Bypasses catalog and triggers a brand-new multi-vendor crawl.

---

### 2.2 Access Requests & Approval Lifecycle (C3)
If your organization requires administrative approval to consume catalog intelligence (`reuse_requires_approval=True`), the import button transitions into **Request Access**:
1. Enter a business justification (e.g., *"Targeting executive buying committee for Q4 strategic sales"*).
2. Click **Submit Request**.
3. A timeline displays your request status:
   - **Submitted:** Request logged with immutable audit identifier.
   - **In Review:** Under review by the platform administrator team (standard SLA: 24 hours).
   - **Fulfilled:** Once approved, intelligence is atomically hydrated into your workspace.
4. **Self-Cancellation:** If you no longer need the intelligence, you may cancel your pending request at any time prior to approval with zero credit penalty.

---

### 2.3 Notifications & Alerts (C0 / C3)
- **Notification Bell:** Located in the top navigation bar. Displays an unread badge counter for pending decisions.
- **In-App Dropdown:** Clicking the bell displays recent notifications (e.g., *"Your request for Solar Systems Energy was approved"*). Clicking an item marks it as read and opens the hydrated account.
- **Email Notifications:** If SMTP is configured, email alerts are dispatched upon request submission, approval, or rejection.

---

### 2.4 Provenance Badges & Refresh Detection (C4)
Every account in your workspace displays an explicit provenance pill in account headers and sidebar lists:

| Badge | Meaning | Action Available |
| :--- | :--- | :--- |
| <span style="background:#ecfdf5;color:#065f46;border:1px solid #a7f3d0;padding:2px 8px;border-radius:12px;font-weight:700;">Verified catalog v1</span> | Hydrated from catalog golden record; pristine with no local edits. | Up to date. |
| <span style="background:#eff6ff;color:#1e40af;border:1px solid #bfdbfe;padding:2px 8px;border-radius:12px;font-weight:700;">Update available (v2)</span> | A newer verified catalog snapshot (v2) is available in the platform catalog. | Click to preview diff and refresh. |
| <span style="background:#fffbeb;color:#92400e;border:1px solid #fde68a;padding:2px 8px;border-radius:12px;font-weight:700;">Verified catalog v1 · edited</span> | Hydrated from catalog, but locally edited by tenant team members. | Local changes protected during refresh. |
| <span style="background:#f1f5f9;color:#475569;border:1px solid #cbd5e1;padding:2px 8px;border-radius:12px;font-weight:700;">Fetched</span> | Harvested directly via tenant pipeline execution. | Local origin. |

---

### 2.5 Refreshing Intelligence & Resolving Local Edits (C2)
When an account has a newer catalog snapshot available:
1. Click the **Update Available** pill or open **Catalog Refresh**.
2. **Side-by-Side Diff Preview:** Review added/changed personas, LOB modifications, and company profile changes.
3. **Local-Edit Protection:**
   - If tenant users modified any fields (e.g., custom persona phone number or LOB notes), the system flags **Local Edits Present** and protects those rows.
   - **Safe Refresh:** Updates only unmodified records and appends new personas.
   - **Explicit Overwrite:** If you wish to overwrite local edits, you must explicitly check *"I understand local edits will be replaced"* and provide a justification. A point-in-time backup is automatically archived into `account_origin_backups` before overwrite.

---

### 2.6 Credit Transparency & Transaction Ledger (C5)
Navigate to the **Credits & Ledger** tab in the main navigation hub:
- **Available Balance Meter:** Current balance vs. allocated limit with a visual progress bar.
- **Low-Balance Alert Banner:** Automatically warns users when credits drop below 5,000 cr.
- **KPI Cards:**
  - **Verified Reuse Savings:** Estimated credits saved vs. fresh vendor crawls.
  - **Reuse Fees Paid:** Total credits deducted for catalog imports.
  - **Net Credit Savings:** Gross savings minus fees paid.
- **Spend by Category:** Visual breakdown across Catalog Reuse, Catalog Refresh, Pipeline Enrich, and Refunds.
- **Transaction Audit Ledger:** Paginated and filterable transaction table with timestamps, deltas, user names, and formula details.
- **CSV Export:** Client Administrators can download a streaming CSV report protected against spreadsheet formula injection attacks (`=`, `+`, `-`, `@`).

---

## 3. Super-Admin Administrative Workflows

Platform Super-Administrators manage cross-tenant governance, approvals, and platform health.

### 3.1 Super-Admin Approval Inbox (C3)
Accessible via the top navigation bar or `/api/catalog/inbox`:
- **Keyboard Shortcuts:**
  - `J` / `K`: Navigate down / up through pending requests.
  - `A`: Approve selected request.
  - `R`: Reject selected request.
- **Risk Indicators:**
  - `quota_exceeded`: Target client has reached their plan's account quota.
  - `insufficient_credits`: Target client does not have enough credits to pay the fee.
  - `sla_breached`: Request has waited longer than `CATALOG_APPROVAL_SLA_HOURS`.
- **Version Superseding (`use_latest_version`):**
  - If a newer snapshot was published while the request was pending, the admin can approve either the requested version or toggle `use_latest_version=True` to hydrate the latest version.
- **Bulk Decisions:** Select multiple requests and execute one-click bulk approval or rejection.

### 3.2 Platform Credit Summary
Super-administrators can query global credit metrics across all tenant organizations via `GET /api/credits/platform-summary` or filter by individual client slug.

### 3.3 Credit Ledger Reconciliation Guard
A maintenance command is provided to audit database integrity and detect discrepancies between `clients.api_credits_used` and transaction ledger sums:

```bash
# Audit all clients and report drift
python scripts/verify_credit_ledger.py --verbose

# Audit a single tenant
python scripts/verify_credit_ledger.py --client acme_corp

# Automatically reconcile and repair detected drift
python scripts/verify_credit_ledger.py --fix
```

---

## 4. Permission Matrix

| Role | Browse Library | Request Reuse | Execute Reuse | Refresh Diff | View Ledger | Export CSV | Approve Inbox |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Super Admin** | ✓ | ✓ | ✓ | ✓ | ✓ (Full) | ✓ | ✓ |
| **Client Admin** | ✗ | ✓ | ✓ | ✓ | ✓ (Full) | ✓ | ✗ |
| **Analyst** | ✗ | ✗ | ✗ | ✓ (Read) | ✓ (User Masked) | ✗ | ✗ |
| **Viewer** | ✗ | ✗ | ✗ | ✓ (Read) | ✓ (User Masked) | ✗ | ✗ |
