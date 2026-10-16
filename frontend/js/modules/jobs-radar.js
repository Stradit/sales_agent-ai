// Organisational Hiring & Strategic Trend Radar Engine
// Dynamically extracts strategic domain investment, leadership expansion indices,
// tech stack signals, and actionable sales pitch triggers from live PostgreSQL job records.

import { esc } from './utils.js';
import { STRADIT_OFFERINGS } from './constants.js';

export const HIRING_DOMAINS = [
  {
    id: 'ai_automation',
    name: 'AI & Process Automation',
    icon: 'fa-solid fa-microchip',
    color: 'linear-gradient(135deg, #6366f1, #8b5cf6)',
    offerId: 'ai',
    keywords: [
      /\bai\b/i, /artificial intelligence/i, /machine learning/i, /\bml\b/i, /genai/i, /generative ai/i,
      /llm/i, /copilot/i, /process analyst/i, /deep learning/i, /nlp/i, /automation/i, /cognitive/i,
      /neural/i, /agentic/i, /prompt/i
    ],
    pitchAngle: 'Enterprise AI Governance, Agentic Workflow Orchestration, and LLMOps Advisory',
  },
  {
    id: 'cloud_platform',
    name: 'Cloud & Platform Modernization',
    icon: 'fa-solid fa-cloud',
    color: 'linear-gradient(135deg, #0ea5e9, #0284c7)',
    offerId: 'cloud',
    keywords: [
      /cloud/i, /infrastructure/i, /full-stack/i, /full stack/i, /platform/i, /devops/i, /architect/i,
      /systems lead/i, /site reliability/i, /\bsre\b/i, /\baws\b/i, /\bazure\b/i, /\bgcp\b/i,
      /kubernetes/i, /microservices/i, /distributed systems/i, /software engineer/i
    ],
    pitchAngle: 'Hybrid Cloud Architecture Modernization, Legacy Decoupling, and SRE Scale',
  },
  {
    id: 'data_analytics',
    name: 'Data Engineering & Analytics',
    icon: 'fa-solid fa-chart-column',
    color: 'linear-gradient(135deg, #10b981, #059669)',
    offerId: 'data',
    keywords: [
      /data/i, /quantitative/i, /analytics/i, /\bbi\b/i, /data science/i, /\betl\b/i, /\bsql\b/i,
      /snowflake/i, /databricks/i, /pipeline/i, /warehouse/i, /business intelligence/i, /lakehouse/i
    ],
    pitchAngle: 'Modern Data Stack Integration, Real-Time Analytics Pipelines, and Decision Intelligence',
  },
  {
    id: 'risk_compliance',
    name: 'Risk, Compliance & Control',
    icon: 'fa-solid fa-shield-halved',
    color: 'linear-gradient(135deg, #f59e0b, #d97706)',
    offerId: 'cyber',
    keywords: [
      /risk/i, /compliance/i, /control/i, /cyber/i, /security/i, /audit/i, /governance/i,
      /identity/i, /fraud/i, /surveillance/i, /regulatory/i, /collateral/i
    ],
    pitchAngle: 'Automated Regulatory Compliance Pipelines, Threat Intelligence, and Access Governance',
  },
  {
    id: 'core_operations',
    name: 'Core Business & Operations',
    icon: 'fa-solid fa-sitemap',
    color: 'linear-gradient(135deg, #ec4899, #db2777)',
    offerId: null,
    keywords: [
      /product management/i, /\bpom\b/i, /operations/i, /credit/i, /investor services/i,
      /client processing/i, /custody/i, /asset servicing/i, /settlement/i, /trading/i,
      /wealth/i, /portfolio/i, /specialist/i, /accountant/i, /sales/i, /manager/i
    ],
    pitchAngle: 'Core Operational Streamlining, Asset Servicing Modernization, and Digital Front-Office Enablement',
  },
];

