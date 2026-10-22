/**
 * Page Lineage & Navigation Tracking Module
 * Tracks navigation history across browser tabs and sessions so back buttons
 * always return users to their previous page (e.g. /tasks, /deals, /command-center)
 * rather than hard-redirecting to the main dashboard.
 */

const LINEAGE_KEY = 'doshin_page_lineage';
const LAST_PAGE_KEY = 'doshin_last_page';
const MAX_DEPTH = 15;

/**
 * Returns a clean path + query + hash for navigation tracking
 */
export function getCurrentPath() {
  if (typeof window === 'undefined') return '/';
  return window.location.pathname + window.location.search + window.location.hash;
}

/**
 * Friendly label for known routes
 */
export function getPageLabel(url) {
  if (!url) return null;
  const path = url.split('?')[0].toLowerCase();
  if (path.includes('/tasks')) return 'Tasks';
  if (path.includes('/deals')) return 'Deals Pipeline';
  if (path.includes('/command-center')) return 'Command Center';
  if (path.includes('/copilot-pipeline')) return 'Copilot Pipeline';
  if (path.includes('/copilot')) return 'Sales Copilot';
  if (path.includes('/introductions')) return 'Introductions';
  if (path.includes('/forecast')) return 'Forecast';
  if (path.includes('/email-sync')) return 'Email Sync';
  if (path.includes('/capture')) return 'Capture';
  if (path.includes('/admin')) return 'Admin';
  if (path === '/' || path.includes('/accounts') || path.includes('/index')) return 'Accounts';
  return null;
}

/**
 * Records the current page visit into lineage history
 */
export function recordCurrentPage() {
  if (typeof window === 'undefined') return;
  const current = getCurrentPath();
  const path = window.location.pathname.toLowerCase();

  // Don't record authentication or error routes
  if (path.includes('/login') || path.includes('/signup') || path.includes('/forgot') || path.includes('/reset')) {
    return;
  }

  try {
    let stack = [];
    try {
      stack = JSON.parse(sessionStorage.getItem(LINEAGE_KEY) || '[]');
      if (!stack.length) {
        stack = JSON.parse(localStorage.getItem(LINEAGE_KEY) || '[]');
      }
    } catch (_) { stack = []; }

    // If current is identical to top of stack, do nothing
    if (stack.length && stack[stack.length - 1] === current) {
      return;
    }

    // If current was visited earlier in the stack (e.g. user navigated back to an earlier page),
    // prune everything after that point so history doesn't create cycles.
    const existingIndex = stack.lastIndexOf(current);
    if (existingIndex !== -1) {
      stack = stack.slice(0, existingIndex + 1);
    } else {
      const last = stack.length ? stack[stack.length - 1] : null;
      if (last && last !== current) {
        sessionStorage.setItem(LAST_PAGE_KEY, last);
        localStorage.setItem(LAST_PAGE_KEY, last);
      }
      stack.push(current);
    }

    if (stack.length > MAX_DEPTH) stack = stack.slice(-MAX_DEPTH);
    sessionStorage.setItem(LINEAGE_KEY, JSON.stringify(stack));
    localStorage.setItem(LINEAGE_KEY, JSON.stringify(stack));
  } catch (_) {}
}

/**
 * Determines the last page the user was on
 */
export function getLastPage(fallbackUrl = '/') {
  if (typeof window === 'undefined') return fallbackUrl;
  const current = getCurrentPath();
  const currentPathname = window.location.pathname.toLowerCase();

  // Helper: check if a target candidate is valid (not current, not login)
  const isValidTarget = (candidate) => {
    if (!candidate || typeof candidate !== 'string' || !candidate.startsWith('/')) return false;
    if (candidate === current) return false;
    const candPath = candidate.split('?')[0].toLowerCase();
    if (candPath.includes('/login') || candPath.includes('/signup')) return false;
    // When on profile page, don't redirect back to profile itself
    if (currentPathname.includes('/profile') && candPath.includes('/profile')) return false;
    return true;
  };

  // 1. Explicit 'from' or 'return_to' URL query parameter
  try {
    const params = new URLSearchParams(window.location.search);
    const from = params.get('from') || params.get('return_to');
    if (isValidTarget(from)) {
      return from;
    }
  } catch (_) {}

  // 2. Same-origin document.referrer
  try {
    if (document.referrer) {
      const refUrl = new URL(document.referrer);
      if (refUrl.origin === window.location.origin) {
        const refPath = refUrl.pathname + refUrl.search + refUrl.hash;
        if (isValidTarget(refPath)) {
          return refPath;
        }
      }
    }
  } catch (_) {}

  // 3. Stored lineage history stack
  try {
    let stack = [];
    try {
      stack = JSON.parse(sessionStorage.getItem(LINEAGE_KEY) || '[]');
      if (!stack.length) stack = JSON.parse(localStorage.getItem(LINEAGE_KEY) || '[]');
    } catch (_) { stack = []; }

    for (let i = stack.length - 1; i >= 0; i--) {
      const entry = stack[i];
      if (isValidTarget(entry)) {
        return entry;
      }
    }

    const last = sessionStorage.getItem(LAST_PAGE_KEY) || localStorage.getItem(LAST_PAGE_KEY);
    if (isValidTarget(last)) {
      return last;
    }
  } catch (_) {}

  return fallbackUrl;
}

