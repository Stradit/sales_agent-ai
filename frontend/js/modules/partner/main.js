// Partner portal (/partner): a partner / advisor sees only their own introductions (status timeline,
// StradIT contact, whether it became an opportunity), submits new ones and adds notes.
// API: /api/partner/* (apps/sales_crm/introductions.py). Admins may preview with ?connector_id=.
import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { getCurrentUser, logout, refreshAccessToken } from '../auth-client.js';
import { showToast } from '../toast.js';
import { esc } from '../utils.js';

const $ = (id) => document.getElementById(id);
const STEPS = ['proposed', 'requested', 'accepted', 'intro_made', 'meeting_held', 'converted'];
const STEP_LABEL = { proposed: 'Submitted', requested: 'Requested', accepted: 'Accepted', intro_made: 'Intro made',
  meeting_held: 'Meeting held', converted: 'Opportunity', declined: 'Declined', stale: 'On hold' };
const preview = new URLSearchParams(location.search).get('connector_id');
const q = preview ? `?connector_id=${encodeURIComponent(preview)}` : '';
let intros = [];
let openId = null;
let editId = null;

async function api(path, opts = {}) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(`/api/partner${path}${q ? sep + q.slice(1) : ''}`, { headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401) { location.replace('/login?next=/partner'); throw new Error('unauthenticated'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

const day = (ts) => (ts ? new Date(ts).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '');

function stepper(i) {
  if (['declined', 'stale'].includes(i.status)) return `<span class="pt-status ${i.status}">${STEP_LABEL[i.status]}</span>`;
  const idx = STEPS.indexOf(i.status);
  return `<ol class="pt-steps" aria-label="Progress: ${esc(STEP_LABEL[i.status])}">${STEPS.map((s, n) =>
    `<li class="${n < idx ? 'done' : n === idx ? 'current' : ''}"><span></span>${STEP_LABEL[s]}</li>`).join('')}</ol>`;
}

function renderKpis() {
  const count = (f) => intros.filter(f).length;
  const kpis = [['Submitted', intros.length, 'fa-paper-plane'],
    ['In progress', count(i => ['proposed', 'requested', 'accepted', 'intro_made'].includes(i.status)), 'fa-hourglass-half'],
    ['Meetings held', count(i => ['meeting_held', 'converted'].includes(i.status)), 'fa-people-group'],
    ['Became opportunities', count(i => i.status === 'converted'), 'fa-trophy']];
  $('ptKpis').innerHTML = kpis.map(([l, v, ic]) => `<div class="pt-kpi"><i class="fa-solid ${ic}"></i><strong>${v}</strong><span>${l}</span></div>`).join('');
}

function eventsHtml(events) {
  return `<ul class="pt-events">${events.map(e => `<li><span class="pt-dot ${e.kind}"></span><div>
    ${e.kind === 'note' ? esc(e.note) : `${e.to_label ? `<strong>${esc(STEP_LABEL[e.to_status] || e.to_label)}</strong>` : ''}${e.note ? `${e.to_label ? ' — ' : ''}${esc(e.note)}` : ''}`}
    <small>${esc(e.by)} · ${esc(day(e.at))}</small></div></li>`).join('')}</ul>`;
}

function card(i, detail) {
  return `<article class="pt-card pt-intro${openId === i.id ? ' open' : ''}" data-intro="${i.id}">
    <button type="button" class="pt-intro-head" data-toggle="${i.id}" aria-expanded="${openId === i.id}">
      <div><strong>${esc(i.contact_name || 'Contact')}</strong>${i.contact_title ? `<span>${esc(i.contact_title)}</span>` : ''}
        <span>${esc(i.account_name || '')}</span></div>
      <div class="pt-intro-meta">${i.opportunity_stage ? `<span class="pt-status converted"><i class="fa-solid fa-trophy"></i> Opportunity · ${esc(i.opportunity_stage)}</span>` : ''}
        <small>Updated ${esc(day(i.updated_at))}</small><i class="fa-solid fa-chevron-down"></i></div>
    </button>
    ${stepper(i)}
    ${openId === i.id && editId === i.id ? editForm(i) : ''}
    ${openId === i.id && editId !== i.id ? `<div class="pt-detail">
      ${i.context ? `<p class="pt-context"><strong>Your note:</strong> ${esc(i.context)}</p>` : ''}
      ${preview ? '' : i.editable
        ? `<div class="pt-detail-actions"><button type="button" class="pt-btn" data-edit="${i.id}"><i class="fa-solid fa-pen"></i> Edit introduction</button></div>`
        : (['proposed', 'requested', 'accepted', 'intro_made', 'meeting_held'].includes(i.status)
          ? '<p class="pt-muted"><i class="fa-solid fa-lock"></i> StradIT is working on this introduction, so its details are locked. Send an update below if something changed.</p>' : '')}
      <p class="pt-muted">${i.stradit_contact ? `Your StradIT contact: <strong>${esc(i.stradit_contact)}</strong>` : 'A StradIT team member will pick this up shortly.'}</p>
      ${detail ? eventsHtml(detail.events) : '<p class="pt-muted">Loading…</p>'}
      ${preview ? '' : `<form class="pt-note" data-note="${i.id}"><input maxlength="2000" placeholder="Add an update for the StradIT team…" aria-label="Add a note">
        <button type="submit" class="pt-btn pt-primary">Send</button></form>`}
    </div>` : ''}
  </article>`;
}

function editForm(i) {
  return `<form class="pt-detail pt-form pt-edit" data-edit-form="${i.id}" autocomplete="off" novalidate>
    <h2>Edit introduction</h2>
    <div class="pt-grid">
      <label>Company<input name="account_name" required minlength="2" maxlength="200" value="${esc(i.account_name || '')}"></label>
      <label>Contact name<input name="contact_name" required minlength="2" maxlength="200" value="${esc(i.contact_name || '')}"></label>
      <label>Their title <span>(optional)</span><input name="contact_title" maxlength="200" value="${esc(i.contact_title || '')}"></label>
      <label>Their work email <span>(optional)</span><input name="contact_email" type="email" maxlength="255" value="${esc(i.contact_email || '')}"></label>
    </div>
    <label>Why this introduction?<textarea name="context" required minlength="10" maxlength="4000" rows="4">${esc(i.context || '')}</textarea></label>
    <div class="pt-form-foot"><span class="pt-muted">The StradIT team is told what you changed.</span>
      <button type="button" class="pt-btn pt-ghost" data-edit-cancel>Cancel</button>
      <button type="submit" class="pt-btn pt-primary"><i class="fa-solid fa-check"></i> Save changes</button></div>
  </form>`;
}

let detailCache = {};
function renderList() {
  if (!intros.length) {
    $('ptList').innerHTML = `<div class="pt-card pt-empty"><i class="fa-solid fa-handshake"></i><h2>No introductions yet</h2>
      <p>When you introduce someone to StradIT, submit it here so you can follow its progress.</p></div>`;
    return;
  }
  $('ptList').innerHTML = intros.map(i => card(i, detailCache[i.id])).join('');
}

async function load() {
  try {
    const data = await api('/introductions');
    intros = data.introductions;
    renderKpis();
    renderList();
  } catch (err) { $('ptList').innerHTML = `<div class="pt-card pt-empty"><p>${esc(err.message)}</p></div>`; }
}

async function openIntro(id) {
  openId = openId === id ? null : id;
  editId = null;
  renderList();
  if (!openId) return;
  try { detailCache[id] = await api(`/introductions/${id}`); renderList(); } catch (err) { showToast(err.message); }
}

async function saveEdit(form) {
  if (!form.reportValidity()) return;
  const id = Number(form.dataset.editForm);
  const v = (n) => form.elements[n].value.trim();
  const btn = form.querySelector('[type="submit"]');
  btn.disabled = true;
  try {
    const out = await api(`/introductions/${id}`, { method: 'PATCH', body: {
      account_name: v('account_name'), contact_name: v('contact_name'), contact_title: v('contact_title') || null,
      contact_email: v('contact_email') || null, context: v('context'),
    } });
    detailCache[id] = out;
    intros = intros.map(x => (x.id === id ? { ...x, ...out } : x));
    editId = null;
    renderList();
    showToast('Saved — the StradIT team has been told');
  } catch (err) { showToast(err.message); btn.disabled = false; }
}

function wire() {
  $('ptNewBtn').addEventListener('click', () => { $('ptForm').hidden = false; $('ptCompany').focus(); });
  $('ptCancel').addEventListener('click', () => { $('ptForm').hidden = true; });
  $('ptForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      const out = await api('/introductions', { method: 'POST', body: {
        account_name: $('ptCompany').value.trim(), contact_name: $('ptContact').value.trim(),
        contact_title: $('ptTitle').value.trim() || null, contact_email: $('ptEmail').value.trim() || null, context: $('ptContext').value.trim(),
      } });
      $('ptForm').reset(); $('ptForm').hidden = true;
      showToast('Thank you — the StradIT team has been notified');
      openId = out.id; detailCache[out.id] = out;
      load();
    } catch (err) { showToast(err.message); }
  });
  $('ptList').addEventListener('click', (e) => {
    const t = e.target.closest('[data-toggle]');
    if (t) { openIntro(Number(t.dataset.toggle)); return; }
    const ed = e.target.closest('[data-edit]');
    if (ed) {
      editId = Number(ed.dataset.edit); renderList();
      const first = $('ptList').querySelector('[data-edit-form] input');
      if (first) first.focus();
      return;
    }
    if (e.target.closest('[data-edit-cancel]')) { editId = null; renderList(); }
  });
  $('ptList').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (e.target.dataset.editForm) { saveEdit(e.target); return; }
    const id = Number(e.target.dataset.note);
    const input = e.target.querySelector('input');
    if (!input.value.trim()) return;
    try { detailCache[id] = await api(`/introductions/${id}/notes`, { method: 'POST', body: { text: input.value.trim() } }); renderList(); showToast('Update sent'); }
    catch (err) { showToast(err.message); }
  });
  $('ptLogout').addEventListener('click', async () => { await logout(); location.replace('/login'); });
  $('ptMail').addEventListener('change', async (e) => {
    try { await api('/notifications', { method: 'PUT', body: { email_updates: e.target.checked } }); showToast('Saved'); }
    catch (err) { showToast(err.message); e.target.checked = !e.target.checked; }
  });
}

async function init() {
  initThemeToggle();
  await refreshAccessToken();
  const user = getCurrentUser();
  if (!user) { location.replace('/login?next=/partner'); return; }
  if (user.role !== 'partner' && !(user.role === 'super_admin' && preview)) {
    location.replace(user.role === 'super_admin' ? '/admin' : '/');
    return;
  }
  $('ptUser').textContent = user.full_name || user.email;
  if (preview) {
    $('ptPreview').hidden = false;
    $('ptPreview').innerHTML = '<i class="fa-solid fa-eye"></i> Admin preview — this is exactly what the partner sees. Notes can\'t be added in preview.';
    $('ptNewBtn').hidden = true;
  }
  try { const me = await api('/me'); $('ptOrg').textContent = me.connector.organisation || me.connector.name; } catch (err) { $('ptList').innerHTML = `<div class="pt-card pt-empty"><p>${esc(err.message)}</p></div>`; return; }
  wire();
  load();
  if (!preview) {
    try { const n = await api('/notifications'); $('ptMail').checked = n.email_updates; $('ptMailWrap').hidden = false; } catch { /* optional */ }
  }
}

init();
