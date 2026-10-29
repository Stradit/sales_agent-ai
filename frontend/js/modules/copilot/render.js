// Pure rendering for the Sales Copilot (workspace page + dock). See
// apps/sales_copilot/README.md §19. Model output is never inserted as HTML:
// text is escaped first, then a small allow-list of markdown (bold, italic,
// code, bullets, headings) and citation chips is re-applied.
import { esc } from '../utils.js';
import { photoAttr } from '../persona-photo.js';

const MODE_LABEL = {
  llm: ['AI answer', 'fa-wand-magic-sparkles', 'Written by the AI from the sources below'],
  database: ['From database', 'fa-database', 'Answered directly from your data — no AI involved'],
  retrieval_only: ['Sources only', 'fa-list', 'AI summary unavailable right now — showing the most relevant sources'],
  cached: ['Cached answer', 'fa-clock-rotate-left', 'Same question and same data as a recent answer — reused, no AI request spent'],
};

const TYPE_LABEL = {
  persona_card: 'Profile', callprep: 'Call-prep', personality_profile: 'Personality', digest_channel: 'Digest',
  account_card: 'Account', lob_card: 'Line of business', signal: 'Signal', news: 'News', blog: 'Blog',
  linkedin_post: 'LinkedIn', reddit: 'Reddit', social: 'X / Twitter', filing: 'SEC filing', cxo_move: 'Leadership move',
  patent: 'Patent', job: 'Job posting', job_theme: 'Hiring summary', activity: 'Logged activity',
};

const TYPE_ICON = {
  persona_card: 'fa-user', callprep: 'fa-comment-dots', personality_profile: 'fa-brain', digest_channel: 'fa-newspaper',
  account_card: 'fa-building', lob_card: 'fa-sitemap', signal: 'fa-bolt', news: 'fa-newspaper', blog: 'fa-pen-nib',
  linkedin_post: 'fa-linkedin', reddit: 'fa-reddit', social: 'fa-x-twitter', filing: 'fa-file-contract',
  cxo_move: 'fa-user-tie', patent: 'fa-lightbulb', job: 'fa-briefcase', job_theme: 'fa-chart-column', activity: 'fa-clock-rotate-left',
};

// Scraped text is sometimes double-encoded (UTF-8 read as Windows-1252): "Wealthâ€™s".
const MOJIBAKE = [['â€™', '’'], ['â€˜', '‘'], ['â€œ', '“'], ['â€\u009d', '”'], ['â€"', '—'], ['â€“', '–'],
  ['â€”', '—'], ['â€¦', '…'], ['â€¢', '•'], ['Â ', ' '], ['Â·', '·'], ['Ã©', 'é'], ['Ã¨', 'è'], ['Ã¼', 'ü'], ['Ã¶', 'ö']];
export function fixText(s) {
  let out = String(s == null ? '' : s);
  if (!/[âÃÂ]/.test(out)) return out;
  MOJIBAKE.forEach(([bad, good]) => { out = out.split(bad).join(good); });
  // Latin-1 variant ("â\u0080\u0099"): re-decode each UTF-8 byte run
  out = out.replace(/[Â-ô][\u0080-¿]{1,3}/g, (run) => {
    try { return decodeURIComponent(run.split('').map(ch => `%${ch.charCodeAt(0).toString(16).padStart(2, '0')}`).join('')); }
    catch { return run; }
  });
  return out.replace(/â€[\u0080-¿]?/g, '’');
}

