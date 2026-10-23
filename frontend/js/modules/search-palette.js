// Global search palette — Ctrl/Cmd+F (a second Ctrl+F falls through to the browser's
// own find; Ctrl+K stays with the copilot dock). Started on every page by
// topbar-auth.js once someone is signed in; a page can call initSearchPalette(opts)
// again to open results in place (the account page does). Type-ahead comes from GET /api/search/suggest (plain
// Postgres, fast, typo-tolerant); phrase-like queries also get "Related content" from
// GET /api/search/semantic (copilot's vector + full-text retrieval, no LLM call).
// Each palette session (open → pick or close) sends one POST /api/search/log so
// /api/search/misses can show what people looked for and didn't find.
// See apps/sales_search/api.py.

const RECENT_KEY = 'searchPalette.recent';
const RECENT_MAX = 6;
const SUGGEST_DELAY = 150;
const SEMANTIC_DELAY = 450;

const TYPE_ICONS = {
  account: 'fa-building',
  persona: 'fa-user',
  lob: 'fa-sitemap',
  signal: 'fa-bolt',
  deal: 'fa-handshake',
  task: 'fa-list-check',
  doc: 'fa-file-lines',
  copilot: 'fa-wand-magic-sparkles',
  semantic: 'fa-magnifying-glass',
};

const DOC_TYPE_LABELS = {
  persona_card: 'Profile', callprep: 'Call prep', personality_profile: 'Personality',
  digest_channel: 'Digest', news: 'News', blog: 'Blog', post: 'Post', job: 'Job',
};

const MOD = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function loadRecent() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]'); } catch { return []; }
}

function saveRecent(item) {
  if (!['account', 'persona', 'lob', 'signal', 'deal', 'task'].includes(item.type)) return;
  try {
    const keep = { type: item.type, id: item.id, account_id: item.account_id, lob_id: item.lob_id, title: item.title, subtitle: item.subtitle };
    const list = [keep, ...loadRecent().filter(r => !(r.type === item.type && r.id === item.id))].slice(0, RECENT_MAX);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  } catch { /* storage unavailable — recents are a convenience */ }
}

// Where a result goes when the page doesn't handle it in place.
export function resultUrl(item) {
  switch (item.type) {
    case 'account': return `/?account=${item.id}`;
    case 'persona': return `/?account=${item.account_id}&tab=committee&persona=${item.id}`;
    case 'lob': return `/?account=${item.account_id}&lob=${item.id}`;
    case 'signal': return `/?account=${item.account_id}&tab=alerts`;
    case 'deal': return `/deals?deal=${item.id}`;
    case 'task': return '/tasks';
    case 'doc': return item.url || (item.account_id ? `/?account=${item.account_id}` : null);
    case 'copilot': {
      const p = new URLSearchParams({ q: item.q });
      if (item.account_id) p.set('account_id', item.account_id);
      return `/copilot?${p}`;
    }
    default: return null;
  }
}

function navigateTo(item) {
  const url = resultUrl(item);
  if (!url) return;
  if (item.type === 'doc' && item.url) window.open(url, '_blank', 'noopener');
  else window.location.href = url;
}

const STYLESHEET = '/css/search-palette.css?v=1.2';
const config = { getAccountId: () => null, open: null };

/**
 * Creates the palette once; later calls only update the options.
 * @param {object}   opts
 * @param {function} [opts.getAccountId]  current account id (boosts its results; scopes "Ask Copilot")
 * @param {function} [opts.open]          async (item) => boolean; return true if handled in-page
 */
