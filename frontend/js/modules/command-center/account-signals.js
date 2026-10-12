// Four account-level signal widgets, all derived from the same /api/accounts
// payload already fetched for the matrix/nav (no extra network calls):
// capital events (funding/M&A), org coverage gaps, competitor mentions, and
// tech/IP signals. Kept in one file since they share the same data source
// and row markup — see TASK_MANAGEMENT_README.md-style honesty: every field
// used here is a real column on Account, nothing fabricated.
import { loadRealAccounts, loadMatrixAccounts } from './real-accounts.js';
import { openDossier } from './drawer.js';
import { esc, formatMoney } from './utils.js';
import { renderSkeleton } from '../skeleton.js';
import { ccState, matchesCurrentAccount } from './state.js';

async function loadEnrichedAccounts() {
  const [raw, matrix] = await Promise.all([loadRealAccounts(), loadMatrixAccounts()]);
  const matrixById = new Map(matrix.map(a => [a.id, a]));
  return raw.map(a => ({ ...a, _matrix: matrixById.get(a.id) }));
}

function bindClickThrough(list, accountsById) {
  list.querySelectorAll('.cc-clickable-row').forEach(row => {
    row.addEventListener('click', () => {
      const account = accountsById.get(Number(row.dataset.accountId));
      if (account && account._matrix) openDossier(account._matrix);
    });
  });
}

async function renderWidget(listId, emptyMessage, computeEntries, rowHtml) {
  const list = document.getElementById(listId);
  if (!list) return;
  list.innerHTML = renderSkeleton('feed-rows');
  let accounts;
  try {
    accounts = await loadEnrichedAccounts();
  } catch (err) {
    console.error(err);
    list.innerHTML = '<li class="cc-drawer-empty">Could not load account data.</li>';
    return;
  }

  let filteredAccounts = accounts;
  const hasFilter = (ccState.activeAccountIds && ccState.activeAccountIds.size > 0) || ccState.activeAccountId;
  if (hasFilter) {
    filteredAccounts = accounts.filter(a => matchesCurrentAccount(a.id, a.name || a.display_name));
  }

  const entries = computeEntries(filteredAccounts);
  if (!entries.length) {
    list.innerHTML = `<li class="cc-drawer-empty">${emptyMessage}</li>`;
    return;
  }
  list.innerHTML = entries.map(rowHtml).join('');
  bindClickThrough(list, new Map(accounts.map(a => [a.id, a])));
}

// ── Capital events (funding / IPO / acquisitions) ──────────────────────
function capitalEntries(accounts) {
  const cutoff = Date.now() - 365 * 86400000;
  return accounts
    .filter(a => a.last_funding_date && !isNaN(new Date(a.last_funding_date).getTime()) && new Date(a.last_funding_date).getTime() >= cutoff)
    .map(a => ({ account: a, date: new Date(a.last_funding_date) }))
    .sort((a, b) => b.date - a.date);
}
function capitalRowHtml({ account: a, date }) {
  const amt = a.total_funding_amount_usd ? formatMoney(a.total_funding_amount_usd) : null;
  const acquisitions = a.num_acquisitions ? `${a.num_acquisitions} acquisition${a.num_acquisitions !== 1 ? 's' : ''}` : null;
  return `
    <li class="cc-feed-row cc-clickable-row" data-account-id="${a.id}">
      <div class="cc-feed-body">
        <div class="cc-feed-title-row"><span class="cc-feed-title">${esc(a.name)}</span></div>
        <div class="cc-feed-summary">${esc(a.last_funding_type || 'Funding event')}${amt ? ` — ${amt}` : ''}${acquisitions ? ` · ${acquisitions}` : ''}</div>
      </div>
      <div class="cc-feed-count">${esc(date.toLocaleDateString(undefined, { month: 'short', year: 'numeric' }))}</div>
    </li>`;
}
export function renderCapitalEvents() {
  return renderWidget('ccCapitalList', 'No funding, IPO, or acquisition events on file in the last 12 months for your accounts.', capitalEntries, capitalRowHtml);
}

