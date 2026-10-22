// Forecast page (/forecast): fiscal-quarter roll-up by owner / business line / stage / category,
// progress to target, week-over-week changes, deal list with inline category, targets and
// exchange-rate editors. API: apps/sales_crm/forecast.py. No AI requests.
import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { showToast } from '../toast.js';
import { esc } from '../utils.js';
import { downloadFile } from '../download.js';
import { renderTrend, renderBridge } from './charts.js';

const $ = (id) => document.getElementById(id);
const CAT_LABEL = { closed: 'Closed won', commit: 'Commit', best_case: 'Best case', pipeline: 'Pipeline', omitted: 'Omitted' };
const CHANGE_ICON = {
  won: ['fa-trophy', 'good'], lost: ['fa-circle-xmark', 'bad'], slipped_out: ['fa-calendar-xmark', 'bad'], new: ['fa-plus', 'info'],
  category: ['fa-tag', 'info'], advanced: ['fa-arrow-right', 'good'], moved_back: ['fa-arrow-left', 'bad'],
  slipped: ['fa-calendar-minus', 'warn'], pulled_in: ['fa-calendar-plus', 'good'], amount: ['fa-dollar-sign', 'info'], removed: ['fa-trash', 'bad'],
};
const CHANGE_LABEL = { won: 'Won', lost: 'Lost', slipped_out: 'Slipped out of quarter', new: 'New', category: 'Category', advanced: 'Advanced',
  moved_back: 'Moved back', slipped: 'Close date moved', pulled_in: 'Pulled in', amount: 'Amount', removed: 'Removed' };

const st = { meta: null, data: null, trend: null, groupBy: 'owner', dealFilter: null, me: null, exec: false, prevGroupBy: 'owner' };

