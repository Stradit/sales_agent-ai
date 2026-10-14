// Introductions page (/introductions): warm-intro board (proposed → … → converted),
// intro drawer (triage, fields, convert to deal, timeline + notes), attribution report and
// connector management. API: apps/sales_crm/introductions.py. No AI requests.
import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { showToast } from '../toast.js';
import { esc } from '../utils.js';
import { downloadFile } from '../download.js';
import { mountTimeline } from '../activity-timeline.js';

const $ = (id) => document.getElementById(id);
const BOARD = ['proposed', 'requested', 'accepted', 'intro_made', 'meeting_held', 'converted'];
const OPEN = BOARD.slice(0, 5);
const KIND_LABEL = { partner: 'Partner', advisor: 'Advisor', employee: 'Employee', customer: 'Customer', other: 'Other' };
const EVENT_ICON = { status: 'fa-arrow-right', note: 'fa-comment', field: 'fa-pen', created: 'fa-flag', converted: 'fa-trophy' };
// Mirrors STAGE_NEEDS in introductions.py: entering these stages needs a chosen contact.
const NEEDS_CONTACT = ['accepted', 'intro_made', 'meeting_held'];
const STAGE_HINT = {
  proposed: 'Ask the connector to request the introduction.',
  requested: 'Waiting for the contact to agree to be introduced.',
  accepted: 'The contact agreed — get the introduction made.',
  intro_made: 'Introduced — book the first meeting.',
};

const st = {
  meta: null, intros: [], summary: {}, labels: {}, connectors: [], accounts: [], view: 'board',
  intro: null, dragId: null, partnerUsers: [], partnersState: 'idle', defaultPct: 50, newDirty: false,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) { location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); throw new Error('unauthenticated'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

// ── Formatting ───────────────────────────────────────────────────────────────
function money(v, cur = 'USD') {
  if (v == null) return '—';
  try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: cur || 'USD', notation: v >= 100000 ? 'compact' : 'standard', maximumFractionDigits: v >= 100000 ? 1 : 0 }).format(v); }
  catch { return `${Math.round(v).toLocaleString()} ${cur}`; }
}
function ago(ts) {
  if (!ts) return '';
  const s = (Date.now() - new Date(ts).getTime()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
const contactOf = (i) => i.persona_name || i.submitted.contact_name || 'Contact not chosen';
const accountOf = (i) => i.account_name || i.submitted.account_name || '—';
const readOnly = () => !!(st.meta && st.meta.me && st.meta.me.read_only && st.meta.auth_enforced);
const isAdmin = () => !!(st.meta && st.meta.me && (st.meta.me.role === 'super_admin' || !st.meta.auth_enforced));
const label = (s) => st.labels[s] || s;

// ── Contact suggestions (new-introduction form and drawer) ──────────────────
function attachSuggest(input, list, { search, onPick }) {
  let timer = null;
  let items = [];
  let active = -1;
  const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); active = -1; };
  const paint = () => {
    list.innerHTML = items.length
      ? items.map((p, n) => `<li role="option" id="${list.id}-${n}" data-n="${n}" aria-selected="${n === active}">${esc(p.name)}${p.title ? `<small>${esc(p.title)}</small>` : ''}</li>`).join('')
      : '<li class="in-suggest-empty" aria-disabled="true">No matching people at this account</li>';
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    if (active >= 0) input.setAttribute('aria-activedescendant', `${list.id}-${active}`); else input.removeAttribute('aria-activedescendant');
  };
  const run = async () => {
    try { items = await search(input.value.trim()); active = -1; if (document.activeElement === input) paint(); } catch { /* ignore */ }
  };
  const pick = (n) => { const p = items[n]; close(); if (p) onPick(p); };
  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 180); });
  input.addEventListener('focus', run);
  input.addEventListener('blur', () => setTimeout(close, 150));
  input.addEventListener('keydown', (e) => {
    if (list.hidden) return;
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && items.length) {
      e.preventDefault();
      active = (active + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      paint();
    } else if (e.key === 'Enter' && active >= 0) { e.preventDefault(); pick(active); }
    else if (e.key === 'Escape') { e.stopPropagation(); close(); }
  });
  list.addEventListener('mousedown', (e) => { const li = e.target.closest('li[data-n]'); if (li) { e.preventDefault(); pick(Number(li.dataset.n)); } });
}

const peopleAt = (accountId) => (q) => api(`/api/crm/people?account_id=${accountId}&q=${encodeURIComponent(q)}`);

// ── Board ────────────────────────────────────────────────────────────────────
function card(i) {
  const stale = OPEN.includes(i.status) && i.days_in_status >= 21;
  return `<article class="dl-card in-card" draggable="${OPEN.includes(i.status) && !readOnly()}" data-intro="${i.id}" tabindex="0"
      aria-label="${esc(contactOf(i))}, ${esc(accountOf(i))}, via ${esc(i.connector.name)}">
    <div class="dl-card-name">${esc(contactOf(i))}</div>
    <div class="dl-card-acct">${esc(accountOf(i))}${i.needs_triage ? ' <span class="in-pill warn">Needs triage</span>' : ''}</div>
    <div class="dl-card-row">
      <span class="in-pill"><i class="fa-solid fa-handshake"></i> ${esc(i.connector.name)}</span>
      ${i.business_line_name ? `<span>${esc(i.business_line_name)}</span>` : ''}
    </div>
    ${i.deal ? `<div class="dl-card-row"><span class="dl-value">${esc(money(i.deal.value_amount, i.deal.currency))}</span>
      <span>${esc(i.deal.stage)}</span>${i.attribution_pct != null ? `<span title="Attribution">${i.attribution_pct}%</span>` : ''}</div>` : ''}
    ${i.next_step ? `<div class="dl-card-next"><i class="fa-solid fa-arrow-right"></i> ${esc(i.next_step)}</div>` : ''}
    <div class="dl-card-row in-card-foot${stale ? ' in-stale' : ''}"><span>${i.owner_name ? esc(i.owner_name) : 'Unassigned'}</span>
      <span>${i.days_in_status}d in ${esc(i.status_label)}</span></div>
  </article>`;
}

