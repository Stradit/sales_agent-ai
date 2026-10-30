// "Scheduled jobs" tab on the Admin page (/admin): the 6-hourly source and Command Center refreshes —
// on/off, how often, which source channels, last / next run, Run now and the run history.
// API: /api/admin/jobs (services/scheduler.py). Super admins only.
import { showToast } from './toast.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const STATUS = { ok: ['ok', 'Succeeded'], failed: ['bad', 'Failed'], skipped: ['muted', 'Skipped'], running: ['warn', 'Running…'] };
const INTERVALS = [1, 2, 3, 6, 12, 24, 48, 168];

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

const when = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—');
function rel(iso) {
  if (!iso) return '';
  const mins = Math.round((new Date(iso) - Date.now()) / 60000);
  const abs = Math.abs(mins);
  const txt = abs < 60 ? `${abs} min` : abs < 48 * 60 ? `${Math.round(abs / 60)} h` : `${Math.round(abs / 1440)} days`;
  return mins >= 0 ? (abs < 2 ? 'due now' : `in ${txt}`) : `${txt} ago`;
}
const every = (h) => (h % 24 === 0 && h >= 24 ? `every ${h / 24 === 1 ? 'day' : `${h / 24} days`}` : `every ${h} hour${h === 1 ? '' : 's'}`);

function summaryText(job, run) {
  if (!run) return '';
  const s = run.summary || {};
  if (run.status === 'failed') return esc(run.error || 'Failed');
  if (s.skipped) return esc(s.skipped);
  if (job === 'command_center_refresh') return `${s.users ?? 0} users refreshed · ${s.signals ?? 0} signals in their feeds`;
  if (job === 'sources_refresh') {
    return `${s.accounts ?? 0} accounts scraped (${esc((s.channels || []).join(', '))})${s.accounts_failed ? ` · ${s.accounts_failed} failed` : ''}`;
  }
  return '';
}

