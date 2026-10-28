// Deals page views: Board (main.js) · List with sorting, bulk edit and CSV (#6) · Insights —
// win/loss and stage velocity (#9) · saved views per user (#6, /api/crm/views).
const VIEW_KEY = 'deals.view';
const PRESETS = [
  { id: 'p-mine', name: 'My open deals', state: { view: 'list', mine: true, status: 'open' } },
  { id: 'p-close', name: 'Closing this month', state: { view: 'list', status: 'open', close: 'month', sort: 'expected_close', dir: 'asc' } },
  { id: 'p-attn', name: 'Needs attention', state: { view: 'list', status: 'open', attention: true, sort: 'health', dir: 'asc' } },
  { id: 'p-lost', name: 'Recently lost', state: { view: 'list', status: 'lost', sort: 'updated_at', dir: 'desc' } },
];
const COLS = [
  { key: 'name', label: 'Deal' }, { key: 'stage', label: 'Stage' }, { key: 'amount', label: 'Value', num: true },
  { key: 'expected_close', label: 'Expected close' }, { key: 'next_step_due', label: 'Next step' },
  { key: 'health', label: 'Health', num: true }, { key: 'owner', label: 'Owner' }, { key: 'updated_at', label: 'Updated' },
];
const STAGE_ORDER = ['intro', 'discovery', 'proposal', 'pilot', 'contract', 'won', 'lost'];

let c = null;           // context from main.js
const v = { view: 'board', sort: 'updated_at', dir: 'desc', status: 'open', close: '', selected: new Set(),
  saved: [], days: 365, insights: null, activeView: null };

export function initViews(ctx) {
  c = ctx;
  const params = new URLSearchParams(location.search);
  let view = params.get('view');
  if (!view) { try { view = localStorage.getItem(VIEW_KEY); } catch { /* storage unavailable */ } }
  v.view = ['board', 'list', 'insights'].includes(view) ? view : 'board';
  document.querySelector('.dl-views').addEventListener('click', (e) => {
    const b = e.target.closest('[data-view]');
    if (b) { v.activeView = null; setView(b.dataset.view); renderSaved(); }
  });
  document.querySelector('.dl-views').addEventListener('keydown', (e) => {   // arrow keys between tabs
    if (!['ArrowLeft', 'ArrowRight'].includes(e.key)) return;
    const tabs = [...document.querySelectorAll('.dl-view-tab')];
    const i = tabs.indexOf(document.activeElement);
    const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
    next.focus(); next.click();
  });
  wireList();
  wireInsights();
  wireSaved();
  setView(v.view, { quiet: true });
  loadSaved();
}

function setView(view, { quiet = false } = {}) {
  v.view = view;
  try { localStorage.setItem(VIEW_KEY, view); } catch { /* ignore */ }
  if (!quiet) { const url = new URL(location.href); url.searchParams.set('view', view); history.replaceState(null, '', url); }
  document.querySelectorAll('.dl-view-tab').forEach((t) => {
    const on = t.dataset.view === view;
    t.setAttribute('aria-selected', String(on)); t.tabIndex = on ? 0 : -1; t.classList.toggle('active', on);
  });
  c.$('dlBoard').hidden = view !== 'board';
  c.$('dlClosed').hidden = view !== 'board';
  c.$('dlDigest').hidden = view === 'insights';
  c.$('dlList').hidden = view !== 'list';
  c.$('dlInsights').hidden = view !== 'insights';
  refreshViews();
}

// Called by main.js after every reload of the deals list
export function refreshViews() {
  if (!c) return;
  if (v.view === 'list') renderList();
  if (v.view === 'insights') loadInsights();
}

// ── List (#6) ────────────────────────────────────────────────────────────────
function sortValue(d, key) {
  switch (key) {
    case 'name': return (d.name || '').toLowerCase();
    case 'stage': return STAGE_ORDER.indexOf(d.stage);
    case 'amount': return d.amount_usd ?? d.value_amount ?? -1;
    case 'health': return d.health && d.health.score != null ? d.health.score : 999;
    case 'owner': return ((d.owner && d.owner.name) || '').toLowerCase();
    case 'next_step_due': return d.next_step_due || '9999';
    case 'expected_close': return d.expected_close || '9999';
    default: return d[key] || '';
  }
}

