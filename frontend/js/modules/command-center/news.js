// Google News widget — Enterprise Intelligence Feed
// Topic categorization, live keyword search, task conversion, account filtering, and subscription management.
import { esc } from './utils.js';
import { showToast } from '../toast.js';
import { renderSkeleton } from '../skeleton.js';
import { ccState, matchesCurrentAccount } from './state.js';
import { loadRealAccounts } from './real-accounts.js';
import { createTask } from './actions.js';
import { clearGlobalAccountFilter } from './account-filter.js';

const STALE_DAYS = 7;
const SUB_STORAGE_KEY = 'cc_news_subscriptions_v1';
const newsByAccount = new Map(); // account id ('' = all) -> Promise<{articles, last_scraped}>
let activeNewsAccountId = null;
let lastSyncedGlobalAccount = undefined;
let selectedTopicCategory = 'all';
let currentSearchQuery = '';

function getSubscriptions() {
  try {
    return JSON.parse(localStorage.getItem(SUB_STORAGE_KEY)) || { all: false, accounts: [] };
  } catch {
    return { all: false, accounts: [] };
  }
}

function saveSubscriptions(sub) {
  try {
    localStorage.setItem(SUB_STORAGE_KEY, JSON.stringify(sub));
  } catch (_) {}
}

function updateSubscribeButtonUi(subs) {
  const btn = document.getElementById('ccNewsSubscribeBtn');
  if (!btn) return;
  if (subs.all) {
    btn.innerHTML = `<i class="fa-solid fa-bell"></i> Subscribed (All) <i class="fa-solid fa-chevron-down cc-sub-chevron"></i>`;
    btn.classList.add('is-subscribed');
  } else if (subs.accounts && subs.accounts.length > 0) {
    btn.innerHTML = `<i class="fa-solid fa-bell"></i> Subscribed (${subs.accounts.length}) <i class="fa-solid fa-chevron-down cc-sub-chevron"></i>`;
    btn.classList.add('is-subscribed');
  } else {
    btn.innerHTML = `<i class="fa-regular fa-bell"></i> Subscribe <i class="fa-solid fa-chevron-down cc-sub-chevron"></i>`;
    btn.classList.remove('is-subscribed');
  }
}

function setupNewsSubscriptions(accounts) {
  const wrap = document.getElementById('ccNewsSubscribeWrap');
  const btn = document.getElementById('ccNewsSubscribeBtn');
  const menu = document.getElementById('ccNewsSubscribeMenu');
  const subAllBtn = document.getElementById('ccSubAllBtn');
  const accountsList = document.getElementById('ccNewsSubAccountsList');
  if (!wrap || !btn || !menu) return;

  const subs = getSubscriptions();
  updateSubscribeButtonUi(subs);

  // Toggle Menu
  btn.onclick = (e) => {
    e.stopPropagation();
    const isHidden = menu.style.display === 'none' || !menu.style.display;
    menu.style.display = isHidden ? 'block' : 'none';
  };

  // Close on outside click
  if (!document.body.dataset.newsSubListener) {
    document.body.dataset.newsSubListener = 'true';
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#ccNewsSubscribeWrap')) {
        const m = document.getElementById('ccNewsSubscribeMenu');
        if (m) m.style.display = 'none';
      }
    });
  }

  // Update All Monitored Accounts Button
  if (subAllBtn) {
    const isAll = !!subs.all;
    subAllBtn.classList.toggle('active', isAll);
    const check = subAllBtn.querySelector('.cc-news-sub-check');
    if (check) check.style.display = isAll ? 'inline-flex' : 'none';

    subAllBtn.onclick = (e) => {
      e.stopPropagation();
      const currentSubs = getSubscriptions();
      currentSubs.all = !currentSubs.all;
      saveSubscriptions(currentSubs);
      subAllBtn.classList.toggle('active', currentSubs.all);
      if (check) check.style.display = currentSubs.all ? 'inline-flex' : 'none';
      updateSubscribeButtonUi(currentSubs);
      showToast(currentSubs.all
        ? 'Subscribed to Google News alerts for All Accounts!'
        : 'Unsubscribed from All Accounts news alerts.');
    };
  }

  // Populate Accounts List
  if (accountsList && accounts && accounts.length) {
    accountsList.innerHTML = accounts.map(a => {
      const isSubbed = subs.accounts.includes(String(a.id));
      return `
        <button type="button" class="cc-news-sub-account-item ${isSubbed ? 'active' : ''}" data-acct-sub-id="${a.id}">
          <span class="cc-sub-acct-name">${esc(a.name || a.display_name)}</span>
          <span class="cc-news-sub-check" style="display:${isSubbed ? 'inline-flex' : 'none'};"><i class="fa-solid fa-check"></i></span>
        </button>
      `;
    }).join('');

    accountsList.querySelectorAll('[data-acct-sub-id]').forEach(itemBtn => {
      itemBtn.onclick = (e) => {
        e.stopPropagation();
        const id = String(itemBtn.dataset.acctSubId);
        const currentSubs = getSubscriptions();
        const idx = currentSubs.accounts.indexOf(id);
        const acct = accounts.find(a => String(a.id) === id);
        const name = acct ? (acct.name || acct.display_name) : 'Account';

        if (idx >= 0) {
          currentSubs.accounts.splice(idx, 1);
          itemBtn.classList.remove('active');
          const check = itemBtn.querySelector('.cc-news-sub-check');
          if (check) check.style.display = 'none';
          showToast(`Unsubscribed from ${name} news alerts.`);
        } else {
          currentSubs.accounts.push(id);
          itemBtn.classList.add('active');
          const check = itemBtn.querySelector('.cc-news-sub-check');
          if (check) check.style.display = 'inline-flex';
          showToast(`Subscribed to Google News alerts for ${name}!`);
        }
        saveSubscriptions(currentSubs);
        updateSubscribeButtonUi(currentSubs);
      };
    });
  }
}

