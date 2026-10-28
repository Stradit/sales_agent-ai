import { state } from './state.js';
import {
  navSearch, navSearchClear, filterCountAll, navTree, navFilters,
  navCollapseBtn, dashNav, navCollapseIcon, navSortSelect, navToggleAllTree, navAccountCount
} from './dom.js';
import { esc, initials } from './utils.js';
import { getAccountContentEntries, matchOfferings } from './alerts.js';
import { renderSelection } from './selection.js';

export function renderNavTree() {
  const q = (navSearch.value || '').trim().toLowerCase();
  if (navSearchClear) navSearchClear.classList.toggle('d-none', !q);

  const visibleAccounts = state.accounts;

  if (filterCountAll) filterCountAll.textContent = visibleAccounts.length;

  let filtered = visibleAccounts.filter(a => {
    // Search matching
    if (q) {
      const text = `${a.name || ''} ${a.ticker || ''} ${a.legal_name || ''} ${(a.industries || []).join(' ')}`.toLowerCase();
      if (!text.includes(q)) return false;
    }

    // Quick filter tabs
    if (state.navFilter === 'high_score') {
      return (a.heat_score || 0) >= 70;
    }
    if (state.navFilter === 'signal_ready') {
      const entries = getAccountContentEntries(a);
      const matches = matchOfferings(entries);
      return (a.signals_count || 0) > 0 || matches.length > 0;
    }
    if (state.navFilter === 'deep_org') {
      const contacts = a.total_contacts_captured || (a.personas || []).length || 0;
      return contacts >= 10;
    }
    if (state.navFilter === 'multi_lob') {
      return (a.lobs || []).length > 1;
    }
    return true;
  });

  // Sorting
  filtered.sort((a, b) => {
    if (state.navSort === 'score') return (b.heat_score || 0) - (a.heat_score || 0);
    if (state.navSort === 'name') return (a.name || '').localeCompare(b.name || '');
    if (state.navSort === 'contacts') {
      const cA = a.total_contacts_captured || (a.personas || []).length || 0;
      const cB = b.total_contacts_captured || (b.personas || []).length || 0;
      return cB - cA;
    }
    if (state.navSort === 'recent') {
      return new Date(b.extracted_at || 0) - new Date(a.extracted_at || 0);
    }
    return 0;
  });

  if (navAccountCount) {
    navAccountCount.textContent = `Accounts (${filtered.length})`;
  }

  if (!filtered.length) {
    navTree.innerHTML = '<div class="nav-empty"><i class="fa-solid fa-magnifying-glass" style="font-size:1.4rem;"></i>No accounts match the current filter or search.</div>';
    return;
  }

  navTree.innerHTML = filtered.map(a => {
    const isOpen = state.expandedAccountIds.has(a.id);
    const isActive = state.activeAccountId === a.id && !state.activeLobId;
    const lobs = a.lobs || [];
    const score = a.heat_score;
    const scoreClass = score >= 70 ? 'high' : (score >= 40 ? 'mid' : 'none');
    const scoreLabel = score != null ? `${score} SCORE` : '— SCORE';
    const contactsCount = a.total_contacts_captured || (a.personas || []).length || 0;
    const signalsCount = a.signals_count || 0;
    const subtitle = [a.ticker ? `Ticker: ${a.ticker}` : '', (a.industries || [])[0] || ''].filter(Boolean).join(' · ') || (a.location || 'Enterprise');

    const lobsHtml = isOpen ? `
      <div class="nav-lobs">
        ${lobs.map(l => {
          const isLobActive = state.activeAccountId === a.id && state.activeLobId === l.id;
          const lobContacts = (l.personas || []).length;
          return `
            <button type="button" class="nav-lob-card ${isLobActive ? 'active' : ''}" data-acct="${a.id}" data-lob="${l.id}" title="View ${esc(l.name)} division">
              <span class="nav-lob-title"><i class="fa-solid fa-folder"></i> ${esc(l.name)}</span>
              ${lobContacts ? `<span class="nav-lob-badge">${lobContacts}</span>` : ''}
            </button>
            ${(l.subLobs || []).length ? `<div class="nav-sublobs">${(l.subLobs || []).map(s => `<div class="nav-sublob-row">${esc(s.name)}</div>`).join('')}</div>` : ''}
          `;
        }).join('') || '<div class="nav-sublob-row">No lines of business</div>'}
      </div>` : '';

    return `
      <div class="nav-account">
        <div class="nav-account-card ${isActive ? 'active' : ''}" data-acct="${a.id}">
          <div class="nav-account-top">
            <span class="nav-account-avatar">${esc(initials(a.name))}</span>
            <span class="nav-account-name" title="${esc(a.name)}">${esc(a.name)}</span>
            <span class="nav-score-badge ${scoreClass}">${esc(scoreLabel)}</span>
            ${lobs.length ? `
              <button type="button" class="nav-tree-toggle ${isOpen ? 'open' : ''}" data-toggle-acct="${a.id}" title="Toggle divisions">
                <i class="fa-solid fa-chevron-right"></i>
              </button>
            ` : ''}
          </div>

          <div class="nav-account-sub">${esc(subtitle)}</div>

          <div class="nav-account-tags">
            ${signalsCount ? `<span class="nav-micro-tag"><i class="fa-solid fa-bolt text-warning"></i> ${signalsCount}</span>` : ''}
            ${contactsCount ? `<span class="nav-micro-tag"><i class="fa-solid fa-users"></i> ${contactsCount} contacts</span>` : ''}
            ${lobs.length ? `<span class="nav-micro-tag"><i class="fa-solid fa-diagram-project"></i> ${lobs.length} LOB${lobs.length !== 1 ? 's' : ''}</span>` : ''}
          </div>
        </div>
        ${lobsHtml}
      </div>`;
  }).join('');
}