function renderBoard() {
  $('inBoard').innerHTML = BOARD.map(s => {
    const items = st.intros.filter(i => i.status === s);
    return `<section class="dl-col" data-status="${s}" aria-label="${esc(st.labels[s])}">
      <div class="dl-col-head"><div class="dl-col-title">${esc(st.labels[s])} <span class="dl-count">${items.length}</span></div></div>
      <div class="dl-col-body">${items.map(card).join('') || `<div class="dl-empty-col">${s === 'converted' ? 'Drop here to convert' : 'Nothing here'}</div>`}</div>
    </section>`;
  }).join('');
  const closed = st.intros.filter(i => ['declined', 'stale'].includes(i.status));
  $('inClosed').innerHTML = closed.length ? `<details><summary>Declined ${st.summary.declined || 0} · Stale ${st.summary.stale || 0}</summary>
    <div class="dl-closed-grid">${closed.map(card).join('')}</div></details>` : '';
  const triage = st.intros.filter(i => i.needs_triage);
  $('inTriage').hidden = !triage.length;
  $('inTriage').innerHTML = triage.length ? `<i class="fa-solid fa-inbox"></i> <strong>${triage.length}</strong> partner submission${triage.length === 1 ? '' : 's'}
    need${triage.length === 1 ? 's' : ''} linking to an account: ${triage.slice(0, 4).map(i => `<button type="button" class="in-link" data-open-intro="${i.id}">${esc(i.submitted.account_name)}</button>`).join(', ')}` : '';
  const open = st.intros.filter(i => OPEN.includes(i.status)).length;
  const conv = st.summary.converted || 0;
  const total = st.intros.length;
  $('inSummary').textContent = total
    ? `${open} open · ${conv} converted${total ? ` (${Math.round(conv / total * 100)}% of ${total})` : ''}`
    : 'No introductions yet — log the first warm intro from a partner or advisor.';
}

function params() {
  const p = new URLSearchParams();
  if ($('inConnector').value) p.set('connector_id', $('inConnector').value);
  if ($('inBusinessLine').value) p.set('business_line_id', $('inBusinessLine').value);
  if ($('inMine').checked) p.set('mine', 'true');
  if ($('inSearch').value.trim()) p.set('q', $('inSearch').value.trim());
  return p;
}

async function loadIntros() {
  try {
    const data = await api(`/api/crm/introductions?${params()}`);
    st.intros = data.introductions;
    st.summary = data.summary;
    st.labels = Object.fromEntries(data.statuses.map(x => [x.key, x.label]));
    renderBoard();
  } catch (err) { $('inSummary').textContent = err.message; }
}

async function loadConnectors() {
  const data = await api('/api/crm/connectors');
  st.connectors = data.connectors;
  st.defaultPct = data.default_attribution_pct;
  const keep = $('inConnector').value;
  $('inConnector').innerHTML = '<option value="">All connectors</option>' + st.connectors.map(c =>
    `<option value="${c.id}">${esc(c.name)}${c.active ? '' : ' (inactive)'}</option>`).join('');
  $('inConnector').value = keep;
}

// ── Drawer ───────────────────────────────────────────────────────────────────
function field(text, input, wide = false) { return `<label class="dl-field${wide ? ' dl-field-wide' : ''}">${text}${input}</label>`; }

// Manual moves go one stage at a time; converting only happens from Meeting held (see check_transition).
function canStep(i, s) {
  if (readOnly() || !OPEN.includes(i.status) || s === i.status) return false;
  if (s === 'converted') return i.status === 'meeting_held';
  return Math.abs(BOARD.indexOf(s) - BOARD.indexOf(i.status)) === 1;
}

function stageBox(i) {
  if (i.status === 'meeting_held') {
    return `<div class="in-stage-box"><h4>Meeting held — record the outcome</h4>
      ${i.account_id ? '' : '<ul class="in-checklist"><li><i class="fa-solid fa-circle-exclamation"></i> Link it to an account before converting</li></ul>'}
      <div class="in-stage-actions" id="inActions">
        <button type="button" class="dl-btn dl-btn-primary" data-show-convert${i.can_convert ? '' : ' disabled'}><i class="fa-solid fa-trophy"></i> Convert to deal</button>
        <button type="button" class="dl-btn dl-btn-danger" data-decline><i class="fa-solid fa-circle-xmark"></i> Mark declined</button>
        <button type="button" class="dl-btn" data-stale><i class="fa-solid fa-hourglass-end"></i> Mark stale</button></div></div>`;
  }
  const next = i.next_status;
  const missing = i.next_missing || [];
  return `<div class="in-stage-box"><h4>${esc(STAGE_HINT[i.status] || '')} Next: ${esc(label(next))}</h4>
    <ul class="in-checklist">${missing.length
      ? missing.map(m => `<li><i class="fa-solid fa-circle-exclamation"></i> ${esc(m.charAt(0).toUpperCase() + m.slice(1))}</li>`).join('')
      : `<li><i class="fa-solid fa-circle-check"></i> Ready for ${esc(label(next))}</li>`}</ul>
    <div class="in-stage-actions" id="inActions">
      <button type="button" class="dl-btn dl-btn-primary" data-advance${missing.length ? ' disabled' : ''}>Move to ${esc(label(next))} <i class="fa-solid fa-arrow-right"></i></button>
      <button type="button" class="dl-btn dl-btn-danger" data-decline><i class="fa-solid fa-circle-xmark"></i> Decline</button>
      <button type="button" class="dl-btn" data-stale><i class="fa-solid fa-hourglass-end"></i> Mark stale</button></div></div>`;
}

function eventLine(i, e, ro) {
  const toggle = e.kind === 'note' && !ro && i.connector.has_login
    ? `<button type="button" class="in-link in-vis" data-vis="${e.id}" data-to="${e.partner_visible ? 'false' : 'true'}">${e.partner_visible ? 'Make internal' : 'Share with partner'}</button>` : '';
  return `<li><i class="fa-solid ${EVENT_ICON[e.kind] || 'fa-circle'}"></i>
    <div>${e.kind === 'status' || e.kind === 'converted' || e.kind === 'created'
      ? `${e.from_label ? `${esc(e.from_label)} → ` : ''}<strong>${esc(e.to_label || '')}</strong>${e.note ? ` — ${esc(e.note)}` : ''}`
      : esc(e.note || '')}
      ${e.partner_visible === false ? '<span class="in-pill">internal</span>' : ''}${toggle}
      <div class="dl-when">${esc(e.by || 'System')} · ${esc(ago(e.at))}</div></div></li>`;
}

