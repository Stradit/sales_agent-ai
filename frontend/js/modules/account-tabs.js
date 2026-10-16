// Per-account render module ΓÇö the tab-based "on-demand" detail view (only the active
// tab's content is computed; this already satisfies on-demand loading for this view,
// distinct from the digest's new IntersectionObserver-based lazy render).
import { state } from './state.js';
import { el, dashContent, signalModalBody } from './dom.js';
import { esc, initials, dedupePersonas, getPersonasFor, getTechFor, resolveAccountTargetKey } from './utils.js';
import { showToast } from './toast.js';
import { getAccountContentEntries, matchOfferings, renderAlertCards } from './alerts.js';
import { renderGrowthOpportunities, renderDomainExpansionOpportunities } from './opportunities.js';
import { getAccountJobs, renderJobCard } from './jobs.js';
import { renderHiringTrendRadar } from './jobs-radar.js';
import { computeSignals, renderEngagementPanel } from './signals.js';
import { renderOrgChart } from './org-chart.js';
import { renderContentPanel } from './content-panel.js';
import { renderWeeklyUpdateTab } from './weekly-update.js';
import { renderFinancialSnippets } from './financial-snippets.js';
import { openContactDrawer } from './contact-drawer.js';
import { openSignalModal, closeSignalModal } from './signal-modal.js';
import { renderSelection } from './selection.js';
import { renderNavTree } from './nav-tree.js';
import { mountTimeline } from './activity-timeline.js';
import { mountAccountCrm } from './crm-extras.js';

export function renderScoreRing(account) {
  const score = account.heat_score;
  if (score == null) {
    return `<div class="score-ring" title="No opportunity score yet ΓÇö heat_score wasn't populated for this account during ingestion">
      <div class="score-ring-value">ΓÇö<br><span style="font-size:.55rem;font-weight:600;">SCORE</span></div>
    </div>`;
  }
  const pct = Math.max(0, Math.min(100, score));
  const deg = pct * 3.6;
  const color = pct >= 70 ? 'var(--success)' : (pct >= 40 ? 'var(--warning)' : 'var(--danger)');
  return `<div class="score-ring" style="background:conic-gradient(${color} ${deg}deg, var(--border-color) ${deg}deg 360deg);" title="Opportunity score from heat_score (0-100, captured during ingestion)">
    <div class="score-ring-value">${Math.round(pct)}<br><span style="font-size:.55rem;font-weight:600;">SCORE</span></div>
  </div>`;
}

export function renderTrendPill(account) {
  if (account.trend_score_90d == null) return '';
  const t = account.trend_score_90d;
  const up = t >= 0;
  return `<div style="margin-top:10px;"><span class="pill ${up ? 'pill-success' : 'pill-danger'}"><i class="bi ${up ? 'bi-graph-up-arrow' : 'bi-graph-down-arrow'}"></i> 90-Day Trend: ${up ? '+' : ''}${t}</span></div>`;
}

// Account-only Sales Alerts wrapper ΓÇö shares its engine with the digest's
// renderGlobalSalesAlerts (alerts.js).
export function renderSalesAlerts(account) {
  const entries = getAccountContentEntries(account);
  if (!entries.length) {
    return `<div class="empty-block">
      <div class="empty-block-icon"><i class="bi bi-bell-slash"></i></div>
      <div class="empty-block-text">No captured content yet to match against StradIT's service lines.</div>
    </div>`;
  }
  return renderAlertCards(matchOfferings(entries), { getAccount: () => account });
}

export function renderAccountJobsTab(account) {
  const jobs = getAccountJobs(account);
  return renderHiringTrendRadar(account, jobs);
}

// ΓöÇΓöÇ Quick Outreach Arsenal Copy Helpers ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
function getTopIcebreakerText(account) {
  const personas = dedupePersonas(account.personas || []);
  const p = personas.find(p => p.personalized_icebreaker);
  if (p && p.personalized_icebreaker) return p.personalized_icebreaker;
  return `Congratulations on ${account.name}'s ongoing market initiatives and technology investments.`;
}

