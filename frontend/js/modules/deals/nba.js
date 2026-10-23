// Next best action (#12): one suggestion at the top of the deal room, with the one-click fix, an
// optional AI-drafted email, side-widget assistant, and email history from PostgreSQL deal_email_drafts.
// Rules: apps/sales_deals/next_action.py; draft: POST /{id}/next-action/draft; history: GET /{id}/email-drafts.
let c = null; // { api, esc, showToast, getDeal, runGapAction, openTab, rerender }
const nb = {
  dealId: null,
  code: null,
  draft: null,
  busy: false,
  widgetOpen: false,
  widgetView: 'draft', // 'draft' | 'history'
  history: [],
  historyLoading: false,
};

function formatDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function cleanSubject(s) {
  if (!s) return '';
  let str = s.trim();
  for (let i = 0; i < 3; i++) {
    const cleaned = str.replace(/^(?:(?:Our\s+)?Next\s+step(?:\s+is)?\s*[:\-–—]\s*|Do\s+or\s+reschedule\s*:\s*|Confirm\s+(?:the\s+)?next\s+step(?:\s+with\s+[^:]+)?\s*:\s*|Action\s*:\s*|Goal\s*:\s*|Subject\s*:\s*)/i, '').trim();
    if (cleaned === str) break;
    str = cleaned;
  }
  if (str && str[0] === str[0].toLowerCase()) {
    str = str[0].toUpperCase() + str.slice(1);
  }
  return str;
}

export function initNba(ctx) {
  c = ctx;
  bindWidgetEvents();
}

function bindWidgetEvents() {
  const widget = document.getElementById('dlEmailWidget');
  const overlay = document.getElementById('dlWidgetOverlay');
  if (widget && !widget._nbaBound) {
    widget._nbaBound = true;
    widget.addEventListener('click', async (e) => {
      await handleWidgetClick(e);
    });
    widget.addEventListener('input', (e) => {
      handleWidgetInput(e);
    });
  }
  if (overlay && !overlay._nbaBound) {
    overlay._nbaBound = true;
    overlay.addEventListener('click', () => closeEmailWidget());
  }
}

export function closeEmailWidget() {
  nb.widgetOpen = false;
  const w = document.getElementById('dlEmailWidget');
  const o = document.getElementById('dlWidgetOverlay');
  if (w) w.hidden = true;
  if (o) o.hidden = true;
}

export function openEmailWidget(view = 'draft') {
  nb.widgetOpen = true;
  nb.widgetView = view;
  const w = document.getElementById('dlEmailWidget');
  const o = document.getElementById('dlWidgetOverlay');
  if (w) w.hidden = false;
  if (o) o.hidden = false;
  renderWidget();
}

export function nbaCard(d) {
  const a = d.next_action;
  if (!a) {
    if (nb.dealId !== d.id) {
      nb.dealId = d.id;
      nb.code = null;
      nb.draft = null;
      nb.history = [];
      closeEmailWidget();
    }
    return '';
  }
  if (nb.dealId !== d.id || nb.code !== a.code) {
    nb.dealId = d.id;
    nb.code = a.code;
    nb.draft = null;
    nb.history = [];
    closeEmailWidget();
  }
  const ro = document.body.classList.contains('role-viewer');
  const fixBtn = !ro && (a.fix || a.tab)
    ? `<button type="button" class="dl-btn dl-btn-primary" data-nba-fix><i class="fa-solid fa-arrow-right"></i> Do it</button>` : '';
  const draftBtn = !ro && a.can_draft
    ? `<button type="button" class="dl-btn" data-nba-draft${nb.busy ? ' disabled' : ''}><i class="fa-solid fa-wand-magic-sparkles"></i> ${nb.busy ? 'Drafting…' : nb.draft ? 'Redraft' : 'Draft the email'}</button>` : '';
  const historyBtn = !ro && a.can_draft
    ? `<button type="button" class="dl-btn" data-nba-history title="View past generated emails"><i class="fa-solid fa-clock-rotate-left"></i> History</button>` : '';

  const draftBadge = nb.draft
    ? `<div class="dl-nba-draft-badge"><i class="fa-solid fa-check"></i> Email drafted &amp; saved — <button type="button" class="tk-link" data-nba-open-widget>View draft in side widget</button></div>` : '';

  bindWidgetEvents();
  if (nb.widgetOpen) renderWidget();

  return `<section class="dl-nba" aria-labelledby="dlNbaTitle">
    <div class="dl-nba-label"><i class="fa-solid fa-bullseye"></i> Next best action</div>
    <h3 id="dlNbaTitle">${c.esc(a.title)}</h3>
    <p class="dl-nba-why">${c.esc(a.why)}</p>
    ${fixBtn || draftBtn ? `<div class="dl-nba-actions">${fixBtn}${draftBtn}${historyBtn}
      ${a.can_draft && !nb.draft ? '<span class="dl-muted dl-nba-note">Uses one AI request from the team\'s daily allowance.</span>' : ''}</div>` : ''}
    ${draftBadge}
  </section>`;
}

