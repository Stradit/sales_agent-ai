// Deals pipeline page (/deals). Board of Intro → Discovery → Proposal → Pilot → Contract
// with drag-and-drop, plus a deal room drawer (stage stepper, fields, health, exit
// checklist, buying committee, activity, tasks, stage toolkit) and a weekly digest. API: apps/sales_deals/api.py.
import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { showToast } from '../toast.js';
import { esc } from '../utils.js';
import { downloadFile } from '../download.js';
import { renderToolkit, copilotHref } from './toolkit.js';
import { mountTimeline } from '../activity-timeline.js';
import { photoAttr } from '../persona-photo.js';
import { initViews, refreshViews, filtersChanged } from './views.js';
import { planPane, planTabLabel, planClick, planChange, planSubmit, resetPlanEditing } from './plan.js';
import { initRoomShare, roomShareBox, roomShareClick, roomShareSubmit } from './room-share.js';
import { initNba, nbaCard, nbaClick, nbaInput } from './nba.js';

const $ = (id) => document.getElementById(id);
const OPEN_STAGES = ['intro', 'discovery', 'proposal', 'pilot', 'contract'];
const ROLE_LABEL = { champion: 'Champion', economic_buyer: 'Economic buyer', technical_evaluator: 'Technical evaluator',
  influencer: 'Influencer', blocker: 'Blocker', user: 'User' };
const ACTIVITY_ICON = { note: 'fa-comment', stage: 'fa-diagram-next', checklist: 'fa-list-check', stakeholder: 'fa-user-plus',
  field: 'fa-pen', created: 'fa-flag' };

const PRIORITIES = ['high', 'medium', 'low'];

const st = { meta: null, deals: [], summary: {}, accounts: [], deal: null, tab: 'toolkit', dragId: null, tk: null, tkKey: '',
  me: null, users: null, editTask: null };

// Mirrors WON_FROM in apps/sales_deals/api.py: no "won" before a proposal is on the table.
const canWin = (d) => ((st.meta && st.meta.won_from) || ['proposal', 'pilot', 'contract']).includes(d.stage);