function getColdEmailPitchText(account, matches) {
  const topMatch = matches && matches.length ? matches[0] : null;
  const offeringName = topMatch ? topMatch.label : 'Enterprise Digital Transformation';
  const pitch = topMatch ? topMatch.pitch : 'scale high-impact engineering workflows and decision intelligence.';
  return `Hi [First Name],\n\nI've been following ${account.name}'s strategic initiatives and noticed your focus in ${offeringName.toLowerCase()}.\n\nAt StradIT, we specialize in ${pitch}\n\nWould you be open to a brief 10-minute introductory conversation next Tuesday to share benchmarks from similar enterprise leaders?\n\nBest regards,\n[Your Name]`;
}

function getAccountCheatSheetText(account, lob) {
  const personas = dedupePersonas(account.personas || []);
  const tech = getTechFor(account, lob);
  return `=== SALES BATTLECARD: ${account.name} ===\n` +
    `Industry: ${(account.industries || []).join(', ') || 'Enterprise'}\n` +
    `Revenue: ${account.revenue || 'N/A'} | Location: ${account.location || 'N/A'}\n` +
    `Headcount: ${account.employee_count_range || 'N/A'} | Ticker: ${account.ticker || 'Private'}\n\n` +
    `Top Tech: ${tech.slice(0, 8).join(', ') || 'N/A'}\n\n` +
    `Key Buying Committee:\n` +
    personas.slice(0, 6).map(p => `ΓÇó ${p.name} ΓÇö ${p.title || 'Executive'} [${p.tier || 'Leader'}]${p.email ? ` (${p.email})` : ''}`).join('\n');
}

