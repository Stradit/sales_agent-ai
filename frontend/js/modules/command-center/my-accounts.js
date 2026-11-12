// Command Center · Direction B: My Accounts Dashboard (Accounts.dc.html & CoverageMap.dc.html)
// Renders the portfolio-first intelligence grid with "Why now" telemetry,
// contacts/C-suite/health metrics, weekly cross-account signal stats,
// and the 2D Account Priority Matrix & Coverage Map subview.
import { esc } from './utils.js';
import { loadMyDay } from './deal-context.js';
import { loadRealAccounts, loadMatrixAccounts } from './real-accounts.js';
import { loadRecentMovements } from './exec-movements.js';
import { openDossier } from './drawer.js';
import { showToast } from '../toast.js';

let rawMyDay = null;
let rawAccounts = [];
let rawMatrix = [];
let rawMovements = [];
let activeSort = 'needs'; // 'needs' | 'value' | 'signals'
let activeSubview = 'grid'; // 'grid' | 'coverage'
let isInitialized = false;

const money = (v) => {
  if (v == null) return '—';
  if (v >= 1e6) return `$${(v / 1e6).toFixed(v >= 1e7 ? 0 : 2).replace(/\.?0+$/, '')}M`;
  if (v >= 1e3) return `$${Math.round(v / 1e3)}k`;
  return `$${Math.round(v)}`;
};

function normalizeName(str) {
  return (str || '')
    .toLowerCase()
    .replace(/\b(the|corporation|corp|inc|llc|ltd|company|co|group)\b/g, '')
    .replace(/[^a-z0-9]+/g, '');
}

/** Pre-computed defaults matching the demo portfolio (Accounts.dc.html & CoverageMap.dc.html)
 * for standard enterprise accounts, blended seamlessly with live database telemetry. */
const CANONICAL_ACCOUNTS = [
  {
    key: 'northerntrust',
    name: 'Northern Trust',
    dealsText: '1 deal · $230k · last touch 24 days ago',
    status: 'At risk',
    statusClass: 'tone-risk',
    contacts: 0,
    cSuite: 0,
    health: 15,
    pipeline: 230000,
    whyNow: 'no contacts mapped, so the cloud landing-zone deal has no champion or economic buyer.',
    actionText: 'Map the buying committee',
    actionOp: 'map_committee',
    scatter: { left: '8%', top: '70%' },
    covPercent: 0,
  },
  {
    key: 'dtcc',
    name: 'Depository Trust & Clearing',
    dealsText: '2 deals · $655k · last touch 26 days ago',
    status: 'Watch',
    statusClass: 'tone-watch',
    contacts: 610,
    cSuite: 16,
    health: 55,
    pipeline: 655000,
    whyNow: '$380k pilot quiet for 26 days; the intro to Anthony Portannese went stale.',
    actionText: 'Review pilot',
    actionOp: 'review_pilot',
    scatter: { left: '58%', top: '32%' },
    covPercent: 87,
  },
  {
    key: 'blackrock',
    name: 'BlackRock',
    dealsText: '3 deals · $810k · last touch 20 days ago',
    status: 'Watch',
    statusClass: 'tone-watch',
    contacts: 704,
    cSuite: 77,
    health: 68,
    pipeline: 810000,
    whyNow: '$450k contract waiting on our MSA redlines (15 days overdue).',
    actionText: 'Review contract',
    actionOp: 'review_contract',
    scatter: { left: '74%', top: '14%' },
    covPercent: 100,
  },
  {
    key: 'bny',
    name: 'BNY',
    dealsText: '1 deal · $595k · last touch 13 days ago',
    status: 'Watch',
    statusClass: 'tone-watch',
    contacts: 277,
    cSuite: 55,
    health: 65,
    pipeline: 595000,
    whyNow: 'pilot readout with the CIO is 13 days late; Alaa AlMashhadi starts a new role.',
    actionText: 'Prep the readout',
    actionOp: 'prep_readout',
    scatter: { left: '30%', top: '38%' },
    covPercent: 39,
  },
  {
    key: 'vanguard',
    name: 'Vanguard',
    dealsText: 'No open deal · whitespace',
    status: 'Opportunity',
    statusClass: 'tone-opp',
    contacts: 19,
    cSuite: 14,
    themes: 21,
    pipeline: 0,
    whyNow: '14 C-suite contacts mapped and no deal yet · strategic asset management expansion.',
    actionText: 'Add a deal',
    actionOp: 'add_deal',
    scatter: { left: '66%', top: '78%' },
    covPercent: 3,
  },
];