function listRows() {
  const attn = !!(c.$('dlAttention') && c.$('dlAttention').checked);
  const now = new Date();
  const monthEnd = new Date(now.getFullYear(), now.getMonth() + 1, 0).toISOString().slice(0, 10);
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10);
  const rows = c.st.deals.filter((d) => {
    if (v.status === 'open' && !c.OPEN_STAGES.includes(d.stage)) return false;
    if (v.status === 'won' && d.stage !== 'won') return false;
    if (v.status === 'lost' && d.stage !== 'lost') return false;
    if (attn && !c.needsAttention(d)) return false;
    if (v.close === 'month' && !(d.expected_close && d.expected_close >= monthStart && d.expected_close <= monthEnd)) return false;
    return true;
  });
  const dir = v.dir === 'asc' ? 1 : -1;
  return rows.sort((a, b) => {
    const x = sortValue(a, v.sort); const y = sortValue(b, v.sort);
    return (x < y ? -1 : x > y ? 1 : 0) * dir;
  });
}

function renderList() {
  const rows = listRows();
  const ids = new Set(rows.map(d => d.id));
  v.selected.forEach((id) => { if (!ids.has(id)) v.selected.delete(id); });
  const viewer = document.body.classList.contains('role-viewer');
  const head = COLS.map((col) => {
    const on = v.sort === col.key;
    return `<th scope="col" aria-sort="${on ? (v.dir === 'asc' ? 'ascending' : 'descending') : 'none'}"${col.num ? ' class="num"' : ''}>
      <button type="button" data-sort="${col.key}">${c.esc(col.label)}${on ? ` <i class="fa-solid fa-arrow-${v.dir === 'asc' ? 'up' : 'down'}"></i>` : ''}</button></th>`;
  }).join('');
  const body = rows.map((d) => {
    const overdue = c.isOverdue(d.next_step_due) && c.OPEN_STAGES.includes(d.stage);
    const h = d.health || {};
    return `<tr data-row="${d.id}" class="${c.needsAttention(d) ? 'dl-attn' : ''}">
      ${viewer ? '' : `<td class="dl-sel"><input type="checkbox" data-select="${d.id}" aria-label="Select ${c.esc(d.name)}"${v.selected.has(d.id) ? ' checked' : ''}></td>`}
      <td><button type="button" class="dl-link" data-open="${d.id}">${c.esc(d.name)}</button><div class="dl-list-sub">${c.esc(d.account_name)}${d.business_line_name ? ` · ${c.esc(d.business_line_name)}` : ''}</div></td>
      <td><span class="dl-stage-pill s-${d.stage}">${c.esc(d.stage_label)}</span>${d.stage === 'lost' && c.lostText(d) ? `<div class="dl-list-sub">${c.esc(c.lostText(d))}</div>` : ''}</td>
      <td class="num">${c.esc(c.money(d.value_amount, d.currency))}</td>
      <td>${d.expected_close ? c.esc(c.dateLabel(d.expected_close)) : '<span class="dl-muted">—</span>'}</td>
      <td class="${overdue ? 'dl-overdue' : ''}">${d.next_step ? `${c.esc(d.next_step)}${d.next_step_due ? `<div class="dl-list-sub">${overdue ? 'Overdue · ' : ''}${c.esc(c.dateLabel(d.next_step_due))}</div>` : ''}` : '<span class="dl-muted">—</span>'}</td>
      <td class="num">${h.score != null ? c.healthBadge(h) : '<span class="dl-muted">—</span>'}</td>
      <td>${c.esc((d.owner && d.owner.name) || '—')}</td>
      <td>${c.esc(c.dateLabel(d.updated_at))}</td>
    </tr>`;
  }).join('');
  const allOn = rows.length && rows.every(d => v.selected.has(d.id));
  const n = v.selected.size;
  c.$('dlList').innerHTML = `
    <div class="dl-list-tools">
      <label>Show <select id="dlListStatus" class="dl-select">
        ${[['open', 'Open deals'], ['all', 'All deals'], ['won', 'Won'], ['lost', 'Lost']].map(([k, l]) => `<option value="${k}"${v.status === k ? ' selected' : ''}>${l}</option>`).join('')}
      </select></label>
      <label>Expected close <select id="dlListClose" class="dl-select">
        <option value="">Any time</option><option value="month"${v.close === 'month' ? ' selected' : ''}>This month</option></select></label>
      <span class="dl-list-count">${rows.length} deal${rows.length === 1 ? '' : 's'}</span>
      <button type="button" class="dl-btn" data-export><i class="fa-solid fa-file-csv"></i> Export ${n ? `${n} selected` : 'list'}</button>
    </div>
    ${n && !viewer ? `<div class="dl-bulk" role="region" aria-label="Bulk edit">
      <strong>${n} selected</strong>
      <label>Forecast <select id="dlBulkFc" class="dl-select"><option value="">—</option><option value="pipeline">Pipeline</option>
        <option value="best_case">Best case</option><option value="commit">Commit</option><option value="omitted">Omitted</option></select></label>
      <label>Expected close <input type="date" id="dlBulkClose"></label>
      <label>Next step due <input type="date" id="dlBulkDue"></label>
      <button type="button" class="dl-btn dl-btn-primary" data-bulk-apply>Apply</button>
      <button type="button" class="dl-btn" data-bulk-clear>Clear selection</button>
    </div>` : ''}
    <div class="dl-table-wrap"><table class="dl-table">
      <thead><tr>${viewer ? '' : `<th class="dl-sel"><input type="checkbox" data-select-all aria-label="Select all"${allOn ? ' checked' : ''}></th>`}${head}</tr></thead>
      <tbody>${body || `<tr><td colspan="${COLS.length + 1}" class="dl-empty-col">No deals match these filters.</td></tr>`}</tbody>
    </table></div>`;
}