// ΓöÇΓöÇ Render Tab 1: Executive Briefing ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
function renderExecutiveBriefingTab(account, lob, signals, matches) {
  const industries = (account.industries || []).slice(0, 6);
  const tech = getTechFor(account, lob);

  return `
    <!-- Top Strategic Signals Callout -->
    <div class="panel" style="border-left: 3px solid var(--brand);">
      <div class="panel-title">
        <span><i class="bi bi-stars"></i> Top Strategic Triggers &amp; Catalysts</span>
        <span class="context-badge live">${signals.length + matches.length} active triggers</span>
      </div>
      <p class="section-desc">Key high-priority catalysts detected from filings, social discourse, and technology footprints.</p>

      <div style="display:flex; flex-direction:column; gap:8px; margin-bottom:4px;">
        ${matches.length ? `
          <div class="signal-chip" style="background:var(--brand-soft); border-color:rgba(228, 101, 33,.2); color:var(--brand);">
            <i class="bi bi-lightning-charge-fill"></i>
            <span><strong>${matches[0].label} Opportunity:</strong> ${esc(matches[0].pitch)}</span>
          </div>` : ''}
        ${signals.slice(0, 2).map((s, i) => `
          <button type="button" class="signal-chip ${s.detail ? 'clickable' : ''}" ${s.detail ? `data-signal-idx="${i}" title="Click to view detailed breakdown"` : ''}>
            <span class="signal-icon"><i class="bi ${s.icon}"></i></span>
            <span class="signal-text">${esc(s.text)}</span>
            ${s.detail ? '<span class="signal-badge-btn"><span>View Details</span> <i class="bi bi-chevron-right"></i></span>' : ''}
          </button>`).join('')}
      </div>
    </div>

    <!-- Firmographics & Scale -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-buildings"></i> Firmographics &amp; Scale</span>
        <span class="context-badge live"><i class="bi bi-check-circle-fill"></i> Verified Data</span>
      </div>
      <p class="section-desc">Core corporate attributes including founding year, workforce size, organizational structure, and operational industry sectors.</p>
      <div class="chip-row">
        ${account.founded_year ? `<span class="chip" title="Year company was founded"><i class="bi bi-calendar3"></i> Founded ${esc(account.founded_year)}</span>` : ''}
        ${account.employee_count_range ? `<span class="chip" title="Estimated global employee headcount"><i class="bi bi-people"></i> ${esc(account.employee_count_range)}</span>` : ''}
        ${account.company_type ? `<span class="chip" title="Corporate ownership structure"><i class="bi bi-building"></i> ${esc(account.company_type)}</span>` : ''}
        ${(account.lobs || []).length ? `<span class="chip" title="Distinct operational business units and divisions discovered"><i class="bi bi-folder2"></i> ${account.lobs.length} line${account.lobs.length !== 1 ? 's' : ''} of business</span>` : ''}
        ${industries.map(i => `<span class="chip" title="Primary industry taxonomy sector"><i class="bi bi-tag"></i> ${esc(i)}</span>`).join('')}
        ${(!account.founded_year && !account.employee_count_range && !industries.length) ? '<span class="chip">No firmographic data captured yet</span>' : ''}
      </div>
    </div>

    <!-- Detected Tech Stack -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-cpu-fill"></i> Detected Technology Stack</span>
        <span class="context-badge ai">${tech.length} identified</span>
      </div>
      <p class="section-desc">Cloud infrastructure, data tooling, enterprise frameworks, and developer platforms active in this account.</p>
      <div class="chip-row">
        ${tech.length ? tech.map(t => `<span class="chip" title="Active technology in stack"><i class="bi bi-cpu"></i> ${esc(t)}</span>`).join('') : '<span class="chip">No tech stack detected yet</span>'}
      </div>
    </div>

    <!-- Digital Footprint & Traffic Analytics -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-activity"></i> Digital Footprint &amp; Web Analytics</span>
        <span class="context-badge live"><i class="bi bi-broadcast"></i> Web Telemetry</span>
      </div>
      <p class="section-desc">Audience retention, web ranking momentum, and visitor volume.</p>
      ${renderEngagementPanel(account)}
    </div>

    <!-- Multi-Source Signals Summary -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-radar"></i> Multi-Source Account Signals</span>
        <span class="context-badge live">${signals.length} signals</span>
      </div>
      <p class="section-desc">Aggregated from LinkedIn headcount, SEC 10-K filings, GLEIF legal entities, and funding records. Click to explore details.</p>
      ${signals.length ? signals.map((s, i) => `
        <button type="button" class="signal-chip ${s.detail ? 'clickable' : ''}" ${s.detail ? `data-signal-idx="${i}" title="Click to view detailed breakdown"` : ''}>
          <span class="signal-icon"><i class="bi ${s.icon}"></i></span>
          <span class="signal-text">${esc(s.text)}</span>
          ${s.detail ? '<span class="signal-badge-btn"><span>Explore Details</span> <i class="bi bi-chevron-right"></i></span>' : ''}
        </button>`).join('')
        : `<div class="empty-block"><div class="empty-block-icon"><i class="bi bi-search"></i></div><div class="empty-block-text">No signals detected yet for this ${lob ? 'line of business' : 'account'}.</div></div>`}
    </div>
  `;
}

// ΓöÇΓöÇ Render Tab 2: Buying Committee & Org Chart ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
function renderBuyingCommitteeTab(account, lob) {
  return `
    <div class="info-banner">
      <i class="bi bi-diagram-3-fill"></i>
      <div>
        <strong>Verified Reporting Hierarchy:</strong> Direct reporting relationships and decision-making tiers for ${lob ? `<strong>${esc(lob.name)}</strong>` : 'the enterprise group'}. Click any executive node to open their complete AI Call-Prep Dossier.
      </div>
    </div>

    <!-- Full-Width Verified Hierarchy Panel -->
    <div class="panel" style="margin-bottom:0; min-height: 480px;">
      <div class="panel-title">
        <span><i class="bi bi-diagram-3"></i> Verified Hierarchy</span>
        <span class="context-badge live">${lob ? esc(lob.name) : 'Corporate Level'}</span>
      </div>
      <p class="section-desc">Interactive organizational reporting tree showing executive hierarchy, leadership tiers, and decision-making authority.</p>
      ${renderOrgChart(account, lob)}
    </div>
  `;
}