// ── Org coverage gaps (no C-suite / thin contact coverage mapped) ──────
function getAccountCoverage(a) {
  const total = typeof a.total_contacts_captured === 'number'
    ? a.total_contacts_captured
    : (typeof a.num_contacts === 'number'
      ? a.num_contacts
      : (Array.isArray(a.personas) ? a.personas.length : 0));

  let cSuite = 0;
  if (typeof a.c_suite_count === 'number' && a.c_suite_count > 0) {
    cSuite = a.c_suite_count;
  } else if (Array.isArray(a.personas) && a.personas.length > 0) {
    cSuite = a.personas.filter(p => p.tier === 'C-Suite' || [1, 2].includes(p.hierarchy_level)).length;
  }

  const vp = typeof a.vp_count === 'number'
    ? a.vp_count
    : (Array.isArray(a.personas) ? a.personas.filter(p => (p.tier || '').toLowerCase().includes('vp')).length : 0);

  return { total, cSuite, vp };
}

function coverageEntries(accounts) {
  const gaps = [];
  for (const a of accounts) {
    const { total, cSuite, vp } = getAccountCoverage(a);
    if (total === 0) {
      gaps.push({ account: a, total, cSuite, gapType: 'no-contacts', gapLabel: 'No contacts mapped' });
    } else if (cSuite === 0) {
      gaps.push({ account: a, total, cSuite, gapType: 'no-csuite', gapLabel: 'No C-suite mapped' });
    } else if (vp === 0 && total < 10) {
      gaps.push({ account: a, total, cSuite, gapType: 'no-vp', gapLabel: 'No VP tier mapped' });
    }
  }
  return gaps.sort((a, b) => a.total - b.total);
}

function coverageRowHtml({ account: a, total, gapLabel }) {
  const acctName = a.name || a.display_name || a.key || 'Account';
  return `
    <li class="cc-feed-row cc-clickable-row cc-coverage-row" data-account-id="${a.id}">
      <div class="cc-feed-body">
        <div class="cc-coverage-title-row">
          <span class="cc-coverage-acct-name" title="${esc(acctName)}">${esc(acctName)}</span>
          <span class="cc-coverage-badge"><i class="fa-solid fa-triangle-exclamation"></i> ${esc(gapLabel || 'Coverage gap')}</span>
        </div>
        <div class="cc-coverage-sub-row">
          <span class="cc-coverage-count"><i class="fa-regular fa-user"></i> ${total} contact${total !== 1 ? 's' : ''} mapped</span>
          <span class="cc-coverage-action">Open Org Chart &rarr;</span>
        </div>
      </div>
    </li>`;
}

let coverageChartInstance = null;

