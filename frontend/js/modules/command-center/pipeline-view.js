// Command Center · Direction C: Pipeline + Signals Cockpit (Pipeline.dc.html & DealsPhone.dc.html)
// Proportional stage funnel ribbon, interactive split cockpit deals table,
// live telemetry diff inspector ("What changed since you last spoke"), and suggested next steps.
import { esc } from './utils.js';
import { loadMyDay } from './deal-context.js';
import { loadRecentMovements } from './exec-movements.js';
import { logTouch } from './actions.js';
import { showToast } from '../toast.js';

let rawMyDay = null;
let rawMovements = [];
let selectedDealKey = 'bny-pilot';
let activeStageFilter = 'all';
let isInitialized = false;

const money = (v) => {
  if (v == null) return '—';
  if (v >= 1e6) return `$${(v / 1e6).toFixed(v >= 1e7 ? 0 : 2).replace(/\.?0+$/, '')}M`;
  if (v >= 1e3) return `$${Math.round(v / 1e3)}k`;
  return `$${Math.round(v)}`;
};

const healthTone = (h) => (h == null ? 'muted' : h < 40 ? 'risk' : h < 70 ? 'watch' : 'good');

const CANONICAL_DEALS = [
  {
    key: 'bny-pilot',
    id: 1,
    name: 'BNY — Applied AI governance pilot',
    account: 'BNY',
    stage: 'pilot',
    stageLabel: 'Pilot',
    value: 320000,
    health: 65,
    expectedClose: '17 Dec 2026',
    pilotProgress: '0% of pilot exit criteria done (0 of 4)',
    nextStep: 'CIO readout',
    nextStepLate: '13 d late',
    lastTouch: '12 d',
    newSinceThen: [
      { type: 'exec', label: '1 exec move', detail: 'Alaa AlMashhadi starts a new role — check whether they join the buying committee.' },
      { type: 'news', label: '2 news', detail: 'BNY expands enterprise AI governance board following regulatory guidelines.' }
    ],
    suggestedStep: 'Reschedule the CIO readout and open with the leadership change; tick the pilot exit criteria you can already show.'
  },
  {
    key: 'blackrock-contract',
    id: 2,
    name: 'BlackRock — AI agent testing framework',
    account: 'BlackRock',
    stage: 'contract',
    stageLabel: 'Contract',
    value: 450000,
    health: 45,
    expectedClose: '30 Nov 2026',
    nextStep: 'MSA redlines',
    nextStepLate: '14 d late',
    lastTouch: '20 d',
    newSinceThen: [
      { type: 'hiring', label: '4 hiring', detail: '4 new senior cloud and AI testing roles posted in NYC.' }
    ],
    suggestedStep: 'Return MSA redlines to BlackRock legal team; confirm economic buyer sign-off before month-end.'
  },
  {
    key: 'dtcc-pilot',
    id: 3,
    name: 'DTCC — Digital assets settlement pilot',
    account: 'DTCC',
    stage: 'pilot',
    stageLabel: 'Pilot',
    value: 380000,
    health: 55,
    expectedClose: '15 Jan 2027',
    pilotProgress: '25% of pilot exit criteria done (1 of 4)',
    nextStep: 'Success criteria',
    nextStepLate: '12 d late',
    lastTouch: '26 d',
    lastTouchLate: true,
    newSinceThen: [
      { type: 'news', label: '3 news', detail: 'DTCC announces digital securities post-trade settlement pilot program.' }
    ],
    suggestedStep: 'Re-engage sponsor on agreed pilot success criteria; leverage the digital settlement news hook.'
  },
  {
    key: 'dtcc-recon',
    id: 4,
    name: 'DTCC — AI-assisted reconciliation',
    account: 'DTCC',
    stage: 'proposal',
    stageLabel: 'Proposal · no economic buyer',
    value: 275000,
    health: 50,
    expectedClose: '28 Feb 2027',
    nextStep: 'Map exceptions',
    nextStepLate: '21 d late',
    lastTouch: '52 d',
    lastTouchLate: true,
    newSinceThen: [],
    suggestedStep: 'Find the economic buyer; reconnect after the stale introduction to Anthony Portannese.'
  },
  {
    key: 'nt-landing',
    id: 5,
    name: 'Northern Trust — Secure cloud landing zone',
    account: 'Northern Trust',
    stage: 'proposal',
    stageLabel: 'Proposal · no contacts mapped',
    value: 230000,
    health: 15,
    expectedClose: '15 Dec 2026',
    nextStep: 'Security questionnaire',
    nextStepLate: '6 d late',
    lastTouch: '24 d',
    newSinceThen: [],
    suggestedStep: 'Map buying committee immediately; request intro through executive network to unblock security review.'
  },
  {
    key: 'blackrock-emea',
    id: 6,
    name: 'BlackRock — Cloud cost optimisation (EMEA)',
    account: 'BlackRock',
    stage: 'proposal',
    stageLabel: 'Proposal',
    value: 210000,
    health: 30,
    expectedClose: '20 Jan 2027',
    nextStep: 'EMEA workshop',
    nextStepLate: '8 d late',
    lastTouch: '43 d',
    lastTouchLate: true,
    newSinceThen: [
      { type: 'hiring', label: '3 hiring', detail: '3 new cloud infrastructure directors hired in London office.' }
    ],
    suggestedStep: 'Reach out to newly hired EMEA cloud directors to reschedule the scoping workshop.'
  },
  {
    key: 'bny-fund',
    id: 7,
    name: 'BNY — Fund accounting data quality',
    account: 'BNY',
    stage: 'proposal',
    stageLabel: 'Proposal',
    value: 180000,
    health: 50,
    expectedClose: '10 Feb 2027',
    nextStep: 'Revised proposal',
    nextStepLate: '15 d late',
    lastTouch: '22 d',
    newSinceThen: [
      { type: 'exec', label: '1 exec move', detail: 'Alaa AlMashhadi starts a new role in asset servicing.' }
    ],
    suggestedStep: 'Deliver revised pricing proposal tailored to the new asset servicing leadership structure.'
  },
  {
    key: 'blackrock-aladdin',
    id: 8,
    name: 'BlackRock — Aladdin data integration',
    account: 'BlackRock',
    stage: 'intro',
    stageLabel: 'Intro',
    value: 150000,
    health: 70,
    expectedClose: '30 Mar 2027',
    nextStep: 'Introductory readout',
    nextStepLate: 'due next week',
    lastTouch: '5 d',
    newSinceThen: [],
    suggestedStep: 'Confirm architecture overview meeting with Aladdin team.'
  },
  {
    key: 'dtcc-cyber',
    id: 9,
    name: 'DTCC — Cyber resilience assessment',
    account: 'DTCC',
    stage: 'discovery',
    stageLabel: 'Discovery',
    value: 95000,
    health: 65,
    expectedClose: '15 Apr 2027',
    nextStep: 'Scoping questionnaire',
    nextStepLate: 'due in 4 d',
    lastTouch: '7 d',
    newSinceThen: [],
    suggestedStep: 'Send assessment questionnaire prior to discovery call.'
  }
];

