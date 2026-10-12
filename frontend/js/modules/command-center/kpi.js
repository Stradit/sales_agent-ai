import { loadCommandCenter, getCommandCenter, isGenerated } from './generator.js';
import { esc } from './utils.js';
import { loadMatrixAccounts } from './real-accounts.js';
import { loadRecentMovements } from './exec-movements.js';
import { openDossier } from './drawer.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { renderTimeline } from './timeline.js';

function sparklinePath(values, w, h) {
  const max = Math.max(...values), min = Math.min(...values);
  const range = max - min || 1;
  const step = w / (values.length - 1);
  const pts = values.map((v, i) => [i * step, h - ((v - min) / range) * h]);
  return pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
}

function sparklineSvg(values) {
  const w = 100, h = 32;
  const d = sparklinePath(values, w, h);
  return `<svg class="cc-spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <path d="${d}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`;
}

export function highlightAndScrollTo(selector) {
  const el = document.querySelector(selector);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.remove('cc-panel-highlight');
  void el.offsetWidth;
  el.classList.add('cc-panel-highlight');
  setTimeout(() => el.classList.remove('cc-panel-highlight'), 2200);
}

async function computeKpi() {
  const [accounts, execChangesAll, _, dueSoonItems] = await Promise.all([
    loadMatrixAccounts(),
    loadRecentMovements().catch(() => []),
    loadCommandCenter(),
    fetch('/api/me/action-items').then(r => r.ok ? r.json() : { action_items: [] }).then(d => d.action_items || []).catch(() => []),
  ]);
  const cc = getCommandCenter();
  const velocity = cc.velocity || { this_week: 0, delta_pct: 0, trend: [] };

  const topAccount = accounts.length
    ? [...accounts].sort((a, b) => b.compositeScore - a.compositeScore)[0]
    : null;

  const aging = execChangesAll.filter(e => !e.actioned && (Date.now() - e.date.getTime()) / 86400000 >= 5);
  const joined = execChangesAll.filter(e => e.type === 'joined').length;
  const promoted = execChangesAll.filter(e => e.type === 'promoted').length;
  const other = execChangesAll.length - joined - promoted;

  const playbookList = cc.playbook || [];
  const hasFilter = (ccState.activeAccountIds && ccState.activeAccountIds.size > 0) || ccState.activeAccountId;
  const matchingPlays = hasFilter
    ? playbookList.filter(p => matchesCurrentAccount(p.account_id, p.account_name))
    : playbookList;

  const inMotionCount = matchingPlays.filter(p => p.tasked).length || (cc.plays && cc.plays.in_motion) || 0;
  const stalledCount = (cc.plays && cc.plays.stalled) || 0;

  // Filter tasks for active account(s) if selected
  let relevantTasks = dueSoonItems.filter(i => i.status === 'open' || i.status === 'in_progress');
  if (hasFilter) {
    relevantTasks = relevantTasks.filter(i => matchesCurrentAccount(i.account_id, i.account_name));
  }

  const now = Date.now();
  let overdueCount = 0;
  let dueTodayCount = 0;
  let dueThisWeekCount = 0;
  let totalDueSoon = 0;

  relevantTasks.forEach(t => {
    if (!t.due_date) return;
    const dueTime = new Date(t.due_date).getTime();
    if (isNaN(dueTime)) return;
    const diffDays = Math.round((dueTime - now) / 86400000);
    if (diffDays < 0) {
      overdueCount++;
      totalDueSoon++;
    } else if (diffDays === 0) {
      dueTodayCount++;
      totalDueSoon++;
    } else if (diffDays <= 7) {
      dueThisWeekCount++;
      totalDueSoon++;
    }
  });

  return {
    accounts,
    topAccount,
    generated: isGenerated(),
    velocity: velocity.this_week,
    velocityDeltaPct: velocity.delta_pct,
    velocityTrend: velocity.trend || [],
    execChangesOpen: execChangesAll.length,
    execAging: aging.length,
    execJoined: joined,
    execPromoted: promoted,
    execOther: other,
    playbookTotal: matchingPlays.length,
    playsInMotion: inMotionCount,
    playsStalled: stalledCount,
    totalDueSoon,
    overdueCount,
    dueTodayCount,
    dueThisWeekCount,
    openTasksCount: relevantTasks.length,
  };
}