const TECH_CATALOG = [
  { name: 'AWS', rx: /\baws\b/i },
  { name: 'Azure', rx: /\bazure\b/i },
  { name: 'GCP', rx: /\b(gcp|google cloud)\b/i },
  { name: 'Kubernetes', rx: /\b(kubernetes|k8s)\b/i },
  { name: 'Docker', rx: /\bdocker\b/i },
  { name: 'Python', rx: /\bpython\b/i },
  { name: 'Java', rx: /\bjava\b/i },
  { name: 'React', rx: /\breact\b/i },
  { name: 'Angular', rx: /\bangular\b/i },
  { name: 'PyTorch', rx: /\bpytorch\b/i },
  { name: 'TensorFlow', rx: /\btensorflow\b/i },
  { name: 'Snowflake', rx: /\bsnowflake\b/i },
  { name: 'Databricks', rx: /\bdatabricks\b/i },
  { name: 'Kafka', rx: /\bkafka\b/i },
  { name: 'SQL', rx: /\bsql\b/i },
  { name: 'PostgreSQL', rx: /\b(postgres|postgresql)\b/i },
  { name: 'Terraform', rx: /\bterraform\b/i },
  { name: 'Spark', rx: /\bspark\b/i },
  { name: 'Hadoop', rx: /\bhadoop\b/i },
  { name: 'Node.js', rx: /\bnode(\.js)?\b/i },
  { name: 'Go', rx: /\b(golang|go language)\b/i },
  { name: 'C++', rx: /(?:^|\s|\W)c\+\+(?:$|\s|\W)/i },
  { name: 'Salesforce', rx: /\bsalesforce\b/i },
  { name: 'ServiceNow', rx: /\bservicenow\b/i }
];