async function api(path, opts = {}) {
  const res = await fetch(`/api/deals${path}`, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); throw new Error('unauthenticated'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

// ── Formatting ───────────────────────────────────────────────────────────────
function money(v, cur = 'USD') {
  if (v == null) return '—';
  try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: cur, notation: v >= 100000 ? 'compact' : 'standard', maximumFractionDigits: v >= 100000 ? 1 : 0 }).format(v); }
  catch { return `${Math.round(v).toLocaleString()} ${cur}`; }
}
function dateLabel(d) { return d ? new Date(`${String(d).slice(0, 10)}T00:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''; }
function isOverdue(d) { return d && new Date(`${String(d).slice(0, 10)}T23:59:59`) < new Date(); }
function initials(n) { return (n || '?').split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase(); }
function ago(ts) {
  const m = Math.round((Date.now() - new Date(ts).getTime()) / 60000);
  if (m < 60) return `${Math.max(m, 1)}m ago`;
  if (m < 1440) return `${Math.round(m / 60)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
}
function healthBadge(h) {
  if (!h || h.score == null) return '<span class="dl-health dl-health-closed">Closed</span>';
  return `<span class="dl-health dl-health-${h.level}" title="Deal health ${h.score}/100">${h.score}</span>`;
}

// ── Board ────────────────────────────────────────────────────────────────────
function card(d) {
  const overdue = isOverdue(d.next_step_due);
  // Show the most urgent gap on the card: a nudge (quiet / overdue / close date passed) before the rest
  const h = d.health || {};
  const urgentAt = (h.gap_codes || []).findIndex(c => ATTENTION.includes(c));
  const gap = (h.gaps || [])[urgentAt >= 0 ? urgentAt : 0] || '';
  return `<article class="dl-card${d.stage === 'won' ? ' dl-won' : ''}${d.stage === 'lost' ? ' dl-lost' : ''}${needsAttention(d) ? ' dl-attn' : ''}" draggable="${OPEN_STAGES.includes(d.stage) && !document.body.classList.contains('role-viewer')}"
      data-deal="${d.id}" tabindex="0" aria-label="${esc(d.name)}, ${esc(d.account_name)}">
    <div class="dl-card-top">
      <div><div class="dl-card-name">${esc(d.name)}</div><div class="dl-card-acct">${esc(d.account_name)}${d.business_line_name ? ` · ${esc(d.business_line_name)}` : ''}</div></div>
      ${healthBadge(d.health)}
    </div>
    <div class="dl-card-row">
      <span class="dl-value">${esc(money(d.value_amount, d.currency))}</span>
      ${['commit', 'best_case'].includes(d.forecast_category) && OPEN_STAGES.includes(d.stage) ? `<span class="dl-fc-tag ${d.forecast_category}">${d.forecast_category === 'commit' ? 'Commit' : 'Best case'}</span>` : ''}
      ${d.expected_close ? `<span><i class="fa-regular fa-calendar"></i> ${esc(dateLabel(d.expected_close))}</span>` : ''}
      <span title="Buying committee"><i class="fa-solid fa-users"></i> ${d.stakeholder_count}</span>
      ${d.open_tasks ? `<span title="Open tasks"><i class="fa-solid fa-list-check"></i> ${d.open_tasks}</span>` : ''}
      ${d.stage_progress && d.stage_progress.total ? `<span title="Exit criteria done"><i class="fa-regular fa-square-check"></i> ${d.stage_progress.done}/${d.stage_progress.total}</span>` : ''}
    </div>
    ${d.next_step ? `<div class="dl-card-next ${overdue ? 'dl-overdue' : ''}"><i class="fa-solid fa-arrow-right"></i> ${esc(d.next_step)}${d.next_step_due ? ` · ${esc(dateLabel(d.next_step_due))}` : ''}</div>` : ''}
    ${gap && OPEN_STAGES.includes(d.stage) ? `<div class="dl-card-gap"><i class="fa-solid fa-triangle-exclamation"></i> ${esc(gap)}</div>` : ''}
    ${d.stage === 'lost' ? `<div class="dl-card-lost"><i class="fa-solid fa-circle-xmark"></i> ${esc(lostText(d) || 'No reason recorded')}</div>` : ''}
  </article>`;
}

// Deals that need a nudge: no recent activity, next step overdue, or close date already past
const ATTENTION = ['quiet', 'no_activity', 'next_step_overdue', 'close_date_passed'];
const needsAttention = (d) => OPEN_STAGES.includes(d.stage) && ((d.health && d.health.gap_codes) || []).some(c => ATTENTION.includes(c));

function renderBoard() {
  const cols = (st.meta ? st.meta.stages : []).filter(s => OPEN_STAGES.includes(s.key));
  const attnOnly = !!($('dlAttention') && $('dlAttention').checked);
  if ($('dlAttentionCount')) $('dlAttentionCount').textContent = st.deals.filter(needsAttention).length;
  $('dlBoard').innerHTML = cols.map(s => {
    const ds = st.deals.filter(d => d.stage === s.key && (!attnOnly || needsAttention(d)));
    const sum = st.summary[s.key] || { count: 0, value: 0 };
    return `<section class="dl-col" data-stage="${s.key}" aria-label="${esc(s.label)}">
      <div class="dl-col-head"><div class="dl-col-title">${esc(s.label)} <span class="dl-count">${sum.count}</span></div>
        <div class="dl-col-meta">${esc(money(sum.value))} pipeline</div></div>
      <div class="dl-col-body">${ds.map(card).join('') || '<div class="dl-empty-col">Drag a deal here</div>'}</div>
    </section>`;
  }).join('');
  const closed = st.deals.filter(d => ['won', 'lost'].includes(d.stage));
  const won = st.summary.won || { count: 0, value: 0 };
  const lost = st.summary.lost || { count: 0, value: 0 };
  const reasons = (lost.by_reason || []).map(r => `<span class="dl-reason-pill${r.key === 'uncategorised' ? ' muted' : ''}">${esc(r.label)} <b>${r.count}</b></span>`).join('');
  $('dlClosed').innerHTML = closed.length ? `<details><summary>Closed — ${won.count} won (${esc(money(won.value))}) · ${lost.count} lost</summary>
      ${reasons ? `<div class="dl-loss-reasons"><span class="dl-loss-title">Why we lose</span>${reasons}</div>` : ''}
      <div class="dl-closed-grid">${closed.map(card).join('')}</div></details>` : '';
  const open = st.deals.filter(d => OPEN_STAGES.includes(d.stage));
  const atRisk = open.filter(d => d.health && d.health.level === 'risk').length;
  $('dlSummary').textContent = open.length
    ? `${open.length} open deals · ${money(open.reduce((a, d) => a + (d.value_amount || 0), 0))} pipeline${atRisk ? ` · ${atRisk} at risk` : ''}`
    : 'No open deals yet — create one to start tracking the journey from intro to contract.';
  refreshViews();
}

// ── Weekly digest (README §21.5) ─────────────────────────────────────────────
const DIGEST_KEY = 'dl.digest.open';
function digestList(title, icon, items, tone) {
  if (!items.length) return '';
  return `<div class="dl-dg-list ${tone}"><h3><i class="fa-solid ${icon}"></i> ${esc(title)} <span>${items.length}</span></h3><ul>
    ${items.slice(0, 5).map(x => `<li><button type="button" data-deal-open="${x.id}"><strong>${esc(x.name)}</strong>
      <small>${esc(x.account_name)} · ${esc(x.stage_label || x.to_stage || '')}${x.why ? ` · ${esc(x.why)}` : ''}</small></button></li>`).join('')}
    ${items.length > 5 ? `<li class="dl-dg-more">+${items.length - 5} more</li>` : ''}</ul></div>`;
}

async function loadDigest() {
  const box = $('dlDigest');
  try {
    const g = await api(`/pipeline/digest?days=7${$('dlMine').checked ? '&mine=true' : ''}`);
    const t = g.totals;
    if (!t.open && !g.closed.length) { box.innerHTML = ''; return; }
    const moves = g.moves.map(m => ({ id: m.deal_id, name: m.name, account_name: m.account_name,
      stage_label: `${m.from_stage} → ${m.to_stage}` }));
    const lists = [digestList('At risk', 'fa-heart-crack', g.at_risk, 'risk'), digestList('Overdue next steps', 'fa-clock', g.overdue, 'risk'),
      digestList('Stuck > 30 days', 'fa-hourglass-half', g.stuck, 'watch'), digestList('Closing in 30 days', 'fa-calendar-check', g.closing_soon, 'good'),
      digestList('Moved this week', 'fa-arrow-right', moves, 'info'), digestList('Closed this week', 'fa-flag-checkered', g.closed, 'info')].join('');
    let open = true;
    try { open = localStorage.getItem(DIGEST_KEY) !== '0'; } catch { /* storage unavailable */ }
    box.innerHTML = `<details${open ? ' open' : ''}><summary><span><i class="fa-solid fa-newspaper"></i> This week in your pipeline</span>
        <span class="dl-dg-kpis"><span><b>${t.open}</b> open</span><span><b>${esc(money(t.value))}</b> pipeline</span>
        <span title="Value × stage probability (Intro 10% … Contract 80%)"><b>${esc(money(t.weighted))}</b> weighted</span>
        ${t.avg_health != null ? `<span><b>${t.avg_health}</b> avg health</span>` : ''}</span></summary>
      <div class="dl-dg-grid">${lists || '<p class="dl-dg-calm"><i class="fa-solid fa-circle-check"></i> Nothing needs attention this week.</p>'}</div></details>`;
    box.querySelector('details').addEventListener('toggle', (e) => {
      try { localStorage.setItem(DIGEST_KEY, e.target.open ? '1' : '0'); } catch { /* ignore */ }
    });
  } catch { box.innerHTML = ''; }
}

async function loadDeals() {
  const params = new URLSearchParams();
  if ($('dlAccount').value) params.set('account_id', $('dlAccount').value);
  if ($('dlBusinessLine').value) params.set('business_line_id', $('dlBusinessLine').value);
  if ($('dlMine').checked) params.set('mine', 'true');
  if ($('dlSearch').value.trim()) params.set('q', $('dlSearch').value.trim());
  try {
    const data = await api(`?${params}`);
    st.deals = data.deals;
    st.summary = data.summary;
    st.lostCategories = data.lost_categories || st.lostCategories || {};
    if (data.own_only) {   // sales reps only ever see their own deals (server-enforced)
      $('dlMine').checked = true; $('dlMine').disabled = true;
      $('dlMine').closest('label').title = 'Sales reps see the deals they own';
    }
    renderBoard();
    loadDigest();
  } catch (err) { $('dlSummary').textContent = err.message; }
}

// After a stage move: one toast saying what happened (and any open exit criteria noted), with Undo
function stageMoved(d, fromStage) {
  const warn = (d.warnings || [])[0];
  const note = warn ? ` (${warn.split(':')[0].replace(/^Moved on with /, '')} noted)` : '';
  showToast(`Moved to ${d.stage_label}${note}`, { action: { label: 'Undo', onClick: () => undoMove(d.id, fromStage) } });
}

async function undoMove(id, fromStage) {
  try {
    // Going back a stage needs a reason on the server; record why
    const d = await api(`/${id}`, { method: 'PATCH', body: { stage: fromStage, reason: 'Undo — moved by mistake' } });
    if (st.deal && st.deal.id === id) { st.deal = d; renderRoom(); }
    await loadDeals();
    showToast(`Back in ${d.stage_label}`);
  } catch (err) { showToast(err.message); }
}

async function moveDeal(id, stage, extra = {}) {
  const from = (st.deals.find(x => x.id === id) || {}).stage;
  try {
    const d = await api(`/${id}`, { method: 'PATCH', body: { stage, ...extra } });
    if (from && d.stage !== from) stageMoved(d, from);
    else (d.warnings || []).forEach(w => showToast(w));
    if (st.deal && st.deal.id === id) { st.deal = d; renderRoom(); }
    await loadDeals();
  } catch (err) { showToast(err.message); }
}

function canStep(d, s) {
  if (!d || !OPEN_STAGES.includes(d.stage) || s === d.stage) return false;
  return Math.abs(OPEN_STAGES.indexOf(s) - OPEN_STAGES.indexOf(d.stage)) === 1;
}

async function moveToStage(target, dealId = null) {
  const d = dealId ? st.deals.find(x => x.id === dealId) : st.deal;
  if (!d || target === d.stage) return;
  const from = OPEN_STAGES.indexOf(d.stage);
  const to = OPEN_STAGES.indexOf(target);
  if (from >= 0 && to >= 0) {
    if (Math.abs(to - from) !== 1) { showToast('Move one stage at a time'); return; }
    if (to < from) {
      if (!st.deal || st.deal.id !== d.id) await openDeal(d.id);
      const targetLabel = (st.meta.stages.find(x => x.key === target) || {}).label || target;
      const reason = await askReason(`Why move back to ${targetLabel}? (required)`, 'Move back', false);
      if (reason) {
        if (st.deal && st.deal.id === d.id) patchDeal({ stage: target, reason });
        else moveDeal(d.id, target, { reason });
      }
      return;
    }
  }
  if (st.deal && st.deal.id === d.id) patchDeal({ stage: target });
  else moveDeal(d.id, target);
}

// ── Deal room ────────────────────────────────────────────────────────────────
function field(label, input, wide = false) { return `<label class="dl-field${wide ? ' dl-field-wide' : ''}">${label}${input}</label>`; }

// What each stage is for, in one line (matches the exit criteria in apps/sales_deals/api.py CHECKLIST)
const STAGE_GOAL = {
  intro: 'Find the right person and a reason to talk now, then book the first meeting.',
  discovery: 'Confirm their pains, who decides and how they buy.',
  proposal: 'Map your offering to their priorities and get the proposal in front of them.',
  pilot: 'Prove the value in a time-boxed pilot with agreed success criteria.',
  contract: 'Get approvals and the security review through, then the signature.',
};
// Exit criteria the app ticks by itself (AUTO_CHECK in api.py) — show where to act instead
const AUTO_HINT = {
  'intro|target_identified': ['Add someone in Buying committee', 'committee'],
  'discovery|economic_buyer': ['Set an Economic buyer in Buying committee', 'committee'],
  'discovery|champion': ['Set a Champion in Buying committee', 'committee'],
  'proposal|offerings_mapped': ['Pick the StradIT offerings above', null],
};
const stageLabel = (s) => ((st.meta.stages.find(x => x.key === s) || {}).label || s);

// Deal health gaps → the one-click fix (codes from health() in apps/sales_deals/api.py)
const GAP_ACTIONS = {
  no_champion: { label: 'Add a champion', tab: 'committee', role: 'champion' },
  no_economic_buyer: { label: 'Add the economic buyer', tab: 'committee', role: 'economic_buyer' },
  blockers: { label: 'Review the committee', tab: 'committee' },
  no_next_step: { label: 'Set a next step', field: 'next_step' },
  next_step_overdue: { label: 'Update next step', field: 'next_step_due' },
  close_date_passed: { label: 'Update close date', field: 'expected_close' },
  quiet: { label: 'Log activity', tab: 'activity' },
  no_activity: { label: 'Log activity', tab: 'activity' },
  exit_criteria: { label: 'Review stage', box: true },
  long_in_stage: { label: 'Move on or update', box: true },
  plan_overdue: { label: 'Review the close plan', tab: 'plan' },
};

function runGapAction(code) {
  const a = GAP_ACTIONS[code];
  if (!a) return;
  const room = $('dlRoom');
  const flash = (node) => { node.classList.add('dl-flash'); setTimeout(() => node.classList.remove('dl-flash'), 1600); };
  if (a.tab) {
    st.tab = a.tab; renderRoom();
    const tabs = room.querySelector('.dl-tabs');
    if (tabs) tabs.scrollIntoView({ behavior: 'smooth', block: 'start' });
    if (a.role && $('dlPersonRole')) { $('dlPersonRole').value = a.role; $('dlPersonSearch').focus(); }
    return;
  }
  if (a.field) {
    const input = room.querySelector(`[data-field="${a.field}"]`);
    if (input) { input.scrollIntoView({ behavior: 'smooth', block: 'center' }); input.focus(); flash(input); }
    return;
  }
  if (a.box) {
    const box = room.querySelector('.dl-stage-box');
    if (box) { box.scrollIntoView({ behavior: 'smooth', block: 'start' }); flash(box); }
  }
}

// The guided box at the top of the deal room: where the deal is, what this stage is for, what's
// left, and the one obvious next action (mirrors the introductions drawer's .in-stage-box).
function stageBox(d) {
  const i = OPEN_STAGES.indexOf(d.stage);
  const next = OPEN_STAGES[i + 1] || null;
  const items = d.checklist.filter(c => c.stage === d.stage);
  const done = items.filter(c => c.done).length;
  const open = items.length - done;
  const days = d.health && d.health.days_in_stage != null ? ` · ${d.health.days_in_stage} day${d.health.days_in_stage === 1 ? '' : 's'}` : '';
  const pct = items.length ? Math.round(done / items.length * 100) : 100;
  const list = items.map(c => {
    const auto = AUTO_HINT[`${c.stage}|${c.item_key}`];
    return `<li class="${c.done ? 'done' : ''}"><label><input type="checkbox" data-check="${c.stage}|${c.item_key}"${c.done ? ' checked' : ''}>
      <span>${esc(c.label)}</span></label>
      ${auto && !c.done ? `<button type="button" class="dl-stage-auto" ${auto[1] ? `data-tab="${auto[1]}"` : 'data-goto-offerings'} title="Ticks itself once this is done">${esc(auto[0])} →</button>` : ''}</li>`;
  }).join('');
  const win = canWin(d)
    ? `<button type="button" class="dl-btn${next ? '' : ' dl-btn-primary'}" data-close-deal="won"><i class="fa-solid fa-trophy"></i> Mark won</button>`
    : `<span class="dl-hint" title="A deal can be marked won from Proposal onwards"><i class="fa-solid fa-lock"></i> Won unlocks at Proposal</span>`;
  return `<section class="dl-stage-box" aria-label="Stage guidance">
    <div class="dl-stage-head">
      <span class="dl-stage-now">${esc(stageLabel(d.stage))}${days}</span>
      ${next ? `<span class="dl-stage-next">Next: <strong>${esc(stageLabel(next))}</strong></span>` : '<span class="dl-stage-next">Final stage</span>'}
    </div>
    <p class="dl-stage-goal">${esc(STAGE_GOAL[d.stage] || '')}</p>
    ${items.length ? `<div class="dl-stage-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${items.length}" aria-valuenow="${done}" aria-label="Exit criteria done">
        <span style="width:${pct}%"></span></div>
      <div class="dl-stage-count">${done} of ${items.length} exit criteria done</div>
      <ul class="dl-stage-list">${list}</ul>` : ''}
    <div class="dl-close-btns dl-stage-actions">
      ${next ? `<button type="button" class="dl-btn dl-btn-primary" data-advance="${next}">Move to ${esc(stageLabel(next))} <i class="fa-solid fa-arrow-right"></i></button>` : ''}
      ${win}
      <button type="button" class="dl-btn dl-btn-danger" data-close-deal="lost"><i class="fa-solid fa-circle-xmark"></i> Mark lost</button>
    </div>
    ${next && open ? `<p class="dl-stage-note"><i class="fa-solid fa-circle-info"></i> You can move on now — the ${open} open item${open === 1 ? '' : 's'} will be noted in the change log.</p>` : ''}
  </section>`;
}

function renderRoom() {
  const d = st.deal;
  if (!d) return;
  const idx = OPEN_STAGES.indexOf(d.stage);
  const h = d.health || {};
  const byStage = OPEN_STAGES.map(s => ({ s, items: d.checklist.filter(c => c.stage === s) }));
  const offerings = (st.meta.offerings || []).map(o => `<button type="button" class="dl-chip${d.offerings.includes(o.key) ? ' on' : ''}" data-offering="${o.key}" aria-pressed="${d.offerings.includes(o.key)}">${esc(o.label)}</button>`).join('');
  const committeeGaps = d.committee_gaps.map(g => ROLE_LABEL[g]).join(' and ');
  $('dlRoom').innerHTML = `
    <div class="dl-room-head">
      <div class="dl-room-head-row">
        <div><h2 class="dl-room-title" id="dlRoomTitle">${esc(d.name)}</h2>
          <div class="dl-room-acct"><a href="/?account=${d.account_id}">${esc(d.account_name)}</a>${d.owner && d.owner.name ? ` · owner ${esc(d.owner.name)}` : ''}
            ${d.introduction ? ` · <a href="/introductions?intro=${d.introduction.id}" title="Sourced by a warm introduction"><i class="fa-solid fa-handshake"></i> Introduced by ${esc(d.introduction.connector_name)}${d.introduction.attribution_pct != null ? ` (${d.introduction.attribution_pct}%)` : ''}</a>` : ''}</div></div>
        <button type="button" class="dl-icon" data-close-room aria-label="Close deal"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="dl-stepper" role="group" aria-label="Stage">${OPEN_STAGES.map((s, i) => {
        const lbl = st.meta.stages.find(x => x.key === s).label;
        const allowed = canStep(d, s);
        return `<button type="button" class="dl-step${i < idx ? ' done' : ''}${s === d.stage ? ' current' : ''}" data-stage-step="${s}" aria-current="${s === d.stage ? 'step' : 'false'}"${allowed ? '' : ' disabled'}${!allowed && s !== d.stage && OPEN_STAGES.includes(d.stage) ? ' title="Move one stage at a time"' : ''}>${esc(lbl)}</button>`;
      }).join('')}</div>
      ${OPEN_STAGES.includes(d.stage) ? nbaCard(d) + stageBox(d)
        : `<div class="dl-warn">${d.stage === 'won' ? '<i class="fa-solid fa-trophy"></i> ' : ''}This deal is <strong>${esc(d.stage_label)}</strong>${lostText(d) ? ` — ${esc(lostText(d))}` : ''}. <button type="button" class="dl-btn" data-stage-step="contract">Reopen</button></div>`}
    </div>
    <div class="dl-room-body">
      <div class="dl-grid">
        ${field('Value', `<input type="number" min="0" step="1000" data-field="value_amount" value="${d.value_amount ?? ''}">`)}
        ${field('Expected close', `<input type="date" data-field="expected_close" value="${d.expected_close ? String(d.expected_close).slice(0, 10) : ''}">`)}
        ${OPEN_STAGES.includes(d.stage) ? field('Forecast category', `<select data-field="forecast_category">${[['pipeline', 'Pipeline'], ['best_case', 'Best case'], ['commit', 'Commit'], ['omitted', 'Omitted']]
          .map(([k, l]) => `<option value="${k}"${k === d.forecast_category ? ' selected' : ''}>${l}</option>`).join('')}</select>`) : ''}
        ${field('Next step', `<input data-field="next_step" maxlength="500" value="${esc(d.next_step || '')}" placeholder="What happens next?">`, true)}
        ${field('Next step due', `<input type="date" data-field="next_step_due" value="${d.next_step_due ? String(d.next_step_due).slice(0, 10) : ''}">`)}
        ${OPEN_STAGES.includes(d.stage) ? field('Probability %', `<input type="number" min="0" max="100" step="5" data-field="probability" value="${d.probability ?? ''}"
          placeholder="${d.stage_probability ?? ''} (stage default)">`) : ''}
        ${field('Business line', `<select data-field="business_line_id"><option value="">— Not set —</option>${(st.meta.business_lines || []).map(b =>
          `<option value="${b.id}"${b.id === d.business_line_id ? ' selected' : ''}>${esc(b.name)}</option>`).join('')}</select>`)}
        ${field('Deal name', `<input data-field="name" maxlength="200" value="${esc(d.name)}">`, true)}
        <div class="dl-field dl-field-wide">StradIT offerings<div class="dl-chips">${offerings}</div></div>
      </div>

      ${h.score != null ? `<div class="dl-section"><h3 class="dl-h3">Deal health</h3>
        <div class="dl-health-panel">
          <div class="dl-ring ${h.level}" style="--v:${h.score}"><span>${h.score}</span></div>
          <div class="dl-health-lists">
            ${h.gaps.length ? `<strong>Needs attention</strong><ul class="gap">${h.gaps.map((g, n) => {
              const act = GAP_ACTIONS[(h.gap_codes || [])[n]];
              return `<li><span>${esc(g)}</span>${act ? ` <button type="button" class="dl-gap-fix" data-gap-action="${h.gap_codes[n]}">${esc(act.label)} →</button>` : ''}</li>`;
            }).join('')}</ul>` : ''}
            ${h.reasons.length ? `<strong>Going well</strong><ul class="ok">${h.reasons.map(g => `<li>${esc(g)}</li>`).join('')}</ul>` : ''}
            <span class="cp-muted" style="font-size:.72rem;color:var(--text-muted)">${h.days_in_stage} days in ${esc(d.stage_label)}</span>
          </div>
        </div></div>` : ''}

      <div class="dl-tabs" role="tablist">${[['toolkit', 'Stage toolkit'], ['checklist', 'Exit criteria'], ['committee', `Buying committee (${d.stakeholders.length})`], ['plan', planTabLabel(d)], ['activity', 'Activity'], ['tasks', `Tasks (${d.tasks.filter(t => !['done', 'cancelled'].includes(t.status)).length})`]]
        .map(([k, l]) => `<button type="button" class="dl-tab${st.tab === k ? ' active' : ''}" data-tab="${k}" role="tab" aria-selected="${st.tab === k}">${esc(l)}</button>`).join('')}</div>

      <div class="dl-pane${st.tab === 'toolkit' ? ' active' : ''}" role="tabpanel" id="dlToolkit">${st.tab === 'toolkit' ? renderToolkit(st.tk, d, openStages()) : ''}</div>

      <div class="dl-pane${st.tab === 'checklist' ? ' active' : ''}" role="tabpanel">
        ${byStage.map(({ s, items }) => {
          const lbl = st.meta.stages.find(x => x.key === s).label;
          const done = items.filter(c => c.done).length;
          return `<details class="dl-check-stage${s === d.stage ? ' current' : ''}"${s === d.stage ? ' open' : ''}>
            <summary><span>${esc(lbl)}</span><span>${done}/${items.length}</span></summary>
            ${items.map(c => `<label class="dl-check-item${c.done ? ' done' : ''}"><input type="checkbox" data-check="${s}|${c.item_key}"${c.done ? ' checked' : ''}>
              <span>${esc(c.label)}</span>${c.note ? `<span class="dl-check-note">${esc(c.note)}</span>` : ''}</label>`).join('')}
          </details>`;
        }).join('')}
      </div>

      <div class="dl-pane${st.tab === 'committee' ? ' active' : ''}" role="tabpanel">
        ${committeeGaps ? `<div class="dl-gap-note"><i class="fa-solid fa-triangle-exclamation"></i> Missing: ${esc(committeeGaps)}
          <a class="dl-warm-link" href="/introductions?new=1&account_id=${d.account_id}"><i class="fa-solid fa-route"></i> Who can introduce me?</a></div>` : ''}
        ${d.stakeholders.map(p => `<div class="dl-person">
            <span class="dl-avatar" ${photoAttr(p, p.persona_id)}>${esc(initials(p.name))}</span>
            <div><div class="dl-person-name"><a href="/profile?account=${p.account_id}&persona_id=${p.persona_id}" target="_blank" rel="noopener">${esc(p.name)}</a></div>
              <div class="dl-person-title">${esc(p.title || '')}</div>
              ${p.recent_move ? '<div class="dl-person-flag"><i class="fa-solid fa-user-clock"></i> Recent leadership change</div>' : ''}</div>
            <div class="dl-person-ctl">
              <select data-role="${p.persona_id}" aria-label="Role">${Object.entries(ROLE_LABEL).map(([k, l]) => `<option value="${k}"${k === p.role ? ' selected' : ''}>${l}</option>`).join('')}</select>
              <select data-sentiment="${p.persona_id}" aria-label="Sentiment"><option value="">—</option>${['positive', 'neutral', 'negative'].map(x => `<option${x === p.sentiment ? ' selected' : ''}>${x}</option>`).join('')}</select>
              <button type="button" class="dl-icon" data-remove-person="${p.persona_id}" aria-label="Remove ${esc(p.name)}"><i class="fa-regular fa-trash-can"></i></button>
            </div></div>`).join('') || '<p class="cp-muted" style="font-size:.8rem;color:var(--text-muted)">No one mapped yet. Start with the economic buyer and a champion.</p>'}
        <div class="dl-add-row">
          <input id="dlPersonSearch" placeholder="Add someone from ${esc(d.account_name)}…" aria-label="Search people to add" autocomplete="off">
          <select id="dlPersonRole" aria-label="Role for new person">${Object.entries(ROLE_LABEL).map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select>
          <div class="dl-suggest" id="dlPersonSuggest" hidden></div>
        </div>
      </div>

      <div class="dl-pane${st.tab === 'activity' ? ' active' : ''}" role="tabpanel">
        <div id="dlTimeline"></div>
        <details class="dl-changelog"><summary>Deal change log (${d.activity.length})</summary>
        <form class="dl-note-form" id="dlNoteForm"><textarea id="dlNote" maxlength="2000" placeholder="Internal note about the deal (not a customer interaction)…" aria-label="Internal note"></textarea>
          <button type="submit" class="dl-btn dl-btn-primary">Add</button></form>
        <ul class="dl-timeline">${d.activity.map(a => `<li><i class="fa-solid ${ACTIVITY_ICON[a.kind] || 'fa-circle'}"></i>
          <div>${esc(a.text)}<div class="dl-when">${esc(a.by || 'System')} · ${esc(ago(a.created_at))}</div></div></li>`).join('')}</ul>
        </details>
      </div>

      <div class="dl-pane${st.tab === 'plan' ? ' active' : ''}" role="tabpanel">
        ${st.tab === 'plan' ? roomShareBox(d) + planPane(d, PLAN_CTX) : ''}
      </div>
      <div class="dl-pane${st.tab === 'tasks' ? ' active' : ''}" role="tabpanel">
        <form class="dl-task-form" id="dlTaskForm">
          <label class="dl-task-title">New task<input id="dlTaskTitle" maxlength="500" placeholder="e.g. Send the governance one-pager" required></label>
          <label>Due<input id="dlTaskDue" type="date"></label>
          <label>Priority<select id="dlTaskPriority">${priorityOptions('medium')}</select></label>
          <label>Assignee<select id="dlTaskAssignee">${assigneeOptions(st.me && st.me.id)}</select></label>
          <div class="dl-task-submit"><button type="submit" class="dl-btn dl-btn-primary"><i class="fa-solid fa-plus"></i> Add task</button></div>
        </form>
        ${d.tasks.length ? `<ul class="dl-task-list">${d.tasks.map(taskRow).join('')}</ul>`
          : '<p class="dl-task-empty">No tasks yet. Tasks added here also appear on <a href="/tasks">My Tasks</a>.</p>'}
      </div>
    </div>
    <div class="dl-room-foot">
      <a class="dl-btn" href="${esc(copilotHref(d, ''))}" title="Ask the Sales Copilot about this deal"><i class="fa-solid fa-wand-magic-sparkles"></i> Ask Copilot</a>
      <button type="button" class="dl-btn" data-export><i class="fa-regular fa-file-excel"></i> Export</button>
      <button type="button" class="dl-btn dl-btn-danger" data-delete style="margin-left:auto"><i class="fa-regular fa-trash-can"></i> Delete</button>
    </div>`;
  if (st.tab === 'toolkit') loadToolkit();
  if (st.tab === 'activity' && $('dlTimeline')) {
    mountTimeline($('dlTimeline'), { objectType: 'deal', objectId: d.id, accountId: d.account_id, dealId: d.id,
      onChange: () => { api(`/${d.id}`).then(fresh => { if (st.deal && st.deal.id === fresh.id) { st.deal.tasks = fresh.tasks; st.deal.health = fresh.health; } loadDeals(); }).catch(() => {}); } });
  }
}

// ── Tasks ────────────────────────────────────────────────────────────────────
const isClosedTask = (t) => ['done', 'cancelled'].includes(t.status);
const priorityOptions = (sel) => PRIORITIES.map(p => `<option value="${p}"${p === sel ? ' selected' : ''}>${p[0].toUpperCase()}${p.slice(1)}</option>`).join('');

function assigneeOptions(sel) {
  const people = [...(st.users || [])];
  if (st.me && !people.some(u => u.id === st.me.id)) people.unshift({ id: st.me.id, name: st.me.full_name || st.me.email });
  return people.map(u => `<option value="${u.id}"${u.id === sel ? ' selected' : ''}>${esc(u.id === (st.me && st.me.id) ? `${u.name} (me)` : u.name)}</option>`).join('');
}

function taskRow(t) {
  const closed = isClosedTask(t);
  if (st.editTask === t.id) {
    return `<li class="dl-task" data-task="${t.id}"><span></span><form class="dl-task-edit" data-task-edit="${t.id}">
      <label class="dl-task-title">Task<input name="title" maxlength="500" value="${esc(t.title)}" required></label>
      <label>Due<input name="due_date" type="date" value="${t.due_date ? String(t.due_date).slice(0, 10) : ''}"></label>
      <label>Priority<select name="priority">${priorityOptions(t.priority)}</select></label>
      <label>Assignee<select name="assigned_to_id">${assigneeOptions(t.assigned_to_id)}</select></label>
      <footer><button type="button" class="dl-btn" data-task-cancel>Cancel</button><button type="submit" class="dl-btn dl-btn-primary">Save</button></footer>
    </form></li>`;
  }
  const overdue = t.due_date && !closed && isOverdue(t.due_date);
  return `<li class="dl-task${closed ? ' done' : ''}" data-task="${t.id}">
    <input type="checkbox" data-task-done="${t.id}"${t.status === 'done' ? ' checked' : ''} aria-label="${t.status === 'done' ? 'Reopen' : 'Complete'}: ${esc(t.title)}">
    <div><div class="dl-task-name">${esc(t.title)}</div>
      <div class="dl-task-meta">
        <span class="dl-pill ${esc(t.priority)}">${esc(t.priority)}</span>
        ${t.status === 'in_progress' || t.status === 'cancelled' ? `<span class="dl-pill">${esc(t.status.replace('_', ' '))}</span>` : ''}
        ${t.due_date ? `<span class="${overdue ? 'dl-overdue' : ''}"><i class="fa-regular fa-calendar"></i> ${overdue ? 'Overdue · ' : ''}${esc(dateLabel(t.due_date))}</span>` : ''}
        <span class="dl-task-who" title="Assignee">${t.assignee ? `<span class="dl-avatar" aria-hidden="true">${esc(initials(t.assignee))}</span>${esc(t.assignee)}` : '<i class="fa-regular fa-user"></i> Unassigned'}</span>
      </div></div>
    <div class="dl-task-actions">${closed ? '' : `<button type="button" class="dl-icon" data-task-editbtn="${t.id}" aria-label="Edit ${esc(t.title)}"><i class="fa-solid fa-pen"></i></button>`}</div>
  </li>`;
}

async function loadUsers() {
  if (st.users) return;
  try { st.users = ((await (await fetch('/api/users')).json()).users) || []; } catch { st.users = []; }
}

// Task edits go through the shared action-items API (it owns access checks and the "assigned to you" email).
async function patchTask(id, body) {
  const res = await fetch(`/api/action-items/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Couldn't update the task (${res.status})`);
  return data;
}

// Close plan module context (deals/plan.js)
const PLAN_CTX = { api, esc, dateLabel, showToast, getDeal: () => st.deal, refresh: () => refreshDeal() };
initRoomShare({ api, esc, dateLabel, showToast, getDeal: () => st.deal, rerender: () => { if (st.deal && st.tab === 'plan') renderRoom(); } });
initNba({ api, esc, showToast, getDeal: () => st.deal, runGapAction: (code) => runGapAction(code),
  openTab: (tab) => { st.tab = tab; renderRoom(); }, rerender: () => { if (st.deal) renderRoom(); } });

async function refreshDeal() {
  const fresh = await api(`/${st.deal.id}`);
  if (st.deal && st.deal.id === fresh.id) { st.deal = fresh; renderRoom(); }
  loadDeals();
}

function openStages() { return (st.meta ? st.meta.stages : []).filter(s => OPEN_STAGES.includes(s.key)); }

// Toolkit data is cached per deal + stage + last update, so edits elsewhere refresh it.
async function loadToolkit(stage) {
  const d = st.deal;
  const want = stage || (st.tk && st.tk.dealId === d.id ? st.tk.stage : null) || (OPEN_STAGES.includes(d.stage) ? d.stage : 'contract');
  const key = `${d.id}|${want}|${d.updated_at}|${d.stakeholders.length}|${d.offerings.join(',')}`;
  if (key === st.tkKey && st.tk) return;
  st.tkKey = key;
  const pane = () => $('dlToolkit');
  if (!st.tk || st.tk.dealId !== d.id || st.tk.stage !== want) {
    st.tk = null;
    if (pane()) pane().innerHTML = renderToolkit(null, d, openStages());
    const nav = pane() && pane().querySelector(`[data-tk-stage="${want}"]`);
    if (nav) nav.classList.add('active');
  }
  try {
    const res = await api(`/${d.id}/toolkit?stage=${want}`);
    if (!st.deal || st.deal.id !== d.id || st.tkKey !== key) return;
    st.tk = { ...res, dealId: d.id };
  } catch (err) {
    st.tk = { stage: want, dealId: d.id, error: err.message };
  }
  if (pane() && st.tab === 'toolkit') pane().innerHTML = renderToolkit(st.tk, st.deal, openStages());
}

let medTimer = null;
async function saveQualification(key, value) {
  try {
    const res = await api(`/${st.deal.id}/qualification`, { method: 'PATCH', body: { values: { [key]: value } } });
    st.deal.qualification = res.qualification;
    const box = $('dlToolkit') && $('dlToolkit').querySelector(`[data-med="${key}"]`);
    if (box) box.closest('.tk-med').classList.toggle('filled', !!value);
    clearTimeout(medTimer);
    medTimer = setTimeout(() => showToast('MEDDICC saved'), 150);
  } catch (err) { showToast(err.message); }
}

async function openDeal(id) {
  resetPlanEditing();
  try {
    st.deal = await api(`/${id}`);
    $('dlOverlay').hidden = false;
    $('dlRoom').hidden = false;
    renderRoom();
    const url = new URL(location.href); url.searchParams.set('deal', id); history.replaceState(null, '', url);
    $('dlRoom').querySelector('[data-close-room]').focus();
  } catch (err) { showToast(err.message); }
}

function closeRoom() {
  $('dlOverlay').hidden = true;
  $('dlRoom').hidden = true;
  st.deal = null;
  const url = new URL(location.href); url.searchParams.delete('deal'); history.replaceState(null, '', url);
}

async function patchDeal(body) {
  const from = st.deal.stage;
  try {
    st.deal = await api(`/${st.deal.id}`, { method: 'PATCH', body });
    if (body.stage && st.deal.stage !== from) stageMoved(st.deal, from);
    else (st.deal.warnings || []).forEach(w => showToast(w));
    renderRoom();
    loadDeals();
  } catch (err) { showToast(err.message); }
}

let suggestTimer = null;
async function suggestPeople(q) {
  const box = $('dlPersonSuggest');
  try {
    const people = await api(`/${st.deal.id}/candidates?q=${encodeURIComponent(q)}`);
    box.innerHTML = people.map(p => `<button type="button" data-add-person="${p.id}">${esc(p.name)}
      <small>${esc(p.title || '')}${p.decision_authority ? ` · ${esc(p.decision_authority)}` : ''}</small></button>`).join('')
      || '<button type="button" disabled>No matches at this account</button>';
    box.hidden = false;
  } catch { box.hidden = true; }
}

function wireRoom() {
  const room = $('dlRoom');
  room.addEventListener('click', async (e) => {
    const t = e.target;
    if (t.closest('[data-close-room]')) { closeRoom(); return; }
    const step = t.closest('[data-stage-step]');
    if (step && step.dataset.stageStep !== st.deal.stage) {
      if (!step.disabled) moveToStage(step.dataset.stageStep);
      return;
    }
    const adv = t.closest('[data-advance]');
    if (adv) { moveToStage(adv.dataset.advance); return; }
    const fix = t.closest('[data-gap-action]');
    if (fix) { runGapAction(fix.dataset.gapAction); return; }
    if (t.closest('[data-goto-offerings]')) {
      const chips = $('dlRoom').querySelector('.dl-chips');
      if (chips) { chips.scrollIntoView({ behavior: 'smooth', block: 'center' }); const first = chips.querySelector('button'); if (first) first.focus(); }
      return;
    }
    const close = t.closest('[data-close-deal]');
    if (close) {
      if (close.dataset.closeDeal === 'lost') {
        const lostBody = await askLost();
        if (lostBody) patchDeal({ stage: 'lost', ...lostBody });
      } else { patchDeal({ stage: 'won' }); }
      return;
    }
    const chip = t.closest('[data-offering]');
    if (chip) {
      const k = chip.dataset.offering;
      const next = st.deal.offerings.includes(k) ? st.deal.offerings.filter(x => x !== k) : [...st.deal.offerings, k];
      patchDeal({ offerings: next });
      return;
    }
    if (await nbaClick(e)) return;
    if (st.tab === 'plan' && (await roomShareClick(e) || await planClick(e))) return;
    const tab = t.closest('[data-tab]');
    if (tab) {
      const fromBox = !!tab.closest('.dl-stage-box');
      st.tab = tab.dataset.tab; renderRoom();
      if (fromBox) { const tabs = $('dlRoom').querySelector('.dl-tabs'); if (tabs) tabs.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
      return;
    }
    const tkStage = t.closest('[data-tk-stage]');
    if (tkStage) { loadToolkit(tkStage.dataset.tkStage); return; }
    const tkAdd = t.closest('[data-tk-add]');
    if (tkAdd) {
      try {
        st.deal = await api(`/${st.deal.id}/stakeholders`, { method: 'POST', body: { persona_id: Number(tkAdd.dataset.tkAdd), role: 'influencer' } });
        showToast('Added as influencer — set the role in Buying committee'); renderRoom(); loadDeals();
      } catch (err) { showToast(err.message); }
      return;
    }
    const tkCheck = t.closest('[data-tk-check]');
    if (tkCheck) {
      const [stage, key] = tkCheck.dataset.tkCheck.split('|');
      try { st.deal = await api(`/${st.deal.id}/checklist/${stage}/${key}`, { method: 'PATCH', body: { done: true } }); showToast('Exit criterion ticked'); renderRoom(); loadDeals(); }
      catch (err) { showToast(err.message); }
      return;
    }
    const medUse = t.closest('[data-med-use]');
    if (medUse) {
      const box = $('dlToolkit').querySelector(`[data-med="${medUse.dataset.medUse}"]`);
      box.value = medUse.dataset.medText; medUse.remove();
      saveQualification(medUse.dataset.medUse, box.value);
      return;
    }
    const rm = t.closest('[data-remove-person]');
    if (rm) {
      try { st.deal = await api(`/${st.deal.id}/stakeholders/${rm.dataset.removePerson}`, { method: 'DELETE' }); renderRoom(); loadDeals(); }
      catch (err) { showToast(err.message); }
      return;
    }
    const add = t.closest('[data-add-person]');
    if (add) {
      try {
        st.deal = await api(`/${st.deal.id}/stakeholders`, { method: 'POST', body: { persona_id: Number(add.dataset.addPerson), role: $('dlPersonRole').value } });
        renderRoom(); loadDeals(); $('dlPersonSearch') && $('dlPersonSearch').focus();
      } catch (err) { showToast(err.message); }
      return;
    }
    if (t.closest('[data-export]')) { downloadFile(`/api/deals/${st.deal.id}/export`, 'deal.xlsx').catch(err => showToast(err.message)); return; }
    const editBtn = t.closest('[data-task-editbtn]');
    if (editBtn) {
      await loadUsers();
      st.editTask = Number(editBtn.dataset.taskEditbtn); renderRoom();
      const input = room.querySelector('[data-task-edit] input[name="title"]');
      if (input) input.focus();
      return;
    }
    if (t.closest('[data-task-cancel]')) { st.editTask = null; renderRoom(); return; }
    if (t.closest('[data-delete]')) {
      if (!await confirmDelete(st.deal)) return;
      try { await api(`/${st.deal.id}`, { method: 'DELETE' }); closeRoom(); loadDeals(); showToast('Deal deleted'); }
      catch (err) { showToast(err.message); }
    }
  });
  room.addEventListener('change', async (e) => {
    const t = e.target;
    if (await planChange(e)) return;
    if (t.dataset.taskDone) {
      t.disabled = true;
      try {
        await patchTask(Number(t.dataset.taskDone), { status: t.checked ? 'done' : 'open' });
        showToast(t.checked ? 'Task completed' : 'Task reopened');
        await refreshDeal();
      } catch (err) { showToast(err.message); t.checked = !t.checked; t.disabled = false; }
      return;
    }
    if (t.closest('[data-task-edit]')) return;
    if (t.dataset.med) { saveQualification(t.dataset.med, t.value.trim()); return; }
    if (t.dataset.field) {
      const v = (t.type === 'number' || t.dataset.field === 'business_line_id') ? (t.value === '' ? null : Number(t.value)) : (t.value || null);
      if (t.dataset.field === 'forecast_category' && !v) return;
      if (t.dataset.field === 'name' && !v) { t.value = st.deal.name; return; }
      patchDeal({ [t.dataset.field]: v });
    } else if (t.dataset.check) {
      const [stage, key] = t.dataset.check.split('|');
      try { st.deal = await api(`/${st.deal.id}/checklist/${stage}/${key}`, { method: 'PATCH', body: { done: t.checked } }); renderRoom(); loadDeals(); }
      catch (err) { showToast(err.message); t.checked = !t.checked; }
    } else if (t.dataset.role || t.dataset.sentiment) {
      const pid = Number(t.dataset.role || t.dataset.sentiment);
      const p = st.deal.stakeholders.find(x => x.persona_id === pid);
      const body = { persona_id: pid, role: t.dataset.role ? t.value : p.role, sentiment: t.dataset.sentiment ? (t.value || null) : p.sentiment };
      try { st.deal = await api(`/${st.deal.id}/stakeholders`, { method: 'POST', body }); renderRoom(); loadDeals(); }
      catch (err) { showToast(err.message); }
    }
  });
  room.addEventListener('input', (e) => {
    nbaInput(e);
    if (e.target.id !== 'dlPersonSearch') return;
    clearTimeout(suggestTimer);
    const q = e.target.value.trim();
    suggestTimer = setTimeout(() => suggestPeople(q), 200);
  });
  room.addEventListener('focusin', (e) => { if (e.target.id === 'dlPersonSearch') suggestPeople(e.target.value.trim()); });
  room.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (await roomShareSubmit(e) || await planSubmit(e)) return;
    if (e.target.id === 'dlNoteForm') {
      const text = $('dlNote').value.trim();
      if (!text) return;
      try { st.deal = await api(`/${st.deal.id}/notes`, { method: 'POST', body: { text } }); renderRoom(); loadDeals(); }
      catch (err) { showToast(err.message); }
    } else if (e.target.id === 'dlTaskForm') {
      const body = { title: $('dlTaskTitle').value.trim(), due_date: $('dlTaskDue').value || null, priority: $('dlTaskPriority').value };
      if (!body.title) { $('dlTaskTitle').focus(); return; }
      const assignee = Number($('dlTaskAssignee').value) || null;
      try {
        const out = await api(`/${st.deal.id}/tasks`, { method: 'POST', body });
        st.deal = out;
        if (assignee && st.me && assignee !== st.me.id && out.task_id) {
          try { await patchTask(out.task_id, { assigned_to_id: assignee }); st.deal = await api(`/${st.deal.id}`); }
          catch (err) { showToast(`Task added, but not reassigned: ${err.message}`); }
        }
        renderRoom(); loadDeals(); showToast('Task added — also on My Tasks');
      } catch (err) { showToast(err.message); }
    } else if (e.target.matches('[data-task-edit]')) {
      const f = e.target;
      const id = Number(f.dataset.taskEdit);
      const cur = st.deal.tasks.find(x => x.id === id);
      const title = f.elements.title.value.trim();
      if (!title) { f.elements.title.focus(); return; }
      const body = {};
      if (title !== cur.title) body.title = title;
      if (f.elements.priority.value !== cur.priority) body.priority = f.elements.priority.value;
      const due = f.elements.due_date.value;
      if (due !== String(cur.due_date || '').slice(0, 10)) body.due_date = due || null;   // empty clears it
      const who = Number(f.elements.assigned_to_id.value) || null;
      if (who && who !== cur.assigned_to_id) body.assigned_to_id = who;
      try {
        if (Object.keys(body).length) await patchTask(id, body);
        st.editTask = null;
        await refreshDeal();
        if (Object.keys(body).length) showToast('Task updated');
      } catch (err) { showToast(err.message); }
    }
  });
  document.addEventListener('click', (e) => {
    const box = $('dlPersonSuggest');
    if (box && !e.target.closest('.dl-add-row')) box.hidden = true;
  });
}

// "Why was it lost?" — a pick-list (so losses can be analysed) + competitor + note
function lostText(d) {
  if (d.stage !== 'lost') return '';
  const cat = d.lost_category_label ? d.lost_category_label + (d.lost_competitor ? `: ${d.lost_competitor}` : '') : '';
  return [cat, d.lost_reason].filter(Boolean).join(' — ');
}

function askLost() {
  return new Promise((resolve) => {
    const foot = $('dlRoom').querySelector('.dl-close-btns');
    const cats = Object.entries(st.lostCategories || {});
    if (!foot || !cats.length) { resolve(null); return; }
    foot.classList.add('dl-lost-open');
    foot.innerHTML = `<fieldset class="dl-lost-form">
        <legend>Why was it lost?</legend>
        <div class="dl-lost-cats" role="radiogroup" aria-label="Loss reason">${cats.map(([k, label]) =>
          `<label class="dl-lost-cat"><input type="radio" name="dlLostCat" value="${esc(k)}"><span>${esc(label)}</span></label>`).join('')}</div>
        <input id="dlLostCompetitor" maxlength="120" placeholder="Which competitor won? (optional)" aria-label="Competitor that won" hidden>
        <input id="dlLostReason" maxlength="500" placeholder="Note — what happened? (optional)" aria-label="Note about the loss">
        <div class="dl-lost-btns"><button type="button" class="dl-btn dl-btn-danger" id="dlLostGo">Mark lost</button>
          <button type="button" class="dl-btn" id="dlLostCancel">Cancel</button></div>
      </fieldset>`;
    const note = $('dlLostReason');
    const comp = $('dlLostCompetitor');
    const picked = () => (foot.querySelector('input[name="dlLostCat"]:checked') || {}).value;
    foot.querySelector('.dl-lost-cats input').focus();
    foot.querySelectorAll('input[name="dlLostCat"]').forEach((r) => r.addEventListener('change', () => {
      comp.hidden = picked() !== 'competitor';
      note.placeholder = picked() === 'other' ? 'Note — what happened? (required for Other)' : 'Note — what happened? (optional)';
      (picked() === 'competitor' ? comp : note).focus();
    }));
    const done = (v) => { foot.classList.remove('dl-lost-open'); resolve(v); };
    $('dlLostGo').onclick = () => {
      const cat = picked();
      if (!cat) { showToast('Pick a reason first'); foot.querySelector('.dl-lost-cats input').focus(); return; }
      if (cat === 'other' && !note.value.trim()) { showToast('Add a short note for Other'); note.focus(); return; }
      done({ lost_category: cat, lost_competitor: cat === 'competitor' ? (comp.value.trim() || null) : null,
             lost_reason: note.value.trim() || null });
    };
    $('dlLostCancel').onclick = () => { renderRoom(); done(null); };
    foot.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); $('dlLostCancel').click(); }
      if (e.key === 'Enter' && e.target.tagName === 'INPUT' && e.target.type !== 'radio') { e.preventDefault(); $('dlLostGo').click(); }
    });
  });
}

