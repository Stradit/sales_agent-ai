import { esc } from './utils.js';
import { showToast } from '../toast.js';
import { renderSkeleton } from '../skeleton.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { clearGlobalAccountFilter } from './account-filter.js';

function refreshTopbarBadge() {
  const badge = document.getElementById('topbarMyTasksBadge') || document.getElementById('openActionItemsBadge');
  if (!badge) return;
  fetch('/api/me/action-items')
    .then(r => (r.ok ? r.json() : null))
    .then(d => {
      if (!d) return;
      const open = (d.action_items || []).filter(i => i.status !== 'done' && i.status !== 'cancelled');
      badge.textContent = open.length;
    })
    .catch(() => {});
}

function formatDue(iso, isOverdue) {
  if (!iso) return { text: 'No due date', cls: 'cc-due-upcoming', sev: 'upcoming', icon: 'fa-regular fa-calendar' };
  const target = new Date(iso).getTime();
  const now = Date.now();
  const diffDays = Math.round((target - now) / 86400000);

  if (diffDays < 0 || isOverdue) {
    const d = Math.max(1, Math.abs(diffDays));
    return {
      text: `${d}d overdue`,
      cls: 'cc-due-critical',
      sev: 'critical',
      icon: 'fa-solid fa-triangle-exclamation',
      isOverdue: true
    };
  }
  if (diffDays === 0) {
    return {
      text: 'Due today',
      cls: 'cc-due-urgent',
      sev: 'high',
      icon: 'fa-solid fa-clock',
      isToday: true
    };
  }
  if (diffDays === 1) {
    return {
      text: 'Due tomorrow',
      cls: 'cc-due-soon',
      sev: 'med',
      icon: 'fa-regular fa-clock'
    };
  }
  return {
    text: `Due in ${diffDays}d`,
    cls: 'cc-due-upcoming',
    sev: 'upcoming',
    icon: 'fa-regular fa-calendar-check'
  };
}

async function loadDueSoon() {
  const res = await fetch('/api/me/action-items');
  if (!res.ok) throw new Error(`Failed to load tasks (${res.status})`);
  const data = await res.json();
  const cutoff = Date.now() + 7 * 86400000;
  return (data.action_items || [])
    .filter(i => (i.status === 'open' || i.status === 'in_progress') && i.due_date && new Date(i.due_date).getTime() <= cutoff)
    .sort((a, b) => new Date(a.due_date) - new Date(b.due_date))
    .slice(0, 10);
}

function rowHtml(item) {
  const due = formatDue(item.due_date, item.is_overdue);
  const accountName = item.account_name || 'General';
  const accountUrl = `/account-detail?name=${encodeURIComponent(accountName)}`;
  return `
    <li class="cc-due-card cc-due-sev-${due.sev}" data-item-id="${item.id}">
      <!-- Header Row: Urgency Tag + Target Account Pill -->
      <div class="cc-due-card-header">
        <span class="cc-due-urgency-tag ${due.cls}" title="Timeline: ${due.text}">
          <i class="${due.icon}"></i> ${esc(due.text)}
        </span>
        <a href="${accountUrl}" class="cc-due-acc-pill" title="View Dossier for ${esc(accountName)}">
          <i class="fa-regular fa-building"></i>
          <span class="cc-due-acc-name">${esc(accountName)}</span>
          <i class="fa-solid fa-arrow-up-right-from-square cc-ext-icon"></i>
        </a>
      </div>

      <!-- Main Body: Task Title -->
      <div class="cc-due-card-body">
        <h4 class="cc-due-title" title="${esc(item.title)}">${esc(item.title)}</h4>
      </div>

      <!-- Footer Row: Quick Micro-Actions -->
      <div class="cc-due-card-footer">
        <div class="cc-due-actions-group">
          <button type="button" class="cc-due-btn cc-due-btn-remind" data-action="remind" title="Send email reminder to assignee">
            <i class="fa-regular fa-bell"></i> <span>Remind</span>
          </button>
          <button type="button" class="cc-due-btn cc-due-btn-complete" data-action="complete" title="Mark this task completed">
            <i class="fa-solid fa-check"></i> <span>Done</span>
          </button>
        </div>
      </div>
    </li>
  `;
}

