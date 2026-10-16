// Reusable chat surface for the Sales Copilot — used by the /copilot workspace
// (main.js) and the dock on every other page (dock.js). Owns: message list,
// streaming, composer (@mentions, /commands, ↑ to edit), answer actions.
import { api, download, streamChat } from './client.js';
import { draftPlain, parseDraft, renderEmpty, renderMentionList, renderMessage } from './render.js';
import { showToast } from '../toast.js';

const SAVE_HINT = /\b(meeting|call|demo|lunch|visit)\b.*\b(on|at|next|tomorrow|monday|tuesday|wednesday|thursday|friday|\d{1,2})\b|\b(we (already|pitched|proposed|sent|met)|they (said|told|mentioned|want|prefer)|prefers?|budget is|decision (by|in))\b/i;
const MENTION_RE = /(^|\s)@([\w.'-]{1,40})$/;

function stripCites(s) { return (s || '').replace(/\[(\d+|note)\]/gi, '').replace(/[ \t]+\n/g, '\n').trim(); }
function plain(s) { return stripCites(s).replace(/\*\*(.+?)\*\*/g, '$1').replace(/(^|\s)[*_](.+?)[*_]/g, '$1$2').replace(/^#+\s*/gm, ''); }

export class ChatView {
  constructor(els, opts = {}) {
    this.els = els;            // {messages, form, input, send, mention, jump}
    this.opts = opts;          // {compact, getSuggestions, getRecent, onSession, onMessages, onQuota, onNoteSaved, onScope}
    this.sessionId = null;
    this.messages = [];
    this.context = {};         // {persona_id, account_id, deal_id}
    this.scopeNames = {};      // {persona, account, deal}
    this.controller = null;
    this.mention = { items: [], index: 0, open: false, timer: null };
    this._raf = null;
    this.wire();
  }

  get busy() { return !!this.controller; }

  // ── Scope ────────────────────────────────────────────────
  setScope(kind, id, name) {
    if (kind === 'persona') { this.context.persona_id = id; this.scopeNames.persona = name; }
    if (kind === 'account') { this.context.account_id = id; this.scopeNames.account = name; }
    if (kind === 'deal') { this.context.deal_id = id; this.scopeNames.deal = name; }
    this.opts.onScope && this.opts.onScope(this);
    if (!this.messages.length) this.render();
  }

  clearScope(kind) {
    if (kind === 'persona') { delete this.context.persona_id; delete this.scopeNames.persona; }
    if (kind === 'account') { delete this.context.account_id; delete this.scopeNames.account; }
    if (kind === 'deal') { delete this.context.deal_id; delete this.scopeNames.deal; }
    this.opts.onScope && this.opts.onScope(this);
    if (!this.messages.length) this.render();
  }

  // ── Rendering ────────────────────────────────────────────
  render() {
    const box = this.els.messages;
    if (!this.messages.length) {
      const sugg = this.opts.getSuggestions ? this.opts.getSuggestions(this) : [];
      const recent = this.opts.getRecent ? this.opts.getRecent() : [];
      box.innerHTML = renderEmpty(this.scopeNames.persona || this.scopeNames.account, sugg, recent);
    } else {
      const lastBot = this.messages.map(m => m.role).lastIndexOf('assistant');
      box.innerHTML = this.messages.map((m, i) => renderMessage(m, { compact: this.opts.compact, isLast: i === lastBot && i === this.messages.length - 1 })).join('');
      this.scrollToBottom();
    }
    this.opts.onMessages && this.opts.onMessages(this);
  }

  renderLast() {
    // Cheap re-render of just the streaming bubble (at most once per frame).
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = null;
      const box = this.els.messages;
      const last = box.lastElementChild;
      const msg = this.messages[this.messages.length - 1];
      if (!last || !msg) return;
      const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;
      last.outerHTML = renderMessage(msg, { compact: this.opts.compact, isLast: true });
      if (nearBottom) box.scrollTop = box.scrollHeight;
    });
  }

  scrollToBottom() {
    const box = this.els.messages;
    box.scrollTop = box.scrollHeight;
    if (this.els.jump) this.els.jump.hidden = true;
  }

  // ── Sending ──────────────────────────────────────────────
  expandCommand(text) {
    const t = text.trim();
    const who = this.scopeNames.persona || this.scopeNames.account || '';
    let m;
    if ((m = t.match(/^\/prep\s*(.*)$/i))) return `Prep me for a call with ${m[1] || who || 'this contact'}`;
    if ((m = t.match(/^\/changes?\s*(.*)$/i))) return `What changed for ${m[1] || who || 'my accounts'} in the last 30 days?`;
    if ((m = t.match(/^\/email\s*(.*)$/i))) return `Draft a short, personalized intro email to ${m[1] || who || 'this contact'} using their priorities`;
    if ((m = t.match(/^\/objections?\s*(.*)$/i))) return `What objections will ${m[1] || who || 'they'} raise, and how should I answer?`;
    if ((m = t.match(/^\/news\s*(.*)$/i))) return `What's the latest news and activity for ${m[1] || who || 'my accounts'}?`;
    return t;
  }

  async send(rawText) {
    const text = this.expandCommand(rawText || '');
    if (!text || this.busy) return;
    const suggestSave = SAVE_HINT.test(text) && !/^(\/remember|remember)\b/i.test(text);
    this.messages.push({ role: 'user', content: text, suggestSave, created_at: new Date().toISOString() });
    const bot = { role: 'assistant', content: '', streaming: true, status: 'Understanding your question…', created_at: new Date().toISOString() };
    this.messages.push(bot);
    this.render();
    this.setBusy(true);
    this.controller = new AbortController();
    try {
      await streamChat({ text, session_id: this.sessionId, context: this.context }, {
        status: (d) => { bot.status = d.label; this.renderLast(); },
        meta: (d) => {
          bot.mode = d.mode; bot.intent = d.intent; bot.citations = d.citations || []; bot.extras = d.extras || {};
          if (d.session_id && !this.sessionId) { this.sessionId = d.session_id; this.opts.onSession && this.opts.onSession(this, true); }
          this.renderLast();
        },
        token: (d) => { bot.content += d.t; bot.status = null; this.renderLast(); },
        done: (d) => {
          Object.assign(bot, d.message, { streaming: false, status: null });
          const isNew = this.sessionId !== d.session_id;
          this.sessionId = d.session_id;
          this.opts.onSession && this.opts.onSession(this, isNew);
          d.quota && this.opts.onQuota && this.opts.onQuota(d.quota);
          if (bot.extras && bot.extras.saved_note && this.opts.onNoteSaved) this.opts.onNoteSaved();
        },
        error: (d) => { throw new Error(d.detail || 'Something went wrong'); },
      }, this.controller.signal);
      if (bot.streaming) {   // stream ended without "done"
        bot.streaming = false;
        if (!bot.content) bot.content = 'The answer was interrupted. Please try again.';
      }
    } catch (err) {
      bot.streaming = false;
      bot.status = null;
      if (err.name === 'AbortError') {
        bot.stopped = true;
        if (!bot.content) bot.content = '_Stopped._';
      } else if (err.message !== 'unauthenticated') {
        bot.mode = 'database';
        bot.content = `Sorry — that didn't work: ${err.message}. Your question is back in the box; try again.`;
        this.els.input.value = text;
      }
    } finally {
      this.controller = null;
      this.setBusy(false);
      this.render();
      if (!this.opts.compact) this.els.input.focus();
    }
  }

  stop() { if (this.controller) this.controller.abort(); }

  setBusy(busy) {
    const b = this.els.send;
    b.classList.toggle('is-stop', busy);
    b.title = busy ? 'Stop' : 'Send (Enter)';
    b.setAttribute('aria-label', busy ? 'Stop generating' : 'Send');
    b.innerHTML = busy ? '<i class="fa-solid fa-stop"></i>' : '<i class="fa-solid fa-paper-plane"></i>';
  }

  async openSession(id) {
    const data = await api(`/sessions/${encodeURIComponent(id)}`);
    this.sessionId = data.id;
    this.messages = data.messages;
    this.render();
    this.opts.onSession && this.opts.onSession(this, false, data);
    return data;
  }

  newChat() {
    this.stop();
    this.sessionId = null;
    this.messages = [];
    this.render();
    this.opts.onSession && this.opts.onSession(this, false);
  }

  lastUserText() {
    const u = [...this.messages].reverse().find(m => m.role === 'user');
    return u ? u.content : '';
  }

  // ── Composer: @mentions ──────────────────────────────────
  async updateMentions() {
    const input = this.els.input;
    const before = input.value.slice(0, input.selectionStart);
    const m = before.match(MENTION_RE);
    if (!m) return this.closeMentions();
    clearTimeout(this.mention.timer);
    this.mention.timer = setTimeout(async () => {
      try {
        const items = await api(`/entities?q=${encodeURIComponent(m[2])}`);
        this.mention.items = items;
        this.mention.index = 0;
        this.mention.open = true;
        this.els.mention.innerHTML = renderMentionList(items, 0);
        this.els.mention.hidden = false;
      } catch { this.closeMentions(); }
    }, 150);
  }

  closeMentions() {
    this.mention.open = false;
    if (this.els.mention) this.els.mention.hidden = true;
  }

  pickMention(i) {
    const it = this.mention.items[i];
    if (!it) return;
    const input = this.els.input;
    const pos = input.selectionStart;
    const before = input.value.slice(0, pos).replace(MENTION_RE, (all, sp) => `${sp}${it.name} `);
    input.value = before + input.value.slice(pos);
    input.setSelectionRange(before.length, before.length);
    if (it.type === 'persona') {
      this.setScope('persona', it.id, it.name);
      if (it.account_id) this.setScope('account', it.account_id, it.account);
    } else {
      this.clearScope('persona');
      this.setScope('account', it.id, it.name);
    }
    this.closeMentions();
    input.focus();
  }

  // ── Events ───────────────────────────────────────────────
  wire() {
    const { form, input, messages, mention, jump } = this.els;
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      if (this.busy) { this.stop(); return; }
      const v = input.value;
      input.value = '';
      this.autoGrow();
      this.send(v);
    });
    input.addEventListener('keydown', (e) => {
      if (this.mention.open && this.mention.items.length) {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          const n = this.mention.items.length;
          this.mention.index = (this.mention.index + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
          mention.innerHTML = renderMentionList(this.mention.items, this.mention.index);
          return;
        }
        if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); this.pickMention(this.mention.index); return; }
        if (e.key === 'Escape') { e.preventDefault(); this.closeMentions(); return; }
      }
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); return; }
      if (e.key === 'ArrowUp' && !input.value) {
        const last = this.lastUserText();
        if (last) { e.preventDefault(); input.value = last; this.autoGrow(); }
      }
    });
    input.addEventListener('input', () => { this.autoGrow(); this.updateMentions(); });
    input.addEventListener('blur', () => setTimeout(() => this.closeMentions(), 150));
    if (mention) {
      mention.addEventListener('mousedown', (e) => {
        const b = e.target.closest('[data-mention-index]');
        if (b) { e.preventDefault(); this.pickMention(Number(b.dataset.mentionIndex)); }
      });
    }
    if (jump) jump.addEventListener('click', () => this.scrollToBottom());
    messages.addEventListener('scroll', () => {
      if (!jump) return;
      jump.hidden = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 200;
    });
    messages.addEventListener('click', (e) => this.onMessageClick(e));
    messages.addEventListener('keydown', (e) => {
      const th = e.target.closest('th[data-sort]');
      if (th && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); this.sortTable(th); }
    });
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.cp-menu-wrap')) messages.querySelectorAll('.cp-menu-wrap.open').forEach(w => w.classList.remove('open'));
    });
  }

  autoGrow() {
    const t = this.els.input;
    t.style.height = 'auto';
    t.style.height = `${Math.min(t.scrollHeight, 160)}px`;
  }

  sortTable(th) {
    const table = th.closest('table');
    const idx = [...th.parentNode.children].indexOf(th);
    const dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
    th.parentNode.querySelectorAll('th').forEach(h => delete h.dataset.dir);
    th.dataset.dir = dir;
    const rows = [...table.tBodies[0].rows];
    rows.sort((a, b) => {
      const x = a.cells[idx].textContent.trim().toLowerCase();
      const y = b.cells[idx].textContent.trim().toLowerCase();
      return (x < y ? -1 : x > y ? 1 : 0) * (dir === 'asc' ? 1 : -1);
    });
    rows.forEach(r => table.tBodies[0].appendChild(r));
  }

  async onMessageClick(e) {
    const sug = e.target.closest('.cp-suggestion');
    if (sug) { this.send(sug.dataset.followup || sug.textContent); return; }
    const recent = e.target.closest('[data-open-session]');
    if (recent) { this.openSession(recent.dataset.openSession).catch(err => showToast(err.message)); return; }
    const th = e.target.closest('th[data-sort]');
    if (th) { this.sortTable(th); return; }
    const saveBtn = e.target.closest('[data-save-suggest]');
    if (saveBtn) { await this.onSaveSuggest(saveBtn); return; }

    const msgEl = e.target.closest('[data-message-id]');
    const msg = msgEl ? this.messages.find(m => String(m.id) === msgEl.dataset.messageId) : null;
    const cite = e.target.closest('[data-cite]');
    if (cite && msg) {
      const c = (msg.citations || [])[Number(cite.dataset.cite) - 1];
      if (c && c.url) window.open(c.url, '_blank', 'noopener');
      this.opts.onCite && this.opts.onCite(msg, c);
      return;
    }
    const act = e.target.closest('[data-action]');
    if (!act || !msg) return;
    const action = act.dataset.action;
    try {
      if (action === 'menu-download') {
        act.closest('.cp-menu-wrap').classList.toggle('open');
      } else if (action === 'copy') {
        await navigator.clipboard.writeText(plain(msg.content));
        showToast('Answer copied');
      } else if (action === 'copy-email') {
        const d = parseDraft(msg.content);
        await navigator.clipboard.writeText(d ? `Subject: ${d.subject}\n\n${draftPlain(d.body)}` : plain(msg.content));
        showToast('Email copied — paste it into your mail app');
      } else if (action === 'email') {
        const who = (msg.extras && msg.extras.entities && msg.extras.entities.persona) ? msg.extras.entities.persona.name.split(' ')[0] : 'there';
        await navigator.clipboard.writeText(`Hi ${who},\n\n${plain(msg.content)}\n\nBest regards,\n`);
        showToast('Email draft copied — paste it into your mail app');
      } else if (action === 'fb-up' || action === 'fb-down') {
        const value = action === 'fb-up' ? 1 : -1;
        await api(`/messages/${msg.id}/feedback`, { method: 'POST', body: { value } });
        msg.feedback = value;
        this.render();
        showToast(value === 1 ? 'Thanks — glad it helped' : 'Thanks — this helps us improve answers');
      } else if (action === 'remember') {
        const ents = (msg.extras && msg.extras.entities) || {};
        const firstPara = plain(msg.content).split(/\n\s*\n/)[0].slice(0, 1000);
        await api('/notes', { method: 'POST', body: {
          text: firstPara,
          persona_id: ents.persona ? ents.persona.id : null,
          account_id: (ents.accounts && ents.accounts[0]) ? ents.accounts[0].id : null } });
        showToast('Saved to your notes');
        this.opts.onNoteSaved && this.opts.onNoteSaved();
      } else if (action === 'regenerate') {
        const q = this.lastUserText();
        if (q) this.send(q);
      } else if (action === 'dl-pdf') {
        await download(`/messages/${msg.id}/export?format=pdf`, 'copilot-answer.pdf');
      } else if (action === 'dl-xlsx') {
        await download(`/messages/${msg.id}/export?format=xlsx`, 'copilot-table.xlsx');
      } else if (action === 'dl-xlsx-sources') {
        await download(`/messages/${msg.id}/export?format=xlsx&part=sources`, 'copilot-sources.xlsx');
      }
    } catch (err) { showToast(err.message); }
  }

  async onSaveSuggest(btn) {
    const msgEl = btn.closest('[data-user-message]');
    const idx = [...this.els.messages.children].indexOf(msgEl);
    const msg = this.messages[idx];
    if (!msg) return;
    msg.suggestSave = false;
    if (btn.dataset.saveSuggest === 'yes') {
      try {
        await api('/notes', { method: 'POST', body: {
          text: msg.content.slice(0, 1000), persona_id: this.context.persona_id || null,
          account_id: this.context.account_id || null } });
        showToast('Saved to your notes');
        this.opts.onNoteSaved && this.opts.onNoteSaved();
      } catch (err) { showToast(err.message); }
    }
    this.render();
  }
}
