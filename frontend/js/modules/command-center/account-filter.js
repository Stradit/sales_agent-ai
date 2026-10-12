import { loadRealAccounts } from './real-accounts.js';
import { ccState } from './state.js';
import { esc } from './utils.js';
import { showToast } from '../toast.js';

let filterChangeHandler = null;
let allSortedAccounts = [];

export async function initAccountFilter(onFilterChange) {
  filterChangeHandler = onFilterChange;
  const wrap = document.getElementById('ccAcctFilterWrap');
  const btn = document.getElementById('ccAcctFilterBtn');
  const menu = document.getElementById('ccAcctFilterMenu');
  const filterAllBtn = document.getElementById('ccFilterAllBtn');
  const searchInput = document.getElementById('ccAcctFilterSearch');
  const accountsList = document.getElementById('ccAcctFilterList');

  if (!wrap || !btn || !menu) return;

  const accounts = await loadRealAccounts().catch(() => []);
  allSortedAccounts = [...accounts].sort((a, b) =>
    (a.name || a.display_name || '').localeCompare(b.name || b.display_name || '')
  );

  // Check URL params (?account_id=1,2,3 or ?account=... or ?accounts=...)
  const params = new URLSearchParams(window.location.search);
  const paramVal = params.get('account_id') || params.get('accounts') || params.get('account') || params.get('account_key');

  if (paramVal) {
    const rawIds = paramVal.split(',').map(s => s.trim()).filter(Boolean);
    const matchedIds = [];
    rawIds.forEach(idOrKey => {
      const found = allSortedAccounts.find(a =>
        String(a.id) === idOrKey ||
        a.key === idOrKey ||
        (a.name && a.name.toLowerCase() === idOrKey.toLowerCase())
      );
      if (found) matchedIds.push(found.id);
    });
    if (matchedIds.length > 0) {
      applyAccountSelection(matchedIds, false);
    } else {
      updateFilterUi();
    }
  } else {
    updateFilterUi();
  }

  // Toggle Menu (matching Google News subscribe button logic)
  btn.onclick = (e) => {
    e.stopPropagation();
    const isHidden = menu.style.display === 'none' || !menu.style.display;
    menu.style.display = isHidden ? 'block' : 'none';
    if (isHidden && searchInput) {
      setTimeout(() => searchInput.focus(), 60);
    }
  };

  // Close on outside click
  if (!document.body.dataset.acctFilterListener) {
    document.body.dataset.acctFilterListener = 'true';
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#ccAcctFilterWrap') && !e.target.closest('#ccSelectedAccountTags')) {
        const m = document.getElementById('ccAcctFilterMenu');
        if (m) m.style.display = 'none';
      }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        const m = document.getElementById('ccAcctFilterMenu');
        if (m) m.style.display = 'none';
      }
    });
  }

  // "All Monitored Accounts" Option
  if (filterAllBtn) {
    filterAllBtn.onclick = (e) => {
      e.stopPropagation();
      if (ccState.activeAccountIds.size === 0) return;
      applyAccountSelection([], true);
      showToast('Filter reset to All Monitored Accounts');
    };
  }

  // Search input filter
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      renderAccountsList(searchInput.value);
    });
  }

  // Initial render of accounts list
  renderAccountsList('');
}

function renderAccountsList(query = '') {
  const accountsList = document.getElementById('ccAcctFilterList');
  if (!accountsList) return;

  const q = (query || '').trim().toLowerCase();
  const filtered = q
    ? allSortedAccounts.filter(a => {
        const name = (a.name || a.display_name || '').toLowerCase();
        const ticker = (a.ticker || '').toLowerCase();
        return name.includes(q) || ticker.includes(q);
      })
    : allSortedAccounts;

  if (!filtered.length) {
    accountsList.innerHTML = `<div style="padding:10px 8px;font-size:0.72rem;color:var(--text-muted);text-align:center;">No accounts matching "${esc(query)}"</div>`;
    return;
  }

  accountsList.innerHTML = filtered.map(a => {
    const isSelected = isAccountSelected(a.id);
    const displayName = a.name || a.display_name || 'Account';
    return `
      <button type="button" class="cc-acct-filter-account-item ${isSelected ? 'active' : ''}" data-acct-filter-id="${a.id}">
        <span class="cc-sub-acct-name">${esc(displayName)}${a.ticker ? ` <small style="opacity:0.65;font-weight:normal;">(${esc(a.ticker)})</small>` : ''}</span>
        <span class="cc-acct-filter-check" style="display:${isSelected ? 'inline-flex' : 'none'};"><i class="fa-solid fa-check"></i></span>
      </button>
    `;
  }).join('');

  accountsList.querySelectorAll('[data-acct-filter-id]').forEach(itemBtn => {
    itemBtn.onclick = (e) => {
      e.stopPropagation();
      const rawId = itemBtn.dataset.acctFilterId;
      const id = isNaN(Number(rawId)) ? rawId : Number(rawId);
      toggleAccount(id);
    };
  });
}

