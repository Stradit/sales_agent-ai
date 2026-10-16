// Left-column account navigator, shared by every non-index.html page that
// includes partials/nav.html (Command Center, the Tasks page) — reuses the
// exact same markup/CSS as the Global Accounts Dashboard so it looks and
// feels identical, but is intentionally NOT wired to nav-tree.js/selection.js:
// those render account detail inline into #dashContent/#dashPeople, which
// these pages don't have. Here, picking an account just deep-links to the
// Global Accounts Dashboard for that account (index.html's main.js already
// supports ?account_key=<key>), so this stays self-contained instead of
// re-hosting the whole dashboard's render pipeline.
import { loadRealAccounts } from './real-accounts.js';
import { getCurrentUser } from '../auth-client.js';

function esc(s) {
  const d = document.createElement('div');
  d.textContent = (s == null ? '' : String(s));
  return d.innerHTML;
}
function initials(name) {
  return (name || '?').split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
}

let accounts = [];
let searchQuery = '';
let sortMode = 'score';

function el(id) { return document.getElementById(id); }

function applyFilters() {
  const q = searchQuery.trim().toLowerCase();
  let list = accounts.filter(a => {
    if (!q) return true;
    const text = `${a.name || ''} ${a.ticker || ''} ${a.legal_name || ''} ${(a.industries || []).join(' ')}`.toLowerCase();
    return text.includes(q);
  });
  list = [...list].sort((a, b) => {
    if (sortMode === 'name') return (a.name || '').localeCompare(b.name || '');
    if (sortMode === 'contacts') {
      const cA = a.total_contacts_captured || (a.personas || []).length || 0;
      const cB = b.total_contacts_captured || (b.personas || []).length || 0;
      return cB - cA;
    }
    if (sortMode === 'recent') return new Date(b.extracted_at || 0) - new Date(a.extracted_at || 0);
    return (b.heat_score || 0) - (a.heat_score || 0); // 'score' (default)
  });
  return list;
}

function goToAccount(account) {
  const key = account.key || account.ticker || account.name;
  window.location.href = `/?account_key=${encodeURIComponent(key)}`;
}

function cardHtml(a) {
  const score = a.heat_score;
  const scoreClass = score >= 70 ? 'high' : (score >= 40 ? 'mid' : 'none');
  const scoreLabel = score != null ? `${score} SCORE` : '— SCORE';
  const contactsCount = a.total_contacts_captured || (a.personas || []).length || 0;
  const subtitle = [a.ticker ? `Ticker: ${a.ticker}` : '', (a.industries || [])[0] || ''].filter(Boolean).join(' · ') || (a.location || 'Enterprise');
  return `
    <div class="nav-account">
      <div class="nav-account-card" data-acct="${a.id}">
        <div class="nav-account-top">
          <span class="nav-account-avatar">${esc(initials(a.name))}</span>
          <span class="nav-account-name" title="${esc(a.name)}">${esc(a.name)}</span>
          <span class="nav-score-badge ${scoreClass}">${esc(scoreLabel)}</span>
        </div>
        <div class="nav-account-sub">${esc(subtitle)}</div>
        ${contactsCount ? `<div class="nav-account-tags"><span class="nav-micro-tag"><i class="fa-solid fa-users"></i> ${contactsCount} contacts</span></div>` : ''}
      </div>
    </div>`;
}

function render() {
  const list = applyFilters();
  const countEl = el('navAccountCount');
  if (countEl) countEl.textContent = `Accounts (${list.length})`;
  const tree = el('navTree');
  if (!tree) return;
  if (!list.length) {
    tree.innerHTML = '<div class="nav-empty"><i class="fa-solid fa-magnifying-glass" style="font-size:1.4rem;"></i>No accounts match the current search.</div>';
    return;
  }
  tree.innerHTML = list.map(cardHtml).join('');
  tree.querySelectorAll('.nav-account-card').forEach(card => {
    card.addEventListener('click', () => {
      const account = accounts.find(a => a.id === Number(card.dataset.acct));
      if (account) goToAccount(account);
    });
  });
}