// Small inline prompts instead of window.prompt/confirm (keyboard- and screen-reader friendly)
function askReason(placeholder, okLabel, isDanger = true) {
  return new Promise((resolve) => {
    const foot = $('dlRoom').querySelector('.dl-close-btns');
    if (!foot) { resolve(null); return; }
    foot.innerHTML = `<input id="dlLostReason" maxlength="500" placeholder="${esc(placeholder)}" style="flex:1;min-width:180px" aria-label="${esc(placeholder)}">
      <button type="button" class="dl-btn ${isDanger ? 'dl-btn-danger' : 'dl-btn-primary'}" id="dlLostGo">${esc(okLabel)}</button><button type="button" class="dl-btn" id="dlLostCancel">Cancel</button>`;
    const input = $('dlLostReason');
    input.focus();
    $('dlLostGo').onclick = () => { const v = input.value.trim(); if (!v) { showToast('Please add a reason'); input.focus(); return; } resolve(v); };
    $('dlLostCancel').onclick = () => { renderRoom(); resolve(null); };
    input.onkeydown = (e) => {
      if (e.key === 'Enter') { e.preventDefault(); $('dlLostGo').click(); }
      if (e.key === 'Escape') { e.stopPropagation(); $('dlLostCancel').click(); }
    };
  });
}

