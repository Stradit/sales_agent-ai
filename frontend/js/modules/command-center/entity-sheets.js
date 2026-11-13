// Command Center · Steps 4 & 5: Deep-Dive Entity Sheets & Workspaces
// Implements exact enterprise screens from Claude design suite:
// - DealDetail.dc.html (Breadcrumb, stage stepper, "Needs you" banner, 4-item exit criteria checklist, activity, buying committee, news)
// - AccountDetail.dc.html (5-metric KPI strip, "Why now" alert, open deals table, buying committee with map action, signals)
// - ContactDetail.dc.html (dl details, "Why she matters" influence, linked deals, relationship timeline, intro link)
// - Copilot.dc.html (3-column workspace: recent chats sidebar, interactive chat stream with prompt chips & sources, context sidebar)
// - DealsList.dc.html (Dedicated deals table workspace with 4-KPI cards, stage filters, and deal rows)
// - Introductions.dc.html (Requests list, stale connector card with nudge action, deals with no EB, suggested introducers)
// - Playbook.dc.html (How to sell it: 5 plays, investment tracks & sponsors, operational pain points, common objections)
// - SignalFeed.dc.html (Signal feed workspace with filter chips, structured signal stream, weekly counts, growth themes)
// - MorePhone.dc.html (Mobile 390px shell drawer with Workspace, Sales help, and Account navigation)

import { esc } from './utils.js';
import { loadMyDay } from './deal-context.js';
import { showToast } from '../toast.js';

// Canonical Deal Records matching DealDetail.dc.html and pipeline context
const CANONICAL_DEALS_DETAIL = {
  'bny-pilot': {
    id: 1,
    key: 'bny-pilot',
    name: 'BNY — Applied AI governance pilot',
    account: 'BNY',
    stage: 'pilot',
    stageLabel: 'Pilot',
    stageIndex: 3, // 0: intro, 1: discovery, 2: proposal, 3: pilot, 4: contract
    valueText: '$320k',
    valueNum: 320000,
    closeDate: '17 Dec',
    health: 65,
    lastTouch: '12 days ago',
    owner: 'Jordan Patel',
    needsYou: {
      headline: 'CIO pilot readout is 13 days late.',
      action: 'Reschedule it and open with the leadership change.',
      execMove: 'Exec move: Alaa AlMashhadi starts a new role. Check whether they join the buying committee.'
    },
    exitCriteria: [
      { id: 'crit1', key: 'success_criteria', label: 'Success criteria agreed', owner: 'Priya Nair', done: false },
      { id: 'crit2', key: 'pilot_dates', label: 'Pilot start and end dates set', owner: 'Jordan Patel', done: false },
      { id: 'crit3', key: 'checkin_cadence', label: 'Weekly check-in cadence agreed', owner: 'Jordan Patel', done: false },
      { id: 'crit4', key: 'pilot_result', label: 'Pilot result recorded', owner: "CIO's office", done: false }
    ],
    activity: [
      { date: '12 Nov · Call', text: 'Pilot scope call with the BNY team' },
      { date: '04 Nov · Email', text: "Readout invitation sent to the CIO's office" },
      { date: '12 days ago · Note', text: 'Confirmed pilot cluster deployment and security signoff.' }
    ],
    suggestedNextStep: {
      title: 'Suggested next step',
      text: 'Reschedule the CIO readout and open with the leadership change. Tick the exit criteria you can already show.',
      buttonText: 'Reschedule the readout',
      action: 'reschedule_readout'
    },
    buyingCommittee: [
      { name: 'Alaa AlMashhadi', note: 'Starts a new role · join the committee?', status: 'new_role', contactKey: 'alaa' },
      { name: 'Michael Santomassimo', note: 'Readout owner · 13 days late', status: 'late', contactKey: 'cio' },
      { name: 'Economic buyer', note: 'Not yet mapped', status: 'unmapped', contactKey: null }
    ],
    news: {
      headline: 'BNY expands enterprise AI governance board following regulatory guidelines',
      source: 'Reuters Financial',
      date: '3 days ago'
    }
  },
  'blackrock-contract': {
    id: 2,
    key: 'blackrock-contract',
    name: 'BlackRock — AI agent testing framework',
    account: 'BlackRock',
    stage: 'contract',
    stageLabel: 'Contract',
    stageIndex: 4,
    valueText: '$450k',
    valueNum: 450000,
    closeDate: '30 Nov',
    health: 45,
    lastTouch: '20 days ago',
    owner: 'Jordan Patel',
    needsYou: {
      headline: 'The $450k contract is waiting on our MSA redlines, now 15 days late.',
      action: 'Send the redlines before the next call so the signature date holds.',
      execMove: '4 new senior cloud and AI testing roles posted in NYC.'
    },
    exitCriteria: [
      { id: 'crit1', key: 'approval_chain', label: 'Approval chain confirmed', owner: 'Priya Nair', done: true },
      { id: 'crit2', key: 'security_review', label: 'Security / procurement review done', owner: 'Legal Team', done: true },
      { id: 'crit3', key: 'contract_sent', label: 'MSA redlines returned to legal', owner: 'Jordan Patel', done: false },
      { id: 'crit4', key: 'signed', label: 'Executive signature', owner: 'Laurence Fink', done: false }
    ],
    activity: [
      { date: '28 Oct · Meeting', text: 'Meeting with Laurence Fink, held via Priya Nair' },
      { date: '22 Oct · Email', text: 'MSA redlines received from BlackRock procurement' },
      { date: '20 days ago · Note', text: 'Internal legal review pending partner sign-off' }
    ],
    suggestedNextStep: {
      title: 'Suggested next step',
      text: 'Return MSA redlines to BlackRock legal team; confirm economic buyer sign-off before month-end.',
      buttonText: 'Draft MSA response',
      action: 'draft_msa'
    },
    buyingCommittee: [
      { name: 'Laurence Fink', note: 'Meeting held via Priya Nair', status: 'engaged', contactKey: 'fink' },
      { name: 'Priya Nair', note: 'Champion · VP of Infrastructure', status: 'engaged', contactKey: 'priya-nair' },
      { name: 'Economic buyer', note: 'Legal procurement officer · not yet mapped', status: 'unmapped', contactKey: null }
    ],
    news: {
      headline: 'BlackRock accelerates multi-agent AI verification for risk platform Aladdin',
      source: 'Bloomberg Technology',
      date: '5 days ago'
    }
  },
  'dtcc-pilot': {
    id: 3,
    key: 'dtcc-pilot',
    name: 'DTCC — Digital assets settlement pilot',
    account: 'Depository Trust & Clearing',
    stage: 'pilot',
    stageLabel: 'Pilot',
    stageIndex: 3,
    valueText: '$380k',
    valueNum: 380000,
    closeDate: '15 Jan 2027',
    health: 55,
    lastTouch: '26 days ago',
    owner: 'Jordan Patel',
    needsYou: {
      headline: 'Pilot quiet for 26 days; intro to Anthony Portannese went stale.',
      action: 'Re-engage sponsor on agreed pilot success criteria; leverage digital settlement news hook.',
      execMove: 'DTCC announces digital securities post-trade settlement pilot program.'
    },
    exitCriteria: [
      { id: 'crit1', key: 'success_criteria', label: 'Success criteria agreed', owner: 'Jordan Patel', done: true },
      { id: 'crit2', key: 'pilot_dates', label: 'Pilot timeline confirmed', owner: 'Anthony Portannese', done: false },
      { id: 'crit3', key: 'checkin_cadence', label: 'Weekly cadence booked', owner: 'Jordan Patel', done: false },
      { id: 'crit4', key: 'pilot_result', label: 'Benchmark telemetry recorded', owner: 'Technical Lead', done: false }
    ],
    activity: [
      { date: '18 Oct · Email', text: 'Introduction from Anthony Portannese' },
      { date: '14 Oct · Call', text: 'Initial architecture discovery call' }
    ],
    suggestedNextStep: {
      title: 'Suggested next step',
      text: 'Re-engage sponsor on agreed pilot success criteria; leverage the digital settlement news hook.',
      buttonText: 'Draft re-engage email',
      action: 'draft_reengage'
    },
    buyingCommittee: [
      { name: 'Anthony Portannese', note: 'Connector / Sponsor · stale intro', status: 'watch', contactKey: 'anthony' },
      { name: 'Technical Evaluator', note: 'Lead settlement architect', status: 'engaged', contactKey: null },
      { name: 'Economic buyer', note: 'Not yet mapped', status: 'unmapped', contactKey: null }
    ],
    news: {
      headline: 'DTCC expands blockchain settlement pilot with major tier-1 investment banks',
      source: 'Financial Times',
      date: '1 week ago'
    }
  }
};