export function initSearchPalette(opts = {}) {
  Object.assign(config, opts);
  if (document.getElementById('searchPalette')) return;
  const getAccountId = () => config.getAccountId();

  if (!document.querySelector('link[data-search-palette]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = STYLESHEET;
    link.dataset.searchPalette = '';
    document.head.appendChild(link);
  }

  const root = document.createElement('div');
  root.id = 'searchPalette';
  root.className = 'sp-backdrop d-none';
  root.innerHTML = `
    <div class="sp-panel" role="dialog" aria-modal="true" aria-label="Search">
      <div class="sp-input-row">
        <i class="fa-solid fa-magnifying-glass sp-input-icon"></i>
        <input id="spInput" class="sp-input" type="text" autocomplete="off" spellcheck="false"
               placeholder="Search accounts, people, business units, signals, deals…"
               role="combobox" aria-expanded="true" aria-controls="spResults" aria-autocomplete="list">
        <span class="sp-spinner d-none" id="spSpinner"></span>
        <kbd class="sp-kbd">Esc</kbd>
      </div>
      <div class="sp-results" id="spResults" role="listbox"></div>
      <div class="sp-footer">
        <span><kbd>↑</kbd><kbd>↓</kbd> navigate</span>
        <span><kbd>Enter</kbd> open</span>
        <span><kbd>${MOD}</kbd><kbd>F</kbd> again for browser find</span>
      </div>
    </div>`;
  document.body.appendChild(root);

  const input = root.querySelector('#spInput');
  const results = root.querySelector('#spResults');
  const spinner = root.querySelector('#spSpinner');

  let items = [];          // flat list of selectable rows, in display order
  let active = 0;
  let suggestCtl = null;
  let semanticCtl = null;
  let suggestTimer = null;
  let semanticTimer = null;
  let lastSuggest = { q: '', groups: [] };
  let semanticState = { q: '', status: 'idle', items: [] };   // idle | loading | done | error
  const cache = new Map();
  let logged = true;       // false while a session has something worth reporting

  // One row per session, sent on pick or close; keepalive survives the navigation a pick causes.
  function logSession(chosenType = null) {
    if (logged) return;
    logged = true;
    const q = input.value.trim();
    if (q.length < 2 || lastSuggest.q !== q) return;
    const nResults = lastSuggest.groups.reduce((n, g) => n + g.items.length, 0);
    fetch('/api/search/log', {
      method: 'POST',
      keepalive: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        q, n_results: nResults, chosen_type: chosenType,
        account_id: getAccountId() || null, semantic: semanticState.q === q && semanticState.status === 'done',
      }),
    }).catch(() => { /* analytics only */ });
  }

  const isOpen = () => !root.classList.contains('d-none');

  function open() {
    if (isOpen()) return;
    root.classList.remove('d-none');
    document.body.classList.add('sp-open');
    input.select();
    input.focus();
    render();
  }

  function close() {
    logSession();
    root.classList.add('d-none');
    document.body.classList.remove('sp-open');
    suggestCtl?.abort();
    semanticCtl?.abort();
  }

  async function choose(item) {
    if (!item) return;
    if (item.type === 'semantic') { runSemantic(item.q, true); return; }
    saveRecent(item);
    logSession(item.type);
    close();
    try {
      if (config.open && await config.open(item)) return;
    } catch (err) { console.error('search open failed', err); }
    navigateTo(item);
  }

  // ── Fetching ───────────────────────────────────────────────
  async function runSuggest(q) {
    const accountId = getAccountId();
    const key = `${accountId || ''}|${q.toLowerCase()}`;
    if (cache.has(key)) { lastSuggest = cache.get(key); afterSuggest(q); return; }
    suggestCtl?.abort();
    suggestCtl = new AbortController();
    spinner.classList.remove('d-none');
    try {
      const params = new URLSearchParams({ q });
      if (accountId) params.set('account_id', accountId);
      const res = await fetch(`/api/search/suggest?${params}`, { signal: suggestCtl.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      cache.set(key, data);
      if (input.value.trim() !== q) return;   // user kept typing
      lastSuggest = data;
      afterSuggest(q);
    } catch (err) {
      if (err.name !== 'AbortError') { lastSuggest = { q, groups: [], error: true }; render(); }
    } finally {
      spinner.classList.add('d-none');
    }
  }

  function afterSuggest(q) {
    active = 0;
    render();
    // Phrases and near-misses are where meaning beats spelling.
    const total = lastSuggest.groups.reduce((n, g) => n + g.items.length, 0);
    clearTimeout(semanticTimer);
    if (q.split(/\s+/).length >= 3 || total < 3) {
      semanticTimer = setTimeout(() => runSemantic(q), SEMANTIC_DELAY);
    }
  }

  async function runSemantic(q, focusFirst = false) {
    if (q.length < 3 || (semanticState.q === q && semanticState.status !== 'error')) return;
    semanticCtl?.abort();
    semanticCtl = new AbortController();
    semanticState = { q, status: 'loading', items: [] };
    render();
    try {
      const params = new URLSearchParams({ q });
      const accountId = getAccountId();
      if (accountId) params.set('account_id', accountId);
      const res = await fetch(`/api/search/semantic?${params}`, { signal: semanticCtl.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (input.value.trim() !== q) return;
      semanticState = { q, status: 'done', items: data.items || [] };
    } catch (err) {
      if (err.name === 'AbortError') return;
      semanticState = { q, status: 'error', items: [] };
    }
    render();
    if (focusFirst) {
      const i = items.findIndex(it => it.type === 'doc');
      if (i >= 0) { active = i; highlight(); }
    }
  }

  // ── Rendering ──────────────────────────────────────────────
  function row(item, idx, extraClass = '') {
    const icon = TYPE_ICONS[item.type] || 'fa-circle';
    return `
      <div class="sp-item ${extraClass}" role="option" id="spItem${idx}" data-idx="${idx}" aria-selected="${idx === active}">
        <span class="sp-item-icon sp-icon-${item.type}"><i class="fa-solid ${icon}"></i></span>
        <span class="sp-item-text">
          <span class="sp-item-title">${esc(item.title)}</span>
          ${item.subtitle ? `<span class="sp-item-sub">${esc(item.subtitle)}</span>` : ''}
        </span>
        ${item.badge ? `<span class="sp-item-badge">${esc(item.badge)}</span>` : ''}
      </div>`;
  }

  function render() {
    const q = input.value.trim();
    items = [];
    let html = '';

    if (q.length < 2) {
      const recent = loadRecent();
      if (recent.length) {
        html += `<div class="sp-group-label">Recent</div>`;
        recent.forEach(r => { html += row(r, items.length); items.push(r); });
      } else {
        html += `<div class="sp-empty">Type at least 2 characters — names, titles, business units, signals, deals…</div>`;
      }
      results.innerHTML = html;
      highlight();
      return;
    }

    const upToDate = lastSuggest.q === q;
    if (upToDate) {
      for (const g of lastSuggest.groups) {
        html += `<div class="sp-group-label">${esc(g.label)}</div>`;
        g.items.forEach(it => { html += row(it, items.length); items.push(it); });
      }
      if (lastSuggest.error) html += `<div class="sp-empty">Search is unavailable right now.</div>`;
      else if (!lastSuggest.groups.length && semanticState.q !== q) html += `<div class="sp-empty">No direct matches for “${esc(q)}”.</div>`;
    }

    // Related content (semantic)
    if (semanticState.q === q && semanticState.status !== 'idle') {
      html += `<div class="sp-group-label">Related content <span class="sp-group-hint">by meaning</span></div>`;
      if (semanticState.status === 'loading') {
        html += `<div class="sp-empty"><span class="sp-spinner"></span> Finding related documents…</div>`;
      } else if (semanticState.status === 'error') {
        html += `<div class="sp-empty">Related content is unavailable right now.</div>`;
      } else if (!semanticState.items.length) {
        html += `<div class="sp-empty">Nothing related found.</div>`;
      } else {
        semanticState.items.forEach(d => {
          const it = {
            type: 'doc', title: d.title, url: d.url, account_id: d.account_id,
            subtitle: (d.snippet || '').replace(/\s+/g, ' ').slice(0, 140),
            badge: [DOC_TYPE_LABELS[d.doc_type] || d.doc_type, d.published_at ? d.published_at.slice(0, 10) : null].filter(Boolean).join(' · '),
          };
          html += row(it, items.length, 'sp-item-doc');
          items.push(it);
        });
      }
    } else if (upToDate) {
      const it = { type: 'semantic', q, title: `Search all content for “${q}”`, subtitle: 'News, posts, call-preps and profiles — matched by meaning' };
      html += row(it, items.length, 'sp-item-action');
      items.push(it);
    }

    if (upToDate || semanticState.q === q) {
      const accountId = getAccountId();
      const it = { type: 'copilot', q, account_id: accountId, title: `Ask Copilot: “${q}”`, subtitle: 'Opens the Sales Copilot with this question ready to send' };
      html += row(it, items.length, 'sp-item-action');
      items.push(it);
    }

    results.innerHTML = html || `<div class="sp-empty"><span class="sp-spinner"></span> Searching…</div>`;
    if (active >= items.length) active = 0;
    highlight();
  }

  function highlight() {
    results.querySelectorAll('.sp-item').forEach(el => {
      const on = Number(el.dataset.idx) === active;
      el.classList.toggle('active', on);
      el.setAttribute('aria-selected', String(on));
      if (on) {
        el.scrollIntoView({ block: 'nearest' });
        input.setAttribute('aria-activedescendant', el.id);
      }
    });
  }

  // ── Events ─────────────────────────────────────────────────
  input.addEventListener('input', () => {
    const q = input.value.trim();
    logged = false;
    clearTimeout(suggestTimer);
    clearTimeout(semanticTimer);
    semanticCtl?.abort();
    if (semanticState.q !== q) semanticState = { q: '', status: 'idle', items: [] };
    if (q.length < 2) { suggestCtl?.abort(); lastSuggest = { q: '', groups: [] }; render(); return; }
    suggestTimer = setTimeout(() => runSuggest(q), SUGGEST_DELAY);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); if (items.length) { active = (active + 1) % items.length; highlight(); } }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (items.length) { active = (active - 1 + items.length) % items.length; highlight(); } }
    else if (e.key === 'Enter') {
      e.preventDefault();
      const q = input.value.trim();
      // Enter before suggestions arrive: search now instead of opening a stale row.
      if (q.length >= 2 && lastSuggest.q !== q) { clearTimeout(suggestTimer); runSuggest(q); return; }
      choose(items[active]);
    }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  });

  results.addEventListener('mousemove', (e) => {
    const el = e.target.closest('.sp-item');
    if (el && Number(el.dataset.idx) !== active) { active = Number(el.dataset.idx); highlight(); }
  });
  results.addEventListener('click', (e) => {
    const el = e.target.closest('.sp-item');
    if (el) choose(items[Number(el.dataset.idx)]);
  });
  root.addEventListener('mousedown', (e) => { if (e.target === root) close(); });

  document.addEventListener('keydown', (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || e.altKey || e.shiftKey) return;
    // First Ctrl+F opens the palette; a second one closes it and lets the browser's find run.
    if (e.key.toLowerCase() !== 'f') return;
    if (isOpen()) { close(); return; }
    e.preventDefault();
    open();
  });

  // Topbar trigger — discoverable entry point for people who don't know the shortcut.
  const actions = document.querySelector('.topbar-actions');
  if (actions && !document.getElementById('spTrigger')) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'spTrigger';
    btn.className = 'topbar-link topbar-link-btn sp-trigger';
    btn.title = `Search (${MOD}+F)`;
    btn.innerHTML = `<i class="fa-solid fa-magnifying-glass"></i><span class="sp-trigger-label">Search</span><kbd class="sp-trigger-kbd">${MOD} F</kbd>`;
    btn.addEventListener('click', open);
    actions.insertBefore(btn, actions.firstChild);
  }

  return { open, close };
}