export async function renderDueSoon() {
  const list = document.getElementById('ccDueSoonList');
  const countBadge = document.getElementById('ccDueSoonCountBadge');
  const filterContainer = document.getElementById('ccDueSoonFilterStripContainer');
  if (!list) return;

  list.innerHTML = renderSkeleton('feed-rows');
  if (filterContainer) filterContainer.innerHTML = '';

  let allItems;
  try {
    allItems = await loadDueSoon();
  } catch (err) {
    console.error(err);
    list.innerHTML = '<li class="cc-drawer-empty">Could not load tasks.</li>';
    if (countBadge) countBadge.style.display = 'none';
    return;
  }

  // Header count badge
  if (countBadge) {
    const overdueCount = allItems.filter(i => {
      const d = formatDue(i.due_date, i.is_overdue);
      return d.isOverdue;
    }).length;

    if (overdueCount > 0) {
      countBadge.textContent = `${overdueCount} overdue`;
      countBadge.className = 'cc-due-badge-count cc-badge-has-overdue';
      countBadge.style.display = 'inline-flex';
    } else if (allItems.length > 0) {
      countBadge.textContent = `${allItems.length} due`;
      countBadge.className = 'cc-due-badge-count';
      countBadge.style.display = 'inline-flex';
    } else {
      countBadge.style.display = 'none';
    }
  }

  // Account filtering
  let items = allItems;
  const hasFilter = (ccState.activeAccountIds && ccState.activeAccountIds.size > 0) || ccState.activeAccountId;
  const names = ccState.selectedAccountNames || [];

  if (hasFilter) {
    items = allItems.filter(i => matchesCurrentAccount(i.account_id, i.account_name));

    if (filterContainer) {
      const filterLabel = names.length === 1
        ? names[0]
        : (names.length > 1 ? `${names.length} Selected Accounts` : (ccState.selectedAccountName || 'Selected Accounts'));

      filterContainer.innerHTML = `
        <div class="cc-intel-filter-strip" style="margin-bottom: 6px;">
          <span class="cc-intel-filter-info">
            <i class="fa-solid fa-filter"></i> Filtered by <strong>${esc(filterLabel)}</strong> (${items.length} of ${allItems.length})
          </span>
          <button type="button" class="cc-intel-filter-clear" id="ccClearDueSoonFilter">
            Show All <i class="fa-solid fa-xmark"></i>
          </button>
        </div>
      `;

      const clearBtn = document.getElementById('ccClearDueSoonFilter');
      if (clearBtn) {
        clearBtn.addEventListener('click', () => {
          clearGlobalAccountFilter();
        });
      }
    }
  }

  if (!items.length) {
    list.innerHTML = `
      <li class="cc-due-empty-state">
        <div class="cc-due-empty-icon"><i class="fa-solid fa-circle-check"></i></div>
        <div class="cc-due-empty-title">All tasks on schedule</div>
        <div class="cc-due-empty-sub">${activeAcctName ? `No urgent tasks due for ${esc(activeAcctName)}.` : 'No action items due within the next 7 days.'}</div>
      </li>
    `;
    return;
  }

  list.innerHTML = items.map(rowHtml).join('');

  // Complete listener with interactive animation
  list.querySelectorAll('[data-action="complete"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const card = btn.closest('.cc-due-card');
      const itemId = card ? card.dataset.itemId : null;
      if (!itemId) return;

      btn.disabled = true;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
      if (card) card.classList.add('cc-due-completing');

      try {
        await fetch(`/api/action-items/${itemId}/complete`, { method: 'POST' });
        showToast('Task marked done.');
        refreshTopbarBadge();
        await renderDueSoon();
      } catch (err) {
        console.error(err);
        showToast('Could not update the task.');
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-check"></i> <span>Done</span>';
        if (card) card.classList.remove('cc-due-completing');
      }
    });
  });

  // Remind listener
  list.querySelectorAll('[data-action="remind"]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const card = btn.closest('.cc-due-card');
      const itemId = card ? card.dataset.itemId : null;
      if (!itemId) return;

      btn.disabled = true;
      const originalHtml = btn.innerHTML;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

      try {
        const res = await fetch(`/api/action-items/${itemId}/send-reminder`, { method: 'POST' });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || 'Could not send reminder.');
        showToast(`Reminder sent${data.sent_to ? ` to ${data.sent_to}` : ''}.`);
      } catch (err) {
        console.error(err);
        showToast(err.message || 'Could not send reminder.');
      } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    });
  });
}