// ΓöÇΓöÇ Render Tab 3: Sales Alerts & Battlecards ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
function renderSalesAlertsTab(account, lob, matches) {
  return `
    <!-- Panel 1: Direct Service Line Matches -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-lightning-charge-fill"></i> StradIT Service Line Fit &amp; Opportunities</span>
        <span class="context-badge ai"><i class="bi bi-stars"></i> ${matches.length} matches</span>
      </div>
      <p class="section-desc">Immediate opportunities discovered by cross-referencing StradIT core offerings with real citations in public filings and executive statements.</p>
      ${renderSalesAlerts(account)}
    </div>

    <!-- Panel 2: Growth Whitespace Panel -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-compass"></i> Unserved Content Themes &amp; Growth Whitespace</span>
        <span class="context-badge pending"><i class="bi bi-signpost-split"></i> Whitespace</span>
      </div>
      <p class="section-desc">Recurring themes in this account's discourse that don't match existing service lines, with a suggested build for each ΓÇö potential areas for custom solution engineering. Themes seen before persist here as history even if they stop recurring.</p>
      <div id="growthOpportunitiesBody">${renderGrowthOpportunities(account, (state.opportunityHistory[account.id] || {}).growth_theme)}</div>
    </div>

    <!-- Panel 3: Emerging Expansion Domains & Custom Engineering (New) -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-rocket-takeoff-fill"></i> Emerging Domain Expansion &amp; Custom Integration</span>
        <span class="context-badge live"><i class="bi bi-layers-fill"></i> High-Demand Scope</span>
      </div>
      <p class="section-desc">Trending enterprise initiatives and adjacent technology domains heavily demanded by <strong>${esc(account.name)}</strong> where StradIT can engineer custom integrated solutions. Previously suggested domains stay listed as history.</p>
      <div id="domainExpansionBody">${renderDomainExpansionOpportunities(account, (state.opportunityHistory[account.id] || {}).domain_expansion)}</div>
    </div>
  `;
}

// ΓöÇΓöÇ Render Tab 4: Financials & SEC Intelligence ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
function renderFinancialsTab(account, lob) {
  return `
    ${lob ? `
      <!-- Financial Intelligence (LOB-specific) -->
      <div class="panel">
        <div class="panel-title">
          <span><i class="bi bi-cash-stack"></i> Line of Business Financials</span>
          <span class="context-badge ai">${esc(lob.name)}</span>
        </div>
        <p class="section-desc">Extracted segment revenues, operating performance, and financial disclosures for <strong>${esc(lob.name)}</strong>.</p>
        ${renderFinancialSnippets(lob)}
      </div>` : ''}

    <!-- SEC 10-K & Public Market Intelligence -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-file-earmark-text"></i> SEC 10-K Filings &amp; Regulatory Disclosures</span>
        <span class="context-badge live"><i class="bi bi-bank"></i> Edgar Registry</span>
      </div>
      <p class="section-desc">Annual report disclosures, risk factors, MD&amp;A commentary, and organizational structures extracted from SEC Edgar filings.</p>

      <div style="margin-bottom:14px;">
        ${account.sec_cik ? `
          <div class="stat-row"><span class="stat-label">SEC CIK Identifier</span><span class="stat-value">${esc(account.sec_cik)}</span></div>
          <button type="button" class="action-btn" id="fetchSecBtn" data-cik="${esc(account.sec_cik)}" style="margin-top:12px;width:auto;padding:8px 16px;">
            <i class="bi bi-cloud-arrow-down"></i> Fetch &amp; Index Full 10-K Filing
          </button>
          <div id="secFetchResult"></div>
        ` : `
          <div class="empty-block">
            <div class="empty-block-icon"><i class="bi bi-file-earmark-x"></i></div>
            <div class="empty-block-text">No SEC CIK code recorded for this entity. Typically available for publicly listed US enterprises.</div>
          </div>
        `}
      </div>
    </div>

    <!-- Funding & Capital Structure -->
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-cash-coin"></i> Capitalization &amp; Funding History</span>
        <span class="context-badge live">Crunchbase / Pitchbook</span>
      </div>
      <p class="section-desc">Historical funding rounds, total capital raised, lead investors, and IPO listing milestone records.</p>
      <div class="metrics-grid">
        <div class="metric-tile">
          <div class="metric-value font-semibold">${account.total_funding_amount_usd ? `$${Number(account.total_funding_amount_usd).toLocaleString()}` : 'ΓÇö'}</div>
          <div class="metric-label">Total Capital Raised</div>
        </div>
        <div class="metric-tile">
          <div class="metric-value">${esc(account.last_funding_type || 'ΓÇö')}</div>
          <div class="metric-label">Last Round Type</div>
        </div>
        <div class="metric-tile">
          <div class="metric-value">${esc(account.num_funding_rounds ?? 'ΓÇö')}</div>
          <div class="metric-label">Total Rounds</div>
        </div>
        <div class="metric-tile">
          <div class="metric-value">${esc(account.ipo_status || (account.ticker ? 'Public' : 'Private'))}</div>
          <div class="metric-label">Listing Status</div>
        </div>
      </div>
    </div>
  `;
}