// A real dialog, focus on Cancel, and Delete armed only after a beat — so a double-click on the
// drawer's Delete button can't go straight through.
function confirmDelete(d) {
  return new Promise((resolve) => {
    const wrap = $('dlConfirm');
    const yes = $('dlConfirmYes');
    const no = $('dlConfirmNo');
    const opener = document.activeElement;
    $('dlConfirmText').innerHTML = `<strong>${esc(d.name)}</strong> at ${esc(d.account_name)}${d.value_amount != null ? ` · ${esc(money(d.value_amount, d.currency))}` : ''}`;
    yes.disabled = true;
    wrap.hidden = false;
    no.focus();
    const arm = setTimeout(() => { yes.disabled = false; }, 600);
    const done = (ok) => {
      clearTimeout(arm);
      wrap.hidden = true;
      yes.onclick = null; no.onclick = null;
      wrap.removeEventListener('keydown', onKey, true);
      if (!ok && opener && opener.focus) opener.focus();
      resolve(ok);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(false); }
      if (e.key === 'Tab') { e.preventDefault(); (document.activeElement === no && !yes.disabled ? yes : no).focus(); }
    };
    wrap.addEventListener('keydown', onKey, true);
    yes.onclick = () => done(true);
    no.onclick = () => done(false);
  });
}