function wireList() {
  const box = c.$('dlList');
  box.addEventListener('click', async (e) => {
    const t = e.target;
    const sort = t.closest('[data-sort]');
    if (sort) {
      const k = sort.dataset.sort;
      if (v.sort === k) v.dir = v.dir === 'asc' ? 'desc' : 'asc';
      else { v.sort = k; v.dir = ['amount', 'updated_at'].includes(k) ? 'desc' : 'asc'; }
      renderList(); box.querySelector(`[data-sort="${k}"]`).focus(); return;
    }
    const open = t.closest('[data-open]');
    if (open) { c.openDeal(Number(open.dataset.open)); return; }
    if (t.closest('[data-export]')) { exportCsv(); return; }
    if (t.closest('[data-bulk-clear]')) { v.selected.clear(); renderList(); return; }
    if (t.closest('[data-bulk-apply]')) { await bulkApply(); return; }
    if (t.matches('[data-select-all]')) {
      listRows().forEach(d => (t.checked ? v.selected.add(d.id) : v.selected.delete(d.id))); renderList(); return;
    }
    if (t.matches('[data-select]')) {
      const id = Number(t.dataset.select);
      if (t.checked) v.selected.add(id); else v.selected.delete(id);
      renderList(); box.querySelector(`[data-select="${id}"]`).focus(); return;
    }
    const row = t.closest('tr[data-row]');
    if (row && !t.closest('input, button, a, select')) c.openDeal(Number(row.dataset.row));
  });
  box.addEventListener('change', (e) => {
    if (e.target.id === 'dlListStatus') { v.status = e.target.value; v.activeView = null; renderSaved(); renderList(); }
    if (e.target.id === 'dlListClose') { v.close = e.target.value; v.activeView = null; renderSaved(); renderList(); }
  });
}

async function bulkApply() {
  const body = {};
  if (c.$('dlBulkFc').value) body.forecast_category = c.$('dlBulkFc').value;
  if (c.$('dlBulkClose').value) body.expected_close = c.$('dlBulkClose').value;
  if (c.$('dlBulkDue').value) body.next_step_due = c.$('dlBulkDue').value;
  if (!Object.keys(body).length) { c.showToast('Pick at least one change to apply'); return; }
  const ids = [...v.selected];
  let ok = 0; const failed = [];
  for (const id of ids) {          // one PATCH per deal, so every deal keeps its own rules and change log
    const d = c.st.deals.find(x => x.id === id);
    const b = { ...body };
    if (d && !c.OPEN_STAGES.includes(d.stage)) delete b.forecast_category;   // closed deals are categorised automatically
    if (!Object.keys(b).length) { failed.push(`${d ? d.name : id}: closed`); continue; }
    try { await c.api(`/${id}`, { method: 'PATCH', body: b }); ok += 1; } catch (err) { failed.push(`${d ? d.name : id}: ${err.message}`); }
  }
  v.selected.clear();
  c.showToast(`Updated ${ok} deal${ok === 1 ? '' : 's'}${failed.length ? ` · ${failed.length} skipped (${failed[0]})` : ''}`);
  await c.reload();
}

