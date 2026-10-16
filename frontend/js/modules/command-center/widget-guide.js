/**
 * Widget Guide & Explanations for Sales Command Center
 * Provides concise, informative sales guidance on clicking the '?' button on each widget.
 */

export const WIDGET_GUIDES = {
  matrix: {
    title: 'Account Priority Matrix',
    icon: 'fa-solid fa-table-cells',
    badge: 'Prioritization & Strategy',
    summary: 'Plots monitored enterprise accounts across buying urgency (signal velocity) and deal potential (strategic value). Bubble size reflects estimated contract value.',
    actionTip: 'Focus daily prospecting on top-right quadrant accounts with high urgency and high value. Click any bubble to open the account dossier.',
    signals: 'SEC 10-K Disclosures, Multi-Source Intent Signals & Form 8-K Filings',
  },
  priority_feed: {
    title: 'Priority Signal Feed',
    icon: 'fa-solid fa-bolt',
    badge: 'Real-Time Intent Feed',
    summary: 'Buying triggers from the last 7 days — executive moves, hiring pushes, news and growth themes — scored 0 to 100 by seniority, strength and recency. Built when you press Generate.',
    actionTip: 'Press Generate after a pipeline or content refresh. Filter by category or account, then click "Create task" to add a follow-up to My Tasks.',
    signals: 'CXO Movements, LinkedIn Job Postings, News Articles & Opportunity Signals already in your database',
  },
  playbook: {
    title: "This Week's Playbook",
    icon: 'fa-solid fa-bookmark',
    badge: 'Weekly Action Plan',
    summary: 'The top 5 plays for this week, one per account and signal type, built from the highest-scoring signals in the Priority Signal Feed.',
    actionTip: 'Click "Create task" to create a task for it — open plays count toward "Open plays in motion". Press Generate again each week (or after new data lands).',
    signals: 'Priority Signal Feed (generated)',
  },
  timeline: {
    title: 'Executive Movements',
    icon: 'fa-solid fa-clock-rotate-left',
    badge: 'Leadership Shifts',
    summary: 'Chronological timeline of leadership appointments, promotions, and departures across key buying committee personas over the last 30 days.',
    actionTip: 'Engage newly appointed executives during their first 90 days when they are evaluating new vendors and establishing modernization budgets.',
    signals: 'CXO Leadership Tracker & Verified LinkedIn Updates',
  },
  due_soon: {
    title: 'Due Soon & Follow-ups',
    icon: 'fa-solid fa-circle-check',
    badge: 'Task Management',
    summary: 'Centralized list of scheduled outreach tasks, prospect follow-ups, and CRM action items due within the next 7 days.',
    actionTip: 'Review daily to maintain deal momentum and ensure no client commitments or follow-ups slip through the cracks. Click "View all" for full task queue.',
    signals: 'Internal Task Console & CRM Sync',
  },
  hiring: {
    title: 'Hiring Signals',
    icon: 'fa-solid fa-briefcase',
    badge: 'Talent & Capacity Demand',
    summary: 'Tracks open requisitions, leadership hiring, contractor demand, and active tech hubs queried directly from PostgreSQL.',
    actionTip: 'Surges in technical roles or contract flags indicate funded initiatives. Expand the "Live Requisitions Browser" to view live postings.',
    signals: 'Greenhouse, Lever & LinkedIn Job Scrapers',
  },
  strategic_tracks: {
    title: 'Strategic Investment Tracks',
    icon: 'fa-solid fa-sitemap',
    badge: 'Enterprise Alignment',
    summary: 'Translates technical hiring clusters into major enterprise investment tracks (AI Hub, Cloud Modernization) mapped directly to sponsoring C-Suite & VP leaders.',
    actionTip: 'Align your sales pitch with the target sponsor and recommended pitch play to address their specific department budget priorities.',
    signals: 'SEC 10-K Disclosures & Executive Spends',
  },
  capital: {
    title: 'Capital Events',
    icon: 'fa-solid fa-coins',
    badge: 'Liquidity & Budget Triggers',
    summary: 'Monitors funding rounds, acquisitions, IPO announcements, and corporate restructuring across target accounts over the last 12 months.',
    actionTip: 'Capital events unlock fresh IT budget and create post-merger integration needs—prime windows for enterprise digital transformation pitches.',
    signals: 'SEC EDGAR, Finnhub & Crunchbase',
  },
  coverage: {
    title: 'Org Coverage Gaps',
    icon: 'fa-solid fa-user-xmark',
    badge: 'Relationship Risk',
    summary: 'Audits relationship health across departments to highlight single-threaded accounts and unmapped decision makers.',
    actionTip: 'Multi-thread into accounts by identifying and prospecting missing committee roles before single-point-of-contact deals stall.',
    signals: 'Diffbot Knowledge Graph & Persona Mapping',
  },
  pain_points: {
    title: 'Operational Pain Points',
    icon: 'bi-exclamation-triangle-fill',
    badge: 'Friction & Roadblocks',
    summary: 'Ranks operational pain points and workflow bottlenecks captured across every executive persona in your accounts, ordered by frequency.',
    actionTip: 'Lead your outreach and discovery questions with the #1 ranked pain point across the target account to trigger immediate resonance.',
    signals: 'Persona AI-dossier operational_pain_points array fields',
  },
  objections: {
    title: 'Common Objections',
    icon: 'bi-chat-square-quote-fill',
    badge: 'Pitch Preparation',
    summary: 'Ranks the buying hesitations and sales objections captured across all executive personas, showing which accounts share each concern.',
    actionTip: 'Pre-empt the top objection before the prospect raises it — address budget, timeline, or security concerns proactively in your deck.',
    signals: 'Persona AI-dossier key_objections array fields',
  },
  decision_makers: {
    title: 'New & Changed Decision-Makers',
    icon: 'bi-person-lines-fill',
    badge: 'Buying Committee Changes',
    summary: 'Executives who joined or were promoted in the last 30 days at accounts you track, cross-checked against your own persona directory.',
    actionTip: '"Not yet mapped" means a real gap — add them to the buying committee. "Role change" means someone you already have a relationship with just gained influence — re-engage them.',
    signals: 'CXO movement feed cross-referenced against mapped personas per account',
  },
  news: {
    title: 'Google News',
    icon: 'fa-solid fa-newspaper',
    badge: 'Public Coverage',
    summary: 'Recent Google News coverage already captured in the database for your accounts — earnings, leadership, market moves, and general press.',
    actionTip: 'Reference a specific, recent headline in outreach — it signals you\'re paying attention to their business, not sending a generic template.',
    signals: 'Google News RSS & Public Press Wire',
  },
};

