import { esc } from './utils.js';

export const DASHBOARD_WIDGETS = [
  { id: 'my_day', name: 'My Day', icon: 'fa-sun', category: 'Summary', desc: 'What needs you today: overdue tasks and deal steps, quiet deals, the quarter and reasons to reach out' },
  { id: 'kpi', name: 'KPI Metric Strip', icon: 'fa-chart-line', category: 'Summary', desc: 'Velocity, executive outreach signals, and active plays' },
  { id: 'matrix', name: 'Account Priority Matrix', icon: 'fa-chart-pie', category: 'Strategic', desc: 'Interactive 4-quadrant bubble chart ranking accounts by signal & heat' },
  { id: 'priority_feed', name: 'Priority Signal Feed', icon: 'fa-bolt', category: 'Signals', desc: 'Real-time multi-source buying trigger alerts with new/old status' },
  { id: 'playbook', name: 'This Week’s Playbook', icon: 'fa-list-check', category: 'Plays', desc: 'Curated high-impact sales plays and CRM push actions' },
  { id: 'timeline', name: 'Exec Movements Timeline', icon: 'fa-timeline', category: 'Leadership', desc: 'Recent executive leadership additions and promotions' },
  { id: 'due_soon', name: 'Due Soon & Tasks', icon: 'fa-calendar-check', category: 'Execution', desc: 'Urgent action items and scheduled reminders' },
  { id: 'hiring', name: 'Hiring Signals Bulletin', icon: 'fa-user-tie', category: 'Signals', desc: 'Cross-organization hiring surges with next action steps' },
  { id: 'strategic_tracks', name: 'Strategic Investment Tracks', icon: 'fa-bullseye', category: 'Strategic', desc: 'Capital expenditure tracks and key sponsoring decision makers' },
  { id: 'capital', name: 'Capital Events & Funding', icon: 'fa-landmark', category: 'Finance', desc: 'Funding rounds, IPO filings, and strategic acquisitions' },
  { id: 'coverage', name: 'Org Coverage Gaps', icon: 'fa-sitemap', category: 'Coverage', desc: 'Identified persona coverage blindspots needing discovery' },
  { id: 'pain_points', name: 'Top Pain Points', icon: 'fa-fire-flame-curved', category: 'Discovery', desc: 'Ranked operational pain points mentioned by decision-makers' },
  { id: 'objections', name: 'Key Objections', icon: 'fa-shield-halved', category: 'Discovery', desc: 'Ranked sales and procurement objections across personas' },
  { id: 'news', name: 'Google News Feed', icon: 'fa-newspaper', category: 'News', desc: 'Live Google News RSS mentions and industry press coverage' },
];

const STORAGE_KEY = 'cc_widget_visibility_v1';
// Off until switched on: My Day's headline numbers and "Do today" list cover these (people who already
// customized the dashboard keep their saved choice)
const HIDDEN_BY_DEFAULT = new Set(['kpi', 'due_soon']);
const byDefault = (id) => !HIDDEN_BY_DEFAULT.has(id);

export function getWidgetPreferences() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      const parsed = JSON.parse(saved);
      // Ensure all current widgets have a boolean entry
      const result = {};
      DASHBOARD_WIDGETS.forEach(w => {
        result[w.id] = parsed[w.id] !== undefined ? Boolean(parsed[w.id]) : byDefault(w.id);
      });
      return result;
    }
  } catch (e) {
    console.error('Failed to parse widget preferences:', e);
  }

  // Default: every widget visible except the ones My Day replaces
  const defaults = {};
  DASHBOARD_WIDGETS.forEach(w => { defaults[w.id] = byDefault(w.id); });
  return defaults;
}

export function saveWidgetPreferences(prefs) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch (e) {
    console.error('Failed to save widget preferences:', e);
  }
}