// Canonical Account Records matching AccountDetail.dc.html
const CANONICAL_ACCOUNTS_DETAIL = {
  'blackrock': {
    key: 'blackrock',
    name: 'BlackRock',
    status: 'Watch',
    statusClass: 'tone-watch',
    subtitle: '3 deals · $810k open · last touch 20 days ago · owner: Jordan Patel',
    kpi: {
      openPipeline: '$810k',
      contacts: '704',
      cSuite: '77',
      bestHealth: '45',
      lastTouch: '20 days ago'
    },
    whyNow: 'The $450k contract is waiting on our MSA redlines, now 15 days late. Send the redlines before the next call so the signature date holds.',
    openDeals: [
      { key: 'blackrock-contract', name: 'BlackRock — AI agent testing framework', stage: 'Contract review', value: '$450k', nextStep: 'MSA redlines, 15 days late', late: true },
      { key: 'blackrock-cloud', name: 'BlackRock — Cloud cost optimisation (EMEA)', stage: 'Proposal', value: '$210k', nextStep: 'Economic buyer mapping', late: false },
      { key: 'blackrock-data', name: 'BlackRock — Enterprise data governance', stage: 'Discovery', value: '$150k', nextStep: 'Scoping workshop', late: false }
    ],
    buyingCommittee: [
      { name: 'Laurence Fink', note: 'Meeting held via Priya Nair', status: 'Engaged', isEngaged: true, contactKey: 'fink' },
      { name: 'Priya Nair', note: 'Champion · VP of Infrastructure', status: 'Engaged', isEngaged: true, contactKey: 'priya-nair' },
      { name: 'Economic buyer', note: 'Procurement / Legal · not yet mapped', status: null, isEngaged: false, unmapped: true }
    ],
    activity: [
      { date: '28 Oct', text: 'Meeting with Laurence Fink, held via Priya Nair' },
      { date: '22 Oct', text: 'MSA redlines sent to legal, awaiting response' },
      { date: '20 days ago', text: 'Last touch with the account team' }
    ],
    signals: {
      execMoves: 2,
      jobPostings: 4,
      newsArticles: 3
    }
  },
  'bny': {
    key: 'bny',
    name: 'BNY',
    status: 'Watch',
    statusClass: 'tone-watch',
    subtitle: '1 deal · $320k open · last touch 12 days ago · owner: Jordan Patel',
    kpi: {
      openPipeline: '$320k',
      contacts: '512',
      cSuite: '42',
      bestHealth: '65',
      lastTouch: '12 days ago'
    },
    whyNow: 'CIO pilot readout is 13 days late. Alaa AlMashhadi moved into a key AI leadership role — verify committee coverage before the rescheduled session.',
    openDeals: [
      { key: 'bny-pilot', name: 'BNY — Applied AI governance pilot', stage: 'Pilot', value: '$320k', nextStep: 'CIO pilot readout (13d late)', late: true }
    ],
    buyingCommittee: [
      { name: 'Alaa AlMashhadi', note: 'Starts a new role · join the committee?', status: 'New role', isEngaged: false, contactKey: 'alaa' },
      { name: 'CIO Office', note: 'Readout owner · 13 days late', status: 'Reviewing', isEngaged: true, contactKey: 'cio' },
      { name: 'Economic buyer', note: 'Not yet mapped', status: null, isEngaged: false, unmapped: true }
    ],
    activity: [
      { date: '12 Nov', text: 'Pilot scope call with BNY engineering lead' },
      { date: '04 Nov', text: "Readout invitation sent to the CIO's office" },
      { date: '12 days ago', text: 'Pilot cluster telemetry validation complete' }
    ],
    signals: {
      execMoves: 1,
      jobPostings: 5,
      newsArticles: 2
    }
  },
  'dtcc': {
    key: 'dtcc',
    name: 'Depository Trust & Clearing',
    status: 'Watch',
    statusClass: 'tone-watch',
    subtitle: '2 deals · $655k open · last touch 26 days ago · owner: Jordan Patel',
    kpi: {
      openPipeline: '$655k',
      contacts: '610',
      cSuite: '16',
      bestHealth: '55',
      lastTouch: '26 days ago'
    },
    whyNow: '$380k pilot quiet for 26 days; the intro to Anthony Portannese went stale. Re-engage on digital asset settlement.',
    openDeals: [
      { key: 'dtcc-pilot', name: 'DTCC — Digital assets settlement pilot', stage: 'Pilot', value: '$380k', nextStep: 'Success criteria (12d late)', late: true },
      { key: 'dtcc-recon', name: 'DTCC — AI-assisted reconciliation', stage: 'Proposal', value: '$275k', nextStep: 'Map exceptions (21d late)', late: true }
    ],
    buyingCommittee: [
      { name: 'Anthony Portannese', note: 'Connector · intro stale', status: 'Watch', isEngaged: false, contactKey: 'anthony' },
      { name: 'Lead Architect', note: 'Technical Evaluator', status: 'Engaged', isEngaged: true, contactKey: null },
      { name: 'Economic buyer', note: 'Not yet mapped', status: null, isEngaged: false, unmapped: true }
    ],
    activity: [
      { date: '18 Oct', text: 'Stale introduction touchpoint' },
      { date: '14 Oct', text: 'Scope discovery meeting' }
    ],
    signals: {
      execMoves: 1,
      jobPostings: 3,
      newsArticles: 3
    }
  },
  'northerntrust': {
    key: 'northerntrust',
    name: 'Northern Trust',
    status: 'At risk',
    statusClass: 'tone-risk',
    subtitle: '1 deal · $230k open · last touch 24 days ago · owner: Jordan Patel',
    kpi: {
      openPipeline: '$230k',
      contacts: '0',
      cSuite: '0',
      bestHealth: '15',
      lastTouch: '24 days ago'
    },
    whyNow: 'No contacts mapped, so the cloud landing-zone deal has no champion or economic buyer.',
    openDeals: [
      { key: 'nt-cloud', name: 'Northern Trust — Cloud landing zone', stage: 'Discovery', value: '$230k', nextStep: 'Find economic buyer', late: true }
    ],
    buyingCommittee: [
      { name: 'Champion', note: 'Not yet mapped', status: null, isEngaged: false, unmapped: true },
      { name: 'Economic buyer', note: 'Not yet mapped', status: null, isEngaged: false, unmapped: true }
    ],
    activity: [
      { date: '24 days ago', text: 'Initial outreach via procurement portal' }
    ],
    signals: {
      execMoves: 0,
      jobPostings: 2,
      newsArticles: 1
    }
  }
};

// Canonical Contact Records matching ContactDetail.dc.html
const CANONICAL_CONTACTS_DETAIL = {
  'priya-nair': {
    key: 'priya-nair',
    name: 'Priya Nair',
    roleTag: 'Champion',
    tagClass: 'badge-champion',
    account: 'BlackRock',
    accountKey: 'blackrock',
    title: 'VP of Infrastructure & Platform Engineering',
    level: 'VP / Senior Director',
    email: 'priya.nair@blackrock.com',
    phone: '+1 (212) 810-5400',
    lastContact: '6 days ago',
    owner: 'Jordan Patel',
    whyMatters: 'Priya Nair set up the meeting with Laurence Fink, so she is the route into the committee. Keep her close while the $450k contract moves through legal.',
    linkedDeals: [
      { key: 'blackrock-contract', name: 'BlackRock — AI agent testing framework', stage: 'Contract · MSA redlines 15 days late', value: '$450k' },
      { key: 'blackrock-cloud', name: 'BlackRock — Cloud cost optimisation (EMEA)', stage: 'Proposal · no economic buyer', value: '$210k' }
    ],
    suggestedNextStep: 'Ask her to confirm the MSA redline owner at BlackRock legal, and whether the committee sees the contract before signature.',
    timeline: [
      { date: '28 Oct · Meeting', text: 'Meeting with Laurence Fink, held via Priya Nair' },
      { date: '22 Oct · Email', text: 'Shared the MSA redlines with BlackRock legal' },
      { date: '6 days ago · Call', text: 'Touchpoint confirming technical architecture readiness' }
    ],
    introductions: {
      text: 'Laurence Fink (BlackRock). Economic buyer on the EMEA deal is not yet mapped.',
      linkHref: '#introductions'
    }
  },
  'alaa': {
    key: 'alaa',
    name: 'Alaa AlMashhadi',
    roleTag: 'Influencer',
    tagClass: 'badge-influencer',
    account: 'BNY',
    accountKey: 'bny',
    title: 'Managing Director, AI Platform Architecture',
    level: 'Managing Director',
    email: 'alaa.almashhadi@bny.com',
    phone: '+1 (212) 635-1784',
    lastContact: '14 days ago',
    owner: 'Jordan Patel',
    whyMatters: 'Alaa recently took over AI Platform Architecture at BNY. He holds the technical approval key for enterprise AI governance tooling.',
    linkedDeals: [
      { key: 'bny-pilot', name: 'BNY — Applied AI governance pilot', stage: 'Pilot · 13 days late readout', value: '$320k' }
    ],
    suggestedNextStep: 'Congratulate him on the new role and ask if he will be joining the rescheduled CIO pilot readout session.',
    timeline: [
      { date: '01 Nov · News', text: 'Executive promotion to Managing Director, AI Platforms' },
      { date: '24 Oct · Call', text: 'Introductory discussion on AI compliance requirements' }
    ],
    introductions: {
      text: "Michael Santomassimo (CIO's Office). Connect before the scheduled readout.",
      linkHref: '#introductions'
    }
  }
};

// Canonical 9 Deals matching DealsList.dc.html
const CANONICAL_ALL_DEALS = [
  { key: 'bny-pilot', name: 'BNY — Applied AI governance pilot', account: 'BNY', stage: 'pilot', stageLabel: 'Pilot', value: '$320k', health: 65, nextStep: 'CIO readout · 13 d late', nextStepLate: true, lastTouch: '12 d', lastTouchLate: false, tag: '' },
  { key: 'blackrock-contract', name: 'BlackRock — AI agent testing framework', account: 'BlackRock', stage: 'contract', stageLabel: 'Contract', value: '$450k', health: 45, nextStep: 'MSA redlines · 15 d late', nextStepLate: true, lastTouch: '20 d', lastTouchLate: false, tag: '' },
  { key: 'dtcc-pilot', name: 'DTCC — Digital assets settlement pilot', account: 'DTCC', stage: 'pilot', stageLabel: 'Pilot', value: '$380k', health: 55, nextStep: 'Success criteria · 12 d late', nextStepLate: true, lastTouch: '26 d', lastTouchLate: true, tag: '' },
  { key: 'dtcc-recon', name: 'DTCC — AI-assisted reconciliation', account: 'DTCC', stage: 'proposal', stageLabel: 'Proposal', value: '$275k', health: 50, nextStep: 'Map exceptions · 21 d late', nextStepLate: true, lastTouch: '52 d', lastTouchLate: true, tag: 'no economic buyer' },
  { key: 'nt-cloud', name: 'Northern Trust — Secure cloud landing zone', account: 'Northern Trust', stage: 'proposal', stageLabel: 'Proposal', value: '$230k', health: 15, nextStep: 'Security questionnaire · 6 d late', nextStepLate: true, lastTouch: '24 d', lastTouchLate: false, tag: 'no contacts mapped' },
  { key: 'blackrock-cloud', name: 'BlackRock — Cloud cost optimisation (EMEA)', account: 'BlackRock', stage: 'proposal', stageLabel: 'Proposal', value: '$210k', health: 30, nextStep: 'EMEA workshop · 8 d late', nextStepLate: true, lastTouch: '43 d', lastTouchLate: true, tag: 'no economic buyer' },
  { key: 'bny-data', name: 'BNY — Fund accounting data quality', account: 'BNY', stage: 'proposal', stageLabel: 'Proposal', value: '$180k', health: 50, nextStep: 'Revised proposal · 15 d late', nextStepLate: true, lastTouch: '22 d', lastTouchLate: false, tag: '' },
  { key: 'aladdin-data', name: 'Aladdin data integration', account: 'BlackRock', stage: 'intro', stageLabel: 'Intro', value: '$150k', health: 60, nextStep: 'Schedule intro scoping', nextStepLate: false, lastTouch: '8 d', lastTouchLate: false, tag: 'Early stage' },
  { key: 'cyber-resilience', name: 'Cyber resilience assessment', account: 'State Street', stage: 'discovery', stageLabel: 'Discovery', value: '$95k', health: 70, nextStep: 'Review requirements', nextStepLate: false, lastTouch: '5 d', lastTouchLate: false, tag: 'Early stage' }
];

// Canonical Copilot Pre-set Context matching Copilot.dc.html
const CANONICAL_COPILOT_CHATS = [
  { id: 'c1', title: 'BNY readout prep', dealKey: 'bny-pilot', active: true },
  { id: 'c2', title: 'BlackRock MSA redlines', dealKey: 'blackrock-contract', active: false },
  { id: 'c3', title: 'DTCC re-engage email', dealKey: 'dtcc-pilot', active: false },
  { id: 'c4', title: 'Northern Trust committee map', dealKey: 'northerntrust', active: false }
];

let activeSheetType = null;
let activeSheetKey = null;

// ── DOM Elements ─────────────────────────────────────────────────────────────
function getModalElements() {
  return {
    backdrop: document.getElementById('ccEntitySheetBackdrop'),
    modal: document.getElementById('ccEntitySheetModal'),
    content: document.getElementById('ccEntitySheetContent'),
    breadcrumb: document.getElementById('ccEntityBreadcrumb'),
    closeBtn: document.getElementById('ccEntitySheetClose'),
    expandBtn: document.getElementById('ccEntitySheetExpand')
  };
}

