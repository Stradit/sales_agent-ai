// Copilot pipeline console (/copilot-pipeline). Three views:
//   Runs — start sync / full re-embed / eval-only (+ guardrails, golden-set eval) and watch
//          every stage live; history of past runs.
//   Embedding inspector — one document from source rows to chunks, vectors, Chroma entries
//          and nearest neighbours.
//   Query trace — one question through embed → vector leg → keyword leg → RRF → evidence.
// API: /api/copilotpipeline/* (apps/sales_copilot/pipeline.py), super_admin only.
import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { showToast } from '../toast.js';
import { esc } from '../utils.js';

const $ = (id) => document.getElementById(id);
const st = { overview: null, run: null, viewingId: null, mode: 'sync', poll: null, confirmReembed: false,
  docs: [], docId: null, openStages: new Set() };

const STAGE_ICON = { schema: 'fa-database', render: 'fa-file-lines', version: 'fa-code-branch', embed: 'fa-vector-square',
  index: 'fa-layer-group', verify: 'fa-circle-check', guardrails: 'fa-shield-halved', eval: 'fa-flask' };
const STATUS_ICON = { done: 'fa-check', warn: 'fa-exclamation', failed: 'fa-xmark', running: 'fa-spinner pl-spin', skipped: 'fa-minus' };
const MODE_LABEL = { sync: 'Incremental sync', reembed: 'Full re-embed', eval: 'Eval only' };
const MODE_HINT = {
  sync: 'Re-renders every source row, compares content hashes and embeds only chunks whose text changed. Unchanged text costs nothing.',
  reembed: 'Embeds every chunk again into a staging collection, then swaps it in. Chat keeps using the old vectors until the new set is complete. Takes a few minutes on CPU.',
  eval: 'No indexing. Runs the guardrail/safety suites and the golden-set eval against the current index.',
};

