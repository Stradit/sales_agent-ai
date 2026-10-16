// Sales Copilot workspace page (/copilot). See apps/sales_copilot/README.md §19.
// URL params: ?persona_id= / ?account_id= open a chat scoped to that contact or
// account; ?deal_id= adds the deal (stage, committee, MEDDICC) to every answer;
// ?q= prefills the composer without sending; ?session= reopens a saved chat. The chat surface itself lives in
// chat-view.js (shared with the dock on other pages).
import '../fetch-instrumentation.js'; // must load first — attaches the bearer token to fetch()
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { showToast } from '../toast.js';
import { esc } from '../utils.js';
import { api, download } from './client.js';
import { ChatView } from './chat-view.js';
import { fixText, renderSources, shortTitle } from './render.js';

const $ = (id) => document.getElementById(id);
let view;
let sessionsCache = [];

// ── Suggestions / scope ──────────────────────────────────────────────────────

function suggestions(v) {
  const p = v.scopeNames.persona;
  const a = v.scopeNames.account;
  if (v.scopeNames.deal) return [`What should my next step be on this deal?`, `Who else should be on the buying committee?`,
    `Draft a follow-up email to the champion`, `What risks do you see in this deal?`];
  if (p) return [`Prep me for a call with ${p}`, `What objections will ${p} raise?`,
    `What has ${p} been talking about recently?`, `What changed for ${p} in the last 30 days?`];
  if (a) return [`What's new at ${a}?`, `Who are the decision-makers at ${a}?`,
    `What is ${a} hiring for right now?`, `What are ${a}'s top priorities?`];
  return ['Prep me for a call with Robin Vince', "What's new at BNY?",
    'Which BNY VPs work in technology?', 'What is BlackRock hiring for right now?'];
}

function renderScope(v) {
  const chips = [];
  if (v.scopeNames.deal) chips.push(['deal', `<i class="fa-solid fa-diagram-next"></i> ${esc(v.scopeNames.deal)}`]);
  if (v.scopeNames.persona) chips.push(['persona', `<i class="fa-solid fa-user"></i> ${esc(v.scopeNames.persona)}`]);
  if (v.scopeNames.account) chips.push(['account', `<i class="fa-solid fa-building"></i> ${esc(v.scopeNames.account)}`]);
  $('cpScope').innerHTML = chips.length
    ? chips.map(([k, label]) => `<span class="cp-chip">${label}<button type="button" data-clear-scope="${k}" title="Remove scope" aria-label="Remove scope">✕</button></span>`).join('')
    : '<span class="cp-muted cp-small">All your accounts · type <kbd>@</kbd> to focus on a person or account</span>';
}

async function loadScopeFromUrl() {
  const params = new URLSearchParams(location.search);
  const pid = parseInt(params.get('persona_id'), 10);
  const aid = parseInt(params.get('account_id') || params.get('account'), 10);
  if (pid) {
    try {
      const res = await fetch(`/api/personas/${pid}`);
      if (res.ok) {
        const p = await res.json();
        view.setScope('persona', pid, p.name);
        if (p.account_id) view.context.account_id = p.account_id;
      }
    } catch { /* scope label is cosmetic */ }
  }
  const did = parseInt(params.get('deal_id'), 10);
  if (did) {
    try {
      const res = await fetch(`/api/deals/${did}`);
      if (res.ok) {
        const d = await res.json();
        view.setScope('deal', did, d.name);
        if (!view.context.account_id) view.context.account_id = d.account_id;
      }
    } catch { /* scope label is cosmetic */ }
  }
  const accountId = view.context.account_id || aid;
  if (accountId) {
    try {
      const ctx = await api('/context');
      const a = (ctx.accounts || []).find(x => x.id === accountId);
      if (a) view.setScope('account', a.id, a.name);
    } catch { /* ignore */ }
  }
  renderScope(view);
  return params.get('session');
}

// ── Side panels ──────────────────────────────────────────────────────────────

function allCitations(v) {
  const seen = new Map();
  v.messages.forEach((m, mi) => (m.citations || []).forEach(c => {
    const dedupe = c.url || c.title;
    if (!seen.has(dedupe)) seen.set(dedupe, { ...c, key: `${mi}-${c.n}` });
  }));
  return [...seen.values()];
}