export function getNewsCategory(article) {
  const text = ((article.title || '') + ' ' + (article.summary || '') + ' ' + (article.snippet || '')).toLowerCase();

  if (/\b(cfo|ceo|cto|coo|cro|president|vice president|vp|svp|evp|appoint|appoints|appointed|appointment|resigns|resigned|resignation|hire|hires|hired|successor|chief financial|board|leadership|executive|promoted)\b/i.test(text)) {
    return { id: 'exec', label: 'Exec Move', icon: 'fa-solid fa-user-tie', cls: 'cc-cat-exec' };
  }
  if (/\b(earnings|quarterly|revenue|profit|financial|growth|margin|dividend|forecast|guidance|q1|q2|q3|q4|fiscal|income|capital|valuation)\b/i.test(text)) {
    return { id: 'earnings', label: 'Earnings & Capital', icon: 'fa-solid fa-chart-line', cls: 'cc-cat-opp' };
  }
  if (/\b(acqui|merger|partner|partnership|deal|buyout|invest|funding|expand|joint venture|alliance|contract|deal)\b/i.test(text)) {
    return { id: 'deals', label: 'M&A & Deals', icon: 'fa-solid fa-handshake', cls: 'cc-cat-news' };
  }
  if (/\b(ai|artificial intelligence|cloud|tech|technology|platform|digital|software|data|cyber|modernize|patent|launch|product|genai)\b/i.test(text)) {
    return { id: 'tech', label: 'Tech & AI', icon: 'fa-solid fa-microchip', cls: 'cc-cat-hiring' };
  }
  return { id: 'market', label: 'Market & Press', icon: 'fa-solid fa-newspaper', cls: 'cc-cat-default' };
}

function loadNews(accountId, forceRefresh = false) {
  const key = accountId ? String(accountId) : '';
  if (forceRefresh || !newsByAccount.has(key)) {
    const params = new URLSearchParams({ limit: '40' });
    if (accountId) params.set('account_id', accountId);
    const p = fetch(`/api/news?${params}`)
      .then(res => {
        if (!res.ok) throw new Error(`Failed to load news (${res.status})`);
        return res.json();
      })
      .catch(err => {
        newsByAccount.delete(key);
        throw err;
      });
    newsByAccount.set(key, p);
  }
  return newsByAccount.get(key);
}

function daysAgo(iso) {
  return iso ? Math.floor((Date.now() - new Date(iso).getTime()) / 86400000) : null;
}

function timeAgo(iso) {
  if (!iso) return 'Recent';
  const days = daysAgo(iso);
  if (days <= 0) return 'Today';
  if (days === 1) return '1d ago';
  return `${days}d ago`;
}