function exportCsv() {
  const rows = listRows().filter(d => !v.selected.size || v.selected.has(d.id));
  const cell = (x) => { const s = x == null ? '' : String(x); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const lines = [['Deal', 'Account', 'Business line', 'Stage', 'Value', 'Currency', 'Value (USD)', 'Expected close',
    'Next step', 'Next step due', 'Health', 'Forecast', 'Owner', 'Loss reason', 'Updated'].join(',')];
  rows.forEach(d => lines.push([d.name, d.account_name, d.business_line_name, d.stage_label, d.value_amount, d.currency, d.amount_usd,
    d.expected_close, d.next_step, d.next_step_due, d.health && d.health.score, d.forecast_category,
    d.owner && d.owner.name, c.lostText(d), String(d.updated_at || '').slice(0, 10)].map(cell).join(',')));
  const blob = new Blob([`﻿${lines.join('\r\n')}`], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = `deals-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ── Insights (#9) ────────────────────────────────────────────────────────────
async function loadInsights() {
  const box = c.$('dlInsights');
  const p = new URLSearchParams({ days: String(v.days) });
  if (c.$('dlBusinessLine').value) p.set('business_line_id', c.$('dlBusinessLine').value);
  if (c.$('dlMine') && c.$('dlMine').checked) p.set('mine', 'true');
  if (!v.insights) box.innerHTML = '<p class="dl-muted">Loading insights…</p>';
  try { v.insights = await c.api(`/analytics?${p}`); renderInsights(); }
  catch (err) { box.innerHTML = `<p class="dl-warn">${c.esc(err.message)}</p>`; }
}

const pct = (x) => (x == null ? '—' : `${x}%`);
const bar = (n, max, cls = '') => `<span class="dl-bar ${cls}"><span style="width:${max ? Math.max(2, Math.round((100 * n) / max)) : 0}%"></span></span>`;

function renderInsights() {
  const a = v.insights;
  const t = a.totals;
  const scope = [(c.$('dlMine') && c.$('dlMine').checked) ? 'your deals' : 'deals you can see',
    c.$('dlBusinessLine').value ? c.$('dlBusinessLine').selectedOptions[0].textContent : ''].filter(Boolean).join(' · ');
  const periods = [[90, 'Last 90 days'], [180, 'Last 6 months'], [365, 'Last 12 months'], [730, 'Last 2 years']];
  const closedN = t.won + t.lost;
  const maxReached = Math.max(1, ...a.funnel.map(f => f.reached));
  const slowest = a.funnel.filter(f => f.avg_days != null).sort((x, y) => y.avg_days - x.avg_days)[0];
  const weakest = a.funnel.filter(f => f.to_next_pct != null && f.reached >= 3).sort((x, y) => x.to_next_pct - y.to_next_pct)[0];
  const maxLoss = Math.max(1, ...a.loss_reasons.map(r => r.count));
  const src = a.by_source;
  c.$('dlInsights').innerHTML = `
    <div class="dl-ins-head">
      <p class="dl-muted">Closed and created in the period · ${c.esc(scope)} · filters for business line and "My deals" apply</p>
      <label>Period <select id="dlInsDays" class="dl-select">${periods.map(([d, l]) => `<option value="${d}"${v.days === d ? ' selected' : ''}>${l}</option>`).join('')}</select></label>
    </div>
    <div class="dl-kpis">
      <div class="dl-kpi"><span>Win rate</span><b>${pct(t.win_rate)}</b><small>${t.won} won · ${t.lost} lost</small></div>
      <div class="dl-kpi"><span>Won</span><b>${c.esc(c.money(t.won_value))}</b><small>${t.avg_won_size != null ? `avg ${c.esc(c.money(t.avg_won_size))}` : 'no wins yet'}</small></div>
      <div class="dl-kpi"><span>Sales cycle</span><b>${t.avg_cycle_days != null ? `${t.avg_cycle_days} d` : '—'}</b><small>created → won, average</small></div>
      <div class="dl-kpi"><span>Lost value</span><b>${c.esc(c.money(t.lost_value))}</b><small>${t.lost} deal${t.lost === 1 ? '' : 's'}</small></div>
    </div>
    ${!closedN && !a.cohort_size ? '<p class="dl-dg-calm">No closed or new deals in this period yet — try a longer period.</p>' : ''}
    <div class="dl-ins-grid">
      <section class="dl-ins-card dl-ins-wide" aria-labelledby="insFunnel">
        <h3 id="insFunnel">Stage conversion &amp; time in stage</h3>
        <p class="dl-muted">${a.cohort_size} deal${a.cohort_size === 1 ? '' : 's'} created in the period.
          ${weakest ? `Biggest drop-off: <strong>${c.esc(weakest.label)}</strong> (${weakest.to_next_pct}% move on).` : ''}
          ${slowest ? `Slowest stage: <strong>${c.esc(slowest.label)}</strong> (${slowest.avg_days} days on average).` : ''}</p>
        <table class="dl-funnel">
          <thead><tr><th scope="col">Stage</th><th scope="col">Reached</th><th scope="col" class="num">Move on</th>
            <th scope="col" class="num">Avg days</th><th scope="col" class="num">Open now</th></tr></thead>
          <tbody>${a.funnel.map(f => `<tr>
            <th scope="row">${c.esc(f.label)}</th>
            <td>${bar(f.reached, maxReached, f.stage === 'won' ? 'won' : '')}<span class="dl-bar-n">${f.reached}</span></td>
            <td class="num ${weakest && weakest.stage === f.stage ? 'dl-flag' : ''}">${pct(f.to_next_pct)}</td>
            <td class="num ${slowest && slowest.stage === f.stage ? 'dl-flag' : ''}">${f.avg_days ?? '—'}</td>
            <td class="num">${f.stage === 'won' ? '' : `${f.open_now}${f.open_avg_days != null ? ` <small class="dl-muted">(${f.open_avg_days} d)</small>` : ''}`}</td>
          </tr>`).join('')}</tbody>
        </table>
      </section>
      <section class="dl-ins-card" aria-labelledby="insLoss">
        <h3 id="insLoss">Why we lose</h3>
        ${a.loss_reasons.length ? `<ul class="dl-hbars">${a.loss_reasons.map(r => `<li><span>${c.esc(r.label)}</span>${bar(r.count, maxLoss, 'lost')}<b>${r.count}</b></li>`).join('')}</ul>`
          : '<p class="dl-muted">No lost deals in this period.</p>'}
        ${a.competitors.length ? `<h4>Competitors we lost to</h4><ul class="dl-plain">${a.competitors.map(x => `<li>${c.esc(x.name)} <span class="dl-muted">· ${x.count} deal${x.count === 1 ? '' : 's'} · ${c.esc(c.money(x.value))}</span></li>`).join('')}</ul>` : ''}
      </section>
      <section class="dl-ins-card" aria-labelledby="insSource">
        <h3 id="insSource">Introductions vs other deals</h3>
        <ul class="dl-hbars">
          <li><span>From an introduction</span>${bar(src.introduction.win_rate || 0, 100, 'won')}<b>${pct(src.introduction.win_rate)}</b></li>
          <li><span>Other sources</span>${bar(src.other.win_rate || 0, 100)}<b>${pct(src.other.win_rate)}</b></li>
        </ul>
        <p class="dl-muted">Win rate of closed deals · introductions: ${src.introduction.won} won / ${src.introduction.lost} lost
          (${c.esc(c.money(src.introduction.won_value))}) · other: ${src.other.won} won / ${src.other.lost} lost</p>
        ${a.top_connectors.length ? `<h4>Connectors behind won deals</h4><ul class="dl-plain">${a.top_connectors.map(x => `<li>${c.esc(x.name)} <span class="dl-muted">· ${x.won} won · ${c.esc(c.money(x.value))}</span></li>`).join('')}</ul>` : ''}
      </section>
    </div>`;
}

function wireInsights() {
  c.$('dlInsights').addEventListener('change', (e) => {
    if (e.target.id === 'dlInsDays') { v.days = Number(e.target.value); loadInsights(); }
  });
}

// ── Saved views (#6) ─────────────────────────────────────────────────────────
async function crm(path, opts = {}) {
  const res = await fetch(`/api/crm/views${path}`, { headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

async function loadSaved() {
  try { v.saved = (await crm('?page=deals')).views; } catch { v.saved = []; }
  renderSaved();
}

function currentState() {
  return { view: v.view, status: v.status, close: v.close, sort: v.sort, dir: v.dir, days: v.days,
    mine: !!(c.$('dlMine') && c.$('dlMine').checked), attention: !!(c.$('dlAttention') && c.$('dlAttention').checked),
    account: c.$('dlAccount').value || '', business_line: c.$('dlBusinessLine').value || '', q: c.$('dlSearch').value.trim() };
}

async function applyState(s, id) {
  v.status = s.status || 'open'; v.close = s.close || ''; v.sort = s.sort || 'updated_at'; v.dir = s.dir || 'desc';
  v.days = s.days || v.days;
  if (c.$('dlMine')) c.$('dlMine').checked = !!s.mine;
  if (c.$('dlAttention')) c.$('dlAttention').checked = !!s.attention;
  c.$('dlAccount').value = s.account || ''; c.$('dlBusinessLine').value = s.business_line || ''; c.$('dlSearch').value = s.q || '';
  v.activeView = id;
  setView(s.view || 'list');
  renderSaved();
  await c.reload();
}

function renderSaved() {
  const chip = (id, name, mine) => `<span class="dl-saved-chip${v.activeView === id ? ' active' : ''}">
    <button type="button" data-apply-view="${c.esc(id)}" aria-pressed="${v.activeView === id}">${c.esc(name)}</button>
    ${mine ? `<button type="button" class="dl-saved-x" data-delete-view="${c.esc(id)}" aria-label="Delete saved view ${c.esc(name)}"><i class="fa-solid fa-xmark"></i></button>` : ''}</span>`;
  c.$('dlSaved').innerHTML = `<span class="dl-saved-label">Views</span>
    ${PRESETS.map(p => chip(p.id, p.name, false)).join('')}
    ${v.saved.map(s => chip(String(s.id), s.name, true)).join('')}
    <button type="button" class="dl-btn dl-saved-add" data-save-view><i class="fa-regular fa-bookmark"></i> Save view</button>
    <span class="dl-save-form" id="dlSaveForm" hidden><input id="dlSaveName" maxlength="60" placeholder="Name this view" aria-label="Name for the saved view">
      <button type="button" class="dl-btn dl-btn-primary" data-save-go>Save</button><button type="button" class="dl-btn" data-save-cancel>Cancel</button></span>`;
}

function wireSaved() {
  const box = c.$('dlSaved');
  box.addEventListener('click', async (e) => {
    const t = e.target;
    const apply = t.closest('[data-apply-view]');
    if (apply) {
      const id = apply.dataset.applyView;
      const p = PRESETS.find(x => x.id === id);
      const s = p ? p.state : (v.saved.find(x => String(x.id) === id) || {}).state;
      if (s) await applyState(s, id);
      return;
    }
    const del = t.closest('[data-delete-view]');
    if (del) {
      try { await crm(`/${del.dataset.deleteView}`, { method: 'DELETE' }); if (v.activeView === del.dataset.deleteView) v.activeView = null; c.showToast('Saved view deleted'); loadSaved(); }
      catch (err) { c.showToast(err.message); }
      return;
    }
    const saveTrigger = t.closest('[data-save-view]');
    if (saveTrigger) {
      c.$('dlSaveForm').hidden = false;
      saveTrigger.hidden = true;
      c.$('dlSaveName').value = '';
      c.$('dlSaveName').focus();
      return;
    }
    if (t.closest('[data-save-cancel]')) { renderSaved(); return; }
    if (t.closest('[data-save-go]')) { await saveCurrent(); }
  });
  box.addEventListener('keydown', (e) => {
    if (e.target.id !== 'dlSaveName') return;
    if (e.key === 'Enter') { e.preventDefault(); saveCurrent(); }
    if (e.key === 'Escape') { e.stopPropagation(); renderSaved(); }
  });
}

async function saveCurrent() {
  const name = c.$('dlSaveName').value.trim();
  if (!name) { c.showToast('Give the view a name'); c.$('dlSaveName').focus(); return; }
  try {
    const saved = await crm('', { method: 'POST', body: { page: 'deals', name, state: currentState() } });
    v.activeView = String(saved.id);
    c.showToast(`Saved "${saved.name}"`);
    await loadSaved();
  } catch (err) { c.showToast(err.message); }
}

// Any manual filter change means we're no longer exactly on a saved view
export function filtersChanged() {
  if (v.activeView) { v.activeView = null; renderSaved(); }
}