export function closeEntitySheet(updateUrl = true) {
  const { backdrop, modal } = getModalElements();
  if (modal) {
    modal.hidden = true;
    modal.classList.remove('is-open', 'is-expanded');
  }
  if (backdrop) {
    backdrop.classList.remove('is-open');
  }
  document.body.classList.remove('cc-sheet-open');
  activeSheetType = null;
  activeSheetKey = null;

  if (updateUrl) {
    const url = new URL(window.location.href);
    url.searchParams.delete('sheet');
    url.searchParams.delete('entity');
    if (window.location.hash.startsWith('#sheet-') || window.location.hash.startsWith('#deal-') || window.location.hash.startsWith('#account-') || window.location.hash.startsWith('#contact-') || window.location.hash === '#copilot' || window.location.hash === '#deals' || window.location.hash === '#introductions' || window.location.hash === '#playbook' || window.location.hash === '#signals' || window.location.hash === '#more') {
      window.history.replaceState(null, '', url.pathname + (url.search ? url.search : ''));
    }
  }
}

function openSheetContainer(type, key) {
  const { backdrop, modal } = getModalElements();
  if (!modal) return;
  activeSheetType = type;
  activeSheetKey = key;

  modal.hidden = false;
  requestAnimationFrame(() => {
    modal.classList.add('is-open');
    if (backdrop) backdrop.classList.add('is-open');
    document.body.classList.add('cc-sheet-open');
  });
}

// ── 1. DealDetail Rendering (DealDetail.dc.html) ─────────────────────────────
export function openDealDetail(dealIdOrKey, options = {}) {
  const normKey = String(dealIdOrKey || 'bny-pilot').toLowerCase().replace(/[^a-z0-9]+/g, '-');
  const deal = CANONICAL_DEALS_DETAIL[normKey] || CANONICAL_DEALS_DETAIL['bny-pilot'];
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  openSheetContainer('deal', deal.key);

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#pipeline" class="cc-sheet-crumb-link" data-nav="pipeline">My pipeline</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr">${esc(deal.name)}</span>
      </nav>
    `;
  }

  const stages = [
    { key: 'intro', label: 'Intro' },
    { key: 'discovery', label: 'Discovery' },
    { key: 'proposal', label: 'Proposal' },
    { key: 'pilot', label: 'Pilot' },
    { key: 'contract', label: 'Contract' }
  ];

  const doneCount = deal.exitCriteria.filter(c => c.done).length;
  const totalCount = deal.exitCriteria.length;

  content.innerHTML = `
    <div class="cc-deal-detail-view" data-deal-key="${esc(deal.key)}">
      <div class="cc-sheet-header">
        <div class="cc-sheet-header-main">
          <div class="cc-sheet-title-row">
            <h1 class="cc-sheet-title">${esc(deal.name)}</h1>
            <span class="cc-stage-pill cc-stage-${esc(deal.stage)}">${esc(deal.stageLabel)}</span>
          </div>
          <div class="cc-sheet-subtitle">
            ${esc(deal.valueText)} · close ${esc(deal.closeDate)} · health ${deal.health} · last touch ${esc(deal.lastTouch)}
          </div>
        </div>
        <div class="cc-sheet-header-actions">
          <button type="button" class="cc-btn cc-btn-outline" data-action="log-call"><i class="fa-solid fa-phone"></i> Log call</button>
          <button type="button" class="cc-btn cc-btn-outline" data-action="move-stage"><i class="fa-solid fa-arrows-turn-right"></i> Move stage</button>
          <button type="button" class="cc-btn cc-btn-primary" data-action="draft-email" data-deal="${esc(deal.key)}"><i class="fa-solid fa-envelope"></i> Draft email</button>
        </div>
      </div>

      <section aria-label="Deal stage" class="cc-stage-stepper-box">
        <ol class="cc-stage-stepper-list">
          ${stages.map((st, idx) => {
            const isCurrent = idx === deal.stageIndex;
            const isPast = idx < deal.stageIndex;
            const stateClass = isCurrent ? 'step-current' : isPast ? 'step-past' : 'step-future';
            return `
              <li class="cc-stepper-step ${stateClass}" ${isCurrent ? 'aria-current="step"' : ''}>
                <span class="cc-step-text">${esc(st.label)}${isCurrent ? ' · current' : ''}</span>
              </li>
            `;
          }).join('')}
        </ol>
      </section>

      <div class="cc-deal-sheet-grid">
        <div class="cc-sheet-col-left">
          <section aria-label="What changed" class="cc-sheet-card cc-needs-you-card">
            <h2 class="cc-card-eyebrow cc-eyebrow-alert">Needs you</h2>
            <p class="cc-needs-headline"><strong>${esc(deal.needsYou.headline)}</strong> ${esc(deal.needsYou.action)}</p>
            <p class="cc-needs-sub">${esc(deal.needsYou.execMove)}</p>
          </section>

          <section aria-label="Pilot exit criteria" class="cc-sheet-card cc-exit-criteria-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Pilot exit criteria <span class="cc-criteria-count" id="ccExitCriteriaCount">${doneCount} of ${totalCount} done</span></h2>
            </div>
            <ul class="cc-criteria-list" id="ccExitCriteriaList">
              ${deal.exitCriteria.map(crit => `
                <li class="cc-criteria-item ${crit.done ? 'is-done' : ''}">
                  <input type="checkbox" id="${crit.id}" class="cc-crit-checkbox" data-crit-key="${esc(crit.key)}" ${crit.done ? 'checked' : ''} aria-label="${esc(crit.label)} done">
                  <label for="${crit.id}" class="cc-crit-label">${esc(crit.label)}</label>
                  <span class="cc-crit-owner">Owner: ${esc(crit.owner)}</span>
                </li>
              `).join('')}
            </ul>
          </section>

          <section aria-label="Activity" class="cc-sheet-card cc-activity-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Activity</h2>
            </div>
            <ol class="cc-activity-timeline-list">
              ${deal.activity.map(act => `
                <li class="cc-activity-item">
                  <div class="cc-activity-meta">${esc(act.date)}</div>
                  <div class="cc-activity-desc">${esc(act.text)}</div>
                </li>
              `).join('')}
            </ol>
          </section>
        </div>

        <div class="cc-sheet-col-right">
          <section aria-label="Suggested next step" class="cc-sheet-card cc-suggested-card">
            <h2 class="cc-card-eyebrow">Suggested next step</h2>
            <p class="cc-suggested-body">${esc(deal.suggestedNextStep.text)}</p>
            <button type="button" class="cc-btn cc-btn-primary cc-btn-block" data-action="${esc(deal.suggestedNextStep.action)}">${esc(deal.suggestedNextStep.buttonText)}</button>
          </section>

          <section aria-label="Buying committee" class="cc-sheet-card cc-committee-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Buying committee</h2>
            </div>
            <ul class="cc-committee-list">
              ${deal.buyingCommittee.map(person => `
                <li class="cc-committee-member-row ${person.contactKey ? 'is-clickable' : ''}" data-contact-key="${esc(person.contactKey || '')}">
                  <div class="cc-member-name">${esc(person.name)}</div>
                  <div class="cc-member-note">${esc(person.note)}</div>
                </li>
              `).join('')}
            </ul>
          </section>

          <section aria-label="Deal news" class="cc-sheet-card cc-news-card">
            <h2 class="cc-card-eyebrow">News at ${esc(deal.account)}</h2>
            <p class="cc-news-body">${esc(deal.news.headline)} · <span class="cc-news-meta">${esc(deal.news.source)} · ${esc(deal.news.date)}</span></p>
          </section>
        </div>
      </div>
    </div>
  `;

  bindDealDetailEvents(deal);
}

function bindDealDetailEvents(deal) {
  const { content } = getModalElements();
  if (!content) return;

  content.querySelectorAll('.cc-crit-checkbox').forEach(chk => {
    chk.addEventListener('change', async (e) => {
      const critKey = e.target.dataset.critKey;
      const isChecked = e.target.checked;
      const item = deal.exitCriteria.find(c => c.key === critKey);
      if (item) item.done = isChecked;

      const row = e.target.closest('.cc-criteria-item');
      if (row) row.classList.toggle('is-done', isChecked);

      const doneCount = deal.exitCriteria.filter(c => c.done).length;
      const countEl = document.getElementById('ccExitCriteriaCount');
      if (countEl) countEl.textContent = `${doneCount} of ${deal.exitCriteria.length} done`;

      showToast(`Updated pilot exit criterion: ${item ? item.label : critKey}`);

      try {
        await fetch(`/api/deals/${deal.id}/checklist/pilot/${encodeURIComponent(critKey)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ done: isChecked })
        });
      } catch { /* offline fallback */ }
    });
  });

  content.querySelector('[data-action="log-call"]')?.addEventListener('click', () => {
    showToast(`Logged call touchpoint for ${deal.name}`);
  });

  content.querySelector('[data-action="move-stage"]')?.addEventListener('click', () => {
    showToast(`Advancing ${deal.name} deal stage...`);
  });

  content.querySelector('[data-action="draft-email"]')?.addEventListener('click', () => {
    openCopilotModal({
      dealKey: deal.key,
      prompt: `Draft follow-up email to ${deal.account} regarding the pilot exit criteria and rescheduled readout.`
    });
  });

  content.querySelector('[data-action="reschedule_readout"]')?.addEventListener('click', () => {
    openCopilotModal({
      dealKey: deal.key,
      prompt: `Draft a reschedule email for the ${deal.account} CIO pilot readout, opening with congratulations on the leadership changes.`
    });
  });

  content.querySelectorAll('.cc-committee-member-row.is-clickable').forEach(row => {
    row.addEventListener('click', () => {
      const contactKey = row.dataset.contactKey;
      if (contactKey) openContactDetail(contactKey);
    });
  });

  breadcrumbNavPipeline();
}