export async function mountJobs(el) {
  if (!el) return;
  let data;
  let timer = null;
  const load = async () => { data = await api('/api/admin/jobs'); };

  const jobCard = (j) => {
    const last = j.last_run;
    const [tone, label] = j.running ? STATUS.running : last ? STATUS[last.status] || ['muted', last.status] : ['muted', 'Never run'];
    const intervals = [...new Set([...INTERVALS, j.interval_hours])].sort((a, b) => a - b);
    const channels = j.name === 'sources_refresh' ? `<fieldset class="job-channels"><legend>Sources to scrape</legend>
        ${data.channels.map(c => `<label><input type="checkbox" data-job-channel="${esc(c.key)}"${(j.options.channels || []).includes(c.key) ? ' checked' : ''}>
          <span><strong>${esc(c.key.replace('_', ' '))}</strong><small class="${c.cost === 'Free' ? 'ok' : 'warn'}">${esc(c.cost)}</small></span></label>`).join('')}
      </fieldset>` : '';
    return `<section class="job-card${j.enabled ? '' : ' is-off'}" data-job="${esc(j.name)}">
      <header class="job-head">
        <div><h3>${esc(j.label)}</h3><p>${esc(j.description)}</p></div>
        <label class="job-switch"><input type="checkbox" data-job-enabled${j.enabled ? ' checked' : ''}${j.unavailable ? ' disabled' : ''}>
          <span>${j.enabled ? 'On' : 'Off'}</span></label>
      </header>
      ${j.unavailable ? `<p class="job-note warn"><i class="fa-solid fa-circle-info"></i> ${esc(j.unavailable)}</p>` : ''}
      <dl class="job-facts">
        <div><dt>Runs</dt><dd><select data-job-interval aria-label="How often">${intervals.map(h =>
          `<option value="${h}"${h === j.interval_hours ? ' selected' : ''}>${every(h)}</option>`).join('')}</select></dd></div>
        <div><dt>Last run</dt><dd><span class="job-pill ${tone}">${label}</span> ${last ? `${esc(when(last.started_at))} <small>(${rel(last.started_at)}${last.duration_s != null ? ` · ${last.duration_s}s` : ''})</small>` : ''}</dd></div>
        <div><dt>Next run</dt><dd>${j.next_run_at ? `${esc(when(j.next_run_at))} <small>(${rel(j.next_run_at)})</small>` : '<span class="job-muted">Off</span>'}</dd></div>
        <div class="job-wide"><dt>Result</dt><dd>${summaryText(j.name, last) || '<span class="job-muted">—</span>'}</dd></div>
      </dl>
      ${channels}
      <footer><button type="button" class="admin-btn primary" data-job-run${j.running || j.unavailable ? ' disabled' : ''}>
        <i class="fa-solid fa-play"></i> ${j.running ? 'Running…' : 'Run now'}</button></footer>
    </section>`;
  };

  const render = () => {
    const off = !data.scheduler_enabled;
    el.innerHTML = `<div class="admin-panel jobs-panel">
      <div class="admin-panel-header"><div class="admin-panel-title"><i class="fa-solid fa-clock-rotate-left"></i> Scheduled jobs</div>
        <span class="context-badge ${off ? '' : 'live'}">${off ? 'Scheduler off (SCHEDULER_ENABLED=0) — only Run now works'
          : data.scheduler_running ? `Scheduler running · checks every ${data.check_minutes} min` : 'Scheduler starting…'}</span></div>
      <div class="jobs-grid">${data.jobs.map(jobCard).join('')}</div>
      <details class="crm-set-log" open><summary>Run history (latest ${data.runs.length})</summary>
        ${data.runs.length ? `<div class="admin-table-wrap"><table class="crm-set-table"><thead><tr><th>Started</th><th>Job</th><th>Trigger</th><th>Status</th><th>Took</th><th>Result</th></tr></thead><tbody>
        ${data.runs.map(r => { const [tone, label] = STATUS[r.status] || ['muted', r.status];
          const job = data.jobs.find(j => j.name === r.job);
          return `<tr><td>${esc(when(r.started_at))}</td><td>${esc(job ? job.label : r.job)}</td>
            <td>${r.trigger === 'manual' ? `Run now${r.by ? ` · ${esc(r.by)}` : ''}` : 'Schedule'}</td>
            <td><span class="job-pill ${tone}">${label}</span></td><td>${r.duration_s != null ? `${r.duration_s}s` : '—'}</td>
            <td class="job-result">${summaryText(r.job, r)}</td></tr>`; }).join('')}
        </tbody></table></div>` : '<p class="crm-set-note">No runs yet.</p>'}
      </details></div>`;
    // While something runs, refresh every 5 s so the result shows up without a reload
    clearTimeout(timer);
    if (data.jobs.some(j => j.running)) timer = setTimeout(async () => { if (!el.isConnected) return; await load(); render(); }, 5000);
  };

  const patch = async (card, body) => {
    try { data = await api(`/api/admin/jobs/${encodeURIComponent(card.dataset.job)}`, { method: 'PATCH', body }); render(); showToast('Saved'); }
    catch (err) { showToast(err.message); await load(); render(); }
  };

  el.addEventListener('change', (e) => {
    const card = e.target.closest('[data-job]');
    if (!card) return;
    if (e.target.matches('[data-job-enabled]')) patch(card, { enabled: e.target.checked });
    else if (e.target.matches('[data-job-interval]')) patch(card, { interval_hours: Number(e.target.value) });
    else if (e.target.matches('[data-job-channel]')) {
      const picked = [...card.querySelectorAll('[data-job-channel]:checked')].map(c => c.dataset.jobChannel);
      if (!picked.length) { showToast('Pick at least one source'); e.target.checked = true; return; }
      patch(card, { channels: picked });
    }
  });
  el.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-job-run]');
    if (!btn) return;
    btn.disabled = true;
    try {
      await api(`/api/admin/jobs/${encodeURIComponent(btn.closest('[data-job]').dataset.job)}/run`, { method: 'POST' });
      showToast('Started — the result appears here when it finishes');
      await new Promise(r => setTimeout(r, 800));
    } catch (err) { showToast(err.message); }
    await load();
    render();
  });

  try { await load(); render(); } catch (err) {
    el.innerHTML = `<div class="admin-panel"><div class="admin-panel-body">Couldn't load scheduled jobs: ${esc(err.message)}</div></div>`;
  }
}