function setupCoverageViewToggle() {
  const toggleGroup = document.getElementById('ccCoverageViewToggle');
  if (!toggleGroup) return;

  toggleGroup.querySelectorAll('[data-coverage-view]').forEach(btn => {
    btn.onclick = () => {
      toggleGroup.querySelectorAll('[data-coverage-view]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      const view = btn.dataset.coverageView;
      const chartWrap = document.getElementById('ccCoverageChartWrap');
      const listEl = document.getElementById('ccCoverageList');

      if (view === 'chart') {
        if (chartWrap) chartWrap.style.setProperty('display', 'flex', 'important');
        if (listEl) listEl.style.setProperty('display', 'none', 'important');
        if (coverageChartInstance) coverageChartInstance.resize();
      } else {
        if (chartWrap) chartWrap.style.setProperty('display', 'none', 'important');
        if (listEl) listEl.style.setProperty('display', 'block', 'important');
      }
    };
  });
}

function renderCoveragePieChart(accounts, activeAccountName) {
  const canvas = document.getElementById('ccCoveragePieCanvas');
  if (!canvas || typeof Chart === 'undefined') return;

  const totalMappedBadge = document.getElementById('ccTotalMappedBadge');
  const totalCSuiteBadge = document.getElementById('ccTotalCSuiteBadge');
  const panelNote = document.getElementById('ccCoveragePanelNote');

  let labels = [];
  let data = [];
  let bgColors = [];
  let hoverBgColors = [];
  let totalMapped = 0;
  let totalCSuite = 0;

  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';

  if (activeAccountName && accounts.length === 1) {
    // Single account tier breakdown mode
    const acct = accounts[0];
    const counts = getAccountCoverage(acct);
    totalMapped = counts.total;
    totalCSuite = counts.cSuite;
    const vp = typeof acct.vp_count === 'number' ? acct.vp_count : 0;
    const dir = typeof acct.director_count === 'number' ? acct.director_count : 0;
    const mgr = typeof acct.manager_count === 'number' ? acct.manager_count : 0;
    const other = Math.max(0, totalMapped - (totalCSuite + vp + dir + mgr));

    labels = ['C-Suite', 'VPs', 'Directors', 'Managers', 'Other Roles'];
    data = [totalCSuite, vp, dir, mgr, other];
    bgColors = ['#2563EB', '#0D9488', '#8B5CF6', '#F59E0B', '#94A3B8'];
    hoverBgColors = ['#1D4ED8', '#0F766E', '#7C3AED', '#D97706', '#64748B'];

    if (panelNote) {
      panelNote.textContent = `${esc(acct.name || acct.display_name)} · Tier Breakdown`;
    }
  } else {
    // Global Portfolio mode: contacts mapped per account
    accounts.forEach(a => {
      const counts = getAccountCoverage(a);
      totalMapped += counts.total;
      totalCSuite += counts.cSuite;
    });

    const palette = [
      '#2563EB', '#0D9488', '#8B5CF6', '#F59E0B', '#EC4899', '#06B6D4', '#10B981', '#6366F1'
    ];

    accounts.forEach((a, i) => {
      const counts = getAccountCoverage(a);
      const name = a.name || a.display_name || a.key || `Account ${a.id}`;
      const shortName = name.length > 16 ? name.substring(0, 14) + '…' : name;
      labels.push(shortName);
      data.push(counts.total);
      bgColors.push(palette[i % palette.length]);
      hoverBgColors.push(palette[i % palette.length]);
    });

    if (panelNote) {
      panelNote.textContent = `${totalMapped.toLocaleString()} contacts across ${accounts.length} accounts`;
    }
  }

  if (totalMappedBadge) totalMappedBadge.textContent = totalMapped.toLocaleString();
  if (totalCSuiteBadge) totalCSuiteBadge.textContent = totalCSuite.toLocaleString();

  if (coverageChartInstance) {
    coverageChartInstance.destroy();
    coverageChartInstance = null;
  }

  // Inline Plugin: Render bold numbers directly in colored slice areas
  const sliceNumbersPlugin = {
    id: 'ccSliceNumbers',
    afterDatasetsDraw(chart) {
      const { ctx, data } = chart;
      const meta = chart.getDatasetMeta(0);
      if (!meta || !meta.data || !meta.data.length) return;

      const dark = document.documentElement.getAttribute('data-theme') === 'dark';
      ctx.save();

      meta.data.forEach((element, i) => {
        const val = data.datasets[0].data[i];
        if (!val || val <= 0) return;

        const { startAngle, endAngle, innerRadius, outerRadius } = element;
        const span = endAngle - startAngle;
        const midAngle = (startAngle + endAngle) / 2;

        ctx.save();
        ctx.font = '700 11px Inter, system-ui, -apple-system, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        if (span >= 0.08) {
          // Render centered cleanly within the colored doughnut slice
          const midRadius = innerRadius + (outerRadius - innerRadius) * 0.52;
          const x = element.x + Math.cos(midAngle) * midRadius;
          const y = element.y + Math.sin(midAngle) * midRadius;

          ctx.fillStyle = '#FFFFFF';
          ctx.shadowColor = 'rgba(0, 0, 0, 0.55)';
          ctx.shadowBlur = 3;
          ctx.shadowOffsetX = 0;
          ctx.shadowOffsetY = 1;

          ctx.fillText(String(val), x, y);
        } else {
          // Very narrow slices: render with crisp positioned callout
          const lineStartRadius = outerRadius + 1;
          const lineEndRadius = outerRadius + 5;
          const labelRadius = outerRadius + 11;

          const lx1 = element.x + Math.cos(midAngle) * lineStartRadius;
          const ly1 = element.y + Math.sin(midAngle) * lineStartRadius;
          const lx2 = element.x + Math.cos(midAngle) * lineEndRadius;
          const ly2 = element.y + Math.sin(midAngle) * lineEndRadius;
          const tx = element.x + Math.cos(midAngle) * labelRadius;
          const ty = element.y + Math.sin(midAngle) * labelRadius;

          ctx.beginPath();
          ctx.moveTo(lx1, ly1);
          ctx.lineTo(lx2, ly2);
          ctx.strokeStyle = data.datasets[0].backgroundColor[i] || (dark ? '#94A3B8' : '#64748B');
          ctx.lineWidth = 1.5;
          ctx.stroke();

          ctx.fillStyle = dark ? '#F8FAFC' : '#0F172A';
          ctx.shadowColor = dark ? 'rgba(0, 0, 0, 0.7)' : 'rgba(255, 255, 255, 0.9)';
          ctx.shadowBlur = 2;
          ctx.fillText(String(val), tx, ty);
        }

        ctx.restore();
      });
      ctx.restore();
    }
  };

  // Inline Plugin: Bold total count in center cutout
  const centerMetricPlugin = {
    id: 'ccCenterMetric',
    beforeDraw(chart) {
      const meta = chart.getDatasetMeta(0);
      if (!meta || !meta.data || !meta.data[0]) return;
      const { x, y } = meta.data[0];
      const ctx = chart.ctx;
      const dark = document.documentElement.getAttribute('data-theme') === 'dark';

      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      ctx.font = '700 17px Inter, system-ui, -apple-system, sans-serif';
      ctx.fillStyle = dark ? '#F8FAFC' : '#0F172A';
      ctx.fillText(totalMapped.toLocaleString(), x, y - 5);

      ctx.font = '700 9px Inter, system-ui, -apple-system, sans-serif';
      ctx.fillStyle = dark ? '#94A3B8' : '#64748B';
      ctx.fillText('MAPPED', x, y + 11);
      ctx.restore();
    }
  };

  coverageChartInstance = new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: bgColors,
        hoverBackgroundColor: hoverBgColors,
        borderWidth: 2,
        borderColor: isDark ? '#1E293B' : '#FFFFFF',
        hoverOffset: 8,
        offset: 1
      }]
    },
    plugins: [sliceNumbersPlugin, centerMetricPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      devicePixelRatio: Math.max(window.devicePixelRatio || 1, 2),
      cutout: '55%',
      animation: {
        animateRotate: true,
        animateScale: true,
        duration: 750,
        easing: 'easeOutQuart'
      },
      onHover: (event, chartElement) => {
        const target = event.native && event.native.target;
        if (target) {
          target.style.cursor = chartElement && chartElement.length ? 'pointer' : 'default';
        }
      },
      layout: {
        padding: { top: 6, bottom: 6, left: 6, right: 6 }
      },
      plugins: {
        legend: {
          position: 'right',
          labels: {
            boxWidth: 11,
            boxHeight: 11,
            borderRadius: 3,
            padding: 7,
            font: {
              family: 'Inter, system-ui, -apple-system, sans-serif',
              size: 11.5,
              weight: '600'
            },
            color: isDark ? '#F1F5F9' : '#0F172A',
            generateLabels: (chart) => {
              const dataset = chart.data.datasets[0];
              return chart.data.labels.map((label, idx) => {
                const val = dataset.data[idx] || 0;
                return {
                  text: `${label} (${val})`,
                  fillStyle: dataset.backgroundColor[idx],
                  strokeStyle: dataset.borderColor,
                  lineWidth: 1,
                  hidden: false,
                  index: idx
                };
              });
            }
          }
        },
        tooltip: {
          backgroundColor: '#0F172A',
          titleFont: { family: 'Inter, system-ui, sans-serif', size: 12, weight: '700' },
          bodyFont: { family: 'Inter, system-ui, sans-serif', size: 12, weight: '500' },
          padding: 8,
          cornerRadius: 6,
          callbacks: {
            label: (ctx) => {
              const val = ctx.parsed || 0;
              const pct = totalMapped > 0 ? ((val / totalMapped) * 100).toFixed(1) : 0;
              return ` ${val} contacts (${pct}%)`;
            }
          }
        }
      }
    }
  });
}

export async function renderCoverageGaps() {
  const hasFilter = (ccState.activeAccountIds && ccState.activeAccountIds.size > 0) || ccState.activeAccountId;
  const names = ccState.selectedAccountNames || [];
  const emptyMsg = hasFilter
    ? 'Selected accounts have full executive C-suite coverage mapped.'
    : 'Every account has at least one C-suite contact mapped.';

  // 1. Render the underlying gaps list
  await renderWidget('ccCoverageList', emptyMsg, coverageEntries, coverageRowHtml);

  // 2. Load accounts and render Pie Chart
  try {
    const accounts = await loadEnrichedAccounts();
    let filteredAccounts = accounts;
    if (hasFilter) {
      filteredAccounts = accounts.filter(a => matchesCurrentAccount(a.id, a.name || a.display_name));
    }

    const labelName = names.length === 1 ? names[0] : (names.length > 1 ? `${names.length} Selected Accounts` : null);
    renderCoveragePieChart(filteredAccounts, labelName);
    setupCoverageViewToggle();
  } catch (err) {
    console.error('Error rendering coverage pie chart:', err);
  }
}