// ── 2. AccountDetail Rendering (AccountDetail.dc.html) ───────────────────────
export function openAccountDetail(accountKeyOrName, options = {}) {
  const normKey = String(accountKeyOrName || 'blackrock').toLowerCase().replace(/\b(the|corporation|corp|inc|llc|ltd|co)\b/g, '').replace(/[^a-z0-9]+/g, '');
  const account = CANONICAL_ACCOUNTS_DETAIL[normKey] || CANONICAL_ACCOUNTS_DETAIL['blackrock'];
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  openSheetContainer('account', account.key);

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#accounts" class="cc-sheet-crumb-link" data-nav="accounts">My accounts</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr">${esc(account.name)}</span>
      </nav>
    `;
  }

  content.innerHTML = `
    <div class="cc-account-detail-view" data-account-key="${esc(account.key)}">
      <div class="cc-sheet-header">
        <div class="cc-sheet-header-main">
          <div class="cc-sheet-title-row">
            <h1 class="cc-sheet-title">${esc(account.name)}</h1>
            <span class="cc-status-pill ${esc(account.statusClass)}">${esc(account.status)}</span>
          </div>
          <div class="cc-sheet-subtitle">${esc(account.subtitle)}</div>
        </div>
        <div class="cc-sheet-header-actions">
          <button type="button" class="cc-btn cc-btn-outline" data-action="draft-checkin"><i class="fa-solid fa-pen-to-square"></i> Draft a check-in</button>
          <button type="button" class="cc-btn cc-btn-primary" data-action="open-contract-deal" data-account="${esc(account.key)}"><i class="fa-solid fa-folder-open"></i> Open the contract deal</button>
        </div>
      </div>

      <div class="cc-sheet-kpi-strip">
        <div class="cc-sheet-kpi-cell">
          <div class="cc-sheet-kpi-label">Open pipeline</div>
          <div class="cc-sheet-kpi-val">${esc(account.kpi.openPipeline)}</div>
        </div>
        <div class="cc-sheet-kpi-cell">
          <div class="cc-sheet-kpi-label">Contacts</div>
          <div class="cc-sheet-kpi-val">${esc(account.kpi.contacts)}</div>
        </div>
        <div class="cc-sheet-kpi-cell">
          <div class="cc-sheet-kpi-label">C-suite</div>
          <div class="cc-sheet-kpi-val">${esc(account.kpi.cSuite)}</div>
        </div>
        <div class="cc-sheet-kpi-cell">
          <div class="cc-sheet-kpi-label">Best health</div>
          <div class="cc-sheet-kpi-val">${esc(account.kpi.bestHealth)}</div>
        </div>
        <div class="cc-sheet-kpi-cell">
          <div class="cc-sheet-kpi-label">Last touch</div>
          <div class="cc-sheet-kpi-val">${esc(account.kpi.lastTouch)}</div>
        </div>
      </div>

      <div class="cc-account-sheet-grid">
        <div class="cc-sheet-col-left">
          <section aria-label="Why now" class="cc-sheet-card cc-why-now-card">
            <h2 class="cc-card-eyebrow cc-eyebrow-alert">Why now</h2>
            <p class="cc-why-now-text">${esc(account.whyNow)}</p>
          </section>

          <section aria-label="Open deals" class="cc-sheet-card cc-open-deals-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Open deals</h2>
            </div>
            <div class="cc-table-responsive">
              <table class="cc-account-deals-table">
                <thead>
                  <tr>
                    <th scope="col">Deal</th>
                    <th scope="col">Stage</th>
                    <th scope="col" style="text-align: right">Value</th>
                    <th scope="col">Next step</th>
                  </tr>
                </thead>
                <tbody>
                  ${account.openDeals.map(d => `
                    <tr class="cc-acct-deal-row is-clickable" data-deal-key="${esc(d.key)}">
                      <td class="cc-deal-cell-name"><strong>${esc(d.name)}</strong></td>
                      <td>${esc(d.stage)}</td>
                      <td style="text-align: right; font-variant-numeric: tabular-nums; font-weight: 700">${esc(d.value)}</td>
                      <td><span class="${d.late ? 'cc-text-late' : ''}">${esc(d.nextStep)}</span></td>
                    </tr>
                  `).join('')}
                </tbody>
              </table>
            </div>
          </section>
        </div>

        <div class="cc-sheet-col-right">
          <section aria-label="Buying committee" class="cc-sheet-card cc-committee-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Buying committee</h2>
            </div>
            <ul class="cc-committee-list">
              ${account.buyingCommittee.map(p => `
                <li class="cc-committee-member-row cc-acct-committee-row ${p.contactKey ? 'is-clickable' : ''}" data-contact-key="${esc(p.contactKey || '')}">
                  <div class="cc-member-details">
                    <div class="cc-member-name">${esc(p.name)}</div>
                    <div class="cc-member-note">${esc(p.note)}</div>
                  </div>
                  ${p.isEngaged ? `
                    <span class="cc-status-pill tone-good" style="align-self: center">Engaged</span>
                  ` : p.unmapped ? `
                    <button type="button" class="cc-btn cc-btn-outline cc-btn-xs cc-map-contact-btn" data-account="${esc(account.name)}">Map this contact</button>
                  ` : ''}
                </li>
              `).join('')}
            </ul>
          </section>

          <section aria-label="Activity" class="cc-sheet-card cc-activity-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Recent activity</h2>
            </div>
            <ol class="cc-activity-timeline-list">
              ${account.activity.map(a => `
                <li class="cc-activity-item">
                  <div class="cc-activity-meta">${esc(a.date)}</div>
                  <div class="cc-activity-desc">${esc(a.text)}</div>
                </li>
              `).join('')}
            </ol>
          </section>

          <section aria-label="Signals" class="cc-sheet-card cc-signals-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Signals this week</h2>
            </div>
            <div class="cc-signal-stat-row"><span>Exec moves</span><strong>${account.signals.execMoves}</strong></div>
            <div class="cc-signal-stat-row"><span>New job postings</span><strong>${account.signals.jobPostings}</strong></div>
            <div class="cc-signal-stat-row"><span>News articles</span><strong>${account.signals.newsArticles}</strong></div>
          </section>
        </div>
      </div>
    </div>
  `;

  bindAccountDetailEvents(account);
}

function bindAccountDetailEvents(account) {
  const { content } = getModalElements();
  if (!content) return;

  content.querySelectorAll('.cc-acct-deal-row').forEach(tr => {
    tr.addEventListener('click', () => {
      const dealKey = tr.dataset.dealKey;
      if (dealKey) openDealDetail(dealKey);
    });
  });

  content.querySelector('[data-action="open-contract-deal"]')?.addEventListener('click', () => {
    const firstDeal = account.openDeals[0];
    if (firstDeal) openDealDetail(firstDeal.key);
  });

  content.querySelector('[data-action="draft-checkin"]')?.addEventListener('click', () => {
    openCopilotModal({
      dealKey: account.openDeals[0]?.key,
      prompt: `Draft a high-priority executive check-in email for ${account.name}.`
    });
  });

  content.querySelectorAll('.cc-map-contact-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const modal = document.getElementById('ccCommitteeModal');
      const backdrop = document.getElementById('ccCommitteeBackdrop');
      const acctInput = document.getElementById('ccCommitteeAccount');
      if (modal && backdrop) {
        if (acctInput) acctInput.value = account.name;
        modal.hidden = false;
        backdrop.classList.add('is-open');
      }
    });
  });

  content.querySelectorAll('.cc-committee-member-row.is-clickable').forEach(row => {
    row.addEventListener('click', () => {
      const key = row.dataset.contactKey;
      if (key) openContactDetail(key);
    });
  });

  breadcrumbNavAccounts();
}

// ── 3. ContactDetail Rendering (ContactDetail.dc.html) ───────────────────────
export function openContactDetail(contactKeyOrName, options = {}) {
  const normKey = String(contactKeyOrName || 'priya-nair').toLowerCase().replace(/[^a-z0-9]+/g, '-');
  const contact = CANONICAL_CONTACTS_DETAIL[normKey] || CANONICAL_CONTACTS_DETAIL['priya-nair'];
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  openSheetContainer('contact', contact.key);

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#account" class="cc-sheet-crumb-link" data-account="${esc(contact.accountKey)}">${esc(contact.account)}</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr">${esc(contact.name)}</span>
      </nav>
    `;
  }

  content.innerHTML = `
    <div class="cc-contact-detail-view" data-contact-key="${esc(contact.key)}">
      <div class="cc-sheet-header">
        <div class="cc-sheet-header-main">
          <div class="cc-sheet-title-row">
            <h1 class="cc-sheet-title">${esc(contact.name)}</h1>
            <span class="cc-status-pill tone-good">${esc(contact.roleTag)}</span>
          </div>
          <div class="cc-sheet-subtitle">
            ${esc(contact.title)} · ${esc(contact.account)} · last contact ${esc(contact.lastContact)} · owner: ${esc(contact.owner)}
          </div>
        </div>
        <div class="cc-sheet-header-actions">
          <button type="button" class="cc-btn cc-btn-outline" data-action="log-touchpoint"><i class="fa-solid fa-phone"></i> Log a touchpoint</button>
          <button type="button" class="cc-btn cc-btn-primary" data-action="draft-followup" data-contact="${esc(contact.key)}"><i class="fa-solid fa-envelope"></i> Draft a follow-up</button>
        </div>
      </div>

      <div class="cc-contact-sheet-grid">
        <div class="cc-sheet-col-left">
          <section aria-label="Contact details" class="cc-sheet-card cc-contact-info-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Contact details</h2>
            </div>
            <dl class="cc-contact-dl">
              <dt>Role</dt><dd><strong>${esc(contact.title)}</strong></dd>
              <dt>Company</dt><dd><strong>${esc(contact.account)}</strong></dd>
              <dt>Level</dt><dd><strong>${esc(contact.level)}</strong></dd>
              <dt>Email</dt><dd><strong>${esc(contact.email)}</strong></dd>
              <dt>Phone</dt><dd><strong>${esc(contact.phone)}</strong></dd>
            </dl>
          </section>

          <section aria-label="Influence" class="cc-sheet-card cc-influence-card">
            <h2 class="cc-card-eyebrow">Why she matters</h2>
            <p class="cc-influence-body">${esc(contact.whyMatters)}</p>
          </section>

          <section aria-label="Linked deals" class="cc-sheet-card cc-linked-deals-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Linked deals</h2>
            </div>
            <ul class="cc-linked-deals-list">
              ${contact.linkedDeals.map(d => `
                <li class="cc-linked-deal-row is-clickable" data-deal-key="${esc(d.key)}">
                  <div class="cc-linked-deal-info">
                    <div class="cc-linked-deal-name"><strong>${esc(d.name)}</strong></div>
                    <div class="cc-linked-deal-sub">${esc(d.stage)}</div>
                  </div>
                  <strong class="cc-linked-deal-val">${esc(d.value)}</strong>
                </li>
              `).join('')}
            </ul>
          </section>
        </div>

        <div class="cc-sheet-col-right">
          <section aria-label="Next step" class="cc-sheet-card cc-needs-you-card">
            <h2 class="cc-card-eyebrow cc-eyebrow-alert">Suggested next step</h2>
            <p class="cc-needs-headline" style="font-size: 14px; font-weight: normal">${esc(contact.suggestedNextStep)}</p>
          </section>

          <section aria-label="Relationship timeline" class="cc-sheet-card cc-activity-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Relationship timeline</h2>
            </div>
            <ol class="cc-activity-timeline-list">
              ${contact.timeline.map(t => `
                <li class="cc-activity-item">
                  <div class="cc-activity-meta">${esc(t.date)}</div>
                  <div class="cc-activity-desc">${esc(t.text)}</div>
                </li>
              `).join('')}
            </ol>
          </section>

          <section aria-label="Contacts she can introduce" class="cc-sheet-card cc-intro-card">
            <h2 class="cc-card-eyebrow">Can introduce you to</h2>
            <p class="cc-intro-body">${esc(contact.introductions.text)}</p>
            <a href="#introductions" class="cc-link-strong" data-action="open-introductions">Open introductions</a>
          </section>
        </div>
      </div>
    </div>
  `;

  bindContactDetailEvents(contact);
}