export function analyzeHiringTrends(jobs, account) {
  const total = jobs.length;
  if (!total) {
    return {
      total: 0,
      domains: [],
      topDomain: null,
      leadershipCount: 0,
      leadershipPct: 0,
      leadershipRoles: [],
      topTech: [],
      topLocations: [],
      triggers: [],
      categorizedJobs: new Map(),
    };
  }

  // 1. Domain Categorization
  const categorizedJobs = new Map();
  HIRING_DOMAINS.forEach(d => categorizedJobs.set(d.id, []));

  jobs.forEach(j => {
    const text = `${j.title || ''} ${j.description || ''} ${j.employment_type || ''}`;
    let matched = false;
    for (const domain of HIRING_DOMAINS) {
      if (domain.keywords.some(regex => regex.test(text))) {
        categorizedJobs.get(domain.id).push(j);
        matched = true;
        break; // assign to primary dominant domain
      }
    }
    if (!matched) {
      categorizedJobs.get('core_operations').push(j);
    }
  });

  const domains = HIRING_DOMAINS.map(d => {
    const list = categorizedJobs.get(d.id) || [];
    const count = list.length;
    const pct = Math.round((count / total) * 100);
    let surgeBadge = { label: 'STEADY', icon: 'fa-solid fa-circle-check', cssClass: 'surge-steady' };
    if (pct >= 30) {
      surgeBadge = { label: 'HIGH SURGE', icon: 'fa-solid fa-fire', cssClass: 'surge-high' };
    } else if (pct >= 15) {
      surgeBadge = { label: 'ACTIVE', icon: 'fa-solid fa-bolt', cssClass: 'surge-active' };
    }
    return {
      ...d,
      count,
      pct,
      surgeBadge,
      jobs: list,
    };
  }).filter(d => d.count > 0).sort((a, b) => b.count - a.count);

  const topDomain = domains.length ? domains[0] : null;

  // 2. Leadership Seniority Index
  const leadershipKeywords = [/director/i, /\bvp\b/i, /vice president/i, /\bsvp\b/i, /senior vice president/i, /head of/i, /chief/i, /lead/i, /principal/i];
  const leadershipRoles = jobs.filter(j => leadershipKeywords.some(rx => rx.test(j.title || '')));
  const leadershipCount = leadershipRoles.length;
  const leadershipPct = Math.round((leadershipCount / total) * 100);

  // 3. Tech Signals Extraction
  const techCounts = new Map();
  TECH_CATALOG.forEach(t => {
    let count = 0;
    jobs.forEach(j => {
      if (t.rx.test(j.title || '') || t.rx.test(j.description || '')) count++;
    });
    if (count > 0) techCounts.set(t.name, count);
  });
  const topTech = [...techCounts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([tech, count]) => ({ tech, count }));

  // 4. Location Hubs
  const locCounts = new Map();
  jobs.forEach(j => {
    let loc = (j.location || '').trim();
    if (loc.includes('/')) loc = loc.split('/')[0].trim();
    if (loc) locCounts.set(loc, (locCounts.get(loc) || 0) + 1);
  });
  const topLocations = [...locCounts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6)
    .map(([location, count]) => ({ location, count }));

  // 5. Dynamic Sales Triggers Formulation
  const triggers = [];
  const acctName = account.name || 'this enterprise';

  if (topDomain) {
    const matchedOffering = STRADIT_OFFERINGS.find(o => o.id === topDomain.offerId) || STRADIT_OFFERINGS[0];
    triggers.push({
      title: `Heavy Investment in ${topDomain.name} (${topDomain.pct}% of open requisitions)`,
      detail: `${acctName} has ${topDomain.count} open roles in ${topDomain.name.toLowerCase()}, indicating an active capital allocation and program buildout.`,
      pitch: `Pitch StradIT ${matchedOffering.label}: ${matchedOffering.pitch}`,
      talkingPoint: `We noticed ${acctName}'s substantial talent expansion in ${topDomain.name.toLowerCase()} (${topDomain.count} active postings). At StradIT, we help financial enterprises accelerate these exact initiatives through ${matchedOffering.pitch.toLowerCase()}`,
    });
  }

  if (leadershipCount > 0) {
    triggers.push({
      title: `Executive Leadership Expansion (${leadershipCount} Leadership & Lead Roles)`,
      detail: `${leadershipPct}% of active requisitions are VP, Director, or Lead positions, indicating new initiative formation and department scaling.`,
      pitch: 'Engage incoming leadership before vendor ecosystems and architectures are locked.',
      talkingPoint: `With ${leadershipCount} new VP and leadership appointments currently open across engineering and operations at ${acctName}, this is an ideal inflection point to establish benchmark consulting and advisory support.`,
    });
  }

  if (domains.length > 1) {
    const secondaryDomain = domains[1];
    const topHubStr = topLocations.slice(0, 2).map(l => `${l.location} (${l.count})`).join(', ');
    triggers.push({
      title: `Secondary Growth in ${secondaryDomain.name} (${secondaryDomain.count} roles)`,
      detail: `Expanding capabilities in ${secondaryDomain.name.toLowerCase()}${topHubStr ? ` across regional hubs (${topHubStr})` : ''}.`,
      pitch: `Align case studies on ${secondaryDomain.pitchAngle}.`,
      talkingPoint: `Regarding ${acctName}'s ongoing momentum in ${secondaryDomain.name.toLowerCase()}, StradIT brings pre-built accelerators for ${secondaryDomain.pitchAngle.toLowerCase()}.`,
    });
  }

  return {
    total,
    domains,
    topDomain,
    leadershipCount,
    leadershipPct,
    leadershipRoles,
    topTech,
    topLocations,
    triggers,
    categorizedJobs,
  };
}