async function api(path, opts = {}) {
  const res = await fetch(`/api/copilotpipeline${path}`, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { location.replace(`/login?next=${encodeURIComponent(location.pathname)}`); throw new Error('unauthenticated'); }
  const data = await res.json().catch(() => ({}));
  if (res.status === 403) { $('plDenied').hidden = false; document.querySelectorAll('.pl-view').forEach(v => { v.hidden = true; }); throw new Error('forbidden'); }
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

// ── Formatting ───────────────────────────────────────────────────────────────
const num = (v) => (v == null ? '—' : typeof v === 'number' ? v.toLocaleString() : esc(v));
const pct = (v) => (v == null ? '—' : `${Math.round(v * 1000) / 10}%`);
function secs(s) {
  if (s == null) return '';
  if (s < 1) return `${Math.round(s * 1000)} ms`;
  if (s < 90) return `${s.toFixed(1)} s`;
  return `${Math.floor(s / 60)} m ${Math.round(s % 60)} s`;
}
function when(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  const diff = (Date.now() - d) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}
const pill = (status, label) => `<span class="pl-pill ${esc(status)}">${esc(label || status)}</span>`;
const metric = (label, value) => `<span class="pl-metric">${esc(label)}<b>${value}</b></span>`;
const label = (k) => k.replace(/_/g, ' ').replace(/\bat 10\b/, '@10');

function vecStrip(values) {
  if (!values || !values.length) return '';
  const max = Math.max(...values.map(Math.abs)) || 1;
  const cells = values.slice(0, 48).map((v) => {
    const a = Math.min(1, Math.abs(v) / max).toFixed(2);
    const color = v >= 0 ? `rgba(228, 101, 33,${a})` : `rgba(245,50,92,${a})`;
    return `<span style="background:${color}" title="${v}"></span>`;
  }).join('');
  return `<div class="pl-vec" aria-label="First ${Math.min(48, values.length)} vector dimensions">${cells}</div>
    <div class="pl-vec-legend"><span><i style="background:rgba(228, 101, 33,1)"></i>positive</span><span><i style="background:rgba(245,50,92,1)"></i>negative</span><span>first ${Math.min(48, values.length)} dims</span></div>`;
}
function simBar(s) {
  const w = Math.max(0, Math.min(1, s || 0)) * 100;
  return `<span class="pl-sim"><span style="width:${w}%"></span></span><span class="pl-mono">${(s ?? 0).toFixed(3)}</span>`;
}
function bars(obj) {
  const entries = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(e => e[1]));
  return `<div class="pl-bars">${entries.map(([k, v]) => `<div class="pl-bar"><span>${esc(label(k))}</span>
    <span class="pl-bar-track"><span style="width:${(v / max) * 100}%"></span></span><span class="num">${num(v)}</span></div>`).join('')}</div>`;
}

// ── Overview / KPIs ──────────────────────────────────────────────────────────
async function loadOverview() {
  const o = await api('/status');
  st.overview = o;
  const c = o.config;
  $('plConfig').innerHTML = `<code>${esc(c.embed_model)}</code> · ${c.dims} dims · chunker ${esc(c.chunker)} (${c.chunk_max_words} words, ${c.chunk_overlap_words} overlap) · Chroma ${esc(c.chroma)}`;
  const ix = o.index;
  const match = ix.chroma_matches_ledger;
  $('plKpis').innerHTML = [
    ['Documents', num(ix.documents_total), `${Object.keys(ix.documents_by_type || {}).length} types`],
    ['Distinct chunks', num(ix.chunks_distinct), `${num(ix.index_distinct_chunks)} indexed`],
    ['Chroma vectors', num(ix.chroma_vectors), match ? pill('ok', 'matches ledger') : pill('warn', `ledger ${num(ix.index_entries)}`)],
    ['Last sync', when(ix.last_sync_at), ix.last_sync ? `${num(ix.last_sync.index_added || 0)} added · ${secs(ix.last_sync.seconds)}` : ''],
    ['Background sync', o.sync.running ? pill('running', 'running') : pill('ok', 'idle'), o.sync.last_error ? `<span class="pl-pill failed">${esc(o.sync.last_error.slice(0, 40))}</span>` : `GC ${when(o.sync.last_gc)}`],
  ].map(([l, v, f]) => `<div class="pl-kpi"><div class="pl-kpi-label">${l}</div><div class="pl-kpi-value">${v}</div><div class="pl-kpi-foot">${f || ''}</div></div>`).join('');
  renderHistory(o.runs);
  if (o.current && (o.current.status === 'running' || !st.run)) showRun(o.current);
  else if (!st.run && o.runs.length) openRun(o.runs[0].id);
  else if (!st.run) renderRun(null);
}

function renderHistory(runs) {
  if (!runs.length) { $('plHistory').innerHTML = '<tr><td class="muted">No runs yet.</td></tr>'; return; }
  $('plHistory').innerHTML = `<thead><tr><th>Started</th><th>Mode</th><th>Status</th><th class="num">Docs</th><th class="num">Embedded</th><th class="num">Recall@10</th><th class="num">Time</th></tr></thead><tbody>${
    runs.map(r => `<tr class="clickable${r.id === st.viewingId ? ' selected' : ''}" data-run="${esc(r.id)}">
      <td>${when(r.started_at)}</td><td>${esc(MODE_LABEL[r.mode] || r.mode)}</td><td>${pill(r.status)}</td>
      <td class="num">${num(r.summary?.documents)}</td><td class="num">${num(r.summary?.embedded)}</td>
      <td class="num">${r.summary?.recall_at_10 != null ? pct(r.summary.recall_at_10) : '—'}</td><td class="num">${secs(r.summary?.seconds)}</td></tr>`).join('')}</tbody>`;
}

async function openRun(id) {
  try { showRun(await api(`/runs/${encodeURIComponent(id)}`)); }
  catch (e) { showToast(e.message); }
}

function showRun(run) {
  st.run = run;
  st.viewingId = run?.id || null;
  renderRun(run);
  document.querySelectorAll('#plHistory tr[data-run]').forEach(tr => tr.classList.toggle('selected', tr.dataset.run === st.viewingId));
  if (run?.status === 'running') startPolling();
}

// ── Run rendering ────────────────────────────────────────────────────────────
function renderRun(run) {
  const busy = run?.status === 'running';
  $('plStart').disabled = busy;
  if (!run) {
    $('plRunTitle').textContent = 'Run';
    $('plRunMeta').innerHTML = '<span>No run yet — start one on the left.</span>';
    $('plFlow').innerHTML = (st.overview?.stages || []).map(s => flowNode({ ...s, status: 'pending' })).join('');
    $('plStages').innerHTML = '';
    $('plLogs').textContent = '';
    return;
  }
  $('plRunTitle').textContent = `${MODE_LABEL[run.mode] || run.mode} run`;
  const opts = [run.options?.guardrails && 'guardrails', run.options?.eval && 'eval'].filter(Boolean).join(' + ');
  $('plRunMeta').innerHTML = `${pill(run.status)} <span>${esc(run.id)}</span> <span>started ${when(run.started_at)}</span>${opts ? `<span>· ${esc(opts)}</span>` : ''}${run.summary?.seconds != null && !busy ? `<span>· ${secs(run.summary.seconds)}</span>` : ''}`;
  $('plFlow').innerHTML = run.stages.map(flowNode).join('');
  $('plStages').innerHTML = run.stages.map(stageCard).join('');
  const logs = (run.logs || []).map(l => `${(l.t || '').slice(11, 19)}  ${l.line}`).join('\n');
  const pre = $('plLogs');
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 10;
  pre.textContent = logs;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function flowNode(s) {
  const icon = STATUS_ICON[s.status] || STAGE_ICON[s.key] || 'fa-circle';
  const time = s.status === 'running' ? 'running…' : s.seconds != null ? secs(s.seconds) : s.status === 'skipped' ? 'skipped' : '';
  return `<li class="${esc(s.status)}" data-stage="${esc(s.key)}" title="${esc(s.desc || '')}">
    <div class="pl-node"><i class="fa-solid ${icon}"></i></div>
    <div class="pl-flow-label">${esc(s.label)}</div><div class="pl-flow-time">${time}</div></li>`;
}

function stageCard(s) {
  const open = st.openStages.has(s.key) || s.status === 'running' || s.status === 'failed';
  const time = s.seconds != null ? secs(s.seconds) : s.status === 'running' && s.started_at ? `${secs((Date.now() - new Date(s.started_at)) / 1000)}…` : '';
  return `<details class="pl-stage ${esc(s.status)}" data-stage="${esc(s.key)}" ${open ? 'open' : ''}>
    <summary><i class="fa-solid fa-chevron-right pl-chev"></i><i class="fa-solid ${STAGE_ICON[s.key] || 'fa-circle'}" style="color:var(--brand)"></i>
      <span class="pl-stage-name">${esc(s.label)}</span><span class="pl-stage-desc">${esc(s.desc)}</span>
      <span class="pl-stage-time">${time}</span>${pill(s.status)}</summary>
    <div class="pl-stage-body">${s.error ? `<div class="pl-err">${esc(s.error)}</div>` : ''}${stageBody(s)}</div></details>`;
}

function stageBody(s) {
  const m = s.metrics || {};
  const d = s.detail || {};
  if (s.status === 'skipped') return '<p class="pl-step-sub">Not part of this run.</p>';
  if (s.status === 'pending') return '<p class="pl-step-sub">Waiting…</p>';
  switch (s.key) {
    case 'render':
      return `<div class="pl-metrics">${metric('documents', num(m.documents))}</div>${d.by_type ? bars(d.by_type) : ''}`;
    case 'version':
      return `<div class="pl-metrics">${metric('new versions', num(m.new_versions))}${metric('unchanged', num(m.unchanged))}${metric('restored', num(m.undeleted))}${metric('tombstoned', num(m.tombstoned))}${metric('chunks in new versions', num(m.new_chunks_seen))}${metric('entity links', num(m.entity_links))}</div>
        <p class="pl-step-sub">Documents whose rendered text hash is unchanged are skipped. Identical chunk text is stored once (content-addressed by SHA-256).</p>`;
    case 'embed': {
      const total = m.total || 0;
      const done = m.done || 0;
      const w = total ? (done / total) * 100 : (s.status === 'done' ? 100 : 0);
      const bar = s.status === 'running' && !total ? '<div class="pl-progress indeterminate"><span></span></div>' : `<div class="pl-progress"><span style="width:${w}%"></span></div>`;
      return `${bar}<div class="pl-metrics">${metric('chunks', `${num(done)} / ${num(total)}`)}${metric('model', esc(m.model || st.overview?.config.embed_model || ''))}${metric('dims', num(m.dims))}${m.chunks_per_second ? metric('chunks/s', num(m.chunks_per_second)) : ''}${m.sample_norm ? metric('sample L2 norm', m.sample_norm) : ''}</div>
        ${total === 0 && s.status === 'done' ? '<p class="pl-step-sub">Nothing to embed — every chunk already has a vector.</p>' : ''}
        ${d.sample_head ? `<h3>Sample vector (first chunk of this batch)</h3>${vecStrip(d.sample_head)}` : ''}`;
    }
    case 'index':
      return `<div class="pl-metrics">${metric('added', num(m.added ?? m.to_add))}${metric('removed', num(m.removed ?? m.to_remove))}${metric('Chroma total', num(m.chroma_total))}${metric('ledger total', num(m.ledger_total))}${m.rebuilt ? metric('staging swap', 'yes') : ''}</div>
        <p class="pl-step-sub">Chroma ids are <code>chunk_hash:account_id</code>, so re-running never creates duplicate vectors.</p>`;
    case 'verify':
      return `<div class="pl-metrics">${metric('Chroma vectors', num(m.chroma_vectors))}${metric('ledger entries', num(m.ledger_entries))}${metric('distinct chunks', num(m.distinct_chunks))}${metric('sampled ids found', `${num(m.sample_found)} / ${num(m.sampled)}`)}${metric('wrong dims', num(m.wrong_dims))}</div>
        ${m.matches_ledger === false ? `<div class="pl-err">Chroma holds ${num(m.chroma_vectors - m.ledger_entries)} more/fewer vectors than the ledger. A full re-embed rebuilds Chroma exactly from the ledger.</div>` : ''}
        ${m.staging_left ? '<div class="pl-err">A staging collection from an interrupted re-embed is still present; the next re-embed replaces it.</div>' : ''}`;
    case 'guardrails': return guardrailBody(m, d);
    case 'eval': return evalBody(m, d);
    default:
      return Object.keys(m).length ? `<div class="pl-metrics">${Object.entries(m).map(([k, v]) => metric(label(k), num(v))).join('')}</div>` : '<p class="pl-step-sub">Done.</p>';
  }
}

function guardrailBody(m, d) {
  const failing = (d.rows || []).filter(r => !r.ok);
  return `<div class="pl-metrics">${metric('guardrail cases', num(m.guardrail_cases))}${metric('guardrail accuracy', pct(m.guardrail_accuracy))}${metric('injection accuracy', pct(m.injection_accuracy))}${metric('moderation recall', pct(m.moderation_recall))}${metric('moderation false positives', num(m.moderation_false_positives))}${metric('indexed chunks with injection text', num(m.index_injection_chunks))}</div>
    ${(d.gate_failures || []).map(f => `<div class="pl-err">Gate failed: ${esc(f)}</div>`).join('')}
    ${failing.length ? `<h3>Failing cases</h3><table class="pl-table"><tbody>${failing.map(r => `<tr><td class="pl-mono">${esc(r.id)}</td><td>${esc((r.problems || []).join('; '))}</td></tr>`).join('')}</tbody></table>` : (d.rows ? `<p class="pl-step-sub">All ${d.rows.length} safety cases pass.</p>` : '')}
    ${(d.injection_samples || []).length ? `<h3>Injection text found in the index — neutralised before it reaches the model</h3>${d.injection_samples.map(x => `<div class="pl-diff"><span class="before">before</span><span>${esc(x.before)}</span><span class="after">after</span><span>${esc(x.after)}</span></div>`).join('')}` : ''}
    ${d.checks ? `<h3>Guardrail layers</h3><table class="pl-table"><thead><tr><th>Guardrail</th><th>Where</th><th>What it does</th></tr></thead><tbody>${d.checks.map(c => `<tr><td><b>${esc(c.name)}</b></td><td class="muted">${esc(c.where)}</td><td>${esc(c.what)}</td></tr>`).join('')}</tbody></table>` : ''}`;
}

function evalBody(m, d) {
  const results = d.results || [];
  const flagged = results.filter(r => ['intent_ok', 'persona_ok', 'recall_ok', 'no_llm_ok'].some(k => r[k] === false) || r.acl_leaks);
  const mark = (v) => (v === true ? '<i class="fa-solid fa-check" style="color:var(--success)"></i>' : v === false ? '<i class="fa-solid fa-xmark" style="color:var(--danger)"></i>' : '<span class="muted">·</span>');
  return `<div class="pl-metrics">${metric('cases', num(m.cases))}${metric('intent accuracy', pct(m.intent_accuracy))}${metric('entity accuracy', pct(m.entity_accuracy))}${metric('recall@10', pct(m.recall_at_10))}${metric('ACL leaks', num(m.acl_leaks))}${metric('PII leaks', num(m.pii_leaks))}${metric('no-LLM leaks', num(m.no_llm_leaks))}</div>
    ${d.gates ? `<h3>Quality gates</h3><table class="pl-table"><thead><tr><th>Metric</th><th class="num">Value</th><th class="num">Gate</th><th></th></tr></thead><tbody>${d.gates.map(g => `<tr><td>${esc(label(g.metric))}</td><td class="num">${num(g.value)}</td><td class="num muted">${esc(g.gate)}</td><td>${g.ok ? pill('ok', 'pass') : pill('failed', 'fail')}</td></tr>`).join('')}</tbody></table>` : ''}
    ${(d.changes || []).length ? `<h3>Changed since the previous eval</h3><div class="pl-chips">${d.changes.map(c => `<span class="pl-chip">${esc(c)}</span>`).join('')}</div>` : ''}
    ${results.length ? `<details style="margin-top:10px"${flagged.length ? ' open' : ''}><summary class="pl-step-sub" style="cursor:pointer">${flagged.length ? `${flagged.length} failing case(s)` : `All ${results.length} golden cases`} — show ${flagged.length ? 'failures' : 'cases'}</summary>
      <div class="pl-table-wrap"><table class="pl-table"><thead><tr><th>Case</th><th>Intent</th><th>Intent</th><th>Person</th><th>Recall</th><th>No-LLM</th><th>Top hits (on miss)</th></tr></thead><tbody>${
        (flagged.length ? flagged : results).map(r => `<tr><td class="pl-mono">${esc(r.id)}</td><td>${esc(r.intent)}</td><td>${mark(r.intent_ok)}</td><td>${mark(r.persona_ok)}</td><td>${mark(r.recall_ok)}</td><td>${mark(r.no_llm_ok)}</td><td class="muted">${esc((r.top || []).join(' · '))}</td></tr>`).join('')}</tbody></table></div></details>` : ''}`;
}

// ── Polling ──────────────────────────────────────────────────────────────────
function startPolling() {
  if (st.poll) return;
  st.poll = setInterval(async () => {
    try {
      if (!st.viewingId) { stopPolling(); return; }
      const cur = await api('/runs/current');
      let run = cur && cur.id === st.viewingId ? cur : null;
      if (!run) {
        // No longer the live run: it finished and was replaced, or a server restart cut it off
        // (/runs/current lives in server memory). Its stored row has the final status.
        run = await api(`/runs/${encodeURIComponent(st.viewingId)}`).catch(() => null);
        if (!run) { stopPolling(); return; }
      }
      renderRun(run);
      st.run = run;
      if (run.status !== 'running') {
        stopPolling();
        showToast(run.status === 'succeeded' ? 'Pipeline run finished'
          : run.status === 'interrupted' ? 'Pipeline run was interrupted (the server restarted) — start it again'
            : 'Pipeline run finished with failures');
        loadOverview().catch(() => {});
      }
    } catch { /* transient — keep polling */ }
  }, 1200);
}
function stopPolling() { clearInterval(st.poll); st.poll = null; }

async function startRun() {
  if (st.mode === 'reembed' && !st.confirmReembed) {
    st.confirmReembed = true;
    $('plStart').innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Click again to re-embed everything';
    $('plStart').classList.add('pl-btn-danger');
    setTimeout(resetStartButton, 5000);
    return;
  }
  resetStartButton();
  $('plStart').disabled = true;
  try {
    const run = await api('/runs', { method: 'POST', body: { mode: st.mode, guardrails: $('plGuard').checked, eval: $('plEval').checked } });
    st.openStages.clear();
    showRun(run);
    loadOverview().catch(() => {});
  } catch (e) {
    showToast(e.message);
    $('plStart').disabled = false;
  }
}
function resetStartButton() {
  st.confirmReembed = false;
  $('plStart').classList.remove('pl-btn-danger');
  $('plStart').innerHTML = '<i class="fa-solid fa-play"></i> Start run';
}

function setMode(mode) {
  st.mode = mode;
  resetStartButton();
  document.querySelectorAll('#plMode button').forEach(b => {
    const on = b.dataset.mode === mode;
    b.classList.toggle('active', on);
    b.setAttribute('aria-checked', String(on));
  });
  $('plModeHint').textContent = MODE_HINT[mode];
}

// ── Embedding inspector ──────────────────────────────────────────────────────
let docTimer = null;
async function loadDocs() {
  const q = $('plDocQ').value.trim();
  const t = $('plDocType').value;
  try {
    const out = await api(`/documents?q=${encodeURIComponent(q)}&doc_type=${encodeURIComponent(t)}`);
    if ($('plDocType').options.length <= 1) {
      $('plDocType').insertAdjacentHTML('beforeend', out.doc_types.map(x => `<option value="${esc(x.doc_type)}">${esc(label(x.doc_type))} (${x.count})</option>`).join(''));
    }
    st.docs = out.documents;
    $('plDocList').innerHTML = out.documents.length ? out.documents.map(d => `<li data-doc="${d.id}" class="${d.id === st.docId ? 'active' : ''}" tabindex="0">
      <div class="t">${esc(d.title || d.canonical_key)}</div>
      <div class="m">${esc(label(d.doc_type))} · v${d.version} · ${d.chunks} chunk${d.chunks === 1 ? '' : 's'}${d.published_at ? ` · ${new Date(d.published_at).toLocaleDateString()}` : ''}</div></li>`).join('')
      : '<li class="m">No documents match.</li>';
  } catch (e) { if (e.message !== 'forbidden') showToast(e.message); }
}

async function traceDoc(id, chunk = 0) {
  st.docId = id;
  document.querySelectorAll('#plDocList li[data-doc]').forEach(li => li.classList.toggle('active', Number(li.dataset.doc) === id));
  $('plDocTrace').innerHTML = '<div class="pl-empty"><i class="fa-solid fa-spinner pl-spin"></i><p>Tracing…</p></div>';
  try {
    const t = await api(`/documents/${id}/trace?chunk=${chunk}`);
    $('plDocTrace').innerHTML = renderDocTrace(t);
  } catch (e) {
    $('plDocTrace').innerHTML = `<div class="pl-err">${esc(e.message)}</div>`;
  }
}

function step(n, title, sub, body) {
  return `<li class="pl-step"><span class="pl-step-num">${n}</span><div class="pl-card">
    <div class="pl-step-head"><h3>${title}</h3><span class="pl-step-sub">${sub || ''}</span></div>${body}</div></li>`;
}

function renderDocTrace(t) {
  const d = t.document;
  const accounts = new Set(t.entities.map(e => e.account_id));
  const vectors = t.chunks.reduce((n, c) => n + c.index_entries.length, 0);
  const inChroma = t.chunks.reduce((n, c) => n + c.index_entries.filter(e => e.in_chroma).length, 0);
  const steps = [
    step(1, 'Source rows', `${t.sources.length} row${t.sources.length === 1 ? '' : 's'} merged into one document`,
      t.sources.length ? `<table class="pl-table"><thead><tr><th>Table</th><th>Primary key</th><th>First seen</th></tr></thead><tbody>${t.sources.map(s => `<tr><td>${esc(s.table)}</td><td class="pl-mono">${esc(s.pk)}</td><td class="muted">${when(s.first_seen)}</td></tr>`).join('')}</tbody></table>`
        : '<p class="pl-step-sub">Derived document (no direct source row).</p>'),
    step(2, 'Rendered document', `${esc(label(d.doc_type))} · version ${d.version}${d.is_current ? '' : ' (not current)'}`,
      `<dl class="pl-kv"><dt>Title</dt><dd>${esc(d.title || '—')}</dd><dt>Canonical key</dt><dd class="pl-mono">${esc(d.canonical_key)}</dd>
        <dt>Content hash</dt><dd class="pl-mono">${esc(d.content_hash)}</dd><dt>SimHash</dt><dd class="pl-mono">${esc(d.simhash || '— (not a news-like type)')}</dd>
        <dt>Render version</dt><dd>${esc(d.render_version)}</dd><dt>Published</dt><dd>${d.published_at ? new Date(d.published_at).toLocaleString() : '—'}</dd>
        <dt>Valid from</dt><dd>${when(d.valid_from)}</dd>${d.url ? `<dt>URL</dt><dd><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.url)}</a></dd>` : ''}</dl>
       ${t.versions.length > 1 ? `<h3 style="margin-top:10px">Version history (SCD-2)</h3><table class="pl-table"><thead><tr><th>v</th><th>Hash</th><th class="num">Chunks</th><th>Valid from</th><th>Valid to</th></tr></thead><tbody>${t.versions.map(v => `<tr${v.id === d.id ? ' class="selected"' : ''}><td>${v.version}${v.is_current ? ' ' + pill('ok', 'current') : ''}</td><td class="pl-mono">${esc(v.content_hash)}…</td><td class="num">${v.chunks}</td><td class="muted">${when(v.valid_from)}</td><td class="muted">${v.valid_to ? when(v.valid_to) : '—'}</td></tr>`).join('')}</tbody></table>` : ''}`),
    step(3, 'Attribution', `${t.entities.length} link${t.entities.length === 1 ? '' : 's'} · ${accounts.size} account${accounts.size === 1 ? '' : 's'}`,
      `<div class="pl-chips">${t.entities.map(e => `<span class="pl-chip">${esc(e.persona || e.account)}${e.persona ? ` <small>${esc(e.account)}</small>` : ''} <small>${esc(e.relation)} · ${e.confidence}</small></span>`).join('') || '<span class="pl-step-sub">No entity links — this document is not searchable.</span>'}</div>`),
    step(4, 'Chunks → embeddings', `${t.chunks.length} chunk${t.chunks.length === 1 ? '' : 's'} · ≤ ${t.config.chunk_max_words} words, ${t.config.chunk_overlap_words} overlap · ${esc(t.config.embed_model)}`,
      t.chunks.map(c => chunkCard(c, t.focus_ordinal)).join('')),
    step(5, 'Chroma index entries', `${inChroma} / ${vectors} vector${vectors === 1 ? '' : 's'} present in <code>${esc(t.collection)}</code>`,
      `${t.chroma_error ? `<div class="pl-err">${esc(t.chroma_error)}</div>` : ''}<div class="pl-table-wrap"><table class="pl-table"><thead><tr><th>Chroma id</th><th>Account</th><th>In Chroma</th><th>Metadata</th><th>Indexed</th></tr></thead><tbody>${
        t.chunks.flatMap(c => c.index_entries.map(e => `<tr><td class="pl-mono">${esc(e.id.slice(0, 16))}…:${e.account_id}</td><td>${e.account_id}</td><td>${e.in_chroma ? pill('ok', 'yes') : pill('failed', 'missing')}</td><td class="pl-mono muted">${esc(e.metadata ? JSON.stringify(e.metadata) : '—')}</td><td class="muted">${when(e.ledger_at)}</td></tr>`)).join('') || '<tr><td class="muted" colspan="5">No index entries — the chunk has no account link or has not been synced yet.</td></tr>'}</tbody></table></div>`),
    step(6, `Nearest neighbours of chunk #${t.focus_ordinal}`, 'Cosine similarity in Chroma (all accounts)',
      t.neighbours.length ? `<table class="pl-table"><thead><tr><th>Similarity</th><th>Document</th><th>Type</th><th>Account</th></tr></thead><tbody>${t.neighbours.map(nb => `<tr class="${nb.document_id ? 'clickable' : ''}" data-doc="${nb.document_id || ''}"><td>${simBar(nb.similarity)}</td><td>${esc(nb.title || nb.hash.slice(0, 12))}${nb.self ? ' ' + pill('running', 'this document') : ''}</td><td class="muted">${esc(label(nb.doc_type || ''))}</td><td>${num(nb.account_id)}</td></tr>`).join('')}</tbody></table>`
        : '<p class="pl-step-sub">No vector for this chunk.</p>'),
  ];
  return `<ol class="pl-steps">${steps.join('')}</ol><p class="pl-step-sub">Traced in ${t.ms} ms.</p>`;
}