function renderContext(v) {
  $('cpSources').innerHTML = renderSources(allCitations(v));
  const last = [...v.messages].reverse().find(m => m.role === 'assistant' && m.extras && m.extras.entities);
  const ents = last ? last.extras.entities : null;
  const items = [];
  if (ents && ents.persona) {
    const p = ents.persona;
    items.push(`<div class="cp-focus-item"><i class="fa-solid fa-user"></i>
      <a href="/profile?account=${p.account_id}&persona_id=${p.id}" target="_blank" rel="noopener">${esc(p.name)}</a>
      ${p.title ? `<span class="cp-focus-sub" title="${esc(fixText(p.title))}">${esc(shortTitle(p.title, 120))}</span>` : ''}</div>`);
  }
  ((ents && ents.accounts) || []).forEach(a => items.push(
    `<div class="cp-focus-item"><i class="fa-solid fa-building"></i> <a href="/?account=${a.id}">${esc(a.name)}</a></div>`));
  const notes = last && last.extras && last.extras.notes;
  if (notes && notes.length) {
    items.push(`<div class="cp-h3" style="margin-top:12px;">Your notes used</div>` +
      notes.map(n => `<div class="cp-note">${esc(n.text)}</div>`).join(''));
  }
  $('cpFocus').innerHTML = items.join('') || 'Nothing yet — mention a person or account.';
  $('cpChatActions').hidden = !v.sessionId;
}

// ── Sessions ─────────────────────────────────────────────────────────────────

function groupLabel(d) {
  const days = Math.floor((Date.now() - new Date(d).getTime()) / 86400000);
  if (days < 1) return 'Today';
  if (days < 2) return 'Yesterday';
  if (days < 7) return 'Last 7 days';
  if (days < 30) return 'Last 30 days';
  return 'Older';
}

function renderSessions() {
  const q = ($('cpSessionSearch').value || '').toLowerCase().trim();
  const rows = sessionsCache.filter(s => !q || (s.title || '').toLowerCase().includes(q));
  if (!rows.length) {
    $('cpSessionList').innerHTML = `<div class="cp-muted cp-small cp-pad">${q ? 'No chats match.' : 'No chats yet — ask your first question.'}</div>`;
    return;
  }
  let html = '';
  let group = null;
  rows.forEach(s => {
    const g = s.pinned ? 'Pinned' : groupLabel(s.last_message_at || s.created_at);
    if (g !== group) { html += `<div class="cp-group-label">${g}</div>`; group = g; }
    html += `<div class="cp-session${s.id === view.sessionId ? ' active' : ''}" data-session="${esc(s.id)}" role="button" tabindex="0" title="${esc(s.title || '')}">
      <span class="cp-session-title" data-title>${esc(s.title || 'Untitled chat')}</span>
      <span class="cp-session-actions">
        <button type="button" class="cp-icon-btn" data-rename="${esc(s.id)}" title="Rename" aria-label="Rename chat"><i class="fa-solid fa-pen"></i></button>
        <button type="button" class="cp-icon-btn" data-pin="${esc(s.id)}" data-pinned="${s.pinned}" title="${s.pinned ? 'Unpin' : 'Pin'}" aria-label="${s.pinned ? 'Unpin' : 'Pin'} chat"><i class="fa-solid fa-thumbtack"></i></button>
        <button type="button" class="cp-icon-btn" data-delete="${esc(s.id)}" title="Delete" aria-label="Delete chat"><i class="fa-regular fa-trash-can"></i></button>
      </span></div>`;
  });
  $('cpSessionList').innerHTML = html;
}

async function loadSessions() {
  try {
    sessionsCache = await api('/sessions');
    renderSessions();
    if (!view.messages.length) view.render();   // refresh "continue a recent chat"
  } catch (err) {
    $('cpSessionList').innerHTML = `<div class="cp-muted cp-small cp-pad">${esc(err.message)}</div>`;
  }
}

function startRename(row) {
  const titleEl = row.querySelector('[data-title]');
  const old = titleEl.textContent;
  titleEl.innerHTML = `<input class="cp-rename" value="${esc(old)}" maxlength="120" aria-label="Chat title">`;
  const input = titleEl.querySelector('input');
  input.focus();
  input.select();
  const finish = async (save) => {
    const v = input.value.trim();
    if (save && v && v !== old) {
      try { await api(`/sessions/${encodeURIComponent(row.dataset.session)}`, { method: 'PATCH', body: { title: v } }); }
      catch (err) { showToast(err.message); }
      if (row.dataset.session === view.sessionId) $('cpTitle').textContent = v;
    }
    loadSessions();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
    e.stopPropagation();
  });
  input.addEventListener('blur', () => finish(true));
  input.addEventListener('click', (e) => e.stopPropagation());
}

// ── Notes & prefs ────────────────────────────────────────────────────────────