function markCurrentPage() {
  const here = document.querySelector(`.nav-digest-wrap a[href="${window.location.pathname}"]`);
  if (here) here.classList.add('active');
}

// The Global Accounts Dashboard is admin-granted, not default access.
// If the user has global dashboard access, point to / (Global Overview).
// If the user lacks global dashboard access but HAS assigned accounts,
// point to their assigned accounts directly (e.g. /?account_key=...).
// Only hide the link if they have neither.
function updateDashboardLinkAccess() {
  const dashBtn = el('navDigestBtn');
  if (!dashBtn) return;
  const user = getCurrentUser();
  const hasAccounts = accounts.length > 0;
  const hasDashAccess = user && (user.role === 'super_admin' || user.has_dashboard_access !== false);

  if (!hasDashAccess && !hasAccounts) {
    dashBtn.style.display = 'none';
    return;
  }

  if (!hasDashAccess && hasAccounts) {
    const titleEl = dashBtn.querySelector('.nav-digest-title');
    const subEl = dashBtn.querySelector('.nav-digest-sub');
    if (titleEl) titleEl.textContent = 'Company Accounts';
    if (subEl) subEl.textContent = 'View assigned account dossiers';
    dashBtn.addEventListener('click', () => {
      const first = accounts[0];
      const key = first.key || first.ticker || first.name;
      window.location.href = `/?account_key=${encodeURIComponent(key)}`;
    });
    return;
  }

  dashBtn.addEventListener('click', () => { window.location.href = '/accounts'; });
}

// Sales Command Center is open by default, but a super_admin can revoke it per user
function updateCommandCenterLinkAccess() {
  const ccLink = document.querySelector('.nav-digest-wrap a[href="/command-center"]');
  if (!ccLink) return;
  const user = getCurrentUser();
  if (!user || (user.role !== 'super_admin' && user.has_command_center_access === false)) ccLink.style.display = 'none';
}

function updateTasksLinkAccess() {
  const tasksLink = document.querySelector('.nav-digest-wrap a[href="/tasks"]');
  if (!tasksLink) return;
  const user = getCurrentUser();
  if (!user || (user.role !== 'super_admin' && user.has_tasks_access === false)) tasksLink.style.display = 'none';
}

export async function initAccountsNav() {
  markCurrentPage();
  updateCommandCenterLinkAccess();
  updateTasksLinkAccess();
  // LOB drill-down isn't relevant here — this navigator only switches
  // accounts, so hide the expand/collapse-all control nav-tree.js otherwise offers.
  const toggleAllBtn = el('navToggleAllTree');
  if (toggleAllBtn) toggleAllBtn.style.display = 'none';

  const collapseBtn = el('navCollapseBtn');
  const dashNav = el('dashNav');
  const collapseIcon = el('navCollapseIcon');
  if (collapseBtn && dashNav) {
    collapseBtn.addEventListener('click', () => {
      const collapsed = dashNav.classList.toggle('is-collapsed');
      if (collapseIcon) collapseIcon.className = collapsed ? 'fa-solid fa-table-columns' : 'fa-solid fa-table-columns';
      collapseBtn.setAttribute('title', collapsed ? 'Expand navigator' : 'Collapse navigator');
    });
  }

  const searchInput = el('navSearch');
  const searchClear = el('navSearchClear');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      searchQuery = searchInput.value || '';
      if (searchClear) searchClear.classList.toggle('d-none', !searchQuery.trim());
      render();
    });
  }
  if (searchClear) {
    searchClear.addEventListener('click', () => {
      searchQuery = '';
      if (searchInput) searchInput.value = '';
      searchClear.classList.add('d-none');
      render();
    });
  }
  const sortSelect = el('navSortSelect');
  if (sortSelect) {
    sortSelect.addEventListener('change', () => {
      sortMode = sortSelect.value || 'score';
      render();
    });
  }

  const tree = el('navTree');
  try {
    accounts = await loadRealAccounts();
    render();
  } catch (err) {
    console.error(err);
    if (tree) tree.innerHTML = '<div class="nav-empty">Error loading accounts. Ensure the API is running.</div>';
  }
  updateDashboardLinkAccess();
}
