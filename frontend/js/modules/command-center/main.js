import '../fetch-instrumentation.js';
import { initThemeToggle } from '../theme.js';
import { initTopbarAuth } from '../topbar-auth.js';
import { showToast } from '../toast.js';
import { ccState } from './state.js';
import { renderKpiStrip, bindKpiListeners } from './kpi.js';
import { renderMatrix } from './matrix.js';
import { renderFeed } from './feed.js';
import { renderPlaybook } from './playbook.js';
import { renderTimeline } from './timeline.js';
import { initDrawer } from './drawer.js';
import { initAccountsNav } from './accounts-nav.js';
import { renderDueSoon } from './due-soon.js';
import { renderHiringSignals, renderStrategicInvestmentTracks } from './hiring-signals.js';
import { renderCapitalEvents, renderCoverageGaps } from './account-signals.js';
import { renderObjections } from './objections.js';
import { renderNewsFeed } from './news.js';
import { initWidgetGuides } from './widget-guide.js';
import { initAccountFilter } from './account-filter.js';
import { initWidgetCustomizer } from './widget-customizer.js';
import { loadCommandCenter, generateCommandCenter, getCommandCenter, isGenerated } from './generator.js';
import { esc, relativeTime } from './utils.js';

function weekRangeLabel() {
  const now = new Date();
  const day = now.getDay(); // 0=Sun..6=Sat
  const mondayOffset = day === 0 ? -6 : 1 - day;
  const monday = new Date(now); monday.setDate(now.getDate() + mondayOffset);
  const sunday = new Date(monday); sunday.setDate(monday.getDate() + 6);
  const fmt = (d) => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  return `Week of ${fmt(monday)} – ${fmt(sunday)}`;
}

function renderSubtitle() {
  const el = document.getElementById('ccSubtitle');
  if (!el) return;
  const names = ccState.selectedAccountNames || [];
  if (names.length === 1) {
    el.innerHTML = `<span class="cc-filtered-subtitle"><i class="fa-solid fa-filter"></i> Showing intelligence filtered for <strong>${esc(names[0])}</strong></span>`;
  } else if (names.length > 1) {
    const formatted = names.length <= 3
      ? names.map(n => `<strong>${esc(n)}</strong>`).join(', ')
      : `${names.slice(0, 2).map(n => `<strong>${esc(n)}</strong>`).join(', ')} <em>(+${names.length - 2} more)</em>`;
    el.innerHTML = `<span class="cc-filtered-subtitle"><i class="fa-solid fa-filter"></i> Showing intelligence filtered for ${formatted}</span>`;
  } else {
    const cc = getCommandCenter();
    const generated = isGenerated()
      ? `generated ${relativeTime(new Date(cc.generated_at))}`
      : 'feed &amp; playbook not generated yet';
    el.innerHTML = `${esc(weekRangeLabel())} · ${cc.plays.in_motion} open plays · <span class="cc-generated-at">${generated}</span>`;
  }
}

function initGenerateButton() {
  const btn = document.getElementById('ccGenerateBtn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const label = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Generating…';
    try {
      const data = await generateCommandCenter();
      ccState.activeDomainFilters.clear();
      renderAll();
      const n = data.signals.length;
      showToast(n
        ? `Generated ${n} signal${n === 1 ? '' : 's'} and ${data.playbook.length} play${data.playbook.length === 1 ? '' : 's'}`
        : 'Generated — no signals in the last 7 days. Run the pipeline or content refresh for your accounts first.');
    } catch (e) {
      showToast(e.message || 'Generation failed.');
    } finally {
      btn.disabled = false;
      btn.innerHTML = label;
    }
  });
}

async function renderKpi() {
  const container = document.getElementById('ccKpiStrip');
  if (!container) return;
  const { html, data } = await renderKpiStrip();
  container.innerHTML = html;
  bindKpiListeners(container, data);
}

function renderAll() {
  renderSubtitle();
  renderKpi();
  renderFeed();
  renderPlaybook();
  renderTimeline();
  renderDueSoon();
  renderHiringSignals();
  renderStrategicInvestmentTracks();
  renderCapitalEvents();
  renderCoverageGaps();
  renderObjections();
  renderNewsFeed();
}

function checkDashboardAccessNotice() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('no_dashboard_access') !== '1') return;
  showToast("You don't have Global Accounts Dashboard access yet — ask a super admin to grant you an account.");
  params.delete('no_dashboard_access');
  const rest = params.toString();
  history.replaceState(null, '', window.location.pathname + (rest ? `?${rest}` : ''));
}

async function init() {
  initThemeToggle();
  checkDashboardAccessNotice();
  initDrawer();
  initAccountsNav();
  initWidgetCustomizer();
  initGenerateButton();
  document.addEventListener('cc:plays-changed', () => { renderSubtitle(); renderKpi(); });
  await loadCommandCenter();
  await initAccountFilter((accountId, accountObj) => {
    renderAll();
    renderMatrix();
  });
  renderAll();
  renderMatrix();
  initWidgetGuides();
  window.addEventListener('resize', () => {
    if (ccState.matrixChart) ccState.matrixChart.resize();
  });
}

initTopbarAuth().then((user) => {
  if (!user) {
    document.body.style.display = 'none';
    window.location.replace(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    return;
  }
  if (user.role !== 'super_admin' && user.has_command_center_access === false) {
    if (user.has_dashboard_access !== false) {
      window.location.href = '/?no_command_center_access=1';
      return;
    } else if (user.has_tasks_access !== false) {
      window.location.href = '/tasks?no_command_center_access=1';
      return;
    } else {
      // In-place restricted notice - DO NOT REDIRECT IN A LOOP!
      const container = document.querySelector('.cc-container') || document.body;
      container.innerHTML = `
        <div class="empty-block" style="margin:80px auto; max-width:460px; text-align:center; padding:40px; background:var(--card-bg); border-radius:12px; border:1px solid var(--border-color);">
          <div class="empty-block-icon" style="font-size:2.5rem; color:var(--text-muted); margin-bottom:16px;"><i class="fa-solid fa-shield-halved"></i></div>
          <div style="font-size:1.15rem; font-weight:700; color:var(--text-primary); margin-bottom:8px;">Command Center Access Restricted</div>
          <div style="font-size:0.85rem; color:var(--text-secondary); line-height:1.5;">You do not have access to the Sales Command Center. Please ask a Super Administrator to grant you permissions.</div>
        </div>`;
      return;
    }
  }
  init();
});
