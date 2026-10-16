// Sales Copilot dock (partials/copilot-dock.html): floating button + side panel
// on every main page. Picks up page context when opened — the profile being
// viewed, the selected account on the dashboard, or the open contact drawer —
// and keeps its chat per browser tab so closing/reopening continues it.
import '../fetch-instrumentation.js';
import { state } from '../state.js';
import { esc } from '../utils.js';
import { api } from './client.js';
import { ChatView } from './chat-view.js';

const $ = (id) => document.getElementById(id);
const SESSION_KEY = 'copilotDockSession';
let view = null;

function suggestions(v) {
  const p = v.scopeNames.persona;
  const a = v.scopeNames.account;
  if (p) return [`Prep me for a call with ${p}`, `What objections will ${p} raise?`, `Draft a short intro email to ${p}`];
  if (a) return [`What's new at ${a}?`, `Who are the decision-makers at ${a}?`, `What is ${a} hiring for?`];
  return ["What's new at BNY?", 'Prep me for a call with Robin Vince', 'Which BNY VPs work in technology?'];
}

function renderScope(v) {
  const chips = [];
  if (v.scopeNames.persona) chips.push(['persona', `<i class="fa-solid fa-user"></i> ${esc(v.scopeNames.persona)}`]);
  if (v.scopeNames.account) chips.push(['account', `<i class="fa-solid fa-building"></i> ${esc(v.scopeNames.account)}`]);
  $('cpdScope').innerHTML = chips.map(([k, label]) =>
    `<span class="cp-chip">${label}<button type="button" data-clear-scope="${k}" aria-label="Remove scope">✕</button></span>`).join('');
}

function renderQuota(q) {
  if (!q) return;
  const t = q.team, me = q.me;
  const teamLeft = t.provider_exhausted ? 0 : Math.max(0, t.requests_limit - t.requests_used);
  const myLeft = Math.max(0, me.tokens_limit - me.tokens_used);
  const el = $('cpdQuota');
  el.classList.toggle('cp-warn', teamLeft === 0 || myLeft < 5000);
  el.textContent = (teamLeft === 0 || myLeft < 5000)
    ? `AI summaries paused until ${new Date(q.resets_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} — answers show data & sources`
    : `${teamLeft} team AI requests left today · you ${(myLeft / 1000).toFixed(0)}k tokens`;
}

async function pageContext() {
  // Any page can publish what's open right now (e.g. the Introductions drawer).
  const published = window.doshinPageContext;
  if (published && (published.persona || published.account_id)) return published;
  // Profile page: /profile?account=&persona_id=
  const params = new URLSearchParams(location.search);
  const pid = parseInt(params.get('persona_id'), 10);
  if (location.pathname === '/profile' && pid) {
    try {
      const res = await fetch(`/api/personas/${pid}`);
      if (res.ok) { const p = await res.json(); return { persona: { id: pid, name: p.name }, account_id: p.account_id }; }
    } catch { /* ignore */ }
  }
  // Dashboard: the open contact drawer wins, then the selected account
  const drawer = state.activeDrawerPersona;
  const accountId = state.activeAccountId || (drawer && drawer.account_id);
  const account = accountId ? (state.accounts || []).find(a => a.id === accountId) : null;
  return {
    persona: drawer && drawer.id ? { id: drawer.id, name: drawer.name || drawer.full_name } : null,
    account_id: accountId || null,
    account_name: account ? account.name : null,
  };
}

async function applyContext() {
  const ctx = await pageContext();
  if (ctx.persona) view.setScope('persona', ctx.persona.id, ctx.persona.name);
  if (ctx.account_id) {
    let name = ctx.account_name;
    if (!name) {
      try { name = ((await api('/context')).accounts || []).find(a => a.id === ctx.account_id)?.name; } catch { /* ignore */ }
    }
    if (name) view.setScope('account', ctx.account_id, name);
  }
  renderScope(view);
}

function setExpandLink() {
  const params = new URLSearchParams();
  if (view.sessionId) params.set('session', view.sessionId);
  if (view.context.persona_id) params.set('persona_id', view.context.persona_id);
  if (view.context.account_id) params.set('account_id', view.context.account_id);
  $('cpdExpand').href = `/copilot${params.toString() ? `?${params}` : ''}`;
}

async function open() {
  const panel = $('cpdPanel');
  if (!panel.hidden) { $('cpdInput').focus(); return; }
  panel.hidden = false;
  document.body.classList.add('cpd-open');
  $('cpdFab').setAttribute('aria-expanded', 'true');
  if (!view) {
    view = new ChatView(
      { messages: $('cpdMessages'), form: $('cpdComposer'), input: $('cpdInput'), send: $('cpdSend'),
        mention: $('cpdMention'), jump: $('cpdJump') },
      {
        compact: true,
        getSuggestions: suggestions,
        onScope: renderScope,
        onQuota: renderQuota,
        onSession: (v) => {
          try { v.sessionId ? sessionStorage.setItem(SESSION_KEY, v.sessionId) : sessionStorage.removeItem(SESSION_KEY); } catch { /* private mode */ }
          setExpandLink();
        },
      });
    let saved = null;
    try { saved = sessionStorage.getItem(SESSION_KEY); } catch { /* private mode */ }
    if (saved) {
      try { await view.openSession(saved); } catch { view.render(); }
    } else {
      view.render();
    }
    api('/quota').then(renderQuota).catch(() => {});
  }
  await applyContext();
  setExpandLink();
  $('cpdInput').focus();
}

function close() {
  $('cpdPanel').hidden = true;
  document.body.classList.remove('cpd-open');
  $('cpdFab').setAttribute('aria-expanded', 'false');
  $('cpdFab').focus();
}

function init() {
  if (!$('cpdFab')) return;
  $('cpdFab').addEventListener('click', () => ($('cpdPanel').hidden ? open() : close()));
  $('cpdClose').addEventListener('click', close);
  $('cpdNew').addEventListener('click', async () => { view.newChat(); await applyContext(); $('cpdInput').focus(); });
  $('cpdScope').addEventListener('click', (e) => {
    const b = e.target.closest('[data-clear-scope]');
    if (b && view) { view.clearScope(b.dataset.clearScope); renderScope(view); setExpandLink(); }
  });
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if ($('cpdPanel').hidden) open(); else $('cpdInput').focus();
    } else if (e.key === 'Escape' && !$('cpdPanel').hidden && $('cpdPanel').contains(document.activeElement)) {
      if (view && view.busy) view.stop(); else close();
    }
  });
}

init();