// ── Left Column Event Listeners ───────────────────────────────
if (navSearch) {
  navSearch.addEventListener('input', renderNavTree);
}
if (navSearchClear) {
  navSearchClear.addEventListener('click', function () {
    navSearch.value = '';
    navSearch.focus();
    renderNavTree();
  });
}

if (navCollapseBtn && dashNav) {
  navCollapseBtn.addEventListener('click', function () {
    const isCollapsed = dashNav.classList.toggle('is-collapsed');
    if (navCollapseIcon) {
      navCollapseIcon.className = isCollapsed ? 'fa-solid fa-table-columns' : 'fa-solid fa-table-columns';
    }
    navCollapseBtn.setAttribute('title', isCollapsed ? 'Expand navigator' : 'Collapse navigator');
  });
}

if (navFilters) {
  navFilters.addEventListener('click', function (e) {
    const pill = e.target.closest('.nav-filter-pill');
    if (!pill) return;
    navFilters.querySelectorAll('.nav-filter-pill').forEach(p => p.classList.remove('active'));
    pill.classList.add('active');
    state.navFilter = pill.dataset.filter || 'all';
    renderNavTree();
  });
}

if (navSortSelect) {
  navSortSelect.addEventListener('change', function () {
    state.navSort = navSortSelect.value || 'score';
    renderNavTree();
  });
}

if (navToggleAllTree) {
  navToggleAllTree.addEventListener('click', function () {
    if (state.expandedAccountIds.size > 0) {
      state.expandedAccountIds.clear();
    } else {
      state.accounts.forEach(a => { if ((a.lobs || []).length) state.expandedAccountIds.add(a.id); });
    }
    renderNavTree();
  });
}

navTree.addEventListener('click', function (e) {
  const toggleBtn = e.target.closest('[data-toggle-acct]');
  if (toggleBtn) {
    e.stopPropagation();
    const id = Number(toggleBtn.dataset.toggleAcct);
    if (state.expandedAccountIds.has(id)) state.expandedAccountIds.delete(id);
    else state.expandedAccountIds.add(id);
    renderNavTree();
    return;
  }

  const lobBtn = e.target.closest('.nav-lob-card');
  if (lobBtn) {
    state.activeView = null;
    state.activeAccountId = Number(lobBtn.dataset.acct);
    state.activeLobId = Number(lobBtn.dataset.lob);
    state.expandedAccountIds.add(state.activeAccountId);
    renderNavTree();
    renderSelection();
    return;
  }

  const acctCard = e.target.closest('.nav-account-card');
  if (acctCard) {
    const id = Number(acctCard.dataset.acct);
    state.expandedAccountIds.add(id);
    state.activeView = null;
    state.activeAccountId = id;
    state.activeLobId = null;
    renderNavTree();
    renderSelection();
  }
});