function getMergedDeals() {
  const liveAccounts = (rawMyDay && rawMyDay.accounts) || [];
  const liveDealsMap = new Map();
  liveAccounts.forEach(a => {
    (a.deals || []).forEach(d => {
      liveDealsMap.set(d.deal_id, d);
    });
  });

  return CANONICAL_DEALS.map(deal => {
    const live = liveDealsMap.get(deal.id);
    if (!live) return deal;
    return {
      ...deal,
      name: live.deal || deal.name,
      value: live.value != null ? live.value : deal.value,
      stage: (live.stage || deal.stage).toLowerCase(),
      stageLabel: live.stage || deal.stageLabel,
      pilotProgress: live.pilot_exit_text || deal.pilotProgress,
    };
  });
}

function computeStageFunnel(deals) {
  const stages = [
    { id: 'intro', label: 'Intro', deals: deals.filter(d => d.stage === 'intro'), cls: 'seg-intro' },
    { id: 'discovery', label: 'Disc.', deals: deals.filter(d => d.stage === 'discovery'), cls: 'seg-disc' },
    { id: 'proposal', label: 'Proposal', deals: deals.filter(d => d.stage === 'proposal'), cls: 'seg-prop' },
    { id: 'pilot', label: 'Pilot', deals: deals.filter(d => d.stage === 'pilot'), cls: 'seg-pilot' },
    { id: 'contract', label: 'Contract', deals: deals.filter(d => d.stage === 'contract'), cls: 'seg-contract' },
  ];

  stages.forEach(st => {
    st.val = st.deals.reduce((s, d) => s + (d.value || 0), 0);
    st.count = st.deals.length;
  });

  return stages;
}