function wireBack() {
  // Go back to wherever the user came from in this app; a direct visit or sibling CRM jump falls back to Accounts.
  $('dlBack').addEventListener('click', (e) => {
    let internal = false;
    let isCrmSibling = false;
    try {
      const ref = document.referrer ? new URL(document.referrer) : null;
      internal = !!ref && ref.origin === location.origin && ref.pathname !== location.pathname && !ref.pathname.startsWith('/login');
      if (ref && ['/email-sync', '/forecast', '/introductions'].includes(ref.pathname)) {
        isCrmSibling = true;
      }
    } catch { /* ignore */ }
    if (internal && !isCrmSibling && history.length > 1) {
      e.preventDefault();
      history.back();
    } else {
      e.preventDefault();
      location.assign('/accounts');
    }
  });
}

// ── Drag & drop + keyboard ───────────────────────────────────────────────────
function wireBoard() {
  const board = $('dlBoard');
  document.addEventListener('dragstart', (e) => {
    const c = e.target.closest && e.target.closest('.dl-card');
    if (!c) return;
    st.dragId = Number(c.dataset.deal);
    c.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', c.dataset.deal);
  });
  document.addEventListener('dragend', (e) => {
    const c = e.target.closest && e.target.closest('.dl-card');
    if (c) c.classList.remove('dragging');
    board.querySelectorAll('.drop-hover').forEach(x => x.classList.remove('drop-hover'));
  });
  board.addEventListener('dragover', (e) => {
    const col = e.target.closest('.dl-col');
    if (!col) return;
    e.preventDefault();
    board.querySelectorAll('.drop-hover').forEach(x => x !== col && x.classList.remove('drop-hover'));
    col.classList.add('drop-hover');
  });
  board.addEventListener('drop', (e) => {
    const col = e.target.closest('.dl-col');
    if (!col || !st.dragId) return;
    e.preventDefault();
    col.classList.remove('drop-hover');
    const d = st.deals.find(x => x.id === st.dragId);
    const id = st.dragId;
    st.dragId = null;
    if (d && d.stage !== col.dataset.stage) moveToStage(col.dataset.stage, id);
  });
  document.addEventListener('click', (e) => {
    const c = e.target.closest('.dl-card');
    if (c) openDeal(Number(c.dataset.deal));
  });
  document.addEventListener('keydown', (e) => {
    const c = e.target.closest && e.target.closest('.dl-card');
    if (c && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); openDeal(Number(c.dataset.deal)); }
    // Alt+←/→ moves the focused card between stages (keyboard alternative to drag-and-drop)
    if (c && e.altKey && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
      const d = st.deals.find(x => x.id === Number(c.dataset.deal));
      const i = OPEN_STAGES.indexOf(d.stage) + (e.key === 'ArrowRight' ? 1 : -1);
      if (i >= 0 && i < OPEN_STAGES.length) { e.preventDefault(); moveToStage(OPEN_STAGES[i], d.id); }
    }
    if (e.key === 'Escape') {
      if (!$('dlModal').hidden) {
        if (st.newDirty) showToast('Unsaved deal — use Cancel to discard it');
        else closeNewDeal();
      } else if (!$('dlRoom').hidden) closeRoom();
    }
  });
  $('dlOverlay').addEventListener('click', closeRoom);
}

