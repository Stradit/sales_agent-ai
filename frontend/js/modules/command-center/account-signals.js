// Four account-level signal widgets, all derived from the same /api/accounts
// payload already fetched for the matrix/nav (no extra network calls):
// capital events (funding/M&A), org coverage gaps, competitor mentions, and
// tech/IP signals. Kept in one file since they share the same data source
// and row markup — see TASK_MANAGEMENT_README.md-style honesty: every field
// used here is a real column on Account, nothing fabricated.
import { loadRealAccounts, loadMatrixAccounts } from './real-accounts.js';
import { openDossier } from './drawer.js';
import { dealsFor, dealContext } from './deal-context.js';
import { esc, formatMoney } from './utils.js';
import { renderSkeleton } from '../skeleton.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { createTask, isTasked } from './actions.js';

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
  const taskTitle = `Capital Event: ${a.last_funding_type || 'Funding'} at ${a.name}`;
  const tasked = isTasked(a.name, taskTitle, a.id);
  const btnCls = tasked ? 'cc-btn-done' : 'cc-btn-primary';
  const btnHtml = tasked ? '<i class="fa-solid fa-check"></i> Tasked' : '<i class="fa-solid fa-plus"></i> Create task';

  return `
    <li class="cc-feed-row cc-clickable-row" data-account-id="${a.id}">
      <div class="cc-feed-body">
        <div class="cc-feed-title-row"><span class="cc-feed-title">${esc(a.name)}</span></div>
        <div class="cc-feed-summary">${esc(a.last_funding_type || 'Funding event')}${amt ? ` — ${amt}` : ''}${acquisitions ? ` · ${acquisitions}` : ''}</div>
      </div>
      <div class="cc-feed-meta-col">
        <div class="cc-feed-count">${esc(date.toLocaleDateString(undefined, { month: 'short', year: 'numeric' }))}</div>
        <button type="button" class="cc-btn ${btnCls} cc-btn-xs cc-capital-task-btn" data-act="capital-task" data-account-name="${esc(a.name)}" data-account-id="${a.id}" data-task-title="${esc(taskTitle)}" title="${tasked ? 'Task already created' : `Create task for ${esc(a.name)}`}" ${tasked ? 'disabled' : ''}>${btnHtml}</button>
      </div>
    </li>`;
}
export async function renderCapitalEvents() {
  await renderWidget('ccCapitalList', 'No funding, IPO, or acquisition events on file in the last 12 months for your accounts.', capitalEntries, capitalRowHtml);
  const list = document.getElementById('ccCapitalList');
  if (list) {
    list.querySelectorAll('.cc-capital-task-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        btn.disabled = true;
        const acctName = btn.dataset.accountName;
        const acctId = Number(btn.dataset.accountId);
        const taskTitle = btn.dataset.taskTitle;
        const ok = await createTask(acctName, taskTitle, acctId);
        if (ok) {
          btn.className = 'cc-btn cc-btn-done cc-btn-xs cc-capital-task-btn';
          btn.innerHTML = '<i class="fa-solid fa-check"></i> Tasked';
        } else {
          btn.disabled = false;
        }
      });
    });
  }
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

  const dir = typeof a.director_count === 'number'
    ? a.director_count
    : (Array.isArray(a.personas) ? a.personas.filter(p => (p.tier || '').toLowerCase().includes('director')).length : 0);

  const mgr = typeof a.manager_count === 'number'
    ? a.manager_count
    : (Array.isArray(a.personas) ? a.personas.filter(p => (p.tier || '').toLowerCase().includes('manager')).length : 0);

  return { total, cSuite, vp, dir, mgr };
}

