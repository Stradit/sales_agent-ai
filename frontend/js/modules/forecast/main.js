// Forecast page (/forecast): fiscal-quarter roll-up by owner / business line / stage / category,
// progress to target, week-over-week changes, deal list with inline category, targets and
// exchange-rate editors. API: apps/sales_crm/forecast.py. No AI requests.
import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { showToast } from '../toast.js';
import { esc } from '../utils.js';
import { downloadFile } from '../download.js';

const $ = (id) => document.getElementById(id);
const CAT_LABEL = { closed: 'Closed won', commit: 'Commit', best_case: 'Best case', pipeline: 'Pipeline', omitted: 'Omitted' };
const CHANGE_ICON = {
  won: ['fa-trophy', 'good'], lost: ['fa-circle-xmark', 'bad'], slipped_out: ['fa-calendar-xmark', 'bad'], new: ['fa-plus', 'info'],
  category: ['fa-tag', 'info'], advanced: ['fa-arrow-right', 'good'], moved_back: ['fa-arrow-left', 'bad'],
  slipped: ['fa-calendar-minus', 'warn'], pulled_in: ['fa-calendar-plus', 'good'], amount: ['fa-dollar-sign', 'info'], removed: ['fa-trash', 'bad'],
};
const CHANGE_LABEL = { won: 'Won', lost: 'Lost', slipped_out: 'Slipped out of quarter', new: 'New', category: 'Category', advanced: 'Advanced',
  moved_back: 'Moved back', slipped: 'Close date moved', pulled_in: 'Pulled in', amount: 'Amount', removed: 'Removed' };