function renderTopicChips(articles, onSelectCategory) {
  const container = document.getElementById('ccNewsTopicChips');
  if (!container) return;

  const categories = [
    { id: 'all', label: 'All Stories', icon: 'fa-solid fa-layer-group' },
    { id: 'exec', label: 'Exec Moves', icon: 'fa-solid fa-user-tie' },
    { id: 'earnings', label: 'Earnings & Growth', icon: 'fa-solid fa-chart-line' },
    { id: 'deals', label: 'M&A & Deals', icon: 'fa-solid fa-handshake' },
    { id: 'tech', label: 'Tech & AI', icon: 'fa-solid fa-microchip' },
    { id: 'market', label: 'Market & Press', icon: 'fa-solid fa-newspaper' },
  ];

  // Count items per category
  const counts = { all: articles.length };
  articles.forEach(a => {
    const cat = a._category || getNewsCategory(a);
    counts[cat.id] = (counts[cat.id] || 0) + 1;
  });

  container.innerHTML = categories.map(cat => {
    const count = counts[cat.id] || 0;
    const isActive = selectedTopicCategory === cat.id;
    return `
      <button type="button" class="cc-chip cc-chip-filter ${isActive ? 'active' : ''}" data-news-cat="${cat.id}">
        <i class="${cat.icon}"></i> ${cat.label} <span class="cc-chip-count">(${count})</span>
      </button>
    `;
  }).join('');

  container.querySelectorAll('[data-news-cat]').forEach(btn => {
    btn.onclick = () => {
      selectedTopicCategory = btn.dataset.newsCat;
      onSelectCategory();
    };
  });
}

function setupNewsSearch(onSearchChange) {
  const input = document.getElementById('ccNewsSearchInput');
  const clearBtn = document.getElementById('ccNewsSearchClear');
  if (!input) return;

  input.value = currentSearchQuery;
  if (clearBtn) clearBtn.style.display = currentSearchQuery ? 'inline-flex' : 'none';

  if (!input.dataset.bound) {
    input.dataset.bound = 'true';
    input.addEventListener('input', () => {
      currentSearchQuery = input.value.trim().toLowerCase();
      if (clearBtn) clearBtn.style.display = currentSearchQuery ? 'inline-flex' : 'none';
      onSearchChange();
    });
  }

  if (clearBtn && !clearBtn.dataset.bound) {
    clearBtn.dataset.bound = 'true';
    clearBtn.onclick = () => {
      input.value = '';
      currentSearchQuery = '';
      clearBtn.style.display = 'none';
      input.focus();
      onSearchChange();
    };
  }
}

