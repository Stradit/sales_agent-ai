// Personal, cross-account Task Management page (Clean & Informative)
import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { getCurrentUser } from '../auth-client.js';
import { showToast } from '../toast.js';
import { renderFilterChips } from '../action-items.js';
import { initAccountsNav } from '../command-center/accounts-nav.js';
import { downloadFile } from '../download.js';
import { loadAccountsCached } from '../accounts-cache.js';

function el(id) { return document.getElementById(id); }

function esc(str) {
  if (str == null) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function toDatetimeLocal(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  const YYYY = d.getFullYear();
  const MM = pad(d.getMonth() + 1);
  const DD = pad(d.getDate());
  const hh = pad(d.getHours());
  const mm = pad(d.getMinutes());
  return `${YYYY}-${MM}-${DD}T${hh}:${mm}`;
}

let openEditModalFn = null;
let openCloseModalFn = null;
let openReopenModalFn = null;
let deepLinkOpened = false;

const tp = {
  items: [],
  activeStatus: 'open',
  selectedAccountIds: new Set(),
  accountFilter: '',
  priorityFilter: '',
  searchQuery: '',
  overdueOnly: false,
  sort: 'due',
  accountsCache: [],
  usersCache: [],
};

function initAccountFilterFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const accountsParam = params.get('accounts');
  const accountIdParam = params.get('account_id');
  if (accountsParam) {
    accountsParam.split(',').map(s => s.trim()).filter(Boolean).forEach(id => tp.selectedAccountIds.add(id));
  } else if (accountIdParam) {
    tp.selectedAccountIds.add(accountIdParam.trim());
  }
}

function syncAccountFilterUrl() {
  const url = new URL(window.location.href);
  if (tp.selectedAccountIds.size > 0) {
    url.searchParams.set('accounts', Array.from(tp.selectedAccountIds).join(','));
    url.searchParams.delete('account_id');
  } else {
    url.searchParams.delete('accounts');
    url.searchParams.delete('account_id');
  }
  window.history.replaceState({}, '', url.pathname + url.search + url.hash);
}

function scopedItems() {
  return tp.items.filter(i => {
    if (tp.selectedAccountIds.size > 0) {
      const aid = String(i.account_id ?? '');
      if (!tp.selectedAccountIds.has(aid)) return false;
    } else if (tp.accountFilter && String(i.account_id) !== tp.accountFilter) {
      return false;
    }
    if (tp.priorityFilter && i.priority !== tp.priorityFilter) return false;
    if (tp.overdueOnly && !i.is_overdue) return false;
    if (tp.searchQuery) {
      const q = tp.searchQuery.toLowerCase();
      const title = (i.title || '').toLowerCase();
      const desc = (i.description || '').toLowerCase();
      const acct = (i.account_name || '').toLowerCase();
      const persona = (i.persona_name || (i.persona && i.persona.name) || '').toLowerCase();
      const source = (i.source || '').toLowerCase();
      if (!title.includes(q) && !desc.includes(q) && !acct.includes(q) && !persona.includes(q) && !source.includes(q)) {
        return false;
      }
    }
    return true;
  });
}

function visibleItems() {
  const scoped = scopedItems();
  let filtered;
  if (tp.activeStatus === 'all') {
    filtered = scoped;
  } else if (tp.activeStatus === 'open') {
    filtered = scoped.filter(i => i.status === 'open' || i.status === 'pending_review');
  } else {
    filtered = scoped.filter(i => i.status === tp.activeStatus);
  }
  const priorityRank = { high: 0, medium: 1, low: 2 };
  return [...filtered].sort((a, b) => {
    if (tp.sort === 'priority') return (priorityRank[a.priority] ?? 3) - (priorityRank[b.priority] ?? 3);
    if (tp.sort === 'account') return (a.account_name || '').localeCompare(b.account_name || '');
    if (tp.sort === 'newest') return new Date(b.created_at || 0) - new Date(a.created_at || 0);
    // 'due' (default) — soonest first, no due date last
    if (!a.due_date && !b.due_date) return 0;
    if (!a.due_date) return 1;
    if (!b.due_date) return -1;
    return new Date(a.due_date) - new Date(b.due_date);
  });
}

function updateKpis() {
  const openItems = tp.items.filter(i => i.status === 'open' || i.status === 'in_progress' || i.status === 'pending_review');
  const overdueItems = openItems.filter(i => i.is_overdue);
  const highItems = openItems.filter(i => i.priority === 'high');
  const accountCount = new Set(tp.items.filter(i => i.account_id != null).map(i => i.account_id)).size;

  if (el('tpKpiOpen')) el('tpKpiOpen').textContent = openItems.length;
  if (el('tpKpiOverdue')) el('tpKpiOverdue').textContent = overdueItems.length;
  if (el('tpKpiHigh')) el('tpKpiHigh').textContent = highItems.length;
  if (el('tpKpiAccounts')) el('tpKpiAccounts').textContent = accountCount;

  const subtitle = el('tpSubtitle');
  if (subtitle) {
    subtitle.textContent = `${openItems.length} open across ${accountCount} account${accountCount === 1 ? '' : 's'}${overdueItems.length ? ` · ${overdueItems.length} overdue` : ''}`;
  }
}

function closeAccountMenu() {
  const menu = el('tpAcctFilterMenu');
  const btn = el('tpAcctFilterBtn');
  if (menu) menu.style.display = 'none';
  if (btn) {
    btn.classList.remove('active');
    btn.setAttribute('aria-expanded', 'false');
  }
}

function openAccountMenu() {
  const menu = el('tpAcctFilterMenu');
  const btn = el('tpAcctFilterBtn');
  if (menu) menu.style.display = 'block';
  if (btn) {
    btn.classList.add('active');
    btn.setAttribute('aria-expanded', 'true');
  }
  const searchInput = el('tpAcctFilterSearch');
  if (searchInput) {
    searchInput.value = '';
    filterDropdownAccounts('');
    setTimeout(() => searchInput.focus(), 50);
  }
}

function toggleAccountMenu() {
  const menu = el('tpAcctFilterMenu');
  if (!menu) return;
  if (menu.style.display === 'block') {
    closeAccountMenu();
  } else {
    openAccountMenu();
  }
}

function filterDropdownAccounts(query) {
  const q = (query || '').toLowerCase().trim();
  const items = document.querySelectorAll('#tpAcctFilterList .tp-acct-filter-account-item');
  items.forEach(item => {
    const name = (item.dataset.acctName || '').toLowerCase();
    item.style.display = (!q || name.includes(q)) ? 'flex' : 'none';
  });
}