const st = { meta: null, data: null, groupBy: 'owner', dealFilter: null, me: null };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401) { location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); throw new Error('unauthenticated'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

function usd(v, compact = true) {
  if (v == null) return '—';
  return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', notation: compact && Math.abs(v) >= 10000 ? 'compact' : 'standard',
    maximumFractionDigits: compact && Math.abs(v) >= 10000 ? 1 : 0 }).format(v);
}
const pct = (v) => (v == null ? '—' : `${v}%`);
const dateLabel = (d) => (d ? new Date(`${String(d).slice(0, 10)}T00:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—');

function query(extra = {}) {
  const p = new URLSearchParams({ period: $('fcPeriod').value, ...extra });
  if ($('fcBusinessLine').value) p.set('business_line_id', $('fcBusinessLine').value);
  if ($('fcOwner').value) p.set('owner_id', $('fcOwner').value);
  return p;
}

// ── Render ───────────────────────────────────────────────────────────────────
function renderKpis(t) {
  const cards = [
    ['Target', usd(t.target), t.target == null ? 'Not set' : `${pct(t.attainment_pct)} attained`, 'fa-bullseye'],
    ['Closed won', usd(t.closed), t.gap != null ? `${usd(t.gap)} to go` : '', 'fa-trophy'],
    ['Commit forecast', usd(t.commit_forecast), 'Closed + commit', 'fa-lock'],
    ['Best-case forecast', usd(t.best_case_forecast), 'Closed + commit + best case', 'fa-arrow-trend-up'],
    ['Open pipeline', usd(t.open_pipeline), t.coverage != null ? `${t.coverage}× coverage of the gap` : `${t.deals} deal${t.deals === 1 ? '' : 's'}`, 'fa-layer-group'],
    ['Weighted', usd(t.weighted), 'Amount × stage probability', 'fa-scale-balanced'],
  ];
  $('fcKpis').innerHTML = cards.map(([label, value, sub, icon]) => `<div class="fc-kpi"><span><i class="fa-solid ${icon}"></i> ${label}</span>
    <strong>${esc(value)}</strong><small>${esc(sub)}</small></div>`).join('');
  const target = t.target || 0;
  const scale = Math.max(target, t.best_case_forecast + t.pipeline, 1);
  const w = (v) => `${Math.max(0, Math.min(100, (v / scale) * 100))}%`;
  $('fcBar').innerHTML = `<div class="fc-bar" role="img" aria-label="Closed ${usd(t.closed)}, commit ${usd(t.commit)}, best case ${usd(t.best_case)}, pipeline ${usd(t.pipeline)}${target ? `, target ${usd(target)}` : ''}">
      <span class="closed" style="width:${w(t.closed)}"></span><span class="commit" style="width:${w(t.commit)}"></span>
      <span class="best" style="width:${w(t.best_case)}"></span><span class="pipe" style="width:${w(t.pipeline)}"></span>
      ${target ? `<i class="fc-target" style="left:${w(target)}" title="Target ${usd(target)}"></i>` : ''}</div>
    <div class="fc-legend"><span><b class="closed"></b>Closed</span><span><b class="commit"></b>Commit</span><span><b class="best"></b>Best case</span>
      <span><b class="pipe"></b>Pipeline</span>${target ? '<span><b class="tgt"></b>Target</span>' : ''}</div>`;
}

function cell(row, bucket, value) {
  if (!value) return `<td class="num fc-zero">${usd(value)}</td>`;
  return `<td class="num"><button type="button" class="fc-cell" data-row="${esc(String(row.key))}" data-bucket="${bucket}"
    title="Show these deals">${usd(value)}</button></td>`;
}

function renderRollup(d) {
  const rows = d.rows;
  if (!rows.length) { $('fcRollup').innerHTML = '<p class="fc-empty">No deals close in this quarter yet. Deals count here by their expected close date.</p>'; return; }
  const t = d.totals;
  $('fcRollup').innerHTML = `<table class="fc-table"><thead><tr><th scope="col">${{ owner: 'Owner', business_line: 'Business line', stage: 'Stage', category: 'Category' }[d.group_by]}</th>
      <th scope="col" class="num">Closed</th><th scope="col" class="num">Commit</th><th scope="col" class="num">Best case</th><th scope="col" class="num">Pipeline</th>
      <th scope="col" class="num">Weighted</th><th scope="col" class="num">Commit fcst</th><th scope="col" class="num">Target</th><th scope="col" class="num">Coverage</th></tr></thead>
    <tbody>${rows.map(r => `<tr><th scope="row">${esc(r.label)}<small>${r.deals} deal${r.deals === 1 ? '' : 's'}</small></th>
      ${cell(r, 'closed', r.closed)}${cell(r, 'commit', r.commit)}${cell(r, 'best_case', r.best_case)}${cell(r, 'pipeline', r.pipeline)}
      <td class="num">${usd(r.weighted)}</td><td class="num"><strong>${usd(r.commit_forecast)}</strong></td>
      <td class="num">${r.target != null ? `${usd(r.target)}<small>${pct(r.attainment_pct)}</small>` : '—'}</td>
      <td class="num ${r.coverage != null && r.coverage < 3 ? 'fc-low' : ''}">${r.coverage != null ? `${r.coverage}×` : '—'}</td></tr>`).join('')}</tbody>
    <tfoot><tr><th scope="row">Total</th><td class="num">${usd(t.closed)}</td><td class="num">${usd(t.commit)}</td><td class="num">${usd(t.best_case)}</td>
      <td class="num">${usd(t.pipeline)}</td><td class="num">${usd(t.weighted)}</td><td class="num"><strong>${usd(t.commit_forecast)}</strong></td>
      <td class="num">${usd(t.target)}</td><td class="num">${t.coverage != null ? `${t.coverage}×` : '—'}</td></tr></tfoot></table>`;
}

function dealMatches(x) {
  const f = st.dealFilter;
  if (!f) return true;
  const key = { owner: x.owner_user_id, business_line: x.business_line_id, stage: x.stage, category: x.bucket }[st.data.group_by];
  return String(key) === f.row && x.bucket === f.bucket;
}

function renderDeals(d) {
  const list = d.deals.filter(dealMatches);
  const canEdit = !(st.me && st.me.read_only);
  $('fcDealFilter').hidden = !st.dealFilter;
  if (st.dealFilter) {
    const row = d.rows.find(r => String(r.key) === st.dealFilter.row);
    $('fcDealFilter').innerHTML = `${esc(row ? row.label : '')} · ${esc(CAT_LABEL[st.dealFilter.bucket])}
      <button type="button" class="dl-icon" data-clear-filter aria-label="Clear filter"><i class="fa-solid fa-xmark"></i></button>`;
  }
  $('fcDeals').innerHTML = list.length ? `<table class="fc-table fc-deals"><thead><tr><th scope="col">Deal</th><th scope="col">Owner</th><th scope="col">Stage</th>
      <th scope="col">Category</th><th scope="col" class="num">Amount (USD)</th><th scope="col" class="num">Prob.</th><th scope="col" class="num">Weighted</th>
      <th scope="col">Close</th></tr></thead><tbody>
    ${list.map(x => `<tr><td><a href="/deals?deal=${x.id}">${esc(x.name)}</a><small>${esc(x.account_name)}${x.business_line_id ? ` · ${esc(x.business_line_name)}` : ''}</small></td>
      <td>${esc(x.owner_name)}</td><td>${esc(x.stage_label)}</td>
      <td>${x.bucket === 'closed' || !canEdit ? `<span class="fc-cat ${x.bucket}">${esc(x.category_label)}</span>`
        : `<select class="fc-cat-select ${x.bucket}" data-deal="${x.id}" aria-label="Forecast category for ${esc(x.name)}">
            ${['commit', 'best_case', 'pipeline', 'omitted'].map(k => `<option value="${k}"${k === x.bucket ? ' selected' : ''}>${CAT_LABEL[k]}</option>`).join('')}</select>`}</td>
      <td class="num">${x.amount_usd != null ? usd(x.amount_usd, false) : '—'}${x.currency && x.currency !== 'USD' && x.value_amount != null
        ? `<small>${esc(x.value_amount.toLocaleString())} ${esc(x.currency)}</small>` : ''}</td>
      <td class="num">${x.probability}%${x.probability_is_override ? '<small>override</small>' : ''}</td>
      <td class="num">${x.bucket === 'closed' ? '—' : usd(x.weighted_usd)}</td><td>${esc(dateLabel(x.close_date))}</td></tr>`).join('')}</tbody></table>`
    : '<p class="fc-empty">No deals match.</p>';
  $('fcUndated').innerHTML = d.undated.length ? `<details class="fc-undated"><summary><i class="fa-solid fa-calendar-xmark"></i>
      ${d.undated.length} open deal${d.undated.length === 1 ? ' has' : 's have'} no expected close date, so ${d.undated.length === 1 ? 'it isn\'t' : 'they aren\'t'} in any quarter</summary>
      <ul>${d.undated.map(x => `<li><a href="/deals?deal=${x.id}">${esc(x.name)}</a> · ${esc(x.account_name)} · ${esc(x.stage_label)} · ${usd(x.amount_usd)}</li>`).join('')}</ul></details>` : '';
}

function renderChanges(c) {
  $('fcChangesSince').textContent = c.since ? `since ${dateLabel(c.since)}` : '';
  if (!c.since) { $('fcChanges').innerHTML = `<p class="fc-empty">${esc(c.note || 'No snapshot yet.')}</p>`; return; }
  $('fcChanges').innerHTML = c.changes.length ? `<ul>${c.changes.slice(0, 40).map(x => {
    const [icon, tone] = CHANGE_ICON[x.type] || ['fa-circle', 'info'];
    return `<li class="${tone}"><i class="fa-solid ${icon}"></i><div>${x.exists ? `<a href="/deals?deal=${x.deal_id}">${esc(x.name)}</a>` : esc(x.name)}
      <small>${esc(CHANGE_LABEL[x.type] || x.type)} · ${esc(x.text)}${x.amount_usd ? ` · ${usd(x.amount_usd)}` : ''}</small></div></li>`;
  }).join('')}</ul>` : '<p class="fc-empty">Nothing moved in this quarter since the last snapshot.</p>';
}

async function load() {
  try {
    const [d, c] = await Promise.all([
      api(`/api/forecast?${query({ group_by: st.groupBy })}`),
      api(`/api/forecast/changes?${query()}`),
    ]);
    st.data = d;
    if (st.dealFilter && !d.rows.some(r => String(r.key) === st.dealFilter.row)) st.dealFilter = null;
    const p = st.meta.periods.find(x => x.key === d.period);
    $('fcSub').textContent = `${p ? p.label : d.period} · ${d.totals.deals} deal${d.totals.deals === 1 ? '' : 's'} closing in the quarter · all amounts in USD`;
    renderKpis(d.totals);
    renderRollup(d);
    renderDeals(d);
    renderChanges(c);
    const warn = [...d.warnings];
    if (st.meta.can_edit_fx && st.meta.fx.some(r => r.source === 'default')) warn.push('Exchange rates are starting values — set real rates under Exchange rates.');
    $('fcBanner').hidden = !warn.length;
    $('fcBanner').innerHTML = warn.map(w => `<div><i class="fa-solid fa-triangle-exclamation"></i> ${esc(w)}</div>`).join('');
    const url = new URL(location.href);
    url.search = query({ group_by: st.groupBy }).toString();
    history.replaceState(null, '', url);
  } catch (err) { $('fcSub').textContent = err.message; }
}

// ── Targets & FX dialogs ─────────────────────────────────────────────────────
function openModal(title, body) {
  $('fcModalTitle').textContent = title;
  $('fcModalBody').innerHTML = body;
  $('fcModal').hidden = false;
  const first = $('fcModalBody').querySelector('input:not([disabled])');
  if (first) first.focus();
}

async function openTargets() {
  try {
    const t = await api(`/api/forecast/targets?period=${encodeURIComponent($('fcPeriod').value)}`);
    const row = (kind, id, name, amount, editable) => `<tr><td>${esc(name)}</td><td class="num">
      <input type="number" min="0" step="10000" value="${amount ?? ''}" placeholder="Not set" data-target-kind="${kind}" data-target-id="${id}"
        ${editable ? '' : 'disabled title="You can set targets only for your team"'} aria-label="Target for ${esc(name)}"></td></tr>`;
    openModal(`Targets · ${t.period}`, `<p class="fc-muted">Quarterly targets in USD. Set them per rep or per business line; changes save as you leave each box.</p>
      <table class="fc-table"><thead><tr><th scope="col">Rep</th><th scope="col" class="num">Target (USD)</th></tr></thead>
        <tbody>${t.users.map(u => row('user', u.user_id, u.name, u.amount_usd, u.editable)).join('') || '<tr><td colspan="2">No reps</td></tr>'}</tbody></table>
      <table class="fc-table" style="margin-top:12px"><thead><tr><th scope="col">Business line</th><th scope="col" class="num">Target (USD)</th></tr></thead>
        <tbody>${t.business_lines.map(b => row('bl', b.business_line_id, b.name, b.amount_usd, b.editable)).join('')}</tbody></table>`);
  } catch (err) { showToast(err.message); }
}

function openFx() {
  openModal('Exchange rates', `<p class="fc-muted">1 unit of each currency in USD. Changing a rate recalculates every deal in that currency.</p>
    <table class="fc-table"><thead><tr><th scope="col">Currency</th><th scope="col" class="num">USD per unit</th><th scope="col">Updated</th></tr></thead><tbody>
    ${st.meta.fx.map(r => `<tr><td>${esc(r.currency)}${r.source === 'default' ? ' <span class="fc-cat pipeline">starting value</span>' : ''}</td>
      <td class="num"><input type="number" min="0" step="0.0001" value="${r.usd_rate}" data-fx="${esc(r.currency)}" ${r.currency === 'USD' ? 'disabled' : ''}
        aria-label="${esc(r.currency)} rate"></td><td>${esc(dateLabel(r.updated_at))}</td></tr>`).join('')}
    <tr><td><input maxlength="3" placeholder="AED" id="fcFxNew" aria-label="New currency code" style="width:70px;text-transform:uppercase"></td>
      <td class="num"><input type="number" min="0" step="0.0001" id="fcFxNewRate" placeholder="0.27" aria-label="New currency rate"></td>
      <td><button type="button" class="dl-btn" data-fx-add>Add</button></td></tr></tbody></table>`);
}

async function saveFx(currency, rate) {
  try {
    const r = await api('/api/forecast/fx', { method: 'PUT', body: { currency, usd_rate: rate } });
    showToast(`${currency} saved · ${r.deals_recomputed} deal${r.deals_recomputed === 1 ? '' : 's'} recalculated`);
    st.meta = await api('/api/forecast/meta');
    load();
  } catch (err) { showToast(err.message); }
}

function wire() {
  ['fcPeriod', 'fcBusinessLine', 'fcOwner'].forEach(id => $(id).addEventListener('change', () => { st.dealFilter = null; load(); }));
  document.querySelector('.fc-seg').addEventListener('click', (e) => {
    const b = e.target.closest('[data-group]');
    if (!b) return;
    st.groupBy = b.dataset.group;
    st.dealFilter = null;
    document.querySelectorAll('.fc-seg button').forEach(x => x.classList.toggle('active', x === b));
    load();
  });
  $('fcRollup').addEventListener('click', (e) => {
    const b = e.target.closest('.fc-cell');
    if (!b) return;
    st.dealFilter = { row: b.dataset.row, bucket: b.dataset.bucket };
    renderDeals(st.data);
    $('fcDeals').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  $('fcDealFilter').addEventListener('click', (e) => { if (e.target.closest('[data-clear-filter]')) { st.dealFilter = null; renderDeals(st.data); } });
  $('fcDeals').addEventListener('change', async (e) => {
    const sel = e.target.closest('.fc-cat-select');
    if (!sel) return;
    try { await api(`/api/deals/${sel.dataset.deal}`, { method: 'PATCH', body: { forecast_category: sel.value } }); showToast('Category updated'); load(); }
    catch (err) { showToast(err.message); load(); }
  });
  $('fcExport').addEventListener('click', () => downloadFile(`/api/forecast/export?${query({ group_by: st.groupBy })}`, 'forecast.xlsx').catch(err => showToast(err.message)));
  $('fcTargetsBtn').addEventListener('click', openTargets);
  $('fcFxBtn').addEventListener('click', openFx);
  $('fcModal').addEventListener('click', (e) => { if (e.target.closest('[data-close-modal]') || e.target === $('fcModal')) $('fcModal').hidden = true; });
  $('fcModal').addEventListener('change', async (e) => {
    const t = e.target;
    if (t.dataset.targetKind) {
      const body = { period: $('fcPeriod').value, amount_usd: t.value === '' ? null : Number(t.value) };
      body[t.dataset.targetKind === 'user' ? 'user_id' : 'business_line_id'] = Number(t.dataset.targetId);
      try { await api('/api/forecast/targets', { method: 'PUT', body }); showToast('Target saved'); load(); }
      catch (err) { showToast(err.message); }
    } else if (t.dataset.fx && t.value) {
      saveFx(t.dataset.fx, Number(t.value));
    }
  });
  $('fcModal').addEventListener('click', (e) => {
    if (!e.target.closest('[data-fx-add]')) return;
    const cur = $('fcFxNew').value.trim().toUpperCase();
    const rate = Number($('fcFxNewRate').value);
    if (!/^[A-Z]{3}$/.test(cur) || !(rate > 0)) { showToast('Enter a 3-letter currency code and a rate above 0'); return; }
    saveFx(cur, rate).then(openFx);
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !$('fcModal').hidden) $('fcModal').hidden = true; });
}

async function init() {
  try {
    const [meta, crm] = await Promise.all([api('/api/forecast/meta'), api('/api/crm/meta')]);
    st.meta = meta;
    st.me = crm.me;
  } catch (err) { $('fcSub').textContent = err.message; return; }
  const p = new URLSearchParams(location.search);
  $('fcPeriod').innerHTML = st.meta.periods.map(x => `<option value="${x.key}"${x.current ? ' selected' : ''}>${esc(x.label)}${x.current ? ' — current' : ''}</option>`).join('');
  $('fcBusinessLine').innerHTML += st.meta.business_lines.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
  $('fcOwner').innerHTML += st.meta.owners.map(o => `<option value="${o.id}">${esc(o.name)}</option>`).join('');
  if (p.get('period') && st.meta.periods.some(x => x.key === p.get('period'))) $('fcPeriod').value = p.get('period');
  if (p.get('business_line_id')) $('fcBusinessLine').value = p.get('business_line_id');
  if (p.get('owner_id')) $('fcOwner').value = p.get('owner_id');
  if (['owner', 'business_line', 'stage', 'category'].includes(p.get('group_by'))) {
    st.groupBy = p.get('group_by');
    document.querySelectorAll('.fc-seg button').forEach(x => x.classList.toggle('active', x.dataset.group === st.groupBy));
  }
  $('fcTargetsBtn').hidden = !st.meta.can_edit_targets;
  $('fcFxBtn').hidden = !st.meta.can_edit_fx;
  wire();
  load();
}

initThemeToggle();
initTopbarAuth().then((user) => {
  if (!user) { document.body.style.display = 'none'; location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); return; }
  init();
});