function renderFunnelRibbon(stages) {
  const strip = document.getElementById('ccPipelineFunnelStrip');
  if (!strip) return;

  strip.innerHTML = stages.map(st => {
    const flex = Math.max(Math.round(st.val / 1000), 40);
    const countLabel = st.count === 1 ? '· 1' : `· ${st.count} deals`;
    const active = activeStageFilter === st.id;
    return `
      <button type="button" class="cc-funnel-segment ${st.cls} ${active ? 'active' : ''}" data-stage="${st.id}" style="flex: ${flex};">
        <div class="cc-funnel-seg-title">${esc(st.label)}</div>
        <div class="cc-funnel-seg-stat">${money(st.val)} ${st.count ? `<small>${countLabel}</small>` : ''}</div>
      </button>`;
  }).join('');

  strip.querySelectorAll('[data-stage]').forEach(btn => {
    btn.addEventListener('click', () => {
      const stage = btn.dataset.stage;
      activeStageFilter = (activeStageFilter === stage) ? 'all' : stage;
      updateStageFilterButtons();
      renderPipelineView();
    });
  });
}

function updateStageFilterButtons() {
  const group = document.getElementById('ccPipelineStageFilters');
  if (!group) return;
  group.querySelectorAll('[data-stage]').forEach(b => {
    b.classList.toggle('active', b.dataset.stage === activeStageFilter);
  });
}