function renderWidget() {
  const w = document.getElementById('dlEmailWidget');
  if (!w || !nb.widgetOpen) return;
  const d = c.getDeal();
  if (!d) return;
  const a = d.next_action || {};
  const isHistory = nb.widgetView === 'history';

  const historyCount = nb.history ? nb.history.length : 0;
  const countBadge = historyCount ? `<span class="dl-widget-badge">${historyCount}</span>` : '';

  let bodyContent = '';
  if (isHistory) {
    if (nb.historyLoading) {
      bodyContent = `<div class="dl-widget-loading"><div class="tk-loading" aria-busy="true"><span></span><span></span><span></span></div><p>Loading email history…</p></div>`;
    } else if (!nb.history.length) {
      bodyContent = `<div class="dl-history-empty"><i class="fa-regular fa-folder-open"></i><p>No email drafts generated yet for this deal.</p></div>`;
    } else {
      bodyContent = `<div class="dl-history-list">${nb.history.map(item => `
        <article class="dl-history-card">
          <div class="dl-history-top">
            <span class="dl-history-target"><i class="fa-regular fa-user"></i> ${c.esc(item.recipient_name || item.recipient_email || 'Recipient')}</span>
            <span><i class="fa-regular fa-clock"></i> ${c.esc(formatDateTime(item.created_at))}</span>
          </div>
          <div class="dl-history-subj">Subject: ${c.esc(item.subject)}</div>
          <div class="dl-history-preview">${c.esc(item.body)}</div>
          <div class="dl-history-actions">
            <button type="button" class="dl-btn dl-btn-sm dl-btn-primary" data-history-use="${item.id}"><i class="fa-solid fa-arrow-turn-up"></i> Use this draft</button>
            <button type="button" class="dl-btn dl-btn-sm" data-history-copy="${item.id}"><i class="fa-regular fa-copy"></i> Copy</button>
            <span class="dl-muted" style="margin-left:auto;font-size:.72rem;">by ${c.esc(item.user_name || 'rep')}</span>
          </div>
        </article>`).join('')}</div>`;
    }
  } else {
    // Draft view
    if (nb.busy) {
      bodyContent = `<div class="dl-widget-loading"><div class="tk-loading" aria-busy="true"><span></span><span></span><span></span></div><p>Generating email draft with AI…</p></div>`;
    } else if (nb.draft) {
      const x = nb.draft;
      const targetLabel = x.recipient_name || (a.target && a.target.name) || '';
      const emailLabel = x.to || '';
      bodyContent = `
        <div class="dl-widget-meta-card">
          <div><strong>To:</strong> <span>${c.esc(targetLabel)}${emailLabel ? ` &lt;${c.esc(emailLabel)}&gt;` : ''}</span></div>
          ${a.title ? `<div><strong>Action:</strong> <span>${c.esc(a.title)}</span></div>` : ''}
          ${x.created_at ? `<div class="dl-muted"><i class="fa-regular fa-clock"></i> Saved at ${c.esc(formatDateTime(x.created_at))}</div>` : ''}
        </div>
        <div class="dl-widget-field">
          <label for="dlWidgetSubject">Subject</label>
          <input id="dlWidgetSubject" maxlength="200" value="${c.esc(x.subject)}">
        </div>
        <div class="dl-widget-field">
          <label for="dlWidgetBody">Email Body</label>
          <textarea id="dlWidgetBody" rows="12">${c.esc(x.body)}</textarea>
        </div>
      `;
    } else {
      bodyContent = `
        <div class="dl-widget-loading">
          <i class="fa-solid fa-wand-magic-sparkles" style="font-size:2rem;color:var(--brand);opacity:.6;"></i>
          <p>No active email draft for this action yet.</p>
          <button type="button" class="dl-btn dl-btn-primary" data-nba-draft><i class="fa-solid fa-wand-magic-sparkles"></i> Generate email draft</button>
        </div>
      `;
    }
  }

  const footContent = !isHistory && nb.draft && !nb.busy ? `
    <footer class="dl-widget-foot">
      <button type="button" class="dl-btn dl-btn-primary" data-nba-draft><i class="fa-solid fa-wand-magic-sparkles"></i> Redraft</button>
      <button type="button" class="dl-btn" data-nba-history><i class="fa-solid fa-clock-rotate-left"></i> History</button>
      <button type="button" class="dl-btn" data-nba-copy><i class="fa-regular fa-copy"></i> Copy</button>
      <a class="dl-btn" href="#" data-nba-mail><i class="fa-regular fa-envelope"></i> Open in email</a>
      <span class="dl-muted">AI draft saved to history — check the facts before sending.</span>
    </footer>` : (isHistory ? `
    <footer class="dl-widget-foot">
      <button type="button" class="dl-btn dl-btn-primary" data-widget-tab="draft"><i class="fa-solid fa-arrow-left"></i> Back to editor</button>
      <button type="button" class="dl-btn" data-nba-draft><i class="fa-solid fa-wand-magic-sparkles"></i> Redraft</button>
    </footer>` : '');

  w.innerHTML = `
    <header class="dl-widget-head">
      <h3 class="dl-widget-title"><i class="fa-solid fa-wand-magic-sparkles"></i> Email Assistant</h3>
      <div class="dl-widget-nav">
        <div class="dl-widget-tabs" role="tablist">
          <button type="button" class="dl-widget-tab${!isHistory ? ' active' : ''}" data-widget-tab="draft" role="tab" aria-selected="${!isHistory}"><i class="fa-solid fa-pen-to-square"></i> Draft</button>
          <button type="button" class="dl-widget-tab${isHistory ? ' active' : ''}" data-widget-tab="history" role="tab" aria-selected="${isHistory}"><i class="fa-solid fa-clock-rotate-left"></i> History ${countBadge}</button>
        </div>
        <button type="button" class="dl-icon" data-widget-close aria-label="Close side widget"><i class="fa-solid fa-xmark"></i></button>
      </div>
    </header>
    <div class="dl-widget-body">
      ${bodyContent}
    </div>
    ${footContent}
  `;
}