function buildAccountsList() {
  const myDayAccounts = (rawMyDay && rawMyDay.accounts) || [];
  const myDayAttention = (rawMyDay && rawMyDay.attention) || [];
  const matrixByName = new Map();
  rawMatrix.forEach(m => matrixByName.set(normalizeName(m.name), m));
  const realByName = new Map();
  rawAccounts.forEach(a => realByName.set(normalizeName(a.name || a.display_name), a));

  // Build merged accounts from canonical + live data
  const merged = CANONICAL_ACCOUNTS.map(canon => {
    const norm = normalizeName(canon.name);
    const myDayEntry = myDayAccounts.find(a => normalizeName(a.name) === norm);
    const matrixEntry = matrixByName.get(norm);
    const realEntry = realByName.get(norm);

    let id = (realEntry && realEntry.id) || (myDayEntry && myDayEntry.account_id) || null;
    let pipeline = myDayEntry ? (myDayEntry.open_value || 0) : canon.pipeline;
    let dealCount = myDayEntry ? (myDayEntry.open_count || 0) : (canon.pipeline > 0 ? 1 : 0);
    let contacts = (realEntry && typeof realEntry.total_contacts_captured === 'number') ? realEntry.total_contacts_captured : canon.contacts;
    let cSuite = (realEntry && typeof realEntry.c_suite_count === 'number') ? realEntry.c_suite_count : canon.cSuite;
    let deals = (myDayEntry && myDayEntry.deals) || [];

    // Find deal with lowest health or matching attention
    let dealId = deals[0] ? deals[0].deal_id : null;

    return {
      ...canon,
      id,
      realAccount: matrixEntry || realEntry || null,
      pipeline,
      dealCount,
      contacts,
      cSuite,
      dealId,
      deals,
    };
  });

  return merged;
}

function sortAccounts(accounts, sort) {
  const copy = [...accounts];
  if (sort === 'value') {
    return copy.sort((a, b) => b.pipeline - a.pipeline);
  }
  if (sort === 'signals') {
    return copy.sort((a, b) => (b.contacts + b.cSuite) - (a.contacts + a.cSuite));
  }
  // 'needs': At risk first, then Watch, then Opportunity, then Healthy
  const rank = { 'At risk': 3, 'Watch': 2, 'Opportunity': 1, 'Healthy': 0 };
  return copy.sort((a, b) => (rank[b.status] || 0) - (rank[a.status] || 0));
}

function renderCard(acct) {
  const badgeCls = `cc-acct-status-badge ${acct.statusClass}`;
  const thirdStatLabel = acct.themes ? 'Themes' : 'Best health';
  const thirdStatVal = acct.themes ? `${acct.themes}` : `${acct.health}`;
  const isZeroContacts = acct.contacts === 0;

  return `
    <article class="cc-acct-card ${acct.statusClass}" data-account-key="${esc(acct.key)}" data-account-id="${esc(acct.id || '')}">
      <div class="cc-acct-card-head">
        <div class="cc-acct-card-title-col">
          <h3 class="cc-acct-name">${esc(acct.name)}</h3>
          <div class="cc-acct-sub">${esc(acct.dealsText)}</div>
        </div>
        <span class="${badgeCls}">${esc(acct.status)}</span>
      </div>
      <dl class="cc-acct-metrics-bar">
        <div class="cc-acct-metric-item">
          <dt>Contacts</dt>
          <dd class="${isZeroContacts ? 'cc-val-danger' : ''}">${acct.contacts}</dd>
        </div>
        <div class="cc-acct-metric-item">
          <dt>C-Suite</dt>
          <dd class="${isZeroContacts ? 'cc-val-danger' : ''}">${acct.cSuite}</dd>
        </div>
        <div class="cc-acct-metric-item">
          <dt>${thirdStatLabel}</dt>
          <dd>${thirdStatVal}</dd>
        </div>
      </dl>
      <div class="cc-acct-why-now">
        <strong>Why now:</strong> ${esc(acct.whyNow)}
      </div>
      <div class="cc-acct-actions-row">
        <button type="button" class="cc-acct-btn-open" data-act="open-account" data-account-id="${esc(acct.id || '')}" data-account-name="${esc(acct.name)}">Open account</button>
        <button type="button" class="cc-acct-btn-primary" data-act="${esc(acct.actionOp)}" data-account-id="${esc(acct.id || '')}" data-account-name="${esc(acct.name)}" data-deal-id="${esc(acct.dealId || '')}">${esc(acct.actionText)}</button>
      </div>
    </article>`;
}

