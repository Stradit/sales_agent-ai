// Forecast charts: week-by-week trend (FC-05) and the Commit-forecast bridge since the last
// weekly snapshot. Data: GET /api/forecast/trend. Plain SVG / HTML, no chart library.
import { esc } from '../utils.js';

const SERIES = [
  { key: 'closed', label: 'Won', cls: 'won' },
  { key: 'best_case_forecast', label: 'Best case', cls: 'best' },
  { key: 'commit_forecast', label: 'Commit', cls: 'commit' },
];
const day = (x) => (x instanceof Date ? x : new Date(`${String(x).slice(0, 10)}T00:00:00`));
const short = (x) => day(x).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

// Four axis steps of a round size (1 / 2 / 2.5 / 5 × 10ⁿ), so tick labels read $500K, $1M, $1.5M…
function niceTicks(v) {
  if (v <= 0) return [0, 1];
  const raw = v / 4, p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * p).find(m => m >= raw);
  const n = Math.ceil(v / step);
  return Array.from({ length: n + 1 }, (_, i) => i * step);
}

// points: [{date, closed, commit_forecast, best_case_forecast, live?}], sorted by date.
export function renderTrend(el, { start, end, points, target, usd }) {
  if (!points.length) {
    el.innerHTML = '<p class="fc-empty">The trend fills in week by week: a snapshot of every deal is taken each Monday.</p>';
    return;
  }
  const W = Math.max(320, el.clientWidth || 600), H = 240;
  const m = { l: 52, r: 86, t: 14, b: 26 };
  const x0 = day(start).getTime(), x1 = Math.max(day(end).getTime(), x0 + 1);
  const ticks = niceTicks(Math.max(target || 0, ...points.map(p => p.best_case_forecast)) * 1.05);
  const yMax = ticks[ticks.length - 1];
  const x = (d) => m.l + ((day(d).getTime() - x0) / (x1 - x0)) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - v / yMax) * (H - m.t - m.b);

  // Month gridlines on the x axis
  const months = [];
  for (let d = day(start); d <= day(end); d = new Date(d.getFullYear(), d.getMonth() + 1, 1)) months.push(d);

  // End-of-line labels, nudged apart so they never overlap
  const last = points[points.length - 1];
  const labels = SERIES.map(s => ({ ...s, v: last[s.key], y: y(last[s.key]) })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < labels.length; i++) labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 14);

  const path = (key) => points.map((p, i) => `${i ? 'L' : 'M'}${x(p.date).toFixed(1)},${y(p[key]).toFixed(1)}`).join('');
  el.innerHTML = `<div class="fc-legend fc-chart-legend">${SERIES.slice().reverse().map(s => `<span><b class="${s.cls}"></b>${s.label}</span>`).join('')}
      ${target ? '<span><b class="tgt-dash"></b>Target</span>' : ''}</div>
    <div class="fc-chart-box"><svg class="fc-chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
      aria-label="Forecast trend: ${points.map(p => `${short(p.date)} won ${usd(p.closed)}, commit ${usd(p.commit_forecast)}, best case ${usd(p.best_case_forecast)}`).join('; ')}">
      ${ticks.map(v => `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>
        <text class="axis" x="${m.l - 6}" y="${y(v) + 3}" text-anchor="end">${esc(usd(v))}</text>`).join('')}
      ${months.map(d => `<text class="axis" x="${x(d)}" y="${H - 8}">${esc(d.toLocaleDateString(undefined, { month: 'short' }))}</text>`).join('')}
      ${target ? `<line class="target" x1="${m.l}" x2="${W - m.r}" y1="${y(target)}" y2="${y(target)}"/>
        <text class="lbl" x="${W - m.r + 6}" y="${y(target) - 4}">Target</text>` : ''}
      ${SERIES.map(s => `<path class="line ${s.cls}" d="${path(s.key)}"/>`).join('')}
      ${points.map(p => SERIES.map(s => `<circle class="dot ${s.cls}${p.live ? ' live' : ''}" cx="${x(p.date)}" cy="${y(p[s.key])}" r="4"/>`).join('')).join('')}
      ${labels.map(l => `<text class="lbl" x="${x(last.date) + 8}" y="${l.y + 4}">${esc(l.label)} ${esc(usd(l.v))}</text>`).join('')}
      <line class="cross" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" visibility="hidden"/>
      ${points.map((p, i) => {
        const prev = i ? x(points[i - 1].date) : m.l, next = i < points.length - 1 ? x(points[i + 1].date) : W - m.r;
        const a = (prev + x(p.date)) / 2, b = (x(p.date) + next) / 2;
        return `<rect class="hit" data-i="${i}" x="${i ? a : m.l}" y="${m.t}" width="${Math.max(8, (i < points.length - 1 ? b : W - m.r) - (i ? a : m.l))}" height="${H - m.t - m.b}"/>`;
      }).join('')}
    </svg><div class="fc-tip" hidden></div></div>
    <details class="fc-chart-table"><summary>Show as table</summary><table class="fc-table"><thead><tr><th scope="col">Week of</th>
      <th scope="col" class="num">Won</th><th scope="col" class="num">Commit</th><th scope="col" class="num">Best case</th></tr></thead>
      <tbody>${points.map(p => `<tr><td>${esc(p.live ? 'Today' : short(p.date))}</td><td class="num">${usd(p.closed)}</td>
        <td class="num">${usd(p.commit_forecast)}</td><td class="num">${usd(p.best_case_forecast)}</td></tr>`).join('')}</tbody></table></details>`;

  const svg = el.querySelector('svg'), tip = el.querySelector('.fc-tip'), cross = svg.querySelector('.cross');
  svg.addEventListener('pointermove', (e) => {
    const hit = e.target.closest('.hit');
    if (!hit) return;
    const p = points[Number(hit.dataset.i)], px = x(p.date);
    cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('visibility', 'visible');
    tip.innerHTML = `<b>${esc(p.live ? 'Today' : `Week of ${short(p.date)}`)}</b>
      ${SERIES.slice().reverse().map(s => `<div><i class="${s.cls}"></i>${s.label}<span>${esc(usd(p[s.key]))}</span></div>`).join('')}
      ${target ? `<div><i class="tgt"></i>Target<span>${esc(usd(target))}</span></div>` : ''}`;
    tip.hidden = false;
    const box = el.querySelector('.fc-chart-box').getBoundingClientRect(), scale = box.width / W;
    tip.style.left = `${Math.min(px * scale + 12, box.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${m.t * scale}px`;
  });
  svg.addEventListener('pointerleave', () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); });
}

