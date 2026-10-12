import { getCommandCenter, isGenerated } from './generator.js';
import { esc, relativeTime, ageInDays } from './utils.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { createTask } from './actions.js';
import { clearGlobalAccountFilter } from './account-filter.js';

function matchesAccount(sig) {
  return matchesCurrentAccount(sig.account_id, sig.account_name);
}

export function getCategoryMeta(sig) {
  const kind = (sig.kind || '').toLowerCase();
  const cat = (sig.category || '').toLowerCase();
  const title = (sig.title || '').toLowerCase();
  const summary = (sig.summary || '').toLowerCase();

  // 1. Hiring & Talent Signals (Catches campus recruitment, interviews, internships, career announcements, job postings)
  if (
    kind === 'hiring' ||
    cat.includes('hiring') ||
    /\b(hiring|hires|hire|recruit|recruitment|recruiting|interview|internship|internships|careers|career|job postings|open roles|headcount|talent)\b/i.test(title) ||
    /\b(hiring push|campus recruitment|internship offer|open roles|new roles posted)\b/i.test(summary)
  ) {
    return { icon: 'fa-solid fa-briefcase', label: 'Hiring Signal', cls: 'cc-cat-hiring' };
  }

  // 2. Executive Move & Leadership (Catches executive hires, CFO/CEO searches, officer transitions)
  if (
    kind === 'exec' ||
    cat.includes('exec') ||
    cat.includes('leadership') ||
    cat.includes('cxo') ||
    /\b(cfo|ceo|cto|coo|cro|president|vice president|vp|svp|evp|appoint|appointed|appointment|resigned|resignation|successor|succeed|chief financial|officer transition|executive hire|executive transition|new leader)\b/i.test(title) ||
    /\b(new executive hire|chief financial officer|joined as|promoted to)\b/i.test(summary)
  ) {
    return { icon: 'fa-solid fa-user-tie', label: 'Executive Move', cls: 'cc-cat-exec' };
  }

  // 3. Company News & External Media
  if (kind === 'news' || cat.includes('news')) {
    return { icon: 'fa-solid fa-newspaper', label: 'Company News', cls: 'cc-cat-news' };
  }

  // 4. Growth & Strategic Opportunity (Themes, initiatives, partnerships)
  return { icon: 'fa-solid fa-chart-line', label: 'Growth Signal', cls: 'cc-cat-opp' };
}

function visibleSignals() {
  return getCommandCenter().signals
    .map(s => ({ ...s, meta: getCategoryMeta(s) }))
    .filter(s => {
      if (ccState.activeDomainFilters.size === 0) return true;
      return ccState.activeDomainFilters.has(s.meta.label) || ccState.activeDomainFilters.has(s.category);
    })
    .filter(matchesAccount)
    .sort((a, b) => b.score - a.score);
}

function renderFilterChips() {
  const wrap = document.getElementById('ccFeedFilters');
  if (!wrap) return;
  const categories = ['Executive Move', 'Hiring Signal', 'Company News', 'Growth Signal'];
  wrap.innerHTML = categories.map(d => {
    const active = ccState.activeDomainFilters.has(d);
    return `<button type="button" class="cc-chip cc-chip-filter ${active ? 'active' : ''}" data-domain="${esc(d)}">${esc(d)}</button>`;
  }).join('');
  wrap.querySelectorAll('[data-domain]').forEach(btn => {
    btn.addEventListener('click', () => {
      const d = btn.dataset.domain;
      if (ccState.activeDomainFilters.has(d)) ccState.activeDomainFilters.delete(d);
      else ccState.activeDomainFilters.add(d);
      renderFeed();
    });
  });
}

function renderAccountFilterPill() {
  const wrap = document.getElementById('ccFeedAccountFilter');
  if (!wrap) return;
  const count = ccState.activeAccountIds ? ccState.activeAccountIds.size : (ccState.activeAccountId ? 1 : 0);
  if (count === 0) { wrap.innerHTML = ''; return; }
  const label = count === 1
    ? (ccState.selectedAccountName || 'Account')
    : `${count} Accounts`;
  wrap.innerHTML = `<button type="button" class="cc-chip cc-chip-brand cc-chip-removable" id="ccClearAccountFilter">Filtered: ${esc(label)} <i class="fa-solid fa-xmark"></i></button>`;
  const clearBtn = document.getElementById('ccClearAccountFilter');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      clearGlobalAccountFilter();
    });
  }
}