export function applyWidgetVisibility(prefs) {
  const currentPrefs = prefs || getWidgetPreferences();

  // 1. Toggle visibility on all widgets
  DASHBOARD_WIDGETS.forEach(w => {
    const isVisible = currentPrefs[w.id] !== false;
    const elements = document.querySelectorAll(`[data-widget-id="${w.id}"]`);
    elements.forEach(el => {
      if (isVisible) {
        el.classList.remove('cc-widget-hidden');
      } else {
        el.classList.add('cc-widget-hidden');
      }
    });
  });

  // 2. Adjust parent row grids for 2-column rows
  document.querySelectorAll('.cc-row-2col, .cc-row-3col').forEach(row => {
    const panels = Array.from(row.querySelectorAll('.cc-panel[data-widget-id]'));
    if (panels.length !== 2) {
      // Multi-card grid: hidden cards just drop out and the rest reflow; hide the row only when empty.
      row.classList.toggle('cc-row-hidden', panels.every(p => p.classList.contains('cc-widget-hidden')));
      return;
    }
    if (panels.length === 2) {
      const p1Visible = !panels[0].classList.contains('cc-widget-hidden');
      const p2Visible = !panels[1].classList.contains('cc-widget-hidden');

      if (!p1Visible && !p2Visible) {
        row.classList.add('cc-row-hidden');
      } else {
        row.classList.remove('cc-row-hidden');
        if (p1Visible && !p2Visible) {
          panels[0].classList.add('cc-panel-fullwidth');
        } else {
          panels[0].classList.remove('cc-panel-fullwidth');
        }
        if (p2Visible && !p1Visible) {
          panels[1].classList.add('cc-panel-fullwidth');
        } else {
          panels[1].classList.remove('cc-panel-fullwidth');
        }
      }
    }
  });

  // 3. Trigger chart resize if matrix is visible
  if (currentPrefs.matrix && window.ccMatrixChart) {
    window.ccMatrixChart.resize();
  }
}

export function initWidgetCustomizer() {
  const customizeBtn = document.getElementById('ccCustomizeBtn');
  const modal = document.getElementById('ccCustomizeModal');
  const backdrop = document.getElementById('ccCustomizeBackdrop');
  const closeBtn = document.getElementById('ccCustomizeClose');
  const list = document.getElementById('ccWidgetToggleList');
  const resetBtn = document.getElementById('ccResetWidgetsBtn');
  const showAllBtn = document.getElementById('ccShowAllWidgetsBtn');
  const saveBtn = document.getElementById('ccSaveWidgetsBtn');

  // Apply initially saved preferences
  applyWidgetVisibility();

  if (!customizeBtn || !modal) return;

  function renderToggleList() {
    if (!list) return;
    const prefs = getWidgetPreferences();

    list.innerHTML = DASHBOARD_WIDGETS.map(w => {
      const checked = prefs[w.id] !== false;
      return `
        <div class="cc-custom-item">
          <div class="cc-custom-item-icon">
            <i class="fa-solid ${w.icon}"></i>
          </div>
          <div class="cc-custom-item-text">
            <div class="cc-custom-item-title">
              <strong>${esc(w.name)}</strong>
              <span class="cc-badge cc-badge-neutral">${esc(w.category)}</span>
            </div>
            <div class="cc-custom-item-desc">${esc(w.desc)}</div>
          </div>
          <label class="cc-switch">
            <input type="checkbox" data-widget-toggle="${w.id}" ${checked ? 'checked' : ''}>
            <span class="cc-slider"></span>
          </label>
        </div>
      `;
    }).join('');
  }

  function openModal() {
    renderToggleList();
    modal.classList.add('open');
    if (backdrop) backdrop.classList.add('open');
  }

  function closeModal() {
    modal.classList.remove('open');
    if (backdrop) backdrop.classList.remove('open');
  }

  customizeBtn.addEventListener('click', openModal);
  if (closeBtn) closeBtn.addEventListener('click', closeModal);
  if (backdrop) backdrop.addEventListener('click', closeModal);

  if (showAllBtn) {
    showAllBtn.addEventListener('click', () => {
      list.querySelectorAll('input[data-widget-toggle]').forEach(input => {
        input.checked = true;
      });
    });
  }

  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      list.querySelectorAll('input[data-widget-toggle]').forEach(input => {
        input.checked = byDefault(input.dataset.widgetToggle);
      });
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener('click', () => {
      const newPrefs = {};
      list.querySelectorAll('input[data-widget-toggle]').forEach(input => {
        newPrefs[input.dataset.widgetToggle] = input.checked;
      });
      saveWidgetPreferences(newPrefs);
      applyWidgetVisibility(newPrefs);
      closeModal();
    });
  }
}