/**
 * Navigates back to the last page in the lineage
 */
export function navigateBack(fallbackUrl = '/') {
  const target = getLastPage(fallbackUrl);

  // Pop current page from lineage so it behaves like a clean back-navigation
  try {
    const current = getCurrentPath();
    let stack = [];
    try {
      stack = JSON.parse(sessionStorage.getItem(LINEAGE_KEY) || '[]');
      if (!stack.length) stack = JSON.parse(localStorage.getItem(LINEAGE_KEY) || '[]');
    } catch (_) { stack = []; }

    while (stack.length && stack[stack.length - 1] === current) {
      stack.pop();
    }
    sessionStorage.setItem(LINEAGE_KEY, JSON.stringify(stack));
    localStorage.setItem(LINEAGE_KEY, JSON.stringify(stack));
  } catch (_) {}

  // Can we safely use history.back()?
  // Only if history length > 1 AND same-origin referrer matches target
  let canUseHistoryBack = false;
  try {
    if (typeof window !== 'undefined' && window.history && window.history.length > 1 && document.referrer) {
      const ref = new URL(document.referrer);
      if (ref.origin === window.location.origin) {
        const refPath = ref.pathname + ref.search + ref.hash;
        if (refPath === target || ref.pathname === target.split('?')[0]) {
          canUseHistoryBack = true;
        }
      }
    }
  } catch (_) {}

  if (canUseHistoryBack) {
    window.history.back();
    // Safety fallback: if history.back() didn't cause navigation after 250ms, force redirect
    setTimeout(() => {
      window.location.href = target;
    }, 250);
  } else {
    window.location.href = target;
  }
}

/**
 * Wires a back button element to the Page Lineage
 */
export function wireBackButton(el, fallbackUrl = '/', updateLabel = false) {
  if (!el) return;
  const target = getLastPage(fallbackUrl);
  el.href = target;

  const label = getPageLabel(target);
  if (updateLabel && label) {
    el.innerHTML = `<i class="fa-solid fa-arrow-left"></i> Back to ${label}`;
    el.setAttribute('title', `Back to ${label}`);
  } else if (label) {
    el.setAttribute('title', `Back to ${label}`);
  }

  if (!el.dataset.lineageClickBound) {
    el.dataset.lineageClickBound = 'true';
    el.addEventListener('click', (e) => {
      // Allow standard browser tab opening if modifier keys are pressed
      if (e.ctrlKey || e.metaKey || e.shiftKey) return;
      e.preventDefault();
      e.stopPropagation();
      navigateBack(el.getAttribute('href') || fallbackUrl);
    });
  }
}

/**
 * Automatically wires known back button elements across the page
 */
export function autoWireBackButtons() {
  if (typeof document === 'undefined') return;

  const selectors = [
    '#profilePageBack',
    '#dlBack',
    '#inBack',
    '.profile-page-back',
    '.tp-breadcrumb a',
    '.cc-breadcrumb a',
    '.cp-back',
    '.pl-back',
    '.dl-back',
    '[data-back-button]'
  ];

  selectors.forEach(selector => {
    document.querySelectorAll(selector).forEach(el => {
      if (el.dataset.lineageWired) return;
      el.dataset.lineageWired = 'true';

      const fallback = (el.getAttribute('href') && el.getAttribute('href') !== '#')
        ? el.getAttribute('href')
        : '/';
      const shouldUpdateLabel = el.id === 'profilePageBack' || el.classList.contains('profile-page-back');
      wireBackButton(el, fallback, shouldUpdateLabel);
    });
  });
}

// Automatically record current page whenever this script is executed
recordCurrentPage();

// Auto-wire back buttons once DOM is available
if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoWireBackButtons);
  } else {
    autoWireBackButtons();
  }
}
