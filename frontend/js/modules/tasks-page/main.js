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
let deepLinkOpened = false;

const tp = {
  items: [],
  activeStatus: 'open',
  accountFilter: '',
  priorityFilter: '',
  searchQuery: '',
  sort: 'due',
  accountsCache: [],
  usersCache: [],
};

function scopedItems() {
  return tp.items.filter(i => {
    if (tp.accountFilter && String(i.account_id) !== tp.accountFilter) return false;
    if (tp.priorityFilter && i.priority !== tp.priorityFilter) return false;
    if (tp.searchQuery) {
      const q = tp.searchQuery.toLowerCase();
      const title = (i.title || '').toLowerCase();
      const desc = (i.description || '').toLowerCase();
      const acct = (i.account_name || '').toLowerCase();
      const persona = (i.persona_name || (i.persona && i.persona.name) || '').toLowerCase();
      if (!title.includes(q) && !desc.includes(q) && !acct.includes(q) && !persona.includes(q)) {
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

function populateAccountFilter() {
  const select = el('tpAccountFilter');
  if (!select) return;
  const seen = new Map();
  tp.items.forEach(i => {
    if (i.account_id != null && !seen.has(i.account_id)) {
      seen.set(i.account_id, i.account_name || `Account ${i.account_id}`);
    }
  });
  const current = select.value;
  select.innerHTML = '<option value="">All Accounts</option>' + [...seen.entries()]
    .sort((a, b) => a[1].localeCompare(b[1]))
    .map(([id, name]) => `<option value="${id}">${name}</option>`).join('');
  select.value = current && seen.has(Number(current)) ? current : '';
  tp.accountFilter = select.value;
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

function renderTaskCard(item) {
  const isDone = item.status === 'done';
  const isPending = item.status === 'pending_review';
  const isOverdue = Boolean(item.is_overdue && (item.status === 'open' || item.status === 'in_progress'));
  const isHigh = item.priority === 'high' && !isDone;
  const isSignal = item.source === 'llm_suggested' || item.source === 'priority_signal' || item.source === 'signal_feed';
  const personaName = item.persona_name || (item.persona && item.persona.name) || '';
  const priorityClass = item.priority === 'high' ? 'tp-tag-priority-high' : item.priority === 'low' ? 'tp-tag-priority-low' : 'tp-tag-priority-med';
  const priorityLabel = item.priority === 'high' ? 'High' : item.priority === 'low' ? 'Low' : 'Medium';

  const hasLongDesc = Boolean(item.description && item.description.length > 130);

  return `
    <div class="tp-task-item ${isDone ? 'is-done' : ''} ${isOverdue ? 'is-overdue' : ''} ${isHigh ? 'is-high' : ''} ${isPending ? 'is-pending' : ''}" data-task-id="${item.id}">
      <div class="tp-task-top">
        <div class="tp-task-title-wrap">
          <span class="tp-task-id-badge" title="Task ID #${item.id}">#${item.id}</span>
          <div class="tp-task-text-wrap">
            <h3 class="tp-task-title ${isDone ? 'line-through text-muted' : ''}">${esc(item.title)}</h3>
            ${item.description ? `
              <div class="tp-task-desc-wrap">
                <div class="tp-task-desc ${isDone ? 'text-muted' : ''} ${hasLongDesc ? 'is-clamped' : ''}">
                  ${formatDescriptionHtml(item.description)}
                </div>
                ${hasLongDesc ? `
                  <button type="button" class="tp-desc-toggle-btn" data-action="toggle-desc" title="Expand or collapse full description">
                    <span>Show more</span> <i class="fa-solid fa-chevron-down"></i>
                  </button>
                ` : ''}
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
            <select class="tp-select" data-action="set-status" data-item-id="${item.id}" title="Change status" style="font-size:.75rem; padding:4px 8px;">
              <option value="open" ${item.status === 'open' ? 'selected' : ''}>Open</option>
              <option value="in_progress" ${item.status === 'in_progress' ? 'selected' : ''}>In Progress</option>
              <option value="done" ${item.status === 'done' ? 'selected' : ''}>Closed / Done</option>
              <option value="cancelled" ${item.status === 'cancelled' ? 'selected' : ''}>Cancelled</option>
            </select>
            <button type="button" class="tp-btn-edit" data-action="edit" data-item-id="${item.id}" title="Edit task details & due date">
              <i class="fa-solid fa-pen-to-square"></i> Edit
            </button>
            ${!isDone ? `
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
        ${isSignal ? `
          <span class="tp-tag tp-tag-signal" title="Signal Trigger Feed">
            <i class="fa-solid fa-bolt"></i> Signal Feed
          </span>
        ` : ''}
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
        ${!item.assigned_to_id && !isPending && !(getCurrentUser() && (getCurrentUser().role === 'super_admin' || getCurrentUser().role === 'admin')) ? `
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
          ${tp.searchQuery || tp.accountFilter || tp.priorityFilter ? 'Try clearing or changing your filters.' : 'You have no open tasks. Click "+ Create Task" to add one!'}
        </div>
      </div>
    `;
  }
  return items.map(renderTaskCard).join('');
}

function render() {
  updateKpis();
  el('tpStatusFilters').innerHTML = renderFilterChips(scopedItems(), tp.activeStatus);
  el('tpListBody').innerHTML = renderListBody(visibleItems());
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
  el('tpAccountFilter')?.addEventListener('change', (e) => { tp.accountFilter = e.target.value; render(); });
  el('tpPriorityFilter')?.addEventListener('change', (e) => { tp.priorityFilter = e.target.value; render(); });
  el('tpSort')?.addEventListener('change', (e) => { tp.sort = e.target.value; render(); });
  el('tpSearchInput')?.addEventListener('input', (e) => { tp.searchQuery = (e.target.value || '').trim(); render(); });
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
    try {
      await fetch(`/api/action-items/${select.dataset.itemId}`, {
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
        if (me.role === 'super_admin' || me.role === 'admin') {
          showToast('Administrators cannot be assigned tasks.');
          return;
        }
        await fetch(`/api/action-items/${itemId}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ assigned_to_id: me.id }),
        });
        showToast('Task assigned to you');
      } else if (action === 'complete') {
        await fetch(`/api/action-items/${itemId}/complete`, { method: 'POST' });
        showToast('Ticket closed successfully!');
      } else if (action === 'reopen') {
        await fetch(`/api/action-items/${itemId}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'open' }),
        });
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
    if (tp.accountFilter) params.set('account_id', tp.accountFilter);
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

  if (!modal || !createBtn || !form) return;

  function openModal() {
    form.reset();
    delete form.dataset.mode;
    delete form.dataset.itemId;
    if (errorEl) { errorEl.style.display = 'none'; errorEl.textContent = ''; }
    if (el('tpModalTitle')) el('tpModalTitle').textContent = 'Create Task';
    if (el('tpModalSubtitle')) el('tpModalSubtitle').textContent = 'Add a structured follow-up or action item for your sales pipeline';
    if (submitBtn) submitBtn.innerHTML = '<i class="fa-solid fa-check"></i> Create Task';
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
    await loadPersonasForAccount(item.account_id, item.persona_id);
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
    const isAdmin = me && (me.role === 'super_admin' || me.role === 'admin');
    try {
      const res = await fetch('/api/users');
      if (res.ok) {
        const data = await res.json();
        tp.usersCache = (data.users || []).filter(u => u.role !== 'super_admin' && u.role !== 'admin');
      }
    } catch (e) { /* fallback */ }

    let options = isAdmin
      ? '<option value="">Select sales rep / assignee (optional)...</option>'
      : `<option value="">Assign to me (${esc(me ? (me.name || me.email) : 'Default')})</option>`;

    if (tp.usersCache && tp.usersCache.length) {
      options += tp.usersCache.map(u => `<option value="${u.id}" ${!isAdmin && me && me.id === u.id ? 'selected' : ''}>${esc(u.name || u.email)}</option>`).join('');
    }
    assigneeSelect.innerHTML = options;
  }

  async function loadPersonasForAccount(accountId, selectedPersonaId = null) {
    if (!personaSelect) return;
    if (!accountId) {
      personaSelect.innerHTML = '<option value="">Select account first...</option>';
      return;
    }
    personaSelect.innerHTML = '<option value="">Loading contacts…</option>';
    try {
      const res = await fetch(`/api/accounts/${accountId}/personas`);
      if (res.ok) {
        const data = await res.json();
        const personas = data.personas || data || [];
        if (personas.length) {
          personaSelect.innerHTML = '<option value="">Select contact / persona (optional)...</option>' +
            personas.map(p => `<option value="${p.id}" ${selectedPersonaId && String(p.id) === String(selectedPersonaId) ? 'selected' : ''}>${esc(p.name || p.full_name || 'Contact')} (${esc(p.title || p.job_title || 'Lead')})</option>`).join('');
        } else {
          personaSelect.innerHTML = '<option value="">No contacts found for this account</option>';
        }
      } else {
        personaSelect.innerHTML = '<option value="">Optional (none)</option>';
      }
    } catch (err) {
      personaSelect.innerHTML = '<option value="">Optional (none)</option>';
    }
  }

  // When account changes, dynamically fetch personas for that account
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
    const isAdmin = me && (me.role === 'super_admin' || me.role === 'admin');

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
          assigned_to_id: assigneeId ? Number(assigneeId) : null,
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
          assigned_to_id: assigneeId ? Number(assigneeId) : (isAdmin ? null : (me ? me.id : null)),
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

function init() {
  initThemeToggle();
  initAccountsNav();
  initFilterControls();
  initListDelegation();
  wireExport();
  initCreateTaskModal();
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
