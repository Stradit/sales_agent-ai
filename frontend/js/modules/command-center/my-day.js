// Command Center · My Day — the action-first block at the top of /command-center: what needs the rep today,
// the quarter, deals needing attention and reasons to reach out. Data: GET /api/command-center/my-day
// (services/my_day_service.py), loaded through deal-context.js so the cards below can reuse it.
// Row buttons resolve the item in place: tasks (done / snooze), deal next steps (reschedule / set), quiet deals
// (log a call), stale introductions (mark declined) — each calls the existing endpoint, then My Day reloads.
import { showToast } from '../toast.js';
import { getCurrentUser } from '../auth-client.js';
import { esc, relativeTime } from './utils.js';
import { loadMyDay } from './deal-context.js';

const SHOWN = 6;                      // rows of "Do today" before "Show N more"
const FILTERS = [['all', 'All'], ['deal', 'Deals'], ['task', 'Tasks'], ['intro', 'Intros']];
const NEEDS_FORM = new Set(['reschedule', 'set_step', 'log_call', 'decline']);
let data = null;
let filter = 'all';
let expanded = false;
let editing = null;                   // "<row key>:<op>" of the row showing an inline form

const money = (v) => {
  if (v == null) return '—';
  if (v >= 1e6) return `$${(v / 1e6).toFixed(v >= 1e7 ? 0 : 2).replace(/\.?0+$/, '')}M`;
  if (v >= 1e3) return `$${Math.round(v / 1e3)}k`;
  return `$${Math.round(v)}`;
};
const group = (q) => (q.kind.startsWith('deal') ? 'deal' : q.kind);
const healthTone = (h) => (h == null ? 'muted' : h < 40 ? 'risk' : h < 70 ? 'watch' : 'good');
const rowKey = (q) => `${q.kind}-${q.task_id || q.intro_id || q.deal_id}`;
const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const inDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return isoDay(d); };

function greeting() {
  const h = new Date().getHours();
  const part = h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
  const u = getCurrentUser();
  const first = u && (u.full_name || '').trim().split(/\s+/)[0];
  return first ? `${part}, ${esc(first)}` : part;
}

function tile(label, value, sub, tone, href) {
  return `<a class="cc-md-tile tone-${tone}" href="${href}"><span class="cc-md-tile-label">${label}</span>
    <strong>${value}</strong><span class="cc-md-tile-sub">${sub}</span></a>`;
}

function actionButton(a, q) {
  const cls = `cc-md-act${a.primary ? ' primary' : ''}`;
  const op = a.task_action || a.deal_action || a.intro_action;
  if (op) return `<button type="button" class="${cls}" data-md-op="${op}" data-md-row="${esc(rowKey(q))}">${esc(a.label)}</button>`;
  const ext = /^(mailto:|https?:)/.test(a.href || '') ? ' target="_blank" rel="noopener"' : '';
  return `<a class="${cls}" href="${esc(a.href)}"${ext}>${esc(a.label)}</a>`;
}

// Inline forms for the actions that need a value before they can be saved
function inlineForm(q, op) {
  const key = esc(rowKey(q));
  const buttons = (label) => `<button type="submit" class="cc-md-act primary">${label}</button>
    <button type="button" class="cc-md-act" data-md-cancel>Cancel</button>`;
  const form = (fields, label) => `<form class="cc-md-form" data-md-form="${op}" data-md-row="${key}">${fields}${buttons(label)}</form>`;
  if (op === 'reschedule') {
    return form(`<label>New date <input type="date" name="due" value="${inDays(3)}" min="${inDays(0)}" required></label>`, 'Save');
  }
  if (op === 'set_step') {
    return form(`<label class="grow">Next step <input type="text" name="step" maxlength="500" placeholder="e.g. Book the CIO readout" required></label>
      <label>Due <input type="date" name="due" value="${inDays(7)}" min="${inDays(0)}" required></label>`, 'Save');
  }
  if (op === 'log_call') {
    return form('<label class="grow">What happened? <input type="text" name="note" maxlength="300" placeholder="e.g. Spoke to the sponsor, readout moved to next week" required></label>', 'Log call');
  }
  if (op === 'decline') {
    return form('<label class="grow">Why? <input type="text" name="reason" maxlength="500" value="No reply from the connector" required></label>', 'Mark declined');
  }
  return '';
}

