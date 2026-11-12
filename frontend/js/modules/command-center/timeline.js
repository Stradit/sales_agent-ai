import { esc } from './utils.js';
import { logTouch } from './actions.js';
import { loadRecentMovements, markMovementActioned } from './exec-movements.js';
import { renderSkeleton } from '../skeleton.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { openDossier } from './drawer.js';
import { loadMatrixAccounts } from './real-accounts.js';
import { dealsFor } from './deal-context.js';
import { showToast } from '../toast.js';
import { refreshMyDay } from './my-day.js';

const TIMELINE_FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'joined', label: 'Joined' },
  { id: 'promoted', label: 'Promoted' },
  { id: 'aging', label: 'Aging' },
];

function renderTimelineFilters() {
  const wrap = document.getElementById('ccTimelineFilters');
  if (!wrap) return;
  wrap.innerHTML = TIMELINE_FILTERS.map(f => {
    const active = (ccState.timelineFilter || 'all') === f.id;
    return `<button type="button" class="cc-chip cc-chip-xs cc-chip-filter ${active ? 'active' : ''}" data-timeline-filter="${esc(f.id)}">${esc(f.label)}</button>`;
  }).join('');
  wrap.querySelectorAll('[data-timeline-filter]').forEach(btn => {
    btn.addEventListener('click', () => {
      ccState.timelineFilter = btn.dataset.timelineFilter;
      renderTimeline();
    });
  });
}

let modalInitialized = false;
let currentModalItem = null;

function setupCommitteeModal() {
  if (modalInitialized) return;
  modalInitialized = true;
  const modal = document.getElementById('ccCommitteeModal');
  const backdrop = document.getElementById('ccCommitteeBackdrop');
  const closeBtn = document.getElementById('ccCommitteeClose');
  const cancelBtn = document.getElementById('ccCommitteeCancel');
  const form = document.getElementById('ccCommitteeForm');

  const close = () => {
    if (modal) modal.classList.remove('open');
    if (backdrop) backdrop.classList.remove('open');
    currentModalItem = null;
  };

  if (closeBtn) closeBtn.addEventListener('click', close);
  if (cancelBtn) cancelBtn.addEventListener('click', close);
  if (backdrop) backdrop.addEventListener('click', close);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && modal && modal.classList.contains('open')) close();
  });

  if (form) {
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const submitBtn = document.getElementById('ccCommitteeSubmit');
      if (submitBtn) submitBtn.disabled = true;

      try {
        const dealId = form.deal_id.value;
        const role = form.role.value;
        const sentiment = form.sentiment.value;
        const person = form.person.value;
        const accountId = currentModalItem && currentModalItem.accountId;
        const movementId = currentModalItem && currentModalItem.movementId;

        // 1. Try finding person in candidates
        let personaId = null;
        try {
          const candRes = await fetch(`/api/deals/${dealId}/candidates?q=${encodeURIComponent(person)}`);
          if (candRes.ok) {
            const cands = await candRes.json();
            const matched = cands.find(c => (c.name || '').toLowerCase() === person.toLowerCase());
            if (matched) personaId = matched.id;
          }
        } catch (_) {}

        // 2. If not found in candidates, create new contact for this account
        if (!personaId && accountId) {
          try {
            const addRes = await fetch('/api/crm/contacts', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                account_id: Number(accountId),
                full_name: person,
                title: (currentModalItem && currentModalItem.role) || 'Executive',
              })
            });
            if (addRes.ok) {
              const newContact = await addRes.json();
              personaId = newContact.id;
            } else if (addRes.status === 409) {
              const body = await addRes.json().catch(() => ({}));
              const m = String(body.detail || '').match(/id\s+(\d+)/i);
              if (m) personaId = Number(m[1]);
            }
          } catch (_) {}
        }

        if (!personaId) {
          throw new Error(`Could not find or create a contact record for ${person}.`);
        }

        // 3. Add to deal committee
        const postRes = await fetch(`/api/deals/${dealId}/stakeholders`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            persona_id: personaId,
            role,
            sentiment,
          })
        });
        if (!postRes.ok) {
          const body = await postRes.json().catch(() => ({}));
          throw new Error(body.detail || `Failed to add to committee (${postRes.status})`);
        }

        // 4. Mark movement actioned & show toast
        if (movementId) markMovementActioned(movementId);
        showToast(`Added ${person} to buying committee`);
        close();
        document.dispatchEvent(new CustomEvent('cc:plays-changed'));
        refreshMyDay();
        renderTimeline();
      } catch (err) {
        showToast(err.message || 'Could not add to committee');
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }
}

function openCommitteeModal(item) {
  setupCommitteeModal();
  currentModalItem = item;
  const modal = document.getElementById('ccCommitteeModal');
  const backdrop = document.getElementById('ccCommitteeBackdrop');
  if (!modal || !backdrop) return;

  const personInput = document.getElementById('ccCommitteePerson');
  const accountInput = document.getElementById('ccCommitteeAccount');
  const dealSelect = document.getElementById('ccCommitteeDeal');
  const roleSelect = document.getElementById('ccCommitteeRole');
  const sentimentSelect = document.getElementById('ccCommitteeSentiment');

  if (personInput) personInput.value = item.person;
  if (accountInput) accountInput.value = item.company;

  if (dealSelect) {
    const a = dealsFor(item.accountId, item.company);
    const deals = a && a.deals && a.deals.length ? a.deals : [{ deal_id: item.dealId, deal: item.dealName, stage: 'Open' }];
    dealSelect.innerHTML = deals.map(d => `<option value="${d.deal_id}" ${String(d.deal_id) === String(item.dealId) ? 'selected' : ''}>${esc(d.deal)}${d.stage ? ` (${esc(d.stage)})` : ''}</option>`).join('');
  }

  // Pre-select role if title matches
  if (roleSelect) {
    const rLower = (item.role || '').toLowerCase();
    if (rLower.includes('cfo') || rLower.includes('finance') || rLower.includes('budget') || rLower.includes('treasurer')) {
      roleSelect.value = 'economic_buyer';
    } else if (rLower.includes('cto') || rLower.includes('architect') || rLower.includes('security') || rLower.includes('engineer')) {
      roleSelect.value = 'technical_evaluator';
    } else {
      roleSelect.value = 'champion';
    }
  }
  if (sentimentSelect) sentimentSelect.value = 'positive';

  backdrop.classList.add('open');
  modal.classList.add('open');
}

