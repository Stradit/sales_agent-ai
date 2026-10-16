import { formatMoney } from './utils.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { openDossier } from './drawer.js';
import { loadMatrixAccounts } from './real-accounts.js';
import { renderSkeleton } from '../skeleton.js';

// Chart.js's own default font ('Helvetica Neue'/Arial) doesn't follow this
// app's CSS at all — canvas text isn't affected by page font-family — so
// without this the priority matrix would keep rendering in a different
// typeface than everything else after the Roboto switch. Set once, module-
// load time, guarded the same way renderMatrix() itself guards Chart's
// presence (the CDN <script> may not be loaded on every page that imports
// this module transitively).
if (typeof Chart !== 'undefined') {
  Chart.defaults.font.family = "Roboto, system-ui, -apple-system, sans-serif";
}

const RADIUS_MIN = 12, RADIUS_MAX = 38;

function radiusFor(dealPotential, dealMin, dealMax) {
  const t = (dealPotential - dealMin) / (dealMax - dealMin || 1);
  return RADIUS_MIN + t * (RADIUS_MAX - RADIUS_MIN);
}

// 4-Tier Vibrant Heatmap Spectrum
function colorFor(score) {
  if (score >= 80) return 'rgba(239, 68, 68, 0.92)';   // High Heat Crimson/Coral
  if (score >= 68) return 'rgba(249, 115, 22, 0.92)';  // Flame Orange
  if (score >= 50) return 'rgba(245, 158, 11, 0.90)';  // Active Amber/Gold
  return 'rgba(14, 165, 233, 0.88)';                   // Cool Blue
}

function borderFor(score) {
  if (score >= 80) return 'rgba(254, 202, 202, 0.95)';
  if (score >= 68) return 'rgba(254, 215, 170, 0.95)';
  if (score >= 50) return 'rgba(254, 240, 138, 0.95)';
  return 'rgba(186, 230, 253, 0.95)';
}

const quadrantPlugin = {
  id: 'ccQuadrants',
  beforeDatasetsDraw(chart) {
    const { ctx, chartArea, scales } = chart;
    if (!chartArea) return;
    const xMid = scales.x.getPixelForValue(50);
    const yMid = scales.y.getPixelForValue(50);

    ctx.save();

    // 1. Quadrant Fills (Heatmap Backdrop)
    // Top-Right: Act Now (High Heat Red/Amber gradient)
    const actNowGrad = ctx.createLinearGradient(xMid, yMid, chartArea.right, chartArea.top);
    actNowGrad.addColorStop(0, 'rgba(239, 68, 68, 0.04)');
    actNowGrad.addColorStop(1, 'rgba(239, 68, 68, 0.15)');
    ctx.fillStyle = actNowGrad;
    ctx.fillRect(xMid, chartArea.top, chartArea.right - xMid, yMid - chartArea.top);

    // Top-Left: Nurture (Teal / Emerald)
    ctx.fillStyle = 'rgba(16, 185, 129, 0.04)';
    ctx.fillRect(chartArea.left, chartArea.top, xMid - chartArea.left, yMid - chartArea.top);

    // Bottom-Right: Re-engage (Indigo / Violet)
    ctx.fillStyle = 'rgba(228, 101, 33, 0.04)';
    ctx.fillRect(xMid, yMid, chartArea.right - xMid, chartArea.bottom - yMid);

    // Bottom-Left: Watch (Slate)
    ctx.fillStyle = 'rgba(148, 163, 184, 0.03)';
    ctx.fillRect(chartArea.left, yMid, xMid - chartArea.left, chartArea.bottom - yMid);

    // 2. Center Dividing Gridlines
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(xMid, chartArea.top);
    ctx.lineTo(xMid, chartArea.bottom);
    ctx.moveTo(chartArea.left, yMid);
    ctx.lineTo(chartArea.right, yMid);
    ctx.stroke();

    // Highlight "Act Now" border
    ctx.strokeStyle = 'rgba(239, 68, 68, 0.45)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(xMid, chartArea.top, chartArea.right - xMid, yMid - chartArea.top);

    // 3. Quadrant Title Badges
    const drawBadge = (text, x, y, color, bg) => {
      ctx.font = '700 10px Inter, system-ui, sans-serif';
      const textWidth = ctx.measureText(text).width;
      ctx.fillStyle = bg;
      ctx.fillRect(x, y - 11, textWidth + 8, 15);
      ctx.fillStyle = color;
      ctx.fillText(text, x + 4, y);
    };

    drawBadge('🔥 ACT NOW (HIGH HEAT)', xMid + 10, chartArea.top + 18, '#dc2626', 'rgba(239, 68, 68, 0.14)');
    drawBadge('🌱 NURTURE', chartArea.left + 10, chartArea.top + 18, '#059669', 'rgba(16, 185, 129, 0.12)');
    drawBadge('⚡ RE-ENGAGE', xMid + 10, chartArea.bottom - 10, '#C9541A', 'rgba(228, 101, 33, 0.12)');
    drawBadge('👁️ WATCH', chartArea.left + 10, chartArea.bottom - 10, '#64748b', 'rgba(148, 163, 184, 0.12)');

    ctx.restore();
  },
  afterDatasetsDraw(chart) {
    const { ctx } = chart;
    const meta = chart.getDatasetMeta(0);
    if (!meta || !meta.data) return;

    ctx.save();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    meta.data.forEach((element) => {
      const { x, y } = element.getProps(['x', 'y'], true);
      const raw = element.$context?.raw?.account;
      if (!raw) return;

      const label = raw.ticker || (raw.name || '').split(/,|\s-\s|\s/)[0].slice(0, 5).toUpperCase();
      
      ctx.font = '700 9.5px Inter, system-ui, sans-serif';
      ctx.fillStyle = '#ffffff';
      ctx.shadowColor = 'rgba(0, 0, 0, 0.65)';
      ctx.shadowBlur = 3;
      ctx.fillText(label, x, y);
    });

    ctx.restore();
  }
};

