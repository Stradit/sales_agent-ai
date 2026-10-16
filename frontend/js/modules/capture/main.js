// Email & Calendar Sync page (/email-sync). Connect a Microsoft 365 mailbox, choose what is captured,
// see sync status, disconnect (optionally deleting what was captured). Admins also see the setup
// checklist, the org-wide "store full email bodies" policy and every user's connection.
// API: apps/sales_crm/capture/api.py.
import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { showToast } from '../toast.js';
import { esc } from '../utils.js';

const $ = (id) => document.getElementById(id);
const STATUS = { active: ['Connected', 'good'], needs_reauth: ['Reconnect needed', 'bad'], error: ['Sync error', 'warn'], paused: ['Paused', 'warn'] };
let st = null;

async function api(path, opts = {}) {
  const res = await fetch(`/api/crm/capture${path}`, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401) { location.replace(`/login?next=${encodeURIComponent(location.pathname)}`); throw new Error('unauthenticated'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

const when = (ts) => (ts ? new Date(ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—');

function statsLine(x) {
  if (!x || !Object.keys(x).length) return 'No sync yet';
  const fetched = (x.fetched_inbox || 0) + (x.fetched_sent || 0) + (x.fetched_calendar || 0);
  const skipped = Object.entries(x).filter(([k]) => k.startsWith('skipped_')).reduce((a, [, v]) => a + v, 0);
  return `${fetched} item${fetched === 1 ? '' : 's'} checked · ${x.stored || 0} new · ${x.updated || 0} updated · ${x.removed || 0} removed · ${skipped} not related to your accounts${x.duplicate ? ` · ${x.duplicate} already logged by a colleague` : ''}`;
}

function toggle(name, label, hint, checked, disabled = false) {
  return `<label class="cap-toggle"><input type="checkbox" data-setting="${name}"${checked ? ' checked' : ''}${disabled ? ' disabled' : ''}>
    <span><strong>${label}</strong><small>${hint}</small></span></label>`;
}

function setupCard() {
  if (!st.is_admin) {
    return `<div class="cap-card"><h2><i class="fa-brands fa-microsoft"></i> Microsoft 365 isn't set up yet</h2>
      <p>Your administrator needs to connect this app to StradIT's Microsoft 365 tenant. Once that's done you can connect your mailbox here.</p></div>`;
  }
  return `<div class="cap-card"><h2><i class="fa-brands fa-microsoft"></i> Set up Microsoft 365 (admin, one time)</h2>
    <ol class="cap-steps">
      <li>In the <strong>Microsoft Entra admin center</strong> → App registrations → New registration: <em>single tenant</em>.</li>
      <li>Redirect URI (Web): <code>${esc(st.redirect_uri)}</code></li>
      <li>API permissions → Microsoft Graph → <strong>Delegated</strong>: <code>offline_access</code>, <code>User.Read</code>, <code>Mail.Read</code>,
        <code>Calendars.Read</code> → <em>Grant admin consent</em>.</li>
      <li>Certificates &amp; secrets → New client secret.</li>
      <li>Add to the server's <code>.env</code>: <code>MS_CLIENT_ID</code>, <code>MS_CLIENT_SECRET</code>, <code>MS_TENANT_ID</code>, and
        <code>CAPTURE_TOKEN_KEY</code> (generate with <code>python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"</code>). Restart the app.</li>
    </ol></div>`;
}

function render() {
  const c = st.connection;
  let html = '';
  if (!st.configured) html += setupCard();
  if (st.configured && !c) {
    html += `<div class="cap-card cap-connect"><div><h2><i class="fa-brands fa-microsoft"></i> Connect your Microsoft 365 mailbox</h2>
      <p>We read your Inbox, Sent Items and calendar (from 30 days ago onward) and log only emails and meetings with people at accounts
      you have access to. Everything else is ignored.</p></div>
      <button type="button" class="dl-btn dl-btn-primary" id="capConnect"><i class="fa-brands fa-microsoft"></i> Connect Microsoft 365</button></div>`;
  }
  if (c) {
    const [label, tone] = STATUS[c.status] || [c.status, 'warn'];
    const s = c.settings || {};
    html += `<div class="cap-card"><div class="cap-conn-head">
        <div><h2><i class="fa-brands fa-microsoft"></i> ${esc(c.account_email)}</h2>
          <span class="cap-pill ${tone}">${esc(label)}</span> <span class="cap-muted">Last sync ${esc(when(c.last_sync_at))}${c.status === 'active' ? ` · next ${esc(when(c.next_sync_at))}` : ''}</span></div>
        <div class="cap-actions">
          ${c.status === 'needs_reauth' ? '<button type="button" class="dl-btn dl-btn-primary" id="capConnect">Reconnect</button>'
            : `<button type="button" class="dl-btn" id="capSync"${st.configured ? '' : ' disabled'}><i class="fa-solid fa-rotate"></i> Sync now</button>`}
        </div></div>
      ${c.error ? `<p class="cap-error"><i class="fa-solid fa-triangle-exclamation"></i> ${esc(c.error)}</p>` : ''}
      <p class="cap-muted">${esc(statsLine(c.last_stats))}</p>
      <p class="cap-muted">So far: <strong>${st.captured.meetings}</strong> meetings and <strong>${st.captured.emails}</strong> emails logged from your mailbox.</p></div>

      <div class="cap-card"><h2>What to capture</h2>
        ${toggle('capture_calendar', 'Calendar meetings', 'Meetings with at least one contact from your accounts, including upcoming ones.', s.capture_calendar !== false)}
        ${toggle('capture_email', 'Emails', 'Inbox and Sent Items messages to or from your contacts.', s.capture_email !== false)}
        ${toggle('store_bodies', 'Store full email text', st.org.allow_bodies ? 'Otherwise only the subject and a short preview are kept.'
          : 'Turned off by your organisation — only the subject and a short preview are kept.', !!s.store_bodies, !st.org.allow_bodies)}
        ${toggle('exclude_internal_only', 'Skip internal-only threads', 'Emails and meetings with only StradIT people are never logged.', s.exclude_internal_only !== false)}
        <label class="cap-field">Never capture these domains <small>comma-separated, e.g. a customer you're not allowed to log</small>
          <input id="capExclude" value="${esc((s.exclude_domains || []).join(', '))}" placeholder="example.com, partner.org"></label>
      </div>

      <div class="cap-card cap-danger"><h2>Disconnect</h2>
        <p class="cap-muted">Stops syncing and deletes the stored Microsoft sign-in. Activities already logged stay unless you tick the box.</p>
        <label class="cap-check"><input type="checkbox" id="capPurge"> Also delete the ${st.captured.meetings + st.captured.emails} activities captured from my mailbox</label>
        <button type="button" class="dl-btn dl-btn-danger" id="capDisconnect"><i class="fa-solid fa-link-slash"></i> Disconnect</button></div>`;
  }
  html += `<div class="cap-card cap-privacy"><h2><i class="fa-solid fa-shield-halved"></i> What is and isn't stored</h2><ul>
      <li>Only items with someone from an account <strong>you can access</strong> — matched by their work email, or by company domain (account only).</li>
      <li>Personal addresses (Gmail, Outlook.com, …) are never matched or stored.</li>
      <li>Emails keep the subject and a short preview${st.org.allow_bodies ? ' unless you turn on full text' : ''}; attachments are never downloaded.</li>
      <li>Logged items are visible to colleagues who can open the same account. Mark any item <em>private</em> on its timeline.</li>
      <li>The Microsoft sign-in is stored encrypted and never shown in the app; disconnecting deletes it.</li></ul></div>`;
  $('capMain').innerHTML = html;
}

async function renderAdmin() {
  if (!st.is_admin) return;
  try {
    const a = await fetch('/api/crm/capture/admin').then(r => r.json());
    $('capAdmin').hidden = false;
    $('capAdmin').innerHTML = `<div class="cap-card"><h2><i class="fa-solid fa-user-shield"></i> Admin</h2>
      ${a.token_key_set ? '' : '<p class="cap-error"><i class="fa-solid fa-triangle-exclamation"></i> CAPTURE_TOKEN_KEY is not set — tokens are encrypted with a key derived from JWT_SECRET_KEY.</p>'}
      ${toggle('org_allow_bodies', 'Allow users to store full email text', 'Organisation-wide. When off, only subjects and previews are kept for everyone.', a.allow_bodies)}
      <table class="cap-table"><thead><tr><th scope="col">User</th><th scope="col">Mailbox</th><th scope="col">Status</th><th scope="col">Last sync</th><th scope="col">Captured</th></tr></thead>
      <tbody>${a.connections.map(c => `<tr><td>${esc(c.user_name)}</td><td>${esc(c.account_email)}</td>
        <td><span class="cap-pill ${(STATUS[c.status] || ['', 'warn'])[1]}">${esc((STATUS[c.status] || [c.status])[0])}</span>${c.error ? `<small>${esc(c.error)}</small>` : ''}</td>
        <td>${esc(when(c.last_sync_at))}</td><td>${c.captured}</td></tr>`).join('') || '<tr><td colspan="5" class="cap-muted">No one has connected yet.</td></tr>'}</tbody></table></div>`;
  } catch { /* admin panel is optional */ }
}

async function renderNotify() {
  try {
    const res = await fetch('/api/crm/notifications/prefs');
    if (!res.ok) return;
    const p = await res.json();
    $('capNotify').innerHTML = `<div class="cap-card" id="notifications"><h2><i class="fa-solid fa-bell"></i> Email notifications</h2>
      <p class="cap-muted">Sent to <strong>${esc(p.email)}</strong>.${p.enabled_org_wide ? '' : ' Your administrator hasn\'t switched email notifications on yet — your choices are saved for when they do.'}</p>
      ${p.kinds.map(k => toggle(`notify:${k.key}`, esc(k.label), '', k.on)).join('')}
      <button type="button" class="dl-btn" id="capTestEmail"><i class="fa-solid fa-paper-plane"></i> Send me a test email</button></div>`;
  } catch { /* optional */ }
}

async function load() {
  try { st = await api('/status'); render(); renderAdmin(); renderNotify(); }
  catch (err) { $('capMain').innerHTML = `<p class="cap-error">${esc(err.message)}</p>`; }
}

function wire() {
  document.addEventListener('click', async (e) => {
    const t = e.target;
    if (t.closest('#capConnect')) {
      try { const r = await api('/connect/microsoft', { method: 'POST' }); location.assign(r.auth_url); } catch (err) { showToast(err.message); }
    } else if (t.closest('#capSync')) {
      const b = t.closest('#capSync'); b.disabled = true; b.innerHTML = '<i class="fa-solid fa-rotate fa-spin"></i> Syncing…';
      try { const r = await api('/sync', { method: 'POST' }); showToast(r.error ? r.error : `Synced — ${r.stored || 0} new, ${r.updated || 0} updated`); } catch (err) { showToast(err.message); }
      load();
    } else if (t.closest('#capTestEmail')) {
      try {
        const r = await fetch('/api/crm/notifications/test', { method: 'POST' }).then(x => x.json());
        showToast(r.status === 'sent' ? `Test email sent to ${r.to}` : r.status === 'logged' ? 'Email isn\'t configured on the server yet' : (r.detail || 'Sending failed'));
      } catch (err) { showToast(err.message); }
    } else if (t.closest('#capDisconnect')) {
      const b = t.closest('#capDisconnect');
      if (b.dataset.confirm !== '1') { b.dataset.confirm = '1'; b.textContent = 'Click again to disconnect'; return; }
      try { const r = await api(`/microsoft?purge=${$('capPurge').checked}`, { method: 'DELETE' }); showToast(`Disconnected${r.purged ? ` — ${r.purged} activities deleted` : ''}`); load(); }
      catch (err) { showToast(err.message); }
    }
  });
  document.addEventListener('change', async (e) => {
    const t = e.target;
    if (t.dataset.setting && t.dataset.setting.startsWith('notify:')) {
      try {
        const res = await fetch('/api/crm/notifications/prefs', { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ settings: { [t.dataset.setting.slice(7)]: t.checked } }) });
        if (!res.ok) throw new Error('Could not save');
        showToast('Saved');
      } catch (err) { showToast(err.message); t.checked = !t.checked; }
      return;
    }
    if (t.dataset.setting === 'org_allow_bodies') {
      try {
        const res = await fetch('/api/crm/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ values: { capture_allow_bodies: t.checked } }) });
        if (!res.ok) throw new Error('Could not save the policy');
        showToast('Policy saved'); load();
      } catch (err) { showToast(err.message); t.checked = !t.checked; }
      return;
    }
    const body = t.dataset.setting ? { [t.dataset.setting]: t.checked }
      : t.id === 'capExclude' ? { exclude_domains: t.value.split(',').map(x => x.trim()).filter(Boolean) } : null;
    if (!body) return;
    try { await api('/settings', { method: 'PATCH', body }); showToast('Saved'); load(); }
    catch (err) { showToast(err.message); load(); }
  });
}

function banner() {
  const p = new URLSearchParams(location.search);
  if (!p.get('result')) return;
  $('capBanner').hidden = false;
  $('capBanner').className = `cap-banner ${p.get('result') === 'connected' ? 'good' : 'bad'}`;
  $('capBanner').textContent = p.get('result') === 'connected'
    ? 'Mailbox connected — the first sync is running and takes a minute or two.' : (p.get('msg') || 'Connecting failed.');
  history.replaceState(null, '', location.pathname);
}

initThemeToggle();
initTopbarAuth().then((user) => {
  if (!user) { document.body.style.display = 'none'; location.replace(`/login?next=${encodeURIComponent(location.pathname)}`); return; }
  banner(); wire(); load();
});