function queueRow(q) {
  const pilotBadge = q.pilot_exit_text ? `<span class="cc-md-pilot-badge" title="Pilot exit criteria progress"><i class="fa-solid fa-list-check"></i> ${esc(q.pilot_exit_text)}</span>` : '';
  const ctx = [q.deal ? esc(q.deal) : esc(q.account || ''), q.stage ? esc(q.stage) : '', q.value ? money(q.value) : '', q.extra ? esc(q.extra) : '']
    .filter(Boolean).join(' · ');
  const open = editing && editing.startsWith(`${rowKey(q)}:`);
  return `<li class="cc-md-item tone-${q.tone}${open ? ' is-editing' : ''}">
    <div class="cc-md-item-main"><div class="cc-md-item-title">${esc(q.title)}${pilotBadge}</div>
      <div class="cc-md-item-sub"><strong>${esc(q.why)}</strong>${ctx ? ` · ${ctx}` : ''}</div></div>
    <div class="cc-md-item-acts">${open ? inlineForm(q, editing.split(':')[1]) : (q.actions || []).slice().reverse().map(a => actionButton(a, q)).join('')}</div>
  </li>`;
}

function headMeta() {
  const day = new Date(`${data.today}T12:00:00`).toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
  const parts = [day, `${data.account_count} account${data.account_count === 1 ? '' : 's'}`,
    data.scope === 'mine' ? 'your deals, tasks and introductions' : 'team view — everyone on your accounts'];
  if (data.refreshed_at) parts.push(`signals refreshed ${relativeTime(new Date(data.refreshed_at))}`);
  return parts.map(esc).join(' · ');
}

