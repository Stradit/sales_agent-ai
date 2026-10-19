# CRM Forecasting — overview, features and tickets

Status (2026-10-19): **built, hidden in production.** The page and its nav link only appear with
`FORECAST_ENABLED=1` in `.env` (`api.py`); the API is live either way. Code:
`apps/sales_crm/forecast.py` (API + logic), `frontend/templates/forecast.html`,
`frontend/js/modules/forecast/main.js`. End-to-end flow with Introductions and Deals:
[apps/sales_crm/README.md §0](../apps/sales_crm/README.md).

---

## 1. High-level overview

The forecast answers three questions for a fiscal quarter: **how much have we won, how much will we
win, and what changed since last week?** It is computed live from the deals table — pure SQL and
arithmetic, no AI and no copy of the data.

```
Deals (stage, value, currency, close date, forecast category, probability, owner, business line)
   │  DB trigger: amount_usd = value × fx rate · won → "closed" + closed_at · lost → "omitted"
   ▼
Quarter roll-up ──► KPIs vs target ──► grid by owner / business line / stage / category ──► deals table
   │
   └─ Monday 06:00 IST snapshot of every deal ──► "What moved" (this week vs the snapshot)
```

**Periods.** Fiscal quarters named `FY2026-Q3`. The fiscal-year start month is a CRM setting
(January by default); a fiscal year is named after the calendar year it ends in. A deal belongs to the
quarter of its close date: `closed_at` for won deals, `expected_close` for open ones. Open deals with
no expected close are listed separately as "not in any quarter".

**Categories** (the rep sets them on open deals; won/lost are set by the database):

| Category | Meaning |
|---|---|
| Closed won | Stage = Won (automatic) |
| Commit | The rep is confident it closes this quarter |
| Best case | Could close this quarter if things go well |
| Pipeline | Open, not yet called (default) |
| Omitted | Excluded from the forecast (lost deals automatically) |

**Roll-ups** (cumulative, the usual CRM convention; all money in USD):

| Measure | Formula |
|---|---|
| Commit forecast | Closed + Commit |
| Best-case forecast | Closed + Commit + Best case |
| Open pipeline | Commit + Best case + Pipeline |
| Weighted pipeline | Σ open amount × probability (deal override, else the stage default: Intro 10 · Discovery 20 · Proposal 40 · Pilot 60 · Contract 80 %) |
| Gap to target | max(0, Target − Closed) |
| Attainment | Closed ÷ Target |
| Coverage | Open pipeline ÷ Gap (flagged below 3×) |

**Currency.** Each deal keeps its own currency; `amount_usd` is maintained by a trigger from
`fx_rates`. Seeded rates (EUR 1.08, GBP 1.27, INR 0.012) are placeholders an admin must replace. A
currency without a rate counts as 0 and the page warns which deals are affected.

**Targets.** Per quarter, either per rep **or** per business line (never both on one row).

**Who sees what** (`apps/sales_crm/role_guide.py`):

| Role | View | Targets | Exchange rates |
|---|---|---|---|
| Super admin | Everything | Set any | Edit |
| Sales manager | Their team | Set for their reports (not business lines) | — |
| Sales rep | Only the deals they own, even on accounts they share (FC-01) | — | — |
| Viewer | Read-only (owns no deals, so empty) | — | — |
| Partner | No access | — | — |

---

## 2. Feature list (built)