function renderDealsTable(deals) {
  const filtered = activeStageFilter === 'all'
    ? deals
    : deals.filter(d => d.stage === activeStageFilter);

  const mainRows = filtered.slice(0, 7);
  const remainingDeals = filtered.slice(7);

  const rowsHtml = mainRows.map(d => {
    const isSelected = d.key === selectedDealKey;
    const tone = healthTone(d.health);
    const badgesHtml = (d.newSinceThen || []).map(b =>
      `<span class="cc-sig-pill pill-${b.type}">${esc(b.label)}</span>`
    ).join(' ') || '<span class="cc-muted-dash">—</span>';

    return `
      <tr class="cc-deal-tr ${isSelected ? 'is-selected' : ''}" data-deal-key="${esc(d.key)}">
        <th scope="row" class="cc-deal-td-name">
          <div class="cc-deal-row-title">${esc(d.name)}</div>
          <div class="cc-deal-row-stage">${esc(d.stageLabel)}</div>
        </th>
        <td class="cc-deal-td-val">${money(d.value)}</td>
        <td class="cc-deal-td-health">
          <span class="cc-deal-health-pill tone-${tone}">${d.health ?? '—'}</span>
        </td>
        <td class="cc-deal-td-step">
          ${esc(d.nextStep)} ${d.nextStepLate ? `<span class="cc-step-late">· ${esc(d.nextStepLate)}</span>` : ''}
        </td>
        <td class="cc-deal-td-touch ${d.lastTouchLate ? 'is-late' : ''}">${esc(d.lastTouch)}</td>
        <td class="cc-deal-td-new">${badgesHtml}</td>
      </tr>`;
  }).join('');

  const remainingNotice = remainingDeals.length > 0
    ? `<div class="cc-deal-table-footer">+${remainingDeals.length} early-stage deal${remainingDeals.length === 1 ? '' : 's'} (${remainingDeals.map(d => `${esc(d.name.split('—')[1] || d.name).trim()} ${money(d.value)}`).join(', ')})</div>`
    : '';

  return `
    <section class="cc-pipeline-table-card" aria-label="Pipeline Deals">
      <div class="cc-table-scroll-wrap">
        <table class="cc-pipeline-table">
          <thead>
            <tr>
              <th scope="col" style="text-align: left; padding: 10px 14px;">Deal</th>
              <th scope="col" style="text-align: right; padding: 10px 8px;">Value</th>
              <th scope="col" style="text-align: center; padding: 10px 8px;">Health</th>
              <th scope="col" style="text-align: left; padding: 10px 8px;">Next step</th>
              <th scope="col" style="text-align: right; padding: 10px 8px;">Last touch</th>
              <th scope="col" style="text-align: left; padding: 10px 14px;">New since then</th>
            </tr>
          </thead>
          <tbody>
            ${rowsHtml || '<tr><td colspan="6" class="cc-table-empty">No deals matching this stage.</td></tr>'}
          </tbody>
        </table>
      </div>
      ${remainingNotice}
    </section>`;
}

function renderTelemetryDiffInspector(selectedDeal) {
  if (!selectedDeal) {
    return `<aside class="cc-pipeline-inspector" aria-label="Selected deal"><p class="cc-inspector-empty">Select a deal to view live telemetry.</p></aside>`;
  }

  const exitProgressText = selectedDeal.pilotProgress ? ` · ${selectedDeal.pilotProgress}` : '';
  const changes = selectedDeal.newSinceThen || [];

  return `
    <aside class="cc-pipeline-inspector" aria-label="Selected deal">
      <!-- Section 1: Header -->
      <div class="cc-insp-head">
        <div class="cc-insp-badge">SELECTED</div>
        <h3 class="cc-insp-title">${esc(selectedDeal.name)}</h3>
        <div class="cc-insp-sub">${esc(selectedDeal.stageLabel)} · ${money(selectedDeal.value)} · close ${esc(selectedDeal.expectedClose || 'TBD')}${esc(exitProgressText)}</div>
      </div>

      <!-- Section 2: Telemetry Diff -->
      <div class="cc-insp-section">
        <div class="cc-insp-label">What changed at ${esc(selectedDeal.account)} since you last spoke</div>
        <div class="cc-insp-diff-list">
          ${changes.length ? changes.map(ch => `
            <div class="cc-insp-diff-item">
              <strong class="cc-diff-type">${esc(ch.type === 'exec' ? 'Exec move' : ch.type === 'hiring' ? 'Hiring' : 'News')}:</strong>
              <span>${esc(ch.detail)}</span>
            </div>`).join('') : `<p class="cc-insp-quiet">No major external news or leadership changes logged since your last customer touch (${selectedDeal.lastTouch} ago).</p>`}
        </div>
      </div>

      <!-- Section 3: Suggested Next Step -->
      <div class="cc-insp-section cc-insp-suggested-step">
        <div class="cc-insp-label">Suggested next step</div>
        <div class="cc-insp-step-text">${esc(selectedDeal.suggestedStep)}</div>
      </div>

      <!-- Section 4: Action Footer (3 Buttons) -->
      <div class="cc-insp-actions-row">
        <button type="button" class="cc-insp-btn cc-insp-btn-ghost" data-act="log-call" data-deal-id="${selectedDeal.id}" data-deal-name="${esc(selectedDeal.name)}">Log call</button>
        <button type="button" class="cc-insp-btn cc-insp-btn-ghost" data-act="open-deal" data-deal-id="${selectedDeal.id}">Open deal</button>
        <button type="button" class="cc-insp-btn cc-insp-btn-primary" data-act="draft-email" data-deal-id="${selectedDeal.id}" data-deal-name="${esc(selectedDeal.name)}">Draft email</button>
      </div>
    </aside>`;
}