function coverageEntries(accounts) {
  const gaps = [];
  const isSingle = accounts.length === 1;

  for (const a of accounts) {
    const { total, cSuite, vp, dir, mgr } = getAccountCoverage(a);

    if (total === 0) {
      gaps.push({ account: a, total, cSuite, gapType: 'no-contacts', gapLabel: 'No contacts mapped' });
      continue;
    }

    if (isSingle) {
      // In single-account mode, surface specific actionable tier blindspots
      if (cSuite === 0) {
        gaps.push({ account: a, total, cSuite, gapType: 'no-csuite', gapLabel: 'No C-suite decision makers' });
      } else if (cSuite <= 2) {
        gaps.push({ account: a, total, cSuite, gapType: 'thin-csuite', gapLabel: `Thin C-suite (${cSuite} mapped)` });
      }

      if (vp === 0) {
        gaps.push({ account: a, total, cSuite, gapType: 'no-vp', gapLabel: 'No VP tier mapped' });
      } else if (vp <= 2) {
        gaps.push({ account: a, total, cSuite, gapType: 'thin-vp', gapLabel: `Thin VP tier (${vp} mapped)` });
      }

      if (dir === 0) {
        gaps.push({ account: a, total, cSuite, gapType: 'no-director', gapLabel: 'No Director-level champions' });
      }

      if (mgr === 0 && total < 10) {
        gaps.push({ account: a, total, cSuite, gapType: 'no-manager', gapLabel: 'No Manager-level operators' });
      }
    } else {
      // In global multi-account mode, highlight accounts with structural risk
      if (cSuite === 0) {
        gaps.push({ account: a, total, cSuite, gapType: 'no-csuite', gapLabel: 'No C-suite mapped' });
      } else if (vp === 0) {
        gaps.push({ account: a, total, cSuite, gapType: 'no-vp', gapLabel: 'No VP tier mapped' });
      } else if (vp <= 2) {
        gaps.push({ account: a, total, cSuite, gapType: 'thin-vp', gapLabel: `Thin VP tier (${vp} mapped)` });
      } else if (dir === 0) {
        gaps.push({ account: a, total, cSuite, gapType: 'no-director', gapLabel: 'No Director tier mapped' });
      }
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

// "By account": one bar per account (contacts mapped, C-suite), flagged where an open deal has nobody mapped
function renderCoverageByAccount(accounts) {
  const el = document.getElementById('ccCoverageAccounts');
  if (!el) return;
  const rows = accounts.map(a => ({ a, c: getAccountCoverage(a), p: dealsFor(a.id, a.name || a.display_name) }))
    .sort((x, y) => y.c.total - x.c.total);
  const max = Math.max(1, ...rows.map(r => r.c.total));
  const money = (v) => (v >= 1e6 ? `$${(v / 1e6).toFixed(2).replace(/\.?0+$/, '')}M` : `$${Math.round(v / 1e3)}k`);
  const ctx = dealContext();
  const gaps = ctx ? ctx.eb_gap_deals : 0;
  el.innerHTML = rows.map(({ a, c, p }) => {
    const name = a.name || a.display_name || a.key || 'Account';
    const blind = c.total === 0 && p && p.open_count > 0;
    const right = blind
      ? `0 mapped — ${money(p.open_value)} deal${p.open_count === 1 ? '' : 's'} open`
      : `${c.total.toLocaleString()} · ${c.cSuite} C-suite`;
    return `<button type="button" class="cc-cov-acct${blind ? ' is-blind' : ''}" data-account-id="${esc(a.id)}">
      <span class="cc-cov-acct-row"><strong>${esc(name)}</strong><span>${esc(right)}</span></span>
      <span class="cc-cov-acct-bar"><span style="width:${Math.max(c.total ? 2 : 0, (c.total / max) * 100)}%"></span></span></button>`;
  }).join('') + (gaps
    ? `<p class="cc-cov-acct-foot">${gaps} open deal${gaps === 1 ? ' has' : 's have'} no economic buyer · <a href="/introductions?new=1">Find who can introduce you</a></p>`
    : '');
  el.querySelectorAll('[data-account-id]').forEach(b => b.addEventListener('click', async () => {
    const accts = await loadMatrixAccounts().catch(() => []);
    const acct = accts.find(x => String(x.id) === b.dataset.accountId);
    if (acct) openDossier(acct);
  }));
}

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

      const accountsEl = document.getElementById('ccCoverageAccounts');
      if (accountsEl) accountsEl.style.setProperty('display', view === 'accounts' ? 'flex' : 'none', 'important');
      if (view === 'chart') {
        if (chartWrap) chartWrap.style.setProperty('display', 'flex', 'important');
        if (listEl) listEl.style.setProperty('display', 'none', 'important');
        if (coverageChartInstance) coverageChartInstance.resize();
      } else if (view === 'list') {
        if (chartWrap) chartWrap.style.setProperty('display', 'none', 'important');
        if (listEl) listEl.style.setProperty('display', 'block', 'important');
      } else {
        if (chartWrap) chartWrap.style.setProperty('display', 'none', 'important');
        if (listEl) listEl.style.setProperty('display', 'none', 'important');
      }
    };
  });
  // Show the selected view now (the stylesheet forces the chart visible until told otherwise)
  const active = toggleGroup.querySelector('[data-coverage-view].active');
  if (active) active.onclick();
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
      let rawName = a.display_name || a.name || a.key || `Account ${a.id}`;
      let cleanName = rawName
        .replace(/,?\s*(Inc\.?|LLC|Corporation|Corp\.?|Company|Co\.?|Group)\s*$/i, '')
        .trim();
      if (/depository\s*trust/i.test(cleanName)) cleanName = 'Depository Trust';
      else if (/bank\s*of\s*new\s*york/i.test(cleanName)) cleanName = 'BNY Mellon';
      else if (/vanguard/i.test(cleanName)) cleanName = 'Vanguard';
      else if (/blackrock/i.test(cleanName)) cleanName = 'BlackRock';
      else if (/northern\s*trust/i.test(cleanName)) cleanName = 'Northern Trust';

      const shortName = cleanName.length > 20 ? cleanName.substring(0, 18) + '…' : cleanName;
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

  // Inline Plugin: Render bold numbers directly in colored slice areas using dashboard Roboto font
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
        ctx.font = '700 11px Roboto, system-ui, -apple-system, sans-serif';
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

  // Inline Plugin: Bold total count in center cutout with dashboard-matching typography
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

      ctx.font = '700 17px Roboto, system-ui, -apple-system, sans-serif';
      ctx.fillStyle = dark ? '#F8FAFC' : '#0F172A';
      ctx.fillText(totalMapped.toLocaleString(), x, y - 5);

      ctx.font = '700 9px Roboto, system-ui, -apple-system, sans-serif';
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
            boxWidth: 12,
            boxHeight: 12,
            borderRadius: 2,
            padding: 9,
            font: {
              family: 'Roboto, system-ui, -apple-system, sans-serif',
              size: 12.5,
              weight: '600'
            },
            color: isDark ? '#F8FAFC' : '#0F172A',
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
          titleFont: { family: 'Roboto, system-ui, sans-serif', size: 12, weight: '700' },
          bodyFont: { family: 'Roboto, system-ui, sans-serif', size: 12, weight: '500' },
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
    ? 'Selected account has full C-suite, VP, and Director coverage mapped.'
    : 'All accounts have active leadership coverage mapped.';

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
    renderCoverageByAccount(filteredAccounts);
    setupCoverageViewToggle();
  } catch (err) {
    console.error('Error rendering coverage pie chart:', err);
  }
}