**Forecast page (`/forecast`)**
- Quarter picker (2 back, current, 4 ahead), business-line filter, owner filter ("everyone I can
  see" or one person); the choice is kept in the URL so a view can be shared.
- KPI cards: Target (with % attained), Closed won (with amount to go), Commit forecast, Best-case
  forecast, Open pipeline (with coverage of the gap), Weighted pipeline.
- Progress bar to target: closed / commit / best case stacked against the target line.
- Roll-up grid grouped by **owner, business line, stage or category**: closed, commit, best case,
  pipeline, weighted, commit forecast, target with attainment, coverage (low coverage highlighted);
  owners with a target but no deals still appear. Clicking a cell filters the deals table.
- Deals table for the quarter: account, owner, stage, category, USD amount, probability (default or
  override), weighted amount, close date, next step. **Category is editable inline**; closed deals
  are categorised automatically.
- "Not in any quarter" list: open deals without an expected close date.
- **What moved**: changes since the last weekly snapshot — won, lost, slipped out of the quarter, new,
  category change, advanced / moved back a stage, slipped, pulled in, amount change, removed —
  ordered by importance, then amount.
- Targets dialog (managers and admins) and exchange-rate dialog (admins).
- Excel export: Summary, the roll-up grid, Deals, Changes.

**Behind the scenes**
- DB trigger `crm_deal_forecast_fields`: `amount_usd`, `closed_at`, category for won/lost, and
  category reset when a deal is reopened.
- Weekly snapshot (`forecast_snapshots`) every Monday 06:00 IST, plus a baseline on first start; one
  app instance takes it (Postgres advisory lock). Admins can take one now (`POST /api/forecast/snapshot`).
- Changing an exchange rate recomputes the USD amount of every deal in that currency; target and rate
  changes are written to the audit log.
- Feature flag `FORECAST_ENABLED` hides the page and nav link; old links go to Deals.

**API** (`/api/forecast`): `GET /meta` · `GET ?period=&business_line_id=&owner_id=&group_by=` ·
`GET /changes` · `GET|PUT /targets` · `PUT /fx` · `POST /snapshot` · `GET /export`.

**Tables:** `deals` (`forecast_category`, `probability`, `amount_usd`, `expected_close`, `closed_at`),
`fx_rates`, `sales_targets`, `forecast_snapshots`, `crm_settings` (`fiscal_year_start_month`,
`stage_probability`).

**Tests:** permission matrix (`apps/sales_crm/permission_matrix.py`): category set by role, won →
closed + `closed_at`, won counted as closed in the right quarter with FX, reopen → pipeline,
"what moved" kinds, export.

---

## 3. Tickets

`Type` · `Priority` · status. Delivered work is listed first (for the record), then the backlog.

### Delivered

**FC-D1 — Quarter forecast roll-up** · Story · Done
Show a fiscal quarter's closed, commit, best-case and pipeline totals in USD with commit/best-case
forecasts, weighted pipeline, target, gap, attainment and coverage; group by owner, business line,
stage or category; filter by business line and owner.

**FC-D2 — Forecast categories on deals** · Story · Done
Reps set Commit / Best case / Pipeline / Omitted on open deals (deal room and inline on the forecast);
won → Closed and lost → Omitted automatically; reopening resets to Pipeline.

**FC-D3 — Multi-currency with USD roll-up** · Story · Done
Deals keep their currency; a trigger keeps `amount_usd` from `fx_rates`; admins edit rates; missing
rates are flagged instead of silently mis-adding.

**FC-D4 — Sales targets** · Story · Done
Per-quarter targets per rep or per business line; managers set their reports' targets, admins any;
audit-logged.

**FC-D5 — Weekly snapshots and "What moved"** · Story · Done
Monday snapshot of every deal; the page lists what changed since: won, lost, slipped out, new,
category, stage and amount changes, close-date moves.

**FC-D6 — Forecast export** · Story · Done
Excel workbook with summary, roll-up, deals and changes for the current filters.

### Backlog

**FC-01 — Reps see only their own deals in the forecast** · Bug · High · **Done 2026-10-19**
*Description:* The forecast filters deals by account access only, so a rep who shares an account
with a colleague sees and totals the colleague's deals. The role guide (and the Deals pages since
2026-10-17) say reps see only their own deals.
*Acceptance:* for role `user`, roll-up, deals table, "What moved" and export include only deals they
own (same rule as `apps/sales_deals/api.py: own_deals_only`); managers and admins unchanged; matrix
checks added.
*Resolution:* `forecast.scoped_owner()` forces a rep's owner filter to themself in `rollup()` and
`changes()` (the export uses both). Six matrix checks; three of them fail on the old code.

**FC-02 — Launch checklist: turn the forecast on in production** · Task · High
*Description:* The feature is hidden (`FORECAST_ENABLED=0`). Before enabling: set real exchange rates
(the seeded ones are placeholders), confirm the fiscal-year start month and stage probabilities in
Admin → CRM settings, enter this quarter's targets, check every open deal has an expected close and a
category, take a baseline snapshot.
*Acceptance:* `FORECAST_ENABLED=1` in the production `ENV_FILE`; the page shows no "no exchange
rate" warning and no unexpected undated deals; a short how-to added to the user manual tour.

**FC-03 — Automatic exchange rates** · Story · Medium
*Description:* Rates are entered by hand, so USD totals drift. Fetch daily rates from a free source
(e.g. ECB reference rates) into `fx_rates` with `source='ecb'`; an admin override still wins until
cleared.
*Acceptance:* daily job (advisory-locked, like the snapshot); "last updated" shown in the dialog;
failure leaves the previous rates and logs a warning.

**FC-04 — Forecast call per rep and manager override** · Story · Medium
*Description:* Today the forecast is the arithmetic of deal categories. Add the standard weekly
"call": each rep submits their commit and best-case numbers for the quarter (pre-filled from their
deals, editable with a note); a manager can override their team's number; the page shows rep call vs
manager call vs system roll-up.
*Acceptance:* new table (migration), submissions per rep per week; history kept; visible to the rep,
their manager and admins only.

**FC-05 — Forecast trend chart** · Story · Medium
*Description:* Show how the commit and best-case forecasts and closed won moved week by week through
the quarter (from `forecast_snapshots`), against the target line.
*Acceptance:* one chart under the KPIs; respects the same filters and scope; no new data needed.

**FC-06 — Slip alerts** · Story · Medium
*Description:* When a Commit or Best-case deal slips out of the current quarter, loses its close date
or drops category, notify the owner and their manager (existing notifications, with an email
preference), once per change.
*Acceptance:* uses the same change detection as "What moved"; deduplicated; respects notification
settings and `CRM_NOTIFY_SUPPRESS` in tests.

**FC-07 — Probabilities from history** · Story · Low
*Description:* Stage probabilities are fixed defaults. Offer "historical" probabilities computed from
actual stage-to-won conversion over the last 12 months (the Deals → Insights funnel already has the
data), shown next to the defaults; an admin chooses which one weighting uses.
*Acceptance:* needs a minimum sample per stage (else fall back to the default); the setting is
recorded in `crm_settings`.

**FC-08 — Month and full-year views** · Story · Low
*Description:* Periods are quarters only. Add month and fiscal-year periods to the picker; targets
stay quarterly and are summed for a year (pro-rated for a month).
*Acceptance:* period keys `FY2026-M07` / `FY2026`; export and "What moved" work for each.

**FC-09 — Unit tests for period math and roll-up formulas** · Task · Low
*Description:* Fiscal-period naming and bounds (incl. a non-January fiscal year start) and the
roll-up formulas are covered only through the permission matrix against a live database. Add fast
unit tests next to `tests/test_deal_health.py`.
*Acceptance:* `period_of`, `period_bounds`, `period_list`, `_finish` (gap, attainment, coverage,
no-target cases) and `_bucket` covered; runs in CI's `test` job without a database.