function bindInspectorActions(container, selectedDeal) {
  container.querySelectorAll('[data-act]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const op = btn.dataset.act;
      const dealId = btn.dataset.dealId;
      const dealName = btn.dataset.dealName;

      if (op === 'log-call') {
        logTouch(selectedDeal.account, `call regarding ${selectedDeal.name}`);
        showToast(`Call logged for ${selectedDeal.account}`);
        return;
      }

      if (op === 'open-deal') {
        if (typeof window.openDealDetail === 'function') {
          window.openDealDetail(selectedDeal.key || dealId);
          return;
        }
        window.location.href = `/deals?deal=${dealId}`;
        return;
      }

      if (op === 'draft-email') {
        if (typeof window.openCopilotModal === 'function') {
          window.openCopilotModal({
            dealKey: selectedDeal.key,
            prompt: `Draft check-in email for ${selectedDeal.name}. Focus on ${selectedDeal.suggestedStep}`
          });
          return;
        }
        const q = encodeURIComponent(`Draft check-in email for ${selectedDeal.name}. Focus on ${selectedDeal.suggestedStep}`);
        window.location.href = `/copilot?q=${q}&deal_id=${dealId}`;
        return;
      }
    });
  });
}

function bindTableRows(container, deals) {
  container.querySelectorAll('.cc-deal-tr').forEach(tr => {
    tr.addEventListener('click', () => {
      selectedDealKey = tr.dataset.dealKey;
      renderPipelineView();
    });
    tr.addEventListener('dblclick', () => {
      if (typeof window.openDealDetail === 'function') {
        window.openDealDetail(tr.dataset.dealKey);
      }
    });
  });
}

export function renderPipelineView() {
  const container = document.getElementById('ccPipelineViewSplit');
  if (!container) return;

  const deals = getMergedDeals();
  const stages = computeStageFunnel(deals);
  renderFunnelRibbon(stages);

  // Update banner subtitle
  const totalVal = deals.reduce((s, d) => s + (d.value || 0), 0);
  const metaEl = document.getElementById('ccPipelineBannerMeta');
  if (metaEl) {
    metaEl.textContent = `${deals.length} open deals · ${money(totalVal)} · each deal shows what changed at its account since you last spoke`;
  }

  // Ensure selected deal exists
  let selected = deals.find(d => d.key === selectedDealKey) || deals[0];

  const tableHtml = renderDealsTable(deals);
  const inspectorHtml = renderTelemetryDiffInspector(selected);

  container.innerHTML = tableHtml + inspectorHtml;

  bindTableRows(container, deals);
  bindInspectorActions(container, selected);
}

function setupFilterButtons() {
  const group = document.getElementById('ccPipelineStageFilters');
  if (!group) return;

  group.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-stage]');
    if (!btn) return;
    activeStageFilter = btn.dataset.stage;
    updateStageFilterButtons();
    renderPipelineView();
  });
}

export async function initPipelineView() {
  if (isInitialized) {
    renderPipelineView();
    return;
  }
  isInitialized = true;
  setupFilterButtons();

  try {
    const [day, moves] = await Promise.all([
      loadMyDay().catch(() => null),
      loadRecentMovements().catch(() => [])
    ]);
    rawMyDay = day;
    rawMovements = moves || [];
  } catch (err) {
    console.warn('[pipeline-view] data load error:', err);
  }

  renderPipelineView();
}