function renderRoom() {
  const i = st.intro;
  if (!i) return;
  const ro = readOnly();
  const converted = i.status === 'converted';
  const closed = ['declined', 'stale'].includes(i.status);
  const idx = BOARD.indexOf(i.status);
  // The copilot dock reads this when opened, so questions are scoped to this intro's contact/account.
  window.doshinPageContext = {
    persona: i.persona_id ? { id: i.persona_id, name: i.persona_name } : null,
    account_id: i.account_id || null, account_name: i.account_name || null,
  };
  const blOptions = `<option value="">— Not set —</option>${(st.meta.business_lines || []).filter(b => b.active).map(b =>
    `<option value="${b.id}"${b.id === i.business_line_id ? ' selected' : ''}>${esc(b.name)}</option>`).join('')}`;
  $('inRoom').innerHTML = `
    <div class="dl-room-head">
      <div class="dl-room-head-row">
        <div><h2 class="dl-room-title" id="inRoomTitle">${esc(contactOf(i))}</h2>
          <div class="dl-room-acct">${i.account_id ? `<a href="/?account=${i.account_id}">${esc(accountOf(i))}</a>` : esc(accountOf(i))}
            · via <strong>${esc(i.connector.name)}</strong> <span class="in-pill">${esc(KIND_LABEL[i.connector.kind] || i.connector.kind)}</span></div></div>
        <button type="button" class="dl-icon" data-close-room aria-label="Close"><i class="fa-solid fa-xmark"></i></button>
      </div>
      <div class="dl-stepper" role="group" aria-label="Status">${BOARD.map((s, n) =>
        `<button type="button" class="dl-step${n < idx && !closed ? ' done' : ''}${s === i.status ? ' current' : ''}" data-status-step="${s}"
          aria-current="${s === i.status ? 'step' : 'false'}"${canStep(i, s) ? '' : ' disabled'}
          ${!canStep(i, s) && s !== i.status && OPEN.includes(i.status) && !ro ? `title="${s === 'converted' ? 'Convert once the meeting has been held' : 'Move one stage at a time'}"` : ''}>${esc(label(s))}</button>`).join('')}</div>
      ${closed ? `<div class="dl-warn">This introduction is <strong>${esc(i.status_label)}</strong>${i.closed_reason ? ` — ${esc(i.closed_reason)}` : ''}.
          ${ro ? '' : `<div class="in-stage-actions" id="inActions"><button type="button" class="dl-btn" data-reopen><i class="fa-solid fa-rotate-left"></i> Reopen at ${esc(label(i.reopen_to || 'proposed'))}</button></div>`}</div>`
        : converted ? '<p class="in-lock"><i class="fa-solid fa-lock"></i> Converted — the status is locked. Progress continues on the deal.</p>'
        : (ro ? '' : stageBox(i))}
    </div>
    <div class="dl-room-body">
      ${i.needs_triage ? `<div class="dl-section in-triage-box"><h3 class="dl-h3"><i class="fa-solid fa-inbox"></i> Partner submission — link it</h3>
        <p class="in-muted">Submitted: <strong>${esc(i.submitted.account_name)}</strong> · ${esc(i.submitted.contact_name || '')}
          ${i.submitted.contact_title ? ` (${esc(i.submitted.contact_title)})` : ''}${i.submitted.contact_email ? ` · ${esc(i.submitted.contact_email)}` : ''}</p>
        ${ro ? '' : `<div class="dl-add-row" style="margin-top:6px"><select id="inTriageAccount" aria-label="Account">
          <option value="">Choose the account…</option>${st.accounts.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('')}</select>
          <button type="button" class="dl-btn dl-btn-primary" data-link-account>Link account</button></div>`}</div>` : ''}

      ${i.deal ? `<div class="dl-section in-deal"><h3 class="dl-h3"><i class="fa-solid fa-diagram-next"></i> Opportunity</h3>
        <a class="in-deal-card" href="/deals?deal=${i.deal.id}"><strong>${esc(i.deal.name)}</strong>
          <span>${esc(i.deal.stage)} · ${esc(money(i.deal.value_amount, i.deal.currency))}</span>
          <span>${i.attribution_pct}% attributed to ${esc(i.connector.name)}${i.attributed_value != null ? ` = ${esc(money(i.attributed_value, i.deal.currency))}` : ''}</span></a></div>` : ''}

      <div class="in-convert" id="inConvert" hidden></div>

      <div class="dl-grid">
        ${i.account_id ? `<div class="dl-field dl-field-wide"><span>Contact${NEEDS_CONTACT.includes(i.next_status) || NEEDS_CONTACT.includes(i.status) ? '<span class="in-req" aria-hidden="true">*</span>' : ''}</span>
          <span class="in-suggest-wrap"><input id="inPersonSearch" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="inPersonList"
            aria-label="${i.persona_name ? 'Change contact' : 'Choose contact'}" placeholder="${i.persona_name ? 'Change contact — type a name…' : 'Search people at this account…'}" ${ro || converted ? 'disabled' : ''}>
            <ul class="in-suggest" id="inPersonList" role="listbox" aria-label="Matching contacts" hidden></ul></span>
          <div class="in-chosen">${i.persona_name ? `Current: <strong>${esc(i.persona_name)}</strong>${i.persona_title ? ` — ${esc(i.persona_title)}` : ''}` : 'No contact chosen yet'}</div></div>` : ''}
        ${field('Business line', `<select data-f="business_line_id" ${ro ? 'disabled' : ''}>${blOptions}</select>`)}
        ${['meeting_held', 'converted'].includes(i.status) ? field('Attribution %', `<input type="number" min="0" max="100" step="5" data-f="attribution_pct" value="${i.attribution_pct ?? ''}"
            placeholder="${esc(String(i.connector.default_attribution_pct ?? st.defaultPct))} (default)" ${ro ? 'disabled' : ''}>`) : ''}
        ${field(`Next step${OPEN.includes(i.status) ? ` <span class="in-muted">(${esc(label(i.status))})</span>` : ''}`, `<input data-f="next_step" maxlength="500" value="${esc(i.next_step || '')}" placeholder="What happens next?" ${ro ? 'disabled' : ''}>`, true)}
        <div class="dl-field">Owner<div class="in-owner">${esc(i.owner_name || 'Unassigned')}
          ${ro || (st.meta.me && i.owner_user_id === st.meta.me.id) ? '' : '<button type="button" class="in-link" data-assign-me>Assign to me</button>'}</div></div>
        ${field('Context', `<textarea data-f="context" maxlength="4000" rows="3" ${ro ? 'disabled' : ''}>${esc(i.context || '')}</textarea>`, true)}
      </div>
      <div class="in-stage-actions" id="inContactReason" hidden></div>

      ${i.account_id ? '<div class="dl-section"><h3 class="dl-h3">Customer activity</h3><div id="inActivity"></div></div>' : ''}

      <div class="dl-section"><h3 class="dl-h3">Introduction history</h3>
        ${ro ? '' : `<form class="dl-note-form" id="inNoteForm"><textarea id="inNote" maxlength="2000" placeholder="Add a note…" aria-label="Note"></textarea>
          <div class="in-note-side">${i.connector.has_login
            ? '<label class="dl-toggle"><input type="checkbox" id="inNotePartner"> Visible to partner</label>'
            : `<span class="in-share-note"><i class="fa-solid fa-circle-info"></i> ${esc(i.connector.name)} has no partner login, so notes stay internal.
                ${isAdmin() ? '<button type="button" class="in-link" data-goto-connectors>Link a login</button>' : 'Ask an admin to link one.'}</span>`}
          <button type="submit" class="dl-btn dl-btn-primary">Add</button></div></form>`}
        <ul class="dl-timeline">${[...i.events].reverse().map(e => eventLine(i, e, ro)).join('')}</ul>
      </div>
    </div>`;
  if ($('inPersonSearch') && !ro && !converted) {
    attachSuggest($('inPersonSearch'), $('inPersonList'), { search: peopleAt(i.account_id), onPick: changeContact });
  }
  if (i.account_id && $('inActivity')) {
    mountTimeline($('inActivity'), { objectType: 'introduction', objectId: i.id, accountId: i.account_id, personaId: i.persona_id || null,
      dealId: i.deal ? i.deal.id : null,
      onChange: () => { api(`/api/crm/introductions/${i.id}`).then(fresh => { if (st.intro && st.intro.id === fresh.id && fresh.status !== st.intro.status) { st.intro = fresh; renderRoom(); } loadIntros(); }).catch(() => {}); } });
  }
}

