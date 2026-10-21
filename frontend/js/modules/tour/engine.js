/**
 * Interactive User Manual & Guided Tour Engine
 * Lightweight, zero-dependency spotlight and walkthrough engine for Doshin Sales AI.
 */
import { TOUR_PAGES } from './steps.js';
import { getCurrentUser } from '../auth-client.js';

export const TOUR_RELEASE_VERSION = 'v2_manual';

/**
 * Returns a user-scoped, versioned storage key for tour tracking.
 * - Guarantees that any new user logging in gets the tour automatically.
 * - Guarantees that colleagues pulling this updated code get the tour automatically once.
 * - Persists completion per user & page so it doesn't repeatedly show after they've finished.
 */
export function getTourStorageKey(pageKey) {
  let userIdentifier = 'user';
  try {
    const user = getCurrentUser ? getCurrentUser() : null;
    if (user && (user.id || user.email)) {
      userIdentifier = String(user.id || user.email).toLowerCase().replace(/[^a-z0-9_-]/g, '_');
    } else {
      const rawUser = localStorage.getItem('user');
      if (rawUser) {
        const parsed = JSON.parse(rawUser);
        if (parsed && (parsed.id || parsed.email)) {
          userIdentifier = String(parsed.id || parsed.email).toLowerCase().replace(/[^a-z0-9_-]/g, '_');
        }
      }
    }
  } catch (_) {}
  return `doshin_tour_${TOUR_RELEASE_VERSION}_${userIdentifier}_${pageKey}`;
}

/**
 * Clears tour completion flags for testing / re-triggering
 */
export function resetTourSeen(pageKey) {
  if (pageKey) {
    localStorage.removeItem(getTourStorageKey(pageKey));
  } else {
    Object.keys(localStorage).forEach((k) => {
      if (k.startsWith('doshin_tour_')) {
        localStorage.removeItem(k);
      }
    });
  }
}

if (typeof window !== 'undefined') {
  window.DoshinTour = {
    start: (key) => startTour(key, true),
    reset: resetTourSeen,
    getStorageKey: getTourStorageKey,
    version: TOUR_RELEASE_VERSION
  };
}

let currentTourKey = null;
let currentStepIndex = 0;
let currentSteps = [];
let backdropEl = null;
let spotlightEl = null;
let popoverEl = null;
let activeTargetEl = null;
let boundKeyHandler = null;
let resizeHandler = null;
let scrollHandler = null;

function clearActiveTarget() {
  if (activeTargetEl) {
    activeTargetEl.classList.remove('doshin-tour-target-active');
    if (activeTargetEl.dataset.tourOriginalPos === 'static') {
      activeTargetEl.style.position = '';
      delete activeTargetEl.dataset.tourOriginalPos;
    }
    activeTargetEl = null;
  }
}

/**
 * Detects the logical tour key based on current URL pathname
 */
export function detectPageTourKey() {
  const path = (window.location.pathname || '').toLowerCase();
  if (path.includes('command-center')) return 'command-center';
  if (path.includes('copilot') && !path.includes('copilot-pipeline')) return 'copilot';
  if (path.includes('capture')) return 'capture';
  if (path.includes('deals')) return 'deals';
  if (path.includes('forecast')) return 'forecast';
  if (path.includes('introductions')) return 'introductions';
  if (path.includes('tasks')) return 'tasks';
  if (path === '/' || path.includes('account') || path.includes('index')) return 'dashboard';
  return 'dashboard';
}

/**
 * Ensures DOM elements for the tour exist
 */
function ensureTourDom() {
  if (!backdropEl) {
    backdropEl = document.createElement('div');
    backdropEl.className = 'doshin-tour-backdrop';
    document.body.appendChild(backdropEl);
  }

  if (!spotlightEl) {
    spotlightEl = document.createElement('div');
    spotlightEl.className = 'doshin-tour-spotlight';
    spotlightEl.style.display = 'none';
    document.body.appendChild(spotlightEl);
  }

  if (!popoverEl) {
    popoverEl = document.createElement('div');
    popoverEl.className = 'doshin-tour-popover';
    popoverEl.style.display = 'none';
    document.body.appendChild(popoverEl);
  }
}

