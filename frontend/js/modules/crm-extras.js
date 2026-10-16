// Small CRM widgets used on the dashboard and profile page (apps/sales_crm/records.py):
//   openContactEditor({ accountId, accountName, contact?, onSaved })  add / edit a contact by hand
//   mountAccountCrm(el, accountId)                                    account owner + primary business line
// Styles: css/crm-extras.css. Viewers see values read-only (the API refuses writes anyway).
import { esc } from './utils.js';
import { showToast } from './toast.js';

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

const isViewer = () => document.body.classList.contains('role-viewer');

// ── Contact editor ───────────────────────────────────────────────────────────
export function openContactEditor({ accountId, accountName, contact = null, onSaved }) {
  const editing = !!contact;
  const wrap = document.createElement('div');
  wrap.className = 'cx-modal-wrap';
  wrap.innerHTML = `<form class="cx-modal" role="dialog" aria-modal="true" aria-labelledby="cxTitle" autocomplete="off">
      <header><h2 id="cxTitle">${editing ? 'Edit contact' : `Add a contact${accountName ? ` at ${esc(accountName)}` : ''}`}</h2>
        <button type="button" class="cx-x" data-close aria-label="Close"><i class="fa-solid fa-xmark"></i></button></header>
      <label>Full name<input name="full_name" required minlength="2" maxlength="200" value="${esc(contact?.name || '')}"></label>
      <label>Title<input name="title" maxlength="250" value="${esc(contact?.title || '')}" placeholder="e.g. Chief Data Officer"></label>
      <div class="cx-row">
        <label>Work email<input name="email" type="email" maxlength="255" value="${esc(contact?.email || '')}" placeholder="name@company.com"></label>
        <label>Phone<input name="phone" maxlength="60" value="${esc(contact?.phone || '')}" placeholder="+1 212 555 0100"></label>
      </div>
      <label>LinkedIn URL<input name="linkedin_url" maxlength="500" value="${esc(contact?.linkedin_url || '')}" placeholder="https://www.linkedin.com/in/…"></label>
      <p class="cx-hint"><i class="fa-solid fa-shield-halved"></i> Work details only — personal email addresses (Gmail, Outlook.com …) aren't stored.</p>
      <p class="cx-error" hidden></p>
      <footer>${editing && contact.source === 'manual' ? '<button type="button" class="cx-btn cx-danger" data-delete>Delete</button>' : ''}
        <span class="cx-spacer"></span><button type="button" class="cx-btn" data-close>Cancel</button>
        <button type="submit" class="cx-btn cx-primary">${editing ? 'Save' : 'Add contact'}</button></footer>
    </form>`;
  document.body.appendChild(wrap);
  const form = wrap.querySelector('form');
  const err = wrap.querySelector('.cx-error');
  const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  wrap.addEventListener('click', async (e) => {
    if (e.target === wrap || e.target.closest('[data-close]')) { close(); return; }
    const del = e.target.closest('[data-delete]');
    if (del) {
      if (del.dataset.confirm !== '1') { del.dataset.confirm = '1'; del.textContent = 'Click again to delete'; return; }
      try { await api(`/api/crm/contacts/${contact.id}`, { method: 'DELETE' }); showToast('Contact deleted'); close(); onSaved && onSaved(null); }
      catch (ex) { err.textContent = ex.message; err.hidden = false; }
    }
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = Object.fromEntries(['full_name', 'title', 'email', 'phone', 'linkedin_url'].map(k => [k, form[k].value.trim() || null]));
    try {
      const saved = editing
        ? await api(`/api/crm/contacts/${contact.id}`, { method: 'PATCH', body })
        : await api('/api/crm/contacts', { method: 'POST', body: { ...body, account_id: accountId } });
      showToast(editing ? 'Contact saved' : 'Contact added');
      close();
      onSaved && onSaved(saved);
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  });
  form.full_name.focus();
}

// ── Account owner + business line ─────────────────────────────────────────────
export async function mountAccountCrm(el, accountId) {
  if (!el || !accountId) return;
  let d;
  try { d = await api(`/api/crm/accounts/${accountId}/crm`); } catch { el.innerHTML = ''; return; }
  const ro = !d.can_edit || isViewer();
  const render = () => {
    el.innerHTML = `<span class="cx-meta"><i class="fa-solid fa-user-tie"></i> Owner
        ${ro ? `<strong>${esc(d.owner_name || 'Unassigned')}</strong>` : `<select data-crm="owner" aria-label="Account owner">
          <option value="">Unassigned</option>${d.assignable.map(u => `<option value="${u.id}"${u.id === d.owner_user_id ? ' selected' : ''}>${esc(u.name)}</option>`).join('')}</select>`}</span>
      <span class="cx-meta"><i class="fa-solid fa-layer-group"></i> Business line
        ${ro ? `<strong>${esc(d.business_line_name || 'Not set')}</strong>` : `<select data-crm="bl" aria-label="Primary business line">
          <option value="">Not set</option>${d.business_lines.map(b => `<option value="${b.id}"${b.id === d.primary_business_line_id ? ' selected' : ''}>${esc(b.name)}</option>`).join('')}</select>`}</span>`;
  };
  render();
  el.onchange = async (e) => {
    const t = e.target.closest('[data-crm]');
    if (!t) return;
    const body = t.dataset.crm === 'owner'
      ? (t.value ? { owner_user_id: Number(t.value) } : { clear_owner: true })
      : (t.value ? { primary_business_line_id: Number(t.value) } : { clear_business_line: true });
    try { d = await api(`/api/crm/accounts/${accountId}`, { method: 'PATCH', body }); render(); showToast('Saved'); }
    catch (ex) { showToast(ex.message); render(); }
  };
}