async function openIntro(id) {
  try {
    st.intro = await api(`/api/crm/introductions/${id}`);
    $('inOverlay').hidden = false;
    $('inRoom').hidden = false;
    renderRoom();
    const url = new URL(location.href); url.searchParams.set('intro', id); history.replaceState(null, '', url);
    $('inRoom').querySelector('[data-close-room]').focus();
  } catch (err) { showToast(err.message); }
}

function closeRoom() {
  $('inOverlay').hidden = true;
  $('inRoom').hidden = true;
  st.intro = null;
  window.doshinPageContext = null;
  const url = new URL(location.href); url.searchParams.delete('intro'); history.replaceState(null, '', url);
}

async function patchIntro(body, id = st.intro && st.intro.id) {
  try {
    const out = await api(`/api/crm/introductions/${id}`, { method: 'PATCH', body });
    if (st.intro && st.intro.id === id) { st.intro = out; renderRoom(); }
    loadIntros();
    return out;
  } catch (err) { showToast(err.message); return null; }
}

function inlinePrompt(container, placeholder, okLabel) {
  return new Promise((resolve) => {
    container.innerHTML = `<input class="in-inline-input" maxlength="500" placeholder="${esc(placeholder)}" aria-label="${esc(placeholder)}">
      <button type="button" class="dl-btn dl-btn-primary" data-ok>${esc(okLabel)}</button><button type="button" class="dl-btn" data-cancel>Cancel</button>`;
    const input = container.querySelector('input');
    input.focus();
    const done = (v) => { resolve(v); renderRoom(); };
    container.querySelector('[data-ok]').onclick = () => { if (input.value.trim()) done(input.value.trim()); else input.focus(); };
    container.querySelector('[data-cancel]').onclick = () => done(null);
    input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); container.querySelector('[data-ok]').click(); } if (e.key === 'Escape') { e.stopPropagation(); done(null); } };
  });
}

async function showConvert() {
  const i = st.intro;
  const box = $('inConvert');
  let deals = [];
  try { deals = (await api(`/api/deals?account_id=${i.account_id}&include_closed=false`)).deals.filter(d => !d.introduction); } catch { /* optional */ }
  const blOptions = `<option value="">— Not set —</option>${(st.meta.business_lines || []).filter(b => b.active).map(b =>
    `<option value="${b.id}"${b.id === i.business_line_id ? ' selected' : ''}>${esc(b.name)}</option>`).join('')}`;
  box.innerHTML = `<h3 class="dl-h3"><i class="fa-solid fa-trophy"></i> Convert to an opportunity</h3>
    <div class="in-radio"><label><input type="radio" name="inConvMode" value="new" checked> Create a new deal (Intro stage)</label>
      <label><input type="radio" name="inConvMode" value="link"${deals.length ? '' : ' disabled'}> Link an existing deal${deals.length ? ` (${deals.length})` : ' (none open)'}</label></div>
    <div class="dl-grid" id="inConvNew">
      ${field('Deal name', `<input id="inConvName" maxlength="200" value="${esc(`${accountOf(i)} — intro via ${i.connector.name}`)}">`, true)}
      ${field('Value', '<input id="inConvValue" type="number" min="0" step="1000" placeholder="250000">')}
      ${field('Currency', '<select id="inConvCurrency"><option>USD</option><option>EUR</option><option>GBP</option><option>INR</option></select>')}
      ${field('Expected close', '<input id="inConvClose" type="date">')}
      ${field('Business line', `<select id="inConvBl">${blOptions}</select>`)}
    </div>
    <div id="inConvLink" hidden>${field('Deal', `<select id="inConvDeal">${deals.map(d => `<option value="${d.id}">${esc(d.name)} · ${esc(d.stage_label)}</option>`).join('')}</select>`, true)}</div>
    <div class="dl-grid">${field('Attribution %', `<input id="inConvPct" type="number" min="0" max="100" step="5" value="${i.attribution_pct ?? st.defaultPct}">`)}</div>
    <div class="in-convert-foot"><button type="button" class="dl-btn" data-cancel-convert>Cancel</button>
      <button type="button" class="dl-btn dl-btn-primary" data-do-convert><i class="fa-solid fa-trophy"></i> Convert</button></div>`;
  box.hidden = false;
  box.querySelectorAll('input[name="inConvMode"]').forEach(r => r.addEventListener('change', () => {
    const link = box.querySelector('input[name="inConvMode"]:checked').value === 'link';
    $('inConvNew').hidden = link; $('inConvLink').hidden = !link;
  }));
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  $('inConvName').focus();
}

async function doConvert() {
  const link = document.querySelector('input[name="inConvMode"]:checked').value === 'link';
  const pct = $('inConvPct').value === '' ? null : Number($('inConvPct').value);
  const body = link ? { deal_id: Number($('inConvDeal').value), attribution_pct: pct } : {
    name: $('inConvName').value.trim(), value_amount: $('inConvValue').value === '' ? null : Number($('inConvValue').value),
    currency: $('inConvCurrency').value, expected_close: $('inConvClose').value || null,
    business_line_id: $('inConvBl').value ? Number($('inConvBl').value) : null, attribution_pct: pct,
  };
  try {
    const out = await api(`/api/crm/introductions/${st.intro.id}/convert`, { method: 'POST', body });
    st.intro = out;
    renderRoom();
    loadIntros();
    showToast('Converted — the deal is on the pipeline board');
  } catch (err) { showToast(err.message); }
}