async function triggerDraft() {
  const d = c.getDeal();
  if (!d || !d.next_action || nb.busy) return;
  openEmailWidget('draft');
  nb.busy = true;
  renderWidget();
  c.rerender();
  try {
    const res = await c.api(`/${d.id}/next-action/draft`, { method: 'POST' });
    if (res && res.subject) res.subject = cleanSubject(res.subject);
    nb.draft = res;
    nb.code = d.next_action.code;
    if (nb.history && nb.history.length) {
      nb.history.unshift(res);
    }
    c.showToast('Email draft generated and saved to history');
  } catch (err) {
    c.showToast(err.message || 'Failed to draft email');
  }
  nb.busy = false;
  renderWidget();
  c.rerender();
  const body = document.getElementById('dlWidgetBody');
  if (body) body.focus();
}

async function loadHistory() {
  const d = c.getDeal();
  if (!d) return;
  openEmailWidget('history');
  nb.historyLoading = true;
  renderWidget();
  try {
    const list = await c.api(`/${d.id}/email-drafts`);
    nb.history = (Array.isArray(list) ? list : []).map(h => ({
      ...h,
      subject: cleanSubject(h.subject),
    }));
  } catch (err) {
    c.showToast('Failed to load email history: ' + err.message);
  }
  nb.historyLoading = false;
  renderWidget();
}