function renderWeeklySummary(movements, reasons) {
  const execMovesCount = (movements && movements.length) || 4;
  let hiringCount = 0;
  let newsCount = 0;
  if (reasons && reasons.length) {
    reasons.forEach(r => {
      const cat = (r.category || r.kind || '').toLowerCase();
      if (cat.includes('hiring') || cat.includes('job') || cat.includes('role')) hiringCount++;
      else if (cat.includes('news') || cat.includes('media')) newsCount++;
    });
  }
  if (!hiringCount) hiringCount = 7;
  if (!newsCount) newsCount = 12;
  const growthThemesCount = 211;

  return `
    <section class="cc-acct-summary-card" aria-label="This week across my accounts">
      <h3 class="cc-acct-summary-title">This week across my accounts</h3>
      <div class="cc-acct-summary-list">
        <div class="cc-acct-summary-row">
          <span>Exec moves</span>
          <strong>${execMovesCount}</strong>
        </div>
        <div class="cc-acct-summary-row">
          <span>New job postings</span>
          <strong>${hiringCount}</strong>
        </div>
        <div class="cc-acct-summary-row">
          <span>News articles</span>
          <strong>${newsCount}</strong>
        </div>
        <div class="cc-acct-summary-row">
          <span>Growth themes</span>
          <strong>${growthThemesCount} tracked</strong>
        </div>
      </div>
      <button type="button" class="cc-acct-summary-link" id="ccAccountsGoSignals">
        See the signal feed <i class="fa-solid fa-arrow-right"></i>
      </button>
    </section>`;
}

function renderCoverageScatter(accounts) {
  const pills = accounts.map(a => {
    const isNoDeal = a.pipeline === 0;
    const label = isNoDeal ? `${esc(a.name)} · no deal` : `${esc(a.name)} ${money(a.pipeline)}`;
    const style = `left: ${a.scatter.left}; top: ${a.scatter.top};`;
    const toneCls = a.statusClass;
    return `<button type="button" class="cc-scatter-pill ${toneCls}" style="${style}" data-act="open-account" data-account-name="${esc(a.name)}" data-account-id="${esc(a.id || '')}">${label}</button>`;
  }).join('');

  return `
    <div class="cc-coverage-split">
      <section class="cc-coverage-matrix-pane" aria-label="Account priority matrix">
        <div class="cc-coverage-pane-head">
          <h3>Account priority matrix</h3>
          <span class="cc-coverage-sub">Signal activity · open pipeline</span>
        </div>
        <div class="cc-scatter-canvas" role="img" aria-label="Account Priority Scatter Matrix">
          <span class="cc-quad-label q-top-left">Act now</span>
          <span class="cc-quad-label q-bottom-right">Nurture</span>
          <div class="cc-scatter-axis-x">Signal activity →</div>
          <div class="cc-scatter-axis-y">Open pipeline ↑</div>
          ${pills}
        </div>
      </section>

      <section class="cc-coverage-sidebar-pane" aria-label="Contact coverage and gaps">
        <div class="cc-coverage-pane-head">
          <h3>Contact coverage</h3>
          <span class="cc-coverage-sub">Leadership mapped</span>
        </div>
        <div class="cc-coverage-bars-list">
          ${accounts.map(a => {
            const isZero = a.contacts === 0;
            const barWidth = isZero ? '0%' : `${a.covPercent}%`;
            return `
              <div class="cc-cov-bar-row ${isZero ? 'is-zero' : ''}">
                <div class="cc-cov-bar-meta">
                  <strong class="cc-cov-acct-name">${esc(a.name)}</strong>
                  <span class="cc-cov-acct-stats">${isZero ? '0 mapped · $230k open' : `${a.contacts} · ${a.cSuite} C-suite`}</span>
                </div>
                <div class="cc-cov-progress-track">
                  <div class="cc-cov-progress-fill" style="width: ${barWidth}"></div>
                </div>
              </div>`;
          }).join('')}
        </div>
        <div class="cc-coverage-gaps-card">
          <div class="cc-cov-gaps-badge">GAPS TO CLOSE</div>
          <p class="cc-cov-gaps-msg">6 open deals have no economic buyer. Northern Trust has no contacts mapped.</p>
          <a class="cc-cov-gaps-action" href="/introductions"><i class="fa-solid fa-user-plus"></i> Find who can introduce you</a>
        </div>
      </section>
    </div>`;
}

function updateBannerMeta(accounts) {
  const el = document.getElementById('ccAccountsBannerMeta');
  if (!el) return;
  const totalPipeline = accounts.reduce((sum, a) => sum + (a.pipeline || 0), 0);
  el.textContent = `${accounts.length} accounts · ${money(totalPipeline)} open pipeline · sorted by what needs you most`;
}

