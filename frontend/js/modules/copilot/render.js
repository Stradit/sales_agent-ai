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

// CSV/TSV text → rows of cells (quoted fields, "" escapes, commas and line breaks inside quotes)
function parseDelimited(text, sep = ',') {
  const rows = [];
  let row = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (ch === '"') quoted = false; else cell += ch;
    } else if (ch === '"' && !cell) quoted = true;
    else if (ch === sep) { row.push(cell.trim()); cell = ''; }
    else if (ch === '\n') { row.push(cell.trim()); rows.push(row); row = []; cell = ''; }
    else cell += ch;
  }
  if (cell || row.length) { row.push(cell.trim()); rows.push(row); }
  return rows.filter(r => r.some(c => c));
}
const colLetter = (n) => { let s = ''; for (n += 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s; return s; };

// A CSV block shown like a spreadsheet: column letters, row numbers, a frozen header row, cells that truncate
function renderSheet(seg, dlButton) {
  const rows = parseDelimited(seg.lines.join('\n'), seg.lang === 'tsv' ? '\t' : ',');
  // Still emit the button when empty, so block numbers match downloadBlocks()
  if (!rows.length) return `<div class="cp-code"><div class="cp-code-head"><span>${esc(seg.lang)}</span>${dlButton()}</div></div>`;
  const width = Math.max(...rows.map(r => r.length));
  const cells = (r) => Array.from({ length: width }, (_, c) => {
    const v = r[c] || '';
    return `<td title="${esc(v)}"${/^-?[\d.,]+%?$/.test(v) ? ' class="is-num"' : ''}>${esc(v)}</td>`;
  }).join('');
  return `<div class="cp-sheet">
    <div class="cp-sheet-bar"><span><i class="fa-solid fa-table-cells"></i> Spreadsheet · ${rows.length - 1} row${rows.length === 2 ? '' : 's'} × ${width} column${width === 1 ? '' : 's'}</span>${dlButton()}</div>
    <div class="cp-sheet-scroll" tabindex="0" role="region" aria-label="Spreadsheet preview"><table class="cp-sheet-grid">
      <thead><tr><th class="cp-sheet-corner" aria-hidden="true"></th>${Array.from({ length: width }, (_, c) => `<th scope="col" aria-hidden="true">${colLetter(c)}</th>`).join('')}</tr></thead>
      <tbody>${rows.map((r, i) => `<tr${i === 0 ? ' class="is-head"' : ''}><th scope="row" class="cp-sheet-n">${i + 1}</th>${cells(r)}</tr>`).join('')}</tbody>
    </table></div></div>`;
}

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
      const sheet = isDownloadable(seg) ? renderSheet(seg, dlButton) : '';
      out.push(sheet || `<div class="cp-code"><pre><code>${esc(seg.lines.join('\n'))}</code></pre></div>`);
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
const ACTION_TITLE = { task: 'Create tasks', introduction: 'Request introductions', deal: 'Create a deal',
  activity: 'Log an activity', move_deal: 'Move a deal', update_deal: 'Update a deal', update_intro: 'Update an introduction' };
const FORECAST_OPTIONS = [['pipeline', 'Pipeline'], ['best_case', 'Best case'], ['commit', 'Commit'], ['omitted', 'Omitted']];
const ACTIVITY_TYPES = [['call', 'Call'], ['meeting', 'Meeting'], ['email', 'Email'], ['linkedin', 'LinkedIn'], ['note', 'Note']];
// "2026-10-29T14:05" in the viewer's time zone, for <input type="datetime-local">
const localNow = () => { const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
function renderActions(a, lookups = {}) {
  if (!a || !a.items || (!a.items.length && a.kind !== 'activity')) return '';
  const head = `<header class="cp-action-head"><i class="fa-solid fa-bolt"></i> ${ACTION_TITLE[a.kind] || 'Create'}</header>`;
  if (a.done) {
    const verb = { move_deal: 'Moved', activity: 'Logged', update_deal: 'Saved', update_intro: 'Saved' }[a.kind] || 'Created';
    return `<section class="cp-action is-done">${head}<p class="cp-small">${verb} ${a.done.length}:</p>
      <ul class="cp-action-done">${a.done.map(c => `<li><i class="fa-solid fa-check"></i> ${c.href ? `<a href="${esc(c.href)}">${esc(c.label)}</a>` : esc(c.label)}</li>`).join('')}</ul></section>`;
  }
  if (a.kind === 'move_deal') return renderMove(a, head);
  if (a.kind === 'update_deal') return renderUpdateDeal(a, head);
  if (a.kind === 'update_intro') return renderUpdateIntro(a, head);
  const on = a.kind === 'activity' ? 1 : a.items.filter(it => !it.skip).length;
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
  } else if (a.kind === 'activity') {
    if (a.when === undefined) a.when = localNow();
    shared = `<div class="cp-action-pair">
        <label>Type <select class="cp-action-input" data-act-field="type">${ACTIVITY_TYPES.map(([k, l]) => `<option value="${k}"${a.type === k ? ' selected' : ''}>${l}</option>`).join('')}</select></label>
        <label>When <input class="cp-action-input" type="datetime-local" data-act-field="when" value="${esc(a.when || '')}"></label></div>
      <label>Subject <input class="cp-action-input" data-act-field="subject" maxlength="300" value="${esc(a.subject || '')}"></label>
      <label>Notes (optional)
        <textarea class="cp-action-input" data-act-field="summary" rows="3" maxlength="20000">${esc(a.summary || '')}</textarea></label>
      <p class="cp-small cp-muted">Goes on the ${esc(a.account || 'account')} timeline${a.items.length ? ' and on each ticked person' : ''}, visible to the team.</p>`;
  }
  const noun = { task: 'task', introduction: a.connector ? 'introduction' : 'wishlist request', deal: 'deal' }[a.kind];
  const label = a.kind === 'activity'
    ? `Log ${esc(((ACTIVITY_TYPES.find(([k]) => k === a.type) || [])[1] || 'activity').toLowerCase())}`
    : `Create ${on} ${noun}${on === 1 ? '' : 's'}`;
  return `<section class="cp-action" aria-label="${esc(ACTION_TITLE[a.kind] || 'Create')}">${head}
    ${rows ? `<ul class="cp-action-list">${rows}</ul>` : ''}
    ${shared ? `<div class="cp-action-shared">${shared}</div>` : ''}
    <footer class="cp-action-foot">
      <button type="button" class="cp-btn cp-btn-primary" data-action="act-create"${on ? '' : ' disabled'}>
        <i class="fa-solid fa-check"></i> ${label}</button>
      <span class="cp-small cp-muted">Uses your CRM permissions · nothing changes until you confirm</span>
    </footer></section>`;
}

// Deal stage change: from → to per matching deal; a reason when moving back, a loss reason for Lost.
function renderMove(a, head) {
  const picked = a.items.filter(it => !it.skip && !it.blocked);
  const rows = a.items.map((it, i) => `<li class="cp-action-row${it.blocked ? ' is-blocked' : ''}">
    <input type="checkbox" data-act-item="${i}" data-act-field="skip"${it.skip || it.blocked ? '' : ' checked'}${it.blocked ? ' disabled' : ''} aria-label="Move ${esc(it.name)}">
    <div class="cp-action-main"><strong>${esc(it.name)}</strong><span class="cp-muted">${esc(it.account)}</span>
      <span class="cp-move-steps"><span class="cp-stage">${esc(it.from_label)}</span><i class="fa-solid fa-arrow-right" aria-hidden="true"></i>
        <span class="cp-stage is-to">${esc(it.to_label)}</span>${it.back ? ' <span class="cp-muted">(moving back)</span>' : ''}</span>
      ${it.note ? `<span class="cp-move-note"><i class="fa-solid fa-circle-info"></i> ${esc(it.note)}</span>` : ''}</div></li>`).join('');
  const needReason = picked.some(it => it.back);
  const needLost = picked.some(it => it.to === 'lost');
  const shared = `${needReason ? `<label>Why move back? (required)
      <input class="cp-action-input" data-act-field="reason" maxlength="500" value="${esc(a.reason || '')}"></label>` : ''}
    ${needLost ? `<label>Why was it lost? (required)
      <select class="cp-action-input" data-act-field="lost_category"><option value="">Choose…</option>${(a.lost_categories || []).map(c =>
        `<option value="${esc(c.key)}"${a.lost_category === c.key ? ' selected' : ''}>${esc(c.label)}</option>`).join('')}</select></label>
      <label>Note${a.lost_category === 'other' ? ' (required)' : ' (optional)'}
        <input class="cp-action-input" data-act-field="lost_reason" maxlength="500" value="${esc(a.lost_reason || '')}"></label>` : ''}`;
  return `<section class="cp-action" aria-label="Move a deal">${head}
    <ul class="cp-action-list">${rows}</ul>
    ${shared.trim() ? `<div class="cp-action-shared">${shared}</div>` : ''}
    <footer class="cp-action-foot">
      <button type="button" class="cp-btn cp-btn-primary" data-action="act-create"${picked.length ? '' : ' disabled'}>
        <i class="fa-solid fa-check"></i> Move ${picked.length} deal${picked.length === 1 ? '' : 's'}</button>
      <span class="cp-small cp-muted">Same rules as the Deals page · nothing changes until you confirm</span>
    </footer></section>`;
}

// Which of several matching records to change: ticked rows get the change (usually just the closest match)
function recordRows(a, describe) {
  return `<ul class="cp-action-list">${a.items.map((it, i) => `<li class="cp-action-row">
    <input type="checkbox" data-act-item="${i}" data-act-field="skip"${it.skip ? '' : ' checked'} aria-label="Change ${esc(it.name)}">
    <div class="cp-action-main"><strong>${esc(it.name)}</strong><span class="cp-muted">${describe(it)}</span></div></li>`).join('')}</ul>`;
}
const footer = (on, label) => `<footer class="cp-action-foot">
    <button type="button" class="cp-btn cp-btn-primary" data-action="act-create"${on ? '' : ' disabled'}><i class="fa-solid fa-check"></i> ${label}</button>
    <span class="cp-small cp-muted">Same rules as the page · nothing changes until you confirm</span></footer>`;

// Deal field changes (records.py update_deal_turn): current value → new value, per field
function renderUpdateDeal(a, head) {
  const on = a.items.filter(it => !it.skip).length;
  const fmt = (f, v) => {
    if (v == null || v === '') return '—';
    if (f.key === 'value_amount') return `${Number(v).toLocaleString()} ${esc(a.currency || 'USD')}`;
    if (f.key === 'forecast_category') return esc((FORECAST_OPTIONS.find(([k]) => k === v) || [v, v])[1]);
    if (f.key === 'expected_close' || f.key === 'next_step_due') return esc(new Date(`${String(v).slice(0, 10)}T00:00:00`).toLocaleDateString());
    return esc(String(v)) + (f.key === 'probability' ? '%' : '');
  };
  const input = (f, i) => {
    const attrs = `class="cp-action-input" data-act-fld="${i}" aria-label="New ${esc(f.label)}"`;
    if (f.key === 'forecast_category') {
      return `<select ${attrs}><option value="">Choose…</option>${FORECAST_OPTIONS.map(([k, l]) => `<option value="${k}"${f.value === k ? ' selected' : ''}>${l}</option>`).join('')}</select>`;
    }
    const type = { value_amount: 'number', probability: 'number', expected_close: 'date', next_step_due: 'date' }[f.key] || 'text';
    const extra = f.key === 'value_amount' ? ' min="0" step="1000"' : f.key === 'probability' ? ' min="0" max="100" step="5"' : ' maxlength="500"';
    return `<input ${attrs} type="${type}"${extra} value="${esc(f.value == null ? '' : f.value)}">`;
  };
  const fields = a.fields.map((f, i) => `<label class="cp-field-change"><span>${esc(f.label)} <span class="cp-muted">· now ${fmt(f, f.current)}</span></span>${input(f, i)}</label>`).join('');
  return `<section class="cp-action" aria-label="Update a deal">${head}
    ${recordRows(a, it => `${esc(it.account)} · ${esc(it.from_label || '')}`)}
    <div class="cp-action-shared">${fields}</div>${footer(on, 'Save changes')}</section>`;
}

// Introduction status / next step (records.py update_intro_turn)
function renderUpdateIntro(a, head) {
  const on = a.items.filter(it => !it.skip).length;
  const first = a.items.find(it => !it.skip) || a.items[0];
  const moving = first && a.status !== first.from;
  return `<section class="cp-action" aria-label="Update an introduction">${head}
    ${recordRows(a, it => `${esc(it.account)} · via ${esc(it.connector)} · now ${esc(it.from_label)}`)}
    <div class="cp-action-shared">
      <label>Status <select class="cp-action-input" data-act-field="status">${(a.statuses || []).map(s =>
        `<option value="${esc(s.key)}"${a.status === s.key ? ' selected' : ''}>${esc(s.label)}</option>`).join('')}</select></label>
      ${moving ? `<label>Reason <span class="cp-muted">(needed to move back, mark stale or decline)</span>
        <input class="cp-action-input" data-act-field="reason" maxlength="500" value="${esc(a.reason || '')}"></label>` : ''}
      <label>Next step <input class="cp-action-input" data-act-field="next_step" maxlength="500" value="${esc(a.next_step || '')}"></label>
    </div>${footer(on, 'Save changes')}</section>`;
}

// One-tap next steps under briefs and drafts (actions.py next_actions): each sends its text as a question
function renderNextActions(list) {
  if (!list || !list.length) return '';
  return `<div class="cp-next-actions" aria-label="Next steps"><span class="cp-next-label"><i class="fa-solid fa-bolt"></i> Next step</span>
    ${list.map(q => `<button type="button" class="cp-suggestion cp-next-action" data-followup="${esc(q)}">${esc(q)}</button>`).join('')}</div>`;
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
      ${extras.record && extras.record.href ? `<div class="cp-record-link"><a class="cp-btn" href="${esc(extras.record.href)}"><i class="fa-solid fa-arrow-up-right-from-square"></i> ${esc(extras.record.label || 'Open')}</a></div>` : ''}
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
      ${!streaming && opts.isLast ? renderNextActions(extras.next_actions) + renderFollowups(extras.followups) : ''}
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