// Horizontal waterfall: Commit forecast at the last snapshot → each reason it moved → now.
export function renderBridge(el, bridge, usd) {
  if (!bridge) { el.innerHTML = '<p class="fc-empty">No weekly snapshot yet. The first one is taken automatically.</p>'; return; }
  const rows = [{ label: `Commit forecast, ${short(bridge.since)}`, total: true, from: 0, to: bridge.opening }];
  let run = bridge.opening;
  bridge.steps.forEach(st => { rows.push({ ...st, from: run, to: run + st.amount }); run += st.amount; });
  rows.push({ label: 'Commit forecast now', total: true, from: 0, to: bridge.closing });
  const max = Math.max(...rows.map(r => Math.max(r.from, r.to)), 1);
  const pctOf = (v) => `${(Math.max(0, v) / max) * 100}%`;
  const delta = bridge.closing - bridge.opening;
  el.innerHTML = `<p class="fc-bridge-sum">${bridge.steps.length
      ? `${delta >= 0 ? 'Up' : 'Down'} <b>${esc(usd(Math.abs(delta)))}</b> since ${esc(short(bridge.since))}.`
      : `No change since ${esc(short(bridge.since))}.`}</p>
    <ul class="fc-bridge">${rows.map((r, i) => {
      const lo = Math.min(r.from, r.to), hi = Math.max(r.from, r.to);
      const tone = r.total ? 'total' : r.amount > 0 ? 'up' : 'down';
      const deals = (r.deals || []).slice(0, 5);
      return `<li class="${tone}" ${deals.length ? `data-i="${i}" tabindex="0"` : ''}>
        <span class="fc-bridge-label">${esc(r.label)}${r.deals ? `<small>${r.deals.length} deal${r.deals.length === 1 ? '' : 's'}</small>` : ''}</span>
        <span class="fc-bridge-track"><span class="fc-bridge-bar ${tone}" style="left:${pctOf(lo)};width:${pctOf(hi - lo)}"></span></span>
        <span class="fc-bridge-val">${r.total ? esc(usd(r.to)) : `${r.amount > 0 ? '+' : '−'}${esc(usd(Math.abs(r.amount)))}`}</span>
        ${deals.length ? `<div class="fc-bridge-deals">${deals.map(d => `<div>${d.exists ? `<a href="/deals?deal=${d.id}">${esc(d.name)}</a>` : esc(d.name)}
          <span>${d.amount_usd > 0 ? '+' : '−'}${esc(usd(Math.abs(d.amount_usd)))}</span></div>`).join('')}
          ${r.deals.length > 5 ? `<div class="fc-muted">and ${r.deals.length - 5} more</div>` : ''}</div>` : ''}</li>`;
    }).join('')}</ul>`;
}