async function loadNotes() {
  try {
    const notes = await api('/notes');
    $('cpNoteList').innerHTML = notes.length ? notes.map(n => `<div class="cp-note">
        ${esc(n.text)}
        <div class="cp-note-meta">${n.kind === 'reminder' ? '⏰ ' : ''}${esc(n.kind)}${n.persona ? ` · ${esc(n.persona)}` : ''}${n.account && !n.persona ? ` · ${esc(n.account)}` : ''} · used ${n.use_count}×</div>
        <button type="button" class="cp-icon-btn" data-delete-note="${n.id}" title="Delete note" aria-label="Delete note"><i class="fa-regular fa-trash-can"></i></button>
      </div>`).join('') : '<div class="cp-muted cp-small cp-pad">No notes yet. Say “remember that …” in a chat, or pin an answer.</div>';
  } catch (err) {
    $('cpNoteList').innerHTML = `<div class="cp-muted cp-small cp-pad">${esc(err.message)}</div>`;
  }
}

async function loadPrefs() {
  try {
    const p = await api('/prefs');
    $('cpMemoryToggle').checked = p.memory_enabled;
    $('cpAnswerStyle').value = p.answer_style;
  } catch { /* defaults stay */ }
}

async function savePrefs() {
  try {
    await api('/prefs', { method: 'PUT', body: { memory_enabled: $('cpMemoryToggle').checked, answer_style: $('cpAnswerStyle').value } });
    showToast('Preferences saved');
  } catch (err) { showToast(err.message); }
}

// ── Quota ────────────────────────────────────────────────────────────────────

function renderQuota(q) {
  if (!q) return;
  const t = q.team, me = q.me;
  const reset = new Date(q.resets_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const teamLeft = t.provider_exhausted ? 0 : Math.max(0, t.requests_limit - t.requests_used);
  const myLeft = Math.max(0, me.tokens_limit - me.tokens_used);
  const pct = Math.round((teamLeft / Math.max(1, t.requests_limit)) * 100);
  const low = teamLeft === 0 || myLeft < 5000;
  const el = $('cpQuota');
  el.classList.toggle('cp-warn', low);
  el.innerHTML = `<div class="cp-quota-bar" title="Team AI requests left today"><span style="width:${pct}%"></span></div>
    <div>Team <strong>${teamLeft}/${t.requests_limit}</strong> · You <strong>${(myLeft / 1000).toFixed(1)}k</strong>/${(me.tokens_limit / 1000).toFixed(0)}k tokens · resets ${esc(reset)}</div>
    ${low ? '<div class="cp-small">AI summaries paused — answers show data &amp; sources</div>' : ''}`;
}

async function loadQuota() {
  try { renderQuota(await api('/quota')); } catch { /* optional */ }
}

// ── Wiring ───────────────────────────────────────────────────────────────────

function wire() {
  $('cpNewChat').addEventListener('click', () => {
    view.newChat();
    $('cpTitle').textContent = 'Sales Copilot';
    const url = new URL(location.href);
    url.searchParams.delete('session');
    history.replaceState(null, '', url);
    renderSessions();
    $('cpInput').focus();
  });
  $('cpSessionSearch').addEventListener('input', renderSessions);

  $('cpSessionList').addEventListener('click', async (e) => {
    const row = e.target.closest('[data-session]');
    const del = e.target.closest('[data-delete]');
    const pin = e.target.closest('[data-pin]');
    const ren = e.target.closest('[data-rename]');
    try {
      if (del) {
        e.stopPropagation();
        await api(`/sessions/${encodeURIComponent(del.dataset.delete)}`, { method: 'DELETE' });
        if (del.dataset.delete === view.sessionId) $('cpNewChat').click();
        loadSessions();
        showToast('Chat deleted');
      } else if (pin) {
        e.stopPropagation();
        await api(`/sessions/${encodeURIComponent(pin.dataset.pin)}`, { method: 'PATCH', body: { pinned: pin.dataset.pinned !== 'true' } });
        loadSessions();
      } else if (ren) {
        e.stopPropagation();
        startRename(row);
      } else if (row) {
        await view.openSession(row.dataset.session);
        closeSidebarOnMobile();
      }
    } catch (err) { showToast(err.message); }
  });
  $('cpSessionList').addEventListener('keydown', (e) => {
    const row = e.target.closest('[data-session]');
    if (row && (e.key === 'Enter' || e.key === ' ') && e.target === row) { e.preventDefault(); row.click(); }
    if (row && e.key === 'F2') { e.preventDefault(); startRename(row); }
  });

  $('cpNoteList').addEventListener('click', async (e) => {
    const del = e.target.closest('[data-delete-note]');
    if (!del) return;
    try { await api(`/notes/${del.dataset.deleteNote}`, { method: 'DELETE' }); loadNotes(); showToast('Note deleted'); }
    catch (err) { showToast(err.message); }
  });
  $('cpMemoryToggle').addEventListener('change', savePrefs);
  $('cpAnswerStyle').addEventListener('change', savePrefs);
  $('cpNotesExport').addEventListener('click', () => download('/notes/export?format=xlsx', 'my-copilot-notes.xlsx').catch(err => showToast(err.message)));
  $('cpNotesClear').addEventListener('click', () => { $('cpClearConfirm').hidden = false; $('cpClearInput').focus(); });
  $('cpClearCancel').addEventListener('click', () => { $('cpClearConfirm').hidden = true; $('cpClearInput').value = ''; });
  $('cpClearGo').addEventListener('click', async () => {
    if ($('cpClearInput').value.trim().toUpperCase() !== 'CLEAR') { showToast('Type CLEAR to confirm'); return; }
    try {
      const r = await api('/notes/clear', { method: 'POST', body: { confirm: 'CLEAR' } });
      $('cpClearConfirm').hidden = true;
      $('cpClearInput').value = '';
      loadNotes();
      showToast(`Cleared ${r.cleared} notes`);
    } catch (err) { showToast(err.message); }
  });

  document.querySelectorAll('.cp-tab').forEach(tab => tab.addEventListener('click', () => {
    document.querySelectorAll('.cp-tab').forEach(t => { t.classList.toggle('active', t === tab); t.setAttribute('aria-selected', String(t === tab)); });
    $('cpPaneChats').classList.toggle('active', tab.dataset.tab === 'chats');
    $('cpPaneNotes').classList.toggle('active', tab.dataset.tab === 'notes');
    if (tab.dataset.tab === 'notes') loadNotes();
  }));

  $('cpScope').addEventListener('click', (e) => {
    const b = e.target.closest('[data-clear-scope]');
    if (!b) return;
    const kind = b.dataset.clearScope;
    view.clearScope(kind);
    const url = new URL(location.href);
    if (kind === 'persona') url.searchParams.delete('persona_id');
    else if (kind === 'deal') url.searchParams.delete('deal_id');
    else { url.searchParams.delete('account_id'); url.searchParams.delete('account'); }
    history.replaceState(null, '', url);
  });

  // Chat-level downloads
  $('cpChatActions').addEventListener('click', (e) => {
    const b = e.target.closest('[data-chat-dl]');
    if (!b || !view.sessionId) return;
    const fmt = b.dataset.chatDl;
    download(`/sessions/${encodeURIComponent(view.sessionId)}/export?format=${fmt}`, `copilot-chat.${fmt}`)
      .catch(err => showToast(err.message));
  });

  $('cpHelpBtn').addEventListener('click', () => { $('cpHelp').hidden = !$('cpHelp').hidden; });
  $('cpMenuBtn').addEventListener('click', () => $('cpSidebar').classList.toggle('open'));

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); $('cpInput').focus(); }
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'o') { e.preventDefault(); $('cpNewChat').click(); }
    if (e.key === 'Escape' && view.busy) view.stop();
  });
}