// ΓöÇΓöÇ Render Tab 5: Live Social & Content Listening ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
function renderSocialTab(account, lob) {
  return `
    <div class="panel">
      <div class="panel-title">
        <span><i class="bi bi-chat-square-text"></i> Social &amp; Content Intelligence</span>
        <span class="context-badge ai"><i class="bi bi-robot"></i> LLM Synthesized</span>
      </div>
      <p class="section-desc">Synthesized summaries, executive discussion themes, and storylines captured across Twitter/X, LinkedIn, Reddit, News, and Press releases.</p>
      ${renderContentPanel(account)}
    </div>
  `;
}

export function renderCenter(account, lob) {
  const signals = computeSignals(account, lob);
  state.currentSignals = signals;

  const entries = getAccountContentEntries(account);
  const matches = matchOfferings(entries);

  const personas = dedupePersonas(getPersonasFor(account, lob));
  const targetKey = resolveAccountTargetKey(account);
  const postCount = targetKey ? (state.contentStore.posts[targetKey] || []).length : 0;

  queueMicrotask(() => mountAccountCrm(document.getElementById('acctCrmRow'), account.id));
  let tabContent = '';
  if (state.activeSalesTab === 'committee') {
    tabContent = renderBuyingCommitteeTab(account, lob);
  } else if (state.activeSalesTab === 'alerts') {
    tabContent = renderSalesAlertsTab(account, lob, matches);
  } else if (state.activeSalesTab === 'financials') {
    tabContent = renderFinancialsTab(account, lob);
  } else if (state.activeSalesTab === 'social') {
    tabContent = renderSocialTab(account, lob);
  } else if (state.activeSalesTab === 'weekly') {
    tabContent = renderWeeklyUpdateTab(account);
  } else if (state.activeSalesTab === 'jobs') {
    tabContent = renderAccountJobsTab(account);
  } else if (state.activeSalesTab === 'activity') {
    tabContent = '<div class="panel"><div id="acctActivityTimeline"></div></div>';
    // Mounted right after the caller writes this HTML into the page.
    queueMicrotask(() => {
      const tl = document.getElementById('acctActivityTimeline');
      if (tl) mountTimeline(tl, { objectType: 'account', objectId: account.id, accountId: account.id });
    });
  } else {
    tabContent = renderExecutiveBriefingTab(account, lob, signals, matches);
  }

  return `
    <!-- Account Overview Header Panel -->
    <div class="panel" style="margin-bottom:12px;">
      <div class="acct-header">
        <div class="acct-avatar-lg">${esc(initials(account.name))}</div>
        <div class="acct-header-body">
          <h2 class="acct-name">${esc(account.name)}${lob ? ' ┬╖ ' + esc(lob.name) : ''}</h2>
          <div class="acct-pills">
            <span class="pill pill-brand" title="Public / Private stock ticker classification"><i class="bi bi-tag"></i> ${esc(account.ticker || 'Private')}</span>
            <span class="pill" title="Annual reported revenue"><i class="bi bi-currency-dollar"></i> ${esc((account.revenue || 'Revenue N/A').replace(/^\$/, ''))}</span>
            <span class="pill pill-success" title="Corporate headquarters location"><i class="bi bi-geo-alt"></i> ${esc(account.location || 'Location N/A')}</span>
            ${account.operating_status ? `<span class="pill" title="Current operational status"><i class="bi bi-activity"></i> ${esc(account.operating_status)}</span>` : ''}
          </div>
          <p class="acct-desc">${esc(lob ? (lob.desc || 'No description available.') : (account.desc || 'No description available.'))}</p>
          <div class="acct-crm-row" id="acctCrmRow"></div>
        </div>
        ${renderScoreRing(account)}
      </div>
      ${renderTrendPill(account)}
    </div>

    <!-- Workflow Navigation Tabs -->
    <div class="sales-tabs" id="salesTabsNav">
      <button type="button" class="tab-btn ${state.activeSalesTab === 'briefing' ? 'active' : ''}" data-tab="briefing" title="30-Second account snapshot & core triggers">
        <i class="bi bi-speedometer2"></i> Executive Briefing
      </button>
      <button type="button" class="tab-btn ${state.activeSalesTab === 'committee' ? 'active' : ''}" data-tab="committee" title="Visual hierarchy & stakeholder committee mapping">
        <i class="bi bi-people"></i> Org Structure <span class="tab-badge">${personas.length}</span>
      </button>
      <button type="button" class="tab-btn ${state.activeSalesTab === 'alerts' ? 'active' : ''}" data-tab="alerts" title="StradIT service offering matches & sales battlecards">
        <i class="bi bi-lightning-charge"></i> Sales Alerts &amp; Angles <span class="tab-badge">${matches.length}</span>
      </button>
      <button type="button" class="tab-btn ${state.activeSalesTab === 'social' ? 'active' : ''}" data-tab="social" title="Live discourse, executive tweets, and social sentiment">
        <i class="bi bi-chat-square-text"></i> Social Listening <span class="tab-badge">${postCount}</span>
      </button>
      <button type="button" class="tab-btn ${state.activeSalesTab === 'weekly' ? 'active' : ''}" data-tab="weekly" title="Weekly sales update email, current and archived past weeks">
        <i class="bi bi-envelope-paper"></i> Weekly Update Mail <span class="tab-badge">${(state.weeklyUpdateHistory[account.id] || []).length}</span>
      </button>
      <button type="button" class="tab-btn ${state.activeSalesTab === 'jobs' ? 'active' : ''}" data-tab="jobs" title="Organisational hiring trends, domain distribution & strategic talent radar">
        <i class="bi bi-graph-up-arrow"></i> Hiring Trend Radar <span class="tab-badge">${getAccountJobs(account).length}</span>
      </button>
      <button type="button" class="tab-btn ${state.activeSalesTab === 'activity' ? 'active' : ''}" data-tab="activity" title="Meetings, calls, emails and transcripts logged by the team">
        <i class="fa-solid fa-clock-rotate-left"></i> Activity
      </button>
    </div>

    <!-- Tab Content Area -->
    <div id="salesTabContentArea" class="fade-in">
      ${tabContent}
    </div>
  `;
}