export function renderHiringTrendRadar(account, jobs) {
  if (!jobs || !jobs.length) {
    return `
      <div class="panel">
        <div class="panel-title">
          <span><i class="fa-solid fa-arrow-trend-up"></i> Organisational Hiring &amp; Strategic Trend Radar</span>
          <span class="context-badge">0 Open Roles</span>
        </div>
        <div class="empty-block" style="padding:36px 16px;">
          <div class="empty-block-icon"><i class="fa-solid fa-briefcase"></i></div>
          <div class="empty-block-text">No active LinkedIn job postings recorded in the database for ${esc(account.name || 'this account')}.</div>
        </div>
      </div>
    `;
  }

  const analysis = analyzeHiringTrends(jobs, account);
  const { total, domains, topDomain, leadershipCount, leadershipPct, topTech, topLocations, triggers } = analysis;

  const primaryHub = topLocations.length ? topLocations[0].location : 'Global';

  return `
    <div class="panel radar-container">
      <div class="panel-title">
        <div>
          <span><i class="fa-solid fa-arrow-trend-up" style="color:var(--brand);"></i> Organisational Hiring &amp; Strategic Trend Radar</span>
          <p class="section-desc" style="margin:4px 0 0 0;">Dynamic intelligence derived from ${total} live requisitions in database for <strong>${esc(account.name)}</strong></p>
        </div>
        <span class="context-badge live"><i class="fa-solid fa-layer-group"></i> ${total} Total Roles Monitored</span>
      </div>

      <!-- Top 3 Strategic KPI Metric Cards -->
      <div class="radar-kpi-grid">
        <div class="radar-kpi-card">
          <div class="radar-kpi-header">
            <span class="radar-kpi-label">Top Booming Domain</span>
            <span class="radar-kpi-icon" style="color:var(--brand);"><i class="${topDomain ? topDomain.icon : 'fa-solid fa-star'}"></i></span>
          </div>
          <div class="radar-kpi-value">${topDomain ? esc(topDomain.name) : 'Balanced'}</div>
          <div class="radar-kpi-sub">
            <span class="pill pill-brand">${topDomain ? topDomain.pct : 0}% of active hiring</span>
            <span class="radar-surge-pill ${topDomain ? topDomain.surgeBadge.cssClass : ''}">${topDomain ? topDomain.surgeBadge.label : ''}</span>
          </div>
        </div>

        <div class="radar-kpi-card">
          <div class="radar-kpi-header">
            <span class="radar-kpi-label">Leadership Expansion</span>
            <span class="radar-kpi-icon" style="color:#0ea5e9;"><i class="fa-solid fa-users"></i></span>
          </div>
          <div class="radar-kpi-value">${leadershipCount} VP &amp; Lead Roles</div>
          <div class="radar-kpi-sub">
            <span class="pill pill-success">${leadershipPct}% leadership ratio</span>
            <span style="color:var(--text-muted); font-size:0.75rem;">New initiative formation</span>
          </div>
        </div>

        <div class="radar-kpi-card">
          <div class="radar-kpi-header">
            <span class="radar-kpi-label">Regional Footprint</span>
            <span class="radar-kpi-icon" style="color:#10b981;"><i class="fa-solid fa-location-dot"></i></span>
          </div>
          <div class="radar-kpi-value">${topLocations.length} Active Regional Hubs</div>
          <div class="radar-kpi-sub">
            <span class="pill pill-success">${esc(primaryHub)} (Top Hub)</span>
            <span style="color:var(--text-muted); font-size:0.75rem;">${total} roles mapped across locations</span>
          </div>
        </div>
      </div>

      <!-- Dedicated Horizontal Primary Tech Stack & Regional Hubs Bar -->
      <div class="radar-tech-hubs-bar">
        <div class="radar-tech-col">
          <div class="radar-col-header">
            <span class="radar-col-title"><i class="fa-solid fa-microchip" style="color:var(--brand);"></i> Primary Tech Stack in Motion</span>
            <span class="radar-count-badge">${topTech.length} Detected</span>
          </div>
          <div class="radar-pill-row">
            ${topTech.length ? topTech.map(t => `
              <div class="radar-item-pill tech-pill" title="${t.count} requisitions require ${esc(t.tech)}">
                <i class="fa-solid fa-circle-check"></i>
                <strong>${esc(t.tech)}</strong>
                <span class="pill-badge">${t.count}</span>
              </div>
            `).join('') : '<div class="radar-empty-hint">Multi-stack enterprise technology environment</div>'}
          </div>
        </div>

        <div class="radar-hubs-col">
          <div class="radar-col-header">
            <span class="radar-col-title"><i class="fa-solid fa-location-dot" style="color:#10b981;"></i> Regional Delivery &amp; CoE Hubs</span>
            <span class="radar-count-badge">${topLocations.length} Hubs</span>
          </div>
          <div class="radar-pill-row">
            ${topLocations.length ? topLocations.map(l => `
              <div class="radar-item-pill hub-pill" title="${l.count} requisitions in ${esc(l.location)}">
                <i class="fa-solid fa-city"></i>
                <span>${esc(l.location)}</span>
                <span class="pill-badge">${l.count}</span>
              </div>
            `).join('') : '<div class="radar-empty-hint">Global distribution across enterprise locations</div>'}
          </div>
        </div>
      </div>

      <!-- 1. Domain Investment Distribution -->
      <div class="radar-section">
        <div class="radar-section-title">
          <i class="fa-solid fa-bars-progress"></i> Domain Investment Distribution
        </div>
        <div class="radar-bars-wrap">
          ${domains.map(d => `
            <div class="radar-bar-row">
              <div class="radar-bar-info">
                <span class="radar-bar-name"><i class="${d.icon}"></i> ${esc(d.name)}</span>
                <div class="radar-bar-metrics">
                  <span class="radar-bar-pct"><strong>${d.pct}%</strong> (${d.count} roles)</span>
                  <span class="radar-surge-pill ${d.surgeBadge.cssClass}"><i class="${d.surgeBadge.icon}"></i> ${d.surgeBadge.label}</span>
                </div>
              </div>
              <div class="radar-bar-track">
                <div class="radar-bar-fill" style="width:${d.pct}%; background:${d.color};"></div>
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <!-- 2. Staff Augmentation & Contractor Demand Callout (If Present) -->
      ${(() => {
        const contractRoles = (jobs || []).filter(j => 
          (j.employment_type || '').toLowerCase() === 'contract' || 
          /\(contract\)/i.test(j.title || '')
        );
        if (!contractRoles.length) return '';
        return `
          <div class="radar-section" style="background: linear-gradient(135deg, rgba(16, 185, 129, 0.08), rgba(6, 95, 70, 0.04)); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 0; padding: 16px;">
            <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;">
              <div style="font-size: 0.85rem; font-weight: 700; color: #059669; display: flex; align-items: center; gap: 6px;">
                <i class="fa-solid fa-briefcase"></i> Immediate Staff Augmentation Opportunities (${contractRoles.length} Roles)
              </div>
              <span class="pill pill-success" style="font-size: 0.68rem;">External Sourcing Trigger</span>
            </div>
            <p style="font-size: 0.78rem; color: var(--text-secondary); margin: 0 0 10px 0;">
              <strong>${esc(account.name)}</strong> is actively engaging external contractors for specialized delivery:
            </p>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 8px;">
              ${contractRoles.slice(0, 6).map(j => `
                <div style="background: var(--surface-bg, #ffffff); border: 1px solid rgba(16, 185, 129, 0.2); border-radius: 0; padding: 8px 12px; display: flex; flex-direction: column; gap: 4px;">
                  <a href="${j.job_url ? esc(j.job_url) : '#'}" target="_blank" rel="noopener" style="font-size: 0.78rem; font-weight: 600; color: var(--brand); text-decoration: none; display: inline-flex; align-items: center; gap: 4px;">
                    ${esc(j.title)} <i class="fa-solid fa-arrow-up-right-from-square" style="font-size: 0.65rem;"></i>
                  </a>
                  <div style="font-size: 0.7rem; color: var(--text-muted); display: flex; justify-content: space-between;">
                    <span><i class="fa-solid fa-location-dot"></i> ${esc(j.location || 'US')}</span>
                    <span>${j.applicants ? `${j.applicants} applicants` : 'Active'}</span>
                  </div>
                </div>
              `).join('')}
            </div>
          </div>
        `;
      })()}

      <!-- 3. Strategic Sales Takeaways & Pitch Angles -->
      <div class="radar-section">
        <div class="radar-section-title">
          <i class="fa-solid fa-lightbulb" style="color:#eab308;"></i> Strategic Sales Takeaways &amp; Pitch Angles
        </div>
        <div class="radar-triggers-list">
          ${triggers.map((t, idx) => `
            <div class="radar-trigger-card">
              <div class="radar-trigger-head">
                <div class="radar-trigger-badge">Trigger ${idx + 1}</div>
                <div class="radar-trigger-title">${esc(t.title)}</div>
              </div>
              <div class="radar-trigger-detail">${esc(t.detail)}</div>
              <div class="radar-trigger-pitch">
                <span class="radar-pitch-tag"><i class="fa-solid fa-bullseye"></i> Recommended Play</span>
                <span>${esc(t.pitch)}</span>
              </div>
              <div class="radar-trigger-actions">
                <button type="button" class="radar-copy-btn" data-talking-point="${esc(t.talkingPoint)}">
                  <i class="fa-solid fa-clipboard-check"></i> Copy Executive Talking Point
                </button>
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <!-- 4. Live Paginated Requisitions Browser -->
      <div class="radar-section">
        <div class="radar-section-title" style="display: flex; align-items: center; justify-content: space-between;">
          <span><i class="fa-solid fa-folder-open" style="color:var(--brand);"></i> Live LinkedIn Requisitions Browser (${total} Monitored Roles)</span>
          <span style="font-size: 0.7rem; font-weight: 500; color: var(--text-muted);">Verified PostgreSQL Database Records</span>
        </div>
        <div style="max-height: 480px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 0; background: var(--card-bg, #ffffff);">
          ${jobs.slice(0, 50).map(j => {
            const isContract = (j.employment_type || '').toLowerCase() === 'contract' || /\(contract\)/i.test(j.title || '');
            const isLead = /director|\bvp\b|vice president|\bsvp\b|head of|chief|lead/i.test(j.title || '');
            return `
              <div style="padding: 10px 14px; border-bottom: 1px solid var(--border-color); display: flex; flex-direction: column; gap: 4px;">
                <div style="display: flex; align-items: center; justify-content: space-between; gap: 10px;">
                  <a href="${j.job_url ? esc(j.job_url) : '#'}" target="_blank" rel="noopener" style="font-size: 0.8rem; font-weight: 600; color: var(--brand); text-decoration: none; display: inline-flex; align-items: center; gap: 5px;">
                    ${esc(j.title)} <i class="fa-solid fa-arrow-up-right-from-square" style="font-size: 0.65rem;"></i>
                  </a>
                  <div style="display: flex; gap: 4px; flex-shrink: 0;">
                    ${isContract ? '<span class="pill pill-success" style="font-size: 0.6rem; padding: 1px 5px;">CONTRACT</span>' : ''}
                    ${isLead ? '<span class="pill pill-brand" style="font-size: 0.6rem; padding: 1px 5px;">LEADERSHIP</span>' : ''}
                    ${j.workplace_type ? `<span class="pill" style="font-size: 0.6rem; padding: 1px 5px; background: var(--input-bg); color: var(--text-secondary); text-transform: capitalize;">${esc(j.workplace_type).replace('_', ' ')}</span>` : ''}
                  </div>
                </div>
                <div style="display: flex; align-items: center; gap: 14px; font-size: 0.72rem; color: var(--text-muted);">
                  <span><i class="fa-solid fa-location-dot"></i> ${esc(j.location || 'US')}</span>
                  ${j.applicants ? `<span><i class="fa-solid fa-users"></i> ${j.applicants} applicants</span>` : ''}
                  ${j.posted_date ? `<span><i class="fa-solid fa-clock"></i> ${esc(j.posted_date)}</span>` : ''}
                </div>
              </div>
            `;
          }).join('')}
          ${jobs.length > 50 ? `
            <div style="padding: 12px; text-align: center; font-size: 0.75rem; color: var(--text-muted); background: var(--input-bg);">
              Showing first 50 of ${total} requisitions
            </div>
          ` : ''}
        </div>
      </div>

    </div>
  `;
}