function closeSidebarOnMobile() { $('cpSidebar').classList.remove('open'); }

async function init() {
  view = new ChatView(
    { messages: $('cpMessages'), form: $('cpComposer'), input: $('cpInput'), send: $('cpSend'),
      mention: $('cpMention'), jump: $('cpJump') },
    {
      getSuggestions: suggestions,
      getRecent: () => sessionsCache.slice(0, 3),
      onScope: renderScope,
      onMessages: renderContext,
      onQuota: renderQuota,
      onNoteSaved: loadNotes,
      onSession: (v, isNew, data) => {
        const url = new URL(location.href);
        if (v.sessionId) url.searchParams.set('session', v.sessionId); else url.searchParams.delete('session');
        history.replaceState(null, '', url);
        if (data && data.title) $('cpTitle').textContent = data.title;
        if (isNew || !data) loadSessions(); else renderSessions();
      },
    });
  wire();
  const sessionParam = await loadScopeFromUrl();
  view.render();
  loadSessions();
  loadQuota();
  loadPrefs();
  if (sessionParam) {
    try { await view.openSession(sessionParam); } catch (err) { showToast(err.message); }
  }
  const prefill = new URLSearchParams(location.search).get('q');
  if (prefill && !$('cpInput').value) {
    $('cpInput').value = prefill.slice(0, 2000);
    $('cpInput').dispatchEvent(new Event('input', { bubbles: true }));
    const url = new URL(location.href); url.searchParams.delete('q'); history.replaceState(null, '', url);
  }
  $('cpInput').focus();
}

initThemeToggle();
initTopbarAuth().then((user) => {
  if (!user) {
    document.body.style.display = 'none';
    window.location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    return;
  }
  init();
});