function chunkCard(c, focus) {
  const g = c.guardrails;
  const flags = [
    g.injection_sentences ? pill('warn', `${g.injection_sentences} injection sentence(s) neutralised`) : pill('ok', 'no injection text'),
    g.emails || g.phones ? pill('warn', `${g.emails} email · ${g.phones} phone-like`) : '',
    c.shared_by_documents > 1 ? pill('running', `shared by ${c.shared_by_documents} documents — embedded once`) : '',
  ].join(' ');
  return `<div class="pl-chunk${c.ordinal === focus ? ' focus' : ''}">
    <div class="pl-chunk-head"><b>#${c.ordinal}</b><span>${c.words} words · ~${c.tokens} tokens</span><span class="pl-mono" title="${esc(c.hash)}">sha256 ${esc(c.hash.slice(0, 12))}…</span>${flags}</div>
    <div class="pl-chunk-header">${esc(c.header)}</div>
    <div class="pl-chunk-text">${esc(c.text)}</div>
    <button type="button" class="pl-link" data-expand>Show full text</button>
    ${c.vector ? `<div style="margin-top:8px"><div class="pl-metrics">${metric('dims', c.vector.dims)}${metric('L2 norm', c.vector.norm)}${c.similarity_to_previous != null ? metric('similarity to previous chunk', c.similarity_to_previous.toFixed(3)) : ''}${metric('vectors', c.index_entries.length)}</div>${vecStrip(c.vector.head)}</div>` : '<div class="pl-err">No vector in Chroma for this chunk.</div>'}
    ${c.ordinal !== focus && c.vector ? `<button type="button" class="pl-btn pl-btn-sm" style="margin-top:8px" data-focus="${c.ordinal}"><i class="fa-solid fa-circle-nodes"></i> Neighbours of this chunk</button>` : ''}
  </div>`;
}