/**
 * Checks whether an element is visible in the DOM
 */
function isElementVisible(el) {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

/**
 * Starts the tour for a given page.
 * @param {string} [pageKey] - Optional key ('command-center', 'deals', etc.)
 * @param {boolean} [force=false] - If true, bypasses localStorage seen check.
 */
export function startTour(pageKey, force = false) {
  const key = pageKey || detectPageTourKey();
  const tourData = TOUR_PAGES[key];
  if (!tourData || !tourData.steps || tourData.steps.length === 0) return;

  const storageKey = getTourStorageKey(key);
  if (!force && localStorage.getItem(storageKey)) {
    return; // Already completed and not explicitly requested
  }

  ensureTourDom();

  currentTourKey = key;
  currentStepIndex = 0;
  currentSteps = tourData.steps;

  backdropEl.classList.add('active');

  // Key navigation
  if (boundKeyHandler) window.removeEventListener('keydown', boundKeyHandler);
  boundKeyHandler = (e) => {
    if (e.key === 'Escape') {
      endTour(true);
    } else if (e.key === 'ArrowRight' || e.key === 'Enter') {
      nextStep();
    } else if (e.key === 'ArrowLeft') {
      prevStep();
    }
  };
  window.addEventListener('keydown', boundKeyHandler);

  // Resize and Scroll listeners
  if (resizeHandler) window.removeEventListener('resize', resizeHandler);
  resizeHandler = () => {
    if (popoverEl && popoverEl.style.display !== 'none') {
      renderCurrentStep(false);
    }
  };
  window.addEventListener('resize', resizeHandler);

  if (scrollHandler) window.removeEventListener('scroll', scrollHandler, true);
  scrollHandler = () => {
    if (popoverEl && popoverEl.style.display !== 'none') {
      // Reposition on scroll without triggering recursive smooth scrolls
      renderCurrentStep(false);
    }
  };
  window.addEventListener('scroll', scrollHandler, { passive: true });

  renderCurrentStep(true);
}

/**
 * Advances to the next tour step
 */
export function nextStep() {
  if (currentStepIndex < currentSteps.length - 1) {
    currentStepIndex++;
    renderCurrentStep(true);
  } else {
    endTour(false);
  }
}

/**
 * Moves back to the previous tour step
 */
export function prevStep() {
  if (currentStepIndex > 0) {
    currentStepIndex--;
    renderCurrentStep(true);
  }
}

/**
 * Closes the tour and marks it as seen in localStorage
 */
export function endTour(isSkipped = false) {
  clearActiveTarget();
  if (backdropEl) {
    backdropEl.classList.remove('active');
    backdropEl.style.clipPath = 'none';
  }
  if (spotlightEl) spotlightEl.style.display = 'none';
  if (popoverEl) {
    popoverEl.classList.remove('visible');
    setTimeout(() => {
      popoverEl.style.display = 'none';
    }, 200);
  }

  if (currentTourKey) {
    localStorage.setItem(getTourStorageKey(currentTourKey), 'true');
  }

  if (boundKeyHandler) {
    window.removeEventListener('keydown', boundKeyHandler);
    boundKeyHandler = null;
  }
  if (resizeHandler) {
    window.removeEventListener('resize', resizeHandler);
    resizeHandler = null;
  }
  if (scrollHandler) {
    window.removeEventListener('scroll', scrollHandler, true);
    scrollHandler = null;
  }
}

/**
 * Renders the active step, highlights the target element, and places the popover.
 */
function renderCurrentStep(shouldScroll = true) {
  if (!currentSteps[currentStepIndex]) return;
  const step = currentSteps[currentStepIndex];
  const tourData = TOUR_PAGES[currentTourKey];

  // Resolve target element (tries target selector, then fallback selector)
  let targetEl = document.querySelector(step.target);
  if ((!targetEl || !isElementVisible(targetEl)) && step.targetFallback) {
    const fallback = document.querySelector(step.targetFallback);
    if (fallback && isElementVisible(fallback)) {
      targetEl = fallback;
    }
  }

  const isVisible = isElementVisible(targetEl);

  if (isVisible && shouldScroll) {
    const isFixed = window.getComputedStyle(targetEl).position === 'fixed';
    if (!isFixed) {
      try {
        targetEl.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' });
      } catch (_) {}
    }
  }

  // Allow smooth scroll to settle before computing positions
  setTimeout(() => {
    positionSpotlightAndPopover(targetEl, step, tourData);
  }, (isVisible && shouldScroll) ? 300 : 0);
}

function positionSpotlightAndPopover(targetEl, step, tourData) {
  const totalSteps = currentSteps.length;
  const stepNum = currentStepIndex + 1;
  const isLast = stepNum === totalSteps;

  // Build dots HTML
  const dotsHtml = currentSteps.map((_, i) => `
    <span class="doshin-tour-dot ${i === currentStepIndex ? 'active' : ''}"></span>
  `).join('');

  // Populate Popover Content
  popoverEl.innerHTML = `
    <div class="doshin-tour-header">
      <div class="doshin-tour-header-left">
        <span class="doshin-tour-badge"><i class="fa-solid fa-compass"></i> ${tourData.badge || 'User Manual'}</span>
        <span class="doshin-tour-step-counter">Step ${stepNum} of ${totalSteps}</span>
      </div>
      <button type="button" class="doshin-tour-close-btn" id="doshinTourCloseBtn" title="Close Tour (Esc)" aria-label="Close Tour">
        <i class="fa-solid fa-xmark"></i>
      </button>
    </div>

    <div class="doshin-tour-progress-track">
      <div class="doshin-tour-progress-bar" style="width: ${(stepNum / totalSteps) * 100}%;"></div>
    </div>

    <h3 class="doshin-tour-title">
      <i class="${step.icon || 'fa-solid fa-circle-info'}"></i>
      <span>${step.title}</span>
    </h3>

    <p class="doshin-tour-desc">${step.desc}</p>

    ${step.tip ? `
      <div class="doshin-tour-tip">
        <i class="fa-solid fa-lightbulb"></i>
        <span><strong>Pro Tip:</strong> ${step.tip}</span>
      </div>
    ` : ''}

    <div class="doshin-tour-footer">
      <button type="button" class="doshin-tour-btn doshin-tour-btn-skip" id="doshinTourSkipBtn">Skip Tour</button>
      <div class="doshin-tour-nav-btns">
        <button type="button" class="doshin-tour-btn doshin-tour-btn-back" id="doshinTourBackBtn" style="${currentStepIndex === 0 ? 'visibility:hidden;pointer-events:none;' : ''}">
          <i class="fa-solid fa-chevron-left"></i> Back
        </button>
        <button type="button" class="doshin-tour-btn doshin-tour-btn-next" id="doshinTourNextBtn">
          ${isLast ? 'Got it! <i class="fa-solid fa-check"></i>' : 'Next <i class="fa-solid fa-chevron-right"></i>'}
        </button>
      </div>
    </div>
    <div class="doshin-tour-arrow"></div>
  `;

  // Attach button events
  document.getElementById('doshinTourCloseBtn')?.addEventListener('click', () => endTour(true));
  document.getElementById('doshinTourSkipBtn')?.addEventListener('click', () => endTour(true));
  document.getElementById('doshinTourBackBtn')?.addEventListener('click', prevStep);
  document.getElementById('doshinTourNextBtn')?.addEventListener('click', nextStep);

  popoverEl.style.display = 'flex';

  const isVisible = isElementVisible(targetEl);
  const padding = 6;
  let targetRect = null;
  let isFixed = false;

  clearActiveTarget();

  if (isVisible) {
    activeTargetEl = targetEl;
    const computedPos = window.getComputedStyle(targetEl).position;
    if (computedPos === 'static') {
      targetEl.dataset.tourOriginalPos = 'static';
      targetEl.style.position = 'relative';
    }
    targetEl.classList.add('doshin-tour-target-active');

    const r = targetEl.getBoundingClientRect();
    isFixed = window.getComputedStyle(targetEl).position === 'fixed';

    targetRect = {
      viewportTop: r.top,
      viewportLeft: r.left,
      viewportBottom: r.bottom,
      viewportRight: r.right,
      width: r.width,
      height: r.height,
      docTop: r.top + window.scrollY,
      docLeft: r.left + window.scrollX
    };

    // Calculate cutout coordinates clamped to viewport
    const x1 = Math.max(0, Math.round(targetRect.viewportLeft - padding));
    const y1 = Math.max(0, Math.round(targetRect.viewportTop - padding));
    const x2 = Math.min(window.innerWidth, Math.round(targetRect.viewportRight + padding));
    const y2 = Math.min(window.innerHeight, Math.round(targetRect.viewportBottom + padding));

    // Punch an evenodd cutout hole in the blurred/greyed backdrop so widget is crystal clear
    if (backdropEl) {
      backdropEl.style.clipPath = `polygon(evenodd, 0px 0px, 100vw 0px, 100vw 100vh, 0px 100vh, 0px 0px, ${x1}px ${y1}px, ${x2}px ${y1}px, ${x2}px ${y2}px, ${x1}px ${y2}px, ${x1}px ${y1}px)`;
    }

    // Match spotlight border-radius to target element
    const targetComputed = window.getComputedStyle(targetEl);
    const targetRadius = targetComputed.borderRadius;
    spotlightEl.style.borderRadius = (targetRadius && targetRadius !== '0px') ? targetRadius : '8px';

    // Position spotlight cutout
    spotlightEl.style.display = 'block';
    if (isFixed) {
      spotlightEl.style.position = 'fixed';
      spotlightEl.style.top = `${targetRect.viewportTop - padding}px`;
      spotlightEl.style.left = `${targetRect.viewportLeft - padding}px`;
    } else {
      spotlightEl.style.position = 'absolute';
      spotlightEl.style.top = `${targetRect.docTop - padding}px`;
      spotlightEl.style.left = `${targetRect.docLeft - padding}px`;
    }
    spotlightEl.style.width = `${targetRect.width + padding * 2}px`;
    spotlightEl.style.height = `${targetRect.height + padding * 2}px`;
  } else {
    // If element is not currently visible, cover whole page without cutout
    if (backdropEl) backdropEl.style.clipPath = 'none';
    spotlightEl.style.display = 'none';
  }

  // Calculate Popover Placement
  const popoverWidth = popoverEl.offsetWidth || 360;
  const popoverHeight = popoverEl.offsetHeight || 220;
  const margin = 14;

  let placement = step.preferredPlacement || 'bottom';
  let popoverTop = 0;
  let popoverLeft = 0;

  if (targetRect) {
    const spaceBelow = window.innerHeight - targetRect.viewportBottom;
    const spaceAbove = targetRect.viewportTop;
    const spaceRight = window.innerWidth - targetRect.viewportRight;
    const spaceLeft = targetRect.viewportLeft;

    // Boundary check: adjust placement if preferred placement has insufficient space
    if (placement === 'bottom' && spaceBelow < popoverHeight + margin && spaceAbove > popoverHeight + margin) {
      placement = 'top';
    } else if (placement === 'top' && spaceAbove < popoverHeight + margin && spaceBelow > popoverHeight + margin) {
      placement = 'bottom';
    } else if (placement === 'right' && spaceRight < popoverWidth + margin && spaceLeft > popoverWidth + margin) {
      placement = 'left';
    } else if (placement === 'left' && spaceLeft < popoverWidth + margin && spaceRight > popoverWidth + margin) {
      placement = 'right';
    }

    if (isFixed) {
      popoverEl.style.position = 'fixed';
      if (placement === 'bottom') {
        popoverTop = targetRect.viewportBottom + margin;
        popoverLeft = targetRect.viewportLeft + (targetRect.width / 2) - (popoverWidth / 2);
      } else if (placement === 'top') {
        popoverTop = targetRect.viewportTop - popoverHeight - margin;
        popoverLeft = targetRect.viewportLeft + (targetRect.width / 2) - (popoverWidth / 2);
      } else if (placement === 'right') {
        popoverTop = targetRect.viewportTop + (targetRect.height / 2) - (popoverHeight / 2);
        popoverLeft = targetRect.viewportRight + margin;
      } else if (placement === 'left') {
        popoverTop = targetRect.viewportTop + (targetRect.height / 2) - (popoverHeight / 2);
        popoverLeft = targetRect.viewportLeft - popoverWidth - margin;
      }
    } else {
      popoverEl.style.position = 'absolute';
      if (placement === 'bottom') {
        popoverTop = targetRect.docTop + targetRect.height + margin;
        popoverLeft = targetRect.docLeft + (targetRect.width / 2) - (popoverWidth / 2);
      } else if (placement === 'top') {
        popoverTop = targetRect.docTop - popoverHeight - margin;
        popoverLeft = targetRect.docLeft + (targetRect.width / 2) - (popoverWidth / 2);
      } else if (placement === 'right') {
        popoverTop = targetRect.docTop + (targetRect.height / 2) - (popoverHeight / 2);
        popoverLeft = targetRect.docLeft + targetRect.width + margin;
      } else if (placement === 'left') {
        popoverTop = targetRect.docTop + (targetRect.height / 2) - (popoverHeight / 2);
        popoverLeft = targetRect.docLeft - popoverWidth - margin;
      }
    }
  } else {
    // Center in viewport if target element is missing
    placement = 'center';
    popoverEl.style.position = 'fixed';
    popoverTop = (window.innerHeight / 2) - (popoverHeight / 2);
    popoverLeft = (window.innerWidth / 2) - (popoverWidth / 2);
  }

  // Viewport containment: ensure card does not clip off-screen (horizontally and vertically)
  const screenPad = 14;
  const baseLeft = isFixed ? 0 : window.scrollX;
  const maxLeft = baseLeft + window.innerWidth - popoverWidth - screenPad;
  const minLeft = baseLeft + screenPad;
  popoverLeft = Math.max(minLeft, Math.min(popoverLeft, maxLeft));

  const baseTop = isFixed ? 0 : window.scrollY;
  const maxTop = baseTop + window.innerHeight - popoverHeight - screenPad;
  const minTop = baseTop + screenPad;
  if (maxTop >= minTop) {
    popoverTop = Math.max(minTop, Math.min(popoverTop, maxTop));
  } else {
    popoverTop = minTop;
  }

  popoverEl.setAttribute('data-placement', placement);
  popoverEl.style.top = `${Math.round(popoverTop)}px`;
  popoverEl.style.left = `${Math.round(popoverLeft)}px`;

  // Dynamically align the pointer arrow to point directly at the target element
  const arrowEl = popoverEl.querySelector('.doshin-tour-arrow');
  if (arrowEl && targetRect) {
    if (placement === 'left' || placement === 'right') {
      const targetCenterY = targetRect.viewportTop + (targetRect.height / 2);
      const popoverViewportTop = isFixed ? popoverTop : (popoverTop - window.scrollY);
      const arrowRelY = targetCenterY - popoverViewportTop;
      const clampedY = Math.max(22, Math.min(popoverHeight - 22, arrowRelY));
      arrowEl.style.top = `${Math.round(clampedY)}px`;
      arrowEl.style.transform = 'translateY(-50%) rotate(45deg)';
      arrowEl.style.left = '';
    } else if (placement === 'top' || placement === 'bottom') {
      const targetCenterX = targetRect.viewportLeft + (targetRect.width / 2);
      const popoverViewportLeft = isFixed ? popoverLeft : (popoverLeft - window.scrollX);
      const arrowRelX = targetCenterX - popoverViewportLeft;
      const clampedX = Math.max(22, Math.min(popoverWidth - 22, arrowRelX));
      arrowEl.style.left = `${Math.round(clampedX)}px`;
      arrowEl.style.transform = 'translateX(-50%) rotate(45deg)';
      arrowEl.style.top = '';
    }
  }

  // Reveal popover
  requestAnimationFrame(() => {
    popoverEl.classList.add('visible');
  });
}

/**
 * Initializes automatic detection on load and wires the Topbar tour button.
 */
export function initTourAutoLaunch(delayMs = 1200) {
  // Bind topbar tour button if present
  const topbarBtn = document.getElementById('topbarTourBtn');
  if (topbarBtn) {
    topbarBtn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      startTour(detectPageTourKey(), true);
    };
  }

  // Automatic first-login / first-visit launch
  const key = detectPageTourKey();
  setTimeout(() => {
    const storageKey = getTourStorageKey(key);
    const seen = localStorage.getItem(storageKey);
    if (!seen) {
      startTour(key, false);
    }
  }, delayMs);
}
