/**
 * Complete Tour Step Definitions for Every Page and Widget in Doshin Sales AI
 * Covers all widgets, primary action buttons, filters, and the Copilot assistant.
 */

export const TOUR_PAGES = {
  // ── 1. Sales Command Center ─────────────────────────────────────
  'command-center': {
    name: 'Sales Command Center',
    badge: 'Executive Cockpit',
    steps: [
      {
        target: '#ccAcctFilterBtn',
        title: 'Global & Multi-Account Filter',
        icon: 'fa-solid fa-building',
        desc: 'Filter intelligence across your entire portfolio, or select single or multiple target accounts to focus your session.',
        tip: 'Type in the search field to filter accounts and tickers instantly.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#ccGenerateBtn',
        title: 'Generate Fresh Intelligence',
        icon: 'fa-solid fa-bolt',
        desc: 'Re-scores and generates the Priority Signal Feed and Weekly Playbook from the latest 7 days of ingested market telemetry.',
        tip: 'Click this button each Monday or after new SEC filings and hiring data land.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#ccCustomizeBtn',
        title: 'Customize Dashboard Widgets',
        icon: 'fa-solid fa-sliders',
        desc: 'Personalize your workspace: show, hide, or rearrange widgets (Hiring Trends, Capital Events, Coverage Gaps, Objections).',
        tip: 'Your visibility preferences are remembered automatically.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#ccKpiStrip',
        targetFallback: '[data-widget-id="kpi"]',
        title: 'Executive KPI Strip',
        icon: 'fa-solid fa-chart-simple',
        desc: 'High-level weekly execution metrics: open plays in motion, outreach touches due today, and high-urgency buying signals.',
        tip: 'Click any metric card to filter the feed to items matching that status.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#ccMatrixCanvas',
        targetFallback: '[data-widget-id="matrix"]',
        title: 'Account Priority Matrix',
        icon: 'fa-solid fa-table-cells',
        desc: 'Plots accounts across buying urgency (signal velocity) and deal potential (strategic value). Bubble size reflects estimated contract value.',
        tip: 'Focus prospecting on top-right quadrant accounts exhibiting high heat and strategic value. Click any bubble to inspect.',
        preferredPlacement: 'right'
      },
      {
        target: '#ccFeedList',
        targetFallback: '[data-widget-id="priority_feed"]',
        title: 'Priority Signal Feed',
        icon: 'fa-solid fa-bolt',
        desc: 'Real-time buying triggers scored 0 to 100 based on seniority, strength, and recency (hiring surges, C-suite moves, funding).',
        tip: 'Click "Create task" on any signal row to immediately add an action item to My Tasks.',
        preferredPlacement: 'left'
      },
      {
        target: '#ccPlaybookList',
        targetFallback: '[data-widget-id="playbook"]',
        title: "This Week's Playbook",
        icon: 'fa-solid fa-bookmark',
        desc: 'Curated action plays recommending specific executive targets and customized talk tracks based on the strongest weekly signals.',
        tip: 'Work through these top 5 prioritized plays to kick off your weekly sales outreach.',
        preferredPlacement: 'right'
      },
      {
        target: '#ccDueSoonList',
        targetFallback: '[data-widget-id="due_soon"]',
        title: 'Due Soon & Tasks',
        icon: 'fa-solid fa-circle-check',
        desc: 'Centralized queue of outreach tasks, scheduled calls, and follow-ups due within the next 7 days.',
        tip: 'Click "View all" to manage your complete task queue in My Tasks.',
        preferredPlacement: 'top'
      },
      {
        target: '#ccTimelineList',
        targetFallback: '[data-widget-id="timeline"]',
        title: 'Executive Movements (Last 30 Days)',
        icon: 'fa-solid fa-clock-rotate-left',
        desc: 'Chronological tracker of leadership appointments, promotions, and CXO departures across monitored accounts.',
        tip: 'Engage newly appointed executives during their first 90 days when modernizing budgets are allocated.',
        preferredPlacement: 'top'
      },
      {
        target: '#ccHiringList',
        targetFallback: '[data-widget-id="hiring"]',
        title: 'Hiring Signals & Requisitions',
        icon: 'fa-solid fa-briefcase',
        desc: 'Monitors open technical roles, leadership requisitions, contractor demand, and tech hubs queried directly from live listings.',
        tip: 'Spikes in cloud, AI, or engineering requisitions signal funded transformation initiatives.',
        preferredPlacement: 'top'
      },
      {
        target: '#ccCapitalList',
        targetFallback: '[data-widget-id="capital"]',
        title: 'Capital Events & Liquidity',
        icon: 'fa-solid fa-coins',
        desc: 'Tracks funding rounds, M&A acquisitions, IPO filings, and corporate restructuring events from the last 12 months.',
        tip: 'Capital events unlock fresh IT budget and create post-merger integration opportunities.',
        preferredPlacement: 'top'
      },
      {
        target: '#ccCoverageChartWrap',
        targetFallback: '[data-widget-id="coverage"]',
        title: 'Org Coverage Gaps',
        icon: 'fa-solid fa-user-xmark',
        desc: 'Audits relationship health across departments. Visualizes mapped contacts and flags single-threaded accounts with missing decision-makers.',
        tip: 'Multi-thread into accounts by mapping additional committee roles before deals stall.',
        preferredPlacement: 'top'
      },
      {
        target: '#ccPainPointsBody',
        targetFallback: '[data-widget-id="pain_points"]',
        title: 'Operational Pain Points',
        icon: 'fa-solid fa-triangle-exclamation',
        desc: 'Ranks operational friction points and workflow bottlenecks extracted across executive personas, ordered by occurrence.',
        tip: 'Lead discovery questions with the #1 ranked pain point to trigger immediate stakeholder resonance.',
        preferredPlacement: 'top'
      },
      {
        target: '#ccObjectionsBody',
        targetFallback: '[data-widget-id="objections"]',
        title: 'Common Field Objections',
        icon: 'fa-solid fa-shield-halved',
        desc: 'Ranks real-world sales objections and buying hesitations, showing which accounts share each concern and how to counter them.',
        tip: 'Pre-empt top objections in your proposal decks before the prospect raises them.',
        preferredPlacement: 'top'
      },
      {
        target: '#ccTracksList',
        targetFallback: '[data-widget-id="strategic_tracks"]',
        title: 'Strategic Investment Tracks & Sponsors',
        icon: 'fa-solid fa-sitemap',
        desc: 'Maps hiring clusters into major enterprise investment initiatives (AI Hub, Cloud Modernization) tied to sponsoring C-suite leaders.',
        tip: 'Pitch your solution directly aligned with the sponsoring executive’s strategic charter.',
        preferredPlacement: 'top'
      },
      {
        target: '[data-widget-id="news"]',
        targetFallback: '#ccNewsAccountFilter',
        title: 'Google News Feed & Subscriptions',
        icon: 'fa-solid fa-newspaper',
        desc: 'Real-time public press coverage, earnings releases, and leadership announcements captured for your accounts.',
        tip: 'Click "Subscribe" to receive automatic alerts when new headlines drop for target accounts.',
        preferredPlacement: 'top'
      },
      {
        target: '#cpdFab',
        targetFallback: '.cpd-fab',
        title: 'Ask Sales Copilot',
        icon: 'fa-solid fa-wand-magic-sparkles',
        desc: 'Your 24/7 AI Sales Assistant. Ask questions about any account, draft tailored outreach emails, or generate pre-call briefings.',
        tip: 'Click this floating button or press Ctrl+K anywhere to open Copilot.',
        preferredPlacement: 'left'
      }
    ]
  },

  // ── 2. Global Accounts Dashboard ────────────────────────────────
  'dashboard': {
    name: 'Global Accounts Dashboard',
    badge: 'Account Explorer',
    steps: [
      {
        target: '#navSearch',
        title: 'Global Search & Quick Filter',
        icon: 'fa-solid fa-magnifying-glass',
        desc: 'Search through all accounts by company name, stock ticker symbol, or industry classification.',
        tip: 'Press Ctrl+K (or Cmd+K) anywhere to open global search.',
        preferredPlacement: 'bottom'
      },
      {
        target: '.nav-toolbar',
        title: 'Sort & Hierarchy Controls',
        icon: 'fa-solid fa-arrow-down-a-z',
        desc: 'Sort accounts by highest opportunity score, contact count, or recent updates. Expand or collapse all corporate trees.',
        tip: 'Sort by "Top Score" to find your hottest prospects first.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#dashNav',
        targetFallback: '#navAccountList',
        title: 'Account Navigator Tree',
        icon: 'fa-solid fa-city',
        desc: 'Hierarchical directory of accounts and subsidiaries. Click any company to load its comprehensive sales dossier.',
        tip: 'Companies with unread signals display active indicator badges.',
        preferredPlacement: 'right'
      },
      {
        target: '#dashContent',
        targetFallback: '.dash-main',
        title: 'Account Intelligence Dossier',
        icon: 'fa-solid fa-building-user',
        desc: 'Core corporate snapshot: annual revenue, employee headcount, operating status, and opportunity heat score.',
        tip: 'Click on signal chips to reveal full source citations and matching service lines.',
        preferredPlacement: 'left'
      },
      {
        target: '#salesTabsNav',
        title: 'Workflow Intelligence Tabs',
        icon: 'fa-solid fa-layer-group',
        desc: 'Switch between Executive Briefing, Org Hierarchy, Sales Battlecards & Alerts, Social Listening, Weekly Updates, and Hiring Radar.',
        tip: 'The Org Hierarchy tab maps the power structure and key champions.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#dashPeople',
        targetFallback: '.dash-people',
        title: 'Buying Committee & Decision Makers',
        icon: 'fa-solid fa-users-viewfinder',
        desc: 'Maps decision-makers, champions, budget holders, and technical evaluators with direct LinkedIn profiles and contact details.',
        tip: 'Click on any persona card to launch their deep-dive profile or draft personalized emails.',
        preferredPlacement: 'left'
      },
      {
        target: '#cpdFab',
        targetFallback: '.cpd-fab',
        title: 'Ask Sales Copilot',
        icon: 'fa-solid fa-wand-magic-sparkles',
        desc: 'AI copilot automatically knows which account you are viewing. Ask it to draft customized meeting briefs or compare competitors.',
        tip: 'Click this button to open the AI conversation dock.',
        preferredPlacement: 'top'
      }
    ]
  },

  // ── 3. Deals Pipeline ───────────────────────────────────────────
  'deals': {
    name: 'Deals Pipeline',
    badge: 'Revenue Pipeline',
    steps: [
      {
        target: '#dlNew',
        title: '+ New Deal',
        icon: 'fa-solid fa-plus',
        desc: 'Create an opportunity. Enter target account, business line, contract value, expected close date, and starting stage.',
        tip: 'Assign StradIT offerings to automatically inherit relevant battlecards.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#dlSearch',
        title: 'Search Deals',
        icon: 'fa-solid fa-magnifying-glass',
        desc: 'Filter deals instantly by prospect name, opportunity title, or account keyword.',
        preferredPlacement: 'bottom'
      },
      {
        target: '.dl-tools',
        title: 'Pipeline Filters',
        icon: 'fa-solid fa-filter',
        desc: 'Filter by account, business line, or toggle "My Deals" to isolate your own active opportunities.',
        tip: 'Keep "My Deals" checked for focused daily execution.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#dlDigest',
        title: 'Pipeline Metrics Digest',
        icon: 'fa-solid fa-chart-pie',
        desc: 'Summarizes total pipeline volume, weighted forecasted revenue, and stage conversion probability rollups.',
        tip: 'Weighted forecast automatically discounts deals based on stage progression.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#dlBoard',
        title: 'Stage Progression Kanban',
        icon: 'fa-solid fa-table-columns',
        desc: 'Drag deals across stages: Intro → Discovery → Proposal → Pilot → Contract. Moving cards updates the executive forecast.',
        tip: 'Ensure each deal card has a committed next step and due date.',
        preferredPlacement: 'top'
      },
      {
        target: '#cpdFab',
        targetFallback: '.cpd-fab',
        title: 'Ask Sales Copilot',
        icon: 'fa-solid fa-wand-magic-sparkles',
        desc: 'Use AI to unblock stuck deals, summarize deal progression, or draft follow-up proposals.',
        preferredPlacement: 'top'
      }
    ]
  },

  // ── 4. Revenue Forecast ─────────────────────────────────────────
  'forecast': {
    name: 'Revenue Forecast',
    badge: 'Executive Forecast',
    steps: [
      {
        target: '#fcPeriod',
        title: 'Fiscal Quarter Selector',
        icon: 'fa-solid fa-calendar',
        desc: 'Switch between current, prior, and future quarters to analyze quota pacing and historical trends.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#fcExport',
        title: 'Export Forecast',
        icon: 'fa-regular fa-file-excel',
        desc: 'Download your executive forecast, roll-ups, and pipeline changes directly into formatted Excel spreadsheets.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#fcKpis',
        title: 'Forecast Targets & Coverage',
        icon: 'fa-solid fa-bullseye',
        desc: 'Compares Quota Target, Committed Revenue, Best-Case Scenario, and overall Pipeline Coverage ratio.',
        tip: 'Maintain at least 3x pipeline coverage for consistent quota attainment.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#fcBar',
        title: 'Progress to Target Bar',
        icon: 'fa-solid fa-bars-progress',
        desc: 'Visual progress gauge showing the exact percentage of your quota already closed or solidly committed.',
        tip: 'Hover over the bar to see the exact monetary variance remaining to target.',
        preferredPlacement: 'bottom'
      },
      {
        target: '.fc-seg',
        title: 'Segmentation Controls',
        icon: 'fa-solid fa-layer-group',
        desc: 'Pivot the roll-up table by Owner (reps), Business Line, Opportunity Stage, or Deal Category.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#fcRollup',
        title: 'Forecast Roll-Up Table',
        icon: 'fa-solid fa-table',
        desc: 'Detailed matrix breakdown showing total deals, weighted amounts, and commitment status per segment.',
        preferredPlacement: 'top'
      },
      {
        target: '#fcChanges',
        title: '"What Moved" Variance Tracker',
        icon: 'fa-solid fa-arrow-trend-up',
        desc: 'Audits recent pipeline velocity: flags deals that slipped close dates, changed value, or progressed stages in the last 7 days.',
        tip: 'Catch slipping deals early before the quarter closes.',
        preferredPlacement: 'left'
      },
      {
        target: '#cpdFab',
        targetFallback: '.cpd-fab',
        title: 'Ask Sales Copilot',
        icon: 'fa-solid fa-wand-magic-sparkles',
        desc: 'Ask Copilot to analyze forecast risks or identify deals needed to reach quota.',
        preferredPlacement: 'top'
      }
    ]
  },

  // ── 5. Introductions & Connectors ───────────────────────────────
  'introductions': {
    name: 'Introductions & Connectors',
    badge: 'Partner Network',
    steps: [
      {
        target: '#inNew',
        title: '+ New Introduction',
        icon: 'fa-solid fa-handshake',
        desc: 'Record a warm introduction from an advisor, partner, investor, or board member.',
        tip: 'Logging attribution ensures partners get proper credit upon deal closing.',
        preferredPlacement: 'bottom'
      },
      {
        target: '.in-tabs',
        title: 'View Switcher',
        icon: 'fa-solid fa-layer-group',
        desc: 'Switch between the Status Board, Attribution breakdown, and the Connector Directory.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#inBoard',
        title: 'Introductions Workflow Kanban',
        icon: 'fa-solid fa-table-columns',
        desc: 'Track referrals from Inbound → Outreach Initiated → Discovery Call → Converted to Active Deal.',
        tip: 'Triage new introductions within 24 hours to maximize conversion rates.',
        preferredPlacement: 'top'
      },
      {
        target: '#inExport',
        title: 'Export Referrals',
        icon: 'fa-regular fa-file-excel',
        desc: 'Export the complete introduction database and connector attribution metrics to Excel.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#cpdFab',
        targetFallback: '.cpd-fab',
        title: 'Ask Sales Copilot',
        icon: 'fa-solid fa-wand-magic-sparkles',
        desc: 'Draft warm introductory emails referencing your mutual connector with one click.',
        preferredPlacement: 'top'
      }
    ]
  },

  // ── 6. My Tasks ─────────────────────────────────────────────────
  'tasks': {
    name: 'My Tasks & Action Queue',
    badge: 'Task Management',
    steps: [
      {
        target: '#tpCreateTaskBtn',
        title: '+ Create Task',
        icon: 'fa-solid fa-plus',
        desc: 'Schedule follow-up calls, proposal deliveries, or discovery review items.',
        tip: 'Link the task to an account to automatically display it in that account’s activity timeline.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#tpExport',
        title: 'Export Tasks',
        icon: 'fa-regular fa-file-excel',
        desc: 'Export your current task queue with applied filters into an Excel file.',
        preferredPlacement: 'bottom'
      },
      {
        target: '.tp-kpi-grid',
        title: 'Task Urgency Counters',
        icon: 'fa-solid fa-list-check',
        desc: 'At-a-glance summary of Total Open Tasks, tasks Due Today, and Overdue items.',
        tip: 'Always prioritize clearing "Overdue" tasks first.',
        preferredPlacement: 'bottom'
      },
      {
        target: '.tp-filter-bar',
        targetFallback: '#tpSearch',
        title: 'Task Filters & Search',
        icon: 'fa-solid fa-filter',
        desc: 'Filter tasks by status (Pending, Done), priority (Urgent, Normal), or search for specific deals.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#tpTaskList',
        title: 'Action Item Checklist',
        icon: 'fa-solid fa-square-check',
        desc: 'Your execution queue. Mark tasks complete with one click, or click on a task to edit notes and dates.',
        tip: 'Completing tasks logged from signals automatically updates Command Center play counts.',
        preferredPlacement: 'top'
      },
      {
        target: '#cpdFab',
        targetFallback: '.cpd-fab',
        title: 'Ask Sales Copilot',
        icon: 'fa-solid fa-wand-magic-sparkles',
        desc: 'Ask Copilot to help draft the email or prepare the talking points for your next upcoming task.',
        preferredPlacement: 'top'
      }
    ]
  },

  // ── 7. Sales Copilot Full Workspace ─────────────────────────────
  'copilot': {
    name: 'Sales Copilot AI Workspace',
    badge: 'AI Research Suite',
    steps: [
      {
        target: '#cpNewChat',
        title: '+ New Chat Session',
        icon: 'fa-solid fa-plus',
        desc: 'Start a fresh conversation with the sales intelligence copilot for account deep-dives, RFP support, or strategic research.',
        preferredPlacement: 'bottom'
      },
      {
        target: '.cp-tabs',
        title: 'Chats vs Pinned Notes',
        icon: 'fa-solid fa-thumbtack',
        desc: 'Switch between interactive conversation sessions and your private pinned knowledge notes.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#cpAnswerStyle',
        title: 'Answer Style Controls',
        icon: 'fa-solid fa-sliders',
        desc: 'Select whether you want Brief executive summaries, Balanced answers, or Detailed comprehensive breakdowns.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#cpdInput',
        targetFallback: '.cp-composer',
        title: 'Interactive Copilot Prompt Bar',
        icon: 'fa-solid fa-terminal',
        desc: 'Ask questions about any account, use @ to mention specific personas, or use slash commands like /brief and /pitch.',
        preferredPlacement: 'top'
      }
    ]
  },

  // ── 8. Email & Calendar Sync ────────────────────────────────────
  'capture': {
    name: 'Email & Calendar Sync',
    badge: 'Activity Capture',
    steps: [
      {
        target: '.cap-main',
        targetFallback: '#capMain',
        title: 'Automatic Activity Capture',
        icon: 'fa-solid fa-envelope-circle-check',
        desc: 'Meetings and emails with your contacts are automatically logged to the right account, deal, and introduction records.',
        preferredPlacement: 'bottom'
      },
      {
        target: '#cpdFab',
        targetFallback: '.cpd-fab',
        title: 'Ask Sales Copilot',
        icon: 'fa-solid fa-wand-magic-sparkles',
        desc: 'Ask Copilot to summarize recent emails or draft follow-up notes from recent calendar meetings.',
        preferredPlacement: 'top'
      }
    ]
  }
};