// Status moves from the stepper, the "Move to" button and board drag-and-drop all land here.
async function moveTo(target) {
  const i = st.intro;
  if (!i || target === i.status) return;
  if (target === 'converted') {
    if (i.can_convert) showConvert();
    else showToast(i.status === 'meeting_held' ? 'Link an account first' : 'Convert once the meeting has been held');
    return;
  }
  const from = BOARD.indexOf(i.status);
  const to = BOARD.indexOf(target);
  if (from < 0 || Math.abs(to - from) !== 1) { showToast('Move one stage at a time'); return; }
  if (to < from) {
    const box = $('inActions');
    if (!box) return;
    const reason = await inlinePrompt(box, `Why move back to ${label(target)}? (required)`, 'Move back');
    if (reason) patchIntro({ status: target, reason });
    return;
  }
  if ((i.next_missing || []).length) { showToast(`Before ${label(target)}: ${i.next_missing.join(' and ')}`); return; }
  patchIntro({ status: target });
}

async function changeContact(p) {
  const i = st.intro;
  if (!i || p.id === i.persona_id) return;
  let reason = null;
  if (i.persona_id && i.status !== 'proposed') {
    const box = $('inContactReason');
    box.hidden = false;
    reason = await inlinePrompt(box, `Why change the contact to ${p.name}? (required)`, 'Change contact');
    if (!reason) return;
  }
  patchIntro({ persona_id: p.id, reason });
}

function wireRoom() {
  const room = $('inRoom');
  room.addEventListener('click', async (e) => {
    const t = e.target;
    if (t.closest('[data-close-room]')) { closeRoom(); return; }
    const step = t.closest('[data-status-step]');
    if (step && !step.disabled) { moveTo(step.dataset.statusStep); return; }
    if (t.closest('[data-advance]')) { moveTo(st.intro.next_status); return; }
    if (t.closest('[data-show-convert]')) { moveTo('converted'); return; }
    if (t.closest('[data-cancel-convert]')) { $('inConvert').hidden = true; return; }
    if (t.closest('[data-do-convert]')) { doConvert(); return; }
    if (t.closest('[data-decline]')) {
      const reason = await inlinePrompt($('inActions'), 'Why is it declined? (required)', 'Decline');
      if (reason) patchIntro({ status: 'declined', closed_reason: reason });
      return;
    }
    if (t.closest('[data-stale]')) {
      const reason = await inlinePrompt($('inActions'), 'Why is it stale? (required)', 'Mark stale');
      if (reason) patchIntro({ status: 'stale', reason });
      return;
    }
    if (t.closest('[data-reopen]')) {
      const to = st.intro.reopen_to || 'proposed';
      const reason = await inlinePrompt($('inActions'), `Why reopen it at ${label(to)}? (required)`, 'Reopen');
      if (reason) patchIntro({ status: to, reason });
      return;
    }
    const vis = t.closest('[data-vis]');
    if (vis) {
      try {
        st.intro = await api(`/api/crm/introductions/${st.intro.id}/events/${vis.dataset.vis}`, { method: 'PATCH', body: { partner_visible: vis.dataset.to === 'true' } });
        renderRoom();
        showToast(vis.dataset.to === 'true' ? 'Note shared with the partner' : 'Note is internal again');
      } catch (err) { showToast(err.message); }
      return;
    }
    if (t.closest('[data-goto-connectors]')) { closeRoom(); document.querySelector('.in-tab[data-view="connectors"]').click(); return; }
    if (t.closest('[data-assign-me]')) { patchIntro({ owner_user_id: st.meta.me.id }); return; }
    if (t.closest('[data-link-account]')) {
      const a = $('inTriageAccount').value;
      if (!a) { showToast('Choose the account first'); return; }
      patchIntro({ account_id: Number(a) });
    }
  });
  room.addEventListener('change', (e) => {
    const t = e.target;
    const f = t.dataset.f;
    if (!f) return;
    let v = t.value;
    if (['business_line_id', 'attribution_pct'].includes(f)) v = v === '' ? null : Number(v);
    else v = v.trim() || null;
    if (f === 'attribution_pct' && v != null && !(v >= 0 && v <= 100)) { showToast('Attribution must be a number from 0 to 100'); renderRoom(); return; }
    patchIntro({ [f]: v });
  });
  room.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (e.target.id !== 'inNoteForm') return;
    const text = $('inNote').value.trim();
    if (!text) return;
    try {
      st.intro = await api(`/api/crm/introductions/${st.intro.id}/notes`, { method: 'POST', body: { text, partner_visible: !!($('inNotePartner') && $('inNotePartner').checked) } });
      renderRoom();
    } catch (err) { showToast(err.message); }
  });
}

// ── New introduction ─────────────────────────────────────────────────────────
async function ensureAccounts() {
  if (st.accounts.length) return;
  const res = await fetch('/api/copilot/context');
  if (!res.ok) throw new Error(`Couldn't load accounts (${res.status})`);
  st.accounts = (await res.json()).accounts || [];
}

const NEW_ERR = { inNewConnector: 'inNewConnectorErr', inNcName: 'inNcNameErr', inNewAccount: 'inNewAccountErr', inNewContactSearch: 'inNewContactErr' };

function setNewError(id, msg) {
  const err = $(NEW_ERR[id]);
  err.textContent = msg || '';
  err.hidden = !msg;
  if (msg) $(id).setAttribute('aria-invalid', 'true'); else $(id).removeAttribute('aria-invalid');
}

function syncContactRequired() {
  $('inNewContactReq').hidden = !NEEDS_CONTACT.includes($('inNewStatus').value);
}

function validateNew() {
  const errs = {};
  const addingConnector = !$('inNewConnectorBox').hidden && $('inNcName').value.trim();
  if (!$('inNewConnector').value && !addingConnector) {
    errs.inNewConnector = 'Choose who made the introduction, or add a new connector.';
    if (!$('inNewConnectorBox').hidden) errs.inNcName = 'Enter the new connector’s name.';
  }
  if (!$('inNewAccount').value) errs.inNewAccount = 'Choose the account.';
  if ($('inNewContactSearch').value.trim() && !$('inNewContact').value) errs.inNewContactSearch = 'Pick a contact from the suggestions, or clear the field.';
  else if (NEEDS_CONTACT.includes($('inNewStatus').value) && !$('inNewContact').value) {
    errs.inNewContactSearch = `Choose the contact — it’s required to start at ${label($('inNewStatus').value)}.`;
  }
  Object.keys(NEW_ERR).forEach(id => setNewError(id, errs[id]));
  const first = Object.keys(NEW_ERR).find(id => errs[id]);
  if (first) $(first).focus();
  return !first;
}