export async function renderKpiStrip() {
  const k = await computeKpi();
  const deltaCls = k.velocityDeltaPct >= 0 ? 'cc-delta-up' : 'cc-delta-down';

  const card1 = `
    <div class="cc-kpi-card cc-kpi-clickable" data-kpi-action="jump-feed" title="Click to view Priority Signal Feed" tabindex="0" role="button">
      ${k.generated && k.velocityTrend.length > 1 ? `<div class="cc-kpi-spark-bg">${sparklineSvg(k.velocityTrend)}</div>` : ''}
      <div class="cc-kpi-label-row">
        <span class="cc-kpi-label">Signal velocity this week</span>
        <i class="fa-solid fa-arrow-up-right-from-square cc-kpi-icon"></i>
      </div>
      <div class="cc-kpi-value">${k.generated ? k.velocity : '&mdash;'}${k.generated ? ` <span class="cc-kpi-delta ${deltaCls}">${k.velocityDeltaPct >= 0 ? '+' : ''}${k.velocityDeltaPct}%</span>` : ''}</div>
      <div class="cc-kpi-foot">${k.generated ? 'signals vs. last week' : 'not generated yet'} &middot; <span class="cc-kpi-action-text">View Feed &rarr;</span></div>
    </div>`;

  const card3 = `
    <div class="cc-kpi-card cc-kpi-clickable" data-kpi-action="jump-timeline" title="Click to view Executive Movements" tabindex="0" role="button">
      <div class="cc-kpi-label-row">
        <span class="cc-kpi-label">Exec changes needing outreach</span>
        <i class="fa-solid fa-arrow-up-right-from-square cc-kpi-icon"></i>
      </div>
      <div class="cc-kpi-value">${k.execChangesOpen}</div>
      <div class="cc-kpi-foot ${k.execAging > 0 ? 'cc-warning-text' : ''}">
        ${k.execAging > 0 ? `${k.execAging} aging 5+ days` : 'all within outreach window'} &middot; <span class="cc-kpi-action-text">View Timeline &rarr;</span>
      </div>
      <div class="cc-chip-row">
        ${k.execAging ? `<button type="button" class="cc-chip cc-chip-interactive cc-chip-warning" data-timeline-filter="aging" title="Filter timeline by aging movements">${k.execAging} aging</button>` : ''}
        ${k.execJoined ? `<button type="button" class="cc-chip cc-chip-interactive cc-chip-plain" data-timeline-filter="joined" title="Filter timeline by newly joined execs">${k.execJoined} joined</button>` : ''}
        ${k.execPromoted ? `<button type="button" class="cc-chip cc-chip-interactive cc-chip-plain" data-timeline-filter="promoted" title="Filter timeline by promoted execs">${k.execPromoted} promoted</button>` : ''}
        ${k.execOther ? `<button type="button" class="cc-chip cc-chip-interactive cc-chip-plain" data-timeline-filter="other" title="Filter timeline by other movements">${k.execOther} other</button>` : ''}
        ${!k.execChangesOpen ? '<span class="cc-chip cc-chip-plain">none in last 30 days</span>' : ''}
      </div>
    </div>`;

  const card4 = `
    <div class="cc-kpi-card cc-kpi-clickable" data-kpi-action="jump-playbook" title="Click to view This Week's Playbook" tabindex="0" role="button">
      <div class="cc-kpi-label-row">
        <span class="cc-kpi-label">This week's playbook</span>
        <i class="fa-solid fa-arrow-up-right-from-square cc-kpi-icon"></i>
      </div>
      <div class="cc-kpi-value">${k.generated ? k.playbookTotal : (k.playbookTotal || 0)} <span class="cc-kpi-unit" style="font-size:0.85rem; font-weight:500; color:var(--text-secondary);">plays</span></div>
      <div class="cc-kpi-foot ${k.playsStalled > 0 ? 'cc-warning-text' : ''}">
        ${k.playsInMotion > 0 ? `<strong>${k.playsInMotion}</strong> in motion` : 'Ready to start'} &middot; <span class="cc-kpi-action-text">View Playbook &rarr;</span>
      </div>
      <div class="cc-chip-row">
        <span class="cc-chip cc-chip-plain">${k.playbookTotal} available</span>
        ${k.playsInMotion > 0 ? `<span class="cc-chip cc-chip-brand">${k.playsInMotion} in motion</span>` : ''}
      </div>
    </div>`;

  const cardDueSoon = `
    <div class="cc-kpi-card cc-kpi-clickable" data-kpi-action="jump-due-soon" title="Click to view Due Soon tasks" tabindex="0" role="button">
      <div class="cc-kpi-label-row">
        <span class="cc-kpi-label">Tasks due soon</span>
        <i class="fa-solid fa-arrow-up-right-from-square cc-kpi-icon"></i>
      </div>
      <div class="cc-kpi-value">${k.totalDueSoon} <span class="cc-kpi-unit" style="font-size:0.85rem; font-weight:500; color:var(--text-secondary);">due</span></div>
      <div class="cc-kpi-foot ${k.overdueCount > 0 ? 'cc-warning-text' : ''}">
        ${k.overdueCount > 0 ? `<strong style="color:var(--danger);">${k.overdueCount} overdue</strong>` : (k.dueTodayCount > 0 ? `${k.dueTodayCount} due today` : (k.totalDueSoon > 0 ? `${k.totalDueSoon} next 7 days` : 'all clear this week'))} &middot; <span class="cc-kpi-action-text">View Due Soon &rarr;</span>
      </div>
      <div class="cc-chip-row">
        ${k.overdueCount > 0 ? `<span class="cc-chip cc-chip-danger">${k.overdueCount} overdue</span>` : ''}
        ${k.dueTodayCount > 0 ? `<span class="cc-chip cc-chip-warning">${k.dueTodayCount} today</span>` : ''}
        ${k.dueThisWeekCount > 0 ? `<span class="cc-chip cc-chip-plain">${k.dueThisWeekCount} next 7d</span>` : ''}
        ${k.totalDueSoon === 0 ? `<span class="cc-chip cc-chip-brand">all caught up</span>` : ''}
      </div>
    </div>`;

  return { html: card1 + cardDueSoon + card3 + card4, data: k };
}

export function bindKpiListeners(container, kpiData) {
  if (!container) return;

  container.querySelectorAll('[data-kpi-action]').forEach(card => {
    const action = card.dataset.kpiAction;

    const executeAction = (e) => {
      if (e.target.closest('[data-timeline-filter]')) return;

      if (action === 'jump-feed') {
        highlightAndScrollTo('.cc-feed-panel');
      } else if (action === 'jump-timeline') {
        highlightAndScrollTo('.cc-timeline-panel');
      } else if (action === 'jump-playbook') {
        highlightAndScrollTo('.cc-playbook-panel');
      } else if (action === 'jump-due-soon') {
        highlightAndScrollTo('[data-widget-id="due_soon"]');
      }
    };

    card.addEventListener('click', executeAction);
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        executeAction(e);
      }
    });
  });

  // Wire sub-chips inside KPI cards
  container.querySelectorAll('[data-timeline-filter]').forEach(chip => {
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      const filter = chip.dataset.timelineFilter;
      ccState.timelineFilter = filter;
      renderTimeline();
      highlightAndScrollTo('.cc-timeline-panel');
    });
  });
}