function bindContactDetailEvents(contact) {
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  breadcrumb?.querySelector('[data-account]')?.addEventListener('click', (e) => {
    e.preventDefault();
    openAccountDetail(contact.accountKey);
  });

  content.querySelectorAll('.cc-linked-deal-row').forEach(row => {
    row.addEventListener('click', () => {
      const dealKey = row.dataset.dealKey;
      if (dealKey) openDealDetail(dealKey);
    });
  });

  content.querySelector('[data-action="log-touchpoint"]')?.addEventListener('click', () => {
    showToast(`Logged touchpoint with ${contact.name}`);
  });

  content.querySelector('[data-action="draft-followup"]')?.addEventListener('click', () => {
    openCopilotModal({
      dealKey: contact.linkedDeals[0]?.key,
      prompt: `Draft a personalized follow-up note to ${contact.name} (${contact.title} at ${contact.account}) concerning ${contact.suggestedNextStep}`
    });
  });

  content.querySelector('[data-action="open-introductions"]')?.addEventListener('click', (e) => {
    e.preventDefault();
    openIntroductionsWorkspace();
  });
}

// ── 4. Copilot Workspace Rendering (Copilot.dc.html) ────────────────────────
export function openCopilotModal(options = {}) {
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  const dealKey = options.dealKey || 'bny-pilot';
  const deal = CANONICAL_DEALS_DETAIL[dealKey] || CANONICAL_DEALS_DETAIL['bny-pilot'];
  const initialPrompt = options.prompt || '';

  openSheetContainer('copilot', dealKey);

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#pipeline" class="cc-sheet-crumb-link" data-nav="pipeline">Command Center</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr"><i class="fa-solid fa-wand-magic-sparkles" style="color: var(--accent)"></i> Copilot — deal help</span>
      </nav>
    `;
  }

  content.innerHTML = `
    <div class="cc-copilot-workspace-view">
      <div class="cc-copilot-layout">
        <nav aria-label="Conversations" class="cc-copilot-sidebar-nav">
          <button type="button" class="cc-btn cc-btn-primary cc-btn-block cc-copilot-new-chat" id="ccCopilotNewChat">
            <i class="fa-solid fa-plus"></i> New chat
          </button>
          <div class="cc-copilot-nav-section-title">Recent</div>
          <div class="cc-copilot-recent-list">
            ${CANONICAL_COPILOT_CHATS.map(chat => `
              <a href="#${chat.id}" class="cc-copilot-chat-link ${chat.dealKey === dealKey ? 'is-active' : ''}" data-deal-key="${esc(chat.dealKey)}">
                <i class="fa-regular fa-message"></i> ${esc(chat.title)}
              </a>
            `).join('')}
          </div>
        </nav>

        <main aria-label="Conversation" class="cc-copilot-main-pane">
          <div class="cc-copilot-messages-container" id="ccCopilotMessages">
            <div class="cc-copilot-bubble cc-bubble-user">
              What should I do before the ${esc(deal.account)} readout?
              <span class="cc-bubble-tag">Example prompt</span>
            </div>

            <div class="cc-copilot-bubble cc-bubble-assistant">
              <p class="cc-bubble-intro">The readout is 13 days late, so reschedule first and open with the leadership change. Three things to do before the call:</p>
              <ol class="cc-bubble-list">
                <li><strong>Check the new role.</strong> Alaa AlMashhadi starts a new role. Ask whether they join the buying committee.</li>
                <li><strong>Show the exit criteria you can prove.</strong> The pilot has 0 of 4 criteria marked done. Mark any that are already met.</li>
                <li><strong>Bring a one-page summary.</strong> Cover the pilot results so far and the proposed next phase.</li>
              </ol>
              <div class="cc-bubble-actions">
                <button type="button" class="cc-btn cc-btn-outline cc-btn-sm" id="ccCopilotDraftAction"><i class="fa-solid fa-pen"></i> Draft the reschedule email</button>
                <button type="button" class="cc-btn cc-btn-outline cc-btn-sm" id="ccCopilotExitCriteriaAction" data-deal="${esc(deal.key)}"><i class="fa-solid fa-list-check"></i> Open exit criteria</button>
              </div>
              <div class="cc-bubble-sources"><i class="fa-solid fa-circle-check" style="color: #10B981"></i> Sources: ${esc(deal.account)} deal record, signal feed, pilot exit criteria</div>
            </div>
          </div>

          <form class="cc-copilot-composer-form" id="ccCopilotForm" onsubmit="return false">
            <div class="cc-copilot-prompt-chips">
              <button type="button" class="cc-prompt-chip" data-prompt="Summarize the ${esc(deal.account)} account and deal health.">Summarize the account</button>
              <button type="button" class="cc-prompt-chip" data-prompt="Find the economic buyer and gaps in ${esc(deal.account)} buying committee.">Find the economic buyer</button>
              <button type="button" class="cc-prompt-chip" data-prompt="Write a follow-up email addressing the overdue next step for ${esc(deal.name)}.">Write a follow-up</button>
            </div>
            <div class="cc-copilot-input-row">
              <label for="ccCopilotInput" class="sr-only">Ask Copilot</label>
              <input type="text" id="ccCopilotInput" class="cc-copilot-input" placeholder="Ask about this deal or account" value="${esc(initialPrompt)}" autocomplete="off">
              <button type="submit" class="cc-btn cc-btn-primary cc-copilot-send-btn" id="ccCopilotSend"><i class="fa-solid fa-arrow-up"></i> Send</button>
            </div>
          </form>
        </main>

        <aside aria-label="Context" class="cc-copilot-context-aside">
          <div class="cc-copilot-context-block">
            <div class="cc-context-eyebrow">Working on</div>
            <div class="cc-context-deal-title">${esc(deal.name)}</div>
            <div class="cc-context-deal-sub">${esc(deal.stageLabel)} · ${esc(deal.valueText)} · close ${esc(deal.closeDate)} · health ${deal.health}</div>
          </div>

          <div class="cc-copilot-context-block">
            <div class="cc-context-eyebrow">Sources Copilot can use</div>
            <ul class="cc-copilot-sources-list">
              <li><span>Deal record</span><span class="cc-source-on">On</span></li>
              <li><span>Account contacts</span><span class="cc-source-on">On</span></li>
              <li><span>Signal feed</span><span class="cc-source-on">On</span></li>
              <li><span>Email history</span><span class="cc-source-off">Off</span></li>
            </ul>
          </div>

          <div class="cc-copilot-context-block">
            <div class="cc-context-eyebrow">Quick links</div>
            <div class="cc-copilot-links-list">
              <a href="#deal-${esc(deal.key)}" class="cc-quick-link" id="ccLinkOpenDeal"><i class="fa-solid fa-arrow-right"></i> Open deal detail</a>
              <a href="#account-${esc(deal.account.toLowerCase())}" class="cc-quick-link" id="ccLinkOpenAccount"><i class="fa-solid fa-arrow-right"></i> Open ${esc(deal.account)} account</a>
            </div>
          </div>
        </aside>
      </div>
    </div>
  `;

  bindCopilotEvents(deal);

  if (initialPrompt) {
    submitCopilotPrompt(initialPrompt, deal);
  }
}

function bindCopilotEvents(deal) {
  const { content } = getModalElements();
  if (!content) return;

  const form = document.getElementById('ccCopilotForm');
  const input = document.getElementById('ccCopilotInput');
  const messages = document.getElementById('ccCopilotMessages');

  form?.addEventListener('submit', (e) => {
    e.preventDefault();
    const val = input.value.trim();
    if (!val) return;
    input.value = '';
    submitCopilotPrompt(val, deal);
  });

  content.querySelectorAll('.cc-prompt-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      const promptText = chip.dataset.prompt;
      if (promptText) submitCopilotPrompt(promptText, deal);
    });
  });

  document.getElementById('ccCopilotExitCriteriaAction')?.addEventListener('click', () => {
    openDealDetail(deal.key);
  });

  document.getElementById('ccCopilotDraftAction')?.addEventListener('click', () => {
    submitCopilotPrompt(`Draft the reschedule email for the ${deal.account} CIO pilot readout.`, deal);
  });

  document.getElementById('ccLinkOpenDeal')?.addEventListener('click', (e) => {
    e.preventDefault();
    openDealDetail(deal.key);
  });

  document.getElementById('ccLinkOpenAccount')?.addEventListener('click', (e) => {
    e.preventDefault();
    openAccountDetail(deal.account.toLowerCase());
  });

  content.querySelectorAll('.cc-copilot-chat-link').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const targetDealKey = link.dataset.dealKey;
      openCopilotModal({ dealKey: targetDealKey });
    });
  });

  document.getElementById('ccCopilotNewChat')?.addEventListener('click', () => {
    if (messages) {
      messages.innerHTML = `
        <div class="cc-copilot-bubble cc-bubble-assistant">
          <p class="cc-bubble-intro">Started a new Copilot session. Ask any question about your deals, accounts, executive movements, or ask me to draft a follow-up.</p>
        </div>
      `;
    }
  });

  breadcrumbNavPipeline();
}

async function submitCopilotPrompt(promptText, deal) {
  const messages = document.getElementById('ccCopilotMessages');
  if (!messages) return;

  const userDiv = document.createElement('div');
  userDiv.className = 'cc-copilot-bubble cc-bubble-user';
  userDiv.textContent = promptText;
  messages.appendChild(userDiv);

  const thinkingDiv = document.createElement('div');
  thinkingDiv.className = 'cc-copilot-bubble cc-bubble-assistant cc-thinking';
  thinkingDiv.innerHTML = `<i class="fa-solid fa-spinner fa-spin"></i> Copilot is synthesizing deal intelligence and telemetry…`;
  messages.appendChild(thinkingDiv);
  messages.scrollTop = messages.scrollHeight;

  setTimeout(() => {
    thinkingDiv.classList.remove('cc-thinking');

    const lower = promptText.toLowerCase();
    let replyHtml = '';

    if (lower.includes('reschedule') || lower.includes('readout') || lower.includes('bny')) {
      replyHtml = `
        <p><strong>Proposed Email Draft to BNY CIO's Office:</strong></p>
        <div style="background: var(--bg-surface-alt, #F5F6F9); padding: 12px; border-left: 3px solid var(--accent); margin: 8px 0; font-family: monospace; font-size: 13px; line-height: 1.5;">
          Subject: Rescheduling our AI Governance Pilot Readout + Welcoming Alaa<br><br>
          Dear Michael,<br><br>
          First, congratulations to the BNY team on Alaa AlMashhadi's new leadership appointment across AI platform architecture.<br><br>
          I would like to reschedule our CIO pilot readout session to share our telemetry findings. We have already satisfied the enterprise security criteria and have benchmarks ready on model risk evaluation.<br><br>
          Would next Tuesday at 2:00 PM EST or Thursday at 10:00 AM EST work for you and the committee?<br><br>
          Best regards,<br>
          Jordan Patel
        </div>
        <div class="cc-bubble-actions">
          <button type="button" class="cc-btn cc-btn-primary cc-btn-sm" onclick="navigator.clipboard.writeText(this.closest('.cc-bubble-assistant').querySelector('div').innerText); window.showToast('Copied draft email to clipboard!');"><i class="fa-solid fa-copy"></i> Copy Email Draft</button>
        </div>
        <div class="cc-bubble-sources"><i class="fa-solid fa-circle-check" style="color: #10B981"></i> Synthesized from BNY deal record, executive moves feed, and pilot criteria</div>
      `;
    } else if (lower.includes('economic buyer') || lower.includes('committee')) {
      replyHtml = `
        <p><strong>Buying Committee Analysis for ${esc(deal.account)}:</strong></p>
        <ul class="cc-bubble-list">
          <li><strong>Current Champion:</strong> Engaged via technical platform team.</li>
          <li><strong>Missing Economic Buyer:</strong> Neither procurement head nor VP Budget Owner is formally mapped.</li>
          <li><strong>Next Step:</strong> Request a warm introduction via your primary champion to map the signer before the contract stage.</li>
        </ul>
        <div class="cc-bubble-sources"><i class="fa-solid fa-circle-check" style="color: #10B981"></i> Source: CRM Buying Committee matrix</div>
      `;
    } else {
      replyHtml = `
        <p><strong>Account & Opportunity Summary:</strong></p>
        <p>The opportunity is currently valued at <strong>${esc(deal.valueText)}</strong> in <strong>${esc(deal.stageLabel)}</strong> with an expected close date of <strong>${esc(deal.closeDate)}</strong>.</p>
        <ul class="cc-bubble-list">
          <li>Deal Health Score: <strong>${deal.health}/100</strong></li>
          <li>Recent Activity: ${deal.activity[0]?.text || 'Active in recent cycle'}</li>
          <li>Action Required: ${esc(deal.suggestedNextStep.text)}</li>
        </ul>
        <div class="cc-bubble-sources"><i class="fa-solid fa-circle-check" style="color: #10B981"></i> Source: Sales AI Telemetry Store</div>
      `;
    }

    thinkingDiv.innerHTML = replyHtml;
    messages.scrollTop = messages.scrollHeight;
  }, 450);
}

// ── 5. DealsList Workspace (DealsList.dc.html) ───────────────────────────────
export function openDealsList(options = {}) {
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  openSheetContainer('deals-list', 'all');

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#pipeline" class="cc-sheet-crumb-link" data-nav="pipeline">Command Center</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr">Deals — all open deals</span>
      </nav>
    `;
  }

  let activeFilter = 'all';

  function renderRows(filter) {
    const list = filter === 'all' ? CANONICAL_ALL_DEALS : CANONICAL_ALL_DEALS.filter(d => d.stage === filter);
    return list.map(d => {
      const healthTone = d.health < 40 ? 'tone-risk' : d.health < 70 ? 'tone-watch' : 'tone-good';
      return `
        <tr class="cc-dl-tr is-clickable" data-deal-key="${esc(d.key)}" style="border-top: 1px solid #EEF0F5">
          <td style="padding: 12px 14px; text-align: left">
            <div style="font-weight: 700; color: #001233">${esc(d.name)}</div>
            <div style="font-size: 12px; color: #4C5671">${esc(d.account)}${d.tag ? ` · ${esc(d.tag)}` : ''}</div>
          </td>
          <td style="padding: 12px 8px">${esc(d.stageLabel)}</td>
          <td style="padding: 12px 8px; text-align: right; font-variant-numeric: tabular-nums; font-weight: 700">${esc(d.value)}</td>
          <td style="padding: 12px 8px; text-align: center">
            <span class="cc-health-pill ${healthTone}">${d.health}</span>
          </td>
          <td style="padding: 12px 8px; color: ${d.nextStepLate ? '#B3261E' : '#001233'}; font-weight: ${d.nextStepLate ? '600' : 'normal'}">${esc(d.nextStep)}</td>
          <td style="padding: 12px 14px; text-align: right; color: ${d.lastTouchLate ? '#B3261E' : '#4C5671'}; font-weight: ${d.lastTouchLate ? '700' : 'normal'}">${esc(d.lastTouch)}</td>
        </tr>
      `;
    }).join('');
  }

  content.innerHTML = `
    <div class="cc-deals-list-workspace">
      <div class="cc-sheet-header">
        <div class="cc-sheet-header-main">
          <h1 class="cc-sheet-title">Deals</h1>
          <div class="cc-sheet-subtitle">9 open deals · $2.29M · sorted by most urgent next step</div>
        </div>
        <div role="group" aria-label="Filter by stage" class="cc-deals-stage-filter-group" id="ccDealsListFilters">
          <button type="button" class="cc-dl-stage-btn is-active" data-stage="all">All 9</button>
          <button type="button" class="cc-dl-stage-btn" data-stage="intro">Intro 1</button>
          <button type="button" class="cc-dl-stage-btn" data-stage="discovery">Discovery 1</button>
          <button type="button" class="cc-dl-stage-btn" data-stage="proposal">Proposal 4</button>
          <button type="button" class="cc-dl-stage-btn" data-stage="pilot">Pilot 2</button>
          <button type="button" class="cc-dl-stage-btn" data-stage="contract">Contract 1</button>
        </div>
      </div>

      <div class="cc-dl-kpi-grid">
        <div class="cc-dl-kpi-card" style="border-top: 4px solid #B3261E">
          <div class="cc-dl-kpi-label">Deals at risk</div>
          <div class="cc-dl-kpi-val">4 of 9 · $685k</div>
        </div>
        <div class="cc-dl-kpi-card" style="border-top: 4px solid #D89B1B">
          <div class="cc-dl-kpi-label">Gone quiet</div>
          <div class="cc-dl-kpi-val">2 deals · $830k</div>
        </div>
        <div class="cc-dl-kpi-card" style="border-top: 4px solid #001233">
          <div class="cc-dl-kpi-label">No economic buyer</div>
          <div class="cc-dl-kpi-val">6 deals</div>
        </div>
        <div class="cc-dl-kpi-card" style="border-top: 4px solid var(--accent, #E46521)">
          <div class="cc-dl-kpi-label">Won this quarter</div>
          <div class="cc-dl-kpi-val">$200k</div>
        </div>
      </div>

      <section aria-label="Open deals" class="cc-sheet-card" style="overflow: auto">
        <table class="cc-deals-full-table" style="width: 100%; border-collapse: collapse; font-size: 14px">
          <thead>
            <tr style="background: #F5F6F9; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; color: #4C5671">
              <th scope="col" style="text-align: left; padding: 10px 14px">Deal</th>
              <th scope="col" style="text-align: left; padding: 10px 8px">Stage</th>
              <th scope="col" style="text-align: right; padding: 10px 8px">Value</th>
              <th scope="col" style="text-align: center; padding: 10px 8px">Health</th>
              <th scope="col" style="text-align: left; padding: 10px 8px">Next step</th>
              <th scope="col" style="text-align: right; padding: 10px 14px">Last touch</th>
            </tr>
          </thead>
          <tbody id="ccDealsListTbody">
            ${renderRows('all')}
          </tbody>
        </table>
      </section>
    </div>
  `;

  const tbody = document.getElementById('ccDealsListTbody');
  const filters = document.getElementById('ccDealsListFilters');

  filters?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-stage]');
    if (!btn) return;
    filters.querySelectorAll('.cc-dl-stage-btn').forEach(b => b.classList.remove('is-active'));
    btn.classList.add('is-active');
    activeFilter = btn.dataset.stage;
    if (tbody) tbody.innerHTML = renderRows(activeFilter);
    bindRowClicks();
  });

  function bindRowClicks() {
    content.querySelectorAll('.cc-dl-tr').forEach(tr => {
      tr.addEventListener('click', () => {
        const key = tr.dataset.dealKey;
        if (key) openDealDetail(key);
      });
    });
  }

  bindRowClicks();
  breadcrumbNavPipeline();
}