async function openNew() {
  try { await ensureAccounts(); } catch (err) { showToast(err.message); return; }
  $('inNewForm').reset();
  Object.keys(NEW_ERR).forEach(id => setNewError(id, ''));
  const active = st.connectors.filter(c => c.active);
  $('inNewConnectorBox').hidden = !!active.length;
  $('inNewConnector').innerHTML = active.length
    ? '<option value="">Choose a connector…</option>' + active.map(c =>
      `<option value="${c.id}">${esc(c.name)}${c.organisation ? ` — ${esc(c.organisation)}` : ''}</option>`).join('')
    : '<option value="">No connectors yet — add one below</option>';
  $('inNewAccount').innerHTML = '<option value="">Choose an account…</option>' + st.accounts.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
  $('inNewBusinessLine').innerHTML = '<option value="">— Not set —</option>' + (st.meta.business_lines || []).filter(b => b.active)
    .map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
  $('inNewStatus').innerHTML = OPEN.map(s => `<option value="${s}">${esc(label(s))}</option>`).join('');
  $('inNewContact').value = '';
  $('inNewContactSearch').disabled = true;
  $('inNewContactSearch').placeholder = 'Choose an account first';
  syncContactRequired();
  st.newDirty = false;
  $('inModal').hidden = false;
  (active.length ? $('inNewConnector') : $('inNcName')).focus();
}

function closeNew() { $('inModal').hidden = true; st.newDirty = false; }

function wireNew() {
  $('inNew').addEventListener('click', openNew);
  // Only the Close / Cancel buttons dismiss the form — a stray click outside must not lose the input.
  $('inModal').addEventListener('click', (e) => { if (e.target.closest('[data-close-modal]')) closeNew(); });
  $('inNewForm').addEventListener('input', (e) => {
    st.newDirty = true;
    if (NEW_ERR[e.target.id]) setNewError(e.target.id, '');
  });
  $('inNewForm').addEventListener('change', (e) => {
    st.newDirty = true;
    if (NEW_ERR[e.target.id]) setNewError(e.target.id, '');
    if (e.target.id === 'inNewConnector') setNewError('inNcName', '');
    if (e.target.id === 'inNewStatus') { syncContactRequired(); setNewError('inNewContactSearch', ''); }
  });
  $('inNewConnectorToggle').addEventListener('click', () => { $('inNewConnectorBox').hidden = !$('inNewConnectorBox').hidden; if (!$('inNewConnectorBox').hidden) $('inNcName').focus(); });
  attachSuggest($('inNewContactSearch'), $('inNewContactList'), {
    search: (q) => ($('inNewAccount').value ? peopleAt($('inNewAccount').value)(q) : Promise.resolve([])),
    onPick: (p) => { $('inNewContact').value = p.id; $('inNewContactSearch').value = p.title ? `${p.name} — ${p.title}` : p.name; setNewError('inNewContactSearch', ''); },
  });
  $('inNewContactSearch').addEventListener('input', () => { $('inNewContact').value = ''; });
  $('inNewAccount').addEventListener('change', () => {
    $('inNewContact').value = '';
    $('inNewContactSearch').value = '';
    $('inNewContactSearch').disabled = !$('inNewAccount').value;
    $('inNewContactSearch').placeholder = $('inNewAccount').value ? 'Type to search people at this account…' : 'Choose an account first';
  });
  $('inNewForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!validateNew()) return;
    try {
      let connectorId = Number($('inNewConnector').value) || null;
      if (!$('inNewConnectorBox').hidden && $('inNcName').value.trim()) {
        const c = await api('/api/crm/connectors', { method: 'POST', body: { name: $('inNcName').value.trim(), kind: $('inNcKind').value, organisation: $('inNcOrg').value.trim() || null } });
        connectorId = c.id;
        await loadConnectors();
      }
      if (!connectorId) { setNewError('inNewConnector', 'Choose or add a connector.'); return; }
      const out = await api('/api/crm/introductions', { method: 'POST', body: {
        connector_id: connectorId, account_id: Number($('inNewAccount').value),
        persona_id: $('inNewContact').value ? Number($('inNewContact').value) : null,
        business_line_id: $('inNewBusinessLine').value ? Number($('inNewBusinessLine').value) : null,
        status: $('inNewStatus').value, context: $('inNewContext').value.trim() || null,
      } });
      closeNew();
      await loadIntros();
      openIntro(out.id);
      showToast('Introduction logged');
    } catch (err) { showToast(err.message); }
  });
}

// ── Attribution & connectors views ──────────────────────────────────────────
async function renderAttribution() {
  const box = $('inViewAttribution');
  box.innerHTML = '<p class="in-muted">Loading…</p>';
  try {
    const p = new URLSearchParams();
    if ($('inBusinessLine').value) p.set('business_line_id', $('inBusinessLine').value);
    const data = await api(`/api/crm/introductions/attribution?${p}`);
    const rows = data.connectors;
    const sum = (k) => rows.reduce((a, r) => a + (r[k] || 0), 0);
    box.innerHTML = `<div class="in-view-head"><p class="in-muted">Sourced = value of deals that came from the connector's introductions.
        Attributed = sourced × the intro's attribution % (default ${esc(String(st.defaultPct))}%).${data.note ? ` ${esc(data.note)}` : ''}</p>
      <button type="button" class="dl-btn" data-export-attribution><i class="fa-regular fa-file-excel"></i> Export</button></div>
      <div class="in-kpis">
        <div><span>Introductions</span><strong>${sum('intros')}</strong></div>
        <div><span>Converted</span><strong>${sum('converted')}</strong></div>
        <div><span>Attributed pipeline</span><strong>${esc(money(sum('attributed_pipeline')))}</strong></div>
        <div><span>Attributed won</span><strong>${esc(money(sum('attributed_won')))}</strong></div>
      </div>
      ${rows.length ? `<div class="in-table-wrap"><table class="in-table"><thead><tr><th scope="col">Connector</th><th scope="col">Intros</th><th scope="col">Open</th>
        <th scope="col">Intros made</th><th scope="col">Converted</th><th scope="col">Conv. %</th><th scope="col">Sourced pipeline</th><th scope="col">Attributed pipeline</th>
        <th scope="col">Sourced won</th><th scope="col">Attributed won</th></tr></thead><tbody>
        ${rows.map(r => `<tr><td><button type="button" class="in-link" data-filter-connector="${r.id}">${esc(r.name)}</button>
            <div class="in-muted">${esc(KIND_LABEL[r.kind] || r.kind)}${r.organisation ? ` · ${esc(r.organisation)}` : ''}</div></td>
          <td>${r.intros}</td><td>${r.open}</td><td>${r.intros_made}</td><td>${r.converted}</td><td>${r.conversion_rate}%</td>
          <td>${esc(money(r.sourced_pipeline))}</td><td>${esc(money(r.attributed_pipeline))}</td>
          <td>${esc(money(r.sourced_won))}</td><td><strong>${esc(money(r.attributed_won))}</strong></td></tr>`).join('')}
        </tbody></table></div>` : '<p class="in-muted">No introductions yet.</p>'}`;
  } catch (err) { box.innerHTML = `<p class="in-muted">${esc(err.message)}</p>`; }
}