/**
 * Initializes the widget info modal and binds click handlers to all ? buttons.
 */
export function initWidgetGuides() {
  const modal = document.getElementById('ccWidgetInfoModal');
  const backdrop = document.getElementById('ccWidgetInfoBackdrop');
  const closeBtn = document.getElementById('ccWidgetInfoClose');
  const titleEl = document.getElementById('ccWidgetInfoTitle');
  const iconEl = document.getElementById('ccWidgetInfoIcon');
  const badgeEl = document.getElementById('ccWidgetInfoBadge');
  const summaryEl = document.getElementById('ccWidgetInfoSummary');
  const actionEl = document.getElementById('ccWidgetInfoAction');
  const signalsEl = document.getElementById('ccWidgetInfoSignals');

  function openGuide(key) {
    const guide = WIDGET_GUIDES[key];
    if (!guide || !modal) return;

    if (titleEl) titleEl.textContent = guide.title;
    if (iconEl) iconEl.className = `${guide.icon || 'fa-solid fa-circle-info'}`;
    if (badgeEl) badgeEl.textContent = guide.badge || 'Doshin';
    if (summaryEl) summaryEl.textContent = guide.summary;
    if (actionEl) actionEl.textContent = guide.actionTip;
    if (signalsEl) signalsEl.textContent = guide.signals || 'Real-time telemetry';

    modal.classList.add('active');
    if (backdrop) backdrop.classList.add('active');
    document.body.style.overflow = 'hidden';
  }

  function closeGuide() {
    if (modal) modal.classList.remove('active');
    if (backdrop) backdrop.classList.remove('active');
    document.body.style.overflow = '';
  }

  // Bind all ? buttons in the dashboard
  document.querySelectorAll('[data-widget-info]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const key = btn.getAttribute('data-widget-info');
      openGuide(key);
    });
  });

  closeBtn?.addEventListener('click', closeGuide);
  backdrop?.addEventListener('click', closeGuide);

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modal?.classList.contains('active')) {
      closeGuide();
    }
  });
}