// Plain-language meaning of every forecast term, shown by the ⓘ buttons (one floating tooltip, see wireTips).
const TERMS = {
  target: 'The amount to win this quarter. Set per rep or per business line under Targets.',
  won: 'Deals marked Won with a close date in this quarter: money already in.',
  commit: 'Open deals the rep is confident will close this quarter.',
  best_case: 'Open deals that could close this quarter if things go well.',
  pipeline: 'Other open deals closing this quarter that nobody has called Commit or Best case yet.',
  commit_forecast: 'Won + Commit: the number we expect to land.',
  best_case_forecast: 'Won + Commit + Best case: the most we can realistically land.',
  open_pipeline: 'Every open deal closing this quarter: Commit + Best case + Pipeline.',
  weighted: "Each open deal's amount × its chance of winning (its stage probability), added up. A statistical view of the pipeline.",
  coverage: 'Open pipeline ÷ the amount still to win. Below 3× is risky, because many open deals never close.',
  status: 'On track: Won + Commit reaches the target. At risk: Best-case deals are needed too. Off track: short even if every Commit and Best-case deal closes.',
  pace: 'Where wins would be by today if they came in evenly through the quarter: target × share of the quarter gone.',
  quality: 'Open deals in this quarter with missing or stale information. Fixing them makes the forecast more reliable.',
  trend: 'Won, Commit forecast and Best-case forecast as they stood each Monday (weekly snapshot), plus today.',
  bridge: 'How the Commit forecast (Won + Commit) changed since the last Monday snapshot, split by reason. Hover a step to see its deals.',
  key_deals: 'The largest Commit and Best-case deals in the quarter, with the warnings worth raising in a 1:1.',
};
const info = (k) => `<button type="button" class="fc-info" data-term="${k}" aria-label="${esc(TERMS[k])}"><i class="fa-solid fa-circle-info"></i></button>`;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401) { location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); throw new Error('unauthenticated'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

// Rounded for reading ($1.95M, $770K, $9,500); compact=false gives the exact amount (deal rows).
function usd(v, compact = true) {
  if (v == null) return '—';
  return new Intl.NumberFormat(undefined, compact && Math.abs(v) >= 10000
    ? { style: 'currency', currency: 'USD', notation: 'compact', maximumSignificantDigits: 3 }
    : { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(v);
}
const pct = (v) => (v == null ? '—' : `${Math.abs(v) < 10 ? Math.round(v * 10) / 10 : Math.round(v)}%`);
const cov = (v) => (v == null ? '—' : `${v.toFixed(1)}×`);
const dateLabel = (d) => (d ? new Date(`${String(d).slice(0, 10)}T00:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—');

function query(extra = {}) {
  const p = new URLSearchParams({ period: $('fcPeriod').value, ...extra });
  if ($('fcBusinessLine').value) p.set('business_line_id', $('fcBusinessLine').value);
  if ($('fcOwner').value) p.set('owner_id', $('fcOwner').value);
  return p;
}

// ── Render ───────────────────────────────────────────────────────────────────
// On track = Commit alone reaches the target; at risk = it takes Best case too; off track = even that falls short.
function status(t) {
  if (!t.target) return { tone: 'none', label: 'No target', rank: 3 };
  if (t.closed >= t.target) return { tone: 'good', label: 'Target reached', rank: 2 };
  if (t.commit_forecast >= t.target) return { tone: 'good', label: 'On track', rank: 2 };
  if (t.best_case_forecast >= t.target) return { tone: 'warn', label: 'At risk', rank: 1 };
  return { tone: 'bad', label: 'Off track', rank: 0 };
}
const statusChip = (s) => `<span class="fc-status ${s.tone}">${esc(s.label)}</span>`;

// Where we are in the quarter: null before it starts / after it ends.
function pace(d) {
  const day = (x) => new Date(`${String(x).slice(0, 10)}T00:00:00`);
  const start = day(d.start), end = day(d.end), now = new Date();
  now.setHours(0, 0, 0, 0);
  if (now < start || now > end) return null;
  const total = Math.round((end - start) / 864e5) + 1;
  const n = Math.round((now - start) / 864e5) + 1;
  const weeks = Math.round(total / 7);                 // a 90–92-day quarter is 13 weeks
  return { week: Math.min(weeks, Math.ceil(n / 7)), weeks, gonePct: Math.round((n / total) * 100) };
}

function renderVerdict(d) {
  const t = d.totals, s = status(t), p = pace(d);
  const lines = [];
  if (!t.target) {
    lines.push(`${usd(t.closed)} won so far. If every Commit deal closes: ${usd(t.commit_forecast)}; with Best case too: ${usd(t.best_case_forecast)}.`);
    lines.push(st.meta.can_edit_targets ? 'Set a target under Targets to see whether the quarter is on track.' : 'No target is set for this view.');
  } else {
    lines.push(`<b>${usd(t.closed)}</b> won of a <b>${usd(t.target)}</b> target (${pct(t.attainment_pct)}).`);
    if (t.closed >= t.target) lines.push(`Target reached, ${usd(t.closed - t.target)} above it.`);
    else if (s.label === 'On track') lines.push(`If every Commit deal closes we land at ${usd(t.commit_forecast)}, ${usd(t.commit_forecast - t.target)} above target.`);
    else if (s.label === 'At risk') lines.push(`Commit deals alone land at ${usd(t.commit_forecast)}, <b>${usd(t.target - t.commit_forecast)} short</b>, so Best-case deals have to close too.`);
    else lines.push(`Even if every Commit and Best-case deal closes we land at ${usd(t.best_case_forecast)}, <b>${usd(t.target - t.best_case_forecast)} short</b>.`);
    if (t.gap) {
      lines.push(t.open_pipeline ? `Open pipeline covers the remaining ${usd(t.gap)} ${cov(t.coverage)} (3× is healthy).`
        : `There is no open pipeline left to close the remaining ${usd(t.gap)}.`);
    }
    if (p) {
      const ahead = (t.attainment_pct || 0) >= p.gonePct;
      lines.push(`<b>${ahead ? 'Ahead of' : 'Behind'} pace</b> in week ${p.week} of ${p.weeks}.`);
    }
  }
  $('fcVerdict').className = `fc-verdict ${s.tone}`;
  $('fcVerdict').innerHTML = `${statusChip(s)}<p>${lines.join(' ')}</p>`;
}

// Linear pace: the share of the target that "should" be won by today if wins came in evenly.
function paceGap(t, p) {
  if (!t.target || !p) return null;
  const expected = t.target * (p.gonePct / 100);
  return { expected, diff: t.closed - expected };
}

function renderKpis(d) {
  const t = d.totals, p = pace(d), pg = paceGap(t, p);
  const cards = [
    ['Target', usd(t.target), t.target == null ? 'Not set' : `${pct(t.attainment_pct)} won`, 'fa-bullseye', 'target'],
    ['Won', usd(t.closed), t.gap != null ? `${usd(t.gap)} to go` : '', 'fa-trophy', 'won'],
    ['Commit forecast', usd(t.commit_forecast), 'Won + Commit', 'fa-lock', 'commit_forecast'],
    ['Best-case forecast', usd(t.best_case_forecast), 'Won + Commit + Best case', 'fa-arrow-trend-up', 'best_case_forecast'],
    ['Open pipeline', usd(t.open_pipeline), t.coverage != null ? `${cov(t.coverage)} coverage of the gap` : `${t.deals} deal${t.deals === 1 ? '' : 's'}`, 'fa-layer-group', 'open_pipeline'],
    ['Weighted', usd(t.weighted), 'Amount × chance of winning', 'fa-scale-balanced', 'weighted'],
  ];
  $('fcKpis').innerHTML = cards.map(([label, value, sub, icon, term]) => `<div class="fc-kpi"><span><i class="fa-solid ${icon}"></i> ${label} ${info(term)}</span>
    <strong>${esc(value)}</strong><small>${esc(sub)}</small></div>`).join('');
  const target = t.target || 0;
  const scale = Math.max(target, t.best_case_forecast + t.pipeline, 1);
  const w = (v) => `${Math.max(0, Math.min(100, (v / scale) * 100))}%`;
  $('fcBar').innerHTML = `<div class="fc-bar" role="img" aria-label="Won ${usd(t.closed)}, commit ${usd(t.commit)}, best case ${usd(t.best_case)}, pipeline ${usd(t.pipeline)}${target ? `, target ${usd(target)}` : ''}">
      <span class="closed" style="width:${w(t.closed)}"></span><span class="commit" style="width:${w(t.commit)}"></span>
      <span class="best" style="width:${w(t.best_case)}"></span><span class="pipe" style="width:${w(t.pipeline)}"></span>
      ${target ? `<i class="fc-target" style="left:${w(target)}" title="Target ${usd(target)}"></i>` : ''}
      ${pg ? `<i class="fc-pace" style="left:${w(pg.expected)}" title="Expected by today at an even pace: ${usd(pg.expected)}"></i>` : ''}</div>
    <div class="fc-legend"><span><b class="closed"></b>Won</span><span><b class="commit"></b>Commit</span><span><b class="best"></b>Best case</span>
      <span><b class="pipe"></b>Pipeline</span>${target ? '<span><b class="tgt"></b>Target</span>' : ''}${pg ? `<span><b class="pace"></b>Expected by today ${info('pace')}</span>` : ''}</div>
    ${pg ? `<p class="fc-pace-line ${pg.diff >= 0 ? 'good' : 'bad'}"><i class="fa-solid ${pg.diff >= 0 ? 'fa-person-running' : 'fa-hourglass-half'}"></i>
      <span><b>Pace:</b> week ${p.week} of ${p.weeks}, ${p.gonePct}% of the quarter gone. At an even pace ${usd(pg.expected)} would be won by today;
      ${usd(t.closed)} is won, so the team is <b>${usd(Math.abs(pg.diff))} ${pg.diff >= 0 ? 'ahead of' : 'behind'} pace</b>.</span></p>` : ''}`;
}

function cell(row, bucket, value) {
  if (!value) return `<td class="num fc-zero">${usd(value)}</td>`;
  if (st.exec) return `<td class="num">${usd(value)}</td>`;
  return `<td class="num"><button type="button" class="fc-cell" data-row="${esc(String(row.key))}" data-bucket="${bucket}"
    title="Show these deals">${usd(value)}</button></td>`;
}

// Small closed / commit / best case / pipeline bar against the target line (same scale rule as the main bar).
function miniBar(r, p) {
  const scale = Math.max(r.target || 0, r.best_case_forecast + r.pipeline, 1);
  const w = (v) => `${Math.max(0, Math.min(100, (v / scale) * 100))}%`;
  const pg = paceGap(r, p);
  return `<div class="fc-bar fc-bar-mini" role="img" aria-label="Won ${usd(r.closed)}, commit ${usd(r.commit)}, best case ${usd(r.best_case)}${r.target ? `, target ${usd(r.target)}` : ''}">
    <span class="closed" style="width:${w(r.closed)}"></span><span class="commit" style="width:${w(r.commit)}"></span>
    <span class="best" style="width:${w(r.best_case)}"></span><span class="pipe" style="width:${w(r.pipeline)}"></span>
    ${r.target ? `<i class="fc-target" style="left:${w(r.target)}"></i>` : ''}
    ${pg ? `<i class="fc-pace" style="left:${w(pg.expected)}"></i>` : ''}</div>`;
}

// "$40K behind pace" under a rep's status chip (current quarter, rep with a target, target not reached yet).
function paceNote(r, p) {
  const pg = paceGap(r, p);
  if (!pg || r.closed >= r.target) return '';
  return `<small class="${pg.diff >= 0 ? 'fc-ahead' : 'fc-behind'}">${usd(Math.abs(pg.diff))} ${pg.diff >= 0 ? 'ahead of' : 'behind'} pace</small>`;
}

// Group by owner or business line: one row each with a status and a progress bar, the ones that need help first.
function renderScorecard(d) {
  const rows = d.rows.map(r => ({ ...r, s: status(r) }))
    .sort((a, b) => a.s.rank - b.s.rank || (b.gap || 0) - (a.gap || 0) || (b.best_case_forecast + b.pipeline) - (a.best_case_forecast + a.pipeline));
  const t = d.totals, ts = status(t), p = pace(d);
  const target = (r) => (r.target != null ? `${usd(r.target)}<small>${r.gap ? `${usd(r.gap)} to go` : 'reached'}</small>` : '—');
  const coverage = (r) => `<td class="num ${r.coverage != null && r.coverage < 3 ? 'fc-low' : ''}">${cov(r.coverage)}</td>`;
  $('fcRollup').innerHTML = `<table class="fc-table fc-score"><thead><tr><th scope="col">${d.group_by === 'owner' ? 'Rep' : 'Business line'}</th>
      <th scope="col">Status ${info('status')}</th><th scope="col">Progress to target</th><th scope="col" class="num">Won ${info('won')}</th>
      <th scope="col" class="num">Commit ${info('commit')}</th><th scope="col" class="num">Best case ${info('best_case')}</th>
      <th scope="col" class="num">Pipeline ${info('pipeline')}</th><th scope="col" class="num">Target ${info('target')}</th>
      <th scope="col" class="num">Coverage ${info('coverage')}</th></tr></thead>
    <tbody>${rows.map(r => `<tr><th scope="row">${esc(r.label)}<small>${r.deals} deal${r.deals === 1 ? '' : 's'}</small></th>
      <td>${statusChip(r.s)}${paceNote(r, p)}</td><td class="fc-bar-cell">${miniBar(r, p)}</td>
      ${cell(r, 'closed', r.closed)}${cell(r, 'commit', r.commit)}${cell(r, 'best_case', r.best_case)}${cell(r, 'pipeline', r.pipeline)}
      <td class="num">${target(r)}</td>${coverage(r)}</tr>`).join('')}</tbody>
    <tfoot><tr><th scope="row">Total</th><td>${statusChip(ts)}${paceNote(t, p)}</td><td class="fc-bar-cell">${miniBar(t, p)}</td>
      <td class="num">${usd(t.closed)}</td><td class="num">${usd(t.commit)}</td><td class="num">${usd(t.best_case)}</td>
      <td class="num">${usd(t.pipeline)}</td><td class="num">${target(t)}</td>${coverage(t)}</tr></tfoot></table>
    <p class="fc-muted fc-score-key"><b>On track</b>: Won + Commit reaches the target · <b>At risk</b>: Best-case deals are needed too ·
      <b>Off track</b>: short even if every Commit and Best-case deal closes.${p ? ' The thin dark tick is where an even pace would be by today.' : ''}</p>`;
}

function renderRollup(d) {
  const rows = d.rows;
  if (!rows.length) { $('fcRollup').innerHTML = '<p class="fc-empty">No deals close in this quarter yet. Deals count here by their expected close date.</p>'; return; }
  if (d.group_by === 'owner' || d.group_by === 'business_line') { renderScorecard(d); return; }
  const t = d.totals;
  $('fcRollup').innerHTML = `<table class="fc-table"><thead><tr><th scope="col">${{ owner: 'Owner', business_line: 'Business line', stage: 'Stage', category: 'Category' }[d.group_by]}</th>
      <th scope="col" class="num">Won ${info('won')}</th><th scope="col" class="num">Commit ${info('commit')}</th>
      <th scope="col" class="num">Best case ${info('best_case')}</th><th scope="col" class="num">Pipeline ${info('pipeline')}</th>
      <th scope="col" class="num">Weighted ${info('weighted')}</th><th scope="col" class="num">Commit forecast ${info('commit_forecast')}</th>
      <th scope="col" class="num">Target ${info('target')}</th><th scope="col" class="num">Coverage ${info('coverage')}</th></tr></thead>
    <tbody>${rows.map(r => `<tr><th scope="row">${esc(r.label)}<small>${r.deals} deal${r.deals === 1 ? '' : 's'}</small></th>
      ${cell(r, 'closed', r.closed)}${cell(r, 'commit', r.commit)}${cell(r, 'best_case', r.best_case)}${cell(r, 'pipeline', r.pipeline)}
      <td class="num">${usd(r.weighted)}</td><td class="num"><strong>${usd(r.commit_forecast)}</strong></td>
      <td class="num">${r.target != null ? `${usd(r.target)}<small>${pct(r.attainment_pct)}</small>` : '—'}</td>
      <td class="num ${r.coverage != null && r.coverage < 3 ? 'fc-low' : ''}">${cov(r.coverage)}</td></tr>`).join('')}</tbody>
    <tfoot><tr><th scope="row">Total</th><td class="num">${usd(t.closed)}</td><td class="num">${usd(t.commit)}</td><td class="num">${usd(t.best_case)}</td>
      <td class="num">${usd(t.pipeline)}</td><td class="num">${usd(t.weighted)}</td><td class="num"><strong>${usd(t.commit_forecast)}</strong></td>
      <td class="num">${usd(t.target)}</td><td class="num">${cov(t.coverage)}</td></tr></tfoot></table>`;
}

function dealMatches(x) {
  const f = st.dealFilter;
  if (!f) return true;
  if (f.flag) return (x.flags || []).some(k => k.key === f.flag);
  const key = { owner: x.owner_user_id, business_line: x.business_line_id, stage: x.stage, category: x.bucket }[st.data.group_by];
  return String(key) === f.row && x.bucket === f.bucket;
}

function renderDeals(d) {
  const list = d.deals.filter(dealMatches);
  const canEdit = !(st.me && st.me.read_only);
  $('fcDealFilter').hidden = !st.dealFilter;
  if (st.dealFilter) {
    const row = d.rows.find(r => String(r.key) === st.dealFilter.row);
    $('fcDealFilter').innerHTML = `${st.dealFilter.flag ? esc(QUALITY.find(q => q.key === st.dealFilter.flag).label)
      : `${esc(row ? row.label : '')} · ${esc(CAT_LABEL[st.dealFilter.bucket])}`}
      <button type="button" class="dl-icon" data-clear-filter aria-label="Clear filter"><i class="fa-solid fa-xmark"></i></button>`;
  }
  $('fcDeals').innerHTML = list.length ? `<table class="fc-table fc-deals"><thead><tr><th scope="col">Deal</th><th scope="col">Owner</th><th scope="col">Stage</th>
      <th scope="col">Category</th><th scope="col" class="num">Amount (USD)</th><th scope="col" class="num">Prob.</th><th scope="col" class="num">Weighted</th>
      <th scope="col">Close</th></tr></thead><tbody>
    ${list.map(x => `<tr><td><a href="/deals?deal=${x.id}">${esc(x.name)}</a>${(x.flags || []).map(f =>
        ` <i class="fa-solid ${FLAG_ICON[f.key] || 'fa-triangle-exclamation'} fc-flag-icon" title="${esc(f.text)}" aria-label="${esc(f.text)}"></i>`).join('')}<small>${esc(x.account_name)}${x.business_line_id ? ` · ${esc(x.business_line_name)}` : ''}</small></td>
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

const FLAG_ICON = { close_passed: 'fa-calendar-xmark', early_commit: 'fa-seedling', no_next_step: 'fa-route', next_step_overdue: 'fa-route',
  quiet: 'fa-bell-slash', pushed: 'fa-calendar-minus' };

// Forecast quality: how many open deals in the quarter carry each warning. A count filters the deals table.
const QUALITY = [
  { key: 'undated', label: 'No close date', hint: 'not in any quarter' },
  { key: 'close_passed', label: 'Close date passed' },
  { key: 'early_commit', label: 'Commit still in Intro / Discovery' },
  { key: 'no_next_step', label: 'No next step' },
  { key: 'next_step_overdue', label: 'Next step overdue' },
  { key: 'quiet', label: 'No activity for 14+ days' },
  { key: 'pushed', label: 'Close date pushed out before' },
];

function renderQuality(d) {
  const open = d.deals.filter(x => x.flags);
  const counts = { undated: d.undated.length };
  open.forEach(x => x.flags.forEach(f => { counts[f.key] = (counts[f.key] || 0) + 1; }));
  const shown = QUALITY.filter(q => counts[q.key]);
  const clean = open.filter(x => !x.flags.length).length;
  $('fcQuality').innerHTML = `<span class="fc-quality-title"><i class="fa-solid fa-clipboard-check"></i> Forecast quality ${info('quality')}</span>
    ${shown.length ? shown.map(q => `<button type="button" class="fc-qchip${st.dealFilter && st.dealFilter.flag === q.key ? ' active' : ''}" data-quality="${q.key}"
        title="${q.key === 'undated' ? 'Show the open deals without an expected close date' : 'Show these deals'}">
        <i class="fa-solid ${FLAG_ICON[q.key] || 'fa-calendar-xmark'}"></i><b>${counts[q.key]}</b> ${esc(q.label)}</button>`).join('')
      + `<span class="fc-muted">${clean} of ${open.length} open deal${open.length === 1 ? '' : 's'} with no warnings</span>`
    : `<span class="fc-qchip ok"><i class="fa-solid fa-check"></i> Clean: every open deal has a close date, a next step and recent activity</span>`}`;
}

// The largest Commit / Best-case deals with the warnings worth raising in a 1:1.
function renderKeyDeals(d) {
  const list = d.key_deals || [];
  const flagged = list.filter(x => x.flags.length);
  const atRisk = flagged.reduce((sum, x) => sum + (x.amount_usd || 0), 0);
  $('fcKeySub').textContent = list.length
    ? `Largest Commit and Best-case deals · ${flagged.length} of ${list.length} with warnings${flagged.length ? ` (${usd(atRisk)})` : ''}` : '';
  $('fcKeyDeals').innerHTML = list.length ? `<table class="fc-table fc-key"><thead><tr><th scope="col">Deal</th><th scope="col">Owner</th>
      <th scope="col">Category</th><th scope="col" class="num">Amount (USD)</th><th scope="col">Close</th><th scope="col">Next step</th>
      <th scope="col">Warnings</th></tr></thead><tbody>
    ${list.map(x => `<tr><td><a href="/deals?deal=${x.id}">${esc(x.name)}</a><small>${esc(x.account_name)} · ${esc(x.stage_label)}</small></td>
      <td>${esc(x.owner_name)}</td><td><span class="fc-cat ${x.bucket}">${esc(x.category_label)}</span></td>
      <td class="num">${usd(x.amount_usd)}</td><td>${esc(dateLabel(x.close_date))}</td><td class="fc-next">${esc(x.next_step || '—')}</td>
      <td>${x.flags.length ? `<div class="fc-flags">${x.flags.map(f => `<span class="fc-flag"><i class="fa-solid ${FLAG_ICON[f.key] || 'fa-triangle-exclamation'}"></i>${esc(f.text)}</span>`).join('')}</div>`
        : '<span class="fc-flag ok"><i class="fa-solid fa-check"></i>No warnings</span>'}</td></tr>`).join('')}</tbody></table>`
    : '<p class="fc-empty">No Commit or Best-case deals close in this quarter. Mark the deals reps expect to close as Commit or Best case.</p>';
}

// Weekly snapshots plus, in the current quarter, a live "today" point from the roll-up.
function renderCharts() {
  const d = st.data, tr = st.trend;
  if (!d || !tr) return;
  const points = [...tr.points];
  const today = new Date().toLocaleDateString('en-CA');
  if (pace(d) && (!points.length || String(points[points.length - 1].date) < today)) {
    const t = d.totals;
    points.push({ date: today, closed: t.closed, commit_forecast: t.commit_forecast, best_case_forecast: t.best_case_forecast, live: true });
  }
  renderTrend($('fcTrend'), { start: d.start, end: d.end, points, target: d.totals.target, usd });
  renderBridge($('fcBridge'), tr.bridge, usd);
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
    const [d, c, tr] = await Promise.all([
      api(`/api/forecast?${query({ group_by: st.groupBy })}`),
      api(`/api/forecast/changes?${query()}`),
      api(`/api/forecast/trend?${query()}`),
    ]);
    st.data = d;
    st.trend = tr;
    if (st.dealFilter && st.dealFilter.row != null && !d.rows.some(r => String(r.key) === st.dealFilter.row)) st.dealFilter = null;
    const p = st.meta.periods.find(x => x.key === d.period);
    $('fcSub').textContent = `${p ? p.label : d.period} · ${d.totals.deals} deal${d.totals.deals === 1 ? '' : 's'} closing in the quarter · all amounts in USD`;
    renderVerdict(d);
    renderKpis(d);
    renderQuality(d);
    renderCharts();
    renderRollup(d);
    renderKeyDeals(d);
    renderDeals(d);
    renderChanges(c);
    const warn = [...d.warnings];
    if (st.meta.can_edit_fx && st.meta.fx.some(r => r.source === 'default')) warn.push('Exchange rates are starting values — set real rates under Exchange rates.');
    $('fcBanner').hidden = !warn.length;
    $('fcBanner').innerHTML = warn.map(w => `<div><i class="fa-solid fa-triangle-exclamation"></i> ${esc(w)}</div>`).join('');
    const url = new URL(location.href);
    url.search = query({ group_by: st.groupBy, ...(st.exec ? { view: 'exec' } : {}) }).toString();
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

// One floating tooltip for every ⓘ button: fixed-position, so table scroll boxes never clip it.
function wireTips() {
  const tip = document.createElement('div');
  tip.className = 'fc-tooltip';
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  document.body.appendChild(tip);
  const show = (b) => {
    tip.textContent = TERMS[b.dataset.term] || '';
    tip.hidden = false;
    const r = b.getBoundingClientRect();
    const left = Math.max(8, Math.min(r.left + r.width / 2 - tip.offsetWidth / 2, window.innerWidth - tip.offsetWidth - 8));
    const top = r.bottom + 6 + tip.offsetHeight > window.innerHeight ? r.top - tip.offsetHeight - 6 : r.bottom + 6;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  };
  const hide = () => { tip.hidden = true; };
  ['mouseover', 'focusin'].forEach(ev => document.addEventListener(ev, (e) => { const b = e.target.closest('.fc-info'); if (b) show(b); }));
  ['mouseout', 'focusout'].forEach(ev => document.addEventListener(ev, (e) => { if (e.target.closest('.fc-info')) hide(); }));
  document.addEventListener('click', (e) => { const b = e.target.closest('.fc-info'); if (b) { e.stopPropagation(); show(b); } }, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', hide, { passive: true });
}

// Leadership view: hides deal-level detail and groups by business line; kept in the URL (?view=exec) to share.
function setGroupBy(g) {
  st.groupBy = g;
  document.querySelectorAll('.fc-seg button').forEach(x => x.classList.toggle('active', x.dataset.group === g));
}

function setExec(on) {
  st.exec = on;
  document.body.classList.toggle('fc-exec', on);
  $('fcExecBtn').setAttribute('aria-pressed', String(on));
  $('fcExecBtn').classList.toggle('active', on);
  if (on) { st.prevGroupBy = st.groupBy; setGroupBy('business_line'); } else setGroupBy(st.prevGroupBy || 'owner');
  st.dealFilter = null;
}

function wire() {
  wireTips();
  $('fcExecBtn').addEventListener('click', () => { setExec(!st.exec); load(); });
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
    renderQuality(st.data);
    renderDeals(st.data);
    $('fcDeals').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  $('fcQuality').addEventListener('click', (e) => {
    const b = e.target.closest('[data-quality]');
    if (!b) return;
    if (b.dataset.quality === 'undated') {
      const det = $('fcUndated').querySelector('details');
      if (det) { det.open = true; det.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
      return;
    }
    st.dealFilter = st.dealFilter && st.dealFilter.flag === b.dataset.quality ? null : { flag: b.dataset.quality };
    renderQuality(st.data);
    renderDeals(st.data);
    if (st.dealFilter) $('fcDeals').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  $('fcDealFilter').addEventListener('click', (e) => { if (e.target.closest('[data-clear-filter]')) { st.dealFilter = null; renderQuality(st.data); renderDeals(st.data); } });
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
  let resizeTimer, chartWidth = 0;            // the trend chart is drawn at its box width: redraw when that changes
  new ResizeObserver(([e]) => {
    if (Math.round(e.contentRect.width) === chartWidth) return;
    chartWidth = Math.round(e.contentRect.width);
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderCharts, 150);
  }).observe($('fcTrend'));
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
  if (['owner', 'business_line', 'stage', 'category'].includes(p.get('group_by'))) setGroupBy(p.get('group_by'));
  if (p.get('view') === 'exec') { const g = st.groupBy; setExec(true); setGroupBy(g); st.prevGroupBy = 'owner'; }
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