function populateAccountFilter() {
  const acctMap = new Map();
  tp.items.forEach(i => {
    if (i.account_id != null) {
      const aid = String(i.account_id);
      const name = i.account_name || `Account ${i.account_id}`;
      if (!acctMap.has(aid)) {
        acctMap.set(aid, { id: aid, name, count: 0 });
      }
      acctMap.get(aid).count += 1;
    }
  });

  const sortedAccounts = [...acctMap.values()].sort((a, b) => a.name.localeCompare(b.name));

  // Render accounts in dropdown list
  const listEl = el('tpAcctFilterList');
  if (listEl) {
    if (sortedAccounts.length === 0) {
      listEl.innerHTML = '<div style="padding:10px 12px; font-size:0.75rem; color:var(--text-muted); text-align:center;">No accounts with tasks</div>';
    } else {
      listEl.innerHTML = sortedAccounts.map(acct => {
        const isChecked = tp.selectedAccountIds.has(acct.id);
        return `
          <button type="button" class="tp-acct-filter-account-item ${isChecked ? 'active' : ''}" data-acct-id="${esc(acct.id)}" data-acct-name="${esc(acct.name)}">
            <div class="tp-acct-filter-opt-left">
              <span class="tp-af-checkbox"><i class="fa-solid fa-check"></i></span>
              <span class="tp-af-acct-name" title="${esc(acct.name)}">${esc(acct.name)}</span>
            </div>
            <span class="tp-af-task-count">${acct.count}</span>
          </button>
        `;
      }).join('');
    }
  }

  // Update "All Accounts" option state
  const allBtn = el('tpAcctFilterAllBtn');
  const allCount = el('tpAcctFilterAllCount');
  if (allBtn) {
    if (tp.selectedAccountIds.size === 0) {
      allBtn.classList.add('active');
    } else {
      allBtn.classList.remove('active');
    }
  }
  if (allCount) {
    allCount.textContent = tp.items.length;
  }

  // Update filter trigger button & badge
  const btnText = el('tpAcctFilterBtnText');
  const btnCount = el('tpAcctFilterCount');
  const filterBtn = el('tpAcctFilterBtn');
  const clearBtn = el('tpAcctFilterClearBtn');
  const selectedCount = tp.selectedAccountIds.size;

  if (selectedCount === 0) {
    if (btnText) btnText.textContent = 'All Accounts';
    if (btnCount) btnCount.style.display = 'none';
    if (filterBtn) filterBtn.classList.remove('is-filtered');
    if (clearBtn) clearBtn.style.display = 'none';
  } else {
    if (filterBtn) filterBtn.classList.add('is-filtered');
    if (clearBtn) clearBtn.style.display = 'inline-block';
    if (btnCount) {
      btnCount.textContent = selectedCount;
      btnCount.style.display = 'inline-flex';
    }
    if (btnText) {
      const selectedNames = Array.from(tp.selectedAccountIds)
        .map(id => acctMap.get(id)?.name)
        .filter(Boolean);
      if (selectedCount === 1) {
        btnText.textContent = selectedNames[0] || '1 Account';
      } else if (selectedCount === 2 && selectedNames.length === 2) {
        btnText.textContent = `${selectedNames[0]}, ${selectedNames[1]}`;
      } else {
        btnText.textContent = `${selectedCount} Accounts Selected`;
      }
    }
  }

  // Render active account tag pills
  const tagsContainer = el('tpSelectedAccountTags');
  if (tagsContainer) {
    if (selectedCount === 0) {
      tagsContainer.style.display = 'none';
      tagsContainer.innerHTML = '';
    } else {
      tagsContainer.style.display = 'flex';
      const tagsHtml = `
        <span class="tp-acct-tags-label"><i class="fa-solid fa-filter"></i> Filtering accounts (${selectedCount}):</span>
        ${Array.from(tp.selectedAccountIds).map(id => {
          const name = acctMap.get(id)?.name || `Account #${id}`;
          return `
            <span class="tp-acct-tag">
              <span>${esc(name)}</span>
              <button type="button" class="tp-acct-tag-remove" data-remove-acct-id="${esc(id)}" title="Remove ${esc(name)} filter" aria-label="Remove ${esc(name)}">
                <i class="fa-solid fa-xmark"></i>
              </button>
            </span>
          `;
        }).join('')}
        <button type="button" class="tp-acct-clear-all" id="tpClearAllAcctsBtn">Clear all</button>
      `;
      tagsContainer.innerHTML = tagsHtml;
    }
  }

  // Keep hidden legacy select in sync for any dependents
  const select = el('tpAccountFilter');
  if (select) {
    select.innerHTML = '<option value="">All Accounts</option>' + sortedAccounts
      .map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join('');
    select.value = selectedCount === 1 ? Array.from(tp.selectedAccountIds)[0] : '';
    tp.accountFilter = select.value;
  }
}

function formatDueDate(dueStr, isOverdue) {
  if (!dueStr) return '<span class="tp-tag"><i class="fa-regular fa-calendar"></i> No due date</span>';
  const d = new Date(dueStr);
  if (isNaN(d.getTime())) return `<span class="tp-tag">${esc(dueStr)}</span>`;
  const formatted = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  const cls = isOverdue ? 'overdue' : '';
  const icon = isOverdue ? 'fa-solid fa-circle-exclamation text-red' : 'fa-regular fa-calendar';
  return `<span class="tp-tag tp-tag-due ${cls}"><i class="${icon}"></i> Due ${formatted}${isOverdue ? ' (Overdue)' : ''}</span>`;
}

function formatDescriptionHtml(text) {
  if (!text) return '';
  const escaped = esc(text);
  const urlRegex = /(https?:\/\/[^\s<]+)/g;
  return escaped.replace(urlRegex, (url) => {
    let displayUrl = url;
    try {
      const u = new URL(url);
      const path = u.pathname.length > 25 ? u.pathname.slice(0, 22) + '…' : u.pathname;
      displayUrl = u.hostname + (path !== '/' ? path : '');
    } catch {
      if (url.length > 40) {
        displayUrl = url.slice(0, 37) + '…';
      }
    }
    return `<a href="${url}" target="_blank" rel="noopener noreferrer" class="tp-link" title="${url}"><i class="fa-solid fa-arrow-up-right-from-square"></i> ${displayUrl}</a>`;
  });
}

function extractNotes(desc) {
  if (!desc) return { mainDesc: '', closingNote: null, reopenNote: null };

  let main = desc;
  let closingNote = null;
  let reopenNote = null;

  const rMarker = '\n\nReopen Note: ';
  const rIdx = main.lastIndexOf(rMarker);
  if (rIdx !== -1) {
    reopenNote = main.slice(rIdx + rMarker.length).trim();
    main = main.slice(0, rIdx).trim();
  } else if (main.startsWith('Reopen Note: ')) {
    reopenNote = main.slice(13).trim();
    main = '';
  }

  const cMarker = '\n\nClosing Note: ';
  const altMarker = '\n\n[Closing Note]: ';
  let cIdx = main.lastIndexOf(cMarker);
  let cLen = cMarker.length;
  if (cIdx === -1) {
    cIdx = main.lastIndexOf(altMarker);
    cLen = altMarker.length;
  }
  if (cIdx !== -1) {
    closingNote = main.slice(cIdx + cLen).trim();
    main = main.slice(0, cIdx).trim();
  } else if (main.startsWith('Closing Note: ')) {
    closingNote = main.slice(14).trim();
    main = '';
  } else if (main.startsWith('[Closing Note]: ')) {
    closingNote = main.slice(16).trim();
    main = '';
  }

  return { mainDesc: main, closingNote, reopenNote };
}

function formatSourceTag(source) {
  if (!source) return '';
  const s = String(source).toLowerCase().trim();
  if (s === 'signal_feed' || s === 'priority_signal' || s === 'llm_suggested') {
    return `
      <span class="tp-tag tp-tag-signal" title="Created from Priority Signal Feed">
        <i class="fa-solid fa-bolt"></i> Signal Feed
      </span>`;
  }
  if (s === 'playbook') {
    return `
      <span class="tp-tag tp-tag-playbook" title="Created from This Week's Playbook">
        <i class="fa-solid fa-book-bookmark"></i> Playbook
      </span>`;
  }
  if (s === 'deal' || s === 'deals') {
    return `
      <span class="tp-tag tp-tag-deal" title="Created from Deals Pipeline">
        <i class="fa-solid fa-handshake"></i> Deals
      </span>`;
  }
  if (s === 'hiring' || s === 'hiring_radar' || s === 'hiring_signals') {
    return `
      <span class="tp-tag tp-tag-hiring" title="Created from Hiring Signals">
        <i class="fa-solid fa-user-plus"></i> Hiring Radar
      </span>`;
  }
  if (s === 'dossier' || s === 'account_dossier') {
    return `
      <span class="tp-tag tp-tag-dossier" title="Created from Account Dossier">
        <i class="fa-regular fa-folder-open"></i> Dossier
      </span>`;
  }
  return '';
}

function renderTaskCard(item) {
  const isDone = item.status === 'done';
  const isPending = item.status === 'pending_review';
  const isOverdue = Boolean(item.is_overdue && (item.status === 'open' || item.status === 'in_progress'));
  const isHigh = item.priority === 'high' && !isDone;
  const personaName = item.persona_name || (item.persona && item.persona.name) || '';
  const priorityClass = item.priority === 'high' ? 'tp-tag-priority-high' : item.priority === 'low' ? 'tp-tag-priority-low' : 'tp-tag-priority-med';
  const priorityLabel = item.priority === 'high' ? 'High' : item.priority === 'low' ? 'Low' : 'Medium';

  const { mainDesc, closingNote, reopenNote } = extractNotes(item.description);
  const hasLongDesc = Boolean(mainDesc && mainDesc.length > 130);

  return `
    <div class="tp-task-item ${isDone ? 'is-done' : ''} ${isOverdue ? 'is-overdue' : ''} ${isHigh ? 'is-high' : ''} ${isPending ? 'is-pending' : ''}" data-task-id="${item.id}">
      <div class="tp-task-top">
        <div class="tp-task-title-wrap">
          <span class="tp-task-id-badge" title="Task ID #${item.id}">#${item.id}</span>
          <div class="tp-task-text-wrap">
            <h3 class="tp-task-title ${isDone ? 'text-muted' : ''}">${esc(item.title)}</h3>
            ${mainDesc ? `
              <div class="tp-task-desc-wrap">
                <div class="tp-task-desc ${isDone ? 'text-muted' : ''} ${hasLongDesc ? 'is-clamped' : ''}">
                  ${formatDescriptionHtml(mainDesc)}
                </div>
                ${hasLongDesc ? `
                  <button type="button" class="tp-desc-toggle-btn" data-action="toggle-desc" title="Expand or collapse full description">
                    <span>Show more</span> <i class="fa-solid fa-chevron-down"></i>
                  </button>
                ` : ''}
              </div>
            ` : ''}
            ${isDone && closingNote ? `
              <div class="tp-closing-comment" title="Resolution comment entered when closing ticket">
                <i class="fa-solid fa-clipboard-check"></i>
                <div class="tp-closing-comment-body">
                  <span class="tp-closing-comment-label">Closing Reason / Comment</span>
                  <span class="tp-closing-comment-text">${formatDescriptionHtml(closingNote)}</span>
                </div>
              </div>
            ` : ''}
            ${!isDone && reopenNote ? `
              <div class="tp-reopen-comment" title="Reason entered when ticket was reopened">
                <i class="fa-solid fa-rotate-left"></i>
                <div class="tp-reopen-comment-body">
                  <span class="tp-reopen-comment-label">Reopen Reason</span>
                  <span class="tp-reopen-comment-text">${formatDescriptionHtml(reopenNote)}</span>
                </div>
              </div>
            ` : ''}
          </div>
        </div>
        <div class="tp-task-actions">
          ${isPending ? `
            <button type="button" class="tp-btn-approve" data-action="approve" data-item-id="${item.id}" title="Approve signal-suggested task">
              <i class="fa-solid fa-check"></i> Accept Task
            </button>
            <button type="button" class="tp-btn-dismiss" data-action="reject" data-item-id="${item.id}" title="Dismiss signal-suggested task">
              <i class="fa-solid fa-xmark"></i> Dismiss
            </button>
          ` : `
            <select class="tp-select" data-action="set-status" data-item-id="${item.id}" ${isDone ? 'disabled' : ''} title="${isDone ? 'Ticket is closed. Click Reopen to activate.' : 'Change status'}" style="font-size:.75rem; padding:4px 8px; ${isDone ? 'opacity:0.65; cursor:not-allowed;' : ''}">
              <option value="open" ${item.status === 'open' ? 'selected' : ''}>Open</option>
              <option value="in_progress" ${item.status === 'in_progress' ? 'selected' : ''}>In Progress</option>
              <option value="done" ${item.status === 'done' ? 'selected' : ''}>Closed / Done</option>
              <option value="cancelled" ${item.status === 'cancelled' ? 'selected' : ''}>Cancelled</option>
            </select>
            ${!isDone ? `
              <button type="button" class="tp-btn-edit" data-action="edit" data-item-id="${item.id}" title="Edit task details & due date">
                <i class="fa-solid fa-pen-to-square"></i> Edit
              </button>
              <button type="button" class="tp-btn-close-ticket" data-action="complete" data-item-id="${item.id}" title="Close this task / ticket">
                <i class="fa-solid fa-circle-check"></i> Close Ticket
              </button>
            ` : `
              <button type="button" class="tp-btn-reopen" data-action="reopen" data-item-id="${item.id}" title="Reopen this task / ticket">
                <i class="fa-solid fa-rotate-left"></i> Reopen
              </button>
            `}
          `}
          <button type="button" class="tp-btn-delete" data-action="delete" data-item-id="${item.id}" title="Delete task">
            <i class="fa-regular fa-trash-can"></i>
          </button>
        </div>
      </div>

      <div class="tp-task-meta">
        ${isPending ? `
          <span class="tp-tag tp-tag-pending">
            <i class="fa-solid fa-lightbulb"></i> Suggested
          </span>
        ` : ''}
        ${isDone ? `
          <span class="tp-tag tp-tag-closed">
            <i class="fa-solid fa-circle-check"></i> Closed
          </span>
        ` : ''}
        ${formatSourceTag(item.source)}
        ${item.account_id ? `
          <span class="tp-tag tp-tag-account">
            <i class="fa-solid fa-building"></i>
            <a href="/?account=${item.account_id}">${esc(item.account_name || `Account ${item.account_id}`)}</a>
          </span>
        ` : ''}
        ${personaName ? `
          <span class="tp-tag tp-tag-persona">
            <i class="fa-solid fa-user-tie"></i> ${esc(personaName)}
          </span>
        ` : ''}
        <span class="tp-tag ${priorityClass}">${priorityLabel}</span>
        ${formatDueDate(item.due_date, isOverdue)}
        ${item.assigned_to_name ? `
          <span class="tp-tag">
            <i class="fa-solid fa-user-check"></i> ${esc(item.assigned_to_name)}
          </span>
        ` : ''}
        ${!item.assigned_to_id && !isPending && !(getCurrentUser() && (getCurrentUser().role === 'super_admin' || getCurrentUser().role === 'admin' || getCurrentUser().role === 'sales_manager')) ? `
          <button type="button" class="tp-btn-secondary" data-action="take" data-item-id="${item.id}" style="padding:2px 6px; font-size:.72rem;">
            <i class="fa-solid fa-hand-pointer"></i> Take Task
          </button>
        ` : ''}
      </div>
    </div>
  `;
}

function renderListBody(items) {
  if (!items || !items.length) {
    return `
      <div class="empty-block" style="padding:40px 20px; text-align:center;">
        <div class="empty-block-icon" style="font-size:2rem; color:var(--text-muted); margin-bottom:10px;">
          <i class="fa-solid fa-clipboard-check"></i>
        </div>
        <div style="font-size:1rem; font-weight:600; color:var(--text-primary); margin-bottom:4px;">No tasks found</div>
        <div style="font-size:.82rem; color:var(--text-secondary);">
          ${tp.searchQuery || tp.selectedAccountIds.size > 0 || tp.accountFilter || tp.priorityFilter ? 'Try clearing or changing your filters.' : 'You have no open tasks. Click "+ Create Task" to add one!'}
        </div>
      </div>
    `;
  }
  return items.map(renderTaskCard).join('');
}

function render() {
  updateKpis();
  const scoped = scopedItems();
  const visible = visibleItems();
  el('tpStatusFilters').innerHTML = renderFilterChips(scoped, tp.activeStatus);
  el('tpListBody').innerHTML = renderListBody(visible);

  // Update results count indicator
  const resultsCountEl = el('tpResultsCount');
  if (resultsCountEl) {
    const count = visible.length;
    const total = tp.items.length;
    resultsCountEl.textContent = count === total ? `Showing ${count} tasks` : `Showing ${count} of ${total} tasks`;
  }

  // Update search clear button
  const searchClearBtn = el('tpSearchClear');
  if (searchClearBtn) {
    searchClearBtn.style.display = tp.searchQuery ? 'flex' : 'none';
  }

  // Update reset filters button
  const resetBtn = el('tpResetFiltersBtn');
  if (resetBtn) {
    const isFiltered = Boolean(
      tp.searchQuery ||
      tp.selectedAccountIds.size > 0 ||
      tp.priorityFilter ||
      tp.overdueOnly ||
      (tp.activeStatus && tp.activeStatus !== 'open')
    );
    resetBtn.style.display = isFiltered ? 'inline-flex' : 'none';
  }

  // Highlight active KPI cards
  const kpiCards = document.querySelectorAll('.tp-kpi-card');
  kpiCards.forEach(card => card.classList.remove('active'));
  if (tp.overdueOnly) {
    document.querySelector('.tp-kpi-card[data-kpi-action="overdue"]')?.classList.add('active');
  } else if (tp.priorityFilter === 'high') {
    document.querySelector('.tp-kpi-card[data-kpi-action="high"]')?.classList.add('active');
  } else if (tp.selectedAccountIds.size > 0) {
    document.querySelector('.tp-kpi-card[data-kpi-action="accounts"]')?.classList.add('active');
  } else if (tp.activeStatus === 'open') {
    document.querySelector('.tp-kpi-card[data-kpi-action="open"]')?.classList.add('active');
  }
}

async function refetch() {
  try {
    const res = await fetch('/api/me/action-items');
    if (!res.ok) throw new Error(`Failed to load tasks (${res.status})`);
    const data = await res.json();
    tp.items = data.action_items || [];
    populateAccountFilter();
  } catch (err) {
    console.error(err);
    el('tpListBody').innerHTML = '<div class="empty-block" style="padding:20px 4px;"><div class="empty-block-text">Could not load your tasks.</div></div>';
    throw err;
  }
}

async function refetchAndRender() {
  await refetch();
  render();
  if (!deepLinkOpened && openEditModalFn) {
    const editId = new URLSearchParams(window.location.search).get('edit');
    if (editId) {
      const item = tp.items.find(i => i.id === Number(editId));
      if (item) {
        deepLinkOpened = true;
        openEditModalFn(item);
      }
    }
  }
}

function initFilterControls() {
  // Multi-Account dropdown trigger
  el('tpAcctFilterBtn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleAccountMenu();
  });

  // Close dropdown on outside click or escape
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#tpAcctFilterWrap')) {
      closeAccountMenu();
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAccountMenu();
  });

  // Account search input inside dropdown
  el('tpAcctFilterSearch')?.addEventListener('input', (e) => {
    filterDropdownAccounts(e.target.value);
  });

  // "All Accounts" selection in dropdown
  el('tpAcctFilterAllBtn')?.addEventListener('click', () => {
    tp.selectedAccountIds.clear();
    syncAccountFilterUrl();
    populateAccountFilter();
    render();
  });

  // "Clear all" button in dropdown header
  el('tpAcctFilterClearBtn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    tp.selectedAccountIds.clear();
    syncAccountFilterUrl();
    populateAccountFilter();
    render();
  });

  // Account item toggle inside dropdown
  el('tpAcctFilterList')?.addEventListener('click', (e) => {
    const itemBtn = e.target.closest('.tp-acct-filter-account-item');
    if (!itemBtn) return;
    const aid = itemBtn.dataset.acctId;
    if (tp.selectedAccountIds.has(aid)) {
      tp.selectedAccountIds.delete(aid);
    } else {
      tp.selectedAccountIds.add(aid);
    }
    syncAccountFilterUrl();
    populateAccountFilter();
    render();
  });

  // Tag pills removal delegation
  el('tpSelectedAccountTags')?.addEventListener('click', (e) => {
    const removeBtn = e.target.closest('[data-remove-acct-id]');
    if (removeBtn) {
      const aid = removeBtn.dataset.removeAcctId;
      tp.selectedAccountIds.delete(aid);
      syncAccountFilterUrl();
      populateAccountFilter();
      render();
      return;
    }
    const clearAll = e.target.closest('#tpClearAllAcctsBtn');
    if (clearAll) {
      tp.selectedAccountIds.clear();
      syncAccountFilterUrl();
      populateAccountFilter();
      render();
    }
  });

  el('tpAccountFilter')?.addEventListener('change', (e) => { tp.accountFilter = e.target.value; render(); });
  el('tpPriorityFilter')?.addEventListener('change', (e) => { tp.priorityFilter = e.target.value; render(); });
  el('tpSort')?.addEventListener('change', (e) => { tp.sort = e.target.value; render(); });
  el('tpSearchInput')?.addEventListener('input', (e) => { tp.searchQuery = (e.target.value || '').trim(); render(); });

  // Search clear button
  el('tpSearchClear')?.addEventListener('click', () => {
    tp.searchQuery = '';
    const input = el('tpSearchInput');
    if (input) {
      input.value = '';
      input.focus();
    }
    render();
  });

  // Reset all filters button
  el('tpResetFiltersBtn')?.addEventListener('click', () => {
    tp.searchQuery = '';
    tp.selectedAccountIds.clear();
    tp.accountFilter = '';
    tp.priorityFilter = '';
    tp.overdueOnly = false;
    tp.activeStatus = 'open';
    if (el('tpSearchInput')) el('tpSearchInput').value = '';
    if (el('tpPriorityFilter')) el('tpPriorityFilter').value = '';
    if (el('tpSort')) el('tpSort').value = 'due';
    syncAccountFilterUrl();
    populateAccountFilter();
    render();
  });

  // Interactive KPI Cards Widget Click Delegation
  el('tpKpiGrid')?.addEventListener('click', (e) => {
    const card = e.target.closest('.tp-kpi-card');
    if (!card) return;
    const action = card.dataset.kpiAction;
    if (action === 'open') {
      tp.activeStatus = 'open';
      tp.overdueOnly = false;
      render();
    } else if (action === 'overdue') {
      tp.overdueOnly = !tp.overdueOnly;
      if (tp.overdueOnly) tp.activeStatus = 'all';
      render();
    } else if (action === 'high') {
      tp.priorityFilter = tp.priorityFilter === 'high' ? '' : 'high';
      if (el('tpPriorityFilter')) el('tpPriorityFilter').value = tp.priorityFilter;
      render();
    } else if (action === 'accounts') {
      toggleAccountMenu();
    }
  });
}

function initListDelegation() {
  const list = el('tpListBody');
  const filters = el('tpStatusFilters');

  filters?.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-action="filter-status"]');
    if (!chip) return;
    tp.activeStatus = chip.dataset.value;
    render();
  });

  list?.addEventListener('change', async (e) => {
    const select = e.target.closest('[data-action="set-status"]');
    if (!select) return;
    const itemId = Number(select.dataset.itemId);
    const item = tp.items.find(i => i.id === itemId);

    if (select.value === 'done') {
      if (item && openCloseModalFn) {
        select.value = item.status || 'open';
        openCloseModalFn(item);
        return;
      }
    }

    try {
      await fetch(`/api/action-items/${itemId}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: select.value }),
      });
      showToast('Task updated');
      await refetchAndRender();
    } catch (err) {
      console.error(err);
      showToast('Could not update status.');
    }
  });

  list?.addEventListener('click', async (e) => {
    const toggleBtn = e.target.closest('[data-action="toggle-desc"]');
    if (toggleBtn) {
      const wrap = toggleBtn.closest('.tp-task-desc-wrap');
      const descEl = wrap ? wrap.querySelector('.tp-task-desc') : null;
      if (descEl) {
        const isExp = descEl.classList.toggle('is-expanded');
        descEl.classList.toggle('is-clamped', !isExp);
        toggleBtn.innerHTML = isExp
          ? '<span>Show less</span> <i class="fa-solid fa-chevron-up"></i>'
          : '<span>Show more</span> <i class="fa-solid fa-chevron-down"></i>';
      }
      return;
    }

    const editBtn = e.target.closest('[data-action="edit"]');
    if (editBtn) {
      const itemId = Number(editBtn.dataset.itemId);
      const item = tp.items.find(i => i.id === itemId);
      if (item && openEditModalFn) openEditModalFn(item);
      return;
    }
    const btn = e.target.closest('[data-action="take"], [data-action="complete"], [data-action="reopen"], [data-action="delete"], [data-action="approve"], [data-action="reject"]');
    if (!btn) return;
    const itemId = btn.dataset.itemId;
    const action = btn.dataset.action;
    try {
      if (action === 'approve') {
        await fetch(`/api/action-items/${itemId}/approve`, { method: 'POST' });
        showToast('Signal task approved and added to active tasks!');
      } else if (action === 'reject') {
        await fetch(`/api/action-items/${itemId}/reject`, { method: 'POST' });
        showToast('Signal suggestion dismissed.');
      } else if (action === 'take') {
        const me = getCurrentUser();
        if (!me) { showToast('Sign in to take a task.'); return; }
        if (me.role === 'super_admin' || me.role === 'admin' || me.role === 'sales_manager') {
          showToast('High-authority roles (administrators/managers) cannot be assigned tasks.');
          return;
        }
        await fetch(`/api/action-items/${itemId}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ assigned_to_id: me.id }),
        });
        showToast('Task assigned to you');
      } else if (action === 'complete') {
        const item = tp.items.find(i => i.id === Number(itemId));
        if (item && openCloseModalFn) {
          openCloseModalFn(item);
          return;
        }
        await fetch(`/api/action-items/${itemId}/complete`, { method: 'POST' });
        showToast('Ticket closed successfully!');
      } else if (action === 'reopen') {
        const item = tp.items.find(i => i.id === Number(itemId));
        if (item && openReopenModalFn) {
          openReopenModalFn(item);
          return;
        }
        await fetch(`/api/action-items/${itemId}/reopen`, { method: 'POST' });
        showToast('Ticket reopened');
      } else if (action === 'delete') {
        if (!window.confirm('Are you sure you want to delete this task?')) return;
        await fetch(`/api/action-items/${itemId}`, { method: 'DELETE' });
        showToast('Task deleted');
      }
      await refetchAndRender();
    } catch (err) {
      console.error(err);
      showToast('Could not update the task.');
    }
  });
}

function wireExport() {
  const btn = el('tpExport');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const params = new URLSearchParams();
    if (tp.activeStatus && tp.activeStatus !== 'all') params.set('status', tp.activeStatus);
    if (tp.selectedAccountIds.size > 0) {
      params.set('account_ids', Array.from(tp.selectedAccountIds).join(','));
    } else if (tp.accountFilter) {
      params.set('account_id', tp.accountFilter);
    }
    if (tp.priorityFilter) params.set('priority', tp.priorityFilter);
    try {
      await downloadFile(`/api/me/action-items/export?${params}`, 'my-tasks.xlsx');
      showToast('Tasks downloaded');
    } catch (err) { showToast(err.message); }
  });
}

// ── "Create Task" Modal Controller ────────────────────────────
function initCreateTaskModal() {
  const modal = el('tpCreateModalBackdrop');
  const createBtn = el('tpCreateTaskBtn');
  const closeBtn = el('tpModalCloseBtn');
  const cancelBtn = el('tpModalCancelBtn');
  const form = el('tpCreateTaskForm');
  const accountSelect = el('tpFormAccount');
  const personaSelect = el('tpFormPersona');
  const assigneeSelect = el('tpFormAssignee');
  const errorEl = el('tpFormError');
  const submitBtn = el('tpFormSubmitBtn');

  const personaHint = el('tpFormPersonaHint');

  if (!modal || !createBtn || !form) return;

  function openModal() {
    form.reset();
    delete form.dataset.mode;
    delete form.dataset.itemId;
    if (errorEl) { errorEl.style.display = 'none'; errorEl.textContent = ''; }
    if (el('tpModalTitle')) el('tpModalTitle').textContent = 'Create Task';
    if (el('tpModalSubtitle')) el('tpModalSubtitle').textContent = 'Add a structured follow-up or action item for your sales pipeline';
    if (submitBtn) submitBtn.innerHTML = '<i class="fa-solid fa-check"></i> Create Task';

    // Persona dropdown must be strictly disabled until a target account is chosen
    if (personaSelect) {
      personaSelect.disabled = true;
      personaSelect.value = '';
      personaSelect.innerHTML = '<option value="">Select an account first...</option>';
      personaSelect.title = 'Please select a target account first';
    }
    if (personaHint) {
      personaHint.textContent = 'Select an account first to choose a contact';
      personaHint.style.color = 'var(--text-muted)';
    }

    modal.style.display = 'flex';
    populateModalAccounts();
    populateModalUsers();
    el('tpFormTitle')?.focus();
  }

  function closeModal() {
    modal.style.display = 'none';
  }

  openEditModalFn = async function(item) {
    if (!item) return;
    form.reset();
    form.dataset.mode = 'edit';
    form.dataset.itemId = String(item.id);
    if (errorEl) { errorEl.style.display = 'none'; errorEl.textContent = ''; }
    if (el('tpModalTitle')) el('tpModalTitle').textContent = `Edit Task #${item.id}`;
    if (el('tpModalSubtitle')) el('tpModalSubtitle').textContent = 'Update task details, due date, priority, or assignee';
    if (submitBtn) submitBtn.innerHTML = '<i class="fa-solid fa-check"></i> Save Changes';

    modal.style.display = 'flex';
    await Promise.all([populateModalAccounts(), populateModalUsers()]);

    if (el('tpFormTitle')) el('tpFormTitle').value = item.title || '';
    if (accountSelect) accountSelect.value = item.account_id ? String(item.account_id) : '';
    if (item.account_id) {
      await loadPersonasForAccount(item.account_id, item.persona_id);
    } else {
      if (personaSelect) {
        personaSelect.disabled = true;
        personaSelect.value = '';
        personaSelect.innerHTML = '<option value="">Select an account first...</option>';
        personaSelect.title = 'Please select a target account first';
      }
      if (personaHint) {
        personaHint.textContent = 'Select an account first to choose a contact';
        personaHint.style.color = 'var(--text-muted)';
      }
    }
    if (el('tpFormPriority')) el('tpFormPriority').value = item.priority || 'medium';
    if (el('tpFormStatus')) el('tpFormStatus').value = item.status || 'open';
    if (el('tpFormDueDate')) el('tpFormDueDate').value = toDatetimeLocal(item.due_date);
    if (assigneeSelect) assigneeSelect.value = item.assigned_to_id ? String(item.assigned_to_id) : '';
    if (el('tpFormDesc')) el('tpFormDesc').value = item.description || '';
    el('tpFormDueDate')?.focus();
  };

  createBtn.addEventListener('click', openModal);
  closeBtn?.addEventListener('click', closeModal);
  cancelBtn?.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });

  // If user clicks on persona field while disabled, prompt to select account
  personaSelect?.parentElement?.addEventListener('click', () => {
    if (personaSelect && personaSelect.disabled && (!accountSelect || !accountSelect.value)) {
      if (errorEl) {
        errorEl.textContent = 'Please select a target account first before choosing a contact/persona.';
        errorEl.style.display = 'block';
      }
      accountSelect?.focus();
    }
  });

  async function populateModalAccounts() {
    if (!accountSelect) return;
    try {
      const accounts = await loadAccountsCached();
      tp.accountsCache = accounts || [];
      accountSelect.innerHTML = '<option value="">Select an account...</option>' +
        tp.accountsCache.map(a => `<option value="${a.id}">${esc(a.display_name || a.legal_name || a.name || `Account ${a.id}`)}</option>`).join('');
    } catch (err) {
      console.warn('Could not load accounts for modal:', err);
    }
  }

  async function populateModalUsers() {
    if (!assigneeSelect) return;
    const me = getCurrentUser();
    // High-authority roles: super_admin, admin, sales_manager
    const isHighAuthority = me && (me.role === 'super_admin' || me.role === 'admin' || me.role === 'sales_manager');
    try {
      const res = await fetch('/api/users');
      if (res.ok) {
        const data = await res.json();
        // Tasks can ONLY be assigned to standard sales team users (role === 'user'), never high-authority roles
        tp.usersCache = (data.users || []).filter(u => u.role === 'user');
      }
    } catch (e) { /* fallback */ }

    // High-authority users (admins and managers) CANNOT assign tasks to themselves.
    // They can assign to any sales team user, or leave unassigned.
    // Standard sales users can assign to themselves or another sales user.
    let options = isHighAuthority
      ? '<option value="">Select sales rep / user (optional)...</option>'
      : `<option value="">Assign to me (${esc(me ? (me.name || me.email) : 'Default')})</option>`;

    if (tp.usersCache && tp.usersCache.length) {
      options += tp.usersCache.map(u => `<option value="${u.id}" ${!isHighAuthority && me && me.id === u.id ? 'selected' : ''}>${esc(u.name || u.email)} (Sales Rep)</option>`).join('');
    }
    assigneeSelect.innerHTML = options;
  }

  async function loadPersonasForAccount(accountId, selectedPersonaId = null) {
    if (!personaSelect) return;
    if (!accountId) {
      personaSelect.disabled = true;
      personaSelect.value = '';
      personaSelect.innerHTML = '<option value="">Select an account first...</option>';
      personaSelect.title = 'Please select a target account first';
      if (personaHint) {
        personaHint.textContent = 'Select an account first to choose a contact';
        personaHint.style.color = 'var(--text-muted)';
      }
      return;
    }

    personaSelect.disabled = false;
    personaSelect.removeAttribute('title');
    personaSelect.innerHTML = '<option value="">Loading contacts for selected account…</option>';
    if (personaHint) {
      personaHint.textContent = 'Loading account contacts...';
      personaHint.style.color = 'var(--text-muted)';
    }

    try {
      const res = await fetch(`/api/accounts/${accountId}/personas`);
      if (res.ok) {
        const data = await res.json();
        const personas = data.personas || data || [];
        if (personas.length) {
          personaSelect.innerHTML = '<option value="">Select contact / persona (optional)...</option>' +
            personas.map(p => `<option value="${p.id}" ${selectedPersonaId && String(p.id) === String(selectedPersonaId) ? 'selected' : ''}>${esc(p.name || p.full_name || 'Contact')} (${esc(p.title || p.job_title || 'Lead')})</option>`).join('');
          if (personaHint) {
            personaHint.textContent = `${personas.length} contact${personas.length === 1 ? '' : 's'} available for this account`;
            personaHint.style.color = 'var(--text-muted)';
          }
        } else {
          personaSelect.innerHTML = '<option value="">No contacts found for this account</option>';
          if (personaHint) {
            personaHint.textContent = 'No contacts linked to this account';
            personaHint.style.color = 'var(--text-muted)';
          }
        }
      } else {
        personaSelect.innerHTML = '<option value="">No contacts available</option>';
        if (personaHint) {
          personaHint.textContent = 'Could not load contacts for this account';
          personaHint.style.color = 'var(--text-muted)';
        }
      }
    } catch (err) {
      personaSelect.innerHTML = '<option value="">No contacts available</option>';
    }
  }

  // When account changes, dynamically fetch personas for that account or disable persona select if empty
  accountSelect?.addEventListener('change', async (e) => {
    loadPersonasForAccount(e.target.value);
  });

  // Handle form submission
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (errorEl) { errorEl.style.display = 'none'; errorEl.textContent = ''; }

    const isEdit = form.dataset.mode === 'edit';
    const editItemId = form.dataset.itemId;

    const title = (el('tpFormTitle')?.value || '').trim();
    const accountId = el('tpFormAccount')?.value;
    const personaId = el('tpFormPersona')?.value;
    const priority = el('tpFormPriority')?.value || 'medium';
    const status = el('tpFormStatus')?.value || 'open';
    const dueDateRaw = el('tpFormDueDate')?.value;
    const assigneeId = el('tpFormAssignee')?.value;
    const description = (el('tpFormDesc')?.value || '').trim();

    if (!title) {
      if (errorEl) { errorEl.textContent = 'Please enter a task name.'; errorEl.style.display = 'block'; }
      el('tpFormTitle')?.focus();
      return;
    }
    if (!accountId) {
      if (errorEl) { errorEl.textContent = 'Please select a target account.'; errorEl.style.display = 'block'; }
      el('tpFormAccount')?.focus();
      return;
    }

    const me = getCurrentUser();
    const isHighAuthority = me && (me.role === 'super_admin' || me.role === 'admin' || me.role === 'sales_manager');

    // Assignee validation: tasks can only be assigned to standard sales team users (role === 'user')
    let finalAssigneeId = null;
    if (assigneeId) {
      finalAssigneeId = Number(assigneeId);
      const targetUser = (tp.usersCache || []).find(u => u.id === finalAssigneeId);
      if (targetUser && targetUser.role !== 'user') {
        if (errorEl) {
          errorEl.textContent = 'Tasks can only be assigned to sales team members, not high-authority roles (administrators or managers).';
          errorEl.style.display = 'block';
        }
        return;
      }
    } else if (!isHighAuthority && me && me.role === 'user') {
      finalAssigneeId = me.id;
    }

    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = `<div class="spinner-sm"></div> ${isEdit ? 'Saving…' : 'Creating…'}`;
    }

    try {
      if (isEdit) {
        const payload = {
          title,
          description: description || null,
          priority,
          status,
          persona_id: personaId ? Number(personaId) : null,
          due_date: dueDateRaw ? new Date(dueDateRaw).toISOString() : null,
          assigned_to_id: finalAssigneeId,
        };
        const res = await fetch(`/api/action-items/${editItemId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.detail || `Server error (${res.status})`);
        }
        closeModal();
        showToast('Task updated successfully!');
        await refetchAndRender();
      } else {
        const payload = {
          account_id: Number(accountId),
          title,
          description: description || null,
          priority,
          status,
          persona_id: personaId ? Number(personaId) : null,
          due_date: dueDateRaw ? new Date(dueDateRaw).toISOString() : null,
          assigned_to_id: finalAssigneeId,
        };
        const res = await fetch('/api/action-items', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.detail || `Server error (${res.status})`);
        }
        closeModal();
        showToast('Task created successfully!');
        await refetchAndRender();
      }
    } catch (err) {
      if (errorEl) {
        errorEl.textContent = err.message || `Could not ${isEdit ? 'update' : 'create'} task. Please try again.`;
        errorEl.style.display = 'block';
      }
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = `<i class="fa-solid fa-check"></i> ${isEdit ? 'Save Changes' : 'Create Task'}`;
      }
    }
  });
}

function initCloseTicketModal() {
  const backdrop = el('tpCloseModalBackdrop');
  const form = el('tpCloseTaskForm');
  const closeBtn = el('tpCloseModalCloseBtn');
  const cancelBtn = el('tpCloseModalCancelBtn');
  const taskIdInput = el('tpCloseTaskId');
  const commentInput = el('tpCloseComment');
  const submitBtn = el('tpCloseSubmitBtn');
  const titleEl = el('tpCloseModalTitle');

  function open(item) {
    if (!backdrop || !item) return;
    taskIdInput.value = item.id;
    if (commentInput) commentInput.value = '';
    if (titleEl) titleEl.textContent = `Close Ticket #${item.id}`;
    backdrop.style.display = 'flex';
    setTimeout(() => commentInput?.focus(), 50);
  }

  function close() {
    if (!backdrop) return;
    backdrop.style.display = 'none';
    if (form) form.reset();
  }

  openCloseModalFn = open;

  closeBtn?.addEventListener('click', close);
  cancelBtn?.addEventListener('click', close);
  backdrop?.addEventListener('click', (e) => {
    if (e.target === backdrop) close();
  });

  form?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const itemId = taskIdInput.value;
    const comment = (commentInput?.value || '').trim();
    if (!itemId) return;

    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Closing...';
    }

    try {
      const res = await fetch(`/api/action-items/${itemId}/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ comment }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Server error (${res.status})`);
      }
      close();
      showToast('Ticket closed successfully with comment.');
      await refetchAndRender();
    } catch (err) {
      console.error(err);
      showToast(err.message || 'Could not close ticket.');
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-check"></i> Confirm & Close Ticket';
      }
    }
  });
}

