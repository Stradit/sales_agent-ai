import { esc, formatMoney } from './utils.js';
import { ccState } from './state.js';
import { logTouch, createTask, isTasked, markAsTasked } from './actions.js';
import { loadRecentMovements } from './exec-movements.js';
import { renderSkeleton } from '../skeleton.js';

function el(id) { return document.getElementById(id); }

function metricsHtml(account) {
  return `
    <div class="cc-drawer-metrics">
      <div class="cc-drawer-metric"><div class="cc-drawer-metric-label">Composite score</div><div class="cc-drawer-metric-value">${account.compositeScore}</div></div>
      <div class="cc-drawer-metric"><div class="cc-drawer-metric-label">Signal strength</div><div class="cc-drawer-metric-value">${Math.round(account.signalStrength)}</div></div>
      <div class="cc-drawer-metric"><div class="cc-drawer-metric-label">Engagement recency</div><div class="cc-drawer-metric-value">${Math.round(account.engagementRecency)}</div></div>
      <div class="cc-drawer-metric">
        <div class="cc-drawer-metric-label">Deal potential${account.dealPotentialEstimated ? ' (est.)' : ''}</div>
        <div class="cc-drawer-metric-value">${formatMoney(account.dealPotential)}</div>
      </div>
    </div>
    ${account.dealPotentialEstimated ? '<div class="cc-drawer-note">Deal potential is an estimate based on account score and mapped contacts — there\'s no CRM pipeline value on file.</div>' : ''}`;
}

function execRowsHtml(movements) {
  if (!movements.length) return '<div class="cc-drawer-empty">No exec movements in the last 30 days.</div>';
  return movements.map(m => `
    <div class="cc-drawer-exec-row">
      <span class="cc-badge ${m.type === 'joined' ? 'cc-badge-joined' : 'cc-badge-neutral'}">${esc(m.type)}</span>
      <span>${esc(m.person)} &middot; ${esc(m.role)}</span>
    </div>`).join('');
}

export async function openDossier(account) {
  if (!account) return;
  ccState.drawerAccountId = account.id;
  el('ccDrawerTitle').textContent = account.ticker ? `${account.name} (${account.ticker})` : account.name;
  el('ccDrawerBody').innerHTML = metricsHtml(account) + `
    <div class="cc-drawer-section">
      <div class="cc-drawer-section-label">Exec movements (30d)</div>
      <div id="ccDrawerExecMovements">${renderSkeleton('lines')}</div>
    </div>
    <div class="cc-drawer-section">
      <a class="cc-btn cc-btn-ghost" href="/?account_key=${encodeURIComponent(account.key || account.ticker || account.name)}">Open full account view <i class="fa-solid fa-arrow-right"></i></a>
    </div>
    <div class="cc-drawer-footer-actions">
      <button type="button" class="cc-btn cc-btn-ghost" id="ccDrawerLogTouch">Log touch</button>
      ${(() => {
        const title = `Follow up with ${account.name}`;
        const tasked = isTasked(account.name, title, account.id);
        const btnCls = tasked ? 'cc-btn-done' : 'cc-btn-primary';
        const btnHtml = tasked ? '<i class="fa-solid fa-check"></i> Tasked' : 'Create task';
        return `<button type="button" class="cc-btn ${btnCls}" id="ccDrawerCreateTask" ${tasked ? 'disabled' : ''}>${btnHtml}</button>`;
      })()}
    </div>`;
  el('ccDrawer').classList.add('open');
  el('ccDrawerBackdrop').classList.add('open');

  el('ccDrawerLogTouch').addEventListener('click', () => logTouch(account.name));
  el('ccDrawerCreateTask').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const title = `Follow up with ${account.name}`;
    const ok = await createTask(account.name, title, { accountId: account.id, score: account.compositeScore, source: 'dossier' });
    if (ok) {
      markAsTasked(account.name, title, account.id);
      btn.innerHTML = '<i class="fa-solid fa-check"></i> Tasked';
      btn.classList.remove('cc-btn-primary');
      btn.classList.add('cc-btn-done');
      btn.disabled = true;
    } else {
      btn.disabled = false;
    }
  });

  try {
    const movements = (await loadRecentMovements()).filter(m => m.accountId === account.id);
    const target = el('ccDrawerExecMovements');
    if (target && ccState.drawerAccountId === account.id) target.innerHTML = execRowsHtml(movements);
  } catch (err) {
    console.error(err);
    const target = el('ccDrawerExecMovements');
    if (target) target.innerHTML = '<div class="cc-drawer-empty">Could not load exec movements.</div>';
  }
}

export function closeDossier() {
  el('ccDrawer').classList.remove('open');
  el('ccDrawerBackdrop').classList.remove('open');
  ccState.drawerAccountId = null;
}

export function initDrawer() {
  el('ccDrawerClose').addEventListener('click', closeDossier);
  el('ccDrawerBackdrop').addEventListener('click', closeDossier);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDossier(); });
}