function renderConnectors() {
  const box = $('inViewConnectors');
  const ro = readOnly();
  const admin = isAdmin();
  const freePartners = st.partnerUsers.filter(u => !st.connectors.some(c => c.user_id === u.id));
  const linkCell = () => {
    if (st.partnersState === 'loading' || st.partnersState === 'idle') return '<span class="in-muted"><i class="fa-solid fa-spinner fa-spin"></i> Loading partner logins…</span>';
    if (st.partnersState === 'error') return '<span class="in-muted">Couldn’t load partner logins. <button type="button" class="in-link" data-retry-partners>Retry</button></span>';
    if (!freePartners.length) return '<span class="in-muted">No unlinked partner accounts found — <a class="in-link" href="/admin">create one in Admin</a></span>';
    return `<select data-link-login aria-label="Link partner login"><option value="">Link a partner login…</option>${freePartners.map(u =>
      `<option value="${u.id}">${esc(u.full_name || u.email)}</option>`).join('')}</select>`;
  };
  box.innerHTML = `${ro ? '' : `<form class="in-conn-form" id="inConnForm">
      <input id="inCfName" maxlength="200" placeholder="Name (person or firm)" required aria-label="Connector name">
      <select id="inCfKind" aria-label="Kind">${Object.entries(KIND_LABEL).map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select>
      <input id="inCfOrg" maxlength="200" placeholder="Organisation" aria-label="Organisation">
      <input id="inCfEmail" maxlength="255" type="email" placeholder="Work email" aria-label="Work email">
      <input id="inCfPct" type="number" min="0" max="100" step="5" placeholder="Attribution % (${esc(String(st.defaultPct))})" aria-label="Default attribution %">
      <button type="submit" class="dl-btn dl-btn-primary"><i class="fa-solid fa-plus"></i> Add connector</button></form>`}
    <div class="in-table-wrap"><table class="in-table"><thead><tr><th scope="col">Connector</th><th scope="col">Kind</th><th scope="col">Default %</th>
      <th scope="col">Intros</th><th scope="col">Open</th><th scope="col">Converted</th><th scope="col">Partner login</th><th scope="col">Active</th></tr></thead><tbody>
    ${st.connectors.map(c => `<tr data-connector="${c.id}"><td><strong>${esc(c.name)}</strong><div class="in-muted">${esc(c.organisation || '')}${c.email ? ` · ${esc(c.email)}` : ''}</div></td>
      <td>${esc(KIND_LABEL[c.kind] || c.kind)}</td>
      <td><input type="number" min="0" max="100" step="5" class="in-pct" data-cf="default_attribution_pct" value="${c.default_attribution_pct ?? ''}" placeholder="${esc(String(st.defaultPct))}" ${ro ? 'disabled' : ''} aria-label="Default attribution %"></td>
      <td>${c.intros}</td><td>${c.open}</td><td>${c.converted}</td>
      <td>${c.user_id ? `${esc(c.login_email || '')}${admin ? ` <a class="in-link" href="/partner?connector_id=${c.id}" target="_blank" rel="noopener">Preview</a> <button type="button" class="in-link" data-unlink>Unlink</button>` : ''}`
        : (admin ? linkCell() : '<span class="in-muted">—</span>')}</td>
      <td><input type="checkbox" data-cf="active" ${c.active ? 'checked' : ''} ${ro ? 'disabled' : ''} aria-label="Active"></td></tr>`).join('')
      || '<tr><td colspan="8" class="in-muted">No connectors yet.</td></tr>'}</tbody></table></div>
    ${admin ? '<p class="in-muted">Partner logins are users with the Partner / Advisor role (create them on the Admin page). A linked partner sees only their own introductions.</p>' : ''}`;
}

function wireViews() {
  document.querySelectorAll('.in-tab').forEach(tab => tab.addEventListener('click', () => {
    st.view = tab.dataset.view;
    document.querySelectorAll('.in-tab').forEach(t => { t.classList.toggle('active', t === tab); t.setAttribute('aria-selected', String(t === tab)); });
    $('inViewBoard').hidden = st.view !== 'board';
    $('inViewAttribution').hidden = st.view !== 'attribution';
    $('inViewConnectors').hidden = st.view !== 'connectors';
    if (st.view === 'attribution') renderAttribution();
    if (st.view === 'connectors') renderConnectors();
  }));
  $('inViewAttribution').addEventListener('click', (e) => {
    if (e.target.closest('[data-export-attribution]')) {
      downloadFile('/api/crm/introductions/attribution/export', 'introduction-attribution.xlsx').catch(err => showToast(err.message));
      return;
    }
    const f = e.target.closest('[data-filter-connector]');
    if (f) { $('inConnector').value = f.dataset.filterConnector; document.querySelector('.in-tab[data-view="board"]').click(); loadIntros(); }
  });
  const conn = $('inViewConnectors');
  conn.addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/crm/connectors', { method: 'POST', body: {
        name: $('inCfName').value.trim(), kind: $('inCfKind').value, organisation: $('inCfOrg').value.trim() || null,
        email: $('inCfEmail').value.trim() || null, default_attribution_pct: $('inCfPct').value === '' ? null : Number($('inCfPct').value),
      } });
      await loadConnectors();
      renderConnectors();
      showToast('Connector added');
    } catch (err) { showToast(err.message); }
  });
  conn.addEventListener('change', async (e) => {
    const row = e.target.closest('[data-connector]');
    if (!row) return;
    const id = row.dataset.connector;
    let body = null;
    if (e.target.dataset.cf === 'active') body = { active: e.target.checked };
    else if (e.target.dataset.cf === 'default_attribution_pct') body = { default_attribution_pct: e.target.value === '' ? null : Number(e.target.value) };
    else if (e.target.hasAttribute('data-link-login') && e.target.value) body = { user_id: Number(e.target.value) };
    if (!body) return;
    try { await api(`/api/crm/connectors/${id}`, { method: 'PATCH', body }); await loadConnectors(); renderConnectors(); showToast('Saved'); }
    catch (err) { showToast(err.message); renderConnectors(); }
  });
  conn.addEventListener('click', async (e) => {
    if (e.target.closest('[data-retry-partners]')) { loadPartnerUsers(); return; }
    if (!e.target.closest('[data-unlink]')) return;
    const id = e.target.closest('[data-connector]').dataset.connector;
    try { await api(`/api/crm/connectors/${id}`, { method: 'PATCH', body: { unlink_user: true } }); await loadConnectors(); renderConnectors(); }
    catch (err) { showToast(err.message); }
  });
}

