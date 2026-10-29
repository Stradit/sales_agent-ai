// App navigation for the shared topbar (partials/topbar.html): the tablet-landscape "More" menu, the
// tablet-portrait drawer and the phone tab bar + "More" sheet. Which of them shows at which width is
// shell.css "Responsive topbar"; this file only opens/closes them and marks the current section.

const $ = (id) => document.getElementById(id);

// The section a page belongs to (sub-pages map onto the section they are opened from).
function currentSection() {
  const p = location.pathname.replace(/\/+$/, '') || '/';
  if (p === '/' || p === '/profile') return '/accounts';
  if (p === '/copilot-pipeline') return '/copilot';
  if (p === '/capture') return '/email-sync';
  return p;
}

function markCurrent() {
  const here = currentSection();
  document.querySelectorAll('[data-app-nav]').forEach((a) => {
    if (a.getAttribute('href') === here) a.setAttribute('aria-current', 'page');
  });
  // Phone/tablet: the page's own name next to the logo ("Deals Pipeline | Doshin" → "Deals Pipeline")
  const page = $('topbarPage');
  if (page) page.textContent = document.title.split('|')[0].trim();
}

let opener = null;   // the button that opened the open panel, to return focus on close

function closePanels() {
  ['appDrawer', 'appMoreSheet'].forEach((id) => { const el = $(id); if (el) el.hidden = true; });
  $('appScrim').hidden = true;
  $('appMenuBtn').setAttribute('aria-expanded', 'false');
  $('appMoreBtn').setAttribute('aria-expanded', 'false');
  document.body.classList.remove('app-panel-open');
  if (opener) { opener.focus(); opener = null; }
}

function openPanel(id, btn) {
  closePanels();
  $(id).hidden = false;
  $('appScrim').hidden = false;
  btn.setAttribute('aria-expanded', 'true');
  document.body.classList.add('app-panel-open');
  opener = btn;
  const close = $(id).querySelector('[data-app-close]');
  if (close) close.focus();
}

function closeMoreMenu() {
  $('appNavMoreMenu').hidden = true;
  $('appNavMoreBtn').setAttribute('aria-expanded', 'false');
}

// Mirror the topbar's My Tasks count (topbar-auth.js fills #topbarMyTasksBadge) into the drawer and sheet.
function watchTasksBadge() {
  const sync = () => {
    const src = $('topbarMyTasksBadge');
    const n = src ? parseInt(src.textContent, 10) : NaN;
    document.querySelectorAll('[data-app-tasks-badge]').forEach((b) => {
      b.hidden = !(n > 0);
      b.textContent = n > 0 ? String(n) : '';
    });
  };
  const widget = $('topbarAuthWidget');
  if (widget) new MutationObserver(sync).observe(widget, { childList: true, subtree: true, characterData: true });
  sync();
}

function syncThemeItem() {
  const dark = document.documentElement.getAttribute('data-theme') === 'dark';
  const item = $('appMoreTheme');
  item.setAttribute('aria-pressed', String(dark));
  item.querySelector('i').className = dark ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
  item.querySelector('span').textContent = dark ? 'Light theme' : 'Dark theme';
}

function init() {
  if (!$('appTabbar')) return;
  markCurrent();
  watchTasksBadge();
  syncThemeItem();
  new MutationObserver(syncThemeItem).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  $('appMenuBtn').addEventListener('click', (e) => openPanel('appDrawer', e.currentTarget));
  $('appMoreBtn').addEventListener('click', (e) => openPanel('appMoreSheet', e.currentTarget));
  $('appScrim').addEventListener('click', closePanels);
  document.querySelectorAll('[data-app-close]').forEach((b) => b.addEventListener('click', closePanels));
  // The sheet's settings reuse the topbar's own controls (hidden on phones), so their behaviour stays in one place.
  $('appMoreTheme').addEventListener('click', () => { const t = $('themeToggle'); if (t) t.click(); });
  $('appMoreTour').addEventListener('click', () => { closePanels(); const t = $('topbarTourBtn'); if (t) t.click(); });

  $('appNavMoreBtn').addEventListener('click', (e) => {
    e.stopPropagation();
    const open = $('appNavMoreMenu').hidden;
    $('appNavMoreMenu').hidden = !open;
    $('appNavMoreBtn').setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', (e) => { if (!e.target.closest('.topbar-nav-more')) closeMoreMenu(); });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    closeMoreMenu();
    if (!$('appScrim').hidden) closePanels();
  });
  // Rotating a tablet or resizing past a breakpoint must not leave a drawer for another layout open
  matchMedia('(max-width: 640px)').addEventListener('change', closePanels);
  matchMedia('(max-width: 1023px)').addEventListener('change', closePanels);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