function rowHtml(sig) {
  const isNew = sig.is_new != null
    ? Boolean(sig.is_new)
    : (sig.detectedAt ? ageInDays(sig.detectedAt) <= 4 : false);
  const newBadge = isNew
    ? `<span class="cc-sig-new-badge"><span class="cc-pulse-dot"></span>NEW</span>`
    : '';
  
  const cat = sig.meta || getCategoryMeta(sig);

  const titleHtml = sig.url
    ? `<a class="cc-sig-title-link" href="${esc(sig.url)}" target="_blank" rel="noopener" title="${esc(sig.title)}">${esc(sig.title)} <i class="fa-solid fa-arrow-up-right-from-square cc-sig-external-icon"></i></a>`
    : `<span class="cc-sig-title-text" title="${esc(sig.title)}">${esc(sig.title)}</span>`;

  return `
    <li class="cc-signal-card ${isNew ? 'is-new' : ''}" data-signal-id="${esc(sig.id)}">
      <div class="cc-sig-card-header">
        <div class="cc-sig-badge-group">
          <span class="cc-sig-cat-badge ${cat.cls}">
            <i class="${cat.icon}"></i> ${esc(cat.label)}
          </span>
          <span class="cc-sig-account-badge" title="${esc(sig.account_name)}">
            <i class="fa-regular fa-building"></i> ${esc(sig.account_name)}
          </span>
          <span class="cc-sig-time-badge ${isNew ? 'is-recent' : ''}">
            <i class="fa-regular fa-clock"></i> ${esc(relativeTime(sig.detectedAt))}
          </span>
          ${newBadge}
        </div>
        <div class="cc-sig-action-wrap">
          <button type="button" class="cc-btn cc-btn-primary cc-btn-xs cc-sig-task-btn" data-act="task" title="Create assigned task for ${esc(sig.account_name)}">
            <i class="fa-solid fa-plus"></i> Create task
          </button>
        </div>
      </div>

      <div class="cc-sig-title-row">
        ${titleHtml}
      </div>

      ${sig.summary ? `
      <div class="cc-sig-summary-block" role="button" tabindex="0" title="Click to view complete text">
        <div class="cc-sig-summary-text">${esc(sig.summary)}</div>
        <div class="cc-sig-expand-toggle">
          <span class="cc-sig-toggle-label"><i class="fa-solid fa-chevron-down"></i> Expand full text</span>
        </div>
      </div>` : ''}
    </li>`;
}

export function renderFeed() {
  renderFilterChips();
  renderAccountFilterPill();
  const list = document.getElementById('ccFeedList');
  if (!list) return;
  if (!isGenerated()) {
    list.innerHTML = '<li class="cc-feed-empty">Not generated yet — press <strong>Generate</strong> to build the feed from exec movements, hiring, news and opportunity signals.</li>';
    return;
  }
  const { signals } = getCommandCenter();
  if (!signals.length) {
    list.innerHTML = '<li class="cc-feed-empty">No signals in the last 7 days. Run the pipeline or content refresh for your accounts, then Generate again.</li>';
    return;
  }
  const rows = visibleSignals();
  if (!rows.length) {
    list.innerHTML = '<li class="cc-feed-empty">No signals match the current filters.</li>';
    return;
  }
  list.innerHTML = rows.map(rowHtml).join('');
  list.querySelectorAll('.cc-signal-card').forEach(row => {
    const sig = signals.find(s => s.id === row.dataset.signalId);
    if (!sig) return;

    // Click to expand / collapse full text
    const summaryBlock = row.querySelector('.cc-sig-summary-block');
    if (summaryBlock) {
      const toggleExpand = (e) => {
        if (e.target.closest('button') || e.target.closest('a')) return;
        const isExp = summaryBlock.classList.toggle('is-expanded');
        const label = summaryBlock.querySelector('.cc-sig-toggle-label');
        if (label) {
          label.innerHTML = isExp
            ? '<i class="fa-solid fa-chevron-up"></i> Show less'
            : '<i class="fa-solid fa-chevron-down"></i> Expand full text';
        }
      };

      summaryBlock.addEventListener('click', toggleExpand);
      summaryBlock.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggleExpand(e);
        }
      });
    }

    const btn = row.querySelector('[data-act="task"]');
    if (!btn) return;
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      btn.disabled = true;
      const origHtml = btn.innerHTML;
      btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Creating...';
      try {
        const ok = await createTask(sig.account_name, sig.title, {
          accountId: sig.account_id,
          description: sig.summary,
          priority: 'high',
          source: 'signal_feed',
        });
        if (ok) {
          btn.innerHTML = '<i class="fa-solid fa-check"></i> Tasked';
          btn.classList.remove('cc-btn-primary');
          btn.classList.add('cc-btn-done');
        } else {
          btn.innerHTML = origHtml;
          btn.disabled = false;
        }
      } catch {
        btn.innerHTML = origHtml;
        btn.disabled = false;
      }
    });
  });
}