// ── New deal ─────────────────────────────────────────────────────────────────
async function ensureLookups() {
  if (!st.meta) st.meta = await api('/meta');
  if (!st.accounts.length) {
    const res = await fetch('/api/copilot/context');
    if (!res.ok) throw new Error(`Couldn't load accounts (${res.status})`);
    st.accounts = (await res.json()).accounts || [];
  }
}

async function openNewDeal() {
  try {
    await ensureLookups();
  } catch (err) { showToast(err.message); return; }
  if (!st.accounts.length) { showToast("You don't have access to any accounts yet — ask an admin to grant one."); return; }
  $('dlNewAccount').innerHTML = st.accounts.map(a => `<option value="${a.id}"${String(a.id) === $('dlAccount').value ? ' selected' : ''}>${esc(a.name)}</option>`).join('');
  $('dlNewOfferings').innerHTML = st.meta.offerings.map(o => `<button type="button" class="dl-chip" data-new-offering="${o.key}" aria-pressed="false">${esc(o.label)}</button>`).join('');
  $('dlNewStage').innerHTML = st.meta.stages.filter(s => OPEN_STAGES.includes(s.key)).map(s => `<option value="${s.key}">${esc(s.label)}</option>`).join('');
  $('dlNewForm').reset();
  $('dlNewBusinessLine').innerHTML = '<option value="">— Not set —</option>'
    + (st.meta.business_lines || []).map(b => `<option value="${b.id}"${String(b.id) === $('dlBusinessLine').value ? ' selected' : ''}>${esc(b.name)}</option>`).join('');
  st.newDirty = false;
  $('dlModal').hidden = false;
  $('dlNewName').focus();
}