function committeeLink(e) {
  const a = dealsFor(e.accountId, e.company);
  if (!a || !a.deals.length) return '';
  const d = a.deals[0];
  return ` &middot; <button type="button" class="cc-timeline-committee-btn" data-act="committee" data-account-id="${esc(e.accountId || '')}" data-company="${esc(e.company || '')}" data-person="${esc(e.person || '')}" data-role="${esc(e.role || '')}" data-deal-id="${d.deal_id}" data-deal-name="${esc(d.deal)}" data-movement-id="${e.id}" title="Add to buying committee of ${esc(d.deal)}">Add to committee</button>`;
}

function itemHtml(e) {
  const aging = !e.actioned && (Date.now() - e.date.getTime()) / 86400000 >= 5;
  const dotCls = e.type === 'joined' ? 'cc-dot-joined' : (e.type === 'promoted' ? 'cc-dot-promoted' : 'cc-dot-neutral');
  const dateLabel = e.displayDate || e.date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return `
    <li class="cc-timeline-item cc-compact-timeline-item cc-clickable-row ${aging ? 'cc-timeline-item-aging' : ''}" data-account-id="${esc(e.accountId || '')}" data-company="${esc(e.company || '')}">
      <span class="cc-timeline-dot ${dotCls}"></span>
      <div class="cc-timeline-body">
        <div class="cc-timeline-top">
          <span class="cc-timeline-person" title="${esc(e.person)}">${esc(e.person)}</span>
          <span class="cc-chip cc-chip-xs cc-chip-plain">${esc(e.type)}</span>
        </div>
        <div class="cc-timeline-sub">
          <span class="cc-timeline-role" title="${esc(e.role)}">${esc(e.role)}</span>
          <span class="cc-meta-sep">&middot;</span>
          <strong class="cc-company-name" title="${esc(e.company)}">${esc(e.company)}</strong>
        </div>
        <div class="cc-timeline-date">${esc(dateLabel)}${aging ? ' &middot; <span class="cc-warning-text">aging</span>' : ''}${committeeLink(e)}</div>
      </div>
      ${!e.actioned ? `<button type="button" class="cc-btn cc-btn-ghost cc-btn-xs cc-timeline-touch-btn" data-id="${e.id}" title="Log outreach touch">Log touch</button>` : '<span class="cc-touch-badge" title="Touch logged"><i class="fa-solid fa-check"></i></span>'}
    </li>`;
}

export async function renderTimeline() {
  renderTimelineFilters();
  const list = document.getElementById('ccTimelineList');
  if (!list) return;
  list.innerHTML = renderSkeleton('feed-rows');
  let movements;
  try {
    movements = await loadRecentMovements();
  } catch (err) {
    console.error(err);
    list.innerHTML = '<li class="cc-drawer-empty">Could not load exec movements.</li>';
    return;
  }
  if (!movements.length) {
    list.innerHTML = '<li class="cc-drawer-empty">No exec movements in the last 30 days for your accounts.</li>';
    return;
  }

  const hasFilter = (ccState.activeAccountIds && ccState.activeAccountIds.size > 0) || ccState.activeAccountId;
  if (hasFilter) {
    movements = movements.filter(m => matchesCurrentAccount(m.accountId, m.company));
  }

  const filter = ccState.timelineFilter || 'all';
  const filtered = movements.filter(m => {
    if (filter === 'joined') return m.type === 'joined';
    if (filter === 'promoted') return m.type === 'promoted';
    if (filter === 'aging') return !m.actioned && (Date.now() - m.date.getTime()) / 86400000 >= 5;
    if (filter === 'other') return m.type !== 'joined' && m.type !== 'promoted';
    return true;
  });

  if (!filtered.length) {
    list.innerHTML = `<li class="cc-drawer-empty">No movements matching "${filter}".</li>`;
    return;
  }

  list.innerHTML = filtered.map(itemHtml).join('');
  list.querySelectorAll('button[data-id]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const item = movements.find(m => String(m.id) === btn.dataset.id);
      if (!item) return;
      logTouch(item.company, `outreach to ${item.person}`);
      markMovementActioned(item.id);
      renderTimeline();
    });
  });

  list.querySelectorAll('.cc-timeline-committee-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      openCommitteeModal({
        person: btn.dataset.person,
        role: btn.dataset.role,
        accountId: btn.dataset.accountId,
        company: btn.dataset.company,
        dealId: btn.dataset.dealId,
        dealName: btn.dataset.dealName,
        movementId: btn.dataset.movementId,
      });
    });
  });

  // Clicking an exec timeline item opens the account dossier
  list.querySelectorAll('.cc-timeline-item').forEach(row => {
    row.addEventListener('click', async (ev) => {
      if (ev.target.closest('a')) return;
      const acctId = row.dataset.accountId;
      const company = row.dataset.company;
      const accounts = await loadMatrixAccounts().catch(() => []);
      const acct = accounts.find(a => a.id === acctId || a.name?.toLowerCase() === company?.toLowerCase());
      if (acct) openDossier(acct);
    });
  });
}