// ── Board interactions ───────────────────────────────────────────────────────
function wireBoard() {
  const board = $('inBoard');
  const openCard = (e) => { const c = e.target.closest('[data-intro]'); if (c) openIntro(Number(c.dataset.intro)); };
  board.addEventListener('click', openCard);
  $('inClosed').addEventListener('click', openCard);
  $('inTriage').addEventListener('click', (e) => { const b = e.target.closest('[data-open-intro]'); if (b) openIntro(Number(b.dataset.openIntro)); });
  board.addEventListener('keydown', (e) => { if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('[data-intro]')) { e.preventDefault(); openCard(e); } });
  board.addEventListener('dragstart', (e) => {
    const c = e.target.closest('[data-intro]');
    if (!c) return;
    st.dragId = Number(c.dataset.intro);
    c.classList.add('dragging');
    e.dataTransfer.setData('text/plain', c.dataset.intro);
  });
  board.addEventListener('dragend', (e) => { const c = e.target.closest('[data-intro]'); if (c) c.classList.remove('dragging'); });
  board.addEventListener('dragover', (e) => { const col = e.target.closest('.dl-col'); if (col && st.dragId) { e.preventDefault(); col.classList.add('drop-hover'); } });
  board.addEventListener('dragleave', (e) => { const col = e.target.closest('.dl-col'); if (col) col.classList.remove('drop-hover'); });
  board.addEventListener('drop', async (e) => {
    const col = e.target.closest('.dl-col');
    if (!col || !st.dragId) return;
    e.preventDefault();
    col.classList.remove('drop-hover');
    const id = st.dragId; st.dragId = null;
    const intro = st.intros.find(i => i.id === id);
    const target = col.dataset.status;
    if (!intro || intro.status === target) return;
    const step = BOARD.indexOf(target) - BOARD.indexOf(intro.status);
    if (target !== 'converted' && Math.abs(step) !== 1) { showToast('Move one stage at a time'); return; }
    // Converting and moving back need input (deal details / a reason): do it in the drawer.
    if (target === 'converted' || step < 0) { await openIntro(id); if (st.intro && st.intro.id === id) moveTo(target); return; }
    patchIntro({ status: target }, id);   // the server checks the stage criteria
  });
}

async function loadPartnerUsers() {
  if (!isAdmin()) return;
  st.partnersState = 'loading';
  if (st.view === 'connectors') renderConnectors();
  try {
    const team = await api('/api/crm/team');
    st.partnerUsers = team.filter(u => u.role === 'partner' && u.is_active);
    st.partnersState = 'ready';
  } catch { st.partnersState = 'error'; }
  if (st.view === 'connectors') renderConnectors();
}

function wireBack() {
  // Return to wherever the user came from in this app; a direct visit falls back to the deals pipeline.
  $('inBack').addEventListener('click', (e) => {
    let internal = false;
    try {
      const ref = document.referrer ? new URL(document.referrer) : null;
      internal = !!ref && ref.origin === location.origin && ref.pathname !== location.pathname && !ref.pathname.startsWith('/login');
    } catch { /* ignore */ }
    if (internal && history.length > 1) { e.preventDefault(); history.back(); }
  });
}

function wireNumberInputs() {
  // type=number still accepts e, +, - and pasted text; keep these fields digits (and one decimal point) only.
  document.addEventListener('keydown', (e) => {
    if (e.target.matches && e.target.matches('input[type="number"]') && ['e', 'E', '+', '-'].includes(e.key)) e.preventDefault();
  });
  document.addEventListener('paste', (e) => {
    if (!(e.target.matches && e.target.matches('input[type="number"]'))) return;
    const text = (e.clipboardData && e.clipboardData.getData('text')) || '';
    if (!/^\s*\d*(\.\d*)?\s*$/.test(text)) { e.preventDefault(); showToast('Numbers only'); }
  });
}

// ── Init ─────────────────────────────────────────────────────────────────────
async function init() {
  try {
    st.meta = await api('/api/crm/meta');
    await loadConnectors();
    ensureAccounts().catch(() => {});
    loadPartnerUsers();
  } catch (err) { showToast(err.message); return; }
  $('inBusinessLine').innerHTML = '<option value="">All business lines</option>' + (st.meta.business_lines || [])
    .map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
  if (readOnly()) $('inNew').hidden = true;
  const p = new URLSearchParams(location.search);
  if (p.get('connector_id')) $('inConnector').value = p.get('connector_id');
  let t = null;
  $('inSearch').addEventListener('input', () => { clearTimeout(t); t = setTimeout(loadIntros, 250); });
  ['inConnector', 'inMine'].forEach(id => $(id).addEventListener('change', loadIntros));
  $('inBusinessLine').addEventListener('change', () => { loadIntros(); if (st.view === 'attribution') renderAttribution(); });
  $('inExport').addEventListener('click', () => {
    const q = new URLSearchParams();
    if ($('inConnector').value) q.set('connector_id', $('inConnector').value);
    downloadFile(`/api/crm/introductions/export?${q}`, 'introductions.xlsx').catch(err => showToast(err.message));
  });
  $('inOverlay').addEventListener('click', closeRoom);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('inModal').hidden) {
      if (st.newDirty) showToast('Unsaved introduction — use Cancel to discard it');
      else closeNew();
    } else if (!$('inRoom').hidden) closeRoom();
  });
  wireBack();
  wireNumberInputs();
  wireBoard();
  wireRoom();
  wireNew();
  wireViews();
  await loadIntros();
  if (p.get('intro')) openIntro(Number(p.get('intro')));
  if (p.get('new') === '1') openNew();
}

initThemeToggle();
initTopbarAuth().then((user) => {
  if (!user) { document.body.style.display = 'none'; location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`); return; }
  init();
});
