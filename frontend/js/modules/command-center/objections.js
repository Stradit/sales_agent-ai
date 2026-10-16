// Operational Pain Points & Common Objections Widgets
// Enterprise B2B SaaS Clean Layout — Previous Data Only, Perfectly Arranged
import { esc } from './utils.js';
import { renderSkeleton } from '../skeleton.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { loadRealAccounts } from './real-accounts.js';
import { clearGlobalAccountFilter } from './account-filter.js';

let objectionsPromise = null;

export function loadObjectionsData() {
  if (!objectionsPromise) {
    objectionsPromise = fetch('/api/objections')
      .then(res => res.ok ? res : fetch('/api/command-center/objections'))
      .then(res => {
        if (!res.ok) throw new Error(`Failed to load objections: ${res.status}`);
        return res.json();
      });
  }
  return objectionsPromise;
}

function getAccountList(item, realAccounts) {
  const accountMap = new Map();

  (item.personas || []).forEach(p => {
    if (p.account && !accountMap.has(p.account)) {
      const url = p.account_id
        ? `/?account=${encodeURIComponent(p.account_id)}`
        : `/?account_key=${encodeURIComponent(p.account)}&tab=personas`;
      accountMap.set(p.account, { name: p.account, url });
    }
  });

  (item.accounts || []).forEach(name => {
    if (!accountMap.has(name)) {
      let url = `/?account_key=${encodeURIComponent(name)}&tab=personas`;
      if (realAccounts && realAccounts.length) {
        const target = name.toLowerCase().trim();
        const found = realAccounts.find(r => {
          const rn = (r.name || r.display_name || '').toLowerCase().trim();
          return rn === target || rn.includes(target) || target.includes(rn);
        });
        if (found) {
          url = `/?account=${encodeURIComponent(found.id)}`;
        }
      }
      accountMap.set(name, { name, url });
    }
  });

  return Array.from(accountMap.values());
}

function matchesActiveAccount(item, realAccounts) {
  const hasFilter = (ccState.activeAccountIds && ccState.activeAccountIds.size > 0) || ccState.activeAccountId;
  if (!hasFilter) return true;

  // Check personas
  for (const p of (item.personas || [])) {
    if (matchesCurrentAccount(p.account_id, p.account)) return true;
  }

  // Check accounts
  for (const accName of (item.accounts || [])) {
    if (matchesCurrentAccount(null, accName)) return true;
  }

  return false;
}

function getSeverity(count) {
  if (count >= 200) return { cls: 'cc-sev-critical', dotCls: 'dot-critical' };
  if (count >= 50) return { cls: 'cc-sev-high', dotCls: 'dot-high' };
  return { cls: 'cc-sev-med', dotCls: 'dot-med' };
}

function renderFilterBar(filteredCount, totalCount, onClearId) {
  const hasFilter = (ccState.activeAccountIds && ccState.activeAccountIds.size > 0) || ccState.activeAccountId;
  if (!hasFilter) return '';
  const names = ccState.selectedAccountNames || [];
  const label = names.length === 1 ? names[0] : (names.length > 1 ? `${names.length} Selected Accounts` : (ccState.selectedAccountName || 'Selected Accounts'));
  return `
    <div class="cc-intel-filter-strip">
      <span class="cc-intel-filter-info">
        <i class="fa-solid fa-filter"></i> Filtered by <strong>${esc(label)}</strong> (${filteredCount} of ${totalCount})
      </span>
      <button type="button" class="cc-intel-filter-clear" id="${onClearId}">
        Show All <i class="fa-solid fa-xmark"></i>
      </button>
    </div>
  `;
}

function renderPainPointsList(items, realAccounts, emptyText) {
  if (!items || !items.length) {
    return `<div class="cc-drawer-empty">${esc(emptyText)}</div>`;
  }

  const filtered = items.filter(it => matchesActiveAccount(it, realAccounts));
  if (!filtered.length) {
    return `
      ${renderFilterBar(0, items.length, 'ccClearPainFilter')}
      <div class="cc-drawer-empty">No pain points recorded for <strong>${esc(ccState.selectedAccountName || 'this account')}</strong>.</div>
    `;
  }

  return `
    ${renderFilterBar(filtered.length, items.length, 'ccClearPainFilter')}
    <div class="cc-intel-scroll-wrap">
      <ul class="cc-intel-card-list">
        ${filtered.slice(0, 10).map((item, idx) => {
          const accountList = getAccountList(item, realAccounts);
          const count = item.count || 1;
          const sev = getSeverity(count);

          return `
            <li class="cc-intel-card cc-pain-card ${sev.cls}">
              <div class="cc-intel-row-top">
                <div class="cc-intel-lead">
                  <span class="cc-intel-rank">#${String(idx + 1).padStart(2, '0')}</span>
                  <span class="cc-intel-title" title="${esc(item.text)}">${esc(item.text)}</span>
                </div>
              </div>
              <div class="cc-intel-row-bottom">
                <span class="cc-intel-label"><i class="fa-regular fa-building"></i> Target Accounts:</span>
                <div class="cc-intel-accounts">
                  ${accountList.map(acc => `
                    <a href="${acc.url}" class="cc-account-pill" title="View Dossier for ${esc(acc.name)}">
                      <span class="cc-acc-name">${esc(acc.name)}</span>
                      <i class="fa-solid fa-arrow-up-right-from-square cc-ext-icon"></i>
                    </a>
                  `).join('')}
                </div>
              </div>
            </li>
          `;
        }).join('')}
      </ul>
    </div>
  `;
}

