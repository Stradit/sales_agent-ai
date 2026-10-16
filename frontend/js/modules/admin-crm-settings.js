// "CRM settings" panel on the Admin page (/admin): business lines, fiscal year start, default
// attribution %, stage probabilities, the org-wide email-notifications switch, a test email and the
// notification log. APIs: /api/crm/business-lines, /api/crm/settings, /api/crm/notifications/*.
import { showToast } from './toast.js';

const esc = (s) => { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; };
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const STAGES = [['intro', 'Intro'], ['discovery', 'Discovery'], ['proposal', 'Proposal'], ['pilot', 'Pilot'], ['contract', 'Contract']];
const STATUS_TONE = { sent: 'ok', logged: 'warn', held: 'muted', skipped: 'muted', queued: 'warn', failed: 'bad' };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

export async function mountCrmSettings(el) {
  if (!el) return;
  let settings, lines, log;
  const load = async () => {
    [settings, lines, log] = await Promise.all([api('/api/crm/settings'), api('/api/crm/business-lines'), api('/api/crm/notifications/admin')]);
  };
  const render = () => {
    const probs = settings.stage_probability || {};
    el.innerHTML = `<div class="admin-panel crm-set">
      <div class="admin-panel-header"><div class="admin-panel-title"><i class="fa-solid fa-sliders"></i> CRM settings</div>
        <span class="context-badge live">Deals · Introductions · Forecast</span></div>
      <div class="crm-set-grid">
        <section><h3>Business lines</h3>
          <table class="crm-set-table"><thead><tr><th>Name</th><th>Key</th><th>Order</th><th>Active</th></tr></thead><tbody>
          ${lines.map(b => `<tr data-bl="${b.id}"><td><input data-f="name" value="${esc(b.name)}" maxlength="80" aria-label="Name"></td>
            <td><code>${esc(b.key)}</code></td><td><input data-f="sort" type="number" value="${b.sort}" style="width:60px" aria-label="Order"></td>
            <td><input data-f="active" type="checkbox"${b.active ? ' checked' : ''} aria-label="Active"></td></tr>`).join('')}
          </tbody></table>
          <form class="crm-set-add" id="crmBlAdd"><input name="name" placeholder="New business line" maxlength="80" required>
            <input name="key" placeholder="key e.g. healthcare" pattern="[a-z0-9_]{2,40}" required>
            <button type="submit" class="admin-btn"><i class="fa-solid fa-plus"></i> Add</button></form>
        </section>
        <section><h3>Forecast &amp; attribution</h3>
          <label>Fiscal year starts in<select data-setting="fiscal_year_start_month">${MONTHS.map((m, i) =>
            `<option value="${i + 1}"${settings.fiscal_year_start_month === i + 1 ? ' selected' : ''}>${m}</option>`).join('')}</select></label>
          <label>Default attribution for introductions (%)<input data-setting="default_attribution_pct" type="number" min="0" max="100" step="5"
            value="${settings.default_attribution_pct ?? 50}"></label>
          <div class="crm-set-probs"><span>Stage probability (%) — used for weighted pipeline</span>
            ${STAGES.map(([k, l]) => `<label>${l}<input data-prob="${k}" type="number" min="0" max="100" step="5" value="${probs[k] ?? ''}"></label>`).join('')}</div>
        </section>
        <section><h3>Email notifications</h3>
          <label class="crm-set-switch"><input type="checkbox" data-setting="email_notifications_enabled"${settings.email_notifications_enabled ? ' checked' : ''}>
            <span><strong>Send notification emails</strong><small>Partner updates, assignments, stale intros, triage, account ownership, Microsoft 365
            reconnects and the Monday pipeline email. While off, nothing is sent (the log shows what would have been).</small></span></label>
          <p class="crm-set-note">SMTP: ${log.smtp_configured ? '<span class="ok">configured</span>' : '<span class="bad">not configured — emails are only written to the server log</span>'}</p>
          <button type="button" class="admin-btn" id="crmTestEmail"><i class="fa-solid fa-paper-plane"></i> Send me a test email</button>
          <p class="crm-set-note">${Object.entries(log.counts).map(([k, v]) => `${esc(k)}: ${v}`).join(' · ') || 'No notifications yet.'}</p>
        </section>
      </div>
      <details class="crm-set-log"><summary>Notification log (latest ${log.recent.length})</summary>
        <table class="crm-set-table"><thead><tr><th>When</th><th>To</th><th>Type</th><th>Subject</th><th>Status</th></tr></thead><tbody>
        ${log.recent.map(n => `<tr><td>${esc(new Date(n.created_at).toLocaleString())}</td><td>${esc(n.user_name || n.to_email)}</td>
          <td>${esc(n.kind.replace(/_/g, ' '))}</td><td>${esc(n.subject)}</td>
          <td><span class="crm-set-pill ${STATUS_TONE[n.status] || 'muted'}" title="${esc(n.error || '')}">${esc(n.status)}</span></td></tr>`).join('')
          || '<tr><td colspan="5">Nothing yet.</td></tr>'}</tbody></table></details>
    </div>`;
  };
  const refresh = async () => { try { await load(); render(); } catch (e) { el.innerHTML = `<p class="crm-set-note bad">${esc(e.message)}</p>`; } };

  const saveSettings = async (values) => {
    try { settings = await api('/api/crm/settings', { method: 'PUT', body: { values } }); showToast('Settings saved'); }
    catch (e) { showToast(e.message); render(); }
  };

  el.addEventListener('change', async (e) => {
    const t = e.target;
    const row = t.closest('[data-bl]');
    if (row && t.dataset.f) {
      const v = t.dataset.f === 'active' ? t.checked : t.dataset.f === 'sort' ? Number(t.value) : t.value.trim();
      try { await api(`/api/crm/business-lines/${row.dataset.bl}`, { method: 'PATCH', body: { [t.dataset.f]: v } }); showToast('Business line saved'); }
      catch (err) { showToast(err.message); refresh(); }
      return;
    }
    if (t.dataset.setting) {
      const k = t.dataset.setting;
      const v = t.type === 'checkbox' ? t.checked : Number(t.value);
      if (k === 'email_notifications_enabled' && v && !confirmSwitch(t)) return;
      await saveSettings({ [k]: v });
      if (k === 'email_notifications_enabled') refresh();
      return;
    }
    if (t.dataset.prob) {
      const probs = { ...(settings.stage_probability || {}) };
      el.querySelectorAll('[data-prob]').forEach(i => { probs[i.dataset.prob] = Number(i.value); });
      await saveSettings({ stage_probability: probs });
    }
  });
  // Turning emails on is outward-facing: require a second click.
  function confirmSwitch(t) {
    if (t.dataset.armed === '1') return true;
    t.checked = false;
    t.dataset.armed = '1';
    showToast('Real emails will go to users and partners. Tick the box again to confirm.');
    setTimeout(() => { t.dataset.armed = ''; }, 8000);
    return false;
  }
  el.addEventListener('submit', async (e) => {
    if (e.target.id !== 'crmBlAdd') return;
    e.preventDefault();
    const f = e.target;
    try {
      await api('/api/crm/business-lines', { method: 'POST', body: { name: f.name.value.trim(), key: f.key.value.trim(), sort: lines.length + 1 } });
      showToast('Business line added'); refresh();
    } catch (err) { showToast(err.message); }
  });
  el.addEventListener('click', async (e) => {
    if (!e.target.closest('#crmTestEmail')) return;
    try {
      const r = await api('/api/crm/notifications/test', { method: 'POST' });
      showToast(r.status === 'sent' ? `Test email sent to ${r.to}` : r.status === 'logged' ? 'SMTP not configured — written to the server log' : 'Sending failed — check the server log');
      refresh();
    } catch (err) { showToast(err.message); }
  });
  await refresh();
}