function initReopenTicketModal() {
  const backdrop = el('tpReopenModalBackdrop');
  const form = el('tpReopenTaskForm');
  const closeBtn = el('tpReopenModalCloseBtn');
  const cancelBtn = el('tpReopenModalCancelBtn');
  const taskIdInput = el('tpReopenTaskId');
  const commentInput = el('tpReopenComment');
  const submitBtn = el('tpReopenSubmitBtn');
  const titleEl = el('tpReopenModalTitle');

  function open(item) {
    if (!backdrop || !item) return;
    taskIdInput.value = item.id;
    if (commentInput) commentInput.value = '';
    if (titleEl) titleEl.textContent = `Reopen Ticket #${item.id}`;
    backdrop.style.display = 'flex';
    setTimeout(() => commentInput?.focus(), 50);
  }

  function close() {
    if (!backdrop) return;
    backdrop.style.display = 'none';
    if (form) form.reset();
  }

  openReopenModalFn = open;

  closeBtn?.addEventListener('click', close);
  cancelBtn?.addEventListener('click', close);
  backdrop?.addEventListener('click', (e) => {
    if (e.target === backdrop) close();
  });

  form?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const itemId = taskIdInput.value;
    const comment = (commentInput?.value || '').trim();
    if (!itemId) return;

    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Reopening...';
    }

    try {
      const res = await fetch(`/api/action-items/${itemId}/reopen`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ comment }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `Server error (${res.status})`);
      }
      close();
      showToast('Ticket reopened successfully with reason.');
      await refetchAndRender();
    } catch (err) {
      console.error(err);
      showToast(err.message || 'Could not reopen ticket.');
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-rotate-left"></i> Confirm & Reopen Ticket';
      }
    }
  });
}