// "Name — Title | Tagline | …" → "Name — Title" (headline taglines make titles unreadably long)
export function shortTitle(t, max = 70) {
  const s = fixText(t || 'Untitled').split(/\s[|•]\s/)[0].trim();
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

export function typeLabel(t) { return TYPE_LABEL[t] || (t || '').replace(/_/g, ' '); }
export function typeIcon(t) {
  const i = TYPE_ICON[t] || 'fa-file-lines';
  return (i === 'fa-linkedin' || i === 'fa-reddit' || i === 'fa-x-twitter') ? `fa-brands ${i}` : `fa-solid ${i}`;
}

function timeLabel(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function inline(s, nCites) {
  let h = esc(s);
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, '$1<em>$2</em>');
  h = h.replace(/(^|\W)_(?!\s)(.+?)_(?=\W|$)/g, '$1<em>$2</em>');
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
  h = h.replace(/\[(\d{1,2})\]/g, (m, n) => (Number(n) >= 1 && Number(n) <= nCites)
    ? `<button type="button" class="cp-cite" data-cite="${n}" aria-label="Source ${n}">${n}</button>` : '');
  h = h.replace(/\[note\]/gi, '<span class="cp-cite cp-cite-note" title="From your private notes"><i class="fa-solid fa-thumbtack"></i></span>');
  return h;
}

// Splits an answer into lines, fenced code blocks (```csv … ```) and pipe tables (| a | b |).
// Shared by renderMarkdown and downloadBlocks so a block's index means the same thing in both.
const FENCE_RE = /^\s*```\s*([\w-]*)\s*$/;
const PIPE_RE = /^\s*\|.*\|\s*$/;
const PIPE_RULE_RE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
function segments(text) {
  const lines = fixText(text || '').replace(/\r/g, '').split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    const fence = lines[i].match(FENCE_RE);
    if (fence) {
      const body = [];
      for (i++; i < lines.length && !FENCE_RE.test(lines[i]); i++) body.push(lines[i]);   // unclosed while streaming: to the end
      out.push({ type: 'code', lang: fence[1].toLowerCase(), lines: body });
    } else if (PIPE_RE.test(lines[i])) {
      const rows = [];
      for (; i < lines.length && PIPE_RE.test(lines[i]); i++) {
        if (!PIPE_RULE_RE.test(lines[i])) rows.push(lines[i].trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim()));
      }
      i--;
      out.push({ type: 'table', rows });
    } else out.push({ type: 'line', text: lines[i] });
  }
  return out;
}
const isDownloadable = (s) => s.type === 'table' || (s.type === 'code' && (s.lang === 'csv' || s.lang === 'tsv'));
const csvCell = (v) => (/[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);

// The CSV text of each downloadable block in an answer, in order (tables and ```csv blocks).
export function downloadBlocks(text) {
  return segments(text).filter(isDownloadable).map(s => (s.type === 'table'
    ? s.rows.map(r => r.map(csvCell).join(',')).join('\n')
    : s.lines.join('\n')));
}

