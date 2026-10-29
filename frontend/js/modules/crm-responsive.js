// Phone and tablet behaviour shared by Deals, Introductions and Forecast. The layout itself is
// deals.css "Phone & tablet" (plus introductions.css / forecast.css); desktop is unchanged.
//  - Filters: below 1024px the header's secondary controls (.dl-tool-extra) fold behind one button
//  - Stage tabs: a board's columns get a tab strip; phones show one column at a time, tablets jump to it
//  - Panel tabs: side-by-side chart panels (.fc-charts) become tabs on phones
//  - Table labels: every cell carries its column name, so tables can stack as cards on phones

const phone = matchMedia('(max-width: 640px)');
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function initFilters(tools) {
  const btn = tools.querySelector('.dl-filters-btn');
  if (!btn) return;
  const extras = [...tools.querySelectorAll('.dl-tool-extra')];
  // Count only filters that are set (a select with a value, a ticked checkbox), not action buttons
  const sync = () => {
    const n = extras.filter((el) => (el.matches('select') && el.value) || el.matches('input:checked')).length;
    btn.querySelector('[data-filter-count]').textContent = n ? ` · ${n}` : '';
  };
  btn.addEventListener('click', () => {
    const open = tools.classList.toggle('is-open');
    btn.setAttribute('aria-expanded', String(open));
    sync();
  });
  tools.addEventListener('change', sync);
  // Pages fill the selects (and restore saved values) after load
  extras.filter((el) => el.matches('select')).forEach((s) => new MutationObserver(sync).observe(s, { childList: true }));
  sync();
}

function initBoardTabs(board) {
  const keyOf = (col) => col.dataset.stage || col.dataset.status;
  const bar = document.createElement('div');
  bar.className = 'dl-stagebar';
  bar.setAttribute('role', 'tablist');
  bar.setAttribute('aria-label', 'Stages');
  board.before(bar);
  board.classList.add('has-tabs');
  let active = null;

  const render = () => {
    const cols = [...board.children].filter((c) => c.classList.contains('dl-col'));
    bar.hidden = !cols.length;
    if (!cols.length) return;
    // Keep the chosen stage across re-renders; otherwise start on the first stage that has cards
    if (!cols.some((c) => keyOf(c) === active)) active = keyOf(cols.find((c) => c.querySelector('.dl-card')) || cols[0]);
    cols.forEach((c) => c.classList.toggle('is-active', keyOf(c) === active));
    bar.innerHTML = cols.map((c) => {
      const k = keyOf(c);
      const count = c.querySelector('.dl-count') ? c.querySelector('.dl-count').textContent : c.querySelectorAll('.dl-card').length;
      const meta = c.querySelector('.dl-col-meta') ? c.querySelector('.dl-col-meta').textContent : '';
      return `<button type="button" role="tab" class="dl-stagetab" data-key="${esc(k)}" aria-selected="${k === active}">
        <span>${esc(c.getAttribute('aria-label') || k)} · ${esc(count)}</span>${meta ? `<small>${esc(meta)}</small>` : ''}</button>`;
    }).join('');
  };

  bar.addEventListener('click', (e) => {
    const b = e.target.closest('[data-key]');
    if (!b) return;
    active = b.dataset.key;
    render();
    const col = [...board.children].find((c) => keyOf(c) === active);
    if (!phone.matches && col) col.scrollIntoView({ behavior: 'smooth', inline: 'start', block: 'nearest' });
    const tab = bar.querySelector('[aria-selected="true"]');
    if (tab) tab.scrollIntoView({ inline: 'nearest', block: 'nearest' });
  });
  new MutationObserver(render).observe(board, { childList: true });
  render();
}

function initPanelTabs(container) {
  const panels = [...container.children].filter((p) => p.classList.contains('fc-panel'));
  if (panels.length < 2) return;
  const bar = document.createElement('div');
  bar.className = 'dl-paneltabs';
  bar.setAttribute('role', 'tablist');
  bar.setAttribute('aria-label', 'Charts');
  bar.innerHTML = panels.map((p, i) => {
    const h = p.querySelector('h2');
    return `<button type="button" role="tab" data-panel="${i}" aria-selected="${i === 0}">${esc(h ? h.textContent.trim() : `Chart ${i + 1}`)}</button>`;
  }).join('');
  container.before(bar);
  container.classList.add('has-tabs');
  const show = (i) => {
    panels.forEach((p, n) => p.classList.toggle('is-active', n === i));
    bar.querySelectorAll('[data-panel]').forEach((b) => b.setAttribute('aria-selected', String(Number(b.dataset.panel) === i)));
  };
  bar.addEventListener('click', (e) => { const b = e.target.closest('[data-panel]'); if (b) show(Number(b.dataset.panel)); });
  show(0);
}

function labelTables(root) {
  const apply = () => root.querySelectorAll('table').forEach((t) => {
    const heads = [...t.querySelectorAll('thead th')].map((th) => th.textContent.trim());
    t.querySelectorAll('tbody tr, tfoot tr').forEach((tr) => [...tr.children].forEach((cell, i) => {
      if (heads[i] && !cell.hasAttribute('data-label')) cell.setAttribute('data-label', heads[i]);
    }));
  });
  new MutationObserver(apply).observe(root, { childList: true, subtree: true });
  apply();
}

function init() {
  document.querySelectorAll('.dl-tools').forEach(initFilters);
  ['dlBoard', 'inBoard'].forEach((id) => { const b = document.getElementById(id); if (b) initBoardTabs(b); });
  document.querySelectorAll('.fc-charts').forEach(initPanelTabs);
  document.querySelectorAll('.fc-table-wrap').forEach(labelTables);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