function closeNewDeal() {
  $('dlModal').hidden = true;
  st.newDirty = false;
}

function wireNewDeal() {
  $('dlNew').addEventListener('click', openNewDeal);
  // Only the Close / Cancel buttons dismiss the form — a stray click outside must not lose the input.
  $('dlModal').addEventListener('click', (e) => {
    if (e.target.closest('[data-close-modal]')) closeNewDeal();
    const chip = e.target.closest('[data-new-offering]');
    if (chip) {
      chip.classList.toggle('on');
      chip.setAttribute('aria-pressed', chip.classList.contains('on'));
      st.newDirty = true;
    }
  });
  $('dlNewForm').addEventListener('input', () => { st.newDirty = true; });
  $('dlNewForm').addEventListener('change', () => { st.newDirty = true; });
  $('dlNewForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      account_id: Number($('dlNewAccount').value), name: $('dlNewName').value.trim(),
      value_amount: $('dlNewValue').value ? Number($('dlNewValue').value) : null, currency: $('dlNewCurrency').value,
      expected_close: $('dlNewClose').value || null, next_step: $('dlNewNext').value.trim() || null,
      next_step_due: $('dlNewNextDue').value || null, stage: $('dlNewStage').value,
      business_line_id: $('dlNewBusinessLine').value ? Number($('dlNewBusinessLine').value) : null,
      offerings: [...$('dlNewOfferings').querySelectorAll('.dl-chip.on')].map(c => c.dataset.newOffering),
    };
    try {
      const d = await api('', { method: 'POST', body });
      closeNewDeal();
      showToast('Deal created — add the buying committee next');
      await loadDeals();
      st.tab = 'committee';
      openDeal(d.id);
    } catch (err) { showToast(err.message); }
  });
}

// ── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  try {
    await ensureLookups();
    $('dlAccount').innerHTML = '<option value="">All accounts</option>' + st.accounts.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
  } catch (err) { showToast(err.message); }
  const params = new URLSearchParams(location.search);
  if (params.get('account_id')) $('dlAccount').value = params.get('account_id');
  let t = null;
  $('dlSearch').addEventListener('input', () => { filtersChanged(); clearTimeout(t); t = setTimeout(loadDeals, 250); });
  $('dlAccount').addEventListener('change', () => { filtersChanged(); loadDeals(); });
  $('dlBusinessLine').innerHTML = '<option value="">All business lines</option>'
    + ((st.meta && st.meta.business_lines) || []).map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
  if (params.get('business_line_id')) $('dlBusinessLine').value = params.get('business_line_id');
  $('dlBusinessLine').addEventListener('change', () => { filtersChanged(); loadDeals(); });
  $('dlMine').addEventListener('change', () => { filtersChanged(); loadDeals(); });
  $('dlAttention').addEventListener('change', () => { filtersChanged(); renderBoard(); });
  $('dlDigest').addEventListener('click', (e) => {
    const b = e.target.closest('[data-deal-open]');
    if (b) openDeal(Number(b.dataset.dealOpen));
  });
  wireBack();
  wireBoard();
  wireRoom();
  wireNewDeal();
  loadUsers().then(() => { if (st.deal && st.tab === 'tasks') renderRoom(); });
  initViews({ st, $, api, esc, money, dateLabel, isOverdue, healthBadge, needsAttention, lostText, openDeal,
    showToast, OPEN_STAGES, reload: loadDeals });
  await loadDeals();
  if (params.get('deal')) openDeal(Number(params.get('deal')));
  if (params.get('new') === '1') openNewDeal();
}

initThemeToggle();
initTopbarAuth().then((user) => {
  if (!user) { document.body.style.display = 'none'; location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); return; }
  st.me = user;
  init();
});