dashContent.addEventListener('click', async function (e) {
  // Radar Talking Point Copy
  const copyTalkingPointBtn = e.target.closest('[data-talking-point]');
  if (copyTalkingPointBtn) {
    const text = copyTalkingPointBtn.dataset.talkingPoint;
    try {
      await navigator.clipboard.writeText(text);
      showToast('Copied executive talking point to clipboard!');
    } catch (err) {
      showToast('Failed to copy. Clipboard permission required.');
    }
    return;
  }

  // Tab switcher
  const tabBtn = e.target.closest('.tab-btn');
  if (tabBtn && tabBtn.dataset.tab) {
    state.activeSalesTab = tabBtn.dataset.tab;
    renderSelection();
    return;
  }

  // Quick Arsenal Actions
  const icebreakerBtn = e.target.closest('#copyIcebreakerBtn');
  if (icebreakerBtn) {
    const account = state.accounts.find(a => a.id === state.activeAccountId);
    if (account) {
      const text = getTopIcebreakerText(account);
      try {
        await navigator.clipboard.writeText(text);
        showToast('Copied personalized icebreaker to clipboard!');
      } catch (err) {
        showToast('Failed to copy. Clipboard permission required.');
      }
    }
    return;
  }

  const emailPitchBtn = e.target.closest('#copyEmailPitchBtn');
  if (emailPitchBtn) {
    const account = state.accounts.find(a => a.id === state.activeAccountId);
    if (account) {
      const entries = getAccountContentEntries(account);
      const matches = matchOfferings(entries);
      const text = getColdEmailPitchText(account, matches);
      try {
        await navigator.clipboard.writeText(text);
        showToast('Copied customized cold pitch email template!');
      } catch (err) {
        showToast('Failed to copy. Clipboard permission required.');
      }
    }
    return;
  }

  const cheatSheetBtn = e.target.closest('#copyCheatSheetBtn');
  if (cheatSheetBtn) {
    const account = state.accounts.find(a => a.id === state.activeAccountId);
    if (account) {
      const lob = state.activeLobId ? (account.lobs || []).find(l => l.id === state.activeLobId) : null;
      const text = getAccountCheatSheetText(account, lob);
      try {
        await navigator.clipboard.writeText(text);
        showToast('Copied account battlecard & committee summary!');
      } catch (err) {
        showToast('Failed to copy. Clipboard permission required.');
      }
    }
    return;
  }

  // Persona trigger in committee tab
  const personaCard = e.target.closest('[data-open-persona]');
  if (personaCard) {
    const pName = personaCard.dataset.openPersona;
    const account = state.accounts.find(a => a.id === state.activeAccountId);
    if (account) {
      const p = dedupePersonas(account.personas || []).find(x => x.name === pName);
      if (p) openContactDrawer(p);
    }
    return;
  }

  // Signal trigger
  const chip = e.target.closest('[data-signal-idx]');
  if (chip) { const s = state.currentSignals[Number(chip.dataset.signalIdx)]; if (s) openSignalModal(s); return; }

  // Jump LOB trigger
  const lobBtn = e.target.closest('[data-jump-lob]');
  if (lobBtn) {
    state.activeAccountId = Number(lobBtn.dataset.jumpAccount);
    state.activeLobId = Number(lobBtn.dataset.jumpLob);
    state.expandedAccountIds.add(state.activeAccountId);
    closeSignalModal();
    renderNavTree();
    renderSelection();
  }
});