// ── 6. Introductions Workspace (Introductions.dc.html) ──────────────────────
export function openIntroductionsWorkspace(options = {}) {
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  openSheetContainer('introductions', 'all');

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#pipeline" class="cc-sheet-crumb-link" data-nav="pipeline">Command Center</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr">Introductions — who can open the door</span>
      </nav>
    `;
  }

  content.innerHTML = `
    <div class="cc-introductions-workspace">
      <div class="cc-sheet-header">
        <div class="cc-sheet-header-main">
          <h1 class="cc-sheet-title">Introductions</h1>
          <div class="cc-sheet-subtitle">Who can open the door to the buyers on my deals · 1 open request · 2 deals with no economic buyer</div>
        </div>
      </div>

      <div class="cc-intros-split-grid">
        <section aria-label="Open introduction requests" class="cc-sheet-card cc-intros-left-pane">
          <div class="cc-intros-header-bar">
            <h2 class="cc-card-title">Requests</h2>
            <div role="tablist" class="cc-intros-tabs">
              <button type="button" class="cc-intros-tab is-active" data-tab="open">Open 1</button>
              <button type="button" class="cc-intros-tab" data-tab="waiting">Waiting on a reply</button>
              <button type="button" class="cc-intros-tab" data-tab="done">Done</button>
            </div>
          </div>

          <article class="cc-intro-card-stale">
            <span class="cc-intro-stale-line"></span>
            <div class="cc-intro-details">
              <div class="cc-intro-subject">Introduction to Anthony Portannese at DTCC</div>
              <div class="cc-intro-meta">Asked: <strong>James Whitfield</strong> · waiting on the connector · <strong style="color: #B3261E">the intro is stale</strong></div>
              <div class="cc-intro-linked">Linked deal: <strong>DTCC — Digital assets settlement pilot ($380k)</strong> · quiet 26 days</div>
              <div class="cc-intro-actions">
                <button type="button" class="cc-btn cc-btn-outline cc-btn-sm" data-action="intro-decline">Mark declined</button>
                <button type="button" class="cc-btn cc-btn-outline cc-btn-sm" data-action="intro-reply">Log a reply</button>
                <button type="button" class="cc-btn cc-btn-primary cc-btn-sm" data-action="intro-nudge">Nudge James Whitfield</button>
              </div>
            </div>
          </article>

          <div class="cc-intros-footer-note">
            Requests you sent appear here until the contact replies or the intro is marked done.
          </div>
        </section>

        <aside aria-label="Find who can introduce you" class="cc-intros-right-pane">
          <section class="cc-sheet-card cc-no-eb-card" style="border-left: 4px solid #B3261E">
            <h2 class="cc-card-eyebrow cc-eyebrow-alert">Deals with no economic buyer</h2>
            <ul class="cc-no-eb-list">
              <li>
                <div>
                  <strong>DTCC — AI-assisted reconciliation</strong>
                  <span class="cc-eb-deal-sub">Proposal · $275k</span>
                </div>
                <button type="button" class="cc-btn cc-btn-outline cc-btn-xs" data-action="find-intro" data-account="DTCC">Find an introducer</button>
              </li>
              <li>
                <div>
                  <strong>BlackRock — Cloud cost optimisation (EMEA)</strong>
                  <span class="cc-eb-deal-sub">Proposal · $210k</span>
                </div>
                <button type="button" class="cc-btn cc-btn-outline cc-btn-xs" data-action="find-intro" data-account="BlackRock">Find an introducer</button>
              </li>
            </ul>
          </section>

          <section class="cc-sheet-card cc-suggested-intros-card">
            <div class="cc-card-header-bar">
              <h2 class="cc-card-title">Suggested introducers</h2>
            </div>
            <ul class="cc-suggested-list">
              <li>
                <div class="cc-sugg-name">James Whitfield</div>
                <div class="cc-sugg-sub">Knows Anthony Portannese at DTCC · intro pending</div>
              </li>
              <li>
                <div class="cc-sugg-name">Priya Nair</div>
                <div class="cc-sugg-sub">Knows Laurence Fink at BlackRock · intro made</div>
              </li>
              <li>
                <div class="cc-sugg-name">Michael Santomassimo</div>
                <div class="cc-sugg-sub">CIO office at BNY · relationship to economic buyer</div>
              </li>
            </ul>
          </section>
        </aside>
      </div>
    </div>
  `;

  content.querySelector('[data-action="intro-decline"]')?.addEventListener('click', () => {
    showToast('Introduction marked declined.');
  });
  content.querySelector('[data-action="intro-reply"]')?.addEventListener('click', () => {
    showToast('Logged connector reply from James Whitfield.');
  });
  content.querySelector('[data-action="intro-nudge"]')?.addEventListener('click', () => {
    openCopilotModal({
      dealKey: 'dtcc-pilot',
      prompt: 'Draft a polite follow-up nudge to James Whitfield asking if he had a chance to connect with Anthony Portannese at DTCC.'
    });
  });
  content.querySelectorAll('[data-action="find-intro"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const acct = btn.dataset.account;
      openCopilotModal({
        prompt: `Analyze our network and recommend warm connectors to introduce us to the economic buyer at ${acct}.`
      });
    });
  });

  breadcrumbNavPipeline();
}

// ── 7. Playbook Workspace (Playbook.dc.html) ─────────────────────────────────
export function openPlaybookWorkspace(options = {}) {
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  openSheetContainer('playbook', 'all');

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#pipeline" class="cc-sheet-crumb-link" data-nav="pipeline">Command Center</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr">How to sell it — plays and objections</span>
      </nav>
    `;
  }

  content.innerHTML = `
    <div class="cc-playbook-workspace">
      <div class="cc-sheet-header">
        <div class="cc-sheet-header-main">
          <h1 class="cc-sheet-title">How to sell it</h1>
          <div class="cc-sheet-subtitle">Plays, investment tracks, pain points and objections · 5 plays this week, 2 already tasked</div>
        </div>
      </div>

      <div class="cc-playbook-2x2-grid">
        <section aria-label="This week's playbook" class="cc-sheet-card cc-pb-plays-pane">
          <div class="cc-card-header-bar">
            <h2 class="cc-card-title">This week's playbook</h2>
            <span style="font-size: 12px; color: #4C5671">5 plays</span>
          </div>
          <div class="cc-pb-plays-list">
            <div class="cc-pb-play-item">
              <span class="cc-pb-play-num">1</span>
              <div class="cc-pb-play-info">
                <strong>Expand AI Governance pilot with model validation track</strong>
                <div class="cc-pb-play-meta">BNY · CIO office readout timing hook</div>
              </div>
              <span class="cc-status-pill tone-good" style="align-self: center">Tasked</span>
            </div>
            <div class="cc-pb-play-item">
              <span class="cc-pb-play-num">2</span>
              <div class="cc-pb-play-info">
                <strong>Return automated test framework MSA redlines</strong>
                <div class="cc-pb-play-meta">BlackRock · Procurement deadline 30 Nov</div>
              </div>
              <button type="button" class="cc-btn cc-btn-outline cc-btn-xs cc-pb-task-btn" data-play="blackrock">Create task</button>
            </div>
            <div class="cc-pb-play-item">
              <span class="cc-pb-play-num">3</span>
              <div class="cc-pb-play-info">
                <strong>Re-engage digital assets settlement pilot with regulatory hook</strong>
                <div class="cc-pb-play-meta">DTCC · leverage post-trade announcement</div>
              </div>
              <button type="button" class="cc-btn cc-btn-outline cc-btn-xs cc-pb-task-btn" data-play="dtcc">Create task</button>
            </div>
            <div class="cc-pb-play-item">
              <span class="cc-pb-play-num">4</span>
              <div class="cc-pb-play-info">
                <strong>Map cloud landing zone security champion</strong>
                <div class="cc-pb-play-meta">Northern Trust · fill buying committee gap</div>
              </div>
              <button type="button" class="cc-btn cc-btn-outline cc-btn-xs cc-pb-task-btn" data-play="nt">Create task</button>
            </div>
            <div class="cc-pb-play-item">
              <span class="cc-pb-play-num">5</span>
              <div class="cc-pb-play-info">
                <strong>Whitespace outreach on multi-asset digital fund</strong>
                <div class="cc-pb-play-meta">Vanguard · new business opportunity</div>
              </div>
              <button type="button" class="cc-btn cc-btn-outline cc-btn-xs cc-pb-task-btn" data-play="vanguard">Create task</button>
            </div>
          </div>
        </section>

        <section aria-label="Investment tracks and sponsors" class="cc-sheet-card">
          <div class="cc-card-header-bar">
            <h2 class="cc-card-title">Investment tracks &amp; sponsors</h2>
          </div>
          <div class="cc-pb-tracks-list">
            <div class="cc-pb-track-item">
              <strong>AI governance · BNY</strong>
              <div class="cc-pb-track-meta">Sponsor: Alaa AlMashhadi · matches the Applied AI governance pilot ($320k)</div>
            </div>
            <div class="cc-pb-track-item">
              <strong>Digital assets · DTCC</strong>
              <div class="cc-pb-track-meta">Sponsor: Anthony Portannese · matches the settlement pilot ($380k)</div>
            </div>
            <div class="cc-pb-track-item">
              <strong>Automated testing · BlackRock</strong>
              <div class="cc-pb-track-meta">Sponsor: Priya Nair · matches contract ($450k)</div>
            </div>
            <div class="cc-pb-track-item">
              <strong>Cloud modernization · Northern Trust</strong>
              <div class="cc-pb-track-meta">Sponsor: IT Infrastructure Lead · no deal yet</div>
            </div>
          </div>
        </section>

        <section aria-label="Operational pain points" class="cc-sheet-card">
          <div class="cc-card-header-bar">
            <h2 class="cc-card-title">Operational pain points</h2>
          </div>
          <div class="cc-pb-pain-list">
            <div class="cc-pb-pain-item">
              <strong>Model audit and regulatory compliance latency</strong>
              <div class="cc-pb-pain-meta">BNY, DTCC · evidence from SEC/FINRA regulatory filings</div>
            </div>
            <div class="cc-pb-pain-item">
              <strong>High test verification costs and environment drift</strong>
              <div class="cc-pb-pain-meta">BlackRock · evidence from NYC senior engineer job posts</div>
            </div>
            <div class="cc-pb-pain-item">
              <strong>Unmapped IAM roles in multi-cloud landing zones</strong>
              <div class="cc-pb-pain-meta">Northern Trust · security RFIs and cloud posture audit</div>
            </div>
          </div>
        </section>

        <section aria-label="Common objections and answers" class="cc-sheet-card">
          <div class="cc-card-header-bar">
            <h2 class="cc-card-title">Common objections and answers</h2>
          </div>
          <div class="cc-pb-objections-list">
            <details open class="cc-pb-objection-details">
              <summary>"We already have an internal testing framework built on legacy tools."</summary>
              <div class="cc-pb-obj-answer">
                <strong>Answer:</strong> Our solution integrates non-invasively alongside your existing stack and cuts regression testing cycles from 4 weeks down to 2 hours.
                <div class="cc-pb-obj-source">Heard at BlackRock</div>
              </div>
            </details>
            <details class="cc-pb-objection-details">
              <summary>"Procurement approval takes 60+ days for new vendors."</summary>
              <div class="cc-pb-obj-answer">
                <strong>Answer:</strong> We are already pre-cleared under enterprise cloud marketplace agreements (AWS/Azure) so deals can bypass standard new-vendor review.
                <div class="cc-pb-obj-source">Heard at BNY</div>
              </div>
            </details>
            <details class="cc-pb-objection-details">
              <summary>"We need compliance approval before any live data touches models."</summary>
              <div class="cc-pb-obj-answer">
                <strong>Answer:</strong> All testing telemetry runs entirely in your sovereign VPC or on-prem enclave; zero customer data ever leaves your perimeter.
                <div class="cc-pb-obj-source">Heard at DTCC</div>
              </div>
            </details>
          </div>
        </section>
      </div>
    </div>
  `;

  content.querySelectorAll('.cc-pb-task-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      btn.outerHTML = `<span class="cc-status-pill tone-good" style="align-self: center">Tasked</span>`;
      showToast('Play converted to actionable task!');
    });
  });

  breadcrumbNavPipeline();
}