function renderObjectionsList(items, realAccounts, emptyText) {
  if (!items || !items.length) {
    return `<div class="cc-drawer-empty">${esc(emptyText)}</div>`;
  }

  const filtered = items.filter(it => matchesActiveAccount(it, realAccounts));
  if (!filtered.length) {
    return `
      ${renderFilterBar(0, items.length, 'ccClearObjFilter')}
      <div class="cc-drawer-empty">No objections recorded for <strong>${esc(ccState.selectedAccountName || 'this account')}</strong>.</div>
    `;
  }

  return `
    ${renderFilterBar(filtered.length, items.length, 'ccClearObjFilter')}
    <div class="cc-intel-scroll-wrap">
      <ul class="cc-intel-card-list">
        ${filtered.slice(0, 10).map((item, idx) => {
          const accountList = getAccountList(item, realAccounts);
          const count = item.count || 1;

          return `
            <li class="cc-intel-card cc-objection-card">
              <div class="cc-intel-row-top">
                <div class="cc-intel-lead">
                  <span class="cc-intel-rank cc-rank-obj">#${String(idx + 1).padStart(2, '0')}</span>
                  <span class="cc-intel-title cc-obj-text" title="${esc(item.text)}">
                    <i class="fa-solid fa-quote-left cc-quote-icon"></i> "${esc(item.text)}"
                  </span>
                </div>
              </div>
              <div class="cc-intel-row-bottom">
                <span class="cc-intel-label"><i class="fa-regular fa-building"></i> Target Accounts:</span>
                <div class="cc-intel-accounts">
                  ${accountList.map(acc => `
                    <a href="${acc.url}" class="cc-account-pill" title="View Dossier for ${esc(acc.name)}">
                      <span class="cc-acc-name">${esc(acc.name)}</span>
                      <i class="fa-solid fa-arrow-up-right-from-square cc-ext-icon"></i>
                    </a>
                  `).join('')}
                </div>
              </div>
            </li>
          `;
        }).join('')}
      </ul>
    </div>
  `;
}

function bindClearHandlers() {
  const clearHandler = () => {
    clearGlobalAccountFilter();
  };

  const btn1 = document.getElementById('ccClearPainFilter');
  if (btn1) btn1.addEventListener('click', clearHandler);

  const btn2 = document.getElementById('ccClearObjFilter');
  if (btn2) btn2.addEventListener('click', clearHandler);
}

export async function renderObjections() {
  const painPointsBody = document.getElementById('ccPainPointsBody');
  const objectionsBody = document.getElementById('ccObjectionsBody');

  if (painPointsBody) painPointsBody.innerHTML = renderSkeleton('lines');
  if (objectionsBody) objectionsBody.innerHTML = renderSkeleton('lines');

  let data, realAccounts = [];
  try {
    [data, realAccounts] = await Promise.all([
      loadObjectionsData(),
      loadRealAccounts().catch(() => []),
    ]);
  } catch (err) {
    console.error('Failed to load objections & pain points data:', err);
    if (painPointsBody) painPointsBody.innerHTML = '<div class="cc-drawer-empty">Could not load pain points data.</div>';
    if (objectionsBody) objectionsBody.innerHTML = '<div class="cc-drawer-empty">Could not load objections data.</div>';
    return;
  }

  if (painPointsBody) {
    painPointsBody.innerHTML = renderPainPointsList(
      data.pain_points || [],
      realAccounts,
      'No operational pain points captured yet for your accounts.'
    );
  }
  if (objectionsBody) {
    objectionsBody.innerHTML = renderObjectionsList(
      data.objections || [],
      realAccounts,
      'No common objections captured yet for your accounts.'
    );
  }

  bindClearHandlers();
}