export function renderMarkdown(text, nCites = 0) {
  const out = [];
  let list = null;
  let block = 0;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const dlButton = () => `<button type="button" class="cp-link-btn" data-action="dl-block" data-block="${block++}"><i class="fa-solid fa-file-csv"></i> Download CSV</button>`;
  for (const seg of segments(text)) {
    if (seg.type === 'code') {
      closeList();
      out.push(`<div class="cp-code">${isDownloadable(seg) ? `<div class="cp-code-head"><span>${esc(seg.lang)}</span>${dlButton()}</div>` : ''}
        <pre><code>${esc(seg.lines.join('\n'))}</code></pre></div>`);
      continue;
    }
    if (seg.type === 'table') {
      closeList();
      const [head, ...body] = seg.rows;
      out.push(`<div class="cp-table-wrap"><table class="cp-table"><thead><tr>${(head || []).map(c => `<th scope="col">${inline(c, nCites)}</th>`).join('')}</tr></thead>
        <tbody>${body.map(r => `<tr>${r.map(c => `<td>${inline(c, nCites)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
        <div class="cp-table-dl">${dlButton()}</div>`);
      continue;
    }
    const line = seg.text.trimEnd();
    let m;
    if (!line.trim()) { closeList(); continue; }
    if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) {
      if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
      out.push(`<li>${inline(m[1], nCites)}</li>`);
    } else if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
      if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
      out.push(`<li>${inline(m[1], nCites)}</li>`);
    } else if ((m = line.match(/^#{1,4}\s+(.*)$/))) {
      closeList(); out.push(`<h4>${inline(m[1], nCites)}</h4>`);
    } else {
      closeList(); out.push(`<p>${inline(line, nCites)}</p>`);
    }
  }
  closeList();
  return out.join('');
}

function profileLink(row) {
  const pid = row.persona_id || row.id;
  const fromPath = encodeURIComponent(window.location.pathname + window.location.search);
  return (row.account_id && pid) ? `/profile?account=${encodeURIComponent(row.account_id)}&persona_id=${encodeURIComponent(pid)}&from=${fromPath}` : null;
}

function renderTable(table) {
  if (!table || !table.rows || !table.rows.length) return '';
  const cols = table.columns || Object.keys(table.rows[0]);
  const cell = (row, c) => {
    const v = row[c];
    if (c === 'name' && table.kind === 'people') {
      const href = profileLink(row);
      return href ? `<a href="${href}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(v);
    }
    if (c === 'deal' && table.kind === 'deals' && row.deal_id) return `<a href="/deals?deal=${encodeURIComponent(row.deal_id)}">${esc(v)}</a>`;
    if (c === 'email' && v) return `<a href="mailto:${esc(v)}">${esc(v)}</a>`;
    if (c === 'phone' && v) return `<a href="tel:${esc(v)}">${esc(v)}</a>`;
    if (c === 'changed_at' && v) return esc(new Date(v).toLocaleDateString());
    if (c === 'type') return esc(typeLabel(v));
    if (c === 'title' && table.kind === 'changes' && row.url) return `<a href="${esc(row.url)}" target="_blank" rel="noopener">${esc(v)}</a>`;
    return esc(v == null ? '' : v);
  };
  return `<div class="cp-table-wrap"><table class="cp-table">
    <thead><tr>${cols.map(c => `<th scope="col" data-sort="${esc(c)}" tabindex="0" title="Sort">${esc(c.replace(/_/g, ' '))} <i class="fa-solid fa-sort"></i></th>`).join('')}</tr></thead>
    <tbody>${table.rows.map(r => `<tr>${cols.map(c => `<td>${cell(r, c)}</td>`).join('')}</tr>`).join('')}</tbody>
  </table></div>`;
}

function renderContacts(contacts) {
  return (contacts || []).map(c => {
    const href = profileLink(c);
    return `<div class="cp-contact">
      <span class="cp-contact-avatar" ${photoAttr(c, c.persona_id)}>${esc((c.name || '?').split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase())}</span>
      <div class="cp-contact-main">
        <strong>${href ? `<a href="${href}" target="_blank" rel="noopener">${esc(c.name)}</a>` : esc(c.name)}</strong>
        <span class="cp-muted" title="${esc(fixText(c.title || ''))}">${esc(shortTitle(c.title || '', 90))}${c.account ? ` · ${esc(c.account)}` : ''}</span>
      </div>
      <div class="cp-contact-links">
        ${c.email ? `<a href="mailto:${esc(c.email)}" title="${esc(c.email)}"><i class="fa-solid fa-envelope"></i> Email</a>` : ''}
        ${c.phone ? `<a href="tel:${esc(c.phone)}" title="${esc(c.phone)}"><i class="fa-solid fa-phone"></i> Call</a>` : ''}
        ${c.linkedin ? `<a href="${esc(c.linkedin)}" target="_blank" rel="noopener"><i class="fa-brands fa-linkedin"></i></a>` : ''}
      </div>
    </div>`;
  }).join('');
}

// "Create …" proposal (apps/sales_copilot/actions.py): the records the user can review, edit and confirm.
// Edits live on the message (chat-view.js) so they survive re-renders; nothing is created until "Create".
// lookups: { connectors: [{id, name, organisation}], warm: {account_id: [{id, name, strength}]} }
const ACTION_TITLE = { task: 'Create tasks', introduction: 'Request introductions', deal: 'Create a deal' };
function renderActions(a, lookups = {}) {
  if (!a || !a.items || !a.items.length) return '';
  const head = `<header class="cp-action-head"><i class="fa-solid fa-bolt"></i> ${ACTION_TITLE[a.kind] || 'Create'}</header>`;
  if (a.done) {
    return `<section class="cp-action is-done">${head}<p class="cp-small">Created ${a.done.length}:</p>
      <ul class="cp-action-done">${a.done.map(c => `<li><i class="fa-solid fa-check"></i> ${c.href ? `<a href="${esc(c.href)}">${esc(c.label)}</a>` : esc(c.label)}</li>`).join('')}</ul></section>`;
  }
  const on = a.items.filter(it => !it.skip).length;
  const field = (i, key, value, attrs = '') => `<input class="cp-action-input" data-act-item="${i}" data-act-field="${key}" value="${esc(value == null ? '' : value)}" ${attrs}>`;
  const rows = a.items.map((it, i) => {
    const check = `<input type="checkbox" data-act-item="${i}" data-act-field="skip"${it.skip ? '' : ' checked'} aria-label="Include ${esc(it.name || it.account)}">`;
    if (a.kind === 'deal') {
      return `<li class="cp-action-row cp-action-deal">${check}<div class="cp-action-main"><strong>${esc(it.account)}</strong>
        <label>Deal name ${field(i, 'name', it.name, 'maxlength="200"')}</label>
        <div class="cp-action-pair"><label>Value (USD) ${field(i, 'value', it.value, 'type="number" min="0" step="1000" inputmode="numeric"')}</label>
          <label>Expected close ${field(i, 'close', it.close, 'type="date"')}</label></div></div></li>`;
    }
    const who = `<strong>${esc(it.name)}</strong><span class="cp-muted">${esc(shortTitle(it.title || '', 70))}${it.account ? ` · ${esc(it.account)}` : ''}</span>`;
    return `<li class="cp-action-row">${check}<div class="cp-action-main">${who}
      ${a.kind === 'task' ? field(i, 'title_text', it.title_text, 'maxlength="500" aria-label="Task title"') : ''}</div></li>`;
  }).join('');
  let shared = '';
  if (a.kind === 'task') {
    shared = `<div class="cp-action-pair"><label>Due ${`<input class="cp-action-input" type="date" data-act-field="due" value="${esc(a.due || '')}">`}</label>
      <label>Priority <select class="cp-action-input" data-act-field="priority">${['high', 'medium', 'low'].map(p => `<option value="${p}"${(a.priority || 'medium') === p ? ' selected' : ''}>${p[0].toUpperCase() + p.slice(1)}</option>`).join('')}</select></label></div>`;
  } else if (a.kind === 'introduction') {
    const warm = (a.account_ids || []).flatMap(id => (lookups.warm || {})[id] || []);
    const warmIds = new Set(warm.map(w => String(w.id)));
    const others = (lookups.connectors || []).filter(c => !warmIds.has(String(c.id)));
    const opt = (c, note) => `<option value="${c.id}"${String(a.connector || '') === String(c.id) ? ' selected' : ''}>${esc(c.name)}${note ? ` — ${esc(note)}` : ''}</option>`;
    shared = `<label>Who will introduce you?
        <select class="cp-action-input" data-act-field="connector">
          <option value="">No one yet — add to the introduction wishlist</option>
          ${warm.length ? `<optgroup label="Suggested (warm paths)">${warm.map(w => opt(w, `${w.strength} path`)).join('')}</optgroup>` : ''}
          ${others.length ? `<optgroup label="All connectors">${others.map(c => opt(c, c.organisation)).join('')}</optgroup>` : ''}
        </select></label>
      ${lookups.connectors ? '' : '<p class="cp-small cp-muted">Loading connectors…</p>'}
      <label>Why — shared with the connector (optional)
        <textarea class="cp-action-input" data-act-field="context" rows="2" maxlength="1000">${esc(a.context || '')}</textarea></label>`;
  }
  const noun = { task: 'task', introduction: a.connector ? 'introduction' : 'wishlist request', deal: 'deal' }[a.kind];
  return `<section class="cp-action" aria-label="${esc(ACTION_TITLE[a.kind] || 'Create')}">${head}
    <ul class="cp-action-list">${rows}</ul>
    ${shared ? `<div class="cp-action-shared">${shared}</div>` : ''}
    <footer class="cp-action-foot">
      <button type="button" class="cp-btn cp-btn-primary" data-action="act-create"${on ? '' : ' disabled'}>
        <i class="fa-solid fa-check"></i> Create ${on} ${noun}${on === 1 ? '' : 's'}</button>
      <span class="cp-small cp-muted">Uses your CRM permissions · nothing is created until you confirm</span>
    </footer></section>`;
}

function renderFollowups(list) {
  if (!list || !list.length) return '';
  return `<div class="cp-followups" aria-label="Suggested follow-ups">${list.map(f =>
    `<button type="button" class="cp-suggestion cp-followup" data-followup="${esc(f)}"><i class="fa-solid fa-arrow-turn-down fa-rotate-270"></i> ${esc(f)}</button>`).join('')}</div>`;
}

// Drafted email (intent "draft", guardrails applied server-side): "**Subject:** …\n\nbody\n\n_Based on sources …_"
// Plain-text email body for the clipboard / mail apps: bold markers removed, bullets as "•".
export function draftPlain(body) {
  return (body || '').replace(/\*\*(.+?)\*\*/g, '$1').replace(/^\s*[-*]\s+/gm, '• ');
}

export function parseDraft(content) {
  const text = fixText(content || '');
  const m = text.match(/^\s*\**\s*Subject\s*:?\**\s*:?\s*(.+)\n([\s\S]*)$/i);
  if (!m) return null;
  let body = m[2].trim();
  let basedOn = '';
  const b = body.match(/\n\s*_?Based on sources?[^\n]*_?\s*$/i);
  if (b) { basedOn = b[0].trim(); body = body.slice(0, b.index).trim(); }
  return { subject: m[1].replace(/\*+/g, '').trim(), body, basedOn };
}

const CHECK_ICON = { pass: 'fa-circle-check', fixed: 'fa-wrench', warn: 'fa-triangle-exclamation' };

// quiet = ordinary answers: only shown when a check fixed or flagged something
function renderGuardrails(checks, quiet = false) {
  if (!checks || !checks.length) return '';
  if (quiet && !checks.some(c => c.status !== 'pass')) return '';
  const fixed = checks.filter(c => c.status === 'fixed').length;
  const warn = checks.filter(c => c.status === 'warn').length;
  const summary = warn ? `${warn} to review` : (fixed ? `${fixed} auto-fixed` : 'all passed');
  return `<details class="cp-guard${warn ? ' has-warn' : ''}">
    <summary><i class="fa-solid fa-shield-halved"></i> Guardrail checks · ${esc(summary)}</summary>
    <ul>${checks.map(c => `<li class="cp-guard-${esc(c.status)}"><i class="fa-solid ${CHECK_ICON[c.status] || 'fa-circle'}"></i>
      <strong>${esc(c.check)}</strong> ${esc(c.detail)}</li>`).join('')}</ul>
  </details>`;
}

function renderDraft(msg, nCites) {
  const d = parseDraft(msg.content);
  if (!d) return null;
  const to = (msg.extras && msg.extras.draft_to) || '';
  const mailto = `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(d.subject)}&body=${encodeURIComponent(draftPlain(d.body))}`;
  const paras = renderMarkdown(d.body, 0);   // keeps paragraphs, **bold** labels and bullet lists
  return `<div class="cp-email">
      <div class="cp-email-head"><span class="cp-email-label">Subject</span><strong>${esc(d.subject)}</strong></div>
      ${to ? `<div class="cp-email-head cp-email-to"><span class="cp-email-label">To</span><span>${esc(to)}</span></div>` : ''}
      <div class="cp-email-body">${paras}</div>
      <div class="cp-email-actions">
        <button type="button" class="cp-btn cp-btn-primary" data-action="copy-email"><i class="fa-regular fa-copy"></i> Copy email</button>
        <a class="cp-btn" href="${esc(mailto)}"><i class="fa-regular fa-envelope"></i> Open in mail app</a>
        <span class="cp-muted cp-small">Review before sending — replace [Your name].</span>
      </div>
    </div>
    ${d.basedOn ? `<div class="cp-md cp-small">${renderMarkdown(d.basedOn, nCites)}</div>` : ''}
    ${renderGuardrails(msg.extras && msg.extras.guardrails)}`;
}

export function renderMessage(msg, opts = {}) {
  if (msg.role === 'user') {
    return `<div class="cp-msg cp-msg-user" data-user-message>
      <div class="cp-bubble">${esc(msg.content).replace(/\n/g, '<br>')}</div>
      ${msg.suggestSave ? `<div class="cp-save-suggest">
        <i class="fa-solid fa-thumbtack"></i> Save this to your private notes?
        <button type="button" class="cp-link-btn" data-save-suggest="yes">Save</button>
        <button type="button" class="cp-link-btn cp-muted" data-save-suggest="no">Dismiss</button></div>` : ''}
    </div>`;
  }
  const [label, icon, tip] = MODE_LABEL[msg.mode] || MODE_LABEL.database;
  const cites = msg.citations || [];
  const extras = msg.extras || {};
  const fb = msg.feedback;
  const hasTable = !!(extras.table && extras.table.rows && extras.table.rows.length);
  const streaming = !!msg.streaming;
  return `<div class="cp-msg cp-msg-bot${streaming ? ' is-streaming' : ''}${opts.isLast ? ' is-last' : ''}" data-message-id="${msg.id || ''}">
    <div class="cp-avatar" aria-hidden="true"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
    <div class="cp-bubble">
      <div class="cp-msg-head">
        ${msg.mode ? `<span class="cp-badge cp-badge-${esc(msg.mode)}" title="${esc(tip)}"><i class="fa-solid ${icon}"></i> ${label}</span>` : ''}
        ${msg.stopped ? '<span class="cp-badge cp-badge-cached"><i class="fa-solid fa-circle-stop"></i> Stopped</span>' : ''}
        <span class="cp-time">${esc(timeLabel(msg.created_at))}</span>
      </div>
      ${msg.status && streaming ? `<div class="cp-status"><span class="cp-dot"></span><span class="cp-dot"></span><span class="cp-dot"></span> ${esc(msg.status)}</div>` : ''}
      ${streaming && !msg.content ? '<div class="cp-skel" aria-hidden="true"><span></span><span></span><span></span></div>' : ''}
      ${(!streaming && extras.draft && renderDraft(msg, cites.length)) || `<div class="cp-md">${renderMarkdown(msg.content, cites.length)}${streaming && msg.content ? '<span class="cp-caret" aria-hidden="true"></span>' : ''}</div>`}
      ${!streaming && !extras.draft ? renderGuardrails(extras.guardrails, true) : ''}
      ${renderTable(extras.table)}
      ${hasTable && !streaming && msg.id ? `<div class="cp-table-dl${extras.download ? ' is-primary' : ''}">
        <button type="button" class="cp-btn${extras.download ? ' cp-btn-primary' : ''}" data-action="dl-xlsx"><i class="fa-regular fa-file-excel"></i> Download Excel</button>
        <button type="button" class="cp-btn" data-action="dl-csv"><i class="fa-solid fa-file-csv"></i> CSV</button>
        <span class="cp-small cp-muted">${extras.table.rows.length} row${extras.table.rows.length === 1 ? '' : 's'}</span></div>` : ''}
      ${renderContacts(extras.contacts)}
      ${!streaming && msg.id ? renderActions(extras.actions, opts.lookups) : ''}
      ${cites.length && !opts.compact ? `<div class="cp-cite-row" aria-label="Sources">${cites.slice(0, 5).map(c =>
        `<button type="button" class="cp-src-pill" data-cite="${c.n}" title="${esc(shortTitle(c.title, 200))}${c.snippet ? ' — ' + esc(fixText(c.snippet).slice(0, 160)) : ''}"><span class="cp-cite">${c.n}</span><i class="${typeIcon(c.doc_type)}"></i><span class="cp-pill-text">${esc(shortTitle(c.title, 38))}</span></button>`).join('')}${cites.length > 5 ? `<span class="cp-more-sources">+${cites.length - 5} more in Sources</span>` : ''}</div>` : ''}
      ${!streaming && msg.id ? `<div class="cp-msg-actions">
        <button type="button" class="cp-icon-btn ${fb === 1 ? 'on' : ''}" data-action="fb-up" title="Helpful" aria-label="Helpful"><i class="fa-solid fa-thumbs-up"></i></button>
        <button type="button" class="cp-icon-btn ${fb === -1 ? 'on' : ''}" data-action="fb-down" title="Not helpful" aria-label="Not helpful"><i class="fa-solid fa-thumbs-down"></i></button>
        <span class="cp-sep"></span>
        <button type="button" class="cp-icon-btn" data-action="copy" title="Copy answer" aria-label="Copy answer"><i class="fa-regular fa-copy"></i></button>
        <button type="button" class="cp-icon-btn" data-action="email" title="Copy as email draft" aria-label="Copy as email draft"><i class="fa-regular fa-envelope"></i></button>
        <button type="button" class="cp-icon-btn" data-action="remember" title="Save to my notes" aria-label="Save to my notes"><i class="fa-solid fa-thumbtack"></i></button>
        <span class="cp-menu-wrap">
          <button type="button" class="cp-icon-btn" data-action="menu-download" title="Download" aria-haspopup="true" aria-label="Download"><i class="fa-solid fa-download"></i></button>
          <span class="cp-menu" role="menu">
            <button type="button" role="menuitem" data-action="dl-pdf"><i class="fa-regular fa-file-pdf"></i> Answer as PDF</button>
            ${hasTable ? '<button type="button" role="menuitem" data-action="dl-xlsx"><i class="fa-regular fa-file-excel"></i> Table as Excel</button>' : ''}
            ${cites.length ? '<button type="button" role="menuitem" data-action="dl-xlsx-sources"><i class="fa-regular fa-file-excel"></i> Sources as Excel</button>' : ''}
          </span>
        </span>
        ${opts.isLast ? '<button type="button" class="cp-icon-btn" data-action="regenerate" title="Ask again" aria-label="Ask again"><i class="fa-solid fa-rotate-right"></i></button>' : ''}
      </div>` : ''}
      ${!streaming && opts.isLast ? renderFollowups(extras.followups) : ''}
    </div>
  </div>`;
}

export function renderSources(citations) {
  if (!citations.length) return '<span class="cp-muted cp-small">Sources cited in answers appear here.</span>';
  return citations.map(c => {
    const date = c.published_at ? new Date(c.published_at).toLocaleDateString() : '';
    const inner = `<div class="cp-source-title"><i class="${typeIcon(c.doc_type)}"></i><span title="${esc(fixText(c.title || ''))}">${esc(shortTitle(c.title, 90))}</span></div>
      <div class="cp-source-meta">${esc(typeLabel(c.doc_type))}${date ? ` · ${esc(date)}` : ''}</div>
      ${c.snippet ? `<div class="cp-source-snippet">${esc(fixText(c.snippet).slice(0, 180))}</div>` : ''}`;
    return c.url
      ? `<a class="cp-source" id="cpSrc-${esc(c.key)}" href="${esc(c.url)}" target="_blank" rel="noopener">${inner}</a>`
      : `<div class="cp-source" id="cpSrc-${esc(c.key)}">${inner}</div>`;
  }).join('');
}

export function renderEmpty(scopeName, suggestions, recent = []) {
  return `<div class="cp-empty">
    <div class="cp-empty-icon" aria-hidden="true"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
    <h2>${scopeName ? `Ask about ${esc(scopeName)}` : 'What are you working on?'}</h2>
    <div class="cp-muted">Answers come from your accounts' data — people, call-prep, news, signals, hiring and digests — with sources you can check.</div>
    <div class="cp-suggestions">${suggestions.map(s => `<button type="button" class="cp-suggestion"><span>${esc(s)}</span><i class="fa-solid fa-arrow-right" aria-hidden="true"></i></button>`).join('')}</div>
    <div class="cp-tips">
      <span><kbd>@</kbd> mention a person or account</span>
      <span><kbd>/</kbd> commands: /prep, /remember, /changes</span>
      <span><kbd>↑</kbd> edit your last question</span>
    </div>
    ${recent.length ? `<div class="cp-recent"><div class="cp-h3">Continue a recent chat</div>${recent.map(r =>
      `<button type="button" class="cp-recent-item" data-open-session="${esc(r.id)}"><i class="fa-regular fa-comments"></i> ${esc(r.title || 'Untitled chat')}</button>`).join('')}</div>` : ''}
  </div>`;
}

export function renderMentionList(items, activeIndex) {
  if (!items.length) return '<div class="cp-mention-empty">No matching people or accounts</div>';
  return items.map((it, i) => `<button type="button" class="cp-mention-item${i === activeIndex ? ' active' : ''}" data-mention-index="${i}" role="option" aria-selected="${i === activeIndex}">
    <i class="fa-solid ${it.type === 'persona' ? 'fa-user' : 'fa-building'}"></i>
    <span class="cp-mention-name">${esc(it.name)}</span>
    <span class="cp-muted cp-small">${esc(it.type === 'persona' ? [it.title, it.account].filter(Boolean).join(' · ') : 'Account')}</span>
  </button>`).join('');
}