async function handleWidgetClick(e) {
  const t = e.target;
  if (t.closest('[data-widget-close]')) {
    closeEmailWidget();
    return true;
  }
  const tab = t.closest('[data-widget-tab]');
  if (tab) {
    const view = tab.dataset.widgetTab;
    if (view === 'history') {
      await loadHistory();
    } else {
      openEmailWidget('draft');
    }
    return true;
  }
  if (t.closest('[data-nba-draft]')) {
    await triggerDraft();
    return true;
  }
  if (t.closest('[data-nba-history]')) {
    await loadHistory();
    return true;
  }
  if (t.closest('[data-nba-copy]')) {
    const subj = (document.getElementById('dlWidgetSubject') || {}).value || '';
    const bdy = (document.getElementById('dlWidgetBody') || {}).value || '';
    const txt = `${subj}\n\n${bdy}`;
    try {
      await navigator.clipboard.writeText(txt);
      c.showToast('Draft copied to clipboard');
    } catch {
      c.showToast('Copy failed — select the text and press Ctrl+C');
    }
    return true;
  }
  const mail = t.closest('[data-nba-mail]');
  if (mail) {
    const subj = (document.getElementById('dlWidgetSubject') || {}).value || '';
    const bdy = (document.getElementById('dlWidgetBody') || {}).value || '';
    mail.href = `mailto:${encodeURIComponent((nb.draft && nb.draft.to) || '')}?subject=${encodeURIComponent(subj)}&body=${encodeURIComponent(bdy)}`;
    return true;
  }
  const useBtn = t.closest('[data-history-use]');
  if (useBtn) {
    const id = Number(useBtn.dataset.historyUse);
    const item = nb.history.find(h => h.id === id);
    if (item) {
      nb.draft = {
        id: item.id,
        subject: item.subject,
        body: item.body,
        to: item.recipient_email,
        recipient_name: item.recipient_name,
        created_at: item.created_at,
        model: item.model,
      };
      nb.widgetView = 'draft';
      renderWidget();
      c.rerender();
      c.showToast('Draft loaded into editor');
    }
    return true;
  }
  const copyHistBtn = t.closest('[data-history-copy]');
  if (copyHistBtn) {
    const id = Number(copyHistBtn.dataset.historyCopy);
    const item = nb.history.find(h => h.id === id);
    if (item) {
      const txt = `${item.subject}\n\n${item.body}`;
      try {
        await navigator.clipboard.writeText(txt);
        c.showToast('History draft copied to clipboard');
      } catch {
        c.showToast('Copy failed — select the text and press Ctrl+C');
      }
    }
    return true;
  }
  return false;
}

function handleWidgetInput(e) {
  if (!nb.draft) return;
  if (e.target.id === 'dlWidgetSubject') nb.draft.subject = e.target.value;
  if (e.target.id === 'dlWidgetBody') nb.draft.body = e.target.value;
}

export async function nbaClick(e) {
  const t = e.target;
  const d = c.getDeal();
  if (!d || !d.next_action) return false;
  const a = d.next_action;

  if (t.closest('[data-nba-fix]')) {
    if (a.can_draft) {
      if (a.tab) c.openTab(a.tab);
      if (!nb.draft) {
        await triggerDraft();
      } else {
        openEmailWidget('draft');
      }
      return true;
    }
    if (a.fix) c.runGapAction(a.fix);
    else if (a.tab) c.openTab(a.tab);
    return true;
  }

  if (t.closest('[data-nba-draft]')) {
    await triggerDraft();
    return true;
  }

  if (t.closest('[data-nba-history]')) {
    await loadHistory();
    return true;
  }

  if (t.closest('[data-nba-open-widget]')) {
    openEmailWidget('draft');
    return true;
  }

  return false;
}

export function nbaInput(e) {
  handleWidgetInput(e);
}