function isAccountSelected(id) {
  return ccState.activeAccountIds.has(id) ||
         ccState.activeAccountIds.has(String(id)) ||
         ccState.activeAccountIds.has(Number(id));
}

function toggleAccount(id) {
  const numericId = isNaN(Number(id)) ? id : Number(id);
  let foundKey = null;
  for (const k of ccState.activeAccountIds) {
    if (String(k) === String(id)) {
      foundKey = k;
      break;
    }
  }

  const acct = allSortedAccounts.find(a => String(a.id) === String(id));
  const name = acct ? (acct.name || acct.display_name) : 'Account';

  const newIds = new Set(ccState.activeAccountIds);
  if (foundKey !== null) {
    newIds.delete(foundKey);
    if (newIds.size === 0) {
      showToast(`Removed ${name} filter. Showing All Accounts.`);
    } else {
      showToast(`Removed ${name} filter.`);
    }
  } else {
    newIds.add(numericId);
    showToast(`Filtering by ${name}!`);
  }

  applyAccountSelection(Array.from(newIds), true);
}

export function setGlobalAccountFilter(accountIds) {
  let ids = [];
  if (Array.isArray(accountIds)) {
    ids = accountIds;
  } else if (typeof accountIds === 'string' && accountIds.trim()) {
    ids = accountIds.split(',').map(s => s.trim()).filter(Boolean);
  } else if (accountIds !== null && accountIds !== undefined && accountIds !== '') {
    ids = [accountIds];
  }
  applyAccountSelection(ids, true);
}

export function clearGlobalAccountFilter() {
  applyAccountSelection([], true);
  showToast('Filter reset to All Monitored Accounts');
}

function applyAccountSelection(selectedIds, updateUrl = true) {
  const normalizedIds = (Array.isArray(selectedIds) ? selectedIds : [selectedIds])
    .map(id => (isNaN(Number(id)) ? id : Number(id)))
    .filter(id => id !== null && id !== undefined && id !== '');

  ccState.activeAccountIds = new Set(normalizedIds);

  const selectedObjs = allSortedAccounts.filter(a => {
    return ccState.activeAccountIds.has(a.id) ||
           ccState.activeAccountIds.has(String(a.id)) ||
           ccState.activeAccountIds.has(Number(a.id));
  });

  ccState.selectedAccountObjs = selectedObjs;
  ccState.selectedAccountNames = selectedObjs.map(a => a.name || a.display_name).filter(Boolean);

  if (selectedObjs.length === 1) {
    ccState.activeAccountId = selectedObjs[0].id;
    ccState.selectedAccountName = selectedObjs[0].name || selectedObjs[0].display_name;
    ccState.selectedAccountObj = selectedObjs[0];
  } else if (selectedObjs.length > 1) {
    ccState.activeAccountId = selectedObjs[0].id;
    ccState.selectedAccountName = `${selectedObjs[0].name || selectedObjs[0].display_name} (+${selectedObjs.length - 1})`;
    ccState.selectedAccountObj = selectedObjs[0];
  } else {
    ccState.activeAccountId = null;
    ccState.selectedAccountName = null;
    ccState.selectedAccountObj = null;
  }

  // Update fallback select if present
  const fallbackSelect = document.getElementById('ccGlobalAccountSelect');
  if (fallbackSelect) {
    fallbackSelect.value = selectedObjs.length === 1 ? String(selectedObjs[0].id) : '';
  }

  // Update dropdown button, all option, and tags
  updateFilterUi();

  // Update URL params
  if (updateUrl) {
    const url = new URL(window.location.href);
    if (normalizedIds.length > 0) {
      url.searchParams.set('account_id', normalizedIds.join(','));
      url.searchParams.delete('account');
      url.searchParams.delete('accounts');
      url.searchParams.delete('account_key');
    } else {
      url.searchParams.delete('account_id');
      url.searchParams.delete('account');
      url.searchParams.delete('accounts');
      url.searchParams.delete('account_key');
    }
    history.replaceState(null, '', url.pathname + (url.search ? url.search : ''));
  }

  if (typeof filterChangeHandler === 'function') {
    filterChangeHandler(ccState.activeAccountId, ccState.selectedAccountObj, ccState.activeAccountIds, ccState.selectedAccountObjs);
  }
}