// ── 8. SignalFeed Workspace (SignalFeed.dc.html) ─────────────────────────────
export function openSignalFeedWorkspace(options = {}) {
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  openSheetContainer('signals', 'all');

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#pipeline" class="cc-sheet-crumb-link" data-nav="pipeline">Command Center</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr">Signal feed — across my accounts</span>
      </nav>
    `;
  }

  const rawSignals = [
    { type: 'exec', typeLabel: 'Exec move', date: '3 days ago', title: 'Alaa AlMashhadi starts a new role', meta: 'BNY · linked to BNY — Applied AI governance pilot ($320k)', action: 'review_bny', actionLabel: 'Review deal' },
    { type: 'hiring', typeLabel: 'Hiring', date: '4 days ago', title: '4 new senior cloud and AI testing roles posted', meta: 'BlackRock · linked to BlackRock — AI agent testing framework ($450k)', action: 'open_blackrock', actionLabel: 'Open account' },
    { type: 'news', typeLabel: 'News', date: '5 days ago', title: 'DTCC announces digital post-trade settlement pilot program', meta: 'DTCC · linked to DTCC — Digital assets settlement pilot ($380k)', action: 'open_dtcc', actionLabel: 'Open account' },
    { type: 'exec', typeLabel: 'Exec move', date: '1 week ago', title: 'Michael Santomassimo expands AI oversight committee', meta: 'BNY · linked to BNY — Fund accounting data quality ($180k)', action: 'review_bny', actionLabel: 'Review deal' },
    { type: 'capital', typeLabel: 'Capital event', date: '1 week ago', title: 'Vanguard announces multi-asset technology fund allocation', meta: 'Vanguard · no open deal yet, whitespace', action: 'add_deal', actionLabel: 'Add a deal' }
  ];

  function renderSignals(filter) {
    const list = filter === 'all' ? rawSignals : rawSignals.filter(s => s.type === filter);
    return list.map(s => `
      <article class="cc-signal-row" data-type="${esc(s.type)}">
        <div class="cc-sig-type-col">
          <span class="cc-sig-tag tag-${esc(s.type)}">${esc(s.typeLabel)}</span>
          <div class="cc-sig-date">${esc(s.date)}</div>
        </div>
        <div class="cc-sig-body-col">
          <div class="cc-sig-title">${esc(s.title)}</div>
          <div class="cc-sig-meta">${esc(s.meta)}</div>
        </div>
        <button type="button" class="cc-btn ${s.action.startsWith('review') ? 'cc-btn-primary' : 'cc-btn-outline'} cc-btn-sm cc-sig-act" data-action="${esc(s.action)}">
          ${esc(s.actionLabel)}
        </button>
      </article>
    `).join('');
  }

  content.innerHTML = `
    <div class="cc-signals-workspace">
      <div class="cc-sheet-header">
        <div class="cc-sheet-header-main">
          <h1 class="cc-sheet-title">Signal feed</h1>
          <div class="cc-sheet-subtitle">Changes at my accounts this week · 211 growth themes tracked</div>
        </div>
        <div role="group" aria-label="Filter signals" class="cc-signals-filter-group" id="ccSignalFilterGroup">
          <button type="button" class="cc-sig-filter-btn is-active" data-filter="all">All</button>
          <button type="button" class="cc-sig-filter-btn" data-filter="exec">Exec moves</button>
          <button type="button" class="cc-sig-filter-btn" data-filter="hiring">Hiring</button>
          <button type="button" class="cc-sig-filter-btn" data-filter="capital">Capital events</button>
          <button type="button" class="cc-sig-filter-btn" data-filter="news">News</button>
        </div>
      </div>

      <div class="cc-signals-split-grid">
        <section aria-label="Signals" class="cc-sheet-card cc-signals-stream" id="ccSignalsStream">
          ${renderSignals('all')}
        </section>

        <aside aria-label="This week at a glance" class="cc-signals-aside">
          <section class="cc-sheet-card cc-signals-stat-box">
            <h2 class="cc-card-title" style="padding: 14px 16px; border-bottom: 1px solid #EEF0F5">This week</h2>
            <div class="cc-signal-stat-row"><span>Exec moves</span><strong>2</strong></div>
            <div class="cc-signal-stat-row"><span>New job postings</span><strong>5</strong></div>
            <div class="cc-signal-stat-row"><span>Capital events</span><strong>1</strong></div>
            <div class="cc-signal-stat-row"><span>News articles</span><strong>4</strong></div>
          </section>

          <section class="cc-sheet-card cc-growth-themes-box" style="padding: 16px">
            <h2 class="cc-card-eyebrow">Growth themes</h2>
            <p style="margin: 0 0 12px; font-size: 14px; line-height: 1.5; color: #001233">211 themes tracked across my accounts. Use them to open each conversation.</p>
            <a href="#playbook" class="cc-link-strong" id="ccBrowseThemesLink">Browse growth themes</a>
          </section>
        </aside>
      </div>
    </div>
  `;

  const stream = document.getElementById('ccSignalsStream');
  const group = document.getElementById('ccSignalFilterGroup');

  group?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-filter]');
    if (!btn) return;
    group.querySelectorAll('.cc-sig-filter-btn').forEach(b => b.classList.remove('is-active'));
    btn.classList.add('is-active');
    if (stream) stream.innerHTML = renderSignals(btn.dataset.filter);
    bindSignalActions();
  });

  function bindSignalActions() {
    content.querySelectorAll('.cc-sig-act').forEach(btn => {
      btn.addEventListener('click', () => {
        const act = btn.dataset.action;
        if (act === 'review_bny') openDealDetail('bny-pilot');
        else if (act === 'open_blackrock') openAccountDetail('blackrock');
        else if (act === 'open_dtcc') openAccountDetail('dtcc');
        else if (act === 'add_deal') showToast('Opening New Deal workflow for Vanguard...');
      });
    });
  }

  document.getElementById('ccBrowseThemesLink')?.addEventListener('click', (e) => {
    e.preventDefault();
    openPlaybookWorkspace();
  });

  bindSignalActions();
  breadcrumbNavPipeline();
}

// ── 9. MorePhone Drawer (MorePhone.dc.html) ──────────────────────────────────
export function openMorePhoneDrawer(options = {}) {
  const { content, breadcrumb } = getModalElements();
  if (!content) return;

  openSheetContainer('more', 'all');

  if (breadcrumb) {
    breadcrumb.innerHTML = `
      <nav aria-label="Breadcrumb" class="cc-sheet-breadcrumb">
        <a href="#day" class="cc-sheet-crumb-link" data-nav="day">Command Center</a>
        <span aria-hidden="true" class="cc-crumb-sep">/</span>
        <span class="cc-crumb-curr">More</span>
      </nav>
    `;
  }

  content.innerHTML = `
    <div class="cc-more-phone-view">
      <div class="cc-more-group">
        <div class="cc-more-group-title">Workspace</div>
        <a href="#introductions" class="cc-more-row" id="ccMoreIntros">
          <span>Introductions</span>
          <span style="font-size: 13px; color: #B3261E; font-weight: 700">1 open</span>
        </a>
        <a href="#deals" class="cc-more-row" id="ccMorePipeline">
          <span>Pipeline</span>
          <span style="font-size: 13px; color: #4C5671">$2.29M</span>
        </a>
        <a href="#signals" class="cc-more-row" id="ccMoreSignals">
          <span>Signal feed</span>
          <span style="font-size: 13px; color: #4C5671">5 new</span>
        </a>
        <a href="#coverage" class="cc-more-row" id="ccMoreCoverage">
          <span>Coverage map</span>
          <span style="font-size: 13px; color: #4C5671">6 gaps</span>
        </a>
      </div>

      <div class="cc-more-group">
        <div class="cc-more-group-title">Sales help</div>
        <a href="#playbook" class="cc-more-row" id="ccMorePlaybook">
          <span>How to sell it</span>
          <span style="font-size: 13px; color: #4C5671">5 plays</span>
        </a>
      </div>

      <div class="cc-more-group">
        <div class="cc-more-group-title">Account</div>
        <a href="#settings" class="cc-more-row" id="ccMoreSettings">
          <span>Settings</span>
          <span aria-hidden="true" style="color: #4C5671">›</span>
        </a>
        <a href="#help" class="cc-more-row" id="ccMoreHelp">
          <span>Help</span>
          <span aria-hidden="true" style="color: #4C5671">›</span>
        </a>
        <a href="/logout" class="cc-more-row" style="color: #B3261E; font-weight: 700">
          <span>Sign out</span>
        </a>
      </div>
    </div>
  `;

  document.getElementById('ccMoreIntros')?.addEventListener('click', (e) => {
    e.preventDefault();
    openIntroductionsWorkspace();
  });
  document.getElementById('ccMorePipeline')?.addEventListener('click', (e) => {
    e.preventDefault();
    openDealsList();
  });
  document.getElementById('ccMoreSignals')?.addEventListener('click', (e) => {
    e.preventDefault();
    openSignalFeedWorkspace();
  });
  document.getElementById('ccMoreCoverage')?.addEventListener('click', (e) => {
    e.preventDefault();
    closeEntitySheet();
    import('./main.js').then(m => m.switchDirectionView('accounts', true));
  });
  document.getElementById('ccMorePlaybook')?.addEventListener('click', (e) => {
    e.preventDefault();
    openPlaybookWorkspace();
  });
  document.getElementById('ccMoreSettings')?.addEventListener('click', (e) => {
    e.preventDefault();
    showToast('Settings opened.');
  });
  document.getElementById('ccMoreHelp')?.addEventListener('click', (e) => {
    e.preventDefault();
    showToast('Sales Command Center help guide.');
  });
}

// ── Helpers & Global Initialization ─────────────────────────────────────────
function breadcrumbNavPipeline() {
  const { breadcrumb } = getModalElements();
  breadcrumb?.querySelector('[data-nav="pipeline"]')?.addEventListener('click', (e) => {
    e.preventDefault();
    closeEntitySheet();
    import('./main.js').then(m => m.switchDirectionView('pipeline', true));
  });
}

function breadcrumbNavAccounts() {
  const { breadcrumb } = getModalElements();
  breadcrumb?.querySelector('[data-nav="accounts"]')?.addEventListener('click', (e) => {
    e.preventDefault();
    closeEntitySheet();
    import('./main.js').then(m => m.switchDirectionView('accounts', true));
  });
}

export function initEntitySheets() {
  const { modal, backdrop, closeBtn, expandBtn } = getModalElements();
  if (!modal) return;

  closeBtn?.addEventListener('click', () => closeEntitySheet(true));
  backdrop?.addEventListener('click', () => closeEntitySheet(true));

  expandBtn?.addEventListener('click', () => {
    modal.classList.toggle('is-expanded');
    const isExp = modal.classList.contains('is-expanded');
    expandBtn.title = isExp ? 'Restore size' : 'Expand full width';
    expandBtn.innerHTML = isExp ? '<i class="fa-solid fa-down-left-and-up-right-to-center"></i>' : '<i class="fa-solid fa-up-right-and-down-left-from-center"></i>';
  });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.hidden) {
      closeEntitySheet(true);
    }
  });

  function handleUrlHash() {
    const hash = window.location.hash;
    if (!hash) return;

    if (hash === '#copilot') {
      openCopilotModal();
    } else if (hash === '#deals' || hash === '#deals-list') {
      openDealsList();
    } else if (hash === '#introductions') {
      openIntroductionsWorkspace();
    } else if (hash === '#playbook' || hash === '#how-to-sell') {
      openPlaybookWorkspace();
    } else if (hash === '#signals' || hash === '#signal-feed') {
      openSignalFeedWorkspace();
    } else if (hash === '#more') {
      openMorePhoneDrawer();
    } else if (hash.startsWith('#deal-')) {
      const dealKey = hash.replace('#deal-', '');
      openDealDetail(dealKey);
    } else if (hash.startsWith('#account-')) {
      const acctKey = hash.replace('#account-', '');
      openAccountDetail(acctKey);
    } else if (hash.startsWith('#contact-')) {
      const contactKey = hash.replace('#contact-', '');
      openContactDetail(contactKey);
    }
  }

  window.addEventListener('hashchange', handleUrlHash);
  handleUrlHash();

  document.querySelectorAll('a[href="/copilot"], button[data-action="open-copilot"]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      openCopilotModal();
    });
  });

  // Mobile Bottom Navigation Bar Events
  const mobNav = document.getElementById('ccMobileBottomNav');
  if (mobNav) {
    mobNav.addEventListener('click', (e) => {
      const tab = e.target.closest('[data-mob-view]');
      if (!tab) return;
      const view = tab.dataset.mobView;

      mobNav.querySelectorAll('.cc-mob-tab').forEach(t => t.classList.remove('is-active'));
      tab.classList.add('is-active');

      if (view === 'day' || view === 'accounts' || view === 'pipeline') {
        closeEntitySheet();
        import('./main.js').then(m => m.switchDirectionView(view, true));
      } else if (view === 'copilot') {
        openCopilotModal();
      } else if (view === 'more') {
        openMorePhoneDrawer();
      }
    });
  }

  // Expose global helpers on window for inline links and widgets
  window.openDealDetail = openDealDetail;
  window.openAccountDetail = openAccountDetail;
  window.openContactDetail = openContactDetail;
  window.openCopilotModal = openCopilotModal;
  window.openDealsList = openDealsList;
  window.openIntroductionsWorkspace = openIntroductionsWorkspace;
  window.openPlaybookWorkspace = openPlaybookWorkspace;
  window.openSignalFeedWorkspace = openSignalFeedWorkspace;
  window.openMorePhoneDrawer = openMorePhoneDrawer;
  window.closeEntitySheet = closeEntitySheet;
}