signalModalBody.addEventListener('click', async function (e) {
  const btn = e.target.closest('#fetchSecBtn');
  if (!btn) return;
  const cik = btn.dataset.cik;
  const resultEl = el('secFetchResult');
  if (!cik) { resultEl.innerHTML = `<div class="content-provenance"><i class="bi bi-exclamation-triangle"></i> No SEC CIK on file for this account.</div>`; return; }
  btn.disabled = true;
  btn.innerHTML = '<i class="bi bi-hourglass-split"></i> FetchingΓÇª';
  try {
    const res = await fetch('/api/account/sec-10k-chunks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sec_cik: cik, chunk_size: 1500, overlap: 200 })
    });
    const data = await res.json();
    if (data.status !== 'success') {
      resultEl.innerHTML = `<div class="content-provenance"><i class="bi bi-exclamation-triangle"></i> ${esc(data.message || 'No filing text available.')}</div>`;
      return;
    }
    const bySection = {};
    (data.chunks || []).forEach(c => { (bySection[c.section] = bySection[c.section] || []).push(c); });
    resultEl.innerHTML = `
      <div class="content-provenance" style="margin:10px 0;"><i class="bi bi-check-circle"></i> ${data.filing_type} filed ${esc(data.filing_date || '')} ΓÇö <a href="${esc(data.primary_document_url)}" target="_blank">open original filing <i class="bi bi-box-arrow-up-right"></i></a></div>
      ${Object.entries(bySection).map(([section, chunks]) => `
        <details class="content-observed">
          <summary>${esc(section)} (${chunks.length} chunk${chunks.length !== 1 ? 's' : ''})</summary>
          ${chunks.slice(0, 3).map(c => `<p class="content-summary" style="margin-top:8px;">${esc(c.text.slice(0, 500))}${c.text.length > 500 ? 'ΓÇª' : ''}</p>`).join('')}
        </details>
      `).join('')}
    `;
  } catch (err) {
    resultEl.innerHTML = `<div class="content-provenance"><i class="bi bi-exclamation-triangle"></i> Fetch failed: ${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<i class="bi bi-cloud-arrow-down"></i> Fetch full filing text';
  }
});