function updateFilterUi() {
  const btn = document.getElementById('ccAcctFilterBtn');
  const filterAllBtn = document.getElementById('ccFilterAllBtn');
  const filterAllCheck = document.getElementById('ccFilterAllCheck');
  const tagsContainer = document.getElementById('ccSelectedAccountTags');
  const accountsList = document.getElementById('ccAcctFilterList');

  const count = ccState.activeAccountIds.size;
  const objs = ccState.selectedAccountObjs || [];

  // Update Main Trigger Button (matching Google News subscribe button pattern)
  if (btn) {
    if (count === 0) {
      btn.innerHTML = `<i class="fa-solid fa-building"></i> <span id="ccAcctFilterBtnText">All Accounts</span> <i class="fa-solid fa-chevron-down cc-sub-chevron"></i>`;
      btn.classList.remove('is-filtered');
    } else if (count === 1) {
      const name = objs[0] ? (objs[0].name || objs[0].display_name) : '1 Account';
      btn.innerHTML = `<i class="fa-solid fa-building"></i> <span id="ccAcctFilterBtnText">${esc(name)}</span> <i class="fa-solid fa-chevron-down cc-sub-chevron"></i>`;
      btn.classList.add('is-filtered');
    } else {
      btn.innerHTML = `<i class="fa-solid fa-building"></i> <span id="ccAcctFilterBtnText">Filtered (${count})</span> <i class="fa-solid fa-chevron-down cc-sub-chevron"></i>`;
      btn.classList.add('is-filtered');
    }
  }

  // Update "All Monitored Accounts" Option
  if (filterAllBtn) {
    const isAll = (count === 0);
    filterAllBtn.classList.toggle('active', isAll);
    if (filterAllCheck) {
      filterAllCheck.style.display = isAll ? 'inline-flex' : 'none';
    }
  }

  // Update checkmarks in accounts list
  if (accountsList) {
    accountsList.querySelectorAll('[data-acct-filter-id]').forEach(itemBtn => {
      const rawId = itemBtn.dataset.acctFilterId;
      const isSelected = isAccountSelected(rawId);
      itemBtn.classList.toggle('active', isSelected);
      const check = itemBtn.querySelector('.cc-acct-filter-check');
      if (check) check.style.display = isSelected ? 'inline-flex' : 'none';
    });
  }

  // Update Tag Chips next to the button
  if (tagsContainer) {
    if (count === 0) {
      tagsContainer.innerHTML = '';
    } else {
      tagsContainer.innerHTML = objs.map(a => {
        const name = a.name || a.display_name || 'Account';
        return `
          <span class="cc-account-tag" data-tag-acct-id="${a.id}">
            <i class="fa-solid fa-building" style="font-size:0.65rem;opacity:0.75;"></i>
            <span>${esc(name)}</span>
            ${a.ticker ? `<span style="font-size:0.65rem;opacity:0.8;">(${esc(a.ticker)})</span>` : ''}
            <button type="button" class="cc-account-tag-remove" data-remove-acct-id="${a.id}" title="Remove ${esc(name)} filter" aria-label="Remove ${esc(name)} filter">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </span>
        `;
      }).join('');

      tagsContainer.querySelectorAll('[data-remove-acct-id]').forEach(removeBtn => {
        removeBtn.onclick = (e) => {
          e.stopPropagation();
          const rawId = removeBtn.dataset.removeAcctId;
          const id = isNaN(Number(rawId)) ? rawId : Number(rawId);
          toggleAccount(id);
        };
      });
    }
  }
}