function init() {
  initThemeToggle();
  initAccountsNav();
  initAccountFilterFromUrl();
  initFilterControls();
  initListDelegation();
  wireExport();
  initCreateTaskModal();
  initCloseTicketModal();
  initReopenTicketModal();
  refetchAndRender();
}

initTopbarAuth().then((user) => {
  if (!user) {
    document.body.style.display = 'none';
    window.location.replace(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    return;
  }
  if (user.role !== 'super_admin' && user.has_tasks_access === false) {
    if (user.has_command_center_access !== false) {
      window.location.href = '/command-center?no_tasks_access=1';
      return;
    } else if (user.has_dashboard_access !== false) {
      window.location.href = '/?no_tasks_access=1';
      return;
    } else {
      const mainEl = document.getElementById('tasksMain') || document.body;
      mainEl.innerHTML = `
        <div class="empty-block" style="margin:80px auto; max-width:460px; text-align:center; padding:40px; background:var(--card-bg); border-radius:0; border:1px solid var(--border-color);">
          <div class="empty-block-icon" style="font-size:2.5rem; color:var(--text-muted); margin-bottom:16px;"><i class="fa-solid fa-shield-halved"></i></div>
          <div style="font-size:1.15rem; font-weight:700; color:var(--text-primary); margin-bottom:8px;">Tasks Access Restricted</div>
          <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.5;">You do not have access to Tasks. Please ask a Super Administrator to grant you permissions.</div>
        </div>`;
      return;
    }
  }
  init();
});