function render() {
  const el = document.getElementById('ccMyDay');
  if (!el || !data) return;
  const h = data.headline;
  const items = data.today_items.filter(q => filter === 'all' || group(q) === filter);
  const counts = Object.fromEntries(FILTERS.map(([k]) => [k, k === 'all' ? data.today_items.length : data.today_items.filter(q => group(q) === k).length]));
  const shown = expanded ? items : items.slice(0, SHOWN);
  const q = data.quarter;
  const scale = q ? Math.max(q.target || 0, q.best_case_forecast || 0, 1) : 1;
  const pct = (v) => `${Math.max(0, Math.min(100, ((v || 0) / scale) * 100))}%`;
  const fresh = data.reasons_window_days === 7;

  const meta = document.getElementById('ccMdMeta');
  const title = document.getElementById('ccMdTitle');
  if (meta) meta.innerHTML = headMeta();
  if (title) title.innerHTML = `${greeting()} — ${data.today_total ? `${data.today_total} thing${data.today_total === 1 ? '' : 's'} need${data.today_total === 1 ? 's' : ''} you today` : 'nothing urgent today'}`;
  el.setAttribute('aria-busy', 'false');
  el.innerHTML = `
    <div class="cc-md-tiles">
      ${tile('Overdue', h.overdue, `${h.overdue_steps} deal next step${h.overdue_steps === 1 ? '' : 's'} · ${h.overdue_tasks} task${h.overdue_tasks === 1 ? '' : 's'}`, h.overdue ? 'red' : 'ok', '#ccMdQueue')}
      ${tile('Deals at risk', `${h.at_risk_count} <small>of ${h.open_count}</small>`, `${money(h.at_risk_value)} of ${money(h.open_value)} open pipeline`, h.at_risk_count ? 'amber' : 'ok', '#ccMdAttention')}
      ${tile('Gone quiet', money(h.quiet_value), `${h.quiet_count} late-stage deal${h.quiet_count === 1 ? '' : 's'}, no contact in 14+ days`, h.quiet_count ? 'navy' : 'ok', '#ccMdQueue')}
      ${tile('Reasons to call', data.reasons.length, `${fresh ? 'new signals' : `signals in the last ${data.reasons_window_days} days`} on accounts with open deals`, 'brand', '#ccMdReasons')}
    </div>
    <div class="cc-md-grid">
      <section class="cc-md-card" id="ccMdQueue" aria-label="Do today">
        <div class="cc-md-card-head"><h3>Do today <span>· most urgent first</span></h3>
          <div class="cc-md-filters" role="tablist" aria-label="Filter">${FILTERS.map(([k, l]) =>
            `<button type="button" role="tab" data-md-filter="${k}" aria-selected="${filter === k}">${l} ${counts[k]}</button>`).join('')}</div></div>
        ${shown.length ? `<ol class="cc-md-list">${shown.map(queueRow).join('')}</ol>`
          : `<p class="cc-md-empty">${filter === 'all' ? 'Nothing overdue or waiting on you. Nice.' : 'Nothing here right now.'}</p>`}
        ${items.length > SHOWN ? `<button type="button" class="cc-md-more" data-md-more>${expanded ? 'Show less' : `Show ${items.length - SHOWN} more`}</button>` : ''}
      </section>
      <div class="cc-md-side">
        ${q ? `<section class="cc-md-card cc-md-quarter" aria-label="My quarter">
          <div class="cc-md-card-head"><h3>${data.scope === 'mine' ? 'My' : 'Team'} quarter <span>· ${esc(q.period)}</span></h3><a href="/forecast">Forecast</a></div>
          <div class="cc-md-q-row"><span>Won</span><strong>${money(q.won)}</strong></div>
          <div class="cc-md-bar" role="img" aria-label="Won ${money(q.won)}, commit forecast ${money(q.commit_forecast)}, best case ${money(q.best_case_forecast)}${q.target ? `, target ${money(q.target)}` : ''}">
            <span class="won" style="width:${pct(q.won)}"></span><span class="commit" style="width:calc(${pct(q.commit_forecast)} - ${pct(q.won)})"></span>
            <span class="best" style="width:calc(${pct(q.best_case_forecast)} - ${pct(q.commit_forecast)})"></span>
            ${q.target ? `<i style="left:${pct(q.target)}"></i>` : ''}</div>
          <div class="cc-md-legend"><span class="won">■ Won</span><span class="commit">■ Commit ${money(q.commit_forecast)}</span>
            <span class="best">■ Best case ${money(q.best_case_forecast)}</span>${q.target ? `<span class="target">| Target ${money(q.target)}</span>` : '<a href="/forecast">Set a target in Forecast</a>'}</div>
        </section>` : ''}
        <section class="cc-md-card" id="ccMdAttention" aria-label="Deals needing attention">
          <div class="cc-md-card-head"><h3>Deals needing attention</h3><a href="/deals">All deals</a></div>
          ${data.attention.length ? data.attention.map(d => `<a class="cc-md-deal" href="/deals?deal=${d.deal_id}">
            <span class="cc-md-health ${healthTone(d.health)}" title="Deal health">${d.health ?? '—'}</span>
            <span class="cc-md-deal-main"><strong>${esc(d.deal)}</strong><small>${esc(d.why || d.account)}${d.pilot_exit_text ? ` · <span class="cc-md-pilot-badge-sm"><i class="fa-solid fa-list-check"></i> ${esc(d.pilot_exit_text)}</span>` : ''}</small></span>
            <strong class="cc-md-deal-val">${money(d.value)}</strong></a>`).join('') : '<p class="cc-md-empty">No open deals.</p>'}
        </section>
        <section class="cc-md-card" id="ccMdReasons" aria-label="Reasons to reach out">
          <div class="cc-md-card-head"><h3>Reasons to reach out${fresh ? '' : ` <span>· last ${data.reasons_window_days} days</span>`}</h3></div>
          ${data.reasons.length ? data.reasons.map(r => `<div class="cc-md-reason">
            <span class="cc-md-reason-tag">${esc(r.category || r.kind || 'Signal')} · ${esc(r.account || '')}</span>
            ${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title)}</a>` : `<span>${esc(r.title)}</span>`}
            <small>Affects ${esc(r.deals.slice(0, 2).join(', '))}${r.deals.length > 2 ? ` +${r.deals.length - 2}` : ''}</small></div>`).join('')
            : `<p class="cc-md-empty">No signals in the last ${data.reasons_window_days} days on accounts with open deals. Signals appear here after the sources refresh (Admin › Scheduled jobs).</p>`}
          ${data.reasons.length && !fresh ? '<p class="cc-md-note">Nothing new this week, so these are the latest. Sources refresh every 6 hours (Admin › Scheduled jobs).</p>' : ''}
        </section>
      </div>
    </div>`;
  const input = el.querySelector('.cc-md-form input');
  if (input) input.focus();
}

async function load(force = true) {
  data = await loadMyDay(force);
  render();
}

const findRow = (key) => data && data.today_items.find(q => rowKey(q) === key);

async function call(url, method, body) {
  const res = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `Failed (${res.status})`); }
}

// Runs one row action against the existing endpoints; returns the toast text
async function run(op, q, form) {
  const v = (n) => (form && form.elements[n] ? form.elements[n].value.trim() : '');
  if (op === 'complete') { await call(`/api/action-items/${q.task_id}/complete`, 'POST'); return 'Done'; }
  if (op === 'snooze') {
    const t = new Date(); t.setDate(t.getDate() + 1); t.setHours(9, 0, 0, 0);
    await call(`/api/action-items/${q.task_id}`, 'PATCH', { due_date: t.toISOString() });
    return 'Snoozed to tomorrow';
  }
  if (op === 'reschedule') { await call(`/api/deals/${q.deal_id}`, 'PATCH', { next_step_due: v('due') }); return 'Next step rescheduled'; }
  if (op === 'set_step') { await call(`/api/deals/${q.deal_id}`, 'PATCH', { next_step: v('step'), next_step_due: v('due') }); return 'Next step set'; }
  if (op === 'log_call') {
    await call('/api/crm/activities', 'POST', { type: 'call', direction: 'outbound', subject: v('note'),
      links: [{ object_type: 'deal', object_id: q.deal_id }] });
    return 'Call logged';
  }
  if (op === 'decline') {
    await call(`/api/crm/introductions/${q.intro_id}`, 'PATCH', { status: 'declined', closed_reason: v('reason') });
    return 'Introduction marked declined';
  }
  throw new Error('Unknown action');
}

async function onClick(e) {
  const f = e.target.closest('[data-md-filter]');
  if (f) { filter = f.dataset.mdFilter; expanded = false; editing = null; render(); return; }
  if (e.target.closest('[data-md-more]')) { expanded = !expanded; render(); return; }
  if (e.target.closest('[data-md-cancel]')) { editing = null; render(); return; }
  const b = e.target.closest('button[data-md-op]');
  if (!b) return;
  const q = findRow(b.dataset.mdRow);
  if (!q) return;
  if (NEEDS_FORM.has(b.dataset.mdOp)) { editing = `${rowKey(q)}:${b.dataset.mdOp}`; render(); return; }
  b.disabled = true;
  try {
    showToast(await run(b.dataset.mdOp, q));
    document.dispatchEvent(new CustomEvent('cc:plays-changed'));
    await load();
  } catch (err) { showToast(err.message); b.disabled = false; }
}

async function onSubmit(e) {
  const form = e.target.closest('[data-md-form]');
  if (!form) return;
  e.preventDefault();
  const q = findRow(form.dataset.mdRow);
  if (!q) return;
  form.querySelectorAll('button').forEach(x => { x.disabled = true; });
  try {
    showToast(await run(form.dataset.mdForm, q, form));
    editing = null;
    await load();
  } catch (err) {
    showToast(err.message);
    form.querySelectorAll('button').forEach(x => { x.disabled = false; });
  }
}

function onKeydown(e) {
  if (e.key === 'Escape' && editing) { editing = null; render(); }
}

export async function initMyDay() {
  const el = document.getElementById('ccMyDay');
  if (!el) return;
  el.innerHTML = '<div class="cc-md-loading">Loading My Day…</div>';
  el.addEventListener('click', onClick);
  el.addEventListener('submit', onSubmit);
  el.addEventListener('keydown', onKeydown);
  try { await load(false); } catch (err) {
    el.innerHTML = `<p class="cc-md-empty">${esc(err.message)}</p>`;
  }
}

export function refreshMyDay() { if (data) load().catch(() => {}); }