// ── Query trace ──────────────────────────────────────────────────────────────
async function traceQuery(ev) {
  ev.preventDefault();
  const q = $('plQ').value.trim();
  if (!q) return;
  $('plQTrace').innerHTML = '<div class="pl-empty"><i class="fa-solid fa-spinner pl-spin"></i><p>Tracing…</p></div>';
  try {
    const acc = $('plQAccount').value;
    const t = await api('/query-trace', { method: 'POST', body: { q, account_id: acc ? Number(acc) : null } });
    $('plQTrace').innerHTML = renderQueryTrace(t);
  } catch (e) {
    $('plQTrace').innerHTML = `<div class="pl-err">${esc(e.message)}</div>`;
  }
}

function hitRows(hits, cols) {
  if (!hits?.length) return '<p class="pl-step-sub">No hits.</p>';
  return `<div class="pl-table-wrap"><table class="pl-table"><thead><tr>${cols.map(c => `<th class="${c.cls || ''}">${c.h}</th>`).join('')}</tr></thead><tbody>${
    hits.map(h => `<tr>${cols.map(c => `<td class="${c.cls || ''}">${c.f(h)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}

function renderQueryTrace(t) {
  const by = Object.fromEntries(t.steps.map(s => [s.key, s]));
  const titleCol = { h: 'Document', f: h => `${esc(h.title || h.hash.slice(0, 12))} <span class="muted">${esc(label(h.doc_type || ''))}</span>` };
  const p = by.prepare, e = by.embed, v = by.vector, k = by.keyword, f = by.fuse, r = by.rerank;
  const steps = [
    step(1, 'Prepare', `${p.ms} ms`, `<dl class="pl-kv"><dt>Scope</dt><dd>${p.scope.length} account(s)</dd><dt>Keyword query</dt><dd class="pl-mono">${esc(p.tsquery || '—')}</dd>
      <dt>Dropped terms</dt><dd>${esc(p.dropped_terms.join(', ') || '—')} <span class="muted">(account names — the scope already covers them)</span></dd>
      <dt>Injection check</dt><dd>${p.question_injection_sentences ? pill('warn', `${p.question_injection_sentences} sentence(s) flagged`) : pill('ok', 'clean')}</dd></dl>`),
    step(2, 'Embed question', `${e.ms} ms · ${esc(e.model)}`, `<div class="pl-metrics">${metric('dims', e.vector.dims)}${metric('L2 norm', e.vector.norm)}</div>${vecStrip(e.vector.head)}`),
    step(3, 'Vector search (Chroma)', `${v.ms} ms · top ${v.hits.length}`, (v.error ? `<div class="pl-err">${esc(v.error)}</div>` : '') + hitRows(v.hits, [
      { h: '#', f: h => h.rank }, { h: 'Similarity', f: h => simBar(h.similarity) }, titleCol, { h: 'Account', f: h => num(h.account_id) }])),
    step(4, 'Keyword search (Postgres full-text)', `${k.ms} ms · top ${k.hits.length}`, hitRows(k.hits, [{ h: '#', f: h => h.rank }, titleCol])),
    step(5, 'Reciprocal rank fusion', `k = ${f.k} · score = Σ 1 / (k + rank)`, hitRows(f.hits, [
      { h: 'Score', cls: 'num', f: h => h.score.toFixed(5) }, { h: 'Vector #', cls: 'num', f: h => h.vector_rank ?? '—' },
      { h: 'Keyword #', cls: 'num', f: h => h.keyword_rank ?? '—' }, titleCol, { h: '', f: h => (h.both ? pill('ok', 'both legs') : '') }])),
    step(6, 'Evidence sent to the model', `${r.ms} ms · ${r.evidence.length} / ${r.max_items} items · ~${num(r.tokens_used)} / ${num(r.token_budget)} tokens`,
      `<p class="pl-step-sub" style="margin-top:0">Verified against Postgres (current version, ACL), person and recency boosts, one chunk per document, near-duplicate collapse, token budget.</p>` +
      (r.evidence.map(x => `<div class="pl-evidence"><div class="t">[${x.n}] ${esc(x.title)} <span class="muted">· ${esc(label(x.doc_type))} · score ${x.score.toFixed(4)}${x.published_at ? ` · ${new Date(x.published_at).toLocaleDateString()}` : ''}</span> ${x.injection_sentences ? pill('warn', `${x.injection_sentences} injection sentence(s) removed`) : ''}</div><div class="s">${esc(x.snippet)}</div></div>`).join('') || '<p class="pl-step-sub">No evidence — the copilot would answer "not found".</p>')),
  ];
  return `<ol class="pl-steps">${steps.join('')}</ol>`;
}

// ── Wiring ───────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.pl-tab').forEach(b => { b.classList.toggle('active', b.dataset.tab === tab); b.setAttribute('aria-selected', String(b.dataset.tab === tab)); });
  document.querySelectorAll('.pl-view').forEach(v => { v.hidden = v.dataset.view !== tab; });
  history.replaceState(null, '', `#${tab}`);
  if (tab === 'inspect' && !st.docs.length) loadDocs();
}

function wire() {
  document.querySelectorAll('.pl-tab').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  document.querySelectorAll('#plMode button').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));
  $('plStart').addEventListener('click', startRun);
  $('plHistory').addEventListener('click', (e) => { const tr = e.target.closest('tr[data-run]'); if (tr) { stopPolling(); openRun(tr.dataset.run); } });
  const scrollToStage = (e) => {
    const li = e.target.closest('li[data-stage]');
    if (!li) return;
    const card = document.querySelector(`.pl-stage[data-stage="${li.dataset.stage}"]`);
    if (card) { card.open = true; st.openStages.add(li.dataset.stage); card.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
  };
  $('plFlow').addEventListener('click', scrollToStage);
  $('plStages').addEventListener('toggle', (e) => {
    const d = e.target.closest?.('.pl-stage');
    if (d) { if (d.open) st.openStages.add(d.dataset.stage); else st.openStages.delete(d.dataset.stage); }
  }, true);

  $('plDocQ').addEventListener('input', () => { clearTimeout(docTimer); docTimer = setTimeout(loadDocs, 250); });
  $('plDocType').addEventListener('change', loadDocs);
  const pick = (e) => { const li = e.target.closest('li[data-doc]'); if (li) traceDoc(Number(li.dataset.doc)); };
  $('plDocList').addEventListener('click', pick);
  $('plDocList').addEventListener('keydown', (e) => { if (e.key === 'Enter') pick(e); });
  $('plDocTrace').addEventListener('click', (e) => {
    const exp = e.target.closest('[data-expand]');
    if (exp) { const txt = exp.previousElementSibling; txt.classList.toggle('open'); exp.textContent = txt.classList.contains('open') ? 'Show less' : 'Show full text'; return; }
    const foc = e.target.closest('[data-focus]');
    if (foc) { traceDoc(st.docId, Number(foc.dataset.focus)); return; }
    const nb = e.target.closest('tr[data-doc]');
    if (nb && nb.dataset.doc && Number(nb.dataset.doc) !== st.docId) traceDoc(Number(nb.dataset.doc));
  });
  $('plQForm').addEventListener('submit', traceQuery);
}

async function loadAccounts() {
  try {
    const res = await fetch('/api/copilot/context');
    if (!res.ok) return;
    const { accounts } = await res.json();
    $('plQAccount').insertAdjacentHTML('beforeend', accounts.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join(''));
  } catch { /* scope picker is optional */ }
}

initThemeToggle();
initTopbarAuth().then(() => {
  wire();
  setMode('sync');
  const tab = (location.hash || '').slice(1);
  if (['runs', 'inspect', 'query'].includes(tab)) switchTab(tab);
  loadOverview().catch((e) => { if (e.message !== 'forbidden') showToast(e.message); });
  loadAccounts();
});