export async function renderMatrix() {
  const canvas = document.getElementById('ccMatrixCanvas');
  const wrap = canvas ? canvas.closest('.cc-matrix-canvas-wrap') : null;
  if (!canvas || typeof Chart === 'undefined') return;

  if (wrap) wrap.innerHTML = renderSkeleton('chart');

  let accounts;
  try {
    accounts = await loadMatrixAccounts();
  } catch (err) {
    console.error(err);
    if (wrap) wrap.innerHTML = '<div class="cc-drawer-empty">Could not load accounts.</div>';
    return;
  }

  if (!accounts.length) {
    if (wrap) wrap.innerHTML = '<div class="cc-drawer-empty">No accounts assigned to you yet — ask an admin to grant you account access.</div>';
    return;
  }

  // The skeleton above replaced <canvas id="ccMatrixCanvas"> inside `wrap`
  // with a placeholder div — put a fresh canvas back before drawing into it,
  // since the original `canvas` reference is now a detached (invisible) node.
  if (wrap) wrap.innerHTML = '<canvas id="ccMatrixCanvas"></canvas>';
  const liveCanvas = wrap ? document.getElementById('ccMatrixCanvas') : canvas;

  const dealMin = Math.min(...accounts.map(a => a.dealPotential));
  const dealMax = Math.max(...accounts.map(a => a.dealPotential));
  const data = accounts.map(a => ({
    x: a.signalStrength,
    y: a.engagementRecency,
    r: radiusFor(a.dealPotential, dealMin, dealMax),
    account: a,
  }));

  if (ccState.matrixChart) {
    ccState.matrixChart.destroy();
  }

  const hasFilter = (ccState.activeAccountIds && ccState.activeAccountIds.size > 0) || ccState.activeAccountId;

  ccState.matrixChart = new Chart(liveCanvas.getContext('2d'), {
    type: 'bubble',
    data: {
      datasets: [{
        data,
        backgroundColor: data.map(d => {
          const isMatch = !hasFilter || matchesCurrentAccount(d.account.id, d.account.name);
          return isMatch ? colorFor(d.account.compositeScore) : 'rgba(148, 163, 184, 0.14)';
        }),
        borderColor: data.map(d => {
          const isMatch = !hasFilter || matchesCurrentAccount(d.account.id, d.account.name);
          return isMatch ? borderFor(d.account.compositeScore) : 'rgba(148, 163, 184, 0.2)';
        }),
        borderWidth: data.map(d => {
          const isMatch = !hasFilter || matchesCurrentAccount(d.account.id, d.account.name);
          return isMatch ? (hasFilter ? 3 : 2) : 1;
        }),
        hoverBorderColor: '#0f172a',
        hoverBorderWidth: 2.5,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: 8 },
      scales: {
        x: {
          min: 0, max: 100,
          title: { display: true, text: 'Buying signal strength (0-100)', font: { size: 11, weight: '600' } },
          grid: { color: 'rgba(148,163,184,0.12)' },
          ticks: { font: { size: 10 } },
        },
        y: {
          min: 0, max: 100,
          title: { display: true, text: 'Engagement recency (0-100)', font: { size: 11, weight: '600' } },
          grid: { color: 'rgba(148,163,184,0.12)' },
          ticks: { font: { size: 10 } },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(15, 23, 42, 0.95)',
          padding: 12,
          cornerRadius: 8,
          titleFont: { size: 12, weight: '700' },
          bodyFont: { size: 11 },
          callbacks: {
            title: (items) => items[0].raw.account.name,
            label: (item) => {
              const a = item.raw.account;
              return [
                `Composite Heat Score: ${a.compositeScore} / 100`,
                `Signal Strength: ${a.signalStrength} / 100`,
                `Engagement Recency: ${a.engagementRecency} / 100`,
                `Deal Potential (est.): ${formatMoney(a.dealPotential)}`,
              ];
            },
          },
        },
      },
      onClick: (evt, elements) => {
        if (!elements.length) return;
        const point = data[elements[0].index];
        openDossier(point.account);
      },
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length ? 'pointer' : 'default';
      },
    },
    plugins: [quadrantPlugin],
  });
}