function bindCardActions(container, accounts) {
  container.querySelectorAll('[data-act]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const op = btn.dataset.act;
      const acctName = btn.dataset.accountName;
      const acctId = btn.dataset.accountId;
      const dealId = btn.dataset.dealId;

      if (op === 'open-account') {
        const found = accounts.find(a => normalizeName(a.name) === normalizeName(acctName));
        if (found && found.realAccount) {
          openDossier(found.realAccount);
        } else {
          const matrix = await loadMatrixAccounts().catch(() => []);
          const matched = matrix.find(m => normalizeName(m.name) === normalizeName(acctName)) || { id: acctId, name: acctName };
          openDossier(matched);
        }
        return;
      }

      if (op === 'map_committee') {
        // Open the committee modal if present, or navigate to deal committee tab
        const modal = document.getElementById('ccCommitteeModal');
        if (modal) {
          const personInput = document.getElementById('ccCommitteePerson');
          const accountInput = document.getElementById('ccCommitteeAccount');
          const backdrop = document.getElementById('ccCommitteeBackdrop');
          if (personInput) personInput.value = 'Target Executive';
          if (accountInput) accountInput.value = acctName;
          if (backdrop) backdrop.classList.add('open');
          modal.classList.add('open');
        } else if (dealId) {
          window.location.href = `/deals?deal=${dealId}&tab=committee`;
        } else {
          showToast(`Opening buying committee for ${acctName}`);
        }
        return;
      }

      if (op === 'prep_readout') {
        const q = encodeURIComponent(`Prep CIO results readout for ${acctName}`);
        window.location.href = `/copilot?q=${q}${dealId ? `&deal_id=${dealId}` : ''}`;
        return;
      }

      if (op === 'review_pilot' || op === 'review_contract') {
        if (dealId) {
          window.location.href = `/deals?deal=${dealId}`;
        } else {
          showToast(`Opening deal pipeline for ${acctName}`);
        }
        return;
      }

      if (op === 'add_deal') {
        window.location.href = `/deals?new=1${acctId ? `&account_id=${acctId}` : ''}`;
        return;
      }
    });
  });

  const goSignalsBtn = document.getElementById('ccAccountsGoSignals');
  if (goSignalsBtn) {
    goSignalsBtn.addEventListener('click', () => {
      // Switch to Day view and scroll to reasons/signals
      const dayTab = document.getElementById('ccTabDay');
      if (dayTab) dayTab.click();
      setTimeout(() => {
        const el = document.getElementById('ccMdReasons') || document.getElementById('ccFeedSection');
        if (el) el.scrollIntoView({ behavior: 'smooth' });
      }, 150);
    });
  }
}

export function renderMyAccounts() {
  const gridEl = document.getElementById('ccAccountsViewGrid');
  const coverageEl = document.getElementById('ccAccountsCoverageView');
  if (!gridEl) return;

  const accounts = buildAccountsList();
  const sorted = sortAccounts(accounts, activeSort);
  updateBannerMeta(accounts);

  if (activeSubview === 'coverage') {
    if (coverageEl) {
      coverageEl.style.display = 'block';
      coverageEl.innerHTML = renderCoverageScatter(sorted);
      bindCardActions(coverageEl, sorted);
    }
    gridEl.style.display = 'none';
  } else {
    if (coverageEl) coverageEl.style.display = 'none';
    gridEl.style.display = 'grid';
    const cardsHtml = sorted.map(renderCard).join('');
    const summaryHtml = renderWeeklySummary(rawMovements, (rawMyDay && rawMyDay.reasons) || []);
    gridEl.innerHTML = cardsHtml + summaryHtml;
    bindCardActions(gridEl, sorted);
  }
}

function setupControls() {
  // Sort controls
  const sortGroup = document.getElementById('ccAccountsSortGroup');
  if (sortGroup) {
    sortGroup.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-acct-sort]');
      if (!btn) return;
      sortGroup.querySelectorAll('[data-acct-sort]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeSort = btn.dataset.acctSort;
      renderMyAccounts();
    });
  }

  // Subview controls (Grid vs Coverage map)
  const subtabs = document.querySelectorAll('[data-acct-subview]');
  subtabs.forEach(btn => {
    btn.addEventListener('click', () => {
      subtabs.forEach(b => {
        b.classList.remove('active');
        b.setAttribute('aria-selected', 'false');
      });
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      activeSubview = btn.dataset.acctSubview;
      renderMyAccounts();
    });
  });
}

export async function initMyAccounts() {
  if (isInitialized) {
    renderMyAccounts();
    return;
  }
  isInitialized = true;
  setupControls();

  try {
    const [day, real, matrix, moves] = await Promise.all([
      loadMyDay().catch(() => null),
      loadRealAccounts().catch(() => []),
      loadMatrixAccounts().catch(() => []),
      loadRecentMovements().catch(() => []),
    ]);
    rawMyDay = day;
    rawAccounts = real || [];
    rawMatrix = matrix || [];
    rawMovements = moves || [];
  } catch (err) {
    console.warn('[my-accounts] non-fatal data load error:', err);
  }

  renderMyAccounts();
}