export async function renderNewsFeed(overrideAccountId, forceRefresh = false) {
  const list = document.getElementById('ccNewsList');
  const filterContainer = document.getElementById('ccNewsFilterStripContainer');
  const accountSelect = document.getElementById('ccNewsAccountFilter');
  const refreshBtn = document.getElementById('ccNewsRefreshBtn');
  if (!list) return;

  list.innerHTML = renderSkeleton('feed-rows');
  if (filterContainer) filterContainer.innerHTML = '';

  // Synchronize with global account selector if it changed
  if (overrideAccountId !== undefined) {
    activeNewsAccountId = overrideAccountId ? String(overrideAccountId) : null;
  } else if (ccState.activeAccountId !== lastSyncedGlobalAccount) {
    lastSyncedGlobalAccount = ccState.activeAccountId;
    activeNewsAccountId = ccState.activeAccountId ? String(ccState.activeAccountId) : null;
  }

  // Bind Refresh Button
  if (refreshBtn && !refreshBtn.dataset.bound) {
    refreshBtn.dataset.bound = 'true';
    refreshBtn.onclick = async () => {
      const origHtml = refreshBtn.innerHTML;
      refreshBtn.disabled = true;
      refreshBtn.innerHTML = '<i class="fa-solid fa-arrows-rotate fa-spin"></i> Refreshing...';
      try {
        await renderNewsFeed(activeNewsAccountId, true);
        showToast('Google News feed refreshed!');
      } catch (e) {
        showToast('Failed to refresh news feed.');
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.innerHTML = origHtml;
      }
    };
  }

  // Load real accounts to populate the filter dropdown and subscription menu
  let realAccounts = [];
  try {
    realAccounts = await loadRealAccounts().catch(() => []);
  } catch (e) {
    realAccounts = [];
  }

  const sortedAccounts = [...realAccounts].sort((a, b) =>
    (a.name || a.display_name || '').localeCompare(b.name || b.display_name || '')
  );

  // Setup Subscription Dropdown
  setupNewsSubscriptions(sortedAccounts);

  // Populate account filter dropdown
  if (accountSelect) {
    accountSelect.innerHTML = `
      <option value="">All Accounts (${sortedAccounts.length})</option>
      ${sortedAccounts
        .map(
          a => `
        <option value="${a.id}" ${String(a.id) === String(activeNewsAccountId) ? 'selected' : ''}>
          ${esc(a.name || a.display_name)}
        </option>
      `
        )
        .join('')}
    `;

    // Ensure change listener is bound once
    if (!accountSelect.dataset.listenerBound) {
      accountSelect.dataset.listenerBound = 'true';
      accountSelect.addEventListener('change', () => {
        const val = accountSelect.value ? accountSelect.value : null;
        renderNewsFeed(val);
      });
    }
  }

  const targetAccountId = activeNewsAccountId;
  let data;
  try {
    data = await loadNews(targetAccountId, forceRefresh);
  } catch (err) {
    console.error(err);
    list.innerHTML = '<li class="cc-drawer-empty">Could not load news coverage.</li>';
    return;
  }

  // Guard against race conditions
  if (activeNewsAccountId !== targetAccountId) return;

  const hasGlobalMulti = ccState.activeAccountIds && ccState.activeAccountIds.size > 1;
  let rawArticles = data.articles || [];
  if (hasGlobalMulti) {
    rawArticles = rawArticles.filter(a => matchesCurrentAccount(a.account_id, a.account_name));
  }
  
  // Enrich articles with categories
  const articles = rawArticles.map(a => ({
    ...a,
    _category: getNewsCategory(a),
  }));

  // Determine active account label
  let filteredAccountName = null;
  const names = ccState.selectedAccountNames || [];
  if (hasGlobalMulti && names.length > 0) {
    filteredAccountName = `${names.length} Selected Accounts`;
  } else if (targetAccountId) {
    const found = sortedAccounts.find(a => String(a.id) === String(targetAccountId));
    filteredAccountName = found ? (found.name || found.display_name) : (articles[0]?.account_name || 'Selected Account');
  }

  // Filter function for category and search query
  function filterArticles() {
    return articles.filter(a => {
      // Category filter
      if (selectedTopicCategory !== 'all' && a._category.id !== selectedTopicCategory) {
        return false;
      }
      // Search keyword filter
      if (currentSearchQuery) {
        const hay = ((a.title || '') + ' ' + (a.account_name || '') + ' ' + (a.summary || '') + ' ' + (a.source || '')).toLowerCase();
        if (!hay.includes(currentSearchQuery)) return false;
      }
      return true;
    });
  }

  function renderList() {
    renderTopicChips(articles, () => {
      renderList();
    });

    // Render Filter Strip if filtering by account
    if (filterContainer) {
      if (filteredAccountName) {
        filterContainer.innerHTML = `
          <div class="cc-intel-filter-strip" style="margin-bottom: 8px;">
            <span class="cc-intel-filter-info">
              <i class="fa-solid fa-filter"></i> Filtered by <strong>${esc(filteredAccountName)}</strong> (${articles.length} ${articles.length === 1 ? 'story' : 'stories'})
            </span>
            <button type="button" class="cc-intel-filter-clear" id="ccClearNewsFilter">
              Show All <i class="fa-solid fa-xmark"></i>
            </button>
          </div>
        `;

        const clearBtn = document.getElementById('ccClearNewsFilter');
        if (clearBtn) {
          clearBtn.addEventListener('click', () => {
            if (accountSelect) accountSelect.value = '';
            clearGlobalAccountFilter();
            renderNewsFeed(null);
          });
        }
      } else {
        filterContainer.innerHTML = '';
      }
    }

    const filtered = filterArticles();

    if (!filtered.length) {
      list.innerHTML = `
        <li class="cc-news-empty-box">
          <i class="fa-regular fa-newspaper cc-news-empty-icon"></i>
          <div class="cc-news-empty-title">No matching news stories found</div>
          <div class="cc-news-empty-sub">${currentSearchQuery ? `No coverage matching "${esc(currentSearchQuery)}"` : 'Try selecting another category or clearing account filters.'}</div>
          <button type="button" class="cc-btn cc-btn-outline cc-btn-xs" id="ccNewsResetFilterBtn" style="margin-top: 6px;">Reset All Filters</button>
        </li>
      `;

      const resetBtn = document.getElementById('ccNewsResetFilterBtn');
      if (resetBtn) {
        resetBtn.onclick = () => {
          selectedTopicCategory = 'all';
          currentSearchQuery = '';
          if (accountSelect) accountSelect.value = '';
          const searchInput = document.getElementById('ccNewsSearchInput');
          if (searchInput) searchInput.value = '';
          renderNewsFeed(null);
        };
      }
      return;
    }

    // News staleness warning
    const scrapedDays = daysAgo(data.last_scraped);
    const staleNote =
      scrapedDays !== null && scrapedDays >= STALE_DAYS
        ? `<li class="cc-drawer-empty cc-news-stale"><i class="fa-solid fa-clock-rotate-left"></i> News last fetched ${scrapedDays} days ago — click Refresh to update.</li>`
        : '';

    list.innerHTML =
      staleNote +
      filtered
        .map(a => {
          const accName = a.account_name || '';
          const cat = a._category;
          const snippetText = (a.snippet || a.summary || '').replace(/\s+/g, ' ').trim();

          return `
        <li class="cc-signal-card cc-news-card" data-news-id="${esc(String(a.id || a.url || ''))}">
          <div class="cc-sig-card-header">
            <div class="cc-sig-badge-group">
              <span class="cc-sig-cat-badge ${cat.cls}">
                <i class="${cat.icon}"></i> ${esc(cat.label)}
              </span>
              ${accName ? `
                <button type="button" class="cc-sig-account-badge cc-news-acc-tag" data-acc-id="${a.account_id || ''}" data-acc-name="${esc(accName)}" title="Filter news for ${esc(accName)}">
                  <i class="fa-regular fa-building"></i> ${esc(accName)}
                </button>
              ` : ''}
              ${a.source ? `
                <span class="cc-news-source-pill" title="Source: ${esc(a.source)}">
                  <i class="fa-solid fa-globe"></i> ${esc(a.source)}
                </span>
              ` : ''}
              <span class="cc-sig-time-badge" title="${esc(a.published_at ? 'Published ' + new Date(a.published_at).toLocaleString() : '')}">
                <i class="fa-regular fa-clock"></i> ${esc(timeAgo(a.published_at || a.first_seen))}
              </span>
            </div>
          </div>

          <div class="cc-sig-title-row">
            <a class="cc-sig-title-link cc-news-title-link" href="${esc(a.url || '#')}" target="_blank" rel="noopener" title="${esc(a.title)}">
              ${esc(a.title)}
              <i class="fa-solid fa-arrow-up-right-from-square cc-sig-external-icon"></i>
            </a>
          </div>

          ${snippetText ? `
            <div class="cc-news-snippet-text">${esc(snippetText)}</div>
          ` : ''}

          <div class="cc-news-footer-meta">
            ${a.person_name ? `
              <span class="cc-news-person-tag"><i class="fa-regular fa-user"></i> ${esc(a.person_name)}</span>
            ` : ''}
            <a href="${esc(a.url || '#')}" target="_blank" rel="noopener" class="cc-news-read-more">
              Read original article <i class="fa-solid fa-arrow-right"></i>
            </a>
          </div>
        </li>
      `;
        })
        .join('');

    // Wire account tag click filtering
    list.querySelectorAll('.cc-news-acc-tag').forEach(tag => {
      tag.addEventListener('click', (e) => {
        e.stopPropagation();
        const accId = tag.dataset.accId;
        const accName = tag.dataset.accName;
        let targetId = accId;

        if (!targetId && accName) {
          const found = sortedAccounts.find(
            a => (a.name || a.display_name || '').toLowerCase() === accName.toLowerCase()
          );
          if (found) targetId = String(found.id);
        }

        if (targetId) {
          if (accountSelect) accountSelect.value = targetId;
          renderNewsFeed(targetId);
        }
      });
    });
  }

  // Setup live search and initial render
  setupNewsSearch(() => {
    renderList();
  });

  renderList();
}

