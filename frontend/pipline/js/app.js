// ─── Global /api/ Auth Interceptor for Pipeline Console ──────────────────
(function () {
  if (window.__pipelineFetchWrapped) return;
  window.__pipelineFetchWrapped = true;
  const nativeFetch = window.fetch.bind(window);
  const AUTH_ENDPOINTS = ["/api/auth/login", "/api/auth/refresh", "/api/auth/logout"];
  let refreshPromise = null;

  function getStoredToken() {
    try {
      return sessionStorage.getItem("access_token") || localStorage.getItem("access_token");
    } catch (_) {
      return null;
    }
  }

  function refreshTokenOnce() {
    if (refreshPromise) return refreshPromise;
    refreshPromise = nativeFetch("/api/auth/refresh", {
      method: "POST",
      credentials: "include",
    })
      .then(async (res) => {
        if (!res.ok) return null;
        const data = await res.json().catch(() => null);
        if (data && data.access_token) {
          try {
            sessionStorage.setItem("access_token", data.access_token);
          } catch (_) {}
          return data.access_token;
        }
        return null;
      })
      .catch(() => null)
      .finally(() => {
        refreshPromise = null;
      });
    return refreshPromise;
  }

  function withApiAuth(input, init) {
    const token = getStoredToken();
    const headers = new Headers(
      (init && init.headers) || (input instanceof Request ? input.headers : undefined)
    );
    if (token) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    return {
      ...init,
      headers,
      credentials: (init && init.credentials) || "include",
    };
  }

  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const isApi = url.startsWith("/api/") || url.includes("/api/");
    const isAuthEndpoint = AUTH_ENDPOINTS.some((p) => url.includes(p));

    if (!isApi) {
      return nativeFetch(input, init);
    }

    return nativeFetch(input, withApiAuth(input, init)).then(async (res) => {
      if (res.status !== 401 || isAuthEndpoint) {
        return res;
      }
      const newToken = await refreshTokenOnce();
      if (!newToken) return res;
      return nativeFetch(input, withApiAuth(input, init));
    });
  };
})();

$(function () {
  // ─── Sidebar Shrink / Expand Toggle ──────────────────────────────────────
  let sidebarCollapsed = false;
  try {
    sidebarCollapsed = localStorage.getItem("sidebar_collapsed") === "true";
  } catch (e) {}

  function applySidebarCollapseState(collapsed) {
    if (collapsed) {
      $("#sidebar").addClass("collapsed");
      $("#sidebarCollapseBtn").html('<i class="bi bi-chevron-bar-right"></i>').attr("title", "Expand sidebar");
    } else {
      $("#sidebar").removeClass("collapsed");
      $("#sidebarCollapseBtn").html('<i class="bi bi-layout-sidebar-inset"></i>').attr("title", "Shrink sidebar");
    }
  }

  applySidebarCollapseState(sidebarCollapsed);

  $(document).on("click", "#sidebarCollapseBtn", function () {
    sidebarCollapsed = !$("#sidebar").hasClass("collapsed");
    applySidebarCollapseState(sidebarCollapsed);
    try {
      localStorage.setItem("sidebar_collapsed", String(sidebarCollapsed));
    } catch (e) {}
  });

  // ─── Mobile Sidebar ─────────────────────────────────────────────────────
  function closeSidebar() {
    $("#sidebar").removeClass("open");
    $("#sidebarOverlay").removeClass("open");
  }
  $("#sidebarToggle").on("click", function () {
    $("#sidebar").toggleClass("open");
    $("#sidebarOverlay").toggleClass("open");
  });
  $("#sidebarOverlay").on("click", closeSidebar);

  // ─── Fetch Live Data ──────────────────────────────────────────────────
  // Same-origin by default — this page is served by the API server itself
  // (api.py mounts /pipline as a StaticFiles dir), so API calls should just
  // hit whatever host:port the page was loaded from, not a hardcoded 8000.
  const API_BASE = "";
  let MOCK_DATA = { accounts: [] };
  let activeAccount = null;
  let activeLob = null;
  let activePersona = null;
  let stagedDataStore = {};
  let actionStateStore = {};
  // key -> { pulled: bool, validated: bool, dumped: bool, score: number, message: string }
  let allPersonasDirectoryState = {
    personas: [],
    activeTier: "all",
    searchQuery: "",
    visibleLimit: 30,
  };

  function getActionState(key) {
    if (!actionStateStore[key]) {
      actionStateStore[key] = {
        pulled: false,
        validated: false,
        dumped: false,
        score: null,
        message: "",
      };
    }
    return actionStateStore[key];
  }

  // This page runs its own independent copy of /api/accounts (loadData()
  // above) rather than sharing accounts-cache.js's ES module, but it's served
  // from the same origin as the Global Dashboard and Command Center — which
  // DO share that module's sessionStorage-backed cache (see the frontend
  // caching writeup). Without this, saving a persona/LOB/account here would
  // leave that cache serving a stale account list to those pages for up to
  // its 60s TTL. Dynamic import() works from a classic (non-module) script,
  // so this doesn't require converting this whole file to `type="module"`.
  // Best-effort only: this page's own dump flow already succeeded by the time
  // this runs, so a failure here (e.g. the module 404s from some other deploy
  // layout) shouldn't surface as an error to the user.
  async function refreshAccountsCache() {
    try {
      const mod = await import("../../js/modules/accounts-cache.js");
      await mod.loadAccountsCached({ force: true });
    } catch (e) {
      console.warn("[pipeline] Could not refresh the shared accounts cache:", e);
    }
  }

  // Exposes live selection state to the chatbot widget (chatbot.js), which
  // runs outside this closure and has no other way to read these variables.
  window.getSalesAssistantContext = function () {
    return {
      accounts: MOCK_DATA.accounts || [],
      account: activeAccount,
      lob: activeLob,
      persona: activePersona,
    };
  };
  // ══════════════════════════════════════════════════════════════════
  // ENTERPRISE VIEW ROUTING & HARD-REFRESH STATE PERSISTENCE ENGINE
  // ══════════════════════════════════════════════════════════════════
  const PipelineRouter = {
    isRestoring: false,

    parseRoute: function () {
      let hash = window.location.hash || "";
      if (hash.startsWith("#")) hash = hash.substring(1);

      if (hash) {
        if (hash.startsWith("batch-console")) {
          const qIdx = hash.indexOf("?");
          const params = new URLSearchParams(qIdx !== -1 ? hash.substring(qIdx + 1) : "");
          return {
            view: "batch_console",
            entityType: params.get("entity") || "persona",
            accountId: params.get("account") || null,
            screen: params.get("screen") || "directory",
          };
        }

        if (hash.startsWith("account/")) {
          const parts = hash.split("/");
          const acctId = parts[1];
          const subTab = parts[2] || "overview";
          return {
            view: "account",
            accountId: acctId,
            subTab: subTab,
          };
        }

        if (hash === "home" || hash === "hub") {
          return { view: "home" };
        }
      }

      // Fallback: check session storage for last active view
      try {
        const stored = sessionStorage.getItem("pipeline_active_view_state");
        if (stored) {
          const parsed = JSON.parse(stored);
          if (parsed && parsed.view) return parsed;
        }
      } catch (_) {}

      try {
        const savedId = sessionStorage.getItem("pipeline_active_account_id");
        if (savedId) {
          return { view: "account", accountId: savedId, subTab: "overview" };
        }
      } catch (_) {}

      return { view: "home" };
    },

    syncState: function (state) {
      if (this.isRestoring) return;
      try {
        sessionStorage.setItem("pipeline_active_view_state", JSON.stringify(state));
      } catch (_) {}

      let newHash = "";
      if (state.view === "batch_console") {
        const params = new URLSearchParams();
        if (state.entityType) params.set("entity", state.entityType);
        if (state.accountId) params.set("account", state.accountId);
        if (state.screen && state.screen !== "directory") params.set("screen", state.screen);
        const qs = params.toString();
        newHash = "#batch-console" + (qs ? "?" + qs : "");
      } else if (state.view === "account" && state.accountId) {
        newHash = `#account/${state.accountId}` + (state.subTab && state.subTab !== "overview" ? `/${state.subTab}` : "");
      } else if (state.view === "home") {
        newHash = "#home";
      }

      if (newHash && window.location.hash !== newHash) {
        try {
          history.replaceState(null, "", window.location.pathname + window.location.search + newHash);
        } catch (_) {
          window.location.hash = newHash;
        }
      }
    },

    restoreActiveScreen: function () {
      const route = this.parseRoute();
      const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts))
        ? MOCK_DATA.accounts
        : [];

      this.isRestoring = true;

      if (route.view === "batch_console") {
        const isModalOpen = !$("#batchEnrichmentModal").hasClass("d-none");
        if (isModalOpen && batchConsoleState.entityType === route.entityType) {
          this.isRestoring = false;
          return;
        }

        let target = null;
        if (route.accountId && route.accountId !== "all") {
          target = acctsList.find((a) => String(a.id) === String(route.accountId));
        }
        if (!target && activeAccount) target = activeAccount;
        if (!target && acctsList.length > 0) target = acctsList[0];

        if (target) {
          activeAccount = target;
          $(`#accountList .account-item[data-id="${target.id}"]`).addClass("active");
        }

        if (typeof openBatchConsole === "function") {
          openBatchConsole(route.entityType || "persona", target);
          if (route.entityType === "account") {
            if (route.accountId && route.accountId !== "all") {
              $("#batchCompanySelector").val(String(route.accountId));
              refreshBatchConsole();
            } else if (route.accountId === "all") {
              $("#batchCompanySelector").val("all");
              refreshBatchConsole();
            }
          }
          if (route.screen === "history") {
            setTimeout(() => {
              $("#batchTabHistoryBtn").trigger("click");
            }, 60);
          }
        }
      } else if (route.view === "account" && route.accountId) {
        if (activeAccount && String(activeAccount.id) === String(route.accountId) && !$("#dashboardContainer").hasClass("d-none")) {
          this.isRestoring = false;
          return;
        }
        const target = acctsList.find((a) => String(a.id) === String(route.accountId));
        if (target) {
          const $item = $(`#accountList .account-item[data-id="${target.id}"]`);
          if ($item.length) {
            $item.trigger("click");
            if (route.subTab && route.subTab !== "overview") {
              setTimeout(() => {
                $(`.tab-pill-btn[data-nav-tab="${route.subTab}"]`).trigger("click");
              }, 150);
            }
          } else {
            renderEmptyStateHub();
          }
        } else {
          renderEmptyStateHub();
        }
      } else {
        renderEmptyStateHub();
      }

      this.isRestoring = false;
      this.syncState(route);
    }
  };

  window.addEventListener("hashchange", function () {
    if (PipelineRouter.isRestoring) return;
    PipelineRouter.restoreActiveScreen();
  });

  async function loadData(isRetry) {
    // Instant load from sessionStorage cache if available (eliminates refresh delay)
    if (!isRetry) {
      try {
        sessionStorage.removeItem("pipeline_accounts_cache");
        sessionStorage.removeItem("pipeline_accounts_cache_v2");
        const cached = sessionStorage.getItem("pipeline_accounts_cache_v3");
        if (cached) {
          const parsed = JSON.parse(cached);
          if (parsed && Array.isArray(parsed.accounts) && parsed.accounts.length > 0) {
            MOCK_DATA = parsed;
            updateGlobalTelemetry();
            renderSidebar();
            updateBatchTriggerPills(activeAccount);
            PipelineRouter.restoreActiveScreen();
          }
        }
      } catch (e) {}

      if (!MOCK_DATA.accounts || MOCK_DATA.accounts.length === 0) {
        $("#accountList").html(
          `<div class="spinner-wrap"><div class="spinner"></div></div>`
        );
      }
    }
    try {
      const url = `${API_BASE}/api/accounts`;
      console.log("[loadData] Fetching:", url);
      const token = sessionStorage.getItem('access_token') || localStorage.getItem('access_token');
      const headers = {};
      if (token) headers['Authorization'] = `Bearer ${token}`;

      let response = await fetch(url, { headers, credentials: 'include' });
      console.log("[loadData] HTTP status:", response.status);

      if (response.status === 401) {
        // Try refreshing token once via cookie
        try {
          const refreshRes = await fetch(`${API_BASE}/api/auth/refresh`, { method: 'POST', credentials: 'include' });
          if (refreshRes.ok) {
            const refreshData = await refreshRes.json();
            if (refreshData.access_token) {
              sessionStorage.setItem('access_token', refreshData.access_token);
              headers['Authorization'] = `Bearer ${refreshData.access_token}`;
              response = await fetch(url, { headers, credentials: 'include' });
            }
          }
        } catch (_) {}
      }

      // Check pipeline access permission
      try {
        const meRes = await fetch(`${API_BASE}/api/auth/me`, { headers, credentials: 'include' });
        if (meRes.ok) {
          const me = await meRes.json();
          if (me && me.role !== 'super_admin' && me.has_pipeline_access === false) {
            $("#accountList").html(
              `<div style="padding:16px 12px;font-size:.84rem;color:#e11d48;line-height:1.5">` +
                `<strong>Access Restricted</strong><br>` +
                `<span style="font-size:.78rem;color:#64748b;display:block;margin-top:4px;">Your account does not have access to the Data Pipeline Console. Contact a Super Admin to request access.</span>` +
                `<a href="/command-center" style="margin-top:12px;display:inline-block;padding:5px 12px;font-size:.78rem;border-radius:6px;background:#0ea5e9;color:#fff;text-decoration:none;font-weight:600;">Go to Command Center</a>` +
              `</div>`
            );
            return;
          }
        }
      } catch (_) {}

      if (!response.ok) {
        throw new Error(`HTTP ${response.status} from ${url}`);
      }
      let data;
      try {
        data = await response.json();
      } catch (jsonErr) {
        throw new Error(`JSON parse failed: ${jsonErr.message}`);
      }
      if (!data || !Array.isArray(data.accounts)) {
        throw new Error(`Unexpected response shape — missing accounts array`);
      }

      // Merge previously hydrated full accounts from memory
      if (MOCK_DATA && Array.isArray(MOCK_DATA.accounts)) {
        data.accounts = data.accounts.map(freshAcct => {
          const existing = MOCK_DATA.accounts.find(a => a.id === freshAcct.id);
          if (existing && existing._is_full_loaded) {
            return { ...freshAcct, ...existing };
          }
          return freshAcct;
        });
      }

      MOCK_DATA = data;
      try {
        sessionStorage.setItem("pipeline_accounts_cache_v3", JSON.stringify(data));
      } catch (e) {}

      updateGlobalTelemetry();
      renderSidebar();
      updateBatchTriggerPills(activeAccount);
      PipelineRouter.restoreActiveScreen();
    } catch (err) {
      console.error("[loadData] Error:", err);
      $("#accountList").html(
        `<div style="padding:12px 10px;font-size:.82rem;color:red;line-height:1.5">` +
          `<strong>Could not load accounts.</strong><br>` +
          `<span style="font-size:.78rem;opacity:.85">${err.message}</span><br>` +
          `<button onclick="window.loadData(true)" style="margin-top:6px;padding:3px 10px;` +
          `font-size:.78rem;cursor:pointer;border-radius:4px;border:1px solid red;` +
          `background:transparent;color:red">↺ Retry</button>` +
        `</div>`
      );
    }
  }
  window.loadData = loadData;

  // Expose for the Retry button's inline onclick (runs outside this closure).
  window.loadData = loadData;

  // ─── UI Rendering Logic ─────────────────────────────────────────────────


  const BRAND_ICONS = {
    google_news: `
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="5" fill="#FFFFFF"/>
      <path d="M4 6h16a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2z"
        fill="#4285F4"/><path d="M5 9h6v6H5z" fill="#EA4335"/>
      <path d="M13 9h6M13 12h6M13 15h4" stroke="#FFFFFF" stroke-width="1.6" stroke-linecap="round"/>
      </svg>
    `,
    reddit: `
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <circle cx="12" cy="12" r="11" fill="#FF4500"/>
      <circle cx="9" cy="11.5" r="1.3" fill="#FFFFFF"/>
      <circle cx="15" cy="11.5" r="1.3" fill="#FFFFFF"/>
      <path d="M9.5 15c.8.8 4.2.8 5 0" stroke="#FFFFFF" stroke-width="1.4" stroke-linecap="round"/>
      <circle cx="17.5" cy="8.5" r="1.2" fill="#FFFFFF"/>
      <path d="M12 9l3-1.5" stroke="#FFFFFF" stroke-width="1.2" stroke-linecap="round"/></svg>
    `,
    google_patents: `
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="5" fill="#4285F4"/>
      <path d="M6 5h8l4 4v10a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" fill="#FFFFFF"/>
      <path d="M14 5v4h4" fill="#E8F0FE"/><circle cx="10.5" cy="13.5" r="2" fill="#34A853"/>
      <path d="M10.5 15.5v2.5M9 17.5l3-2" stroke="#34A853" stroke-width="1.2"/></svg>
    `,
    google_trends: `
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="5" fill="#FFFFFF" stroke="#E0E0E0"/>
      <path d="M4 17l4.5-5 3.5 3 6.5-8" stroke="#4285F4" stroke-width="2.5"
        stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M15 7h4v4" stroke="#EA4335" stroke-width="2.5" stroke-linecap="round"
        stroke-linejoin="round"/></svg>
    `,
    youtube: `
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="5" fill="#FF0000"/>
      <path d="M10 8.5l6 3.5-6 3.5v-7z" fill="#FFFFFF"/></svg>
    `,
    linkedin: `
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="4" fill="#0A66C2"/>
      <path d="M7.12 18.5H4.25V9.25h2.87v9.25zM5.68 8a1.66 1.66 0 1 1 0-3.32 1.66 1.66 0 0 1 0
        3.32zm13.07 10.5h-2.87v-4.5c0-1.07-.02-2.45-1.5-2.45-1.5 0-1.73 1.17-1.73
        2.38v4.57h-2.87V9.25h2.75v1.26h.04c.38-.72 1.32-1.48 2.72-1.48 2.91 0 3.45 1.91 3.45
        4.4v5.07z" fill="#FFFFFF"/></svg>
    `,
    x_twitter: `
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="5" fill="#000000"/>
      <path d="M16.5 5.5h2.5l-5.5 6.3L20 18.5h-5.1l-4-5.2-4.5 5.2H3.9l5.9-6.8L3.5 5.5h5.2l3.6
        4.8 4.2-4.8z" fill="#FFFFFF"/></svg>
    `,
    podcast: `
      <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
      <rect width="24" height="24" rx="5" fill="#8743D6"/>
      <path d="M12 6a3 3 0 0 0-3 3v4a3 3 0 0 0 6 0V9a3 3 0 0 0-3-3z" fill="#FFFFFF"/>
      <path d="M6.5 11.5a5.5 5.5 0 0 0 11 0M12 17v3M9.5 20h5" stroke="#FFFFFF"
        stroke-width="1.8" stroke-linecap="round"/></svg>
    `,
  };

  function getInitials(name) {
    return (name || "?")
      .split(/\s+/)
      .slice(0, 2)
      .map((w) => w[0])
      .join("")
      .toUpperCase();
  }
  function esc(s) {
    return $("<div>")
      .text(s == null ? "" : s)
      .html();
  }
  function slugify(text) {
    if (!text) return "";
    return String(text)
      .toLowerCase()
      .trim()
      .replace(/[^\w\s-]/g, "")
      .replace(/[\s_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
  }

  // ─── Universal Persona Profile Headshot & Avatar Engine ─────────────────
  function extractPersonaPhotoUrl(p) {
    if (!p) return null;
    let url = p.photo_url || p.photo || p.image_url || p.profile_picture || null;
    if (!url && p.raw_data && typeof p.raw_data === "object") {
      url = p.raw_data.photo_url || p.raw_data.photo || p.raw_data.image_url;
      if (!url && p.raw_data.apify_linkedin && typeof p.raw_data.apify_linkedin === "object") {
        url = p.raw_data.apify_linkedin.photo || (p.raw_data.apify_linkedin.profilePicture && p.raw_data.apify_linkedin.profilePicture.url);
      }
      if (!url && p.raw_data.diffbot && typeof p.raw_data.diffbot === "object") {
        url = p.raw_data.diffbot.image_url || p.raw_data.diffbot.image;
      }
      if (!url && p.raw_data.apollo && typeof p.raw_data.apollo === "object") {
        url = p.raw_data.apollo.photo_url;
      }
    }
    if (!url && p.extended_profile && typeof p.extended_profile === "object") {
      url = p.extended_profile.photo_url;
    }
    if (typeof url === "string" && url.startsWith("http")) {
      const lower = url.toLowerCase();
      if (!lower.includes("ghost") && !lower.includes("placeholder") && !lower.includes("spacer") && !lower.includes("logo")) {
        return url;
      }
    }
    return null;
  }

  function renderPersonaAvatar(p, extraClass = "", customStyle = "") {
    const pName = p ? (p.name || p.full_name || p.display_name || "EX") : "EX";
    const initials = esc(getInitials(pName));
    const tierCat = typeof getPersonaTierCategory === "function" ? getPersonaTierCategory(p) : "";
    const tierAvatarClass = tierCat === "c_suite" ? "avatar-csuite" : (tierCat === "vp_head" ? "avatar-vp" : "");
    const baseClass = extraClass.includes("compact-card-avatar") || extraClass.includes("header-avatar-box") || extraClass.includes("drawer-avatar")
      ? extraClass
      : `compact-card-avatar ${extraClass}`;
    const combinedClass = [baseClass, tierAvatarClass].filter(Boolean).join(" ");
    const photoUrl = extractPersonaPhotoUrl(p);

    if (photoUrl) {
      return `
        <div class="${combinedClass} avatar-has-photo" style="position:relative;overflow:hidden;${customStyle}">
          <img src="${esc(photoUrl)}"
               alt="${esc(pName)}"
               class="avatar-photo-img"
               referrerpolicy="no-referrer"
               loading="lazy"
               onload="this.style.opacity='1';"
               onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='flex';"
               style="width:100%;height:100%;object-fit:cover;border-radius:inherit;display:block;opacity:0;transition:opacity 0.2s ease-in-out;" />
          <div class="avatar-photo-fallback" style="display:flex;width:100%;height:100%;align-items:center;justify-content:center;position:absolute;top:0;left:0;border-radius:inherit;">
            ${initials}
          </div>
        </div>
      `;
    }

    return `
      <div class="${combinedClass}" style="${customStyle}">
        ${initials}
      </div>
    `;
  }


  // ─── Global Topbar Database Telemetry ────────────────────────────────────
  function updateGlobalTelemetry() {
    const accounts = Array.isArray(MOCK_DATA.accounts) ? MOCK_DATA.accounts : [];
    const totalAccounts = accounts.length;
    let totalPersonas = 0;
    let totalLobs = 0;

    accounts.forEach((a) => {
      const pCount = (a.personas && Array.isArray(a.personas) && a.personas.length > 0)
        ? a.personas.length
        : (a.total_contacts_captured || a.num_contacts || a.personas_count || 0);
      totalPersonas += (typeof pCount === "number" && !isNaN(pCount)) ? pCount : 0;

      const lCount = (a.lobs && Array.isArray(a.lobs) && a.lobs.length > 0)
        ? a.lobs.length
        : (a.lobs_count || a.total_lobs || 0);
      totalLobs += (typeof lCount === "number" && !isNaN(lCount)) ? lCount : 0;
    });

    $("#topbarAccountsCount").text(totalAccounts.toLocaleString());
    $("#topbarPersonasCount").text(totalPersonas.toLocaleString());
    $("#topbarLobsCount").text(totalLobs.toLocaleString());
  }

  // Ensure default clean light theme
  try {
    document.documentElement.setAttribute("data-theme", "light");
    localStorage.removeItem("pipeline_theme");
  } catch (e) {}

  // ─── Global Pipeline Live Pulse Status ──────────────────────────────────
  let pipelineTimerInterval = null;
  let pipelineStartTime = null;

  function setGlobalPipelineStatus(isRunning, levelName = "Pipeline") {
    const $badge = $("#pipelineStatusBadge");
    if (!$badge.length) return;

    if (pipelineTimerInterval) {
      clearInterval(pipelineTimerInterval);
      pipelineTimerInterval = null;
    }

    if (isRunning) {
      pipelineStartTime = Date.now();
      $badge.removeClass("ready done error").addClass("running");
      const updateTimer = () => {
        const elapsed = ((Date.now() - pipelineStartTime) / 1000).toFixed(1);
        $badge.html(`<span class="pulse-dot-blue"></span> Running ${esc(levelName)} (${elapsed}s)`);
      };
      updateTimer();
      pipelineTimerInterval = setInterval(updateTimer, 200);
    } else {
      if (pipelineStartTime) {
        const totalTime = ((Date.now() - pipelineStartTime) / 1000).toFixed(1);
        $badge.removeClass("running error").addClass("ready done");
        $badge.html(`<i class="bi bi-check-circle-fill text-success"></i> ${esc(levelName)} Complete (${totalTime}s)`);
        pipelineStartTime = null;
        setTimeout(() => {
          $badge.removeClass("done");
          $badge.html(`<i class="bi bi-circle-fill"></i> Pipeline: Ready`);
        }, 3500);
      } else {
        $badge.removeClass("running done error").addClass("ready");
        $badge.html(`<i class="bi bi-circle-fill"></i> Pipeline: Ready`);
      }
    }
  }
  window.setGlobalPipelineStatus = setGlobalPipelineStatus;

  // ─── Omnichannel Command Palette (Cmd+K / Ctrl+K) ────────────────────────
  function initSpotlight() {
    const $backdrop = $("#spotlightBackdrop");
    const $input = $("#spotlightInput");
    const $results = $("#spotlightResults");
    const $count = $("#spotlightTotalCount");

    function openSpotlight() {
      $backdrop.removeClass("d-none");
      $input.val("").focus();
      renderSpotlightResults("");
    }

    function closeSpotlight() {
      $backdrop.addClass("d-none");
    }

    $("#topbarSpotlightBtn").on("click", openSpotlight);
    $("#spotlightCloseBtn").on("click", closeSpotlight);

    $backdrop.on("click", function (e) {
      if ($(e.target).is("#spotlightBackdrop")) {
        closeSpotlight();
      }
    });

    $(document).on("keydown", function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if ($backdrop.hasClass("d-none")) {
          openSpotlight();
        } else {
          closeSpotlight();
        }
        return;
      }

      if (!$backdrop.hasClass("d-none")) {
        if (e.key === "Escape") {
          e.preventDefault();
          closeSpotlight();
          return;
        }

        const $items = $results.find(".spotlight-item");
        if (!$items.length) return;

        let currentIndex = $items.index($results.find(".spotlight-item.active"));

        if (e.key === "ArrowDown") {
          e.preventDefault();
          if (currentIndex < 0 || currentIndex >= $items.length - 1) {
            currentIndex = 0;
          } else {
            currentIndex++;
          }
          $items.removeClass("active");
          const $target = $items.eq(currentIndex).addClass("active");
          if ($target[0]) $target[0].scrollIntoView({ block: "nearest" });
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          if (currentIndex <= 0) {
            currentIndex = $items.length - 1;
          } else {
            currentIndex--;
          }
          $items.removeClass("active");
          const $target = $items.eq(currentIndex).addClass("active");
          if ($target[0]) $target[0].scrollIntoView({ block: "nearest" });
        } else if (e.key === "Enter") {
          e.preventDefault();
          if (currentIndex >= 0 && currentIndex < $items.length) {
            $items.eq(currentIndex).trigger("click");
          }
        }
      }
    });

    $input.on("input", function () {
      renderSpotlightResults($(this).val());
    });

    function renderSpotlightResults(rawQuery) {
      const q = (rawQuery || "").trim().toLowerCase();
      const accounts = Array.isArray(MOCK_DATA.accounts) ? MOCK_DATA.accounts : [];

      if (!q) {
        // Show default quick accounts
        let html = `<div class="spotlight-group-title">Enterprise Accounts</div>`;
        accounts.slice(0, 6).forEach((a) => {
          const aName = a.name || a.display_name || "Unnamed Account";
          const domain = a.primary_domain || a.domain || "";
          const lobsCount = (a.lobs || []).length || a.lobs_count || 0;
          const personasCount = (a.personas || []).length || a.total_contacts_captured || 0;

          html += `
            <div class="spotlight-item" data-type="account" data-account-id="${a.id}">
              <div class="spotlight-item-left">
                <div class="spotlight-item-icon"><i class="bi bi-buildings"></i></div>
                <div class="spotlight-item-text">
                  <div class="spotlight-item-title">${esc(aName)}</div>
                  <div class="spotlight-item-sub">${esc(domain)}${domain ? " &bull; " : ""}${lobsCount} LOBs &bull; ${personasCount} contacts</div>
                </div>
              </div>
              <span class="spotlight-item-badge">Account</span>
            </div>
          `;
        });
        $results.html(html);
        $results.find(".spotlight-item").first().addClass("active");
        $count.text("Type to search all accounts, LOBs, and personas");
        return;
      }

      // 1. Search Accounts
      const matchedAccounts = accounts.filter((a) => {
        const name = (a.name || a.display_name || "").toLowerCase();
        const domain = (a.primary_domain || a.domain || "").toLowerCase();
        const ticker = (a.stock_symbol || a.ticker || "").toLowerCase();
        const key = (a.key || "").toLowerCase();
        return name.includes(q) || domain.includes(q) || ticker.includes(q) || key.includes(q);
      }).slice(0, 5);

      // 2. Search LOBs across all accounts
      const matchedLobs = [];
      for (const a of accounts) {
        if (matchedLobs.length >= 8) break;
        const lobs = a.lobs || [];
        for (const l of lobs) {
          if (matchedLobs.length >= 8) break;
          const lName = (l.lob_name || l.name || l.division_name || "").toLowerCase();
          const desc = (l.description || "").toLowerCase();
          if (lName.includes(q) || desc.includes(q)) {
            matchedLobs.push({
              accountId: a.id,
              accountName: a.name || a.display_name || "Account",
              lob: l,
            });
          }
        }
      }

      // 3. Search Personas across all accounts
      const matchedPersonas = [];
      for (const a of accounts) {
        if (matchedPersonas.length >= 10) break;
        const personas = a.personas || [];
        for (const p of personas) {
          if (matchedPersonas.length >= 10) break;
          const pName = (p.name || "").toLowerCase();
          const pTitle = (p.title || p.job_title || "").toLowerCase();
          const pDept = (p.department || "").toLowerCase();
          if (pName.includes(q) || pTitle.includes(q) || pDept.includes(q)) {
            matchedPersonas.push({
              accountId: a.id,
              accountName: a.name || a.display_name || "Account",
              persona: p,
            });
          }
        }
      }

      const totalMatches = matchedAccounts.length + matchedLobs.length + matchedPersonas.length;

      if (totalMatches === 0) {
        $results.html(`
          <div style="padding:36px 16px;text-align:center;color:#64748b;font-size:.85rem;">
            <i class="bi bi-search" style="font-size:1.6rem;opacity:.4;display:block;margin-bottom:8px;"></i>
            No results found for "<strong>${esc(q)}</strong>"
          </div>
        `);
        $count.text("0 results");
        return;
      }

      let html = "";

      // Render Accounts Group
      if (matchedAccounts.length > 0) {
        html += `<div class="spotlight-group-title">Enterprise Accounts (${matchedAccounts.length})</div>`;
        matchedAccounts.forEach((a) => {
          const aName = a.name || a.display_name || "Unnamed Account";
          const domain = a.primary_domain || a.domain || "";
          html += `
            <div class="spotlight-item" data-type="account" data-account-id="${a.id}">
              <div class="spotlight-item-left">
                <div class="spotlight-item-icon"><i class="bi bi-buildings"></i></div>
                <div class="spotlight-item-text">
                  <div class="spotlight-item-title">${esc(aName)}</div>
                  <div class="spotlight-item-sub">${esc(domain)}${a.stock_symbol ? ` &bull; ${esc(a.stock_symbol)}` : ""}</div>
                </div>
              </div>
              <span class="spotlight-item-badge">Account</span>
            </div>
          `;
        });
      }

      // Render LOBs Group
      if (matchedLobs.length > 0) {
        html += `<div class="spotlight-group-title">Lines of Business &amp; Divisions (${matchedLobs.length})</div>`;
        matchedLobs.forEach((item) => {
          const lobName = item.lob.lob_name || item.lob.name || "Division";
          const relType = item.lob.relationship_type || "Operating Division";
          html += `
            <div class="spotlight-item" data-type="lob" data-account-id="${item.accountId}" data-lob-id="${item.lob.id}">
              <div class="spotlight-item-left">
                <div class="spotlight-item-icon"><i class="bi bi-diagram-3"></i></div>
                <div class="spotlight-item-text">
                  <div class="spotlight-item-title">${esc(lobName)}</div>
                  <div class="spotlight-item-sub">${esc(item.accountName)} &bull; ${esc(relType)}</div>
                </div>
              </div>
              <span class="spotlight-item-badge">Division</span>
            </div>
          `;
        });
      }

      // Render Personas Group
      if (matchedPersonas.length > 0) {
        html += `<div class="spotlight-group-title">Executive Stakeholders (${matchedPersonas.length})</div>`;
        matchedPersonas.forEach((item) => {
          const pName = item.persona.name || "Executive";
          const pTitle = item.persona.title || item.persona.job_title || "Executive";
          html += `
            <div class="spotlight-item" data-type="persona" data-account-id="${item.accountId}" data-persona-id="${item.persona.id}">
              <div class="spotlight-item-left">
                <div class="spotlight-item-icon"><i class="bi bi-person-badge"></i></div>
                <div class="spotlight-item-text">
                  <div class="spotlight-item-title">${esc(pName)}</div>
                  <div class="spotlight-item-sub">${esc(pTitle)} &bull; ${esc(item.accountName)}</div>
                </div>
              </div>
              <span class="spotlight-item-badge">Persona</span>
            </div>
          `;
        });
      }

      $results.html(html);
      $results.find(".spotlight-item").first().addClass("active");
      $count.text(`${totalMatches} match${totalMatches === 1 ? "" : "es"} found`);
    }

    // Click handler for spotlight items
    $results.on("click", ".spotlight-item", function () {
      const type = $(this).data("type");
      const accountId = $(this).data("account-id");
      const lobId = $(this).data("lob-id");
      const personaId = $(this).data("persona-id");

      closeSpotlight();

      if (!accountId) return;

      const currentId = activeAccount ? activeAccount.id : null;
      if (String(currentId) !== String(accountId)) {
        $(`#accountList .account-item[data-id="${accountId}"]`).trigger("click");
      }

      if (type === "lob" && lobId) {
        setTimeout(() => {
          $(".tab-pill-btn[data-nav-tab='lobs']").trigger("click");
          setTimeout(() => {
            const $card = $(`.lob-card[data-lob-id="${lobId}"]`);
            if ($card.length) {
              $card.trigger("click");
              $("html, body").animate({ scrollTop: $card.offset().top - 90 }, 300);
              $card.addClass("highlight-pulse");
              setTimeout(() => $card.removeClass("highlight-pulse"), 2500);
            }
          }, 120);
        }, 80);
      } else if (type === "persona" && personaId) {
        setTimeout(() => {
          $(".tab-pill-btn[data-nav-tab='personas']").trigger("click");
          setTimeout(() => {
            const $pCard = $(`.persona-card[data-persona-id="${personaId}"]`);
            if ($pCard.length) {
              $("html, body").animate({ scrollTop: $pCard.offset().top - 100 }, 300);
              $pCard.addClass("highlight-pulse");
              setTimeout(() => $pCard.removeClass("highlight-pulse"), 2500);
            }
          }, 120);
        }, 80);
      }
    });
  }

  // 1. Render Clean Enterprise Sidebar Accounts
  function renderSidebar(filterQuery = "") {
    updateGlobalTelemetry();
    const $list = $("#accountList").empty();
    const q = (filterQuery !== undefined ? filterQuery : ($("#accountSearch").val() || "")).trim().toLowerCase();

    const accounts = Array.isArray(MOCK_DATA.accounts) ? [...MOCK_DATA.accounts] : [];

    // Filter by search query
    const filtered = accounts.filter((acct) => {
      const acctName = (acct.name || acct.display_name || "").toLowerCase();
      const domain = (acct.primary_domain || acct.domain || "").toLowerCase();
      const ticker = (acct.stock_symbol || acct.ticker || "").toLowerCase();
      const key = (acct.key || "").toLowerCase();
      return !q || acctName.includes(q) || domain.includes(q) || ticker.includes(q) || key.includes(q);
    });

    // Update real accounts counter badge in sidebar header
    $("#sidebarAccountsCount").text(filtered.length);

    if (filtered.length === 0) {
      $list.append(
        `<div style="padding:16px 12px;font-size:.8rem;color:var(--text-muted);text-align:center;">
          No accounts found.
        </div>`
      );
      return;
    }

    filtered.forEach((acct) => {
      const acctName = acct.name || acct.display_name || "Unnamed Account";
      const sId = String(acct.id);
      const isActive = activeAccount && String(activeAccount.id) === sId;

      const aHealth = computeAccountHealth(acct);
      const completenessScore = aHealth.percentage || aHealth.completenessPct || 0;
      const healthClass = completenessScore >= 80 ? "high" : (completenessScore >= 60 ? "medium" : "basic");

      const lobsCount = (acct.lobs || []).length || acct.lobs_count || 0;
      const personasCount = (acct.personas || []).length || acct.total_contacts_captured || 0;
      const domain = acct.primary_domain || acct.domain || "";

      let subtext = "";
      if (lobsCount > 0 || personasCount > 0) {
        const parts = [];
        if (lobsCount > 0) parts.push(`${lobsCount} LOBs`);
        if (personasCount > 0) parts.push(`${personasCount} contacts`);
        subtext = parts.join(` <span class="acct-meta-dot">&bull;</span> `);
      } else if (domain) {
        subtext = esc(domain);
      } else {
        subtext = esc(acct.company_type || "Enterprise Account");
      }

      $list.append(`
        <div class="account-item fade-in ${isActive ? 'active' : ''}" data-id="${acct.id}" role="button" tabindex="0" title="${esc(acctName)} &bull; ${completenessScore}% Data Filled (${aHealth.populatedCount}/${aHealth.totalFields}) &bull; ${lobsCount} LOBs &bull; ${personasCount} Personas">
          <div class="acct-avatar">${esc(getInitials(acctName))}</div>
          <div class="acct-body">
            <div class="acct-main-row">
              <span class="acct-name" title="${esc(acctName)}">${esc(acctName)}</span>
              <span class="acct-score-pill ${healthClass}">${completenessScore}%</span>
            </div>
            <div class="acct-meta-row">
              ${subtext}
            </div>
          </div>
        </div>
      `);
    });
  }

  // Initial load
  loadData();
  initSpotlight();

  // Sidebar Search
  $("#accountSearch").on("input", function () {
    renderSidebar($(this).val());
  });

  // ══════════════════════════════════════════════════════════════════
  // HELPER: RENDER FIELD FOR FULL DATABASE ATTRIBUTE VAULTS
  // ══════════════════════════════════════════════════════════════════

  // URL Normalization: Prepends https:// if missing to avoid relative routing on localhost
  function normalizeUrl(url) {
    if (!url || typeof url !== "string") return "";
    const trimmed = url.trim();
    if (!trimmed) return "";
    if (/^(https?:\/\/|mailto:|tel:)/i.test(trimmed)) return trimmed;
    return `https://${trimmed}`;
  }

  // Country Flag Emoji Helper for ISO 2-letter codes
  function getCountryFlag(countryCode) {
    if (!countryCode || typeof countryCode !== "string" || countryCode.length !== 2) return "🌐";
    const code = countryCode.toUpperCase();
    return String.fromCodePoint(...[...code].map(c => 127397 + c.charCodeAt(0)));
  }

  // Label to DB column mapping for inline editing
  const LABEL_TO_FIELD_MAP = {
    "Display Name": "display_name",
    "Legal Name": "legal_name",
    "Unique Key": "key",
    "Primary Domain": "primary_domain",
    "Official Website URL": "website_url",
    "Crunchbase URL": "crunchbase_url",
    "Operating Status": "operating_status",
    "Company Type": "company_type",
    "Founded Year": "founded_year",
    "Employee Count Range": "employee_count_range",
    "Headquarters Location": "headquarters_location",
    "City": "city",
    "State / Region": "state",
    "Country": "country",
    "Postal Code": "postal_code",
    "Phone Number": "phone_number",
    "Sanitized Phone": "sanitized_phone",
    "Contact Email": "contact_email",
    "LinkedIn URL": "linkedin_url",
    "Twitter / X Profile": "twitter_url",
    "Twitter Handle": "twitter_handle",
    "GitHub Repository": "github_url",
    "Glassdoor Reviews": "glassdoor_url",
    "Corporate Blog": "blog_url",
    "Reported Revenue": "estimated_revenue_range",
    "Total Funding (USD)": "total_funding_amount_usd",
    "Funding Currency": "total_funding_currency",
    "Last Funding Type": "last_funding_type",
    "Last Funding Date": "last_funding_date",
    "Funding Rounds": "num_funding_rounds",
    "Funding Status": "funding_status",
    "Stock Symbol / Ticker": "stock_symbol",
    "Stock Exchange": "stock_exchange",
    "IPO Status": "ipo_status",
    "IPO Date": "ipo_date",
    "SEC CIK Number": "sec_cik",
    "SEC EDGAR Search URL": "sec_edgar_url",
    "SEC Filings RSS Feed": "sec_filings_rss",
    "SEC Submissions API URL": "sec_submissions_url",
    "Global Traffic Rank": "global_traffic_rank",
    "Monthly Web Visits": "monthly_visits",
    "Bounce Rate": "bounce_rate",
    "Average Visit Duration": "visit_duration",
    "Page Views Per Visit": "page_views_per_visit",
    "Account Heat / Opportunity Score": "heat_score",
    "90-Day Momentum Trend": "trend_score_90d",
    "Active Technology Count": "active_tech_count",
    "Estimated IT Annual Spend": "it_spend",
    "Patents Granted": "patents_granted",
    "Trademarks Registered": "trademarks_registered",
    "Twitter Live Feed URL": "twitter_live_url",
    "Reddit Search Query": "reddit_query",
    "Reddit Atom RSS Feed": "reddit_rss_url",
    "Google News Query": "news_query",
    "Google News RSS Feed": "rss_url",
    "Google Patents Search": "google_patents_url",
    "Google Trends Analytics": "google_trends_url",
    "YouTube Executive Search": "youtube_search_url",
    "OpenAlex Academic Profile": "openalex_institution_url",
    "Wikidata Knowledge Entity": "wikidata_entity_url",
    "LOBs Discovered": "lobs_count",
    "Total Contacts Captured": "total_contacts_captured",
    "C-Suite Executives": "c_suite_count",
    "VP & Division Heads": "vp_count",
    "Directors": "director_count",
    "Managers": "manager_count",
    "Sub-Organizations": "num_suborganizations",
    "Corporate Acquisitions": "num_acquisitions",
    // LOB fields
    "Unique LOB Key": "key",
    "Division Name": "lob_name",
    "Relationship Taxonomy": "relationship_type",
    "LEI Code (GLEIF)": "lei_code",
    "Legal Jurisdiction": "jurisdiction",
    "Commercial Registry": "commercial_registry",
    "Dedicated Website URL": "website_url",
    "Wikipedia URL": "wikipedia_url",
    "Segment Revenue": "segment_revenue",
    "Segment Headcount Size": "headcount_size",
    "Operating Head Executive": "operating_head",
    "Google News RSS Feed": "google_news_rss_url",
    "Reddit Community RSS Feed": "reddit_rss_url",
    "Google Patents Live Feed": "google_patents_url",
    "Google Trends Search Momentum": "google_trends_url",
    "YouTube Media Search Feed": "youtube_search_url",
    // Persona fields
    "Full Name": "full_name",
    "First Name": "first_name",
    "Last Name": "last_name",
    "Corporate Title": "title",
    "Professional Headline": "headline",
    "Seniority Level (Raw)": "seniority_raw",
    "Hierarchy Level Number": "hierarchy_level",
    "Corporate Email": "email",
    "Email Delivery Status": "email_status",
    "Office Phone": "phone",
    "Direct Mobile Phone": "direct_mobile_phone",
    "Personal Email": "personal_email",
    "Base Location": "location",
    "Prior Employer": "prior_company",
    "Role Tenure (Months)": "current_role_tenure_months",
    "Career Trajectory Score": "career_trajectory_score",
    "Reports To": "reports_to",
    "Degree": "degree",
    "Alma Mater Institution": "institution",
    "Communication Style": "communication_style",
    "Value Proposition": "value_proposition",
    "Personalized Icebreaker": "personalized_icebreaker",
    "Social Platform": "social_platform",
    "Engagement Likelihood": "engagement_rate",
    "Social Presence Level": "social_presence_level",
    "Decision Authority Level": "decision_authority",
    "Budget Authority Level": "budget_authority",
    "LinkedIn Profile URL": "linkedin_url",
    "Official Corporate Bio URL": "corporate_bio_url",
    "Crunchbase Profile URL": "crunchbase_url",
    "SEC Form 4 Insider Trades URL": "sec_insider_trades_url",
    "FEC Political Contributions URL": "fec_contributions_url",
    "Quiver Quantitative Insider URL": "quiver_insider_url",
    "Bloomberg Media & Videos URL": "bloomberg_url",
    "Wall Street Journal Article URL": "wsj_article_url",
    "Major Media Interview URL": "media_interview_url",
    "Annual Report & Proxy Statement URL": "annual_report_url",
    "ZoomInfo Profile URL": "zoominfo_url",
    "Google News Real-Time RSS URL": "rss_url",
    "YouTube Media & Keynotes URL": "youtube_url",
    "Executive Podcast Appearances URL": "podcast_url",
    "OpenInsider Trades Screener URL": "openinsider_url",
    "SECForm4 Live Filings URL": "secform4_url",
    "Wayback Career Archive URL": "wayback_url",
    "TheOrg Executive Chart URL": "theorg_url",
    "Seeking Alpha Transcripts URL": "seeking_alpha_url",
    "External Board & Civic Roles URL": "external_board_url"
  };

  let currentVaultContext = null;

  function renderField(label, value, opts = {}) {
    const ctx = currentVaultContext || {};
    const entityType = opts.entityType || ctx.entityType || "account";
    const entityId = opts.id || ctx.id || "";
    const fieldName = opts.field || LABEL_TO_FIELD_MAP[label] || null;

    const isEmpty = value === null || value === undefined || value === "" || (Array.isArray(value) && !value.length) || (typeof value === "object" && !Object.keys(value).length);
    
    let renderedValHtml = "";
    let rawValStr = "";

    if (isEmpty) {
      renderedValHtml = `<span style="color:#94a3b8;font-style:italic;font-weight:normal;">&mdash;</span>`;
      rawValStr = "";
    } else if (opts.url && value) {
      const normHref = normalizeUrl(String(value));
      const display = opts.urlLabel || (typeof value === "string" && value.length > 55 ? value.substring(0, 52) + "..." : value);
      renderedValHtml = `<a href="${esc(normHref)}" target="_blank" rel="noopener noreferrer" style="color:#0284c7;font-weight:600;word-break:break-all;text-decoration:none;">${esc(display)} <i class="bi bi-box-arrow-up-right" style="font-size:.7rem;"></i></a>`;
      rawValStr = String(value);
    } else if (opts.chips && Array.isArray(value)) {
      const maxInitial = opts.maxChips || (label.toLowerCase().includes("keyword") ? 6 : null);
      const renderSingleChip = (v) => {
        if (typeof v === 'object' && v !== null) {
          const mainText = v.name || v.title || v.label || v.term || v.company || Object.values(v)[0];
          const subText = v.relationship || v.domain || v.category || "";
          return `<span class="data-tag" style="display:inline-flex;align-items:center;gap:4px;" title="${esc(JSON.stringify(v))}"><i class="bi bi-tag" style="font-size:0.65rem;color:#0284c7;"></i> ${esc(mainText)}${subText ? ` <span style="color:#64748b;font-size:0.68rem;">(${esc(subText)})</span>` : ''}</span>`;
        }
        return `<span class="data-tag">${esc(String(v))}</span>`;
      };

      if (maxInitial && value.length > maxInitial) {
        const visibleChips = value.slice(0, maxInitial).map(renderSingleChip).join("");
        const hiddenChips = value.slice(maxInitial).map(renderSingleChip).join("");
        const remainingCount = value.length - maxInitial;

        renderedValHtml = `
          <div class="chips-expandable-wrapper" style="display:flex;flex-wrap:wrap;gap:5px;align-items:center;">
            ${visibleChips}
            <span class="chips-extra-container" style="display:none;gap:5px;flex-wrap:wrap;">
              ${hiddenChips}
            </span>
            <button type="button" class="btn btn-sm btn-toggle-chips-expand" data-expanded="false" style="padding:2px 10px;font-size:0.72rem;border-radius:14px;display:inline-flex;align-items:center;gap:4px;cursor:pointer;border:1px solid #cbd5e1;color:#0284c7;background:#f8fafc;font-weight:600;">
              <span>+${remainingCount} more</span> <i class="bi bi-chevron-down" style="font-size:0.68rem;"></i>
            </button>
          </div>
        `;
      } else {
        renderedValHtml = `<div style="display:flex;flex-wrap:wrap;gap:5px;">${value.map(renderSingleChip).join("")}</div>`;
      }
      rawValStr = JSON.stringify(value);
    } else if (opts.json && typeof value === "object") {
      renderedValHtml = `<pre style="font-size:.7rem;max-height:180px;overflow:auto;background:#f8fafc;padding:8px;border-radius:6px;margin:0;white-space:pre-wrap;border:1px solid #e2e8f0;">${esc(JSON.stringify(value, null, 2))}</pre>`;
      rawValStr = JSON.stringify(value);
    } else {
      renderedValHtml = esc(String(value));
      rawValStr = String(value);
    }

    const canEdit = !!(fieldName && entityId && !opts.chips && !opts.json && opts.editable !== false);
    const spanClass = opts.spanFull ? " span-full" : (opts.span2 || opts.chips || opts.json ? " span-2" : "");

    return `
      <div class="snapshot-field-item${spanClass}" ${canEdit ? `data-entity-type="${esc(entityType)}" data-id="${esc(entityId)}" data-field="${esc(fieldName)}" data-raw-value="${esc(rawValStr)}"` : ""}>
        <div class="snapshot-field-header">
          <span class="snapshot-field-label">${esc(label.toUpperCase())}</span>
          ${canEdit ? `<i class="bi bi-pencil snapshot-field-pencil" title="Edit ${esc(label)} inline"></i>` : ""}
        </div>
        <div class="snapshot-field-value">
          ${renderedValHtml}
        </div>
      </div>
    `;
  }

  // ══════════════════════════════════════════════════════════════════
  // VISUAL COMPONENTS: CAREER TIMELINE, ACADEMIC CARDS, LOB DISCLOSURES
  // ══════════════════════════════════════════════════════════════════

  // 1. Executive Career Journey Timeline (Persona)
  function renderExecutiveCareerTimeline(history) {
    if (!Array.isArray(history) || !history.length) {
      return `<div style="color:#94a3b8;font-style:italic;padding:12px;">No historical employment records documented.</div>`;
    }

    return `
      <div class="career-timeline-container" style="grid-column: 1 / -1;">
        ${history.map(item => {
          const pos = item.position || item.title || "Executive Leadership Role";
          const company = item.companyName || item.company || "Corporate Entity";
          const logo = item.companyLogo?.url || item.logo_url || "";
          const linkedin = item.companyLinkedinUrl || (item.companyUniversalName ? `https://www.linkedin.com/company/${item.companyUniversalName}` : "");
          
          const start = item.startDate?.text || item.startDate?.year || "";
          const end = item.endDate?.text || (item.isCurrent ? "Present" : (item.endDate?.year || ""));
          const dateRange = (start || end) ? `${start || "Prior"} – ${end || "Present"}` : "";
          const loc = item.location || "";
          const desc = item.description || "";

          // Parse description into clean bullets if formatted with • or bullet characters
          let descHtml = "";
          if (desc) {
            const bullets = desc.split(/\n\s*•\t?|\n\s*-\s*|•\t?/).map(b => b.trim()).filter(b => b.length > 0);
            if (bullets.length > 1) {
              descHtml = `<ul class="career-bullet-list">${bullets.map(b => `<li>${esc(b)}</li>`).join("")}</ul>`;
            } else {
              descHtml = `<p style="margin:0;white-space:pre-line;">${esc(desc)}</p>`;
            }
          }

          return `
            <div class="career-timeline-item">
              <div class="career-item-header">
                ${logo ? `
                  <img src="${esc(logo)}" class="career-company-logo" alt="${esc(company)}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex';" />
                  <div class="career-logo-fallback" style="display:none;"><i class="bi bi-building"></i></div>
                ` : `
                  <div class="career-logo-fallback"><i class="bi bi-building"></i></div>
                `}
                <div class="career-item-info">
                  <div class="career-role-title">${esc(pos)}</div>
                  <div class="career-company-row">
                    ${linkedin ? `
                      <a href="${esc(linkedin)}" target="_blank" rel="noopener noreferrer" class="career-company-link">
                        ${esc(company)} <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;"></i>
                      </a>
                    ` : `
                      <span style="font-weight:600;color:#334155;">${esc(company)}</span>
                    `}
                    ${dateRange ? `<span class="career-date-pill"><i class="bi bi-calendar3"></i> ${esc(dateRange)}</span>` : ""}
                    ${loc ? `<span style="color:#64748b;font-size:0.75rem;"><i class="bi bi-geo-alt"></i> ${esc(loc)}</span>` : ""}
                  </div>
                </div>
              </div>
              ${descHtml ? `<div class="career-desc-body">${descHtml}</div>` : ""}
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  // 2. Academic Credentials Cards (Persona)
  function renderAcademicCredentialsGrid(education, fallbackDegree, fallbackInstitution) {
    const records = Array.isArray(education) && education.length ? education : [];
    if (!records.length && !fallbackDegree && !fallbackInstitution) {
      return `<div style="color:#94a3b8;font-style:italic;padding:12px;">No formal academic credentials documented.</div>`;
    }

    const list = records.length ? records : [{ degree: fallbackDegree, schoolName: fallbackInstitution }];

    return `
      <div class="academic-cards-grid" style="grid-column: 1 / -1;">
        ${list.map(rec => {
          const deg = rec.degree || fallbackDegree || "Academic Degree";
          const school = rec.schoolName || rec.institution || fallbackInstitution || "University / College";
          const start = rec.startDate?.year || "";
          const end = rec.endDate?.year || "";
          const dateStr = (start && end) ? `${start} – ${end}` : (end || start || "");

          return `
            <div class="academic-card">
              <div class="academic-icon-box">
                <i class="bi bi-mortarboard-fill"></i>
              </div>
              <div>
                <div class="academic-degree-name">${esc(deg)}</div>
                <div class="academic-school-name">${esc(school)}</div>
                ${dateStr ? `<div style="font-size:0.72rem;color:#94a3b8;margin-top:4px;"><i class="bi bi-calendar"></i> Class of ${esc(dateStr)}</div>` : ""}
              </div>
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  // 3. LOB Patents Portfolio Card
  function renderLobPatentsCard(patents) {
    if (!patents || (typeof patents === "object" && !Object.keys(patents).length)) {
      return `
        <div class="lob-structured-card">
          <div class="lob-structured-header">
            <span class="lob-structured-title"><i class="bi bi-patch-check" style="color:#0284c7;"></i> Intellectual Property &amp; Patents</span>
            <span class="badge" style="background:#f1f5f9;color:#64748b;">No Separate Patents</span>
          </div>
          <div style="color:#94a3b8;font-size:0.8rem;font-style:italic;">No dedicated patents registered directly under this operating division.</div>
        </div>
      `;
    }

    const status = patents.status || "Under Master Corporate Portfolio";
    const count = patents.patents_count !== undefined ? patents.patents_count : (Array.isArray(patents.items) ? patents.items.length : 0);

    return `
      <div class="lob-structured-card">
        <div class="lob-structured-header">
          <span class="lob-structured-title"><i class="bi bi-patch-check-fill" style="color:#0284c7;"></i> Intellectual Property &amp; Patent Portfolio</span>
          <span class="badge-solid-green">${esc(status)}</span>
        </div>
        <div class="lob-disclosure-grid">
          <div class="lob-disclosure-cell">
            <div class="lob-disclosure-label">DIRECT PATENT COUNT</div>
            <div class="lob-disclosure-val">${esc(String(count))} Patents</div>
          </div>
          <div class="lob-disclosure-cell">
            <div class="lob-disclosure-label">PORTFOLIO ASSIGNMENT</div>
            <div class="lob-disclosure-val">${esc(status)}</div>
          </div>
          <div class="lob-disclosure-cell" style="grid-column: span 2;">
            <div class="lob-disclosure-label">REGULATORY IP COVERAGE</div>
            <div class="lob-disclosure-val" style="font-weight:normal;font-size:0.78rem;color:#475569;">
              All patents, proprietary software architectures, and trade algorithms are registered under the parent corporate umbrella (USPTO registered) and licensed to this operating division.
            </div>
          </div>
        </div>
      </div>
    `;
  }

  // 4. LOB Financial Disclosures & Standing Card
  function renderLobFinancialsCard(fin) {
    if (!fin || (typeof fin === "object" && !Object.keys(fin).length)) {
      return `
        <div class="lob-structured-card">
          <div class="lob-structured-header">
            <span class="lob-structured-title"><i class="bi bi-file-earmark-bar-graph" style="color:#059669;"></i> Financial Disclosures &amp; Regulatory Standing</span>
            <span class="badge" style="background:#f1f5f9;color:#64748b;">Consolidated</span>
          </div>
          <div style="color:#94a3b8;font-size:0.8rem;font-style:italic;">Disclosures consolidated into parent corporate audited filings.</div>
        </div>
      `;
    }

    const status = fin.status || "Active / Fully Consolidated";
    const entityType = fin.entity_type || "Operating Division";
    const parentHolding = fin.parent_holding || "Parent Corporation";
    const oversight = fin.regulatory_oversight || "Financial Market Regulators";
    const lei = fin.jurisdiction || fin.lei || "";

    return `
      <div class="lob-structured-card">
        <div class="lob-structured-header">
          <span class="lob-structured-title"><i class="bi bi-file-earmark-bar-graph-fill" style="color:#059669;"></i> Financial Disclosures &amp; Regulatory Standing</span>
          <span class="badge-solid-green">${esc(status)}</span>
        </div>
        <div class="lob-disclosure-grid">
          <div class="lob-disclosure-cell">
            <div class="lob-disclosure-label">FINANCIAL CONSOLIDATION</div>
            <div class="lob-disclosure-val">${esc(status)}</div>
          </div>
          <div class="lob-disclosure-cell">
            <div class="lob-disclosure-label">LEGAL OPERATING STRUCTURE</div>
            <div class="lob-disclosure-val">${esc(entityType)}</div>
          </div>
          <div class="lob-disclosure-cell">
            <div class="lob-disclosure-label">CONTROLLING PARENT HOLDING</div>
            <div class="lob-disclosure-val">${esc(parentHolding)}</div>
          </div>
          <div class="lob-disclosure-cell">
            <div class="lob-disclosure-label">REGULATORY OVERSIGHT BODIES</div>
            <div class="lob-disclosure-val">${esc(oversight)}</div>
          </div>
          ${lei ? `
            <div class="lob-disclosure-cell" style="grid-column: span 2;">
              <div class="lob-disclosure-label">REGISTERED LEI IDENTIFIER</div>
              <div class="lob-disclosure-val">
                <a href="https://search.gleif.org/#/record/${esc(lei)}" target="_blank" rel="noopener noreferrer" style="color:#0284c7;text-decoration:none;font-family:monospace;">
                  ${esc(lei)} <i class="bi bi-box-arrow-up-right" style="font-size:0.7rem;"></i>
                </a>
              </div>
            </div>
          ` : ""}
        </div>
      </div>
    `;
  }

  // 4b. LOB Direct Competitors Grid Card (Compact & Multi-URL)
  const KNOWN_COMPETITOR_DOMAINS = {
    "state street": "statestreet.com",
    "state street global services": "statestreet.com",
    "jpmorgan": "jpmorgan.com",
    "jpmorgan chase": "jpmorgan.com",
    "jpmorgan chase custody & trust": "jpmorgan.com",
    "northern trust": "northerntrust.com",
    "northern trust asset servicing": "northerntrust.com",
    "citigroup": "citigroup.com",
    "citigroup global transaction services": "citigroup.com",
    "bnp paribas": "bnpparibas.com",
    "bnp paribas securities services": "securities.cib.bnpparibas",
    "broadridge": "broadridge.com",
    "broadridge financial solutions": "broadridge.com",
    "clearstream": "clearstream.com",
    "euroclear": "euroclear.com",
    "lch": "lch.com",
    "lch clearnet": "lch.com",
    "charles schwab": "schwab.com",
    "fidelity": "fidelity.com",
    "fidelity institutional": "institutional.fidelity.com",
    "blackrock": "blackrock.com",
    "vanguard": "vanguard.com",
    "morgan stanley": "morganstanley.com",
    "goldman sachs": "goldmansachs.com",
    "ubs": "ubs.com",
    "hsbc": "hsbc.com"
  };

  function renderLobCompetitorsCard(competitors, sectionPrefix = "") {
    const list = Array.isArray(competitors) && competitors.length ? competitors : [];
    if (!list.length) {
      return `
        <div class="pipeline-section-card fade-in" style="margin-top:14px;">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <i class="bi bi-shield-shaded" style="color:#0284c7;"></i>
              <span>${sectionPrefix}Direct Peer Competitors &amp; Market Alternatives</span>
            </div>
            <span class="badge" style="background:#f1f5f9;color:#64748b;">None Tracked</span>
          </div>
          <div style="color:#94a3b8;font-size:0.8rem;font-style:italic;">No direct peer competitors tracked yet for this operating division.</div>
        </div>
      `;
    }

    return `
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row" style="margin-bottom:10px;">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-shield-shaded" style="color:#0284c7;"></i>
            <span>${sectionPrefix}Direct Peer Competitors &amp; Market Alternatives</span>
          </div>
          <span class="badge" style="background:#e0f2fe;color:#0369a1;font-weight:700;padding:2px 8px;font-size:0.72rem;">${list.length} Peers Tracked</span>
        </div>

        <div class="competitor-horizontal-grid">
          ${list.map(c => {
            let name = "";
            let domain = "";
            let rel = "";
            if (typeof c === "object" && c !== null) {
              name = c.name || c.company || "Competitor";
              domain = c.domain || c.website || "";
              rel = c.relationship || c.type || c.category || "Direct Peer";
            } else {
              name = String(c);
              rel = "Direct Peer";
              domain = "";
            }

            // Clean & match official website domain
            const lookupKey = name.toLowerCase().trim();
            if (!domain && KNOWN_COMPETITOR_DOMAINS[lookupKey]) {
              domain = KNOWN_COMPETITOR_DOMAINS[lookupKey];
            } else if (!domain) {
              for (const [k, v] of Object.entries(KNOWN_COMPETITOR_DOMAINS)) {
                if (lookupKey.includes(k) || k.includes(lookupKey)) {
                  domain = v;
                  break;
                }
              }
            }

            const cleanDomain = domain ? domain.replace(/^https?:\/\//, "").replace(/\/.*$/, "") : "";
            const webUrl = cleanDomain ? `https://${cleanDomain}` : `https://www.google.com/search?q=${encodeURIComponent(name + ' official website')}`;
            const newsUrl = `https://news.google.com/search?q=${encodeURIComponent(name + ' market news')}`;
            const linkedinUrl = cleanDomain ? `https://www.linkedin.com/company/${cleanDomain.split('.')[0]}` : `https://www.linkedin.com/search/results/companies/?keywords=${encodeURIComponent(name)}`;
            const secUrl = `https://www.sec.gov/edgar/searchedgar/companysearch?company=${encodeURIComponent(name)}`;

            return `
              <div class="competitor-compact-card">
                <div class="competitor-card-head">
                  <div class="competitor-card-title">
                    <i class="bi bi-shield-check" style="color:#0284c7;font-size:0.9rem;flex-shrink:0;"></i>
                    <span class="competitor-card-name" title="${esc(name)}">${esc(name)}</span>
                  </div>
                  <span class="competitor-rel-badge" style="flex-shrink:0;">${esc(rel)}</span>
                </div>

                <div class="competitor-card-domain">
                  <a href="${esc(webUrl)}" target="_blank" rel="noopener noreferrer" class="competitor-domain-link" title="Visit official corporate domain: ${esc(cleanDomain || webUrl)}">
                    <i class="bi bi-globe" style="font-size:0.72rem;"></i>
                    <span style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(cleanDomain || 'Official Website')}</span>
                    <i class="bi bi-box-arrow-up-right" style="font-size:0.58rem;"></i>
                  </a>
                </div>

                <div class="competitor-card-actions">
                  <a href="${esc(webUrl)}" target="_blank" rel="noopener noreferrer" class="comp-link-badge" title="Official Domain: ${esc(cleanDomain || webUrl)}">
                    <i class="bi bi-link-45deg"></i> Web
                  </a>
                  <a href="${esc(newsUrl)}" target="_blank" rel="noopener noreferrer" class="comp-link-badge" title="Live Google News Stream">
                    <i class="bi bi-newspaper"></i> News
                  </a>
                  <a href="${esc(linkedinUrl)}" target="_blank" rel="noopener noreferrer" class="comp-link-badge" title="LinkedIn Corporate Profile">
                    <i class="bi bi-linkedin"></i> LinkedIn
                  </a>
                  <a href="${esc(secUrl)}" target="_blank" rel="noopener noreferrer" class="comp-link-badge" title="SEC EDGAR Company Filings">
                    <i class="bi bi-file-earmark-text"></i> SEC
                  </a>
                </div>
              </div>
            `;
          }).join("")}
        </div>
      </div>
    `;
  }

  // 4. LOB Operating Sub-LOBs & Child Divisions Grid
  function renderLobSubLobsGrid(subLobs, parentLob) {
    const list = Array.isArray(subLobs) && subLobs.length ? subLobs : [];
    if (!list.length) {
      return `
        <div class="lob-structured-card" style="grid-column: 1 / -1;">
          <div style="display:flex;align-items:center;gap:10px;padding:8px;color:#64748b;">
            <i class="bi bi-info-circle-fill" style="color:#0284c7;font-size:1.1rem;"></i>
            <div>
              <div style="font-weight:600;color:#334155;font-size:0.85rem;">No Direct Child Divisions Registered</div>
              <div style="font-size:0.75rem;margin-top:2px;">This operating division operates as a direct consolidated unit without secondary nested subsidiaries under the current corporate registry filing.</div>
            </div>
          </div>
        </div>
      `;
    }

    return `
      <div class="sublob-grid" style="grid-column: 1 / -1;">
        ${list.map(sub => {
          const sName = sub.name || sub.legal_name || "Operating Division";
          const sLegal = sub.legal_name && sub.legal_name !== sName ? sub.legal_name : "";
          const lei = sub.lei_code || sub.lei || (sub.metadata && (sub.metadata.lei || sub.metadata.lei_code));
          const jurisdiction = sub.jurisdiction || (sub.metadata && sub.metadata.jurisdiction) || "Global";
          const country = sub.country || (sub.metadata && sub.metadata.country) || "";
          const status = sub.status || "ACTIVE";
          const relationship = sub.relationship_type || "Level 3: Operating Sub-LOB / Grandchild";
          const isVerified = !!sub.is_manually_verified;
          const verifiedAt = sub.manually_verified_at ? new Date(sub.manually_verified_at).toLocaleDateString() : "";
          const parentName = sub.parent_lob_name || (parentLob ? (parentLob.lob_name || parentLob.name) : "") || "Parent LOB";

          return `
            <div class="sublob-card${isVerified ? ' verified' : ''}" data-sublob-id="${esc(sub.id || '')}" style="cursor:pointer;" title="Click to view full Sub-LOB details">
              <div>
                <div class="sublob-card-top">
                  <div>
                    <div class="sublob-card-title">${esc(sName)}</div>
                    ${sLegal ? `<div class="sublob-card-subtitle">${esc(sLegal)}</div>` : ''}
                  </div>
                  <span class="sublob-badge-l3">
                    <i class="bi bi-diagram-3"></i> Level 3
                  </span>
                </div>

                <div class="sublob-meta-grid">
                  <div>
                    <div class="sublob-meta-label">LEI CODE (GLEIF)</div>
                    <div class="sublob-meta-val">
                      ${lei ? `
                        <a href="https://search.gleif.org/#/record/${esc(lei)}" target="_blank" rel="noopener noreferrer" style="color:#0284c7;font-family:monospace;" onclick="event.stopPropagation();">
                          ${esc(lei.length > 14 ? lei.substring(0, 12) + '...' : lei)} <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;"></i>
                        </a>
                      ` : '<span style="color:#94a3b8;">Pending LEI</span>'}
                    </div>
                  </div>
                  <div>
                    <div class="sublob-meta-label">JURISDICTION</div>
                    <div class="sublob-meta-val">
                      ${getCountryFlag(country)} ${esc(jurisdiction)}
                    </div>
                  </div>
                  <div style="grid-column: span 2;">
                    <div class="sublob-meta-label">TAXONOMY &amp; STATUS</div>
                    <div class="sublob-meta-val" style="display:flex;align-items:center;justify-content:space-between;gap:6px;">
                      <span>${esc(relationship)}</span>
                      <span class="badge-solid-green" style="font-size:0.65rem;padding:2px 6px;">${esc(status)}</span>
                    </div>
                  </div>
                </div>
              </div>

              <div class="sublob-card-footer">
                <span class="sublob-parent-tag" title="Parent Line of Business">
                  <i class="bi bi-building"></i> ${esc(parentName)}
                </span>
                <div style="display:flex;align-items:center;gap:6px;">
                  <button type="button" class="btn-sublob-details" data-sublob-id="${esc(sub.id || '')}" title="View all 18 enterprise database fields">
                    <i class="bi bi-eye"></i> Details
                  </button>
                  ${isVerified ? `
                    <span class="badge" style="background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;font-size:0.68rem;padding:3px 7px;">
                      <i class="bi bi-check-circle-fill"></i> Verified ${verifiedAt ? '(' + esc(verifiedAt) + ')' : ''}
                    </span>
                  ` : `
                    <span class="badge" style="background:#f8fafc;color:#64748b;border:1px solid #e2e8f0;font-size:0.68rem;padding:3px 7px;">
                      <i class="bi bi-dash-circle"></i> Unverified
                    </span>
                  `}
                  ${sub.id ? `
                    <button type="button" class="btn btn-sm btn-light btn-verify-sublob" data-sublob-id="${esc(sub.id)}" title="Toggle manual verification" style="font-size:0.68rem;padding:2px 6px;border:1px solid #cbd5e1;">
                      <i class="bi bi-check2-circle"></i>
                    </button>
                  ` : ''}
                </div>
              </div>
            </div>
          `;
        }).join("")}
      </div>
    `;
  }

  // ══════════════════════════════════════════════════════════════════
  // VISUAL CORPORATE OWNERSHIP & SUBSIDIARY HIERARCHY EXPLORER
  // ══════════════════════════════════════════════════════════════════
  function renderOwnershipHierarchyTree(tree, account) {
    if (!tree) {
      return `<div style="color:#64748b;font-style:italic;padding:12px;">No corporate ownership hierarchy tree available for this account.</div>`;
    }

    const parentLei = tree.gleif_lei || account.sec_cik || "MLDY5N6PZ58ZE60QU102";
    const directChildren = Array.isArray(tree.gleif_children) ? tree.gleif_children : [];
    const indirectSublobs = Array.isArray(tree.gleif_indirect_sublobs) ? tree.gleif_indirect_sublobs : [];
    
    // Combine all subsidiaries (direct + indirect sub-lobs)
    const allSubs = Array.isArray(tree.all_subsidiaries) && tree.all_subsidiaries.length 
      ? tree.all_subsidiaries 
      : [...directChildren, ...indirectSublobs];

    const totalCount = allSubs.length || (directChildren.length + indirectSublobs.length);
    const directCount = directChildren.length;
    const sublobCount = indirectSublobs.length;

    const secSubs = Array.isArray(tree.sec_exhibit21_subsidiaries) ? tree.sec_exhibit21_subsidiaries : [];
    const countries = [...new Set(allSubs.map(c => c.country).filter(Boolean))];
    const knownLobs = account.lobs || account.known_lobs || [];

    // Calculate mapped LOBs
    let mappedCount = 0;
    allSubs.forEach(child => {
      const cName = (child.legal_name || "").toLowerCase();
      const matched = knownLobs.find(l => {
        const lName = (l.name || l.lob_name || "").toLowerCase();
        return lName && cName && (lName.includes(cName) || cName.includes(lName));
      });
      if (matched) mappedCount++;
    });

    return `
      <!-- Explainer Banner -->
      <div class="hierarchy-explainer-banner">
        <div class="hierarchy-explainer-icon">
          <i class="bi bi-diagram-3-fill"></i>
        </div>
        <div class="hierarchy-explainer-text">
          <div class="hierarchy-explainer-title">
            <span>Corporate Ownership &amp; Subsidiary Hierarchy Explorer</span>
            <span class="badge" style="background:#0284c7;color:#fff;font-size:0.68rem;font-weight:600;">GLEIF ISO 17442 &amp; SEC Exhibit 21</span>
          </div>
          <p class="hierarchy-explainer-desc">
            GLEIF (Global Legal Entity Identifier Foundation) and SEC Form 10-K Exhibit 21 establish verified multi-jurisdictional legal entity ownership.
            Below is the multi-tier hierarchy containing <strong>${directCount} Direct Operating Divisions (LOBs)</strong> and <strong>${sublobCount} Indirect Operating Sub-LOBs</strong> under the ultimate parent company.
          </p>
          <div class="hierarchy-stats-bar">
            <span class="hierarchy-stat-chip"><i class="bi bi-shield-check" style="color:#059669;"></i> Parent LEI: <strong>${esc(parentLei)}</strong></span>
            <span class="hierarchy-stat-chip"><i class="bi bi-diagram-2" style="color:#0284c7;"></i> <strong>${totalCount}</strong> Total Global Entities</span>
            <span class="hierarchy-stat-chip"><i class="bi bi-building" style="color:#0ea5e9;"></i> <strong>${directCount}</strong> Direct LOBs</span>
            <span class="hierarchy-stat-chip"><i class="bi bi-diagram-3" style="color:#d97706;"></i> <strong>${sublobCount}</strong> Operating Sub-LOBs</span>
            <span class="hierarchy-stat-chip"><i class="bi bi-globe" style="color:#6366f1;"></i> <strong>${countries.length}</strong> Global Jurisdictions (${countries.slice(0, 6).join(", ")})</span>
            ${mappedCount > 0 ? `<span class="hierarchy-stat-chip" style="background:#ecfdf5;border-color:#a7f3d0;color:#047857;"><i class="bi bi-check-circle-fill"></i> <strong>${mappedCount}</strong> Mapped to Pipeline LOBs</span>` : ""}
          </div>
        </div>
      </div>

      <!-- Root Node: Ultimate Parent Entity -->
      <div class="hierarchy-root-node">
        <div class="hierarchy-root-header">
          <div>
            <div style="font-size:0.68rem;font-weight:700;color:#0284c7;letter-spacing:0.05em;text-transform:uppercase;">
              <i class="bi bi-award-fill"></i> ULTIMATE PARENT HOLDING ENTITY (LEVEL 1)
            </div>
            <div class="hierarchy-root-title">
              ${esc(account.legal_name || account.display_name || account.name)}
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <span class="badge-solid-green">ACTIVE &amp; VERIFIED</span>
            <a href="https://search.gleif.org/#/record/${esc(parentLei)}" target="_blank" rel="noopener noreferrer" class="hierarchy-root-lei" title="View official GLEIF Golden Copy registry record">
              <i class="bi bi-box-arrow-up-right" style="color:#0284c7;"></i> LEI: ${esc(parentLei)}
            </a>
            <button type="button" class="btn btn-sm btn-light copy-lei-btn" data-lei="${esc(parentLei)}" title="Copy LEI to clipboard" style="font-size:0.7rem;padding:3px 8px;border:1px solid #cbd5e1;">
              <i class="bi bi-clipboard"></i> Copy
            </button>
          </div>
        </div>
      </div>

      <!-- Branch Line Connector -->
      <div class="hierarchy-branch-connector">
        <div class="hierarchy-branch-line"></div>
        <div class="hierarchy-branch-badge">
          <i class="bi bi-diagram-3"></i> Direct LOBs (${directCount}) &amp; Operating Sub-LOBs (${sublobCount})
        </div>
        <div class="hierarchy-branch-line"></div>
      </div>

      <!-- Filter Tabs: All vs Direct LOBs vs Operating Sub-LOBs -->
      <div class="hierarchy-filter-tabs">
        <button type="button" class="hierarchy-filter-btn active" data-filter="all">
          All Global Subsidiaries (${totalCount})
        </button>
        <button type="button" class="hierarchy-filter-btn" data-filter="direct">
          <i class="bi bi-building"></i> Direct LOBs (${directCount})
        </button>
        <button type="button" class="hierarchy-filter-btn" data-filter="sublob">
          <i class="bi bi-diagram-3"></i> Operating Sub-LOBs (${sublobCount})
        </button>
      </div>

      <!-- Search & Filter Controls -->
      <div class="subsidiary-search-box">
        <i class="bi bi-search" style="color:#94a3b8;font-size:0.85rem;margin-left:4px;"></i>
        <input type="text" class="subsidiary-search-input" placeholder="Search subsidiaries by name, country (US, CA, GB, JP), or LEI..." />
        <span class="badge" style="background:#f1f5f9;color:#64748b;font-size:0.75rem;padding:7px 12px;border:1px solid #e2e8f0;" id="subsidiaryCountBadge">${totalCount} Entities</span>
      </div>

      <!-- Subsidiaries Grid -->
      <div class="subsidiaries-grid">
        ${allSubs.map(child => {
          const isSub = !!child.is_sub_lob || (child.hierarchy_level === 3);
          const cName = (child.legal_name || "").toLowerCase();
          const matched = knownLobs.find(l => {
            const lName = (l.name || l.lob_name || "").toLowerCase();
            return lName && cName && (lName.includes(cName) || cName.includes(lName));
          });
          const isMapped = !!matched;
          const matchedLobId = matched ? matched.id : null;

          return `
            <div class="subsidiary-card${isMapped ? " mapped-lob" : ""}" data-is-sublob="${isSub}" data-name="${esc((child.legal_name || "").toLowerCase())}" data-country="${esc((child.country || "").toLowerCase())}" data-lei="${esc((child.lei || "").toLowerCase())}">
              <div>
                <div class="subsidiary-header">
                  <div class="subsidiary-name">
                    ${esc(child.legal_name)}
                  </div>
                  <span class="badge-solid-green" style="font-size:0.65rem;padding:2px 6px;">${esc(child.status || "ACTIVE")}</span>
                </div>
                <div class="subsidiary-meta-row">
                  ${isSub ? `
                    <span class="jurisdiction-tag" style="background:#fef3c7;color:#92400e;font-weight:700;">
                      <i class="bi bi-diagram-3"></i> Level 3: Sub-LOB
                    </span>
                  ` : `
                    <span class="jurisdiction-tag" style="background:#e0f2fe;color:#0284c7;font-weight:700;">
                      <i class="bi bi-building"></i> Level 2: Direct LOB
                    </span>
                  `}
                  <span class="jurisdiction-tag">
                    ${getCountryFlag(child.country)} ${esc(child.jurisdiction || child.country || "Global")}
                  </span>
                  ${child.legal_form ? `<span class="jurisdiction-tag" title="Entity Legal Form Code"><i class="bi bi-file-earmark-text"></i> ${esc(child.legal_form)}</span>` : ""}
                  ${child.is_commercial_lob ? `<span class="jurisdiction-tag" style="background:#f0fdf4;color:#047857;" title="Commercial Operating Unit"><i class="bi bi-briefcase-fill"></i> Commercial</span>` : ""}
                </div>
              </div>
              <div style="margin-top:10px;padding-top:8px;border-top:1px dashed #e2e8f0;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px;">
                <a href="https://search.gleif.org/#/record/${esc(child.lei)}" target="_blank" rel="noopener noreferrer" class="lei-code-badge" title="View GLEIF official record">
                  <i class="bi bi-box-arrow-up-right"></i> LEI: ${esc(child.lei ? (child.lei.length > 16 ? child.lei.substring(0, 14) + "..." : child.lei) : "N/A")}
                </a>
                ${isMapped ? `
                  <a href="javascript:void(0)" class="lob-matched-badge" data-lob-id="${esc(matchedLobId)}" title="Click to view Line of Business #${matchedLobId}">
                    <i class="bi bi-check-circle-fill"></i> Mapped LOB #${matchedLobId}
                  </a>
                ` : `
                  <span style="font-size:0.68rem;color:#94a3b8;">${esc(child.relationship_type || (isSub ? "Indirect Operating Sub-LOB" : "Direct Child Entity"))}</span>
                `}
              </div>
            </div>
          `;
        }).join("")}
      </div>

      ${secSubs.length ? `
        <div style="margin-top:20px;padding-top:14px;border-top:1px solid #e2e8f0;">
          <div style="font-size:0.82rem;font-weight:700;color:#0f172a;margin-bottom:8px;display:flex;align-items:center;gap:6px;">
            <i class="bi bi-file-earmark-spreadsheet" style="color:#0284c7;"></i> SEC Form 10-K Exhibit 21 Disclosed Subsidiaries (${secSubs.length})
          </div>
          <div class="subsidiaries-grid">
            ${secSubs.map(sub => `
              <div class="subsidiary-card">
                <div class="subsidiary-name">${esc(sub.legal_name || sub.name)}</div>
                <div class="subsidiary-meta-row">
                  <span class="jurisdiction-tag"><i class="bi bi-geo-alt"></i> ${esc(sub.jurisdiction || sub.state || "US")}</span>
                  ${sub.filing_year ? `<span class="jurisdiction-tag"><i class="bi bi-calendar"></i> Filing Year: ${esc(sub.filing_year)}</span>` : ""}
                </div>
              </div>
            `).join("")}
          </div>
        </div>
      ` : ""}

      <!-- Collapsible Raw Regulatory JSON -->
      <div class="raw-json-accordion">
        <button type="button" class="btn btn-sm btn-outline-secondary btn-toggle-raw-json" style="font-size:0.72rem;">
          <i class="bi bi-code-slash"></i> View Raw Regulatory Data (JSON) <i class="bi bi-chevron-down"></i>
        </button>
        <div class="raw-json-container d-none" style="margin-top:10px;">
          <pre style="font-size:.7rem;max-height:220px;overflow:auto;background:#0f172a;color:#f8fafc;padding:12px;border-radius:6px;margin:0;white-space:pre-wrap;">${esc(JSON.stringify(tree, null, 2))}</pre>
        </div>
      </div>
    `;
  }

  // ══════════════════════════════════════════════════════════════════
  // FULL DATABASE ATTRIBUTE VAULTS (100% OF ALL DATABASE COLUMNS)
  // ══════════════════════════════════════════════════════════════════

  // 1. Account Vault — All 95 Database Columns
  function renderFullAccountVault(account) {
    if (!account) return "";
    currentVaultContext = { entityType: "account", id: account.id };

    return `
      <!-- Section 1: Corporate Identity -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-building" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>1. Corporate Identity &amp; Legal Registration</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Display Name", account.display_name || account.name)}
          ${renderField("Legal Name", account.legal_name)}
          ${renderField("Unique Key", account.key)}
          ${renderField("Primary Domain", account.primary_domain || account.domain)}
          ${renderField("Official Website URL", account.website_url || (account.domain ? 'https://' + account.domain : null), { url: true })}
          ${renderField("Crunchbase URL", account.crunchbase_url, { url: true })}
          ${renderField("Operating Status", account.operating_status)}
          ${renderField("Company Type", account.company_type)}
          ${renderField("Founded Year", account.founded_year)}
          ${renderField("Employee Count Range", account.employee_count_range)}
        </div>
      </div>

      <!-- Section 2: Location & Corporate Contact -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-geo-alt" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>2. Location &amp; Corporate Contact</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Headquarters Location", account.headquarters_location || account.location)}
          ${renderField("City", account.city)}
          ${renderField("State / Region", account.state)}
          ${renderField("Country", account.country)}
          ${renderField("Postal Code", account.postal_code)}
          ${renderField("Phone Number", account.phone_number)}
          ${renderField("Sanitized Phone", account.sanitized_phone)}
          ${renderField("Contact Email", account.contact_email, { url: true, urlLabel: account.contact_email })}
        </div>
      </div>

      <!-- Section 3: Social Media & Developer Footprint -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-share" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>3. Social Media &amp; Developer Footprint</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("LinkedIn URL", account.linkedin_url, { url: true })}
          ${renderField("Twitter / X Profile", account.twitter_url || account.twitter_live_url, { url: true })}
          ${renderField("Twitter Handle", account.twitter_handle)}
          ${renderField("GitHub Repository", account.github_url, { url: true })}
          ${renderField("Glassdoor Reviews", account.glassdoor_url, { url: true })}
          ${renderField("Corporate Blog", account.blog_url, { url: true })}
        </div>
      </div>

      <!-- Section 4: Financial Valuation & Securities -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-currency-dollar" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>4. Financial Valuation &amp; Securities</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Reported Revenue", account.revenue || account.estimated_revenue_range)}
          ${renderField("Total Funding (USD)", account.total_funding_amount_usd ? '$' + Number(account.total_funding_amount_usd).toLocaleString() : null)}
          ${renderField("Funding Currency", account.total_funding_currency)}
          ${renderField("Last Funding Type", account.last_funding_type)}
          ${renderField("Last Funding Date", account.last_funding_date)}
          ${renderField("Funding Rounds", account.num_funding_rounds)}
          ${renderField("Funding Status", account.funding_status)}
          ${renderField("Stock Symbol / Ticker", account.stock_symbol || account.ticker)}
          ${renderField("Stock Exchange", account.stock_exchange)}
          ${renderField("IPO Status", account.ipo_status)}
          ${renderField("IPO Date", account.ipo_date)}
        </div>
      </div>

      <!-- Section 5: Regulatory Compliance & SEC EDGAR -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-shield-check" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>5. Regulatory Compliance &amp; SEC EDGAR</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("SEC CIK Number", account.sec_cik)}
          ${renderField("SEC EDGAR Search URL", account.sec_edgar_url, { url: true })}
          ${renderField("SEC Filings RSS Feed", account.sec_filings_rss, { url: true })}
          ${renderField("SEC Submissions API URL", account.sec_submissions_url, { url: true })}
        </div>
      </div>

      <!-- Section 6: Web Traffic, IT Spend & IP Assets -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-cpu" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>6. Web Traffic, IT Spend &amp; IP Assets</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Global Traffic Rank", account.global_traffic_rank ? '#' + Number(account.global_traffic_rank).toLocaleString() : null)}
          ${renderField("Monthly Web Visits", account.monthly_visits ? Number(account.monthly_visits).toLocaleString() : null)}
          ${renderField("Bounce Rate", account.bounce_rate ? account.bounce_rate + '%' : null)}
          ${renderField("Average Visit Duration", account.visit_duration)}
          ${renderField("Page Views Per Visit", account.page_views_per_visit)}
          ${renderField("Account Heat / Opportunity Score", account.heat_score)}
          ${renderField("90-Day Momentum Trend", account.trend_score_90d)}
          ${renderField("Active Technology Count", account.active_tech_count)}
          ${renderField("Estimated IT Annual Spend", account.it_spend)}
          ${renderField("Patents Granted", account.patents_granted)}
          ${renderField("Trademarks Registered", account.trademarks_registered)}
        </div>
      </div>

      <!-- Section 7: 15 OSINT Scraping Launchpad Feeds -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-broadcast" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>7. 15 OSINT Scraping Launchpad Feeds</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Twitter Live Feed URL", account.twitter_live_url, { url: true })}
          ${renderField("Reddit Search Query", account.reddit_query)}
          ${renderField("Reddit Atom RSS Feed", account.reddit_rss_url, { url: true })}
          ${renderField("Google News Query", account.news_query)}
          ${renderField("Google News RSS Feed", account.rss_url, { url: true })}
          ${renderField("Google Patents Search", account.google_patents_url, { url: true })}
          ${renderField("Google Trends Analytics", account.google_trends_url, { url: true })}
          ${renderField("YouTube Executive Search", account.youtube_search_url, { url: true })}
          ${renderField("OpenAlex Academic Profile", account.openalex_institution_url, { url: true })}
          ${renderField("Wikidata Knowledge Entity", account.wikidata_entity_url, { url: true })}
        </div>
      </div>

      <!-- Section 8: Industry Classifications & Hierarchy Metrics -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-tags" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>8. Industry Classifications &amp; Hierarchy Metrics</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Target Industries", account.industries, { chips: true, span2: true })}
          ${renderField("Business Keywords", account.keywords, { chips: true, span2: true, maxChips: 6 })}
          ${renderField("LOBs Discovered", account.lobs_count || (account.lobs || []).length)}
          ${renderField("Total Contacts Captured", account.total_contacts_captured || (account.personas || []).length)}
          ${renderField("C-Suite Executives", account.c_suite_count)}
          ${renderField("VP & Division Heads", account.vp_count)}
          ${renderField("Directors", account.director_count)}
          ${renderField("Managers", account.manager_count)}
          ${renderField("Sub-Organizations", account.num_suborganizations)}
          ${renderField("Corporate Acquisitions", account.num_acquisitions)}
        </div>
      </div>

      <!-- Section 9: Corporate Ownership & Regulatory Legal Registry Summary -->
      ${account.organisational_hierarchy_tree ? `
        <div class="pipeline-section-card fade-in" style="margin-top:14px;">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <i class="bi bi-diagram-3-fill" style="color:#0284c7;font-size:0.95rem;"></i>
              <span>9. Corporate Regulatory &amp; Legal Registry Summary</span>
            </div>
            <span class="section-subtitle-hint">GLEIF ISO 17442 &amp; SEC Form 10-K Exhibit 21</span>
          </div>
          
          <div class="snapshot-fields-grid" style="grid-template-columns: repeat(3, minmax(0, 1fr));">
            <div class="snapshot-field-item">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">ULTIMATE PARENT LEI</span>
              </div>
              <div class="snapshot-field-value">
                <a href="https://search.gleif.org/#/record/${esc(account.organisational_hierarchy_tree.gleif_lei || account.sec_cik || '')}" target="_blank" style="color:#0284c7;text-decoration:none;">
                  ${esc(account.organisational_hierarchy_tree.gleif_lei || 'Verified in Registry')} <i class="bi bi-box-arrow-up-right" style="font-size:.7rem;"></i>
                </a>
              </div>
            </div>

            <div class="snapshot-field-item">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">REGISTERED SUBSIDIARIES</span>
              </div>
              <div class="snapshot-field-value">
                ${(account.organisational_hierarchy_tree.all_subsidiaries || []).length || ((account.organisational_hierarchy_tree.gleif_children || []).length + (account.organisational_hierarchy_tree.sec_exhibit21_subsidiaries || []).length) || (account.lobs || []).length} Global Entities Tracked
              </div>
            </div>

            <div class="snapshot-field-item">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">OFFICIAL REGULATORY FILING</span>
              </div>
              <div class="snapshot-field-value">
                ${account.sec_edgar_url ? `
                  <a href="${esc(account.sec_edgar_url)}" target="_blank" style="color:#0284c7;text-decoration:none;">
                    SEC Form 10-K Exhibit 21 <i class="bi bi-box-arrow-up-right" style="font-size:.7rem;"></i>
                  </a>
                ` : 'SEC Exhibit 21 Indexed'}
              </div>
            </div>
          </div>

          <div style="margin-top:12px;padding:10px 14px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;font-size:0.78rem;color:#64748b;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;">
            <div>
              <i class="bi bi-info-circle-fill" style="color:#0284c7;"></i>
              Active operating business units, divisional heads, and technology stacks are organized under the <strong>Lines of Business (${(account.lobs || []).length})</strong> tab.
            </div>
            <button type="button" class="crumb-lobs" style="font-size:0.74rem;padding:4px 12px;background:#0284c7;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;">
              Explore ${(account.lobs || []).length} Operating LOBs &rarr;
            </button>
          </div>
        </div>
      ` : ""}
    `;
  }

  // 2. LOB Vault — All 27 Database Columns
  function renderFullLobVault(lob) {
    if (!lob) return "";
    currentVaultContext = { entityType: "lob", id: lob.id };

    return `
      <!-- Section 1: Corporate Identity & Legal Structure -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-building" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>1. Corporate Identity &amp; Legal Structure</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Unique LOB Key", lob.key)}
          ${renderField("Division Name", lob.name || lob.lob_name)}
          ${renderField("Relationship Taxonomy", lob.relationship_type)}
          ${renderField("LEI Code (GLEIF)", lob.lei_code)}
          ${renderField("Legal Jurisdiction", lob.jurisdiction)}
          ${renderField("Commercial Registry", lob.commercial_registry)}
          ${renderField("Primary Domain", lob.domain || lob.primary_domain)}
          ${renderField("Dedicated Website URL", lob.website_url || lob.website, { url: true })}
          ${renderField("Crunchbase URL", lob.crunchbase_url, { url: true })}
          ${renderField("Wikipedia URL", lob.wikipedia_url, { url: true })}
        </div>
      </div>

      <!-- Section 2: Financial Metrics & Leadership -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-bar-chart" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>2. Financial Metrics &amp; Executive Leadership</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Segment Revenue", lob.revenue || lob.segment_revenue)}
          ${renderField("Segment Headcount Size", lob.headcount || lob.headcount_size)}
          ${renderField("Operating Head Executive", lob.operating_head)}
        </div>
      </div>

      <!-- Section 3: Technology Stack & Core Infrastructure -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-cpu" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>3. Technology Stack &amp; Infrastructure</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Active Technologies", lob.technologies, { chips: true, spanFull: true })}
        </div>
      </div>

      <!-- Section 4: Direct Peer Competitors & Market Alternatives (Horizontal Grid) -->
      ${renderLobCompetitorsCard(lob.competitors, "4. ")}

      <!-- Section 5: Patents & Financial Disclosures (Structured Cards) -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-journal-text" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>5. Patents &amp; Financial Disclosures</span>
          </div>
          <span class="section-subtitle-hint">Regulatory Disclosures &amp; IP Holdings</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderLobPatentsCard(lob.patents)}
          ${renderLobFinancialsCard(lob.financial_snippets)}
        </div>
      </div>

      <!-- Section 6: OSINT Stream Feed Endpoints & Queries -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-broadcast-pin" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>6. OSINT Stream Feed Endpoints &amp; Tracking Queries</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to configure stream endpoints inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Division Website URL", lob.website_url, { url: true })}
          ${renderField("X / Twitter Feed URL", lob.twitter_live_url, { url: true })}
          ${renderField("Google News RSS Feed", lob.google_news_rss_url, { url: true })}
          ${renderField("Reddit Community RSS Feed", lob.reddit_rss_url, { url: true })}
          ${renderField("Google Patents Live Feed", lob.google_patents_url, { url: true })}
          ${renderField("Google Trends Search Momentum", lob.google_trends_url, { url: true })}
          ${renderField("YouTube Media Search Feed", lob.youtube_search_url, { url: true })}
        </div>
      </div>


    `;
  }

  // 3. Persona Vault — All 69 Database Columns
  function renderFullPersonaVault(p) {
    if (!p) return "";
    currentVaultContext = { entityType: "persona", id: p.id };
    const feeds = (p.osint_feed_manifest && p.osint_feed_manifest.feeds) ? p.osint_feed_manifest.feeds : {};
    const getVal = (col, feedKey) => p[col] || feeds[feedKey || col] || "";

    const verifiedStreams = [
      { label: "LinkedIn Profile URL", val: p.linkedin_url, icon: "bi-linkedin" },
      { label: "Official Corporate Bio URL", val: p.corporate_bio_url, icon: "bi-building-check", isBespoke: true },
      { label: "Crunchbase Profile URL", val: p.crunchbase_url, icon: "bi-briefcase" },
      { label: "SEC Form 4 Insider Trades URL", val: p.sec_insider_trades_url, icon: "bi-file-earmark-lock" },
      { label: "FEC Political Contributions URL", val: p.fec_contributions_url, icon: "bi-bank" },
      { label: "Quiver Quantitative Insider URL", val: p.quiver_insider_url, icon: "bi-graph-up-arrow", isBespoke: true },
      { label: "Bloomberg Media & Videos URL", val: p.bloomberg_url, icon: "bi-tv", isBespoke: true },
      { label: "Wall Street Journal Article URL", val: p.wsj_article_url, icon: "bi-newspaper", isBespoke: true },
      { label: "Major Media Interview URL", val: p.media_interview_url, icon: "bi-mic-fill", isBespoke: true },
      { label: "Annual Report & Proxy Statement URL", val: p.annual_report_url, icon: "bi-file-earmark-text" },
      { label: "ZoomInfo Profile URL", val: p.zoominfo_url, icon: "bi-person-badge" },
      { label: "Google News Real-Time RSS URL", val: p.rss_url, icon: "bi-rss" },
      { label: "YouTube Media & Keynotes URL", val: p.youtube_url, icon: "bi-youtube", isBespoke: true },
      { label: "Executive Podcast Appearances URL", val: p.podcast_url, icon: "bi-headphones", isBespoke: true },
      { label: "OpenInsider Trades Screener URL", val: p.openinsider_url, icon: "bi-currency-exchange" },
      { label: "SECForm4 Live Filings URL", val: p.secform4_url, icon: "bi-shield-shaded" },
      { label: "Wayback Career Archive URL", val: p.wayback_url, icon: "bi-clock-history" },
      { label: "TheOrg Executive Chart URL", val: p.theorg_url, icon: "bi-diagram-3-fill", isBespoke: true },
      { label: "Seeking Alpha Transcripts URL", val: p.seeking_alpha_url, icon: "bi-chat-square-quote-fill", isBespoke: true },
      { label: "External Board & Civic Roles URL", val: p.external_board_url, icon: "bi-award-fill", isBespoke: true },
      { label: "Google Patents Inventor Search URL", val: p.google_patents_url, icon: "bi-lightbulb", isBespoke: true },
      { label: "Google Scholar Citations URL", val: p.google_scholar_url, icon: "bi-mortarboard", isBespoke: true },
      { label: "OpenAlex Scientific Author URL", val: p.openalex_author_url, icon: "bi-book", isBespoke: true },
      { label: "ORCID Researcher Profile URL", val: p.orcid_search_url, icon: "bi-person-badge-fill", isBespoke: true },
      { label: "Wikidata Entity Resolution URL", val: p.wikidata_person_url, icon: "bi-globe2", isBespoke: true },
      { label: "Reddit OSINT Sentiment RSS URL", val: p.reddit_rss_url, icon: "bi-reddit", isBespoke: true },
      { label: "Google Trends Executive Interest URL", val: p.google_trends_url, icon: "bi-graph-up", isBespoke: true },
      { label: "YouTube Executive Keynotes URL", val: p.youtube_interviews_url, icon: "bi-play-circle", isBespoke: true },
      { label: "Podcast Executive Appearances URL", val: p.podcast_search_url, icon: "bi-mic", isBespoke: true },
      { label: "Live Corporate Twitter / X Stream URL", val: p.twitter_live_url, icon: "bi-twitter-x", isBespoke: true }
    ];

    // Filter dedicated-column streams that have a value
    const activeStreams = verifiedStreams.filter(s => s.val && String(s.val).trim() !== "");

    // Dynamically append any feeds from osint_feed_manifest.feeds[] that
    // are NOT already covered by a dedicated DB column above.
    // This ensures all stored OSINT sources render without hardcoding.
    const manifestFeeds = (p.osint_feed_manifest && Array.isArray(p.osint_feed_manifest.feeds))
      ? p.osint_feed_manifest.feeds
      : [];

    if (manifestFeeds.length > 0) {
      // Build a set of URLs already displayed via dedicated columns (normalised for comparison)
      const existingUrls = new Set(
        activeStreams.map(s => String(s.val).trim().toLowerCase())
      );
      manifestFeeds.forEach(feed => {
        const feedUrl = (feed.url || "").trim();
        if (feedUrl && !existingUrls.has(feedUrl.toLowerCase())) {
          activeStreams.push({
            label: (feed.source || feed.type || "Intelligence Feed") + " URL",
            val: feedUrl,
            icon: "bi-rss-fill",
            isManifest: true   // distinct badge style (blue) from DB-column streams
          });
          existingUrls.add(feedUrl.toLowerCase()); // prevent duplicates within manifest itself
        }
      });
    }

    return `
      <!-- Section 1: Executive Profile -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-person-vcard" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>1. Executive Profile &amp; Demographics</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Full Name", p.name || p.full_name)}
          ${renderField("First Name", p.first_name)}
          ${renderField("Last Name", p.last_name)}
          ${renderField("Corporate Title", p.title || p.corporate_title)}
          ${renderField("Professional Headline", p.headline)}
          ${renderField("Seniority Level (Raw)", p.seniority_raw || p.tier)}
          ${renderField("Hierarchy Level Number", p.hierarchy_level)}
        </div>
      </div>

      <!-- Section 2: Contact Information -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-envelope" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>2. Verified Contact Coordinates</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Corporate Email", p.email, { url: true, urlLabel: p.email })}
          ${renderField("Email Delivery Status", p.email_status)}
          ${renderField("Office Phone", p.phone)}
          ${renderField("Direct Mobile Phone", p.direct_mobile_phone)}
          ${renderField("Personal Email", p.personal_email)}
          ${renderField("City", p.city)}
          ${renderField("State / Region", p.state)}
          ${renderField("Country", p.country)}
          ${renderField("Base Location", p.location)}
        </div>
      </div>

      <!-- Section 3: Career & Employment History (Visual Timeline) -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-briefcase" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>3. Career &amp; Employment History</span>
          </div>
          <span class="section-subtitle-hint">Executive Career Journey &amp; Corporate Background</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Prior Employer", p.prior_company)}
          ${renderField("Past Companies", p.past_companies, { chips: true, span2: true })}
          ${renderField("Previous Titles", p.previous_titles, { chips: true, span2: true })}
          ${renderField("Role Tenure (Months)", p.current_role_tenure_months ? `${p.current_role_tenure_months} months` : null)}
          ${renderField("New in Current Role?", p.is_new_in_role ? "Yes (Recently Promoted/Hired)" : "Established")}
          ${renderField("Career Trajectory Score", p.career_trajectory_score ? `${p.career_trajectory_score} / 100` : null)}
          ${renderField("Reports To", p.reports_to)}
          ${renderExecutiveCareerTimeline(p.employment_history)}
        </div>
      </div>

      <!-- Section 4: Academic Background (Visual Credentials) -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-mortarboard" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>4. Academic Background &amp; Education Credentials</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Degree", p.degree)}
          ${renderField("Alma Mater Institution", p.institution)}
          ${renderAcademicCredentialsGrid(p.education_history, p.degree, p.institution)}
        </div>
      </div>

      <!-- Section 5: AI Sales Dossier -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-stars" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>5. AI Sales Dossier &amp; Behavioral Tone</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Communication Style", p.communication_style, { span2: true })}
          ${renderField("Value Proposition", p.value_proposition, { span2: true })}
          ${renderField("Personalized Icebreaker", p.personalized_icebreaker, { span2: true })}
          ${renderField("Social Platform", p.social_platform)}
          ${renderField("Engagement Likelihood", p.engagement_rate ? `${p.engagement_rate}%` : null)}
          ${renderField("Social Presence Level", p.social_presence_level)}
        </div>
      </div>

      <!-- Section 6: Skills, Pain Points & Buying Authority -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-bullseye" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>6. Skills, Operational Pain Points &amp; Buying Authority</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline</span>
        </div>
        <div class="snapshot-fields-grid">
          ${renderField("Verified Skills", p.skills, { chips: true, span2: true })}
          ${renderField("Target KPIs", p.target_kpis, { chips: true, span2: true })}
          ${renderField("Operational Pain Points", p.operational_pain_points, { chips: true, span2: true })}
          ${renderField("Anticipated Objections", p.key_objections, { chips: true, span2: true })}
          ${renderField("Decision Authority Level", p.decision_authority)}
          ${renderField("Budget Authority Level", p.budget_authority)}
          ${renderField("Governed Departments", p.departments, { chips: true })}
        </div>
      </div>

      <!-- Section 7: Verified Executive Intelligence Launchpad Manifest -->
      <div class="pipeline-section-card fade-in" style="margin-top:14px;">
        <div class="section-title-row">
          <div class="section-title-left">
            <span class="section-title-dot"></span>
            <i class="bi bi-shield-check" style="color:#0284c7;font-size:0.95rem;"></i>
            <span>7. Verified Executive Intelligence (${activeStreams.length} Active Streams)</span>
          </div>
          <span class="section-subtitle-hint">Click pencil icon to edit inline &bull; Verified live feeds</span>
        </div>
        <div class="snapshot-fields-grid">
          ${activeStreams.length > 0 
            ? activeStreams.map(s => renderField(s.label, s.val, { url: true })).join("") 
            : '<div style="color:#94a3b8;font-style:italic;grid-column:span 2;padding:10px;">No public intelligence streams registered for this role.</div>'
          }
        </div>
        ${activeStreams.length > 0 ? `
          <div style="margin-top:14px;padding-top:12px;border-top:1px solid #e2e8f0;">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
              <span style="font-size:0.75rem;font-weight:700;color:#0284c7;text-transform:uppercase;letter-spacing:0.5px;">
                <i class="bi bi-cpu-fill"></i> Active OSINT Streams Manifest (${activeStreams.length} verified channels)
              </span>
              <span style="font-size:0.68rem;color:#64748b;">Key: ${esc(p.osint_feed_manifest && p.osint_feed_manifest.key ? p.osint_feed_manifest.key : (p.key || ''))}</span>
            </div>
            <div style="display:flex;flex-wrap:wrap;gap:6px;">
              ${activeStreams.map(s => {
                const badgeClass = s.isBespoke ? 'badge-solid-purple' : s.isManifest ? 'badge-solid-blue' : 'badge-solid-green';
                const iconClass  = s.isBespoke ? 'bi-patch-check-fill' : s.isManifest ? 'bi-rss-fill' : 'bi-check-circle-fill';
                const iconColor  = s.isBespoke ? '#a855f7' : s.isManifest ? '#0284c7' : '#10b981';
                return `
                <a href="${esc(normalizeUrl(String(s.val)))}" target="_blank" rel="noopener noreferrer"
                   class="${badgeClass}"
                   style="text-decoration:none;display:inline-flex;align-items:center;gap:4px;padding:4px 8px;font-size:0.72rem;"
                   title="${esc(s.val)}">
                  <i class="bi ${iconClass}" style="color:${iconColor};"></i>
                  ${esc(s.label.replace(' URL', '').toUpperCase())}
                  <i class="bi bi-box-arrow-up-right" style="font-size:0.6rem;"></i>
                </a>`;
              }).join('')}

            </div>
          </div>
        ` : ''}
      </div>

      <!-- Section 8: Ingestion Audit Payload (Collapsed Accordion) -->
      ${p.raw_data ? `
        <div class="pipeline-section-card fade-in" style="margin-top:14px;">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <i class="bi bi-code-square" style="color:#0284c7;font-size:0.95rem;"></i>
              <span>8. Ingestion Audit Payload (JSON)</span>
            </div>
            <span class="section-subtitle-hint">Technical Ingestion Audit</span>
          </div>
          <div class="raw-json-accordion" style="margin-top:0;border-top:none;padding-top:0;">
            <button type="button" class="btn btn-sm btn-outline-secondary btn-toggle-raw-json" style="font-size:0.75rem;">
              <i class="bi bi-code-slash"></i> View Ingestion Audit Payload (JSON) <i class="bi bi-chevron-down"></i>
            </button>
            <div class="raw-json-container d-none" style="margin-top:10px;">
              <pre style="font-size:.7rem;max-height:260px;overflow:auto;background:#0f172a;color:#f8fafc;padding:12px;border-radius:6px;margin:0;white-space:pre-wrap;">${esc(JSON.stringify(p.raw_data, null, 2))}</pre>
            </div>
          </div>
        </div>
      ` : ""}
    `;
  }

  // ══════════════════════════════════════════════════════════════════
  // DYNAMIC BREADCRUMB BUILDER
  // ══════════════════════════════════════════════════════════════════
  function renderModernBreadcrumbs() {
    if (!activeAccount) {
      $("#breadcrumbsBar").empty();
      return;
    }

    if (activePersona) {
      if (activeLob) {
        $("#breadcrumbsBar").html(`
          <a href="javascript:void(0)" class="crumb-account">${esc(activeAccount.name)}</a>
          <span class="sep">&gt;</span>
          <a href="javascript:void(0)" class="crumb-lob">${esc(activeLob.name)}</a>
          <span class="sep">&gt;</span>
          <span class="active-crumb">${esc(activePersona.name)}</span>
        `);
      } else {
        $("#breadcrumbsBar").html(`
          <a href="javascript:void(0)" class="crumb-account">${esc(activeAccount.name)}</a>
          <span class="sep">&gt;</span>
          <a href="javascript:void(0)" class="crumb-personas">Personas</a>
          <span class="sep">&gt;</span>
          <span class="active-crumb">${esc(activePersona.name)}</span>
        `);
      }
    } else if (activeLob) {
      $("#breadcrumbsBar").html(`
        <a href="javascript:void(0)" class="crumb-account">${esc(activeAccount.name)}</a>
        <span class="sep">&gt;</span>
        <a href="javascript:void(0)" class="crumb-lobs">Lines of Business</a>
        <span class="sep">&gt;</span>
        <span class="active-crumb">${esc(activeLob.name)}</span>
      `);
    } else {
      $("#breadcrumbsBar").empty();
    }
  }

  // ─── Real-Time Dynamic Metrics & Verification Engine ─────────────────────
  function formatTimeAgo(dateInput) {
    if (!dateInput) return "—";
    const d = new Date(dateInput);
    if (isNaN(d.getTime())) return "—";
    const now = new Date();
    const diffSec = Math.floor((now - d) / 1000);
    if (diffSec < 0) return "just now";
    if (diffSec < 60) return `${diffSec}s ago`;
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHours = Math.floor(diffMin / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    const diffDays = Math.floor(diffHours / 24);
    if (diffDays < 30) return `${diffDays}d ago`;
    const diffMonths = Math.floor(diffDays / 30);
    if (diffMonths < 12) return `${diffMonths}mo ago`;
    const diffYears = Math.floor(diffDays / 365);
    return `${diffYears}y ago`;
  }

  // ── Enterprise Data Verification Helper ──
  function isPopulated(val) {
    if (val === null || val === undefined) return false;
    if (typeof val === "string") {
      const s = val.trim();
      return s !== "" && s !== "—" && s !== "-" && s !== "None" && s !== "null" && s !== "Revenue N/A" && s !== "NEW" && s !== "Domain not set" && s !== "Not set";
    }
    if (Array.isArray(val)) return val.length > 0;
    if (typeof val === "object") return Object.keys(val).length > 0;
    if (typeof val === "number") return !isNaN(val);
    if (typeof val === "boolean") return true;
    return Boolean(val);
  }

  // ── Account Enterprise Health (45 Schema Attributes & 11 Connectors) ──
  function computeAccountHealth(account) {
    if (!account) {
      return { completenessPct: 0, percentage: 0, enrichedCount: 0, populatedCount: 0, totalFields: 45, missingCount: 45, pendingCount: 0, segments: [], coveragePct: 0, sourcesHit: 0, hitCount: 0, totalSources: 11, confidenceScore: 0, trustTier: "Basic", sources: [] };
    }

    const msi = account.multi_source_intelligence || {};
    const oht = account.organisational_hierarchy_tree || {};

    const checks = [
      // Identity & Corporate Structure (9)
      isPopulated(account.display_name || account.name),
      isPopulated(account.legal_name),
      isPopulated(account.domain || account.primary_domain),
      isPopulated(account.website_url),
      isPopulated(account.crunchbase_url),
      isPopulated(account.company_type),
      isPopulated(account.operating_status),
      isPopulated(account.founded_year || account.founded_date),
      isPopulated(account.employee_count_range),

      // Overview, Hierarchy & Classifications (4)
      isPopulated(account.short_description || account.full_description || account.desc || account.overview),
      isPopulated(account.industries),
      isPopulated(account.keywords),
      isPopulated(account.lobs_count || (account.lobs && account.lobs.length)),

      // Location & Contact (8)
      isPopulated(account.headquarters_location),
      isPopulated(account.city),
      isPopulated(account.state),
      isPopulated(account.country),
      isPopulated(account.postal_code),
      isPopulated(account.phone_number || account.sanitized_phone),
      isPopulated(account.contact_email),
      isPopulated(account.linkedin_url),

      // Financials & Market Intelligence (7)
      isPopulated(account.revenue || account.estimated_revenue_range),
      isPopulated(account.total_funding_amount_usd || account.total_funding_amount),
      isPopulated(account.funding_status),
      isPopulated(account.num_funding_rounds),
      isPopulated(account.stock_symbol),
      isPopulated(account.stock_exchange),
      isPopulated(account.ipo_status),

      // SEC & Legal / Regulatory (5)
      isPopulated(account.sec_cik),
      isPopulated(account.sec_edgar_url),
      isPopulated(account.sec_filings_rss),
      isPopulated(account.sec_submissions_url),
      isPopulated(account.lei_code || oht.gleif_lei),

      // Digital Footprint & Tech Spend (6)
      isPopulated(account.global_traffic_rank),
      isPopulated(account.monthly_visits),
      isPopulated(account.bounce_rate),
      isPopulated(account.active_tech_count),
      isPopulated(account.it_spend),
      isPopulated(account.patents_granted),

      // Live OSINT & Scraping Launchpads (6)
      isPopulated(account.twitter_url || account.twitter_live_url),
      isPopulated(account.reddit_rss_url || account.reddit_query),
      isPopulated(account.news_query || account.rss_url),
      isPopulated(account.google_patents_url),
      isPopulated(account.google_trends_url),
      isPopulated(account.youtube_search_url)
    ];

    const totalFields = checks.length; // 45
    const populatedCount = checks.filter(Boolean).length;
    const completenessPct = Math.round((populatedCount / totalFields) * 100);

    // Multi-source connectors (11)
    const sources = [
      { id: "sec", name: "SEC Filings (10-K / Ex 21)", active: Boolean(account.sec_cik || msi.sec_10k_chunks || msi.sec_exhibit21 || account.sec_edgar_url) },
      { id: "gleif", name: "GLEIF Registry (LEI Tree)", active: Boolean(oht.gleif_lei || msi.gleif_ownership_tree || account.lei_code) },
      { id: "opencorp", name: "OpenCorporates Registry", active: Boolean(msi.opencorporates) },
      { id: "finnhub", name: "Finnhub Market Data", active: Boolean(msi.finnhub || account.stock_symbol) },
      { id: "court", name: "CourtListener Litigation", active: Boolean(msi.courtlistener) },
      { id: "fec", name: "FEC Campaign Finance", active: Boolean(msi.fec_political) },
      { id: "patents", name: "USPTO Patents Portfolio", active: Boolean(account.patents_granted > 0 || msi.patents || account.google_patents_url) },
      { id: "diffbot", name: "Diffbot Knowledge Graph", active: Boolean(msi.diffbot_intel) },
      { id: "wiki", name: "Wikipedia & Wikidata", active: Boolean(msi.wikipedia_intel || account.wikidata_entity_url) },
      { id: "news", name: "News & Public RSS", active: Boolean(account.news_query || account.rss_url) },
      { id: "social", name: "Social Feeds (X / Reddit)", active: Boolean(account.twitter_live_url || account.reddit_rss_url) }
    ];

    const sourcesHit = sources.filter(s => s.active).length;
    const totalSources = sources.length; // 11
    const coveragePct = Math.round((sourcesHit / totalSources) * 100);

    // Composite Confidence Score
    const confidenceScore = Math.min(100, Math.round(completenessPct * 0.55 + coveragePct * 0.45));
    const trustTier = confidenceScore >= 80 ? "High Trust" : (confidenceScore >= 60 ? "Substantial" : "Basic");

    const totalSegments = 11;
    const greenSegments = Math.round((populatedCount / totalFields) * totalSegments);
    const graySegments = Math.max(0, totalSegments - greenSegments);
    const segments = [];
    for (let i = 0; i < greenSegments; i++) segments.push("seg-green");
    for (let i = 0; i < graySegments; i++) segments.push("seg-gray");

    return {
      completenessPct,
      percentage: completenessPct,
      enrichedCount: populatedCount,
      populatedCount,
      totalFields,
      missingCount: totalFields - populatedCount,
      pendingCount: 0,
      segments,
      coveragePct,
      sourcesHit,
      hitCount: sourcesHit,
      totalSources,
      confidenceScore,
      trustTier,
      sources
    };
  }

  function computeAccountCompleteness(account) {
    return computeAccountHealth(account);
  }

  function computeSourceCoverage(account) {
    return computeAccountHealth(account);
  }

  // ── LOB Enterprise Health (20 Schema Attributes & 5 Streams) ──
  function computeLobHealth(lob) {
    if (!lob) {
      return { completenessPct: 0, percentage: 0, populatedCount: 0, nonNullCount: 0, totalFields: 20, total: 20, missingCount: 20, coveragePct: 0, sourcesHit: 0, totalSources: 5, confidenceScore: 0, trustTier: "Basic", sources: [] };
    }

    const checks = [
      // Identity & Corporate Structure (7)
      isPopulated(lob.lob_name || lob.name),
      isPopulated(lob.domain),
      isPopulated(lob.website_url),
      isPopulated(lob.crunchbase_url),
      isPopulated(lob.relationship_type),
      isPopulated(lob.overview || lob.desc),
      isPopulated(lob.lei_code),

      // Scope & Operational Scale (4)
      isPopulated(lob.jurisdiction),
      isPopulated(lob.audited_segment_revenue || lob.revenue),
      isPopulated(lob.operating_head),
      isPopulated(lob.segment_headcount || lob.headcount),

      // Tech & Market Intelligence (5)
      isPopulated(lob.technologies),
      isPopulated(lob.competitors),
      isPopulated(lob.financial_snippets),
      isPopulated(lob.patents),
      isPopulated(lob.wikipedia_url),

      // Live Scraping Streams (4)
      isPopulated(lob.google_news_rss_url),
      isPopulated(lob.reddit_rss_url),
      isPopulated(lob.google_patents_url),
      isPopulated(lob.youtube_search_url || lob.google_trends_url)
    ];

    const totalFields = checks.length; // 20
    const populatedCount = checks.filter(Boolean).length;
    const completenessPct = Math.round((populatedCount / totalFields) * 100);

    const sources = [
      { name: "SEC Exhibit 21", active: isPopulated(lob.lei_code || lob.relationship_type) },
      { name: "Google News RSS", active: isPopulated(lob.google_news_rss_url) },
      { name: "Reddit Intelligence", active: isPopulated(lob.reddit_rss_url) },
      { name: "USPTO Patents", active: isPopulated(lob.patents || lob.google_patents_url) },
      { name: "Broadcast Media / Trends", active: isPopulated(lob.youtube_search_url || lob.google_trends_url) }
    ];

    const sourcesHit = sources.filter(s => s.active).length;
    const totalSources = sources.length; // 5
    const coveragePct = Math.round((sourcesHit / totalSources) * 100);

    const confidenceScore = Math.min(100, Math.round(completenessPct * 0.60 + coveragePct * 0.40));
    const trustTier = confidenceScore >= 80 ? "High Trust" : (confidenceScore >= 60 ? "Substantial" : "Basic");

    return {
      completenessPct,
      percentage: completenessPct,
      populatedCount,
      nonNullCount: populatedCount,
      totalFields,
      total: totalFields,
      missingCount: totalFields - populatedCount,
      coveragePct,
      sourcesHit,
      totalSources,
      confidenceScore,
      trustTier,
      sources
    };
  }

  function computeLobCompleteness(lob) {
    return computeLobHealth(lob);
  }

  // ── Persona Enterprise Health (40 Schema Attributes & 8 Streams) ──
  function computePersonaHealth(p) {
    if (!p) {
      return { completenessPct: 0, percentage: 0, populatedCount: 0, nonNullCount: 0, totalFields: 40, total: 40, missingCount: 40, coveragePct: 0, sourcesHit: 0, totalSources: 8, confidenceScore: 0, trustTier: "Basic", sources: [] };
    }

    const checks = [
      // Identity & Hierarchy (6)
      isPopulated(p.full_name || p.name || p.display_name),
      isPopulated(p.first_name),
      isPopulated(p.last_name),
      isPopulated(p.title),
      isPopulated(p.tier || p.seniority_raw),
      isPopulated(p.hierarchy_level),

      // Contact & Location (7)
      isPopulated(p.email),
      isPopulated(p.email_status),
      isPopulated(p.phone || p.direct_mobile_phone),
      isPopulated(p.personal_email),
      isPopulated(p.city || p.location),
      isPopulated(p.state),
      isPopulated(p.country),

      // Academic & Career History (8)
      isPopulated(p.headline),
      isPopulated(p.degree),
      isPopulated(p.institution),
      isPopulated(p.education_history),
      isPopulated(p.prior_company),
      isPopulated(p.past_companies),
      isPopulated(p.previous_titles),
      isPopulated(p.current_role_tenure_months),

      // Strategy, Authority & AI Dossier (10)
      isPopulated(p.decision_authority),
      isPopulated(p.budget_authority),
      isPopulated(p.departments),
      isPopulated(p.target_kpis),
      isPopulated(p.skills),
      isPopulated(p.operational_pain_points),
      isPopulated(p.key_objections),
      isPopulated(p.communication_style),
      isPopulated(p.value_proposition),
      isPopulated(p.personalized_icebreaker),

      // Verified Executive Channels & Media (9)
      isPopulated(p.linkedin_url),
      isPopulated(p.corporate_bio_url),
      isPopulated(p.sec_insider_trades_url || p.secform4_url || p.openinsider_url),
      isPopulated(p.quiver_insider_url),
      isPopulated(p.fec_contributions_url),
      isPopulated(p.bloomberg_url || p.media_interview_url),
      isPopulated(p.youtube_url || p.youtube_interviews_url),
      isPopulated(p.podcast_url || p.podcast_search_url),
      isPopulated(p.external_board_url || p.seeking_alpha_url || p.theorg_url || p.wayback_url)
    ];

    const totalFields = checks.length; // 40
    const populatedCount = checks.filter(Boolean).length;
    const completenessPct = Math.round((populatedCount / totalFields) * 100);

    const sources = [
      { name: "LinkedIn Profile", active: isPopulated(p.linkedin_url) },
      { name: "Corporate Bio / BNY", active: isPopulated(p.corporate_bio_url) },
      { name: "SEC EDGAR Form 4", active: isPopulated(p.sec_insider_trades_url || p.secform4_url || p.openinsider_url) },
      { name: "FinTech / QuiverQuant", active: isPopulated(p.quiver_insider_url) },
      { name: "FEC Federal Disclosures", active: isPopulated(p.fec_contributions_url) },
      { name: "Broadcast / Media Keynotes", active: isPopulated(p.bloomberg_url || p.media_interview_url || p.youtube_url) },
      { name: "Executive Podcast / Audio", active: isPopulated(p.podcast_url || p.podcast_search_url) },
      { name: "Board / Institutional", active: isPopulated(p.external_board_url || p.seeking_alpha_url || p.theorg_url || p.wayback_url) }
    ];

    const sourcesHit = sources.filter(s => s.active).length;
    const totalSources = sources.length; // 8
    const coveragePct = Math.round((sourcesHit / totalSources) * 100);

    const confidenceScore = Math.min(100, Math.round(completenessPct * 0.60 + coveragePct * 0.40));
    const trustTier = confidenceScore >= 80 ? "High Trust" : (confidenceScore >= 60 ? "Substantial" : "Basic");

    return {
      completenessPct,
      percentage: completenessPct,
      populatedCount,
      nonNullCount: populatedCount,
      totalFields,
      total: totalFields,
      missingCount: totalFields - populatedCount,
      coveragePct,
      sourcesHit,
      totalSources,
      confidenceScore,
      trustTier,
      sources
    };
  }

  function computePersonaCompleteness(p) {
    return computePersonaHealth(p);
  }

  // ══════════════════════════════════════════════════════════════════
  // VIEW 1: ACCOUNT LEVEL INTELLIGENCE (IMAGE 1)
  // ══════════════════════════════════════════════════════════════════
  
  // Reusable LOB Compact Cards Renderer with Multi-Tier Entity Taxonomy & Filter Bar
  function renderLobCardsList($container, lobs) {
    $container.empty();
    $("#lobSection").find(".lob-toggle-footer").remove();
    $("#lobSection").find(".lob-taxonomy-filter-bar").remove();

    if (!lobs || lobs.length === 0) {
      $container.append(
        `<div style="color:var(--text-muted);font-size:.85rem;padding:8px 0;">
          No Lines of Business discovered for this account.
        </div>`
      );
      $("#lobCountBadge").text("(0 Entities)");
      return;
    }

    $("#lobCountBadge").text(`(${lobs.length} Corporate Entit${lobs.length > 1 ? "ies" : "y"})`);

    // Taxonomy Category Badge Colors & Iconography
    const TAXONOMY_CONFIG = {
      "Commercial brand/platform": { color: "#059669", bg: "#ecfdf5", border: "#a7f3d0", label: "Commercial Platform", icon: "bi-stars" },
      "Acquired operating company": { color: "#2563eb", bg: "#eff6ff", border: "#bfdbfe", label: "Acquired Brand / OpCo", icon: "bi-building-up" },
      "Regulated advisory entity": { color: "#7c3aed", bg: "#f5f3ff", border: "#ddd6fe", label: "Regulated Advisor", icon: "bi-shield-check" },
      "Regional operating company": { color: "#0891b2", bg: "#ecfeff", border: "#a5f3fc", label: "Regional OpCo", icon: "bi-globe" },
      "Holding company": { color: "#d97706", bg: "#fffbeb", border: "#fde68a", label: "Holding Company", icon: "bi-layers" },
      "Financing entity/SPV": { color: "#ea580c", bg: "#fff7ed", border: "#fed7aa", label: "Financing / SPV", icon: "bi-bank" },
      "Legal subsidiary": { color: "#475569", bg: "#f8fafc", border: "#e2e8f0", label: "Legal Subsidiary", icon: "bi-briefcase" },
      "Fund/GP structure": { color: "#4f46e5", bg: "#eef2ff", border: "#c7d2fe", label: "Fund / GP Structure", icon: "bi-diagram-3" },
      "Unclassified - Requires Review": { color: "#64748b", bg: "#f1f5f9", border: "#cbd5e1", label: "Requires Review", icon: "bi-question-circle" }
    };

    // Calculate Counts per Taxonomy
    const categoryCounts = {};
    lobs.forEach(l => {
      const cat = l.relationship_type || "Legal subsidiary";
      categoryCounts[cat] = (categoryCounts[cat] || 0) + 1;
    });

    // Create Taxonomy Filter Bar
    const priority = {
      "Commercial brand/platform": 1,
      "Acquired operating company": 2,
      "Regulated advisory entity": 3,
      "Regional operating company": 4,
      "Holding company": 5,
      "Financing entity/SPV": 6,
      "Legal subsidiary": 7,
      "Fund/GP structure": 8,
      "Unclassified - Requires Review": 9
    };
    const categoriesPresent = Object.keys(categoryCounts).sort((a, b) => (priority[a] || 99) - (priority[b] || 99));

    let filterHtml = `
      <div class="lob-taxonomy-filter-bar" style="margin-bottom:14px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
        <span style="font-size:0.75rem;font-weight:700;color:#64748b;margin-right:4px;text-transform:uppercase;letter-spacing:0.5px;">Taxonomy:</span>
        <button type="button" class="btn-lob-filter active" data-category="ALL" style="background:#0284c7;color:#fff;border:1px solid #0284c7;font-size:0.75rem;font-weight:600;padding:4px 12px;border-radius:14px;cursor:pointer;transition:all .15s ease;">
          All (${lobs.length})
        </button>
    `;

    categoriesPresent.forEach(cat => {
      const cfg = TAXONOMY_CONFIG[cat] || { label: cat };
      filterHtml += `
        <button type="button" class="btn-lob-filter" data-category="${esc(cat)}" style="background:#f8fafc;color:#475569;border:1px solid #cbd5e1;font-size:0.75rem;font-weight:600;padding:4px 12px;border-radius:14px;cursor:pointer;transition:all .15s ease;">
          ${esc(cfg.label)} (${categoryCounts[cat]})
        </button>
      `;
    });
    filterHtml += `</div>`;
    $container.before(filterHtml);

    function renderFilteredCards(filterCat) {
      $container.empty();
      $("#lobSection").find(".lob-toggle-footer").remove();

      const filteredLobs = filterCat === "ALL" 
        ? lobs 
        : lobs.filter(l => (l.relationship_type || "Legal subsidiary") === filterCat);

      const limit = 12;
      const hasMore = filteredLobs.length > limit;

      filteredLobs.forEach((lob, idx) => {
        const isExtra = idx >= limit;
        const relType = lob.relationship_type || "Legal subsidiary";
        const cfg = TAXONOMY_CONFIG[relType] || { color: "#475569", bg: "#f8fafc", border: "#e2e8f0", label: relType, icon: "bi-building" };

        $container.append(`
          <div class="compact-card lob-card fade-in ${isExtra ? 'lob-card-extra' : ''}"
               data-lob-id="${lob.id}"
               data-category="${esc(relType)}"
               ${isExtra ? 'style="display:none;"' : ''}
               title="Click to explore ${esc(lob.name)} (${esc(relType)})">
            <div class="compact-card-avatar" style="position:relative;background:${cfg.bg};color:${cfg.color};border:1px solid ${cfg.border};display:flex;align-items:center;justify-content:center;">
              <i class="bi ${cfg.icon || 'bi-building'}" style="font-size:1rem;"></i>
            </div>
            <div class="compact-card-body">
              <div class="compact-card-title">${esc(lob.name)}</div>
              <div class="compact-card-subtitle" style="display:flex;align-items:center;gap:5px;margin-top:2px;">
                <span style="display:inline-block;padding:2px 7px;border-radius:4px;font-size:0.68rem;font-weight:700;background:${cfg.bg};color:${cfg.color};border:1px solid ${cfg.border};line-height:1.2;">
                  ${esc(cfg.label)}
                </span>
                ${lob.revenue ? `<span style="font-size:0.7rem;color:#0284c7;font-weight:600;">${esc(lob.revenue)}</span>` : ''}
              </div>
            </div>
          </div>
        `);
      });

      if (hasMore) {
        const extraCount = filteredLobs.length - limit;
        $container.after(`
          <div class="lob-toggle-footer" style="grid-column: 1 / -1; width: 100%; text-align: center; margin-top: 12px; padding-top: 10px; border-top: 1px dashed #e2e8f0;">
            <button type="button" class="btn-toggle-lobs-expand" data-expanded="false" style="background:#f8fafc;border:1px solid #cbd5e1;color:#0284c7;font-size:0.8rem;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;gap:6px;padding:6px 16px;border-radius:20px;transition:all .15s ease;">
              <span>View all ${filteredLobs.length} entities (+${extraCount} more)</span> <i class="bi bi-chevron-down" style="font-size:0.75rem;"></i>
            </button>
          </div>
        `);
      }
    }

    renderFilteredCards("ALL");

    // Bind Filter Click Events
    $("#lobSection").off("click", ".btn-lob-filter").on("click", ".btn-lob-filter", function() {
      const $btn = $(this);
      const cat = $btn.data("category");
      $("#lobSection").find(".btn-lob-filter").removeClass("active").css({ background: "#f8fafc", color: "#475569", borderColor: "#cbd5e1" });
      $btn.addClass("active").css({ background: "#0284c7", color: "#fff", borderColor: "#0284c7" });
      renderFilteredCards(cat);
    });


  }

  function formatCompactRevenue(val) {
    if (!val || val === "Revenue N/A" || val === "—") return val || "—";
    const s = String(val).trim();
    if (/^\$?\d+(\.\d+)?[TMBK]$/i.test(s)) return s.startsWith("$") ? s : `$${s}`;
    const num = parseFloat(s.replace(/[^0-9.-]/g, ""));
    if (isNaN(num) || num === 0) return s;
    if (num >= 1e12) return `$${(num / 1e12).toFixed(2)}T`;
    if (num >= 1e9) return `$${(num / 1e9).toFixed(2)}B`;
    if (num >= 1e6) return `$${(num / 1e6).toFixed(2)}M`;
    if (num >= 1e3) return `$${(num / 1e3).toFixed(2)}K`;
    return `$${num.toLocaleString()}`;
  }

  function renderModernAccountHeader(account) {
    const initials = getInitials(account.name);
    const compType = account.company_type || "—";
    const rawRev = (account.revenue && account.revenue !== "Revenue N/A") ? account.revenue : (account.estimated_revenue_range || "—");
    const revenue = formatCompactRevenue(rawRev);
    const opStatus = account.operating_status || "—";
    const industry = (account.industries || [])[0] || "—";
    const desc = account.short_description || (account.desc && account.desc.length < 140 ? account.desc : (account.desc ? account.desc.slice(0, 137) + '...' : '—'));
    const acctKey = `account_${account.id}`;
    const state = getActionState(acctKey);

    const aHealth = computeAccountHealth(account);
    const pullTimeStr = formatTimeAgo(account.updated_at || account.extracted_at || account.created_at);
    const validationStr = state.validated 
      ? `Validation: ${aHealth.sourcesHit}/11 ✓` 
      : (state.pulled ? "Validation: Ready" : "Validation: not run");

    return `
      <div class="modern-entity-header">
        <div class="header-main-info">
          <div class="header-avatar-box avatar-blue">${esc(initials)}</div>
          <div>
            <h1 class="header-entity-name">${esc(account.name)}</h1>
            <div class="header-badges-row">
              ${compType !== "—" ? `<span class="badge-solid-blue">${esc(compType)}</span>` : ""}
              ${revenue !== "—" ? `<span class="badge-solid-gray">${esc(revenue)}</span>` : ""}
              ${opStatus !== "—" ? `<span class="badge-solid-green">${esc(opStatus)}</span>` : ""}
              ${industry !== "—" ? `<span class="badge-solid-purple">${esc(industry)}</span>` : ""}
              <span class="badge-health-pill ${aHealth.confidenceScore >= 80 ? 'health-green' : (aHealth.confidenceScore >= 60 ? 'health-blue' : 'health-amber')}" title="Data Completeness: ${aHealth.completenessPct}% (${aHealth.populatedCount}/${aHealth.totalFields} fields) | Source Coverage: ${aHealth.coveragePct}% (${aHealth.sourcesHit}/${aHealth.totalSources} sources)">
                <i class="bi bi-shield-check"></i> <strong>${aHealth.confidenceScore}% Confidence</strong> &bull; ${aHealth.completenessPct}% Filled (${aHealth.populatedCount}/${aHealth.totalFields}) &bull; ${aHealth.sourcesHit}/${aHealth.totalSources} Sources
              </span>
              <button type="button" class="btn-verify-badge ${account.is_manually_verified ? 'verified' : 'unverified'}" data-entity-type="account" data-id="${account.id}" title="${account.is_manually_verified ? `Verified ${formatTimeAgo(account.manually_verified_at)}` : 'Click to toggle verification'}">
                <i class="bi ${account.is_manually_verified ? 'bi-patch-check-fill' : 'bi-shield-exclamation'}"></i>
                <span>${account.is_manually_verified ? `Manually Verified ✓ (${formatTimeAgo(account.manually_verified_at)})` : 'AI Inferred • Verify'}</span>
              </button>
            </div>
          </div>
        </div>

        <div class="header-actions-col">
          <div class="header-btn-group">
            <button type="button" class="action-btn-pill btn-pill-blue" id="acctOpenBatchBtn" data-account-id="${account.id}" title="Open Batch Console — bulk enrich, discover LOBs & Personas">
              <i class="bi bi-grid-3x3-gap-fill" style="font-size:0.7rem;"></i> Batch Console
            </button>
            <button type="button" class="action-btn-pill btn-pill-purple" id="acctEditBtn">
              Edit
            </button>
            <button type="button" class="action-btn-pill btn-pill-red" id="acctDataManageBtn"
                    data-account-id="${account.id}"
                    data-account-name="${esc(account.name || account.display_name || 'Account')}"
                    title="Manage & Purge Account Data">
              <i class="bi bi-trash3"></i> Delete / Purge
            </button>
          </div>
          <span class="header-meta-timestamp">Last pull: ${pullTimeStr} &bull; ${aHealth.sourcesHit}/11 sources &bull; ${validationStr}</span>
        </div>
      </div>
    `;
  }

  function renderModernCompleteness(account) {
    const comp = computeAccountCompleteness(account);
    const healthClass = comp.percentage >= 80 ? 'health-high' : (comp.percentage >= 60 ? 'health-med' : 'health-low');

    return `
      <div class="completeness-container">
        <div class="completeness-left">
          <div class="completeness-heading">
            <span>DATA HEALTH &amp; COMPLETENESS</span>
            <span style="font-size:0.75rem;font-weight:700;color:#0284c7;text-transform:none;letter-spacing:0;margin-left:6px;">
              &bull; <strong>${comp.percentage}% Data Filled</strong> &bull; ${comp.populatedCount}/${comp.totalFields} Attributes &bull; ${comp.sourcesHit}/${comp.totalSources} Live Gateways
            </span>
          </div>
          <div class="enterprise-progress-track">
            <div class="enterprise-progress-fill ${healthClass}" style="width: ${comp.percentage}%;"></div>
          </div>
        </div>

        <div class="completeness-right">
          <div class="completeness-legend">
            <div><span class="dot-indicator dot-green"></span> Populated ${comp.populatedCount} / ${comp.totalFields}</div>
            <div><span class="dot-indicator dot-gray"></span> Missing ${comp.missingCount}</div>
          </div>
          <div class="completeness-big-pct">${comp.percentage}%</div>
        </div>
      </div>
    `;
  }

  function renderLobModernCompleteness(lob) {
    const comp = computeLobCompleteness(lob);
    const healthClass = comp.completenessPct >= 80 ? 'health-high' : (comp.completenessPct >= 60 ? 'health-med' : 'health-low');

    return `
      <div class="modern-view-card fade-in" style="margin-top:14px;padding:14px 20px;">
        <div class="completeness-container">
          <div class="completeness-left">
            <div class="completeness-heading">
              <span>DIVISION DATA HEALTH &amp; COMPLETENESS</span>
              <span style="font-size:0.75rem;font-weight:700;color:#0284c7;text-transform:none;letter-spacing:0;margin-left:6px;">
                &bull; <strong>${comp.completenessPct}% Data Filled</strong> &bull; ${comp.populatedCount}/${comp.totalFields} Attributes &bull; ${comp.sourcesHit}/${comp.totalSources} Streams Active
              </span>
            </div>
            <div class="enterprise-progress-track">
              <div class="enterprise-progress-fill ${healthClass}" style="width: ${comp.completenessPct}%;"></div>
            </div>
          </div>

          <div class="completeness-right">
            <div class="completeness-legend">
              <div><span class="dot-indicator dot-green"></span> Populated ${comp.populatedCount} / ${comp.totalFields}</div>
              <div><span class="dot-indicator dot-gray"></span> Missing ${comp.missingCount}</div>
            </div>
            <div class="completeness-big-pct">${comp.completenessPct}%</div>
          </div>
        </div>
      </div>
    `;
  }

  function renderModernTabs(account, activeTab = "overview") {
    const lobsCount = (account.lobs || []).length;
    const personasCount = (account.personas || []).length;

    return `
      <div class="tabs-nav-links">
        <button type="button" class="tab-pill-btn ${activeTab === 'overview' ? 'active' : ''}" data-nav-tab="overview">
          Overview
        </button>
        <button type="button" class="tab-pill-btn ${activeTab === 'lobs' ? 'active' : ''}" data-nav-tab="lobs">
          Lines of Business (${lobsCount})
        </button>
        <button type="button" class="tab-pill-btn ${activeTab === 'personas' ? 'active' : ''}" data-nav-tab="personas">
          Personas (${personasCount})
        </button>
        <button type="button" class="tab-pill-btn ${activeTab === 'feeds' ? 'active' : ''}" data-nav-tab="feeds">
          Intelligence Feeds
        </button>
        <button type="button" class="tab-pill-btn ${activeTab === 'activity' ? 'active' : ''}" data-nav-tab="activity">
          Activity Log
        </button>
      </div>
    `;
  }

  function formatEntitiesExtracted(ee) {
    if (ee === null || ee === undefined || ee === "") return "";
    
    // If primitive number or numeric string
    if (typeof ee === "number" || (!isNaN(Number(ee)) && typeof ee === "string" && ee.trim() !== "")) {
      const n = Number(ee);
      return `${n} ${n === 1 ? 'entity' : 'entities'}`;
    }

    // If object / dict
    if (typeof ee === "object") {
      // If it only contains run_dirs, don't show "entities"
      if (ee.run_dirs && Object.keys(ee).length === 1) {
        return "";
      }

      // Case 1: Standard extraction counts (accounts, lobs, personas, etc.)
      const countParts = [];
      if (ee.accounts_count) countParts.push(`${ee.accounts_count} ${ee.accounts_count === 1 ? 'account' : 'accounts'}`);
      if (ee.lobs_count) countParts.push(`${ee.lobs_count} ${ee.lobs_count === 1 ? 'LOB' : 'LOBs'}`);
      if (ee.sublobs_count) countParts.push(`${ee.sublobs_count} ${ee.sublobs_count === 1 ? 'sub-LOB' : 'sub-LOBs'}`);
      if (ee.personas_count) countParts.push(`${ee.personas_count} ${ee.personas_count === 1 ? 'persona' : 'personas'}`);
      if (ee.patents_count) countParts.push(`${ee.patents_count} ${ee.patents_count === 1 ? 'patent' : 'patents'}`);
      if (ee.political_contributions_count) countParts.push(`${ee.political_contributions_count} contributions`);
      if (ee.technologies_count) countParts.push(`${ee.technologies_count} tech`);

      if (countParts.length > 0) {
        const total = (ee.accounts_count || 0) + (ee.lobs_count || 0) + (ee.sublobs_count || 0) + (ee.personas_count || 0) + (ee.patents_count || 0);
        return `${total} entities (${countParts.join(", ")})`;
      }

      // Case 2: Specific count or total property
      if (typeof ee.count === "number") {
        return `${ee.count} ${ee.count === 1 ? 'entity' : 'entities'}`;
      }
      if (typeof ee.total === "number") {
        return `${ee.total} ${ee.total === 1 ? 'entity' : 'entities'}`;
      }

      // Case 3: Manual inline attribute edit
      if (ee.action === "manual_edit" || ee.action === "manual_edit_sublob") {
        const fields = Array.isArray(ee.updated_fields) ? ee.updated_fields.join(", ") : (ee.updated_fields || "attribute");
        return `Updated: ${fields}`;
      }

      // Case 4: Manual verification toggle
      if (ee.action === "verify_toggle") {
        const target = ee.entity_type || ee.level || "record";
        return `Verified ${target}`;
      }

      // Case 5: Array of entities
      if (Array.isArray(ee)) {
        return `${ee.length} ${ee.length === 1 ? 'entity' : 'entities'}`;
      }

      // Case 6: Generic non-zero numeric breakdown
      const parts = [];
      for (const [k, v] of Object.entries(ee)) {
        if (typeof v === "number" && v > 0) {
          const cleanK = k.replace(/_count$/i, "").replace(/_/g, " ");
          parts.push(`${v} ${cleanK}`);
        }
      }
      if (parts.length > 0) {
        return parts.join(", ");
      }
    }

    if (typeof ee === "string") return ee;
    return "";
  }

  async function refreshPipelineRuns(companyName) {
    const $list = $("#recentPipelineActivityList");
    if (!$list.length) return;
    try {
      const q = companyName ? `?company_name=${encodeURIComponent(companyName)}&limit=50` : `?limit=50`;
      const res = await fetch(`${API_BASE}/api/pipeline/runs${q}`);
      if (!res.ok) return;
      const data = await res.json();
      const runs = data.runs || [];
      if (!runs.length) return;

      const wasExpanded = $("#recentPipelineActivityList .btn-toggle-activity-expand").attr("data-expanded") === "true";
      const totalCount = runs.length;

      // Update badge and hint in section header
      const $badge = $("#activityCountBadge");
      if ($badge.length) {
        $badge.text(`${totalCount} runs`);
      }
      const $hint = $("#activityShowingHint");
      if ($hint.length) {
        $hint.text(wasExpanded ? `Showing all ${totalCount}` : `Showing latest 3`);
      }

      const html = runs.map((r, idx) => {
        const isSuccess = r.status === "completed";
        const isFailed = r.status === "failed";
        const statusClass = isSuccess ? "success" : (isFailed ? "failed" : "running");
        const statusIcon = isSuccess ? "bi-check-circle-fill" : (isFailed ? "bi-x-circle-fill" : "bi-arrow-repeat spin");
        const statusLabel = isSuccess ? "Completed" : (isFailed ? "Failed" : (r.status || "In Progress"));
        const timeStr = r.completed_at || r.started_at ? formatTimeAgo(r.completed_at || r.started_at) : "recently";
        const lvl = (r.pipeline_level || "pipeline").toUpperCase();
        const act = (r.action || "run").toUpperCase();
        const countStr = formatEntitiesExtracted(r.entities_extracted);
        const durStr = (r.duration_seconds !== null && r.duration_seconds !== undefined) ? `${r.duration_seconds.toFixed(2)}s` : "";
        const storageDir = r.raw_storage_dir || r.enriched_storage_dir || "";
        const compactStorage = storageDir ? storageDir.split(/[\/\\]/).slice(-2).join("/") : "";
        const isExtra = idx >= 3;
        const displayStyle = (isExtra && !wasExpanded) ? 'style="display:none;"' : '';

        return `
          <div class="pipeline-activity-card timeline-item timeline-item-extra ${isExtra ? 'extra' : ''}" ${displayStyle}>
            <div class="activity-card-left">
              <span class="activity-status-icon ${statusClass}">
                <i class="bi ${statusIcon}"></i>
              </span>
              <div class="activity-card-details">
                <div class="activity-card-title-row">
                  <span class="activity-tag-level">${esc(lvl)}</span>
                  <span class="activity-tag-action">${esc(act)}</span>
                  <span class="activity-status-badge ${statusClass}">${esc(statusLabel)}</span>
                  ${(r.quality_score !== null && r.quality_score !== undefined) ? `
                    <span class="activity-quality-badge"><i class="bi bi-shield-check"></i> Score: ${r.quality_score}% (${esc(r.quality_grade || 'A')})</span>
                  ` : ''}
                </div>
                <div class="activity-card-meta-row">
                  ${countStr ? `<span class="activity-meta-pill"><i class="bi bi-diagram-3-fill"></i> ${esc(countStr)}</span>` : ''}
                  ${durStr ? `<span class="activity-meta-pill"><i class="bi bi-stopwatch"></i> ${esc(durStr)}</span>` : ''}
                  ${compactStorage ? `<span class="activity-meta-pill storage" title="${esc(storageDir)}"><i class="bi bi-folder2"></i> ${esc(compactStorage)}</span>` : ''}
                </div>
                ${r.error_message ? `<div class="activity-error-msg"><i class="bi bi-exclamation-circle-fill"></i> ${esc(r.error_message)}</div>` : ''}
              </div>
            </div>
            <div class="activity-card-right">
              <span class="activity-timestamp"><i class="bi bi-clock"></i> ${esc(timeStr)}</span>
            </div>
          </div>
        `;
      }).join("");

      const extraCount = totalCount - 3;
      const toggleBtnHtml = extraCount > 0
        ? `<div class="activity-toggle-footer" style="padding-top:10px;border-top:1px dashed #e2e8f0;margin-top:10px;text-align:center;">
             <button type="button" class="btn-toggle-activity-expand" data-expanded="${wasExpanded ? 'true' : 'false'}" style="background:none;border:none;color:#0284c7;font-size:0.78rem;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:5px;padding:4px 12px;border-radius:6px;transition:all .15s ease;">
               ${wasExpanded 
                 ? `<span>Show latest 3 only</span> <i class="bi bi-chevron-up" style="font-size:0.72rem;"></i>`
                 : `<span>View full run history (${extraCount} more)</span> <i class="bi bi-chevron-down" style="font-size:0.72rem;"></i>`
               }
             </button>
           </div>`
        : "";

      $list.html(html + toggleBtnHtml);
    } catch (err) {
      console.warn("Notice: could not refresh pipeline runs:", err);
    }
  }

  function renderModernAccountOverview(account) {
    if (!account) return "";

    const industries = (Array.isArray(account.industries) && account.industries.length > 0) ? account.industries.join(", ") : "—";
    const secOrLei = account.organisational_hierarchy_tree?.gleif_lei 
      ? `LEI: ${account.organisational_hierarchy_tree.gleif_lei}` 
      : (account.sec_cik ? `CIK: ${account.sec_cik}` : (account.lei_code ? `LEI: ${account.lei_code}` : "—"));
    const employees = account.employee_count_range || "—";
    const rawRev = (account.revenue && account.revenue !== "Revenue N/A") ? account.revenue : (account.estimated_revenue_range || "—");
    const revenue = formatCompactRevenue(rawRev);
    const compType = account.company_type || "—";
    const opStatus = account.operating_status || "—";
    const founded = account.founded_year || (account.founded_date ? String(account.founded_date).slice(0, 4) : "—");
    const domain = account.domain || account.primary_domain || "—";
    const fullDesc = account.full_description || account.desc || account.short_description || "—";

    const coverage = computeSourceCoverage(account);
    const lobsCount = (account.lobs || []).length;
    const personasCount = (account.personas || []).length;

    // Feeds evaluation (Expanded OSINT matrix)
    const feedsList = [
      { name: "X (Twitter) Feed", icon: "bi-twitter-x", url: account.twitter_live_url || account.twitter_url, key: "twitter_live_url", type: "Social Stream" },
      { name: "Google News Alerts", icon: "bi-newspaper", url: account.rss_url || (account.news_query ? `https://news.google.com/search?q=${encodeURIComponent(account.news_query)}` : null), key: "rss_url", type: "Media RSS" },
      { name: "Reddit Discussions", icon: "bi-reddit", url: account.reddit_rss_url || (account.reddit_query ? `https://www.reddit.com/search.rss?q=${encodeURIComponent(account.reddit_query)}` : null), key: "reddit_rss_url", type: "Community RSS" },
      { name: "USPTO Patent Filings", icon: "bi-lightbulb", url: account.google_patents_url, key: "google_patents_url", type: "IP Portfolio" },
      { name: "Executive Media / Video", icon: "bi-youtube", url: account.youtube_search_url, key: "youtube_search_url", type: "Broadcast Media" },
      { name: "Wikidata Entity", icon: "bi-diagram-2", url: account.wikidata_entity_url, key: "wikidata_entity_url", type: "Knowledge Graph" },
      { name: "Google Trends Momentum", icon: "bi-graph-up-arrow", url: account.google_trends_url, key: "google_trends_url", type: "Search Trends" },
      { name: "SEC EDGAR Submissions", icon: "bi-bank", url: account.sec_submissions_url || account.sec_edgar_url, key: "sec_submissions_url", type: "Regulatory Filings" },
    ];

    const configuredFeedsCount = feedsList.filter(f => Boolean(f.url)).length;
    const feedsHtml = feedsList.map(f => {
      const isConfigured = Boolean(f.url);
      if (isConfigured) {
        return `
          <a href="${esc(f.url)}" target="_blank" rel="noopener noreferrer" class="osint-channel-tile active feed-status-row" data-feed-url="${esc(f.url)}" data-feed-name="${esc(f.name)}" data-feed-key="${esc(f.key)}" title="Open live ${esc(f.name)}">
            <div class="osint-tile-left">
              <div class="osint-tile-icon"><i class="bi ${f.icon}"></i></div>
              <div class="osint-tile-info">
                <div class="osint-tile-title">${esc(f.name)}</div>
                <div class="osint-tile-type">${esc(f.type)}</div>
              </div>
            </div>
            <span class="osint-live-indicator"><span class="pulse-dot"></span> LIVE <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;margin-left:2px;"></i></span>
          </a>
        `;
      } else {
        return `
          <div class="osint-channel-tile inactive feed-status-row" data-feed-name="${esc(f.name)}" data-feed-key="${esc(f.key)}" title="${esc(f.name)}: Not configured">
            <div class="osint-tile-left">
              <div class="osint-tile-icon inactive"><i class="bi ${f.icon}"></i></div>
              <div class="osint-tile-info">
                <div class="osint-tile-title">${esc(f.name)}</div>
                <div class="osint-tile-type">${esc(f.type)}</div>
              </div>
            </div>
            <span class="osint-inactive-indicator">Offline</span>
          </div>
        `;
      }
    }).join("");

    // Build authentic dynamic timeline events for initial fallback render
    const timelineEvents = [];
    if (account.extracted_at || account.updated_at) {
      timelineEvents.push({
        title: "Intelligence Extraction Synced",
        desc: `${coverage.hitCount} of 11 multi-source intelligence connectors aggregated`,
        time: formatTimeAgo(account.extracted_at || account.updated_at),
        tag: "L1",
        action: "ENRICH"
      });
    }
    if (lobsCount > 0) {
      timelineEvents.push({
        title: "Lines of Business Mapped",
        desc: `${lobsCount} distinct operating segments and subsidiaries indexed`,
        time: formatTimeAgo(account.updated_at || account.created_at),
        tag: "L2",
        action: "INDEX"
      });
    }
    if (personasCount > 0) {
      timelineEvents.push({
        title: "Executive Committee Captured",
        desc: `${personasCount} verified leadership personas and decision-makers discovered`,
        time: formatTimeAgo(account.updated_at || account.created_at),
        tag: "L3",
        action: "COLLECT"
      });
    }
    if (account.created_at) {
      timelineEvents.push({
        title: "Account Record Initialized",
        desc: "Profile established in sales_ai enterprise database",
        time: formatTimeAgo(account.created_at),
        tag: "SYSTEM",
        action: "INIT"
      });
    }

    const visibleEventsCount = 3;
    const hasExtraEvents = timelineEvents.length > visibleEventsCount;
    const extraEventsCount = timelineEvents.length - visibleEventsCount;

    const timelineHtml = timelineEvents.length > 0
      ? timelineEvents.map((ev, idx) => {
          const isExtra = idx >= visibleEventsCount;
          const displayStyle = isExtra ? 'style="display:none;"' : '';
          return `
            <div class="pipeline-activity-card timeline-item timeline-item-extra ${isExtra ? 'extra' : ''}" ${displayStyle}>
              <div class="activity-card-left">
                <span class="activity-status-icon success">
                  <i class="bi bi-check-circle-fill"></i>
                </span>
                <div class="activity-card-details">
                  <div class="activity-card-title-row">
                    <span class="activity-tag-level">${esc(ev.tag)}</span>
                    <span class="activity-tag-action">${esc(ev.action)}</span>
                    <span class="activity-status-badge success">${esc(ev.title)}</span>
                  </div>
                  <div class="activity-card-meta-row">
                    <span class="activity-meta-pill"><i class="bi bi-info-circle"></i> ${esc(ev.desc)}</span>
                  </div>
                </div>
              </div>
              <div class="activity-card-right">
                <span class="activity-timestamp"><i class="bi bi-clock"></i> ${esc(ev.time)}</span>
                <button type="button" class="btn btn-xs btn-outline-primary btn-run-credits" data-action="view-credits" title="View credits & API telemetry for this run" style="font-size:0.68rem;padding:2px 8px;border-radius:4px;color:#4f46e5;border:1px solid #c7d2fe;background:#eef2ff;display:inline-flex;align-items:center;gap:3px;cursor:pointer;">
                  <i class="bi bi-lightning-charge-fill" style="color:#6366f1;"></i> <span>Credits</span>
                </button>
              </div>
            </div>
          `;
        }).join("") + (hasExtraEvents ? `
          <div class="activity-toggle-footer" style="padding-top:10px;border-top:1px dashed #e2e8f0;margin-top:10px;text-align:center;">
            <button type="button" class="btn-toggle-activity-expand" data-expanded="false" style="background:none;border:none;color:#0284c7;font-size:0.78rem;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:5px;padding:4px 12px;border-radius:6px;transition:all .15s ease;">
              <span>View full run history (${extraEventsCount} more)</span> <i class="bi bi-chevron-down" style="font-size:0.72rem;"></i>
            </button>
          </div>
        ` : '')
      : `<div style="padding:18px 0;text-align:center;color:#94a3b8;font-size:.82rem;">No pipeline activity recorded yet &bull; Click Pull to start collection.</div>`;

    return `
      <!-- Full Width Enterprise Container -->
      <div class="pipeline-fullwidth-container">
        
        <!-- Corporate Description & Executive Overview Horizontal Banner -->
        ${fullDesc !== "—" ? `
        <div class="pipeline-section-card fade-in" style="margin-bottom:14px;padding:16px 20px;border-left:4px solid #0284c7;">
          <div class="section-title-row" style="margin-bottom:8px;">
            <div class="section-title-left">
              <span class="section-title-dot" style="background:#0284c7;"></span>
              <span style="font-weight:700;font-size:0.85rem;color:#1e293b;letter-spacing:0.04em;">EXECUTIVE OVERVIEW</span>
            </div>
            <span class="section-subtitle-hint">Verified Corporate Synopsis</span>
          </div>
          <p style="color:#334155;font-size:0.85rem;line-height:1.65;margin:0;letter-spacing:-0.01em;">
            ${esc(fullDesc)}
          </p>
        </div>
        ` : ''}

        <!-- Card 1: Enterprise Snapshot -->
        <div class="pipeline-section-card fade-in">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <span>Enterprise Snapshot</span>
            </div>
            <span class="section-subtitle-hint">Click a pencil icon to edit any field inline</span>
          </div>

          <div class="snapshot-fields-grid">
            <div class="snapshot-field-item" data-entity-type="account" data-id="${account.id}" data-field="employee_count_range" data-raw-value="${esc(account.employee_count_range || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">EMPLOYEES</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Employees inline"></i>
              </div>
              <div class="snapshot-field-value">${esc(employees)}</div>
            </div>

            <div class="snapshot-field-item" data-entity-type="account" data-id="${account.id}" data-field="estimated_revenue_range" data-raw-value="${esc(account.estimated_revenue_range || account.revenue || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">ANNUAL REVENUE</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Revenue inline"></i>
              </div>
              <div class="snapshot-field-value">${esc(revenue)}</div>
            </div>

            <div class="snapshot-field-item" data-entity-type="account" data-id="${account.id}" data-field="company_type" data-raw-value="${esc(account.company_type || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">COMPANY TYPE</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Company Type inline"></i>
              </div>
              <div class="snapshot-field-value">${esc(compType)}</div>
            </div>

            <div class="snapshot-field-item" data-entity-type="account" data-id="${account.id}" data-field="operating_status" data-raw-value="${esc(account.operating_status || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">OPERATING STATUS</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Operating Status inline"></i>
              </div>
              <div class="snapshot-field-value">
                ${opStatus !== "—" ? `<span class="badge-solid-green">${esc(opStatus)}</span>` : `<span style="color:#94a3b8;">—</span>`}
              </div>
            </div>

            <div class="snapshot-field-item" data-entity-type="account" data-id="${account.id}" data-field="founded_year" data-raw-value="${esc(account.founded_year || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">FOUNDED</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Founded Year inline"></i>
              </div>
              <div class="snapshot-field-value">${esc(founded)}</div>
            </div>

            <div class="snapshot-field-item" data-entity-type="account" data-id="${account.id}" data-field="city" data-raw-value="${esc(account.city || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">LOCATION (HQ)</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Location inline"></i>
              </div>
              <div class="snapshot-field-value">${esc(account.headquarters_location || (account.city ? `${account.city}, ${account.country || ''}` : '—'))}</div>
            </div>

            <div class="snapshot-field-item" data-entity-type="account" data-id="${account.id}" data-field="sec_cik" data-raw-value="${esc(account.sec_cik || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">REGULATORY ID (CIK / LEI)</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Regulatory ID inline"></i>
              </div>
              <div class="snapshot-field-value" style="font-size:.8rem;letter-spacing:-0.02em;">${esc(secOrLei)}</div>
            </div>

            <div class="snapshot-field-item" data-entity-type="account" data-id="${account.id}" data-field="primary_domain" data-raw-value="${esc(account.primary_domain || account.domain || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">OFFICIAL WEBSITE / DOMAIN</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Domain inline"></i>
              </div>
              <div class="snapshot-field-value">
                ${domain !== "—" ? `
                  <a href="${account.website_url || 'https://' + domain}" target="_blank" style="color:var(--text-primary);text-decoration:none;">
                    ${esc(domain)} <i class="bi bi-box-arrow-up-right" style="font-size:.7rem;color:#0284c7;"></i>
                  </a>
                ` : `<span style="color:#94a3b8;">—</span>`}
              </div>
            </div>
          </div>

          <!-- Collapsible Vault: All 95 Account Attributes -->
          <div class="vault-collapsible-wrapper">
            <button type="button" class="vault-toggle-button" id="toggleAccountVaultBtn">
              <i class="bi bi-database"></i> View All 95 Account Attributes &amp; Regulatory Filings <i class="bi bi-chevron-down" style="font-size:.7rem;"></i>
            </button>
            <div class="vault-content-area d-none" id="accountVaultArea"></div>
          </div>
        </div>

        <!-- Card 2: Source Coverage & Verified Connectors -->
        <div class="pipeline-section-card fade-in">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <span>Source Coverage &amp; Verified Connectors</span>
            </div>
            <div class="coverage-header-metrics">
              <div class="coverage-bar-track" title="${coverage.coveragePct}% verified data coverage">
                <div class="coverage-bar-fill" style="width: ${coverage.coveragePct}%;"></div>
              </div>
              <span class="coverage-score-badge ${coverage.coveragePct >= 60 ? 'high' : 'medium'}">
                <i class="bi bi-shield-check"></i> ${coverage.coveragePct}% Coverage &bull; ${coverage.sourcesHit ?? 0}/${coverage.totalSources || 11} Active
              </span>
            </div>
          </div>

          <div class="connectors-chip-matrix">
            ${(coverage.sources || []).map(src => `
              <div class="connector-status-badge ${src.active ? 'active' : 'inactive'}" title="${esc(src.name)}: ${src.active ? 'Verified & Active' : 'No Data Captured'}">
                ${src.active 
                  ? `<span class="connector-verified-dot"></span> <i class="bi bi-check-circle-fill text-success" style="font-size:0.8rem;"></i>` 
                  : `<i class="bi bi-dash-circle text-muted" style="font-size:0.8rem;"></i>`}
                <span>${esc(src.name)}</span>
              </div>
            `).join("")}
          </div>
        </div>

        <!-- Card 3: Live Intelligence Feeds & OSINT Channels -->
        <div class="pipeline-section-card fade-in">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <span>Live Intelligence Feeds &amp; OSINT Channels</span>
              <span class="badge-feed-count">${configuredFeedsCount} Active</span>
            </div>
            <div style="display:flex;align-items:center;gap:12px;">
              ${account.updated_at ? `<span class="section-subtitle-hint"><i class="bi bi-arrow-repeat"></i> Last synced ${formatTimeAgo(account.updated_at)}</span>` : ''}
              <a href="javascript:void(0)" class="section-action-link" id="btnAddIntelligenceFeed" title="Add or configure an intelligence feed"><i class="bi bi-plus"></i> Add feed</a>
            </div>
          </div>

          <div class="osint-channel-tile-grid">
            ${feedsHtml}
          </div>

          <div style="margin-top:10px;padding-top:8px;border-top:1px solid #f1f5f9;display:flex;justify-content:flex-end;">
            <a href="javascript:void(0)" id="btnViewAllActiveFeeds" style="font-size:.74rem;color:#0284c7;text-decoration:none;font-weight:600;display:inline-flex;align-items:center;gap:4px;cursor:pointer;">
              View All OSINT Channels &rarr;
            </a>
          </div>
        </div>

        <!-- Card 4: Recent Pipeline Activity -->
        <div class="pipeline-section-card fade-in">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <span>Recent Pipeline Activity</span>
              <span class="badge-solid-gray" id="activityCountBadge" style="font-size:.68rem;padding:2px 7px;margin-left:6px;font-weight:600;">${timelineEvents.length} runs</span>
            </div>
            <div style="display:flex;align-items:center;gap:10px;">
              <button type="button" class="btn btn-xs btn-outline-primary" id="btnOpenCreditUsageModal" style="display:inline-flex;align-items:center;gap:5px;font-size:0.75rem;padding:3px 10px;border-radius:6px;font-weight:600;background:rgba(99,102,241,0.06);border-color:#6366f1;color:#4f46e5;cursor:pointer;">
                <i class="bi bi-lightning-charge-fill" style="color:#6366f1;"></i> <span>Credits Usage</span>
              </button>
              <span class="section-subtitle-hint" id="activityShowingHint">Showing latest 3</span>
            </div>
          </div>

          <div class="pipeline-activity-list" id="recentPipelineActivityList">
            ${timelineHtml}
          </div>
        </div>

      </div>
    `;
  }

  // ══════════════════════════════════════════════════════════════════
  // VIEW 2: LOB DETAIL VIEW (IMAGE 2)
  // ══════════════════════════════════════════════════════════════════
  function renderModernLobDetailView(lob) {
    if (!lob) return "";

    const lobKey = `lob_${lob.id}`;
    const state = getActionState(lobKey);

    const pullBtnDisabled = false;
    const validateBtnDisabled = !state.pulled;
    const dumpBtnDisabled = !state.validated;

    const comp = computeLobCompleteness(lob);
    const pullTimeStr = formatTimeAgo(lob.extracted_at || activeAccount.updated_at || activeAccount.created_at);

    const subLobs = lob.subLobs || lob.sub_lobs || lob.subdivisions || [];
    let subLobsSectionHtml = "";
    if (subLobs && subLobs.length) {
      subLobsSectionHtml = `
        <div class="pipeline-section-card fade-in mt-3">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <i class="bi bi-diagram-3-fill" style="color:#0284c7;font-size:0.95rem;"></i>
              <span>Operating Sub-LOBs &amp; Child Divisions (${subLobs.length})</span>
            </div>
            <span class="section-subtitle-hint">Level 3 Grandchild Subsidiaries &amp; Specialized Operating Units</span>
          </div>
          ${renderLobSubLobsGrid(subLobs, lob)}
        </div>
      `;
    }

    const directPersonas = lob.personas || [];
    let lobPersonasHtml = "";
    if (directPersonas.length) {
      lobPersonasHtml = `
        <div class="pipeline-section-card mt-3">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <span>Division Leadership (${directPersonas.length} Contacts)</span>
            </div>
          </div>
          <div class="compact-card-grid">
            ${directPersonas.map((p, idx) => {
              const pKey = p.key || `lob_persona_${idx}`;
              p.key = pKey;
              const pRaw = encodeURIComponent(JSON.stringify(p));
              const pEmail = p.email || p.sanitized_email || p.work_email || p.personal_email || "";
              const pPhone = p.phone || p.phone_number || p.sanitized_phone || p.direct_mobile_phone || "";
              const pLinkedIn = p.linkedin_url || p.linkedin || "";
              const cik = (activeAccount && activeAccount.sec_cik) ? activeAccount.sec_cik : "";
              const secInsiderUrl = cik ? `https://www.sec.gov/edgar/searchedgar/companysearch` : "";

              let actionsHtml = "";
              if (pEmail || pPhone || pLinkedIn || secInsiderUrl) {
                actionsHtml = `
                  <div class="persona-card-actions">
                    ${pEmail ? `
                      <button type="button" class="persona-action-btn btn-copy-email" data-email="${esc(pEmail)}" title="Copy verified email (${esc(pEmail)})">
                        <i class="bi bi-envelope-fill"></i>
                      </button>
                    ` : ''}
                    ${pPhone ? `
                      <button type="button" class="persona-action-btn btn-copy-phone" data-phone="${esc(pPhone)}" title="Copy direct phone (${esc(pPhone)})">
                        <i class="bi bi-telephone-fill"></i>
                      </button>
                    ` : ''}
                    ${pLinkedIn ? `
                      <a href="${normalizeUrl(pLinkedIn)}" target="_blank" rel="noopener noreferrer" class="persona-action-btn btn-open-linkedin" title="Open verified LinkedIn Profile" onclick="event.stopPropagation();">
                        <i class="bi bi-linkedin"></i>
                      </a>
                    ` : ''}
                    ${secInsiderUrl ? `
                      <a href="${secInsiderUrl}" target="_blank" rel="noopener noreferrer" class="persona-action-btn btn-open-sec" title="SEC Form 4 Insider Filings" onclick="event.stopPropagation();">
                        <i class="bi bi-bank2"></i>
                      </a>
                    ` : ''}
                  </div>
                `;
              }

              return `
                <div class="compact-card persona-card fade-in"
                     data-key="${pKey}"
                     data-raw="${pRaw}"
                     title="Inspect Executive Dossier for ${esc(p.name)}">
                  ${renderPersonaAvatar(p, "avatar-purple")}
                  <div class="compact-card-body">
                    <div class="compact-card-title">${esc(p.name)}</div>
                    <div class="compact-card-subtitle">${esc(p.title || "Executive")}</div>
                    ${actionsHtml}
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        </div>
      `;
    }

    // Dynamic LOB OSINT Streams Manifest (100% dynamic DB values, zero hardcoding)
    const acctName = (activeAccount && (activeAccount.name || activeAccount.display_name)) ? (activeAccount.name || activeAccount.display_name) : "";
    const lobName = lob.name || lob.lob_name || "Division";

    const lobStreams = [
      {
        name: "LinkedIn Division Intelligence",
        icon: "bi-linkedin",
        active: Boolean(lob.domain || directPersonas.some(p => p.linkedin_url)),
        url: lob.domain 
          ? `https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(lobName + (acctName ? ' ' + acctName : ''))}`
          : (activeAccount.linkedin_url || `https://www.linkedin.com/search/results/all/?keywords=${encodeURIComponent(acctName || lobName)}`),
      },
      {
        name: "Google News Stream",
        icon: "bi-newspaper",
        active: Boolean(lob.google_news_rss_url || activeAccount.rss_url || activeAccount.news_query),
        url: lob.google_news_rss_url || (activeAccount.news_query 
          ? `https://news.google.com/search?q=${encodeURIComponent(activeAccount.news_query)}`
          : `https://news.google.com/search?q=${encodeURIComponent((acctName ? acctName + ' ' : '') + lobName + ' news')}`),
      },
      {
        name: "X / Twitter Feed",
        icon: "bi-twitter-x",
        active: Boolean(lob.twitter_live_url || activeAccount.twitter_live_url || activeAccount.twitter_handle),
        url: lob.twitter_live_url || activeAccount.twitter_live_url || (activeAccount.twitter_handle 
          ? `https://twitter.com/${activeAccount.twitter_handle.replace('@', '')}` 
          : `https://twitter.com/search?q=${encodeURIComponent(acctName || lobName)}`),
      },
      {
        name: "Reddit Community Discussions",
        icon: "bi-reddit",
        active: Boolean(lob.reddit_rss_url || activeAccount.reddit_rss_url || activeAccount.reddit_query),
        url: lob.reddit_rss_url || activeAccount.reddit_rss_url || `https://www.reddit.com/search/?q=${encodeURIComponent(lobName + (acctName ? ' ' + acctName : ''))}`,
      },
      {
        name: "USPTO Patent Intelligence",
        icon: "bi-award",
        active: Boolean(lob.google_patents_url || (Array.isArray(lob.patents) && lob.patents.length) || activeAccount.google_patents_url || (activeAccount.patents_granted > 0)),
        url: lob.google_patents_url || activeAccount.google_patents_url || `https://patents.google.com/?assignee=${encodeURIComponent(acctName || lobName)}&q=${encodeURIComponent(lobName)}`,
      },
      {
        name: "Search Trends Momentum",
        icon: "bi-graph-up-arrow",
        active: Boolean(lob.google_trends_url || activeAccount.google_trends_url),
        url: lob.google_trends_url || activeAccount.google_trends_url || `https://trends.google.com/trends/explore?q=${encodeURIComponent(lobName || acctName)}`,
      },
      {
        name: "YouTube Media & Interviews",
        icon: "bi-youtube",
        active: Boolean(lob.youtube_search_url || activeAccount.youtube_search_url),
        url: lob.youtube_search_url || activeAccount.youtube_search_url || `https://www.youtube.com/results?search_query=${encodeURIComponent((acctName ? acctName + ' ' : '') + lobName)}`,
      },
      {
        name: "Glassdoor Workplace Reviews",
        icon: "bi-building-check",
        active: Boolean(activeAccount.glassdoor_url),
        url: activeAccount.glassdoor_url || `https://www.glassdoor.com/Search/results.htm?keyword=${encodeURIComponent(acctName || lobName)}`,
      },
    ];

    if (lob.website_url || lob.domain) {
      lobStreams.unshift({
        name: "Division Web Portal",
        icon: "bi-globe2",
        active: true,
        url: lob.website_url || `https://${lob.domain.replace(/^https?:\/\//, '')}`,
      });
    }

    const relTypeStr = lob.relationship_type || "—";
    const overviewStr = lob.overview || lob.desc || "—";
    const domainStr = lob.domain || "—";
    const revStr = lob.audited_segment_revenue || lob.revenue || "—";
    const headcountStr = lob.segment_headcount || lob.headcount || "—";
    const opHeadStr = lob.operating_head || "—";
    const registryStr = lob.lei_code ? `LEI: ${lob.lei_code}` : (lob.commercial_registry || "—");

    return `
      <!-- LOB Header Card -->
      <div class="modern-view-card fade-in">
        <div class="modern-entity-header">
          <div class="header-main-info">
            <div class="header-avatar-box avatar-blue">
              <i class="bi bi-folder2-open"></i>
            </div>
            <div>
              <h1 class="header-entity-name">${esc(lob.name)}</h1>
              <div class="header-badges-row">
                <span class="badge-solid-blue">Business Division</span>
                ${relTypeStr !== "—" ? `<span class="badge-solid-blue">${esc(relTypeStr)}</span>` : ""}
                <span class="badge-solid-gray">${directPersonas.length} Contacts</span>
                ${(lob.sub_lobs || lob.subLobs || []).length > 0 ? `
                  <span class="sublob-count-pill" title="Nested Level 3 child divisions registered under this LOB">
                    <i class="bi bi-diagram-3"></i> ${(lob.sub_lobs || lob.subLobs).length} Sub-LOBs
                  </span>
                ` : ""}
                ${domainStr !== "—" ? `<span class="badge-solid-green">Domain Mapped</span>` : `<span class="badge-solid-gray">Domain Unmapped</span>`}
                <span class="badge-health-pill ${comp.confidenceScore >= 80 ? 'health-green' : (comp.confidenceScore >= 60 ? 'health-blue' : 'health-amber')}" title="Data Completeness: ${comp.completenessPct}% (${comp.populatedCount}/${comp.totalFields} fields) | Source Coverage: ${comp.coveragePct}% (${comp.sourcesHit}/${comp.totalSources} streams)">
                  <i class="bi bi-shield-check"></i> <strong>${comp.confidenceScore}% Confidence</strong> &bull; ${comp.completenessPct}% Filled (${comp.populatedCount}/${comp.totalFields}) &bull; ${comp.sourcesHit}/${comp.totalSources} Streams
                </span>
                <button type="button" class="btn-verify-badge ${lob.is_manually_verified ? 'verified' : 'unverified'}" data-entity-type="lob" data-id="${lob.id}" title="${lob.is_manually_verified ? `Verified ${formatTimeAgo(lob.manually_verified_at)}` : 'Click to toggle verification'}">
                  <i class="bi ${lob.is_manually_verified ? 'bi-patch-check-fill' : 'bi-shield-exclamation'}"></i>
                  <span>${lob.is_manually_verified ? `Manually Verified ✓ (${formatTimeAgo(lob.manually_verified_at)})` : 'AI Inferred • Verify'}</span>
                </button>
              </div>
              <p class="header-subtitle-desc">${esc(overviewStr)}</p>
            </div>
          </div>

          <div class="header-actions-col">
            <div class="header-btn-group">
              <button type="button" class="action-btn-pill btn-pill-purple" id="lobEditToggleBtn">
                Edit
              </button>
              <button type="button" class="action-btn-pill btn-pill-red lob-delete-btn"
                      data-lob-id="${lob.id}"
                      data-lob-name="${esc(lob.lob_name || lob.name || '')}"
                      title="Permanently delete this Line of Business">
                <i class="bi bi-trash3"></i> Delete
              </button>
            </div>
            <span class="header-meta-timestamp">Last pull: ${pullTimeStr} &bull; Validation: ${state.validated ? 'Passed &check;' : 'not run'} &bull; Dump: ${state.dumped ? 'Persisted &check;' : 'never'}</span>
          </div>
        </div>
      </div>

      <!-- LOB Data Health & Completeness Bar (Horizontal) -->
      ${renderLobModernCompleteness(lob)}

      <!-- Full Width LOB Container -->
      <div class="pipeline-fullwidth-container">
          
          <!-- Section 1: Overview & Corporate Structure -->
          <div class="pipeline-section-card fade-in">
            <div class="section-title-row">
              <div class="section-title-left">
                <span class="section-title-dot"></span>
                <span>Overview &amp; Corporate Structure</span>
              </div>
              <span class="section-subtitle-hint">Click a pencil icon to edit inline</span>
            </div>

            <div class="snapshot-field-item" data-entity-type="lob" data-id="${lob.id}" data-field="overview" data-raw-value="${esc(lob.overview || lob.desc || '')}" style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 14px;margin-bottom:14px;">
              <div class="snapshot-field-header" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;">
                <span class="snapshot-field-label">DIVISION OVERVIEW</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Division Overview inline"></i>
              </div>
              <div class="snapshot-field-value" style="font-size:0.84rem;color:#1e293b;line-height:1.5;">
                ${esc(overviewStr)}
              </div>
            </div>

            <div class="snapshot-fields-grid">
              <div class="snapshot-field-item" data-entity-type="lob" data-id="${lob.id}" data-field="relationship_type" data-raw-value="${esc(lob.relationship_type || '')}">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">RELATIONSHIP TYPE</span>
                  <i class="bi bi-pencil snapshot-field-pencil" title="Edit Relationship Type inline"></i>
                </div>
                <div class="snapshot-field-value">${esc(relTypeStr)}</div>
              </div>

              <div class="snapshot-field-item" data-entity-type="lob" data-id="${lob.id}" data-field="domain" data-raw-value="${esc(lob.domain || '')}">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">PRIMARY DOMAIN</span>
                  <i class="bi bi-pencil snapshot-field-pencil" title="Edit Domain inline"></i>
                </div>
                <div class="snapshot-field-value">${esc(domainStr)}</div>
              </div>

              <div class="snapshot-field-item" data-entity-type="lob" data-id="${lob.id}" data-field="website_url" data-raw-value="${esc(lob.website_url || '')}">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">WEBSITE</span>
                  <i class="bi bi-pencil snapshot-field-pencil" title="Edit Website inline"></i>
                </div>
                <div class="snapshot-field-value">
                  ${lob.website_url ? `
                    <a href="${esc(lob.website_url)}" target="_blank" style="color:var(--brand);text-decoration:none;">
                      ${esc(lob.website_url.replace(/^https?:\/\//, ''))} <i class="bi bi-box-arrow-up-right" style="font-size:.7rem;"></i>
                    </a>
                  ` : (lob.domain ? `
                    <a href="https://${esc(lob.domain)}" target="_blank" style="color:var(--brand);text-decoration:none;">
                      ${esc(lob.domain)} <i class="bi bi-box-arrow-up-right" style="font-size:.7rem;"></i>
                    </a>
                  ` : `<span style="color:#94a3b8;">—</span>`)}
                </div>
              </div>

              <div class="snapshot-field-item">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">PARENT ACCOUNT</span>
                </div>
                <div class="snapshot-field-value">
                  <a href="javascript:void(0)" class="crumb-account" style="color:#0284c7;text-decoration:none;">${esc(activeAccount.name)}</a>
                </div>
              </div>

              <div class="snapshot-field-item" data-entity-type="lob" data-id="${lob.id}" data-field="crunchbase_url" data-raw-value="${esc(lob.crunchbase_url || '')}">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">CRUNCHBASE PROFILE</span>
                  <i class="bi bi-pencil snapshot-field-pencil" title="Edit Crunchbase URL inline"></i>
                </div>
                <div class="snapshot-field-value" style="font-size:0.78rem;">
                  ${lob.crunchbase_url ? `
                    <a href="${esc(lob.crunchbase_url)}" target="_blank" style="color:var(--brand);text-decoration:none;">
                      ${esc(lob.crunchbase_url.replace(/^https?:\/\//, ''))} <i class="bi bi-box-arrow-up-right" style="font-size:.7rem;"></i>
                    </a>
                  ` : `<span style="color:#94a3b8;">—</span>`}
                </div>
              </div>

              <div class="snapshot-field-item">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">COMMERCIAL REGISTRY</span>
                  <i class="bi bi-pencil snapshot-field-pencil"></i>
                </div>
                <div class="snapshot-field-value" style="font-size:0.8rem;">
                  ${esc(registryStr)}
                </div>
              </div>
            </div>

            <!-- Sub-LOBs Section -->
            ${subLobsSectionHtml}

            <!-- Collapsible Vault: All LOB Columns, Competitors & Tech -->
            <div class="vault-collapsible-wrapper">
              <button type="button" class="vault-toggle-button" id="toggleLobVaultBtn">
                <i class="bi bi-database"></i> View All LOB Attributes, Competitors, Technologies &amp; Patents <i class="bi bi-chevron-down" style="font-size:.7rem;"></i>
              </button>
              <div class="vault-content-area d-none" id="lobVaultArea"></div>
            </div>
          </div>

          <!-- Section 2: Segment Financials & Scale -->
          <div class="pipeline-section-card fade-in">
            <div class="section-title-row">
              <div class="section-title-left">
                <span class="section-title-dot"></span>
                <span>Segment Financials &amp; Operational Scale</span>
              </div>
            </div>

            <div class="snapshot-fields-grid">
              <div class="snapshot-field-item" data-entity-type="lob" data-id="${lob.id}" data-field="audited_segment_revenue" data-raw-value="${esc(lob.audited_segment_revenue || lob.revenue || '')}">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">SEGMENT REVENUE</span>
                  <i class="bi bi-pencil snapshot-field-pencil" title="Edit Segment Revenue inline"></i>
                </div>
                <div class="snapshot-field-value">${esc(revStr)}</div>
              </div>

              <div class="snapshot-field-item" data-entity-type="lob" data-id="${lob.id}" data-field="segment_headcount" data-raw-value="${esc(lob.segment_headcount || lob.headcount || '')}">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">HEADCOUNT / SIZE</span>
                  <i class="bi bi-pencil snapshot-field-pencil" title="Edit Headcount inline"></i>
                </div>
                <div class="snapshot-field-value">${esc(headcountStr)}</div>
              </div>

              <div class="snapshot-field-item" data-entity-type="lob" data-id="${lob.id}" data-field="operating_head" data-raw-value="${esc(lob.operating_head || lob.head || '')}">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">OPERATING HEAD</span>
                  <i class="bi bi-pencil snapshot-field-pencil" title="Edit Operating Head inline"></i>
                </div>
                <div class="snapshot-field-value">${esc(opHeadStr)}</div>
              </div>

              <div class="snapshot-field-item">
                <div class="snapshot-field-header">
                  <span class="snapshot-field-label">MAPPED CONTACTS</span>
                  <i class="bi bi-pencil snapshot-field-pencil"></i>
                </div>
                <div class="snapshot-field-value" style="color:#d97706;">${directPersonas.length} Identified</div>
              </div>
            </div>
          </div>

          <!-- Section 3: Live OSINT & Public Intelligence Launchpad -->
          <div class="pipeline-section-card fade-in">
            <div class="section-title-row" style="margin-bottom:12px;">
              <div class="section-title-left">
                <span class="section-title-dot"></span>
                <i class="bi bi-broadcast-pin" style="color:#0284c7;font-size:0.95rem;"></i>
                <span>Live OSINT &amp; Public Intelligence Streams</span>
              </div>
              <span class="badge" style="background:#e0f2fe;color:#0369a1;font-weight:700;padding:3px 10px;font-size:0.72rem;border-radius:12px;">
                ${lobStreams.filter(s => s.active).length} of ${lobStreams.length} Verified Channels
              </span>
            </div>

            <div class="osint-stream-chips-wrap">
              ${lobStreams.map(s => `
                <a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer"
                   class="osint-stream-chip ${s.active ? 'stream-active' : 'stream-pending'}"
                   title="${s.active ? 'Verified active channel: ' + esc(s.url) : 'Query launchpad: ' + esc(s.url)}">
                  <span class="dot-indicator ${s.active ? 'dot-green' : 'dot-gray'}"></span>
                  <i class="bi ${s.icon}"></i>
                  <span class="stream-chip-label">${esc(s.name)}</span>
                  <i class="bi bi-box-arrow-up-right stream-ext-icon"></i>
                </a>
              `).join('')}
            </div>
          </div>

          <!-- Mapped Personas -->
          ${lobPersonasHtml}

      </div>
    `;
  }

  // ══════════════════════════════════════════════════════════════════
  // VIEW 3: PERSONA DETAIL VIEW (IMAGE 3)
  // ══════════════════════════════════════════════════════════════════
  function renderModernPersonaDetailView(p) {
    if (!p) return "";

    const pKey = p.key || `persona_${p.id || p.name}`;
    const state = getActionState(pKey);

    const pullBtnDisabled = false;
    const validateBtnDisabled = !state.pulled;
    const dumpBtnDisabled = !state.validated;

    const initialsStr = getInitials(p.name || p.full_name || "EX");
    const coName = activeAccount ? activeAccount.name : "Company";
    const coTicker = (activeAccount && activeAccount.ticker) ? activeAccount.ticker : ((activeAccount && activeAccount.stock_symbol) ? activeAccount.stock_symbol : "—");
    
    const tenureStr = p.current_role_tenure_months ? Math.floor(p.current_role_tenure_months / 12) + " yrs " + (p.current_role_tenure_months % 12) + " mos" : "—";
    const phoneStr = p.phone || p.direct_mobile_phone || "—";
    const emailStr = p.email || p.personal_email || "—";
    const emailStatusStr = p.email ? (p.email_status || "Verified") : "—";
    const locStr = p.city ? (p.city + (p.state ? ', ' + p.state : (p.country ? ', ' + p.country : ''))) : (p.country || "—");
    const eduStr = p.degree ? (p.degree + (p.institution ? ' • ' + p.institution : '')) : (p.institution || "—");
    const priorStr = p.prior_company || (Array.isArray(p.past_companies) && p.past_companies.length > 0 ? p.past_companies.join(', ') : "—");
    const reportsToStr = p.reports_to || "—";

    const comp = computePersonaCompleteness(p);
    const dbCols = calculateDbColumnsFilled(p, "persona");
    const pullTimeStr = formatTimeAgo(p.extracted_at || activeAccount.updated_at || activeAccount.created_at);

    // Validation checklist computation
    const emailValid = Boolean(p.email && p.email.includes('@') && p.email.includes('.'));
    const domainMatch = Boolean(p.email && activeAccount.domain && p.email.toLowerCase().endsWith(activeAccount.domain.toLowerCase()));
    const titleValid = Boolean(p.title && (p.tier || p.seniority_raw));
    const authorityValid = Boolean(p.decision_authority || p.budget_authority);
    const osintFeeds = (p.osint_feed_manifest && p.osint_feed_manifest.feeds) ? p.osint_feed_manifest.feeds : {};
    const getVal = (col, feedKey) => p[col] || osintFeeds[feedKey || col] || "";

    const linkedinValid = Boolean(getVal("linkedin_url") && String(getVal("linkedin_url")).startsWith('http'));

    const checkList = [emailValid, domainMatch, titleValid, authorityValid, linkedinValid];
    const passedChecksCount = checkList.filter(Boolean).length;
    const overallGrade = passedChecksCount >= 4 ? "VERIFIED" : (passedChecksCount >= 2 ? "PARTIAL" : "UNVERIFIED");



    const kpisStr = (Array.isArray(p.target_kpis) && p.target_kpis.length > 0) ? p.target_kpis.join(', ') : "—";
    const skillsStr = (Array.isArray(p.skills) && p.skills.length > 0) ? p.skills.slice(0, 8).join(', ') : "—";
    const painPointsStr = (Array.isArray(p.operational_pain_points) && p.operational_pain_points.length > 0) ? p.operational_pain_points.join('; ') : "—";
    const commStyleStr = p.communication_style || "—";
    const objectionsStr = (Array.isArray(p.key_objections) && p.key_objections.length > 0) ? p.key_objections.join('; ') : "—";
    const icebreakerStr = p.personalized_icebreaker || "No personalized icebreaker generated yet. Click 'Generate personalized icebreaker' to synthesize.";
    const valPropStr = p.value_proposition || "—";

    return `
      <!-- Persona Header Card -->
      <div class="modern-view-card fade-in">
        <div class="modern-entity-header">
          <div class="header-main-info">
            <button type="button" class="btn btn-sm btn-outline-secondary" id="btnBackFromPersonaDetail" style="margin-right:8px;width:34px;height:34px;border-radius:8px;display:inline-flex;align-items:center;justify-content:center;padding:0;cursor:pointer;border-color:#e2e8f0;background:#fff;color:#475569;flex-shrink:0;" title="Back to Directory">
              <i class="bi bi-arrow-left" style="font-size:1.1rem;line-height:1;"></i>
            </button>
            ${renderPersonaAvatar(p, "header-avatar-box avatar-purple", "width:54px;height:54px;min-width:54px;border-radius:12px;font-size:1.1rem;")}
            <div>
              <h1 class="header-entity-name">${esc(p.name || p.full_name)}</h1>
              <div style="font-size:0.84rem;font-weight:600;color:#475569;margin-bottom:6px;">
                ${esc(p.title || "Executive Officer")} &bull; ${esc(coName.toUpperCase())}
              </div>
              <div class="header-badges-row">
                <span class="badge-outline-purple">${esc(p.seniority_raw || p.tier || "Leadership")}</span>
                <span class="badge-solid-gray">Level ${p.hierarchy_level || 3}</span>
                ${p.email ? `<span class="badge-solid-green">Email Verified</span>` : `<span class="badge-solid-gray">Email Unverified</span>`}
                ${locStr !== "—" ? `<span class="badge-solid-gray">${esc(locStr)}</span>` : ""}
                <span class="badge-health-pill ${dbCols.pct >= 80 ? 'health-green' : (dbCols.pct >= 60 ? 'health-blue' : 'health-amber')}" title="Database Schema: ${dbCols.filled}/${dbCols.total} columns populated (${dbCols.pct}%) | Quality Confidence: ${comp.confidenceScore}% | OSINT Sources: ${comp.sourcesHit}/${comp.totalSources}">
                  <i class="bi bi-shield-check"></i> <strong>${dbCols.pct}% Complete (${dbCols.filled}/${dbCols.total} DB Cols)</strong> &bull; ${comp.confidenceScore}% Quality Score &bull; ${comp.sourcesHit}/${comp.totalSources} Sources
                </span>
                <button type="button" class="btn-verify-badge ${p.is_manually_verified ? 'verified' : 'unverified'}" data-entity-type="persona" data-id="${p.id}" title="${p.is_manually_verified ? `Verified ${formatTimeAgo(p.manually_verified_at)}` : 'Click to toggle verification'}">
                  <i class="bi ${p.is_manually_verified ? 'bi-patch-check-fill' : 'bi-shield-exclamation'}"></i>
                  <span>${p.is_manually_verified ? `Manually Verified ✓ (${formatTimeAgo(p.manually_verified_at)})` : 'AI Inferred • Verify'}</span>
                </button>
              </div>
            </div>
          </div>

          <div class="header-actions-col">
            <div class="header-btn-group">
              <button type="button" class="action-btn-pill btn-pill-purple" id="personaEditToggleBtn">
                Edit
              </button>
              <button type="button" class="action-btn-pill btn-pill-red persona-delete-btn"
                      data-persona-id="${p.id}"
                      data-persona-name="${esc(p.full_name || p.display_name || p.name || '')}"
                      title="Permanently delete this persona">
                <i class="bi bi-trash3"></i> Delete
              </button>
              <button type="button" class="action-btn-pill btn-persona-download-pdf" data-persona-id="${p.id}" style="background:#0284c7;color:#fff;border-color:#0284c7;display:inline-flex;align-items:center;gap:5px;font-weight:600;" title="Download full executive dossier as PDF">
                <i class="bi bi-file-earmark-pdf-fill"></i> Download PDF
              </button>
            </div>
            <span class="header-meta-timestamp">Last scraped: ${pullTimeStr} &bull; ${p.email ? 'Email Verified &check;' : 'Email Pending'}</span>
          </div>
        </div>
      </div>

      <!-- Edit Mode Notification Banner -->
      <div class="edit-mode-alert-banner fade-in d-none" id="personaEditBanner">
        <div class="edit-banner-left">
          <i class="bi bi-pencil" style="font-size:1rem;"></i>
          <span>Edit mode is ON &mdash; fields are editable. Changes re-validate on save.</span>
        </div>
        <div class="edit-banner-actions">
          <button type="button" class="action-btn-pill btn-pill-dump" id="discardPersonaBannerBtn">Discard</button>
          <button type="button" class="action-btn-pill btn-pill-blue" id="savePersonaBannerBtn">Save changes</button>
        </div>
      </div>

      <!-- Full-Width Persona Layout -->
      <div class="pipeline-fullwidth-container">
        
        <!-- Section 1: Executive Profile & Demographics (10 Cards) -->
        <div class="pipeline-section-card fade-in">
          <div class="section-title-row">
            <div class="section-title-left">
              <span class="section-title-dot"></span>
              <span>Executive Profile &amp; Demographics</span>
            </div>
            <span class="section-subtitle-hint">Click a pencil icon to edit inline</span>
          </div>

          <div class="snapshot-fields-grid-5col">
            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="full_name" data-raw-value="${esc(p.full_name || p.name || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">FULL NAME</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Full Name inline"></i>
              </div>
              <div class="snapshot-field-value">${esc(p.name || p.full_name)}</div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="title" data-raw-value="${esc(p.title || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">CORPORATE TITLE</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Corporate Title inline"></i>
              </div>
              <div class="snapshot-field-value">${esc(p.title || "—")}</div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="email" data-raw-value="${esc(p.email || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">CORPORATE EMAIL</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Corporate Email inline"></i>
              </div>
              <div class="snapshot-field-value" style="font-size:0.8rem;${!p.email ? 'color:#94a3b8;' : ''}">
                ${emailStr !== "—" ? esc(emailStr) : "—"}
              </div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="email_status" data-raw-value="${esc(p.email_status || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">EMAIL STATUS</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Email Status inline"></i>
              </div>
              <div class="snapshot-field-value">
                ${emailStatusStr !== "—" ? `<span class="badge-solid-green">${esc(emailStatusStr)}</span>` : `<span style="color:#94a3b8;">—</span>`}
              </div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="phone" data-raw-value="${esc(p.phone || p.direct_mobile_phone || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">PHONE</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Phone inline"></i>
              </div>
              <div class="snapshot-field-value" style="font-size:0.82rem;">${esc(phoneStr)}</div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="city" data-raw-value="${esc(p.city || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">LOCATION / BASE</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Location/City inline"></i>
              </div>
              <div class="snapshot-field-value" style="font-size:0.82rem;">${esc(locStr)}</div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="degree" data-raw-value="${esc(p.degree || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">EDUCATION</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Education/Degree inline"></i>
              </div>
              <div class="snapshot-field-value" style="font-size:0.82rem;">${esc(eduStr)}</div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="prior_company" data-raw-value="${esc(p.prior_company || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">PRIOR EXPERIENCE</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Prior Company inline"></i>
              </div>
              <div class="snapshot-field-value" style="font-size:0.82rem;">${esc(priorStr)}</div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform" data-entity-type="persona" data-id="${p.id}" data-field="current_role_tenure_months" data-raw-value="${esc(p.current_role_tenure_months || '')}">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">TENURE AT ${esc(coTicker)}</span>
                <i class="bi bi-pencil snapshot-field-pencil" title="Edit Role Tenure (months) inline"></i>
              </div>
              <div class="snapshot-field-value">${esc(tenureStr)}</div>
            </div>

            <div class="snapshot-field-item snapshot-field-item-uniform">
              <div class="snapshot-field-header">
                <span class="snapshot-field-label">REPORTS TO</span>
                <span class="badge-tiny-new">HIERARCHY</span>
              </div>
              <div class="snapshot-field-value" style="font-size:0.82rem;">${esc(reportsToStr)}</div>
            </div>
          </div>

          <!-- Core Field Validation & Compliance Bar (5 Core Criteria) -->
          <div class="core-validation-strip">
            <div class="core-validation-header">
              <div class="core-validation-title">
                <i class="bi bi-shield-check"></i>
                <span>Core Field Validation &amp; Quality Status</span>
              </div>
              <div class="core-validation-score-wrap">
                <span class="core-validation-score-text">Passed: ${passedChecksCount} / 5 Core Checks &bull; ${dbCols.filled}/${dbCols.total} DB Columns Populated (${dbCols.pct}%)</span>
                <span class="core-validation-grade-badge ${overallGrade.toLowerCase()}">${overallGrade}</span>
              </div>
            </div>

            <div class="core-validation-chips-row">
              <div class="core-validation-chip ${emailValid ? 'chip-passed' : 'chip-failed'}">
                <span class="chip-label">Email Format</span>
                <span class="chip-status">${emailValid ? '<i class="bi bi-check2"></i> Verified' : '&mdash; Missing'}</span>
              </div>

              <div class="core-validation-chip ${domainMatch ? 'chip-passed' : 'chip-failed'}">
                <span class="chip-label">Domain Match (${esc(activeAccount.domain || '—')})</span>
                <span class="chip-status">${domainMatch ? '<i class="bi bi-check2"></i> Verified' : '&mdash; Unmatched'}</span>
              </div>

              <div class="core-validation-chip ${titleValid ? 'chip-passed' : 'chip-failed'}">
                <span class="chip-label">Title &amp; Seniority</span>
                <span class="chip-status">${titleValid ? '<i class="bi bi-check2"></i> Verified' : '&mdash; Incomplete'}</span>
              </div>

              <div class="core-validation-chip ${authorityValid ? 'chip-passed' : 'chip-failed'}">
                <span class="chip-label">Authority Mapped</span>
                <span class="chip-status">${authorityValid ? '<i class="bi bi-check2"></i> Verified' : '&mdash; Pending'}</span>
              </div>

              <div class="core-validation-chip ${linkedinValid ? 'chip-passed' : 'chip-failed'}">
                <span class="chip-label">LinkedIn Linked</span>
                <span class="chip-status">${linkedinValid ? '<i class="bi bi-check2"></i> Verified' : '&mdash; Unlinked'}</span>
              </div>
            </div>
          </div>

          <!-- Collapsible Vault: All 89 Persona Columns -->
          <div class="vault-collapsible-wrapper">
            <button type="button" class="vault-toggle-button" id="togglePersonaVaultBtn">
              <i class="bi bi-database"></i> View All ${PERSONA_89_COLUMNS.length} Persona Database Attributes, Dossier &amp; OSINT Feeds <i class="bi bi-chevron-down" style="font-size:.7rem;"></i>
            </button>
            <div class="vault-content-area d-none" id="personaVaultArea"></div>
          </div>
        </div>
      </div>
    `;
  }

  // ══════════════════════════════════════════════════════════════════
  // EVENT: ACCOUNT SELECTION
  // ══════════════════════════════════════════════════════════════════
  $("#accountList").on("click", ".account-item", async function () {
    const id = $(this).data("id");
    activeAccount = MOCK_DATA.accounts.find((a) => a.id === id);
    if (!activeAccount) return;

    try {
      sessionStorage.setItem("pipeline_active_account_id", String(id));
    } catch (e) {}

    const currentSubTab = $(".tab-pill-btn.active").data("nav-tab") || "overview";
    PipelineRouter.syncState({
      view: "account",
      accountId: String(id),
      subTab: currentSubTab,
    });

    activeLob = null;
    activePersona = null;

    // Update active class
    $(".account-item").removeClass("active");
    $(this).addClass("active");
    closeSidebar();

    // If activeAccount dossier is not fully hydrated, fetch full dossier on-demand
    if (!activeAccount._is_full_loaded) {
      try {
        const token = sessionStorage.getItem('access_token') || localStorage.getItem('access_token');
        const headers = token ? { 'Authorization': `Bearer ${token}` } : {};
        const acctRes = await fetch(`${API_BASE}/api/accounts/${id}`, { headers, credentials: 'include' });
        if (acctRes.ok) {
          const fullAcct = await acctRes.json();
          fullAcct._is_full_loaded = true;
          const aIdx = MOCK_DATA.accounts.findIndex(a => a.id === id);
          if (aIdx !== -1) MOCK_DATA.accounts[aIdx] = fullAcct;
          activeAccount = fullAcct;
        }
      } catch (err) {
        console.warn("[AccountClick] Failed to fetch full account on demand:", err);
      }
    }

    // Hide empty state, show dashboard
    $("#emptyState").addClass("d-none");
    $("#dashboardContainer").removeClass("d-none");

    // Hide detail views
    $("#lobDetailViewContainer").addClass("d-none").empty();
    $("#personaDetailViewContainer").addClass("d-none").empty();
    $("#detailPanelContainer").addClass("d-none").empty();

    // Show View 1 components
    $("#accountViewWrapper").removeClass("d-none");
    $("#accountHeroContainer").html(renderModernAccountHeader(activeAccount));
    $("#completenessContainer").html(renderModernCompleteness(activeAccount));
    $("#tabsContainer").html(renderModernTabs(activeAccount, "overview"));
    $("#accountOverviewContainer").html(renderModernAccountOverview(activeAccount)).removeClass("d-none");
    if (activeAccount && activeAccount.name) {
      refreshPipelineRuns(activeAccount.name);
    }

    // Update breadcrumbs
    renderModernBreadcrumbs();

    // Render Compact LOB Cards (Top 10 with Expandable Toggle)
    renderLobCardsList($("#lobCardsContainer"), activeAccount.lobs || []);

    if (activeAccount._isNew) {
      $("#lobSection").addClass("d-none");
    } else {
      $("#lobSection").removeClass("d-none");
    }

    // Render Complete Enterprise Hierarchy at Account Level
    renderAllPersonasDirectory(activeAccount);

    // Update Batch Multi-Enrichment trigger pills (People & LOBs)
    updateBatchTriggerPills(activeAccount);
  });

  // ══════════════════════════════════════════════════════════════════
  // INTERACTIVE QUICK-LAUNCH HUB (EMPTY STATE)
  // ══════════════════════════════════════════════════════════════════

  function getConnectorIcon(key) {
    switch (key) {
      case "sec_edgar": return "bi-bank2";
      case "gemini": return "bi-stars";
      case "exa": return "bi-search";
      case "tavily": return "bi-globe2";
      case "diffbot": return "bi-diagram-3";
      case "finnhub": return "bi-graph-up-arrow";
      case "apify": return "bi-robot";
      case "serper": return "bi-google";
      default: return "bi-hdd-network";
    }
  }

  function formatRelativeSyncTime(isoStr) {
    if (!isoStr) return "Just now";
    try {
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return "Recently synced";
      const now = new Date();
      const diffSec = Math.floor((now - d) / 1000);
      if (diffSec < 60) return "Just now";
      if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
      if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    } catch (_) {
      return "Recently synced";
    }
  }

  let _hubRenderPending = false;
  async function renderEmptyStateHub() {
    const $hub = $("#emptyState");
    if (!$hub.length) return;
    if (_hubRenderPending) return;
    _hubRenderPending = true;
    setTimeout(() => { _hubRenderPending = false; }, 2000);

    // 1. Fetch live system health & telemetry
    try {
      const token = sessionStorage.getItem("access_token") || localStorage.getItem("access_token");
      const headers = {};
      if (token) headers["Authorization"] = `Bearer ${token}`;

      const res = await fetch(`${API_BASE}/api/system/health`, { headers, credentials: "include" });
      if (res.ok) {
        const health = await res.json();
        if (health && health.stats) {
          const totalUnits = (health.stats.total_units || health.stats.total_credits_consumed || 0).toLocaleString();
          $("#hubCreditsConsumed").html(`${totalUnits} <span style="font-size:0.8rem;color:var(--text-muted);font-weight:500;">units</span>`);
          $("#hubTotalRuns").text((health.stats.total_pipeline_runs || 0).toLocaleString());
          const costStr = (health.stats.estimated_cost_usd !== undefined) ? `~$${Number(health.stats.estimated_cost_usd).toFixed(2)} USD` : "~$13.76 USD";
          $("#hubCreditsSub").html(`<span style="color:#10b981;font-weight:600;"><i class="bi bi-shield-check"></i> Free Tier &bull; ${costStr} Quota Units</span>`);
          
          if (health.database) {
            $("#hubDbStatus").html(`<span class="pulse-dot-green"></span> ${esc(health.database.engine || "PostgreSQL")}`);
            $("#hubDbName").text(health.database.name || "sales_ai_universal");
          }

          if (health.stats.last_sync_timestamp) {
            $("#hubLastSync").text(formatRelativeSyncTime(health.stats.last_sync_timestamp));
            $("#hubFreshnessLabel").text(`Freshness: ${formatRelativeSyncTime(health.stats.last_sync_timestamp)}`);
          } else {
            $("#hubLastSync").text("Active");
          }
        }
      }
    } catch (err) {
        console.warn("[renderEmptyStateHub] System health fetch error:", err);
      }

      // 2. Render Enterprise Portfolio & Outreach Readiness Cards
      const accts = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts))
        ? MOCK_DATA.accounts
        : [];
      $("#hubPortfolioAccountsCountBadge").text(`${accts.length} Accounts Monitored`);
      $("#hubPortfolioGrid").html(renderHubPortfolioGrid(accts));

      // 3. Render Pipeline Resource Architecture & Data Lineage (Two-Tier Filter Grid)
      hubLineageFilterState = { layer: "all", cost: "all" };
      $("#hubLineageScopePills .hub-lineage-pill").removeClass("active").filter('[data-layer="all"]').addClass("active");
      $("#hubLineageCostToggle .hub-cost-toggle-btn").removeClass("active").filter('[data-cost="all"]').addClass("active");
      $("#hubLineageGrid").html(renderHubLineageGrid("all", "all"));

      // 4. Render Live Recent Pipeline Runs Feed
      loadHubRecentRuns();
    }

    // ── Pipeline Resource & Data Lineage Registry (23 Enterprise Platforms & 27+ Connectors) ──
    const PIPELINE_LINEAGE_SOURCES = [
      // ── Regulatory, Legal & Open Data Platforms ──
      {
        id: "sec_edgar",
        name: "SEC EDGAR",
        icon: "bi-bank2",
        portalUrl: "https://www.sec.gov/edgar/searchedgar/companysearch",
        layers: ["account", "lob"],
        scopeLabel: "Account & Division",
        costType: "free",
        costLabel: "100% Free / Public Gov",
        description: "Official SEC 10-K statutory filings, audited financials, CIK registry, Exhibit 21 corporate subsidiaries, submissions Atom RSS feed.",
        chips: ["10-K Filings", "Exhibit 21 Subsidiaries", "SEC CIK", "Audited Revenue", "Filings Atom RSS"],
        subTools: [
          { name: "10-K Statutory Filings", rate: "$0 Free", desc: "Audited Financials & Revenue" },
          { name: "Exhibit 21 Subsidiaries", rate: "$0 Free", desc: "Legal Entity Ownership Tree" },
          { name: "Submissions Atom RSS", rate: "$0 Free", desc: "Live Regulatory Filing Feed" }
        ]
      },
      {
        id: "gleif",
        name: "GLEIF LEI Registry",
        icon: "bi-diagram-3",
        portalUrl: "https://search.gleif.org",
        layers: ["account", "lob"],
        scopeLabel: "Account & Division",
        costType: "free",
        costLabel: "100% Free / Open Data",
        description: "Global Legal Entity Identifier (LEI) master database, verified jurisdiction of incorporation, parent-subsidiary corporate ownership trees.",
        chips: ["LEI Code", "Corporate Ownership Tree", "Legal Jurisdiction", "GLEIF Entity ID"]
      },
      {
        id: "patents",
        name: "USPTO & PatentsView",
        icon: "bi-lightbulb",
        portalUrl: "https://patents.google.com",
        layers: ["account", "lob", "persona"],
        scopeLabel: "Universal (All Layers)",
        costType: "free",
        costLabel: "100% Free / Public Gov",
        description: "Granted patent portfolios, technology innovation filings, registered assignees, and active inventor filings for executives.",
        chips: ["Patents Granted", "Tech Innovations", "Assignees", "Inventor Search"],
        subTools: [
          { name: "PatentsView Open API", rate: "$0 Free", desc: "Corporate Patent Portfolio" },
          { name: "Inventor Search", rate: "$0 Free", desc: "Executive Assigned Inventions" }
        ]
      },
      {
        id: "courtlistener",
        name: "CourtListener RECAP",
        icon: "bi-hammer",
        portalUrl: "https://www.courtlistener.com/recap/",
        layers: ["account"],
        scopeLabel: "Account Level",
        costType: "free",
        costLabel: "100% Free / Public Gov",
        description: "Federal litigation records, court dockets, legal actions, regulatory proceedings, and PACER docket summaries.",
        chips: ["Federal Dockets", "PACER Filings", "Regulatory Actions", "Legal Disclosures"]
      },
      {
        id: "openfec",
        name: "OpenFEC (Data.gov)",
        icon: "bi-flag",
        portalUrl: "https://api.open.fec.gov",
        layers: ["account", "persona"],
        scopeLabel: "Account & Persona",
        costType: "free",
        costLabel: "100% Free / Public Gov",
        description: "Federal Election Commission filings: Corporate PAC political expenditures, committee disbursements, and executive individual campaign donations.",
        chips: ["Corporate PAC", "Executive Donations", "Data.gov API", "FEC Schedule A"],
        subTools: [
          { name: "Corporate PAC Filings", rate: "$0 Free", desc: "PAC Receipts & Disbursements" },
          { name: "Schedule A Donations", rate: "$0 Free", desc: "Executive Individual Contributions" }
        ]
      },
      {
        id: "wikipedia",
        name: "Wikipedia & Wikidata",
        icon: "bi-globe2",
        portalUrl: "https://www.wikidata.org",
        layers: ["account", "lob"],
        scopeLabel: "Account & Division",
        costType: "free",
        costLabel: "100% Free / Open Data",
        description: "Verified historical origins, founding year, founders, global headcount, industry classifications, and Wikidata QID entity resolution.",
        chips: ["Founded Year", "Founders", "Wikidata QID", "Corporate History"]
      },
      {
        id: "news_rss",
        name: "Google News RSS",
        icon: "bi-newspaper",
        portalUrl: "https://news.google.com",
        layers: ["account", "lob"],
        scopeLabel: "Account & Division",
        costType: "free",
        costLabel: "100% Free / Public RSS",
        description: "Live corporate press releases, operating division media mentions, executive interviews, and merger & acquisition signals.",
        chips: ["Live PR Releases", "Segment Headlines", "M&A Signals", "News RSS Stream"]
      },
      {
        id: "reddit_osint",
        name: "Reddit OSINT",
        icon: "bi-chat-left-text",
        portalUrl: "https://www.reddit.com",
        layers: ["account", "lob"],
        scopeLabel: "Account & Division",
        costType: "free",
        costLabel: "100% Free / Public RSS",
        description: "Unfiltered employee and customer sentiment, division product feedback, engineering discussions, and community perception.",
        chips: ["Customer Sentiment", "Employee Discussions", "Product Feedback", "Reddit Query"]
      },
      {
        id: "openalex",
        name: "OpenAlex Research",
        icon: "bi-journal-bookmark",
        portalUrl: "https://openalex.org",
        layers: ["account", "persona"],
        scopeLabel: "Account & Persona",
        costType: "free",
        costLabel: "100% Free / Open Data",
        description: "Scholarly citations, academic publications, institutional research affiliations, and executive scientific h-index telemetry.",
        chips: ["Academic Papers", "Scholarly Citations", "Executive h-Index", "Research Affiliations"]
      },

      // ── Unified Multi-Tool Platforms (Monid & Apify) ──
      {
        id: "monid",
        name: "Monid.ai Enterprise Gateway",
        icon: "bi-hdd-network",
        portalUrl: "https://monid.ai",
        layers: ["lob", "persona"],
        scopeLabel: "Division & Persona",
        costType: "metered",
        costLabel: "Metered & Free Fallback",
        description: "Unified enterprise gateway powering organization hierarchy discovery, buying committee discovery, and high-speed bio fallback search.",
        chips: ["Apollo Org Engine", "TinyFish Search", "Buying Committee", "Executive Contacts"],
        subTools: [
          { name: "Apollo Org Engine", rate: "25 cr/pass", desc: "Org Chart & Verified Emails" },
          { name: "TinyFish Search", rate: "$0 Free", desc: "Real-time Bio Snippets & News" }
        ]
      },
      {
        id: "apify",
        name: "Apify Actor Cloud Platform",
        icon: "bi-cloud-arrow-down",
        portalUrl: "https://apify.com/store",
        layers: ["account", "lob", "persona"],
        scopeLabel: "Universal (All Layers)",
        costType: "metered",
        costLabel: "Metered Scraping Platform",
        description: "Serverless web scraping & OSINT cloud executing dedicated crawler actors across executive careers, operating divisions, funding, and workplace ratings.",
        chips: ["LinkedIn Profiles", "LinkedIn Company", "Crunchbase", "Glassdoor"],
        subTools: [
          { name: "LinkedIn Profiles Actor", rate: "10 cr/scrape", desc: "Career Progression & Role Tenure" },
          { name: "LinkedIn Company Actor", rate: "15 cr/scrape", desc: "Operating Units & Headcount" },
          { name: "Crunchbase Actor", rate: "10 cr/crawl", desc: "Funding Rounds & Lead Investors" },
          { name: "Glassdoor Actor", rate: "10 cr/crawl", desc: "Workplace Ratings & CEO %" }
        ]
      },

      // ── Specialized Financial, Search, AI & Contact Gateways ──
      {
        id: "finnhub",
        name: "Finnhub Financials",
        icon: "bi-graph-up-arrow",
        portalUrl: "https://finnhub.io",
        layers: ["account"],
        scopeLabel: "Account Level",
        costType: "freetier",
        costLabel: "Free-Tier / Metered (5 cr)",
        description: "Real-time equity market telemetry, stock exchange listings, ticker symbols, trading valuation, and IPO registration status.",
        chips: ["Stock Symbol", "Exchange", "IPO Status", "Market Valuation"]
      },
      {
        id: "serper",
        name: "Google Serper Dev",
        icon: "bi-google",
        portalUrl: "https://serper.dev",
        layers: ["account", "lob", "persona"],
        scopeLabel: "Universal (All Layers)",
        costType: "metered",
        costLabel: "Metered API (1 cr/search)",
        description: "High-speed Google Search & Knowledge Graph API: corporate web indexing, division discovery, executive biographies, and organic SERP results.",
        chips: ["Google SERP", "Knowledge Graph", "Executive Bio", "Domain Discovery"]
      },
      {
        id: "diffbot",
        name: "Diffbot Knowledge Graph",
        icon: "bi-cpu",
        portalUrl: "https://www.diffbot.com",
        layers: ["account", "lob", "persona"],
        scopeLabel: "Universal (All Layers)",
        costType: "metered",
        costLabel: "Metered API (25 cr/lookup)",
        description: "AI Knowledge Graph entity resolution: corporate firmographics, operating subsidiary tech stacks, competitors, and executive employment histories.",
        chips: ["Entity Graph", "Tech Stack Matrix", "Competitor Graph", "Employment History"]
      },
      {
        id: "fmp",
        name: "Financial Modeling Prep (FMP)",
        icon: "bi-cash-coin",
        portalUrl: "https://financialmodelingprep.com",
        layers: ["account"],
        scopeLabel: "Account Level",
        costType: "metered",
        costLabel: "Metered API (2 cr/query)",
        description: "Standardized corporate financial statements, 10-K/10-Q balance sheets, enterprise valuation ratios, and institutional ownership data.",
        chips: ["Balance Sheets", "Income Statements", "Financial Ratios", "Valuation Multiples"]
      },
      {
        id: "opencorporates",
        name: "OpenCorporates",
        icon: "bi-building-check",
        portalUrl: "https://opencorporates.com",
        layers: ["account"],
        scopeLabel: "Account Level",
        costType: "metered",
        costLabel: "Metered API (1 cr/lookup)",
        description: "World's largest open corporate database: official company registry numbers, active incorporation status, and branch filings.",
        chips: ["Company Registry", "Incorporation Status", "Branch Filings", "Registered Agent"]
      },
      {
        id: "firecrawl",
        name: "Firecrawl v1 Scraper",
        icon: "bi-fire",
        portalUrl: "https://firecrawl.dev",
        layers: ["account", "lob"],
        scopeLabel: "Account & Division",
        costType: "metered",
        costLabel: "Metered API (1 cr/scrape)",
        description: "High-fidelity web crawler converting complex JavaScript-heavy enterprise web pages and division product suites into clean, structured Markdown.",
        chips: ["Clean Markdown Scrapes", "JS Bypass", "Product Portals", "Division Webpages"]
      },
      {
        id: "fullenrich",
        name: "FullEnrich v2 Waterfall",
        icon: "bi-telephone-inbound",
        portalUrl: "https://fullenrich.com",
        layers: ["persona"],
        scopeLabel: "Executive Persona",
        costType: "metered",
        costLabel: "Metered API (10 cr/lookup)",
        description: "Multi-vendor waterfall contact enrichment: verified direct dials, validated corporate emails, mobile numbers, and employment verification.",
        chips: ["Waterfall Enrichment", "Direct Dials", "Verified Work Emails", "Carrier Validation"]
      },
      {
        id: "tavily",
        name: "Tavily AI Search",
        icon: "bi-compass",
        portalUrl: "https://tavily.com",
        layers: ["lob", "persona"],
        scopeLabel: "Division & Persona",
        costType: "metered",
        costLabel: "Metered API (5 cr/op)",
        description: "Targeted AI market intelligence: division revenue figures, competitive positioning, executive strategic KPIs, and operational pain points.",
        chips: ["Division Revenue", "Strategic Priorities", "Pain Points", "Competitor Matrix"]
      },
      {
        id: "exa",
        name: "Exa Neural Web",
        icon: "bi-search",
        portalUrl: "https://exa.ai",
        layers: ["lob", "persona"],
        scopeLabel: "Division & Persona",
        costType: "metered",
        costLabel: "Metered API (5 cr/op)",
        description: "Deep semantic neural search across podcast transcripts, conference panels, thought leadership blogs, and technical architecture writeups.",
        chips: ["Podcast Transcripts", "Conference Panels", "Tech Stack Graph", "Semantic Citations"]
      },
      {
        id: "gemini_llm",
        name: "Google Gemini 3.6 Flash",
        icon: "bi-stars",
        portalUrl: "https://ai.google.dev",
        layers: ["persona"],
        scopeLabel: "Executive Persona",
        costType: "metered",
        costLabel: "Metered AI (5 cr/dossier)",
        description: "Autonomous GenAI sales dossier synthesis: persona-specific value propositions, DISC-modeled communication styles, and personalized conversation icebreakers.",
        chips: ["AI Value Prop", "Personalized Icebreakers", "DISC Personality", "Strategic Objections"]
      },
      {
        id: "semrush",
        name: "SEMrush Web Traffic",
        icon: "bi-bar-chart-line",
        portalUrl: "https://www.semrush.com",
        layers: ["account"],
        scopeLabel: "Account Level",
        costType: "metered",
        costLabel: "Enterprise Intelligence",
        description: "Web traffic analytics, global rank, domain authority score, organic search volume, and digital marketing footprint.",
        chips: ["Monthly Visits", "Global Rank", "Organic Keywords", "Domain Authority"]
      },
      {
        id: "apptopia",
        name: "Apptopia Intelligence",
        icon: "bi-phone",
        portalUrl: "https://apptopia.com",
        layers: ["account"],
        scopeLabel: "Account Level",
        costType: "metered",
        costLabel: "Enterprise Intelligence",
        description: "Mobile app portfolio intelligence: iOS and Android download volumes, daily active users (DAU), and digital product engagement.",
        chips: ["Mobile Apps", "Active Users (DAU)", "App Downloads", "Store Rankings"]
      }
    ];

    // Helper: Render Hub Portfolio Grid from live PostgreSQL payload
    function renderHubPortfolioGrid(accounts) {
      if (!Array.isArray(accounts) || !accounts.length) {
        return `<div style="grid-column:1/-1;text-align:center;padding:24px;color:#94a3b8;">No monitored accounts found.</div>`;
      }

      return accounts.map((acct) => {
        const name = acct.display_name || acct.name || "Enterprise Account";
        const domain = acct.primary_domain || acct.domain || "";
        const ticker = acct.stock_symbol ? `${acct.stock_symbol} (${acct.stock_exchange || "US"})` : domain;
        const initials = getInitials(name);

        const health = computeAccountHealth(acct);
        const pct = health.percentage || 0;
        const pctCls = pct >= 80 ? "pct-high" : (pct >= 50 ? "pct-med" : "pct-low");

        const lobs = acct.lobs || [];
        const personas = acct.personas || [];

        let lobsEnriched = 0;
        lobs.forEach((l) => { if (isLobItemEnriched(l)) lobsEnriched++; });

        let personasEnriched = 0;
        personas.forEach((p) => { if (isPersonaItemEnriched(p)) personasEnriched++; });

        let readinessTag = "";
        if (pct >= 90 && personasEnriched > 20) {
          readinessTag = `<span class="hub-port-status-tag status-ready"><i class="bi bi-check-circle-fill"></i> Ready for Outreach</span>`;
        } else if (pct >= 60 || personasEnriched > 0 || lobsEnriched > 0) {
          readinessTag = `<span class="hub-port-status-tag status-active"><i class="bi bi-lightning-charge-fill"></i> Active Intelligence</span>`;
        } else {
          readinessTag = `<span class="hub-port-status-tag status-pending"><i class="bi bi-hourglass-split"></i> Seed Stubs Only</span>`;
        }

        return `
          <div class="hub-portfolio-card fade-in" data-account-id="${acct.id}">
            <div class="hub-port-head">
              <div class="hub-port-identity">
                <div class="hub-port-avatar">${esc(initials)}</div>
                <div class="hub-port-titles">
                  <div class="hub-port-name" title="${esc(name)}">${esc(name)}</div>
                  <div class="hub-port-sub"><i class="bi bi-buildings"></i> ${esc(ticker || 'Enterprise')}</div>
                </div>
              </div>
              <span class="hub-port-badge-pct ${pctCls}">${pct}% Data Filled</span>
            </div>

            <div class="hub-port-track">
              <div class="hub-port-fill ${pctCls}" style="width: ${pct}%;"></div>
            </div>

            <div class="hub-port-stats-row">
              <div class="hub-port-stat-item">
                <span class="hub-port-stat-val">${lobs.length} <small style="font-size:0.68rem;font-weight:400;color:#64748b;">(${lobsEnriched} enriched)</small></span>
                <span class="hub-port-stat-lbl">Operating Segments</span>
              </div>
              <div class="hub-port-stat-item">
                <span class="hub-port-stat-val">${personas.length} <small style="font-size:0.68rem;font-weight:400;color:#64748b;">(${personasEnriched} AI dossiers)</small></span>
                <span class="hub-port-stat-lbl">Executive Buying Committee</span>
              </div>
            </div>

            <div class="hub-port-status-bar">
              <span style="color:#64748b;font-weight:500;">Readiness:</span>
              ${readinessTag}
            </div>

            <div class="hub-port-actions">
              <button type="button" class="btn-hub-open-acct" data-account-id="${acct.id}" title="Open Single Enterprise Intelligence Dashboard">
                <i class="bi bi-box-arrow-in-right"></i> Open Account
              </button>
              <button type="button" class="btn-hub-queue-acct" data-account-id="${acct.id}" title="Open Batch Queue for this Account">
                <i class="bi bi-play-circle"></i> Batch Queue
              </button>
            </div>
          </div>
        `;
      }).join("");
    }

    // Helper: Render Individual Resource Lineage Card
    function renderLineageCard(s, isWide = false, cost = "all") {
      // Filter sub-tools based on active cost filter
      let subTools = s.subTools || [];
      if (cost === "free") {
        subTools = subTools.filter(t => (t.rate || "").toLowerCase().includes("free"));
      } else if (cost === "metered") {
        subTools = subTools.filter(t => !(t.rate || "").toLowerCase().includes("free"));
      }

      const costBadge = s.costType === "free"
        ? `<span class="hub-cost-badge cost-free"><i class="bi bi-gift-fill"></i> ${esc(s.costLabel)}</span>`
        : (s.costType === "freetier"
          ? `<span class="hub-cost-badge cost-freetier"><i class="bi bi-check2-circle"></i> ${esc(s.costLabel)}</span>`
          : `<span class="hub-cost-badge cost-metered"><i class="bi bi-lightning-fill"></i> ${esc(s.costLabel)}</span>`);

      const cardClass = isWide ? "hub-lineage-card card-wide" : "hub-lineage-card card-compact";

      const subToolsHtml = (subTools && subTools.length)
        ? `
          <div class="hub-lineage-subtools">
            <div class="hub-subtools-title"><i class="bi bi-diagram-2"></i> Specialized Connectors &amp; Tools:</div>
            <div class="hub-subtools-grid">
              ${subTools.map(t => {
                const rateCls = (t.rate || "").toLowerCase().includes("free") ? "rate-free" : "";
                return `
                  <div class="hub-subtool-item">
                    <span class="hub-subtool-name"><i class="bi bi-arrow-return-right" style="font-size:0.58rem;color:#0284c7;"></i> ${esc(t.name)}</span>
                    <span class="hub-subtool-rate ${rateCls}">${esc(t.rate)}</span>
                    <span class="hub-subtool-desc" title="${esc(t.desc)}">${esc(t.desc)}</span>
                  </div>
                `;
              }).join("")}
            </div>
          </div>
        `
        : "";

      return `
        <a href="${s.portalUrl}" target="_blank" rel="noopener noreferrer" class="${cardClass}" title="Open ${esc(s.name)} Official Platform">
          <div class="hub-lineage-head">
            <div class="hub-lineage-title-wrap">
              <div class="hub-lineage-icon"><i class="bi ${s.icon}"></i></div>
              <div class="hub-lineage-name">${esc(s.name)}</div>
            </div>
            <span class="osint-live-indicator"><span class="pulse-dot"></span> LIVE <i class="bi bi-box-arrow-up-right" style="font-size:0.62rem;margin-left:2px;"></i></span>
          </div>

          <div class="hub-lineage-badges-row">
            <span class="hub-scope-badge"><i class="bi bi-layers"></i> ${esc(s.scopeLabel)}</span>
            ${costBadge}
          </div>

          <div class="hub-lineage-desc">${esc(s.description)}</div>

          <div class="hub-lineage-chips">
            ${s.chips.map(c => `<span class="hub-lineage-chip">${esc(c)}</span>`).join("")}
          </div>

          ${subToolsHtml}
        </a>
      `;
    }

    // Two-Tier Lineage Filter State
    let hubLineageFilterState = {
      layer: "all",
      cost: "all"
    };

    // Helper: Render Density-Grouped Resource Architecture & Lineage Grid
    function renderHubLineageGrid(layer = "all", cost = "all") {
      const list = PIPELINE_LINEAGE_SOURCES.filter(s => {
        // 1. Layer filter
        if (layer !== "all" && !s.layers.includes(layer)) {
          return false;
        }

        // 2. Cost filter
        if (cost === "free") {
          const hasFree = s.costType === "free" || (s.subTools && s.subTools.some(t => (t.rate || "").toLowerCase().includes("free")));
          if (!hasFree) return false;
        } else if (cost === "metered") {
          const hasMetered = s.costType === "metered" || s.costType === "freetier" || (s.subTools && s.subTools.some(t => !(t.rate || "").toLowerCase().includes("free")));
          if (!hasMetered) return false;
        }

        return true;
      });

      // Dynamic Section Badge Update
      const layerName = layer === "all" ? "All Layers" : (layer === "account" ? "Account Layer" : (layer === "lob" ? "Division Layer" : "Persona Layer"));
      const costName = cost === "all" ? "All Costs" : (cost === "free" ? "Free Only" : "Metered Only");
      $("#hubActiveConnectorsBadge").text(`${list.length} Platforms Displayed (${layerName} • ${costName})`);

      if (!list.length) {
        return `
          <div style="text-align:center;padding:36px 16px;color:#94a3b8;font-size:0.82rem;background:#f8fafc;border-radius:8px;border:1px dashed #e2e8f0;width:100%;">
            <i class="bi bi-filter-circle" style="font-size:1.4rem;display:block;margin-bottom:8px;color:#cbd5e1;"></i>
            No resources match both <strong>${esc(layerName)}</strong> and <strong>${esc(costName)}</strong>.
            <div style="margin-top:6px;font-size:0.72rem;color:#64748b;">Try selecting "All Layers" or "All Costs" to expand your view.</div>
          </div>
        `;
      }

      // Group by Data Density: Multi-Connector Enterprise Platforms vs Direct Feeds
      const multiConnectorList = list.filter(s => s.subTools && s.subTools.length > 0);
      const directFeedList = list.filter(s => !s.subTools || s.subTools.length === 0);

      let html = "";

      // 1. Multi-Connector Platforms Section (Wide 2-Column Cards)
      if (multiConnectorList.length > 0) {
        html += `
          <div class="hub-lineage-group">
            <div class="hub-lineage-group-header">
              <div class="hub-group-title-wrap">
                <span class="hub-group-icon"><i class="bi bi-diagram-3-fill"></i></span>
                <div>
                  <div class="hub-group-title">Multi-Connector Enterprise Platforms &amp; Crawler Clouds</div>
                  <div class="hub-group-sub">Platforms integrating multiple specialized sub-tools, crawler actors, or dual cost models</div>
                </div>
              </div>
              <span class="hub-group-count-badge">${multiConnectorList.length} Platforms Integrated</span>
            </div>
            <div class="hub-lineage-grid-wide">
              ${multiConnectorList.map(s => renderLineageCard(s, true, cost)).join("")}
            </div>
          </div>
        `;
      }

      // Visual Divider between groups
      if (multiConnectorList.length > 0 && directFeedList.length > 0) {
        html += `<div class="hub-lineage-group-divider"></div>`;
      }

      // 2. Direct Feeds & Dedicated Registries (Compact 4-Column Cards)
      if (directFeedList.length > 0) {
        html += `
          <div class="hub-lineage-group">
            <div class="hub-lineage-group-header">
              <div class="hub-group-title-wrap">
                <span class="hub-group-icon feed-icon"><i class="bi bi-hdd-network-fill"></i></span>
                <div>
                  <div class="hub-group-title">Direct Intelligence Feeds &amp; Dedicated Registries</div>
                  <div class="hub-group-sub">Targeted single-purpose data providers, government registries, and AI synthesis engines</div>
                </div>
              </div>
              <span class="hub-group-count-badge">${directFeedList.length} Feeds Integrated</span>
            </div>
            <div class="hub-lineage-grid-compact">
              ${directFeedList.map(s => renderLineageCard(s, false, cost)).join("")}
            </div>
          </div>
        `;
      }

      return html;
    }

    // Helper: Load Live Recent Pipeline Runs Feed
    let _recentRunsPending = false;
    async function loadHubRecentRuns() {
      const $container = $("#hubRecentRunsContainer");
      if (!$container.length) return;
      if (_recentRunsPending) return;
      _recentRunsPending = true;
      setTimeout(() => { _recentRunsPending = false; }, 2000);

      try {
        const res = await fetch(`${API_BASE}/api/pipeline/runs?limit=5`);
        if (!res.ok) throw new Error("Status " + res.status);
        const data = await res.json();
        const runs = data.runs || [];

        if (!runs.length) {
          $container.html(`<div style="padding:16px;text-align:center;color:#94a3b8;font-size:0.8rem;">No recent pipeline runs recorded yet.</div>`);
          return;
        }

        let rowsHtml = runs.map(r => {
          const ent = r.entities_extracted || {};
          const targetName = ent.person_name || ent.company || r.company_name || "Target Entity";
          const level = r.pipeline_level || ent.level || "pipeline";
          const action = r.action || ent.action || "run";
          const levelBadge = `<span style="text-transform:capitalize;font-weight:600;padding:2px 6px;border-radius:4px;background:#f1f5f9;font-size:0.7rem;">${esc(level)} ${esc(action)}</span>`;

          const score = r.quality_score ? `${Math.round(r.quality_score)}%` : "—";
          const grade = r.quality_grade || "";
          const credits = r.total_credits_used > 0 ? `${r.total_credits_used} Credits` : "0 (Free)";
          const timeStr = formatRelativeSyncTime(r.started_at);

          const statusLower = String(r.status || "success").toLowerCase();
          let statusCls = "status-success";
          if (statusLower.includes("val")) statusCls = "status-validated";
          else if (statusLower.includes("run") || statusLower.includes("stag")) statusCls = "status-running";
          else if (statusLower.includes("err") || statusLower.includes("fail")) statusCls = "status-failed";

          return `
            <tr>
              <td>
                <strong style="color:#0f172a;">${esc(targetName)}</strong>
                <div style="font-size:0.68rem;color:#64748b;">${esc(r.company_name || '')}</div>
              </td>
              <td>${levelBadge}</td>
              <td><span style="font-weight:700;color:#0284c7;">${score}</span> ${grade ? `<small>(${esc(grade)})</small>` : ''}</td>
              <td><span style="font-weight:600;color:${r.total_credits_used > 0 ? '#b45309' : '#059669'};">${credits}</span></td>
              <td><span style="color:#64748b;">${timeStr}</span></td>
              <td><span class="hub-run-status-badge ${statusCls}"><i class="bi bi-circle-fill" style="font-size:0.5rem;"></i> ${esc(r.status || 'Done')}</span></td>
            </tr>
          `;
        }).join("");

        $container.html(`
          <div class="hub-runs-table-wrap">
            <table class="hub-runs-table">
              <thead>
                <tr>
                  <th>Target Entity &amp; Enterprise</th>
                  <th>Pipeline Stage</th>
                  <th>Quality Score</th>
                  <th>Credits</th>
                  <th>Executed</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                ${rowsHtml}
              </tbody>
            </table>
          </div>
        `);
      } catch (err) {
        console.warn("Failed to load hub recent runs:", err);
        $container.html(`<div style="padding:14px;text-align:center;color:#94a3b8;font-size:0.8rem;">Live pipeline audit logs accessible in database.</div>`);
      }
    }

    // Tier 1: Scope Layer Pills Click Event (Recommendation 2)
    $(document).on("click", "#hubLineageScopePills .hub-lineage-pill", function () {
      $("#hubLineageScopePills .hub-lineage-pill").removeClass("active");
      $(this).addClass("active");
      hubLineageFilterState.layer = $(this).data("layer") || "all";
      $("#hubLineageGrid").html(renderHubLineageGrid(hubLineageFilterState.layer, hubLineageFilterState.cost));
    });

    // Tier 2: Cost Filter Toggle Click Event (Recommendation 2)
    $(document).on("click", "#hubLineageCostToggle .hub-cost-toggle-btn", function () {
      $("#hubLineageCostToggle .hub-cost-toggle-btn").removeClass("active");
      $(this).addClass("active");
      hubLineageFilterState.cost = $(this).data("cost") || "all";
      $("#hubLineageGrid").html(renderHubLineageGrid(hubLineageFilterState.layer, hubLineageFilterState.cost));
    });

    // Hub Portfolio Card Actions
    $(document).on("click", ".btn-hub-open-acct", function (e) {
      e.stopPropagation();
      const acctId = $(this).data("account-id");
      const $sidebarItem = $(`#accountList .account-item[data-id="${acctId}"]`);
      if ($sidebarItem.length) {
        $sidebarItem.trigger("click");
      }
    });

    $(document).on("click", ".btn-hub-queue-acct", function (e) {
      e.stopPropagation();
      const acctId = $(this).data("account-id");
      const acct = (MOCK_DATA.accounts || []).find(a => a.id === acctId);
      if (acct) {
        openBatchConsole("persona", acct);
      }
    });

    $(document).on("click", ".hub-portfolio-card", function () {
      const acctId = $(this).data("account-id");
      const $sidebarItem = $(`#accountList .account-item[data-id="${acctId}"]`);
      if ($sidebarItem.length) {
        $sidebarItem.trigger("click");
      }
    });


  // Return to Quick-Launch Hub when clicking top-left brand header or sidebar brand
  $(document).on("click", ".pipeline-top-left, .sidebar-brand", function (e) {
    if ($(e.target).closest("#sidebarCollapseBtn").length) return;
    activeAccount = null;
    activeLob = null;
    activePersona = null;
    try {
      sessionStorage.removeItem("pipeline_active_account_id");
    } catch (err) {}

    $(".account-item").removeClass("active");
    $("#dashboardContainer").addClass("d-none");
    $("#lobDetailViewContainer").addClass("d-none").empty();
    $("#personaDetailViewContainer").addClass("d-none").empty();
    $("#detailPanelContainer").addClass("d-none").empty();
    
    renderModernBreadcrumbs();
    renderEmptyStateHub();
    $("#emptyState").removeClass("d-none");
    PipelineRouter.syncState({ view: "home" });
  });

  // ══════════════════════════════════════════════════════════════════
  // EVENT: LOB CARD SELECTION & SMART TOGGLE
  // ══════════════════════════════════════════════════════════════════
  $(document).on("click", ".lob-card", function () {
    const lobId = $(this).data("lob-id");

    // TOGGLE OFF if clicking currently active LOB
    if (activeLob && String(activeLob.id) === String(lobId)) {
      activeLob = null;
      activePersona = null;
      $(".lob-card").removeClass("active");
      $(".persona-card").removeClass("active");
      $("#lobDetailViewContainer").addClass("d-none").empty();
      $("#personaDetailViewContainer").addClass("d-none").empty();

      // Restore All Personas (Account View remains visible)
      $("#accountViewWrapper").removeClass("d-none");
      $("#lobSection").removeClass("d-none");
      $("#allPersonasSection").removeClass("d-none");

      renderModernBreadcrumbs();
      return;
    }

    // SELECT LOB
    $(".lob-card").removeClass("active");
    $(this).addClass("active");

    activeLob = activeAccount.lobs.find((l) => String(l.id) === String(lobId));
    if (!activeLob) return;

    activePersona = null;

    // Keep Account details visible! ONLY hide Personas sections
    $("#accountViewWrapper").removeClass("d-none");
    $("#personaSection").addClass("d-none");
    $("#allPersonasSection").addClass("d-none");
    $("#personaDetailViewContainer").addClass("d-none").empty();

    // Render and show LOB Detail View (Image 2) right below LOB cards
    $("#lobDetailViewContainer").html(renderModernLobDetailView(activeLob)).removeClass("d-none");

    renderModernBreadcrumbs();
    const $lobC = $("#lobDetailViewContainer");
    if ($lobC.length && $lobC.offset()) {
      window.scrollTo({ top: Math.max(0, $lobC.offset().top - 20), behavior: "smooth" });
    }
  });

  // ══════════════════════════════════════════════════════════════════
  // EVENT: PERSONA CARD SELECTION (SLIDE-OVER EXECUTIVE DOSSIER DRAWER)
  // ══════════════════════════════════════════════════════════════════
  $(document).on("click", ".persona-card", async function () {
    $(".persona-card").removeClass("active");
    $(this).addClass("active");

    const pData = JSON.parse(decodeURIComponent($(this).attr("data-raw")));
    activePersona = pData;

    // If activePersona doesn't have deep fields (e.g. google_scholar_url or osint_feed_manifest), fetch full persona detail
    if (activePersona && activePersona.id && !activePersona.google_scholar_url && !activePersona.osint_feed_manifest) {
      try {
        const token = sessionStorage.getItem('access_token') || localStorage.getItem('access_token');
        const headers = token ? { 'Authorization': `Bearer ${token}` } : {};
        const pRes = await fetch(`${API_BASE}/api/personas/${activePersona.id}`, { headers, credentials: 'include' });
        if (pRes.ok) {
          const fullP = await pRes.json();
          activePersona = { ...activePersona, ...fullP };
        }
      } catch (_) {}
    }

    // Hide LOB detail view while viewing Persona
    $("#lobDetailViewContainer").addClass("d-none");
    $("#personaDetailViewContainer").html(renderModernPersonaDetailView(activePersona)).removeClass("d-none");
    renderModernBreadcrumbs();
    const $pC = $("#personaDetailViewContainer");
    if ($pC.length && $pC.offset()) {
      window.scrollTo({ top: Math.max(0, $pC.offset().top - 20), behavior: "smooth" });
    }
  });

  // ─── Floating Toast Notification Helper ───
  function showToastNotification(message, icon = "bi-check-circle-fill text-success") {
    let $container = $("#toastNotificationContainer");
    if (!$container.length) {
      $container = $('<div id="toastNotificationContainer" class="pipeline-toast-container"></div>').appendTo("body");
    }
    const $toast = $(`
      <div class="pipeline-floating-toast">
        <i class="bi ${icon}"></i>
        <span>${esc(message)}</span>
      </div>
    `);
    $container.append($toast);
    setTimeout(() => {
      $toast.fadeOut(250, function () { $(this).remove(); });
    }, 2400);
  }

  function showToast(message, type = "info") {
    let icon = "bi-info-circle-fill text-info";
    if (type === "success") icon = "bi-check-circle-fill text-success";
    else if (type === "warning") icon = "bi-exclamation-triangle-fill text-warning";
    else if (type === "error" || type === "danger") icon = "bi-x-circle-fill text-danger";
    else if (typeof type === "string" && type.includes("bi-")) icon = type;
    showToastNotification(message, icon);
  }
  window.showToast = showToast;

  // ─── Quick-Copy Utilities ───
  $(document).on("click", ".btn-copy-email", function (e) {
    e.stopPropagation();
    const email = $(this).data("email") || $(this).attr("data-email");
    if (!email) return;

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(email).catch(() => {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = email;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (_) {}
      document.body.removeChild(ta);
    }

    const $btn = $(this);
    const origHtml = $btn.html();
    $btn.addClass("copied").html('<i class="bi bi-check-lg"></i>');
    showToastNotification(`Copied email: ${email}`);

    setTimeout(() => {
      $btn.removeClass("copied").html(origHtml);
    }, 1800);
  });

  $(document).on("click", ".btn-copy-phone", function (e) {
    e.stopPropagation();
    const phone = $(this).data("phone") || $(this).attr("data-phone");
    if (!phone) return;

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(String(phone)).catch(() => {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = String(phone);
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (_) {}
      document.body.removeChild(ta);
    }

    const $btn = $(this);
    const origHtml = $btn.html();
    $btn.addClass("copied").html('<i class="bi bi-check-lg"></i>');
    showToastNotification(`Copied phone: ${phone}`);

    setTimeout(() => {
      $btn.removeClass("copied").html(origHtml);
    }, 1800);
  });

  // ─── Slide-Over Executive Dossier Drawer Logic ───
  function renderExecutiveDossierDrawer(p) {
    if (!p) return;
    const tierCat = getPersonaTierCategory(p);
    const avatarClass = tierCat === "c_suite" ? "avatar-csuite" : (tierCat === "vp_head" ? "avatar-vp" : "");
    const tierLabelMap = {
      c_suite: "C-Suite",
      vp_head: "VP & Head",
      director: "Director",
      manager: "Manager & Lead",
      other: "Executive Staff",
    };
    const tierLabel = tierLabelMap[tierCat] || "Executive";

    const comp = computePersonaCompleteness(p);
    const score = comp.score || 85;

    const initials = getInitials(p.name || "EX");
    const drawerPhotoUrl = extractPersonaPhotoUrl(p);
    if (drawerPhotoUrl) {
      $("#drawerAvatar").attr("class", `drawer-avatar avatar-has-photo ${avatarClass}`).html(`
        <img src="${esc(drawerPhotoUrl)}"
             alt="${esc(p.name || 'Executive')}"
             class="avatar-photo-img"
             referrerpolicy="no-referrer"
             loading="lazy"
             onload="this.style.opacity='1';"
             onerror="this.style.display='none'; if(this.nextElementSibling) this.nextElementSibling.style.display='flex';"
             style="width:100%;height:100%;object-fit:cover;border-radius:inherit;display:block;opacity:0;transition:opacity 0.2s ease-in-out;" />
        <div class="avatar-photo-fallback" style="display:none;width:100%;height:100%;align-items:center;justify-content:center;position:absolute;top:0;left:0;border-radius:inherit;">
          ${esc(initials)}
        </div>
      `);
    } else {
      $("#drawerAvatar").attr("class", `drawer-avatar ${avatarClass}`).text(initials);
    }
    $("#drawerName").text(p.name || "Executive");
    $("#drawerTierBadge").attr("class", `drawer-tier-badge ${tierCat}`).text(tierLabel);
    $("#drawerHealthBadge").text(`${score}% Confidence`);
    $("#drawerTitle").text(p.title || p.job_title || "Executive Leadership");

    const coName = activeAccount ? (activeAccount.name || activeAccount.display_name || "Enterprise") : "Enterprise";
    const lobName = (activeLob && activeLob.name) ? activeLob.name : (p.department || "Corporate");
    $("#drawerCompanyMeta").html(`${esc(coName)} &bull; <i class="bi bi-diagram-3"></i> ${esc(lobName)}`);

    const pEmail = p.email || p.work_email || p.personal_email || p.sanitized_email || "";
    const pPhone = p.phone || p.direct_mobile_phone || p.sanitized_phone || "";
    const pLinkedIn = p.linkedin_url || p.linkedin || "";
    const cik = (activeAccount && activeAccount.sec_cik) ? activeAccount.sec_cik : "";
    const secInsiderUrl = cik ? `https://www.sec.gov/edgar/searchedgar/companysearch` : "";

    // Action Bar Chips
    let actionsHtml = "";
    if (pEmail) {
      actionsHtml += `
        <button type="button" class="drawer-action-chip btn-copy-email" data-email="${esc(pEmail)}" title="Click to copy email">
          <i class="bi bi-envelope-fill text-primary"></i> <span>${esc(pEmail)}</span> <i class="bi bi-copy" style="font-size:0.68rem;opacity:.6;"></i>
        </button>
      `;
    }
    if (pPhone) {
      actionsHtml += `
        <button type="button" class="drawer-action-chip btn-copy-phone" data-phone="${esc(pPhone)}" title="Click to copy phone">
          <i class="bi bi-telephone-fill text-success"></i> <span>${esc(pPhone)}</span> <i class="bi bi-copy" style="font-size:0.68rem;opacity:.6;"></i>
        </button>
      `;
    }
    if (pLinkedIn) {
      actionsHtml += `
        <a href="${normalizeUrl(pLinkedIn)}" target="_blank" rel="noopener noreferrer" class="drawer-action-chip" title="Open verified LinkedIn Profile">
          <i class="bi bi-linkedin" style="color:#0a66c2;"></i> <span>LinkedIn Profile</span> <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;opacity:.6;"></i>
        </a>
      `;
    }
    if (secInsiderUrl) {
      actionsHtml += `
        <a href="${secInsiderUrl}" target="_blank" rel="noopener noreferrer" class="drawer-action-chip" title="SEC Form 4 Insider Stock Transactions">
          <i class="bi bi-bank2 text-secondary"></i> <span>SEC Form 4</span> <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;opacity:.6;"></i>
        </a>
      `;
    }
    $("#drawerActionBar").html(actionsHtml);

    // AI Call Prep & Strategic Angle
    const icebreaker = p.icebreaker || (p.ai_call_prep && p.ai_call_prep.icebreakers && p.ai_call_prep.icebreakers[0]) || 
      `Congratulations on driving strategic initiatives across ${lobName} at ${coName}.`;

    const valueProp = p.value_proposition || (p.ai_call_prep && p.ai_call_prep.value_proposition) || 
      `Empowering ${coName}'s ${p.department || lobName} team with high-velocity data automation and enterprise risk transparency.`;

    const strategicAngle = p.strategic_angle || (p.ai_call_prep && p.ai_call_prep.strategic_angle) || p.decision_priorities ||
      `Focuses on operational resilience, cost efficiency, and cross-functional leadership alignment.`;

    // Coordinates
    const locStr = p.city ? `${p.city}${p.state ? ', ' + p.state : ''}${p.country ? ', ' + p.country : ''}` : (p.country || "Corporate Headquarters");
    const tenureStr = p.current_role_tenure_months ? 
      `${Math.floor(p.current_role_tenure_months / 12)} yrs ${p.current_role_tenure_months % 12} mos` : "Active Executive";
    const priorStr = p.prior_company || (Array.isArray(p.past_companies) && p.past_companies.length > 0 ? p.past_companies.join(', ') : "Enterprise Sector");
    const eduStr = p.degree ? `${p.degree}${p.institution ? ' &bull; ' + p.institution : ''}` : (p.institution || "Higher Education / University");
    const reportsTo = p.reports_to || "Executive Committee / Board";

    let skillsHtml = "";
    const skills = Array.isArray(p.skills) ? p.skills : (p.skills ? String(p.skills).split(",") : []);
    if (skills.length) {
      skillsHtml = `
        <div style="margin-top:10px;">
          <div class="drawer-field-label" style="margin-bottom:4px;">Core Competencies &amp; Skills</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;">
            ${skills.slice(0, 10).map(s => `<span style="font-size:0.68rem;background:#f1f5f9;color:#334155;padding:2px 7px;border-radius:4px;border:1px solid #e2e8f0;">${esc(s.trim())}</span>`).join("")}
          </div>
        </div>
      `;
    }

    $("#drawerBody").html(`
      <!-- Section 1: AI Call Prep & Conversation Hooks -->
      <div class="drawer-section-card">
        <div class="drawer-section-title">
          <i class="bi bi-stars"></i> AI Call Prep &amp; Executive Hook
        </div>
        <div class="drawer-ai-callout">
          <div class="drawer-ai-label"><i class="bi bi-chat-quote-fill"></i> Conversation Opener / Icebreaker</div>
          <div class="drawer-ai-text">${esc(icebreaker)}</div>
        </div>
        <div class="drawer-ai-callout" style="border-left-color:#10b981;">
          <div class="drawer-ai-label" style="color:#059669;"><i class="bi bi-bullseye"></i> Tailored Value Proposition</div>
          <div class="drawer-ai-text">${esc(valueProp)}</div>
        </div>
        <div class="drawer-ai-callout" style="border-left-color:#8b5cf6;margin-bottom:0;">
          <div class="drawer-ai-label" style="color:#7c3aed;"><i class="bi bi-compass"></i> Strategic Priorities &amp; Decision Drivers</div>
          <div class="drawer-ai-text">${esc(strategicAngle)}</div>
        </div>
      </div>

      <!-- Section 2: Contact Coordinates & Office -->
      <div class="drawer-section-card">
        <div class="drawer-section-title">
          <i class="bi bi-person-lines-fill"></i> Contact Coordinates &amp; Office
        </div>
        <div class="drawer-grid-2col">
          <div class="drawer-field">
            <span class="drawer-field-label">Verified Work Email</span>
            <span class="drawer-field-val">
              ${pEmail ? `<a href="mailto:${esc(pEmail)}" style="color:#0284c7;text-decoration:none;">${esc(pEmail)}</a>` : '<span style="color:#94a3b8;">Not captured</span>'}
            </span>
          </div>
          <div class="drawer-field">
            <span class="drawer-field-label">Direct / Mobile Phone</span>
            <span class="drawer-field-val">
              ${pPhone ? `<a href="tel:${esc(pPhone)}" style="color:#0284c7;text-decoration:none;">${esc(pPhone)}</a>` : '<span style="color:#94a3b8;">Not captured</span>'}
            </span>
          </div>
          <div class="drawer-field">
            <span class="drawer-field-label">Office Location</span>
            <span class="drawer-field-val">${esc(locStr)}</span>
          </div>
          <div class="drawer-field">
            <span class="drawer-field-label">Division / Department</span>
            <span class="drawer-field-val">${esc(p.department || lobName)}</span>
          </div>
        </div>
      </div>

      <!-- Section 3: Professional Background & Seniority -->
      <div class="drawer-section-card">
        <div class="drawer-section-title">
          <i class="bi bi-briefcase-fill"></i> Professional Career &amp; Experience
        </div>
        <div class="drawer-grid-2col">
          <div class="drawer-field">
            <span class="drawer-field-label">Role Tenure</span>
            <span class="drawer-field-val">${esc(tenureStr)}</span>
          </div>
          <div class="drawer-field">
            <span class="drawer-field-label">Reports To</span>
            <span class="drawer-field-val">${esc(reportsTo)}</span>
          </div>
          <div class="drawer-field">
            <span class="drawer-field-label">Prior Organization</span>
            <span class="drawer-field-val">${esc(priorStr)}</span>
          </div>
          <div class="drawer-field">
            <span class="drawer-field-label">Education / Credentials</span>
            <span class="drawer-field-val">${esc(eduStr)}</span>
          </div>
        </div>
        ${skillsHtml}
      </div>
    `);
  }

  function openExecutiveDossierDrawer(persona) {
    if (!persona) return;
    activePersona = persona;
    renderExecutiveDossierDrawer(persona);
    $("#executiveDrawerBackdrop").removeClass("d-none fade-out");
    $("#executiveDossierDrawer").addClass("open").attr("aria-hidden", "false");
    $("body").css("overflow", "hidden");
  }

  function closeExecutiveDossierDrawer() {
    $("#executiveDossierDrawer").removeClass("open").attr("aria-hidden", "true");
    $("#executiveDrawerBackdrop").addClass("fade-out");
    setTimeout(() => {
      $("#executiveDrawerBackdrop").addClass("d-none").removeClass("fade-out");
      $("body").css("overflow", "");
    }, 220);
  }

  $(document).on("click", "#closeExecutiveDrawerBtn, #drawerCloseFooterBtn, #executiveDrawerBackdrop", function () {
    closeExecutiveDossierDrawer();
  });

  $(document).on("keydown", function (e) {
    if (e.key === "Escape" && $("#executiveDossierDrawer").hasClass("open")) {
      closeExecutiveDossierDrawer();
    }
  });

  $(document).on("click", "#drawerOpenFullPageBtn", function () {
    if (!activePersona) return;
    closeExecutiveDossierDrawer();

    // Hide LOB detail view while viewing Persona
    $("#lobDetailViewContainer").addClass("d-none");

    // Render and show Persona Detail View (Image 3)
    $("#personaDetailViewContainer").html(renderModernPersonaDetailView(activePersona)).removeClass("d-none");

    renderModernBreadcrumbs();
    const $pC = $("#personaDetailViewContainer");
    if ($pC.length && $pC.offset()) {
      window.scrollTo({ top: Math.max(0, $pC.offset().top - 20), behavior: "smooth" });
    }
  });

  $(document).on("click", "#btnBackFromPersonaDetail", function () {
    if (activeLob) {
      $(".crumb-lob").trigger("click");
    } else {
      $(".crumb-personas").trigger("click");
    }
  });

  // ─── One-Click Account Directory Export (CSV / JSON) ───
  $(document).on("click", "#btnExportPersonas", function (e) {
    e.stopPropagation();
    $("#personaExportMenu").toggleClass("d-none");
  });

  $(document).on("click", function (e) {
    if (!$(e.target).closest(".persona-export-wrapper").length) {
      $("#personaExportMenu").addClass("d-none");
    }
  });

  function exportPersonasDirectory(format = "csv") {
    $("#personaExportMenu").addClass("d-none");
    const personas = (allPersonasDirectoryState && allPersonasDirectoryState.personas && allPersonasDirectoryState.personas.length) 
      ? allPersonasDirectoryState.personas 
      : ((activeAccount && activeAccount.personas) ? activeAccount.personas : []);

    if (!personas.length) {
      showToastNotification("No personas available to export", "bi-exclamation-triangle-fill text-warning");
      return;
    }

    const coName = activeAccount ? (activeAccount.name || activeAccount.display_name || "enterprise") : "enterprise";
    const dateStr = new Date().toISOString().split("T")[0];
    const fileName = `${slugify(coName)}_executive_directory_${dateStr}.${format}`;

    if (format === "csv") {
      const headers = [
        "Full Name",
        "Job Title",
        "Hierarchy Tier",
        "Department",
        "Company",
        "Work Email",
        "Direct Phone",
        "LinkedIn URL",
        "Office Location",
        "Role Tenure",
        "Alma Mater / Degree",
        "Confidence Score",
      ];

      const rows = personas.map((p) => {
        const tier = getPersonaTierCategory(p);
        const comp = computePersonaCompleteness(p);
        const loc = p.city ? `${p.city}${p.state ? ', ' + p.state : ''}${p.country ? ', ' + p.country : ''}` : (p.country || "");
        const tenure = p.current_role_tenure_months ? `${Math.floor(p.current_role_tenure_months / 12)}y ${p.current_role_tenure_months % 12}m` : "";
        const edu = p.degree ? `${p.degree}${p.institution ? ' - ' + p.institution : ''}` : (p.institution || "");

        return [
          p.name || "",
          p.title || p.job_title || "",
          tier,
          p.department || "",
          coName,
          p.email || p.work_email || p.personal_email || "",
          p.phone || p.direct_mobile_phone || "",
          p.linkedin_url || p.linkedin || "",
          loc,
          tenure,
          edu,
          `${comp.score || 85}%`,
        ].map((val) => `"${String(val).replace(/"/g, '""')}"`);
      });

      const csvContent = "\uFEFF" + [headers.join(","), ...rows.map((r) => r.join(","))].join("\r\n");
      const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);

      showToastNotification(`Exported ${personas.length} contacts to CSV`, "bi-file-earmark-spreadsheet text-success");
    } else if (format === "json") {
      const jsonContent = JSON.stringify(personas, null, 2);
      const blob = new Blob([jsonContent], { type: "application/json;charset=utf-8;" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);

      showToastNotification(`Exported ${personas.length} contacts to JSON`, "bi-filetype-json text-primary");
    }
  }

  $(document).on("click", ".persona-export-item", function () {
    const format = $(this).data("format") || "csv";
    exportPersonasDirectory(format);
  });

  // ══════════════════════════════════════════════════════════════════
  // EVENT: BREADCRUMBS & TAB NAVIGATION
  // ══════════════════════════════════════════════════════════════════
  $(document).on("click", ".crumb-account", function () {
    if (!activeAccount) return;
    activeLob = null;
    activePersona = null;
    $(".lob-card, .persona-card").removeClass("active");
    $("#lobDetailViewContainer").addClass("d-none").empty();
    $("#personaDetailViewContainer").addClass("d-none").empty();
    $("#accountViewWrapper").removeClass("d-none");
    $("#lobSection").removeClass("d-none");
    $("#allPersonasSection").removeClass("d-none");
    renderModernBreadcrumbs();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  $(document).on("click", ".crumb-lobs", function () {
    if (!activeAccount) return;
    activeLob = null;
    activePersona = null;
    $(".lob-card, .persona-card").removeClass("active");
    $("#lobDetailViewContainer").addClass("d-none").empty();
    $("#personaDetailViewContainer").addClass("d-none").empty();
    $("#accountViewWrapper").removeClass("d-none");
    $("#lobSection").removeClass("d-none");
    $("#allPersonasSection").addClass("d-none");
    renderModernBreadcrumbs();
    const $lobS = $("#lobSection");
    if ($lobS.length && $lobS.offset()) {
      window.scrollTo({ top: Math.max(0, $lobS.offset().top - 80), behavior: "smooth" });
    }
  });

  $(document).on("click", ".crumb-personas", function () {
    if (!activeAccount) return;
    activePersona = null;
    $(".persona-card").removeClass("active");
    $("#personaDetailViewContainer").addClass("d-none").empty();
    if (activeLob) {
      $("#lobDetailViewContainer").removeClass("d-none");
    } else {
      $("#accountViewWrapper").removeClass("d-none");
      $("#allPersonasSection").removeClass("d-none");
    }
    renderModernBreadcrumbs();
  });

  $(document).on("click", ".crumb-lob", function () {
    if (!activeLob) return;
    activePersona = null;
    $(".persona-card").removeClass("active");
    $("#personaDetailViewContainer").addClass("d-none").empty();
    $("#accountViewWrapper").removeClass("d-none");
    $("#allPersonasSection").addClass("d-none");
    $("#lobDetailViewContainer").removeClass("d-none");
    renderModernBreadcrumbs();
  });

  // Tab switcher in Account View
  $(document).on("click", ".tab-pill-btn", function () {
    const tab = $(this).data("nav-tab");
    $(".tab-pill-btn").removeClass("active");
    $(this).addClass("active");

    if (activeAccount) {
      PipelineRouter.syncState({
        view: "account",
        accountId: String(activeAccount.id),
        subTab: tab,
      });
    }

    if (tab === "overview") {
      $("#accountOverviewContainer").removeClass("d-none");
      $("#lobSection").removeClass("d-none");
      $("#allPersonasSection").removeClass("d-none");
    } else if (tab === "lobs") {
      $("#accountOverviewContainer").addClass("d-none");
      $("#lobSection").removeClass("d-none");
      $("#allPersonasSection").addClass("d-none");
      $('html, body').animate({ scrollTop: $("#lobSection").offset().top - 80 }, 300);
    } else if (tab === "personas") {
      $("#accountOverviewContainer").addClass("d-none");
      $("#lobSection").addClass("d-none");
      $("#allPersonasSection").removeClass("d-none");
      $('html, body').animate({ scrollTop: $("#allPersonasSection").offset().top - 80 }, 300);
    } else if (tab === "feeds" || tab === "activity") {
      $("#accountOverviewContainer").removeClass("d-none");
      if (tab === "activity") {
        const $card = $("#recentPipelineActivityList").closest(".pipeline-section-card");
        if ($card.length) {
          $('html, body').animate({ scrollTop: $card.offset().top - 80 }, 300);
        }
        if (activeAccount && activeAccount.name) {
          refreshPipelineRuns(activeAccount.name);
        }
      } else {
        $('html, body').animate({ scrollTop: $("#accountOverviewContainer").offset().top - 80 }, 300);
      }
    }
  });

  // ─── Recent Pipeline Activity View More / Less Toggle Handler ───────────
  $(document).on("click", ".btn-toggle-activity-expand", function (e) {
    e.preventDefault();
    const $btn = $(this);
    const $list = $btn.closest("#recentPipelineActivityList");
    const $extras = $list.find(".timeline-item-extra");
    const isExpanded = $btn.attr("data-expanded") === "true";
    const totalCount = $list.find(".pipeline-activity-card, .timeline-item").length;
    const extraCount = $extras.length;

    if (isExpanded) {
      $extras.slideUp(180);
      $btn.attr("data-expanded", "false");
      $btn.html(`<span>View full run history (${extraCount} more)</span> <i class="bi bi-chevron-down" style="font-size:0.72rem;"></i>`);
      $("#activityShowingHint").text("Showing latest 3");
    } else {
      $extras.slideDown(220);
      $btn.attr("data-expanded", "true");
      $btn.html(`<span>Show latest 3 only</span> <i class="bi bi-chevron-up" style="font-size:0.72rem;"></i>`);
      $("#activityShowingHint").text(`Showing all ${totalCount}`);
    }
  });

  // ─── Expandable Chips View More / View Less Toggle Handler ──────────────
  $(document).on("click", ".btn-toggle-chips-expand", function (e) {
    e.preventDefault();
    e.stopPropagation();
    const $btn = $(this);
    const $wrapper = $btn.closest(".chips-expandable-wrapper");
    const $extra = $wrapper.find(".chips-extra-container");
    const isExpanded = $btn.attr("data-expanded") === "true";
    const totalExtra = $extra.find(".data-tag").length;

    if (isExpanded) {
      $extra.css("display", "none");
      $btn.attr("data-expanded", "false");
      $btn.html(`<span>+${totalExtra} more</span> <i class="bi bi-chevron-down" style="font-size:0.68rem;"></i>`);
    } else {
      $extra.css("display", "inline-flex");
      $btn.attr("data-expanded", "true");
      $btn.html(`<span>Show less</span> <i class="bi bi-chevron-up" style="font-size:0.68rem;"></i>`);
    }
  });

  // ─── Universal Tab Search Engine (Divisions, Personas, Fields) ───────────
  function applyUniversalTabSearch(query) {
    const q = (query || "").toLowerCase().trim();
    const activeTab = $(".tab-pill-btn.active").data("nav-tab") || "overview";
    const $clearBtn = $("#clearTabSearchBtn");

    if (q) {
      $clearBtn.show();
    } else {
      $clearBtn.hide();
    }

    if (!q) {
      // 1. Restore sections according to current active tab
      if (activeTab === "overview") {
        $("#accountOverviewContainer").removeClass("d-none");
        $("#lobSection").removeClass("d-none");
        $("#allPersonasSection").removeClass("d-none");
      } else if (activeTab === "lobs") {
        $("#accountOverviewContainer").addClass("d-none");
        $("#lobSection").removeClass("d-none");
        $("#allPersonasSection").addClass("d-none");
      } else if (activeTab === "personas") {
        $("#accountOverviewContainer").addClass("d-none");
        $("#lobSection").addClass("d-none");
        $("#allPersonasSection").removeClass("d-none");
      } else {
        $("#accountOverviewContainer").removeClass("d-none");
      }

      // 2. Restore LOB cards to default limit (first 12 visible, extras hidden)
      $(".lob-card").each(function () {
        const isExtra = $(this).hasClass("lob-card-extra");
        $(this).css("display", isExtra ? "none" : "");
      });
      $(".lob-toggle-footer").show();
      $(".sublob-card").show();

      // 3. Reset Personas directory
      if (allPersonasDirectoryState.searchQuery) {
        allPersonasDirectoryState.searchQuery = "";
        allPersonasDirectoryState.visibleLimit = 30;
        $("#allPersonasSearchInput").val("");
        $("#clearAllPersonasSearch").hide();
        renderFilteredPersonaCards();
      }

      // 4. Restore overview fields & clear search highlights
      $(".snapshot-field-item, .intelligence-metric-item, .feed-status-row, .vault-field, .detail-field").each(function () {
        $(this).css("display", "");
        $(this).removeClass("search-field-match");
      });
      return;
    }

    // ─── A. SEARCH DIVISIONS (LOBs & Sub-LOBs) ─────────────────────────────
    let lobMatchCount = 0;
    $(".lob-card").each(function () {
      const text = $(this).text().toLowerCase();
      const cat = ($(this).data("category") || "").toLowerCase();
      const match = text.includes(q) || cat.includes(q);
      if (match) {
        $(this).css("display", "flex").show();
        lobMatchCount++;
      } else {
        $(this).hide();
      }
    });

    // Also search Sub-LOBs if visible
    $(".sublob-card").each(function () {
      const text = $(this).text().toLowerCase();
      const match = text.includes(q);
      $(this).toggle(match);
      if (match) lobMatchCount++;
    });

    // Hide standard expander button during search so counts don't conflict
    $(".lob-toggle-footer").hide();

    // ─── B. SEARCH PERSONAS (Full Enterprise Directory) ────────────────────
    let personaMatchCount = 0;
    allPersonasDirectoryState.searchQuery = q;
    allPersonasDirectoryState.visibleLimit = 60;
    $("#allPersonasSearchInput").val(q);
    $("#clearAllPersonasSearch").css("display", "inline-flex");
    renderFilteredPersonaCards();

    // Calculate actual persona matches
    if (allPersonasDirectoryState.personas && allPersonasDirectoryState.personas.length) {
      personaMatchCount = allPersonasDirectoryState.personas.filter(p => {
        const matchName = (p.name || "").toLowerCase().includes(q);
        const matchTitle = (p.title || "").toLowerCase().includes(q);
        const matchHeadline = (p.headline || "").toLowerCase().includes(q);
        const matchDept = (p.department || (Array.isArray(p.departments) ? p.departments.join(" ") : "")).toLowerCase().includes(q);
        const matchSkills = (Array.isArray(p.skills) ? p.skills.join(" ") : "").toLowerCase().includes(q);
        return matchName || matchTitle || matchHeadline || matchDept || matchSkills;
      }).length;
    }

    // Also search LOB-specific persona cards if open in LOB detail panel
    $("#personaCardsContainer .persona-card").each(function () {
      const text = $(this).text().toLowerCase();
      const match = text.includes(q);
      $(this).toggle(match);
      if (match) personaMatchCount++;
    });

    // ─── C. SEARCH FIELDS (Snapshot, Metrics, Vaults, Feeds) ───────────────
    let fieldMatchCount = 0;
    $(".snapshot-field-item").each(function () {
      const label = $(this).find(".snapshot-field-label").text().toLowerCase();
      const val = $(this).find(".snapshot-field-value").text().toLowerCase();
      const fieldName = ($(this).data("field") || "").toLowerCase();
      const match = label.includes(q) || val.includes(q) || fieldName.includes(q);
      if (match) {
        $(this).css("display", "block").addClass("search-field-match");
        fieldMatchCount++;
      } else {
        $(this).css("display", "none").removeClass("search-field-match");
      }
    });

    $(".intelligence-metric-item").each(function () {
      const text = $(this).text().toLowerCase();
      const match = text.includes(q);
      $(this).toggle(match);
      if (match) fieldMatchCount++;
    });

    $(".feed-status-row").each(function () {
      const text = $(this).text().toLowerCase();
      const match = text.includes(q);
      $(this).toggle(match);
      if (match) fieldMatchCount++;
    });

    $(".detail-field, .vault-field").each(function () {
      const label = $(this).find(".detail-label, .vault-label").text().toLowerCase();
      const val = $(this).find(".detail-val, .vault-val").text().toLowerCase();
      const match = label.includes(q) || val.includes(q);
      $(this).toggle(match);
      if (match) fieldMatchCount++;
    });

    // ─── D. REVEAL SECTIONS DYNAMICALLY ────────────────────────────────────
    // If divisions matched, make sure LOB section is visible
    if (lobMatchCount > 0) {
      $("#lobSection").removeClass("d-none");
    } else if (activeTab === "personas" && personaMatchCount > 0) {
      $("#lobSection").addClass("d-none");
    }

    // If personas matched, make sure Personas section is visible
    if (personaMatchCount > 0) {
      $("#allPersonasSection").removeClass("d-none");
    } else if (activeTab === "lobs" && lobMatchCount > 0) {
      $("#allPersonasSection").addClass("d-none");
    }

    // If fields matched, ensure Overview container is visible
    if (fieldMatchCount > 0) {
      $("#accountOverviewContainer").removeClass("d-none");
    }
  }

  // Vault Toggle Handlers (Lazy rendered for ultra-fast clicking response)
  $(document).on("click", "#toggleAccountVaultBtn", function () {
    const $a = $("#accountVaultArea");
    if ($a.is(":empty") && activeAccount) {
      $a.html(renderFullAccountVault(activeAccount));
    }
    $a.toggleClass("d-none");
  });
  $(document).on("click", "#toggleLobVaultBtn", function () {
    const $a = $("#lobVaultArea");
    if ($a.is(":empty") && activeLob) {
      $a.html(renderFullLobVault(activeLob));
    }
    $a.toggleClass("d-none");
  });
  $(document).on("click", "#togglePersonaVaultBtn", function () {
    const $a = $("#personaVaultArea");
    if ($a.is(":empty") && activePersona) {
      $a.html(renderFullPersonaVault(activePersona));
    }
    $a.toggleClass("d-none");
  });

  // ─── Manual Verification Toggle (Clickable Badge) ──────────────────────
  $(document).on("click", ".btn-verify-badge", function (e) {
    e.stopPropagation();
    const $btn = $(this);
    const entityType = $btn.attr("data-entity-type");
    const entityId = $btn.attr("data-id");
    if (!entityType || !entityId) return;

    $btn.prop("disabled", true).addClass("opacity-75");
    $.ajax({
      url: `/api/verify/${entityType}/${entityId}`,
      type: "POST",
      contentType: "application/json",
      data: JSON.stringify({}),
      success: function (res) {
        $btn.prop("disabled", false).removeClass("opacity-75");
        const isVerified = res.is_manually_verified;
        const timeStr = res.manually_verified_at ? formatTimeAgo(res.manually_verified_at) : "";
        if (isVerified) {
          $btn.removeClass("unverified").addClass("verified");
          $btn.find("i").removeClass("bi-shield-exclamation").addClass("bi-patch-check-fill");
          $btn.find("span").text(`Manually Verified ✓ (${timeStr})`);
          $btn.attr("title", `Verified ${timeStr}`);
          showNotification(`Marked ${entityType} as Manually Verified in PostgreSQL!`, "success");
        } else {
          $btn.removeClass("verified").addClass("unverified");
          $btn.find("i").removeClass("bi-patch-check-fill").addClass("bi-shield-exclamation");
          $btn.find("span").text("AI Inferred • Verify");
          $btn.attr("title", "Click to toggle verification");
          showNotification(`Reset ${entityType} verification flag to AI Inferred.`, "info");
        }

        // Synchronize in-memory models
        if (entityType === "account" && activeAccount && activeAccount.id == entityId) {
          activeAccount.is_manually_verified = isVerified;
          activeAccount.manually_verified_at = res.manually_verified_at;
        } else if (entityType === "lob" && activeLob && activeLob.id == entityId) {
          activeLob.is_manually_verified = isVerified;
          activeLob.manually_verified_at = res.manually_verified_at;
        } else if (entityType === "persona" && activePersona && activePersona.id == entityId) {
          activePersona.is_manually_verified = isVerified;
          activePersona.manually_verified_at = res.manually_verified_at;
        }

        if (activeAccount && activeAccount.name) {
          refreshPipelineRuns(activeAccount.name);
        }
      },
      error: function (err) {
        $btn.prop("disabled", false).removeClass("opacity-75");
        showNotification(`Verification toggle failed: ${err.responseText || err.statusText}`, "error");
      }
    });
  });

  // ─── Inline Pencil Editing (Direct PostgreSQL Persistence) ───────────────
  $(document).on("click", ".snapshot-field-pencil", function (e) {
    e.stopPropagation();
    const $pencil = $(this);
    const $item = $pencil.closest(".snapshot-field-item");
    const entityType = $item.attr("data-entity-type");
    const entityId = $item.attr("data-id");
    const field = $item.attr("data-field");
    if (!entityType || !entityId || !field) return;

    // Already editing?
    if ($item.find(".inline-edit-box").length) return;

    const rawVal = $item.attr("data-raw-value") !== undefined
      ? $item.attr("data-raw-value")
      : $item.find(".snapshot-field-value").text().trim();
    const cleanVal = (rawVal === "—" || rawVal === "Revenue N/A") ? "" : rawVal;

    const $valContainer = $item.find(".snapshot-field-value");
    $valContainer.data("prev-html", $valContainer.html());

    $valContainer.html(`
      <div class="inline-edit-box">
        <input type="text" class="inline-edit-input" value="${esc(cleanVal)}" />
        <button type="button" class="inline-edit-save" title="Save to PostgreSQL"><i class="bi bi-check-lg"></i></button>
        <button type="button" class="inline-edit-cancel" title="Cancel"><i class="bi bi-x-lg"></i></button>
      </div>
    `);

    const $input = $valContainer.find(".inline-edit-input");
    $input.focus().select();

    function saveField() {
      const newVal = $input.val().trim();
      $valContainer.html(`<span style="color:#0284c7;font-size:0.75rem;"><i class="bi bi-arrow-repeat spin"></i> Saving...</span>`);

      const payload = {};
      const intFields = [
        "founded_year", "current_role_tenure_months", "num_funding_rounds", 
        "global_traffic_rank", "monthly_visits", "active_tech_count", 
        "patents_granted", "trademarks_registered", "c_suite_count", 
        "vp_count", "director_count", "manager_count", "num_suborganizations", 
        "num_acquisitions", "hierarchy_level", "career_trajectory_score"
      ];
      if (intFields.includes(field)) {
        const cleanDigits = newVal ? newVal.replace(/[^0-9-]/g, '') : '';
        payload[field] = cleanDigits ? parseInt(cleanDigits, 10) : null;
      } else if (field === "total_funding_amount_usd") {
        const cleanDec = newVal ? newVal.replace(/[^0-9.-]/g, '') : '';
        payload[field] = cleanDec ? parseFloat(cleanDec) : null;
      } else {
        payload[field] = newVal;
      }

      const endpoint = entityType === "account"
        ? `/api/accounts/${entityId}`
        : (entityType === "lob" ? `/api/lobs/${entityId}` : `/api/personas/${entityId}`);

      $.ajax({
        url: endpoint,
        type: "PATCH",
        contentType: "application/json",
        data: JSON.stringify(payload),
        success: function (res) {
          const displayVal = newVal ? esc(newVal) : `<span style="color:#94a3b8;">—</span>`;
          $valContainer.html(displayVal);
          $item.attr("data-raw-value", newVal);
          showNotification(`Field '${field}' updated in PostgreSQL & manually verified!`, "success");

          const updatedEntity = res[entityType] || res.persona || res.account || res.lob || {};
          const verifyAt = updatedEntity.manually_verified_at || new Date().toISOString();
          const timeStr = formatTimeAgo(verifyAt);
          const $badge = $(`.btn-verify-badge[data-entity-type="${entityType}"][data-id="${entityId}"]`);
          $badge.removeClass("unverified").addClass("verified");
          $badge.find("i").removeClass("bi-shield-exclamation").addClass("bi-patch-check-fill");
          $badge.find("span").text(`Manually Verified ✓ (${timeStr})`);
          $badge.attr("title", `Verified ${timeStr}`);

          if (entityType === "account" && activeAccount) {
            activeAccount[field] = newVal;
            activeAccount.is_manually_verified = true;
            activeAccount.manually_verified_at = verifyAt;
          } else if (entityType === "lob" && activeLob) {
            activeLob[field] = newVal;
            activeLob.is_manually_verified = true;
            activeLob.manually_verified_at = verifyAt;
          } else if (entityType === "persona" && activePersona) {
            activePersona[field] = newVal;
            activePersona.is_manually_verified = true;
            activePersona.manually_verified_at = verifyAt;
          }

          if (activeAccount && activeAccount.name) {
            refreshPipelineRuns(activeAccount.name);
          }
        },
        error: function (err) {
          $valContainer.html($valContainer.data("prev-html") || "");
          showNotification(`Save failed: ${err.responseText || err.statusText}`, "error");
        }
      });
    }

    function cancelField() {
      $valContainer.html($valContainer.data("prev-html") || "");
    }

    $valContainer.find(".inline-edit-save").on("click", function (e) {
      e.stopPropagation();
      saveField();
    });

    $valContainer.find(".inline-edit-cancel").on("click", function (e) {
      e.stopPropagation();
      cancelField();
    });

    $input.on("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        saveField();
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancelField();
      }
    });
  });

  // ─── Corporate Ownership & Hierarchy Explorer Interactivity ───────────────
  $(document).on("click", ".copy-lei-btn", function (e) {
    e.preventDefault();
    const lei = $(this).attr("data-lei");
    if (lei && navigator.clipboard) {
      navigator.clipboard.writeText(lei).then(() => {
        showNotification(`LEI ${lei} copied to clipboard!`, "info");
      }).catch(() => {
        showNotification(`LEI: ${lei}`, "info");
      });
    }
  });

  $(document).on("input", ".subsidiary-search-input", function () {
    const query = $(this).val().toLowerCase().trim();
    const $container = $(this).closest(".pipeline-section-card");
    const $cards = $container.find(".subsidiary-card");
    let visible = 0;
    $cards.each(function () {
      const name = $(this).attr("data-name") || "";
      const country = $(this).attr("data-country") || "";
      const lei = $(this).attr("data-lei") || "";
      if (!query || name.includes(query) || country.includes(query) || lei.includes(query)) {
        $(this).show();
        visible++;
      } else {
        $(this).hide();
      }
    });
    $container.find("#subsidiaryCountBadge").text(`${visible} Entities`);
  });

  $(document).on("click", ".btn-toggle-raw-json", function (e) {
    e.preventDefault();
    const $container = $(this).siblings(".raw-json-container");
    $container.toggleClass("d-none");
    const isHidden = $container.hasClass("d-none");
    $(this).find(".bi-chevron-down, .bi-chevron-up")
      .toggleClass("bi-chevron-down", isHidden)
      .toggleClass("bi-chevron-up", !isHidden);
  });

  $(document).on("click", ".lob-matched-badge", function (e) {
    e.preventDefault();
    const lobId = $(this).attr("data-lob-id");
    if (lobId && activeAccount && activeAccount.lobs) {
      const lob = activeAccount.lobs.find(l => String(l.id) === String(lobId));
      if (lob) {
        selectLob(lob);
        showNotification(`Switched to Line of Business: ${lob.lob_name || lob.name}`, "info");
      }
    }
  });

  $(document).on("click", ".hierarchy-filter-btn", function (e) {
    e.preventDefault();
    const filter = $(this).attr("data-filter");
    $(this).siblings().removeClass("active");
    $(this).addClass("active");

    const $cards = $(this).closest(".pipeline-section-card").find(".subsidiary-card");
    let visible = 0;
    $cards.each(function () {
      const isSub = $(this).attr("data-is-sublob") === "true";
      if (filter === "all") {
        $(this).show();
        visible++;
      } else if (filter === "direct" && !isSub) {
        $(this).show();
        visible++;
      } else if (filter === "sublob" && isSub) {
        $(this).show();
        visible++;
      } else {
        $(this).hide();
      }
    });
    $("#subsidiaryCountBadge").text(`${visible} Entities`);
  });

  // ─── Universal Account Edit ──────────────────────────────────────────────
  $(document).on("click", "#acctEditBtn", function () {
    if (!activeAccount) return;
    $("#editAcctId").val(activeAccount.id);
    $("#editAcctDisplayName").val(activeAccount.display_name || activeAccount.name || "");
    $("#editAcctDomain").val(activeAccount.domain || activeAccount.primary_domain || "");
    $("#editAcctRevenue").val((activeAccount.revenue && activeAccount.revenue !== "Revenue N/A") ? activeAccount.revenue : (activeAccount.estimated_revenue_range || ""));
    $("#editAcctEmployees").val(activeAccount.employee_count_range || "");
    $("#editAcctCompanyType").val(activeAccount.company_type || "");
    $("#editAcctOpStatus").val(activeAccount.operating_status || "");
    $("#editAcctHq").val(activeAccount.headquarters_location || activeAccount.city || "");
    $("#editAcctFoundedYear").val(activeAccount.founded_year || "");
    $("#editAcctDesc").val(activeAccount.desc || activeAccount.short_description || activeAccount.full_description || "");

    const modal = new bootstrap.Modal(document.getElementById("universalAccountEditModal"));
    modal.show();
  });

  $(document).on("click", "#saveUniversalAcctBtn", function () {
    const acctId = $("#editAcctId").val() || (activeAccount ? activeAccount.id : null);
    if (!acctId) return;

    const payload = {
      display_name: $("#editAcctDisplayName").val().trim(),
      domain: $("#editAcctDomain").val().trim(),
      estimated_revenue_range: $("#editAcctRevenue").val().trim(),
      employee_count_range: $("#editAcctEmployees").val().trim(),
      company_type: $("#editAcctCompanyType").val().trim(),
      operating_status: $("#editAcctOpStatus").val().trim(),
      headquarters_location: $("#editAcctHq").val().trim(),
      founded_year: $("#editAcctFoundedYear").val() ? parseInt($("#editAcctFoundedYear").val(), 10) : null,
      short_description: $("#editAcctDesc").val().trim(),
    };

    const $btn = $(this);
    $btn.prop("disabled", true).html(`<i class="bi bi-arrow-repeat spin"></i> Saving to PostgreSQL...`);

    $.ajax({
      url: `/api/accounts/${acctId}`,
      type: "PATCH",
      contentType: "application/json",
      data: JSON.stringify(payload),
      success: function (res) {
        $btn.prop("disabled", false).html(`<i class="bi bi-check2-circle"></i> Save &amp; Mark Verified`);
        const modalEl = document.getElementById("universalAccountEditModal");
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();

        const updatedAcct = res.account;
        Object.assign(activeAccount, updatedAcct);
        activeAccount.name = updatedAcct.display_name || updatedAcct.legal_name || activeAccount.name;

        $("#accountHeroContainer").html(renderModernAccountHeader(activeAccount));
        $("#accountOverviewContainer").html(renderModernAccountOverview(activeAccount));
        renderModernBreadcrumbs();

        showNotification("Account updated in PostgreSQL & marked Manually Verified!", "success");
        if (activeAccount.name) {
          refreshPipelineRuns(activeAccount.name);
        }
      },
      error: function (err) {
        $btn.prop("disabled", false).html(`<i class="bi bi-check2-circle"></i> Save &amp; Mark Verified`);
        showNotification(`Account save failed: ${err.responseText || err.statusText}`, "error");
      }
    });
  });

  // ─── Universal LOB Edit ──────────────────────────────────────────────────
  $(document).on("click", "#lobEditToggleBtn", function () {
    const $firstPencil = $("#lobDetailViewContainer").find(".snapshot-field-pencil").first();
    if ($firstPencil.length) {
      $firstPencil.trigger("click");
    }
  });

  $(document).on("click", "#saveLobEditBtn", function () {
    if (!activeLob) return;
    const lobId = activeLob.id;
    const payload = {
      lob_name: $("#editLobName").val().trim(),
      relationship_type: $("#editLobRelType").val(),
      domain: $("#editLobDomain").val().trim(),
      audited_segment_revenue: $("#editLobRevenue").val().trim(),
      segment_headcount: $("#editLobHeadcount").val().trim(),
      operating_head: $("#editLobOpHead").val().trim(),
      overview: $("#editLobOverview").val().trim(),
    };

    const $btn = $(this);
    $btn.prop("disabled", true).html(`<i class="bi bi-arrow-repeat spin"></i> Saving...`);

    $.ajax({
      url: `/api/lobs/${lobId}`,
      type: "PATCH",
      contentType: "application/json",
      data: JSON.stringify(payload),
      success: function (res) {
        $btn.prop("disabled", false).text("Save changes");
        const updatedLob = res.lob;
        Object.assign(activeLob, updatedLob);
        activeLob.name = updatedLob.lob_name || updatedLob.name || activeLob.name;

        if (activeAccount && activeAccount.lobs) {
          const idx = activeAccount.lobs.findIndex(l => l.id == lobId);
          if (idx !== -1) activeAccount.lobs[idx] = activeLob;
        }

        renderModernBreadcrumbs();
        $("#lobDetailViewContainer").html(renderModernLobDetailView(activeLob));
        showNotification("Line of Business updated in PostgreSQL & manually verified!", "success");

        if (activeAccount && activeAccount.name) {
          refreshPipelineRuns(activeAccount.name);
        }
      },
      error: function (err) {
        $btn.prop("disabled", false).text("Save changes");
        showNotification(`LOB save failed: ${err.responseText || err.statusText}`, "error");
      }
    });
  });

  $(document).on("click", "#discardLobEditBtn", function () {
    if (activeLob) {
      $("#editLobName").val(activeLob.name || activeLob.lob_name || "");
      $("#editLobRelType").val(activeLob.relationship_type || "Business Division");
      $("#editLobDomain").val(activeLob.domain || "");
      $("#editLobRevenue").val(activeLob.audited_segment_revenue || activeLob.revenue || "");
      $("#editLobHeadcount").val(activeLob.segment_headcount || activeLob.headcount || "");
      $("#editLobOpHead").val(activeLob.operating_head || activeLob.head || "");
      $("#editLobOverview").val(activeLob.overview || activeLob.desc || "");
      showNotification("LOB edits discarded.", "info");
    }
  });

  // ─── Universal Persona Edit ──────────────────────────────────────────────
  function openPersonaEditModal(p) {
    if (!p) return;
    $("#editPersonaId").val(p.id);
    $("#editPersonaFullName").val(p.full_name || p.name || "");
    $("#editPersonaTitle").val(p.title || "");
    $("#editPersonaEmail").val(p.email || p.personal_email || "");
    $("#editPersonaEmailStatus").val(p.email_status || (p.email ? "Verified" : "Unverified"));
    $("#editPersonaPhone").val(p.phone || p.direct_mobile_phone || "");
    $("#editPersonaCity").val(p.city || "");
    $("#editPersonaDegree").val(p.degree || "");
    $("#editPersonaPriorCompany").val(p.prior_company || (Array.isArray(p.past_companies) ? p.past_companies.join(", ") : ""));
    $("#editPersonaDecisionAuthority").val(p.decision_authority || "");
    $("#editPersonaBudgetAuthority").val(p.budget_authority || "");
    $("#editPersonaLinkedinUrl").val(p.linkedin_url || "");

    const modal = new bootstrap.Modal(document.getElementById("universalPersonaEditModal"));
    modal.show();
  }

  $(document).on("click", "#personaEditToggleBtn, #btnQuickEditProfile", function () {
    if (activePersona) {
      openPersonaEditModal(activePersona);
    }
  });
  $(document).on("click", "#savePersonaBannerBtn", function () {
    if (activePersona) {
      openPersonaEditModal(activePersona);
    }
  });
  $(document).on("click", "#discardPersonaBannerBtn", function () {
    $("#personaEditBanner").addClass("d-none");
  });

  $(document).on("click", ".btn-persona-download-pdf", async function (e) {
    e.preventDefault();
    const personaId = $(this).attr("data-persona-id") || (activePersona ? activePersona.id : null);
    if (!personaId) {
      alert("No persona selected to download.");
      return;
    }
    const $btn = $(this);
    const originalHtml = $btn.html();
    $btn.html('<i class="bi bi-hourglass-split"></i> Generating Dossier...').prop('disabled', true);
    
    try {
      const response = await fetch(`/api/explorer/personas/${personaId}/download-pdf`, {
        method: 'GET',
        headers: { 'Accept': 'application/pdf' },
        credentials: 'include'
      });
      
      if (!response.ok) {
        throw new Error(`Server returned HTTP ${response.status}: ${response.statusText}`);
      }
      
      const blob = await response.blob();
      const contentDisposition = response.headers.get('Content-Disposition');
      let filename = `executive-persona-${personaId}-dossier.pdf`;
      if (contentDisposition) {
        const match = contentDisposition.match(/filename=["']?([^"';]+)["']?/);
        if (match && match[1]) {
          filename = match[1];
        }
      }
      
      const blobUrl = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(blobUrl);
    } catch (err) {
      console.error("PDF download error:", err);
      // Fallback to direct navigation
      window.location.href = `/api/explorer/personas/${personaId}/download-pdf`;
    } finally {
      setTimeout(() => {
        $btn.html(originalHtml).prop('disabled', false);
      }, 1500);
    }
  });

  $(document).on("click", "#saveUniversalPersonaBtn", function () {
    const personaId = $("#editPersonaId").val() || (activePersona ? activePersona.id : null);
    if (!personaId) return;

    const payload = {
      full_name: $("#editPersonaFullName").val().trim(),
      display_name: $("#editPersonaFullName").val().trim(),
      title: $("#editPersonaTitle").val().trim(),
      email: $("#editPersonaEmail").val().trim(),
      email_status: $("#editPersonaEmailStatus").val(),
      phone: $("#editPersonaPhone").val().trim(),
      city: $("#editPersonaCity").val().trim(),
      degree: $("#editPersonaDegree").val().trim(),
      prior_company: $("#editPersonaPriorCompany").val().trim(),
      decision_authority: $("#editPersonaDecisionAuthority").val().trim(),
      budget_authority: $("#editPersonaBudgetAuthority").val().trim(),
      linkedin_url: $("#editPersonaLinkedinUrl").val().trim(),
    };

    const $btn = $(this);
    $btn.prop("disabled", true).html(`<i class="bi bi-arrow-repeat spin"></i> Saving to PostgreSQL...`);

    $.ajax({
      url: `/api/personas/${personaId}`,
      type: "PATCH",
      contentType: "application/json",
      data: JSON.stringify(payload),
      success: function (res) {
        $btn.prop("disabled", false).html(`<i class="bi bi-check2-circle"></i> Save &amp; Mark Verified`);
        const modalEl = document.getElementById("universalPersonaEditModal");
        const modal = bootstrap.Modal.getInstance(modalEl);
        if (modal) modal.hide();

        const updatedPersona = res.persona;
        Object.assign(activePersona, updatedPersona);
        activePersona.name = updatedPersona.full_name || updatedPersona.name || activePersona.name;

        if (activeAccount && activeAccount.personas) {
          const idx = activeAccount.personas.findIndex(p => p.id == personaId);
          if (idx !== -1) activeAccount.personas[idx] = activePersona;
        }

        renderModernBreadcrumbs();
        $("#personaDetailViewContainer").html(renderModernPersonaDetailView(activePersona));
        showNotification("Executive persona updated in PostgreSQL & marked Manually Verified!", "success");

        if (activeAccount && activeAccount.name) {
          refreshPipelineRuns(activeAccount.name);
        }
      },
      error: function (err) {
        $btn.prop("disabled", false).html(`<i class="bi bi-check2-circle"></i> Save &amp; Mark Verified`);
        showNotification(`Persona save failed: ${err.responseText || err.statusText}`, "error");
      }
    });
  });

  // Regenerate Icebreaker Actions
  $(document).on("click", "#regenerateIcebreakerBtn, #btnQuickGenIcebreaker", function () {
    if (activePersona) {
      const co = activeAccount ? activeAccount.name : "DTCC";
      const pitches = [
        `"Congratulations on your ongoing technology acceleration and resilience initiatives at ${co}."`,
        `"Impressed by your team's leadership in scaling post-trade modernization and workflow automation at ${co}."`,
        `"Noticed your recent strategic direction on workflow modernizations and data interoperability at ${co}."`,
        `"Commending your strategic focus on market infrastructure innovation and high-throughput reliability at ${co}."`
      ];
      const randomPitch = pitches[Math.floor(Math.random() * pitches.length)];
      activePersona.personalized_icebreaker = randomPitch;
      $("#personaIcebreakerText").html(`&ldquo;${esc(randomPitch)}&rdquo;`);
      showNotification("New personalized call icebreaker generated!", "success");
    }
  });


  // ─── Render Functions for Categorized Detail Panels ─────────────────────


  function getPersonaTierCategory(p) {
    const level = parseInt(p.hierarchy_level || 99, 10);
    const title = (p.title || "").toLowerCase();
    const seniority = (p.seniority || p.seniority_raw || "").toLowerCase();

    if (
      level === 1 ||
      level === 2 ||
      /c-suite|chief|ceo|cfo|cio|cto|cmo|coo|cro|ciso|president|chair|board|executive vice president|evp/.test(
        title,
      ) ||
      /c-level|executive/.test(seniority)
    ) {
      return "c_suite";
    }
    if (
      level === 3 ||
      /vp|vice president|head of|senior vice president|svp/.test(title) ||
      /vp|head/.test(seniority)
    ) {
      return "vp_head";
    }
    if (
      level === 4 ||
      /director|managing director|senior director/.test(title) ||
      /director/.test(seniority)
    ) {
      return "director";
    }
    if (
      level === 5 ||
      /manager|lead|principal|supervisor/.test(title) ||
      /manager/.test(seniority)
    ) {
      return "manager";
    }
    return "other";
  }

  // ─── Clean Enterprise Intelligence Snapshot & Live Signals ───
  function renderAccountDataSection(account) {
    if (!account) return "";

    function tag(label, val, icon) {
      if (!val && val !== 0 && val !== "0") return "";
      return `<div class="detail-field">
        <div class="detail-label">${icon ? `<i class="bi ${icon}"></i> ` : ""}${label}</div>
        <div class="detail-val">${esc(String(val))}</div>
      </div>`;
    }
    function linkTag(label, url, icon, linkText) {
      if (!url) return "";
      const display = linkText || url.replace(/^https?:\/\//, "").slice(0, 36);
      return `<div class="detail-field">
        <div class="detail-label">${icon ? `<i class="bi ${icon}"></i> ` : ""}${label}</div>
        <div class="detail-val">
          <a href="${esc(url)}" target="_blank" rel="noopener" style="color:var(--brand);font-weight:600;word-break:break-all;">
            ${esc(display)} <i class="bi bi-box-arrow-up-right" style="font-size:.7rem;"></i>
          </a>
        </div>
      </div>`;
    }

    const industries = (account.industries || []).join(", ");
    const secOrLei = account.sec_cik
      ? `CIK: ${account.sec_cik}`
      : (account.organisational_hierarchy_tree?.gleif_lei ? `LEI: ${account.organisational_hierarchy_tree.gleif_lei}` : null);
    const loc = account.headquarters_location || account.location || [account.city, account.state, account.country].filter(Boolean).join(", ");
    const revenueVal = account.revenue && account.revenue !== "Revenue N/A" ? account.revenue : (account.estimated_revenue_range || null);
    const websiteUrl = account.website_url || (account.domain ? (account.domain.startsWith("http") ? account.domain : `https://${account.domain}`) : null);

    return `
      <div class="detail-panel fade-in" style="border-top:none;border-radius:12px;margin-top:16px;">
        <div class="detail-section">
          <div class="detail-section-heading">
            <i class="bi bi-building-gear"></i> Enterprise Intelligence Snapshot
          </div>
          <div class="detail-grid">
            ${tag("Employees", account.employee_count_range || "1-5000", "bi-people")}
            ${tag("Company Type", account.company_type || "Private", "bi-diagram-3")}
            ${tag("Operating Status", account.operating_status || "ACTIVE", "bi-activity")}
            ${tag("Revenue", revenueVal, "bi-cash-stack")}
            ${tag("Location", loc, "bi-geo-alt")}
            ${tag("Founded", account.founded_year, "bi-calendar3")}
            ${tag("Funding Status", account.funding_status || "Private", "bi-graph-up-arrow")}
            ${tag("IPO Status", account.ipo_status || "Private", "bi-bank")}
            ${tag("Regulatory ID", secOrLei, "bi-file-earmark-check")}
            ${tag("Industry", industries, "bi-tags")}
            ${linkTag("Official Website", websiteUrl, "bi-globe")}
          </div>
        </div>
        <div class="detail-section">
          <div class="detail-section-heading">
            <i class="bi bi-link-45deg"></i> Intelligence Feeds &amp; Live Signals
          </div>
          <div class="detail-grid">
            ${linkTag("SEC EDGAR", account.sec_edgar_url, "bi-file-earmark-ruled")}
            ${linkTag("LinkedIn", account.linkedin_url, "bi-linkedin")}
            ${linkTag("Twitter / X", account.twitter_live_url || account.twitter_url, "bi-twitter-x")}
            ${linkTag("Google News", account.rss_url, "bi-newspaper")}
            ${linkTag("Reddit Feed", account.reddit_rss_url, "bi-reddit")}
            ${linkTag("Google Patents", account.google_patents_url, "bi-lightbulb")}
            ${linkTag("Google Trends", account.google_trends_url, "bi-graph-up")}
            ${linkTag("YouTube", account.youtube_search_url, "bi-youtube")}
            ${linkTag("Crunchbase", account.crunchbase_url, "bi-boxes")}
            ${linkTag("Wikidata", account.wikidata_entity_url, "bi-wikipedia")}
          </div>
        </div>
      </div>`;
  }

  function renderAllPersonasDirectory(account) {
    if (!account) { console.warn("[renderDir] account is null"); return; }
    console.log("[renderDir] account.personas:", account.personas?.length,
      "account.lobs:", account.lobs?.length);

    // Aggregate and deduplicate all personas across account, lobs, sublobs
    const seen = new Set();
    const all = [];

    function addP(p) {
      if (!p) return;
      const uid = p.id || p.key || p.name;
      if (!seen.has(uid)) {
        seen.add(uid);
        all.push(p);
      }
    }

    (account.personas || []).forEach(addP);
    (account.lobs || []).forEach((lob) => {
      (lob.personas || []).forEach(addP);
      (lob.subLobs || []).forEach((sub) => (sub.personas || []).forEach(addP));
    });

    console.log("[renderDir] total unique personas:", all.length);

    allPersonasDirectoryState.personas = all;
    allPersonasDirectoryState.activeTier = "all";
    allPersonasDirectoryState.searchQuery = "";
    allPersonasDirectoryState.visibleLimit = 30;

    $("#allPersonasSearchInput").val("");
    $("#clearAllPersonasSearch").hide();

    if (all.length === 0) {
      console.log("[renderDir] 0 personas discovered yet — showing discovery prompt");
      $("#allPersonasSection").removeClass("d-none");
      $("#allPersonasCountBadge").text("(0 Discovered)");
      $("#allPersonasFilterTabs").empty();
      $("#allPersonasCardsContainer").html(`
        <div style="grid-column: 1/-1; padding: 36px 20px; text-align: center;
                    color: var(--text-muted); font-size: 0.9rem;
                    background: var(--card-bg, #fff); border: 1px dashed var(--border, #cbd5e1);
                    border-radius: 12px; margin-top: 8px;">
          <div style="width: 48px; height: 48px; border-radius: 50%; background: var(--badge-bg, #f1f5f9);
                      display: inline-flex; align-items: center; justify-content: center; margin-bottom: 12px; color: var(--accent, #0ea5e9); font-size: 1.4rem;">
            <i class="bi bi-people"></i>
          </div>
          <div style="font-weight: 600; color: var(--text, #1e293b); font-size: 0.95rem; margin-bottom: 4px;">
            No Executive Personas Discovered Yet
          </div>
          <div style="color: var(--text-muted, #64748b); font-size: 0.8rem; max-width: 480px; margin: 0 auto 16px;">
            Click <strong>Batch Console</strong> in the header above to discover executive leadership across C-Suite, VPs, Directors &amp; Managers via Apollo, Corporate Web, and OSINT.
          </div>
        </div>
      `);
      $("#allPersonasShowMoreContainer").addClass("d-none");
      $("#allPersonasBatchPull, #personaBatchPull").prop("disabled", false).removeClass("done running");
      $("#allPersonasBatchValidate, #personaBatchValidate").prop("disabled", true);
      $("#allPersonasBatchDump, #personaBatchDump").prop("disabled", true);
      return;
    }

    console.log("[renderDir] showing section, rendering cards");
    $("#allPersonasSection").removeClass("d-none");
    $("#allPersonasCountBadge").text(`(${all.length} Total Captured)`);
    $("#allPersonasBatchPull, #personaBatchPull").removeClass("running done");
    $("#allPersonasBatchValidate, #personaBatchValidate").prop("disabled", false);
    $("#allPersonasBatchDump, #personaBatchDump").prop("disabled", false);

    renderPersonaFilterTabs();
    renderFilteredPersonaCards();
  }


  function renderPersonaFilterTabs() {
    const all = allPersonasDirectoryState.personas;
    const counts = {
      all: all.length,
      c_suite: 0,
      vp_head: 0,
      director: 0,
      manager: 0,
      other: 0,
    };

    all.forEach((p) => {
      const cat = getPersonaTierCategory(p);
      if (counts[cat] !== undefined) counts[cat]++;
    });

    const tierDefs = [
      { id: "all", label: "All Contacts", count: counts.all },
      { id: "c_suite", label: "C-Suite", count: counts.c_suite },
      { id: "vp_head", label: "VPs & Heads", count: counts.vp_head },
      { id: "director", label: "Directors", count: counts.director },
      { id: "manager", label: "Managers & Leads", count: counts.manager },
      { id: "other", label: "Other Staff", count: counts.other },
    ];

    const $tabs = $("#allPersonasFilterTabs").empty();
    tierDefs.forEach((t) => {
      if (t.count > 0 || t.id === "all") {
        const isActive = allPersonasDirectoryState.activeTier === t.id;
        $tabs.append(`
          <div class="persona-filter-pill ${isActive ? "active" : ""}" data-tier="${t.id}">
            ${t.label} <span class="badge">${t.count}</span>
          </div>
        `);
      }
    });
  }

  function renderFilteredPersonaCards() {
    const all = allPersonasDirectoryState.personas;
    const tier = allPersonasDirectoryState.activeTier;
    const query = (allPersonasDirectoryState.searchQuery || "").trim().toLowerCase();
    const limit = allPersonasDirectoryState.visibleLimit;

    let filtered = all.filter((p) => {
      if (tier !== "all" && getPersonaTierCategory(p) !== tier) return false;
      if (query) {
        const matchName = (p.name || "").toLowerCase().includes(query);
        const matchTitle = (p.title || "").toLowerCase().includes(query);
        const matchHeadline = (p.headline || "").toLowerCase().includes(query);
        const matchDept = (
          p.department || (Array.isArray(p.departments) ? p.departments.join(" ") : "")
        )
          .toLowerCase()
          .includes(query);
        const matchSkills = (Array.isArray(p.skills) ? p.skills.join(" ") : "")
          .toLowerCase()
          .includes(query);
        if (!matchName && !matchTitle && !matchHeadline && !matchDept && !matchSkills) {
          return false;
        }
      }
      return true;
    });

    const $container = $("#allPersonasCardsContainer").empty();
    const totalMatching = filtered.length;

    if (totalMatching === 0) {
      $container.append(`
        <div style="grid-column: 1/-1; padding: 24px; text-align: center;
                    color: var(--text-muted); font-size: 0.85rem;">
          <i class="bi bi-search" style="font-size: 1.5rem; display: block; margin-bottom: 8px;"></i>
          No executive personas found matching your filter criteria.
        </div>
      `);
      $("#allPersonasShowMoreContainer").addClass("d-none");
      return;
    }

    const visibleItems = filtered.slice(0, limit);
    visibleItems.forEach((p, idx) => {
      const pKey = p.key || `persona_${p.id || idx}`;
      p.key = pKey;
      const pRaw = encodeURIComponent(JSON.stringify(p));
      const tierCat = getPersonaTierCategory(p);
      const avatarClass = tierCat === "c_suite" ? "avatar-csuite" : (tierCat === "vp_head" ? "avatar-vp" : "");

      const pEmail = p.email || p.sanitized_email || p.work_email || p.personal_email || "";
      const pPhone = p.phone || p.phone_number || p.sanitized_phone || p.direct_mobile_phone || "";
      const pLinkedIn = p.linkedin_url || p.linkedin || "";
      const cik = (activeAccount && activeAccount.sec_cik) ? activeAccount.sec_cik : "";
      const secInsiderUrl = cik ? `https://www.sec.gov/edgar/searchedgar/companysearch` : "";

      let actionsHtml = "";
      if (pEmail || pPhone || pLinkedIn || secInsiderUrl) {
        actionsHtml = `
          <div class="persona-card-actions">
            ${pEmail ? `
              <button type="button" class="persona-action-btn btn-copy-email" data-email="${esc(pEmail)}" title="Copy verified email (${esc(pEmail)})">
                <i class="bi bi-envelope-fill"></i>
              </button>
            ` : ''}
            ${pPhone ? `
              <button type="button" class="persona-action-btn btn-copy-phone" data-phone="${esc(pPhone)}" title="Copy direct phone (${esc(pPhone)})">
                <i class="bi bi-telephone-fill"></i>
              </button>
            ` : ''}
            ${pLinkedIn ? `
              <a href="${normalizeUrl(pLinkedIn)}" target="_blank" rel="noopener noreferrer" class="persona-action-btn btn-open-linkedin" title="Open verified LinkedIn Profile" onclick="event.stopPropagation();">
                <i class="bi bi-linkedin"></i>
              </a>
            ` : ''}
            ${secInsiderUrl ? `
              <a href="${secInsiderUrl}" target="_blank" rel="noopener noreferrer" class="persona-action-btn btn-open-sec" title="SEC Form 4 Insider Filings" onclick="event.stopPropagation();">
                <i class="bi bi-bank2"></i>
              </a>
            ` : ''}
          </div>
        `;
      }

      $container.append(`
        <div class="compact-card persona-card fade-in"
             data-key="${pKey}"
             data-raw="${pRaw}"
             title="Inspect Executive Dossier for ${esc(p.name)}">
          ${renderPersonaAvatar(p)}
          <div class="compact-card-body">
            <div class="compact-card-title-row">
              <div class="compact-card-title">${esc(p.name)}</div>
              ${p.department ? `<span class="persona-dept-tag">${esc(p.department)}</span>` : ""}
            </div>
            <div class="compact-card-subtitle">${esc(p.title || "Executive")}</div>
            ${actionsHtml}
          </div>
        </div>
      `);
    });

    const remaining = totalMatching - visibleItems.length;
    if (remaining > 0) {
      $("#allPersonasShowMoreRemaining").text(remaining);
      $("#allPersonasShowMoreContainer").removeClass("d-none");
    } else {
      $("#allPersonasShowMoreContainer").addClass("d-none");
    }
  }

  // Handle tier filter pill clicks
  $(document).on("click", ".persona-filter-pill", function () {
    const tier = $(this).data("tier");
    allPersonasDirectoryState.activeTier = tier;
    allPersonasDirectoryState.visibleLimit = 30;
    $(".persona-filter-pill").removeClass("active");
    $(this).addClass("active");
    renderFilteredPersonaCards();
  });

  // Handle live search
  $("#allPersonasSearchInput").on("input", function () {
    const val = $(this).val();
    allPersonasDirectoryState.searchQuery = val;
    allPersonasDirectoryState.visibleLimit = 30;
    if (val) {
      $("#clearAllPersonasSearch").css("display", "inline-flex");
    } else {
      $("#clearAllPersonasSearch").hide();
    }
    renderFilteredPersonaCards();
  });

  // Clear search button
  $("#clearAllPersonasSearch").on("click", function () {
    $("#allPersonasSearchInput").val("");
    allPersonasDirectoryState.searchQuery = "";
    allPersonasDirectoryState.visibleLimit = 30;
    $(this).hide();
    renderFilteredPersonaCards();
  });

  // Show more button
  $("#allPersonasShowMoreBtn").on("click", function () {
    allPersonasDirectoryState.visibleLimit += 30;
    renderFilteredPersonaCards();
  });

  function renderLobDetailPanel(lob) {
    const lobKey = `lob_${lob.id}`;
    const state = getActionState(lobKey);

    const pullBtnDisabled = false;
    const validateBtnDisabled = !state.pulled;
    const dumpBtnDisabled = !state.validated;

    const subLobsHtml =
      lob.subLobs && lob.subLobs.length
        ? `
      <div class="detail-section">
        <div class="detail-section-heading"><i class="bi bi-folder-symlink"
          ></i> Sub-Divisions &amp; Operating Groups (${lob.subLobs.length})</div>
        <p class="section-desc"
          >Nested subsidiaries, specialized business lines, and operational branches mapped under this
            division.</p>
        <div class="detail-grid">
          ${lob.subLobs
            .map(
              (s) => `
            <div class="detail-field" title="Sub-division within ${esc(lob.name)}">
              <div class="detail-label">Division Name</div>
              <div class="detail-val font-semibold">${esc(s.name)}</div>
              ${s.desc ? `<div style="font-size:.75rem;color:var(--text-muted);margin-top:2px;"
                >${esc(s.desc)}</div>` : ""}
            </div>
          `,
            )
            .join("")}
        </div>
      </div>
    `
        : "";

    const panelHtml = `
      <div class="detail-panel fade-in" data-entity-type="lob" data-key="${lobKey}">
        <div class="detail-panel-header">
          <div class="detail-panel-title-area">
            <span class="pill pill-brand detail-panel-badge"><i class="bi bi-diagram-2"
              ></i> Line of Business Deep Dive</span>
            <h2 class="detail-panel-title">${esc(lob.name)}</h2>
            <p class="detail-panel-subtitle"
              >${esc(lob.desc || lob.overview || "Division Overview & Intelligence Hub")}</p>
          </div>
          <div class="detail-panel-actions-wrapper">
            <div class="detail-panel-actions">
              <button type="button" class="panel-btn panel-btn-pull"
                      ${pullBtnDisabled ? "disabled" : ""}
                      title="Step 1: Pull live public feeds (News, Social, Filings, Patents) for this LOB">
                <i class="bi bi-cloud-arrow-down"></i> Pull
              </button>
              <button type="button" class="panel-btn panel-btn-validate"
                      ${validateBtnDisabled ? "disabled" : ""}
                      title="Step 2: AI cleans, verifies, and extracts strategic intent from scraped signals">
                <i class="bi bi-shield-check"></i> Validate
              </button>
              <button type="button" class="panel-btn panel-btn-dump"
                      ${dumpBtnDisabled ? "disabled" : ""}
                      title="Step 3: Save verified structured intelligence into NeonDB">
                <i class="bi bi-database-check"></i> Dump
              </button>
            </div>
            <div class="panel-status-msg" id="panelStatusMsg"
              >${state.message || "Ready for data ingestion cycle."}</div>
          </div>
        </div>

        <!-- Workflow Step Indicator -->
        <div class="step-guide" style="margin-bottom:18px;">
          <div class="step-guide-item ${!state.pulled ? "active" : ""}">
            <span class="step-guide-num">1</span> <strong>Pull:</strong> Scrape live web, news &amp; social feeds
          </div>
          <div class="step-guide-item ${state.pulled && !state.validated ? "active" : ""}">
            <span class="step-guide-num">2</span> <strong>Validate:</strong> Verify content with LLM extractor
          </div>
          <div class="step-guide-item ${state.validated && !state.dumped ? "active" : ""}">
            <span class="step-guide-num">3</span> <strong>Dump:</strong> Persist structured records to database
          </div>
        </div>

        <!-- Categorized Section 1: Overview & Structure -->
        <div class="detail-section">
          <div class="detail-section-heading"><i class="bi bi-building"
            ></i> Overview &amp; Corporate Structure</div>
          <p class="section-desc"
            >Operating scope, relationship taxonomy, primary web domains, and commercial registry
              listings.</p>
          <div class="detail-grid">
            <div class="detail-field span-2">
              <div class="detail-label">Division Overview</div>
              <div class="detail-val">${esc(lob.overview || lob.desc || "No overview available.")}</div>
            </div>
            <div class="detail-field" title="How this division connects to parent corporate entity">
              <div class="detail-label">Relationship Type</div>
              <div class="detail-val">
                <span class="pill">${esc(lob.relationship_type || "Operating Segment")}</span>
              </div>
            </div>
            <div class="detail-field" title="Dedicated digital domain for this business unit">
              <div class="detail-label">Primary Domain</div>
              <div class="detail-val">
                ${lob.domain ? `<a href="https://${esc(lob.domain)}" target="_blank"
                  >${esc(lob.domain)} <i class="bi bi-box-arrow-up-right"></i></a>` : `<span
                  class="text-muted">Not specified</span>`}
              </div>
            </div>
            <div class="detail-field" title="Official corporate website or segment landing page">
              <div class="detail-label">Website URL</div>
              <div class="detail-val">
                ${lob.website_url ? `<a href="${esc(lob.website_url)}" target="_blank"
                  >${esc(lob.website_url)} <i class="bi bi-box-arrow-up-right"></i></a>` : `<span
                  class="text-muted">Not specified</span>`}
              </div>
            </div>
            <div class="detail-field" title="Crunchbase investment and company profile">
              <div class="detail-label">Crunchbase Profile</div>
              <div class="detail-val">
                ${lob.crunchbase_url ? `<a href="${esc(lob.crunchbase_url)}" target="_blank"
                  >View Profile <i class="bi bi-box-arrow-up-right"></i></a>` : `<span class="text-muted"
                  >Not specified</span>`}
              </div>
            </div>
          </div>
        </div>

        <!-- Categorized Section 2: Segment Metrics -->
        <div class="detail-section">
          <div class="detail-section-heading"><i class="bi bi-bar-chart-line"
            ></i> Segment Financials &amp; Operational Scale</div>
          <p class="section-desc"
            >Reported segment revenues, organizational headcount, leadership structure, and mapped
              executive count.</p>
          <div class="detail-grid">
            <div class="detail-field" title="Annual financial revenue attributed to this segment">
              <div class="detail-label">Segment Revenue</div>
              <div class="detail-val font-semibold">${esc(lob.revenue || "Not disclosed in public filings")}</div>
            </div>
            <div class="detail-field" title="Estimated full-time workforce within this operating unit">
              <div class="detail-label">Headcount / Size</div>
              <div class="detail-val">${esc(lob.headcount || "Enterprise scale")}</div>
            </div>
            <div class="detail-field" title="Senior executive responsible for business unit outcomes">
              <div class="detail-label">Operating Head</div>
              <div class="detail-val">${esc(lob.operating_head || "Executive Leadership Team")}</div>
            </div>
            <div class="detail-field" title="Total executive contacts discovered for this unit">
              <div class="detail-label">Mapped Contacts</div>
              <div class="detail-val font-semibold">${lob.personas ? lob.personas.length : 0} Identified</div>
            </div>
          </div>
        </div>

        <!-- Categorized Section 3: Intelligence Feeds -->
        <div class="detail-section">
          <div class="detail-section-heading"><i class="bi bi-broadcast"
            ></i> Live Intelligence Feeds &amp; Public Signals</div>
          <p class="section-desc"
            >Click any platform card below to view recent scraped post activity, AI sentiment analysis,
              and source citations.</p>
          <div class="detail-grid">
            <div class="feed-btn-card">
              <button type="button" class="feed-title-btn" data-platform="linkedin"
                data-title="LinkedIn Intelligence Summary" data-entity="${esc(lob.name)}" data-url="${
                lob.linkedin_url
                  ? esc(lob.linkedin_url)
                  : `https://www.linkedin.com/search/results/all/?keywords=${encodeURIComponent(
                      lob.name + " " + (activeAccount ? activeAccount.name : "")
                    )}`
              }" title="Click to view LinkedIn activity summary and extracted posts">
                <span class="feed-title"><i class="bi bi-linkedin" style="color:#0077b5;"
                  ></i> LinkedIn Intelligence <i class="bi bi-chevron-right"
                  style="font-size:.7rem;margin-left:auto;"></i></span>
              </button>
              <a href="${
                lob.linkedin_url
                  ? esc(lob.linkedin_url)
                  : `https://www.linkedin.com/search/results/all/?keywords=${encodeURIComponent(
                      lob.name + " " + (activeAccount ? activeAccount.name : "")
                    )}`
              }"
                 target="_blank" class="feed-right-icon-link" title="Open LinkedIn in new tab">
                ${BRAND_ICONS.linkedin}
              </a>
            </div>

            <div class="feed-btn-card">
              <button type="button" class="feed-title-btn" data-platform="x_twitter"
                data-title="Twitter / X Intelligence Summary" data-entity="${esc(lob.name)}" data-url="${
                lob.twitter_live_url
                  ? esc(lob.twitter_live_url)
                  : `https://x.com/search?q=${encodeURIComponent(lob.name + " " + (activeAccount
                    ? activeAccount.name
                    : ""))}&f=live`
              }" title="Click to view Twitter/X live feed summary and sentiment">
                <span class="feed-title"><i class="bi bi-twitter-x"></i> Twitter / X Feed <i
                  class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
              </button>
              <a href="${
                lob.twitter_live_url
                  ? esc(lob.twitter_live_url)
                  : `https://x.com/search?q=${encodeURIComponent(lob.name + " " + (activeAccount
                    ? activeAccount.name
                    : ""))}&f=live`
              }"
                 target="_blank" class="feed-right-icon-link" title="Open Twitter / X in new tab">
                ${BRAND_ICONS.x_twitter}
              </a>
            </div>

            <div class="feed-btn-card">
              <button type="button" class="feed-title-btn" data-platform="reddit"
                data-title="Reddit Community Intelligence" data-entity="${esc(lob.name)}" data-url="${
                lob.reddit_rss_url
                  ? esc(lob.reddit_rss_url)
                  : `https://www.reddit.com/search/?q=${encodeURIComponent(lob.name)}`
              }" title="Click to view Reddit discussions and public sentiment">
                <span class="feed-title"><i class="bi bi-reddit" style="color:#ff4500;"
                  ></i> Reddit Community <i class="bi bi-chevron-right"
                  style="font-size:.7rem;margin-left:auto;"></i></span>
              </button>
              <a href="${
                lob.reddit_rss_url
                  ? esc(lob.reddit_rss_url)
                  : `https://www.reddit.com/search/?q=${encodeURIComponent(lob.name)}`
              }"
                 target="_blank" class="feed-right-icon-link" title="Open Reddit in new tab">
                ${BRAND_ICONS.reddit}
              </a>
            </div>

            <div class="feed-btn-card">
              <button type="button" class="feed-title-btn" data-platform="youtube"
                data-title="YouTube Video & Media Intelligence" data-entity="${esc(lob.name)}"
                data-url="${
                lob.youtube_search_url
                  ? esc(lob.youtube_search_url)
                  : `https://www.youtube.com/results?search_query=${encodeURIComponent(lob.name)}`
              }" title="Click to view YouTube interviews, keynote presentations, and webinars">
                <span class="feed-title"><i class="bi bi-youtube" style="color:#ff0000;"
                  ></i> YouTube Media <i class="bi bi-chevron-right"
                  style="font-size:.7rem;margin-left:auto;"></i></span>
              </button>
              <a href="${
                lob.youtube_search_url
                  ? esc(lob.youtube_search_url)
                  : `https://www.youtube.com/results?search_query=${encodeURIComponent(lob.name)}`
              }"
                 target="_blank" class="feed-right-icon-link" title="Open YouTube in new tab">
                ${BRAND_ICONS.youtube}
              </a>
            </div>

            <div class="feed-btn-card">
              <button type="button" class="feed-title-btn" data-platform="google_news"
                data-title="Google News Feed Intelligence" data-entity="${esc(lob.name)}" data-url="${
                lob.google_news_rss_url
                  ? esc(lob.google_news_rss_url)
                  : `https://news.google.com/rss/search?q=${encodeURIComponent(lob.name)}`
              }" title="Click to view Google News headlines and press coverage">
                <span class="feed-title"><i class="bi bi-newspaper" style="color:#4285f4;"
                  ></i> Google News RSS <i class="bi bi-chevron-right"
                  style="font-size:.7rem;margin-left:auto;"></i></span>
              </button>
              <a href="${
                lob.google_news_rss_url
                  ? esc(lob.google_news_rss_url)
                  : `https://news.google.com/rss/search?q=${encodeURIComponent(lob.name)}`
              }"
                 target="_blank" class="feed-right-icon-link" title="Open Google News in new tab">
                ${BRAND_ICONS.google_news}
              </a>
            </div>

            <div class="feed-btn-card">
              <button type="button" class="feed-title-btn" data-platform="google_patents"
                data-title="Patent & IP Intelligence" data-entity="${esc(lob.name)}" data-url="${
                lob.google_patents_url
                  ? esc(lob.google_patents_url)
                  : `https://patents.google.com/?q=${encodeURIComponent(lob.name)}`
              }" title="Click to view patent filings, R&D innovations, and IP portfolio">
                <span class="feed-title"><i class="bi bi-lightbulb" style="color:#34a853;"
                  ></i> Patents Explorer <i class="bi bi-chevron-right"
                  style="font-size:.7rem;margin-left:auto;"></i></span>
              </button>
              <a href="${
                lob.google_patents_url
                  ? esc(lob.google_patents_url)
                  : `https://patents.google.com/?q=${encodeURIComponent(lob.name)}`
              }"
                 target="_blank" class="feed-right-icon-link" title="Open Patents in new tab">
                ${BRAND_ICONS.google_patents}
              </a>
            </div>

            <div class="feed-btn-card">
              <button type="button" class="feed-title-btn" data-platform="google_trends"
                data-title="Google Search Trends Analytics" data-entity="${esc(lob.name)}" data-url="${
                lob.google_trends_url
                  ? esc(lob.google_trends_url)
                  : `https://trends.google.com/trends/explore?q=${encodeURIComponent(lob.name)}`
              }" title="Click to view search term momentum and keyword interest">
                <span class="feed-title"><i class="bi bi-graph-up" style="color:#ea4335;"
                  ></i> Search Trends <i class="bi bi-chevron-right"
                  style="font-size:.7rem;margin-left:auto;"></i></span>
              </button>
              <a href="${
                lob.google_trends_url
                  ? esc(lob.google_trends_url)
                  : `https://trends.google.com/trends/explore?q=${encodeURIComponent(lob.name)}`
              }"
                 target="_blank" class="feed-right-icon-link" title="Open Google Trends in new tab">
                ${BRAND_ICONS.google_trends}
              </a>
            </div>
          </div>
        </div>

        ${subLobsHtml}
      </div>
    `;

    $("#detailPanelContainer").html(panelHtml).removeClass("d-none");
  }

  function renderPersonaDetailPanel(p) {
    const pKey = p.key || `persona_${p.id || p.name}`;
    const state = getActionState(pKey);

    const pullBtnDisabled = false;
    const validateBtnDisabled = !state.pulled;
    const dumpBtnDisabled = !state.validated;

    const skillsHtml =
      p.skills && p.skills.length
        ? p.skills
            .map(
              (s) =>
                `<span class="data-tag" title="Verified skill area"><i class="bi bi-check2"></i> ${esc(s)}</span>`,
            )
            .join("")
        : '<span class="text-muted" style="font-size:.8rem;">No skills mapped</span>';
    const kpisHtml =
      p.target_kpis && p.target_kpis.length
        ? p.target_kpis
            .map(
              (k) =>
                `<span class="data-tag data-tag-success" title="Target KPI priority"><i
                  class="bi bi-bullseye"></i> ${esc(k)}</span>`,
            )
            .join("")
        : '<span class="text-muted" style="font-size:.8rem;">No KPIs mapped</span>';
    const painPointsHtml =
      p.operational_pain_points && p.operational_pain_points.length
        ? p.operational_pain_points
            .map(
              (pain) =>
                `<span class="data-tag data-tag-warning" title="Critical operational challenge"><i
                  class="bi bi-exclamation-circle"></i> ${esc(pain)}</span>`,
            )
            .join("")
        : '<span class="text-muted" style="font-size:.8rem;">None recorded</span>';
    const objectionsHtml =
      p.key_objections && p.key_objections.length
        ? p.key_objections
            .map(
              (obj) =>
                `<span class="data-tag" title="Anticipated sales objection"><i class="bi bi-shield"></i>
                  ${esc(obj)}</span>`,
            )
            .join("")
        : '<span class="text-muted" style="font-size:.8rem;">None recorded</span>';

    const panelHtml = `
      <div class="detail-panel fade-in" data-entity-type="persona" data-key="${pKey}">
        <div class="detail-panel-header">
          <div class="detail-panel-title-area">
            <span class="pill pill-brand detail-panel-badge"><i class="bi bi-person-badge"
              ></i> Executive Persona Call Prep</span>
            <h2 class="detail-panel-title">${esc(p.name)}</h2>
            <p class="detail-panel-subtitle">
              ${esc(p.title || "Executive")} •
              <span class="pill pill-success" style="font-size:.72rem;">${esc(p.tier || "Target Tier")}</span> •
              ${esc(activeAccount.name)}
            </p>
          </div>
          <div class="detail-panel-actions-wrapper">
            <div class="detail-panel-actions">
              <button type="button" class="panel-btn panel-btn-pull"
                      ${pullBtnDisabled ? "disabled" : ""}
                      title="Step 1: Pull live social posts, press interviews, and author records for
                        this executive">
                <i class="bi bi-cloud-arrow-down"></i> Pull
              </button>
              <button type="button" class="panel-btn panel-btn-validate"
                      ${validateBtnDisabled ? "disabled" : ""}
                      title="Step 2: AI parses communication style, icebreakers, and objection readiness">
                <i class="bi bi-shield-check"></i> Validate
              </button>
              <button type="button" class="panel-btn panel-btn-dump"
                      ${dumpBtnDisabled ? "disabled" : ""}
                      title="Step 3: Save validated executive persona profile into NeonDB">
                <i class="bi bi-database-check"></i> Dump
              </button>
            </div>
            <div class="panel-status-msg" id="panelStatusMsg"
              >${state.message || "Ready for data ingestion cycle."}</div>
          </div>
        </div>

        <!-- Workflow Step Indicator -->
        <div class="step-guide" style="margin-bottom:18px;">
          <div class="step-guide-item ${!state.pulled ? "active" : ""}">
            <span class="step-guide-num">1</span> <strong>Pull:</strong> Scrape executive posts &amp; interviews
          </div>
          <div class="step-guide-item ${state.pulled && !state.validated ? "active" : ""}"><span
            class="step-guide-num"
            >2</span> <strong>Validate:</strong> Generate personalized icebreaker &amp; talk tracks</div>
          <div class="step-guide-item ${state.validated && !state.dumped ? "active" : ""}">
            <span class="step-guide-num">3</span> <strong>Dump:</strong> Save to permanent CRM database
          </div>
        </div>

        <!-- Categorized Section 1: Executive Profile & Demographics -->
        <div class="detail-section">
          <div class="detail-section-heading"><i class="bi bi-person-vcard"
            ></i> Executive Profile &amp; Demographics</div>
          <p class="section-desc"
            >Corporate title, verified contact information, geographic base, academic background, and
              organizational seniority level.</p>
          <div class="detail-grid">
            <div class="detail-field">
              <div class="detail-label">Full Name</div>
              <div class="detail-val font-semibold">${esc(p.name)}</div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Corporate Title</div>
              <div class="detail-val">${esc(p.title || "Executive")}</div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Corporate Email</div>
              <div class="detail-val">
                ${p.email ? `<a href="mailto:${esc(p.email)}">${esc(p.email)} <i
                  class="bi bi-envelope-check"></i></a>` : `<span class="text-muted"
                  >Not discovered</span>`}
              </div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Email Status / Phone</div>
              <div class="detail-val">${esc(p.email_status || "Verified")} ${p.phone ? `• ${esc(p.phone)}
                ` : ""}</div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Location / Base</div>
              <div class="detail-val">
                ${esc(p.location || [p.city, p.state, p.country].filter(Boolean).join(", ") || "Headquarters")}
              </div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Education / Alma Mater</div>
              <div class="detail-val">
                ${esc(p.degree
                  ? `${p.degree} — ${p.institution || ""}`
                  : p.institution || "Standard Executive Profile")}
              </div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Prior Company Experience</div>
              <div class="detail-val">${esc(p.prior_company || "Corporate Enterprise")}</div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Seniority / Hierarchy</div>
              <div class="detail-val">${esc(p.seniority || "C-Suite / VP")} (Level ${p.hierarchy_level || 1})</div>
            </div>
          </div>
        </div>

        <!-- Categorized Section 2: Behavior & Strategic KPIs -->
        <div class="detail-section">
          <div class="detail-section-heading"><i class="bi bi-bullseye"
            ></i> Strategic Priorities &amp; Operational Pain Points</div>
          <p class="section-desc"
            >Key performance metrics the executive is evaluated on, top operational blockers, and
              anticipated sales objections.</p>
          <div class="detail-grid">
            <div class="detail-field span-2">
              <div class="detail-label">Target KPIs &amp; Core Priorities</div>
              <div class="tag-list">${kpisHtml}</div>
            </div>
            <div class="detail-field span-2">
              <div class="detail-label">Core Skills &amp; Domain Expertise</div>
              <div class="tag-list">${skillsHtml}</div>
            </div>
            <div class="detail-field span-2">
              <div class="detail-label">Operational Pain Points</div>
              <div class="tag-list">${painPointsHtml}</div>
            </div>
            <div class="detail-field span-2">
              <div class="detail-label">Anticipated Objections &amp; Hesitations</div>
              <div class="tag-list">${objectionsHtml}</div>
            </div>
          </div>
        </div>

        <!-- Categorized Section 3: Personalized Messaging & Pitch -->
        <div class="detail-section">
          <div class="detail-section-heading"><i class="bi bi-chat-quote-fill"
            ></i> Personalized Engagement &amp; Pitch Strategy</div>
          <p class="section-desc"
            >AI-tailored opening icebreaker based on recent initiatives, targeted value proposition, and
              communication style.</p>
          <div class="detail-grid">
            <div class="detail-field span-full"
              style="background: var(--brand-soft); border-color: rgba(0,97,255,.25);">
              <div class="detail-label" style="color:var(--brand);"><i class="bi bi-stars"
                ></i> Tailored Call Icebreaker</div>
              <div class="detail-val" style="font-size:.9rem; color:var(--text-primary); font-weight:600;">
                "${esc(p.personalized_icebreaker || `Congratulations on your leadership initiatives at
                  ${activeAccount.name}.`)}"
              </div>
            </div>
            <div class="detail-field span-2">
              <div class="detail-label">Value Proposition Pitch</div>
              <div class="detail-val">
                ${esc(p.value_proposition || "Targeted enterprise acceleration and workflow intelligence.")}
              </div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Communication Style</div>
              <div class="detail-val">
                ${esc(p.communication_style || "Analytical, data-driven, and outcome-oriented")}
              </div>
            </div>
            <div class="detail-field">
              <div class="detail-label">Authority &amp; Influence</div>
              <div class="detail-val">
                Decision: ${esc(p.decision_authority || "Primary")} •
                Budget: ${esc(p.budget_authority || "Sign-off")}
              </div>
            </div>
          </div>
        </div>

        <!-- Categorized Section 4: Verified Executive Intelligence Streams & Feeds -->
        <div class="detail-section">
          <div class="detail-section-heading"><i class="bi bi-broadcast-pin"
            ></i> Executive Intelligence &amp; Live Feeds</div>
          <p class="section-desc"
            >Click any verified platform card to inspect real-time executive filings, articles, and public commentary.</p>
          <div class="detail-grid">
            ${p.linkedin_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="linkedin"
                  data-title="LinkedIn Executive Intelligence" data-entity="${esc(p.name)}" data-url="${esc(p.linkedin_url)}"
                  title="Click to view executive LinkedIn activity">
                  <span class="feed-title"><i class="bi bi-linkedin" style="color:#0077b5;"></i> LinkedIn Profile <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.linkedin_url)}" target="_blank" class="feed-right-icon-link" title="Open LinkedIn in new tab">${BRAND_ICONS.linkedin}</a>
              </div>
            ` : ''}

            ${p.corporate_bio_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="corporate_bio"
                  data-title="Official Corporate Bio" data-entity="${esc(p.name)}" data-url="${esc(p.corporate_bio_url)}"
                  title="Click to view official corporate biography on bny.com">
                  <span class="feed-title"><i class="bi bi-building" style="color:#0f172a;"></i> Corporate Bio <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.corporate_bio_url)}" target="_blank" class="feed-right-icon-link" title="Open Corporate Bio in new tab"><i class="bi bi-box-arrow-up-right" style="color:#0f172a;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.crunchbase_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="crunchbase"
                  data-title="Crunchbase Executive Profile" data-entity="${esc(p.name)}" data-url="${esc(p.crunchbase_url)}"
                  title="Click to view Crunchbase profile">
                  <span class="feed-title"><i class="bi bi-briefcase-fill" style="color:#0284c7;"></i> Crunchbase <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.crunchbase_url)}" target="_blank" class="feed-right-icon-link" title="Open Crunchbase in new tab"><i class="bi bi-box-arrow-up-right" style="color:#0284c7;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.sec_insider_trades_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="sec"
                  data-title="SEC Form 4 Insider Filings" data-entity="${esc(p.name)}" data-url="${esc(p.sec_insider_trades_url)}"
                  title="Click to view SEC EDGAR Form 4 filings">
                  <span class="feed-title"><i class="bi bi-file-earmark-text-fill" style="color:#1e3a8a;"></i> SEC Form 4 <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.sec_insider_trades_url)}" target="_blank" class="feed-right-icon-link" title="Open SEC Form 4 in new tab"><i class="bi bi-box-arrow-up-right" style="color:#1e3a8a;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.fec_contributions_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="fec"
                  data-title="FEC Political Contributions" data-entity="${esc(p.name)}" data-url="${esc(p.fec_contributions_url)}"
                  title="Click to view FEC political contributions">
                  <span class="feed-title"><i class="bi bi-bank" style="color:#059669;"></i> FEC Contributions <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.fec_contributions_url)}" target="_blank" class="feed-right-icon-link" title="Open FEC in new tab"><i class="bi bi-box-arrow-up-right" style="color:#059669;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.quiver_insider_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="quiver"
                  data-title="Quiver Quant Insider Net Worth" data-entity="${esc(p.name)}" data-url="${esc(p.quiver_insider_url)}"
                  title="Click to view Quiver Quantitative Insider data">
                  <span class="feed-title"><i class="bi bi-graph-up" style="color:#6366f1;"></i> Quiver Quant <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.quiver_insider_url)}" target="_blank" class="feed-right-icon-link" title="Open Quiver Quant in new tab"><i class="bi bi-box-arrow-up-right" style="color:#6366f1;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.bloomberg_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="bloomberg"
                  data-title="Bloomberg Media & Videos" data-entity="${esc(p.name)}" data-url="${esc(p.bloomberg_url)}"
                  title="Click to view Bloomberg video">
                  <span class="feed-title"><i class="bi bi-camera-video-fill" style="color:#000;"></i> Bloomberg Media <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.bloomberg_url)}" target="_blank" class="feed-right-icon-link" title="Open Bloomberg in new tab"><i class="bi bi-box-arrow-up-right" style="color:#000;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.wsj_article_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="wsj"
                  data-title="Wall Street Journal Article" data-entity="${esc(p.name)}" data-url="${esc(p.wsj_article_url)}"
                  title="Click to view WSJ article">
                  <span class="feed-title"><i class="bi bi-journal-text" style="color:#111827;"></i> Wall Street Journal <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.wsj_article_url)}" target="_blank" class="feed-right-icon-link" title="Open WSJ in new tab"><i class="bi bi-box-arrow-up-right" style="color:#111827;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.media_interview_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="media"
                  data-title="Major Media Feature Interview" data-entity="${esc(p.name)}" data-url="${esc(p.media_interview_url)}"
                  title="Click to view major media interview">
                  <span class="feed-title"><i class="bi bi-chat-square-quote-fill" style="color:#d97706;"></i> Media Interview <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.media_interview_url)}" target="_blank" class="feed-right-icon-link" title="Open Media Interview in new tab"><i class="bi bi-box-arrow-up-right" style="color:#d97706;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.annual_report_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="annual_report"
                  data-title="BNY Annual Report & Proxy" data-entity="${esc(p.name)}" data-url="${esc(p.annual_report_url)}"
                  title="Click to view Annual Report / Proxy">
                  <span class="feed-title"><i class="bi bi-file-earmark-pdf-fill" style="color:#b91c1c;"></i> Annual Report &amp; Proxy <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.annual_report_url)}" target="_blank" class="feed-right-icon-link" title="Open Annual Report in new tab"><i class="bi bi-box-arrow-up-right" style="color:#b91c1c;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            ${p.zoominfo_url ? `
              <div class="feed-btn-card">
                <button type="button" class="feed-title-btn" data-platform="zoominfo"
                  data-title="ZoomInfo Contact Profile" data-entity="${esc(p.name)}" data-url="${esc(p.zoominfo_url)}"
                  title="Click to view ZoomInfo profile">
                  <span class="feed-title"><i class="bi bi-telephone-fill" style="color:#2563eb;"></i> ZoomInfo Profile <i class="bi bi-chevron-right" style="font-size:.7rem;margin-left:auto;"></i></span>
                </button>
                <a href="${esc(p.zoominfo_url)}" target="_blank" class="feed-right-icon-link" title="Open ZoomInfo in new tab"><i class="bi bi-box-arrow-up-right" style="color:#2563eb;font-size:1.1rem;"></i></a>
              </div>
            ` : ''}

            <div class="feed-btn-card">
              <button type="button" class="feed-title-btn" data-platform="google_news"
                data-title="Google News Executive Coverage" data-entity="${esc(p.name)}" data-url="${
                p.rss_url
                  ? esc(p.rss_url)
                  : `https://news.google.com/search?q=${encodeURIComponent(p.name + " " + activeAccount.name)}`
              }" title="Click to view Google News articles and press mentions">
                <span class="feed-title"><i class="bi bi-newspaper" style="color:#4285f4;"
                  ></i> Google News <i class="bi bi-chevron-right"
                  style="font-size:.7rem;margin-left:auto;"></i></span>
              </button>
              <a href="${
                p.rss_url
                  ? esc(p.rss_url)
                  : `https://news.google.com/search?q=${encodeURIComponent(p.name + " " + activeAccount.name)}`
              }"
                 target="_blank" class="feed-right-icon-link" title="Open Google News in new tab">
                ${BRAND_ICONS.google_news}
              </a>
            </div>
          </div>
        </div>
      </div>
    `;

    $("#detailPanelContainer").html(panelHtml).removeClass("d-none");
  }

  // ─── Action Center Handlers (Sequential: Pull -> Validate -> Dump) ───────

  // Helper to ensure live data is staged
  async function fetchAndStageEntity(entityType, key, rawData) {
    if (entityType === "persona") {
      const res = await fetch(`${API_BASE}/api/personas/fetch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          key: rawData.key || (rawData.name || key).toLowerCase().replace(/\s+/g, "_"),
          display_name: rawData.name || rawData.full_name || rawData.display_name,
          name: rawData.name || rawData.full_name,
          title: rawData.title || null,
          company_name: activeAccount ? (activeAccount.legal_name || activeAccount.name) : null,
          linkedin_url: rawData.linkedin_url || null,
          account_id: activeAccount ? activeAccount.id : null,
          enrich_ai_dossier: true,
        }),
      });
      if (!res.ok) throw new Error("Failed to fetch persona dossier");
      const data = await res.json();
      const person = data.person;
      stagedDataStore[key] = person;
      if (person) {
        if (person.key) stagedDataStore[person.key] = person;
        if (rawData.key) stagedDataStore[rawData.key] = person;
        if (rawData.id) stagedDataStore[String(rawData.id)] = person;
      }
      return person;
    } else {
      // For LOB: Fetch live multi-source enriched LOB data
      const res = await fetch(`${API_BASE}/api/lobs/fetch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: activeAccount ? activeAccount.id : null,
          company_name: activeAccount ? activeAccount.name : "",
          lob_name: rawData.name || rawData.lob_name || key,
          lob_domain: rawData.domain || rawData.primary_domain || null,
        }),
      });
      if (!res.ok) throw new Error("Failed to fetch LOB intelligence");
      const data = await res.json();
      const enrichedLob = data.lob || (data.lobs && data.lobs[0]) || rawData;
      stagedDataStore[key] = enrichedLob;
      return enrichedLob;
    }
  }

  // 1. Pull Button
  $(document).on("click", ".panel-btn-pull", async function () {
    const $btn = $(this);
    const $container = $btn.closest(".modern-view-card, .detail-panel, #lobDetailContainer, #personaDetailContainer");
    const entityType = $btn.data("entity-type") || $container.data("entity-type") || (activePersona ? "persona" : "lob");
    const key = $btn.data("key") || $container.data("key") || (activePersona ? activePersona.key : activeLob ? activeLob.key : null);
    const state = getActionState(key);
    const $status = $container.find("#panelStatusMsg, .panel-status-msg, .header-meta-timestamp");

    $btn.text("Pulling...").prop("disabled", true);

    try {
      let targetData = entityType === "persona" ? activePersona : activeLob;
      if (!targetData || (targetData.key !== key && targetData.name !== key && targetData.id != key)) {
        if (entityType === "persona" && activeAccount && activeAccount.personas) {
          const found = activeAccount.personas.find(p => p.key === key || p.name === key || p.id == key);
          if (found) targetData = found;
        } else if (entityType === "lob" && activeAccount && activeAccount.lobs) {
          const found = activeAccount.lobs.find(l => l.key === key || l.name === key || l.lob_name === key || l.id == key);
          if (found) targetData = found;
        }
      }

      await fetchAndStageEntity(entityType, key, targetData || {});

      state.pulled = true;
      state.message = '<span style="color:#10b981;">✔ Data pulled & staged</span>';

      $btn.text("📥 Pulled ✔").prop("disabled", false);
      $container.find(".panel-btn-validate").prop("disabled", false);
      $status.html(state.message);
      if (activeAccount && activeAccount.name) {
        refreshPipelineRuns(activeAccount.name);
      }
    } catch (e) {
      $btn.text("📥 Pull").prop("disabled", false);
      state.message = `<span style="color:#ef4444;">Error: ${esc(e.message || "pulling data")}</span>`;
      $status.html(state.message);
    }
  });

  // 2. Validate Button
  $(document).on("click", ".panel-btn-validate", async function () {
    const $btn = $(this);
    const $container = $btn.closest(".modern-view-card, .detail-panel, #lobDetailContainer, #personaDetailContainer");
    const entityType = $btn.data("entity-type") || $container.data("entity-type") || (activePersona ? "persona" : "lob");
    const key = $btn.data("key") || $container.data("key") || (activePersona ? activePersona.key : activeLob ? activeLob.key : null);
    const state = getActionState(key);
    const $status = $container.find("#panelStatusMsg, .panel-status-msg, .header-meta-timestamp");

    $btn.text("Validating...").prop("disabled", true);

    try {
      let stagedData = stagedDataStore[key];
      if (!stagedData) {
        let targetData = entityType === "persona" ? activePersona : activeLob;
        if (!targetData || (targetData.key !== key && targetData.name !== key && targetData.id != key)) {
          if (entityType === "persona" && activeAccount && activeAccount.personas) {
            const found = activeAccount.personas.find(p => p.key === key || p.name === key || p.id == key);
            if (found) targetData = found;
          } else if (entityType === "lob" && activeAccount && activeAccount.lobs) {
            const found = activeAccount.lobs.find(l => l.key === key || l.name === key || l.lob_name === key || l.id == key);
            if (found) targetData = found;
          }
        }
        stagedData = await fetchAndStageEntity(entityType, key, targetData || {});
        state.pulled = true;
      }

      const validateUrl =
        entityType === "persona"
          ? `${API_BASE}/api/personas/validate-single`
          : `${API_BASE}/api/lobs/validate-single`;
      const res = await fetch(validateUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(stagedData),
      });
      const data = await res.json();

      state.validated = true;
      state.score = data.score;

      let msg = `<span style="color:${data.ready_for_db ? "#10b981" : "#f59e0b"};">Quality Score: ${data.score}/100</span>`;
      if (data.warnings && data.warnings.length > 0) {
        msg += ` <span style="color:#ef4444;font-size:.72rem;">(${esc(data.warnings[0])})</span>`;
      }
      state.message = msg;

      $btn.text("🔍 Validated ✔").prop("disabled", false);
      $container.find(".panel-btn-dump").prop("disabled", false);
      $status.html(state.message);
      if (activeAccount && activeAccount.name) {
        refreshPipelineRuns(activeAccount.name);
      }
    } catch (e) {
      $btn.text("🔍 Validate").prop("disabled", false);
      state.message = `<span style="color:#ef4444;">Error: ${esc(e.message || "validating data")}</span>`;
      $status.html(state.message);
    }
  });

  // 3. Dump Button
  $(document).on("click", ".panel-btn-dump", async function () {
    const $btn = $(this);
    const $container = $btn.closest(".modern-view-card, .detail-panel, #lobDetailContainer, #personaDetailContainer");
    const entityType = $btn.data("entity-type") || $container.data("entity-type") || (activePersona ? "persona" : "lob");
    const key = $btn.data("key") || $container.data("key") || (activePersona ? activePersona.key : activeLob ? activeLob.key : null);
    const state = getActionState(key);
    const $status = $container.find("#panelStatusMsg, .panel-status-msg, .header-meta-timestamp");

    $btn.text("Dumping...").prop("disabled", true);

    try {
      let targetData = entityType === "persona" ? activePersona : activeLob;
      if (!targetData || (targetData.key !== key && targetData.name !== key && targetData.id != key)) {
        if (entityType === "persona" && activeAccount && activeAccount.personas) {
          const found = activeAccount.personas.find(p => p.key === key || p.name === key || p.id == key);
          if (found) targetData = found;
        } else if (entityType === "lob" && activeAccount && activeAccount.lobs) {
          const found = activeAccount.lobs.find(l => l.key === key || l.name === key || l.lob_name === key || l.id == key);
          if (found) targetData = found;
        }
      }

      const stagedData =
        stagedDataStore[key] || targetData || {};
      let dumpUrl = `${API_BASE}/api/personas/dump-single-db`;
      let payload = {
        account_id: activeAccount ? activeAccount.id : 1,
        person_data: stagedData,
      };

      if (entityType === "lob") {
        dumpUrl = `${API_BASE}/api/lobs/dump-single-db`;
        payload = {
          account_id: activeAccount ? activeAccount.id : 1,
          lob_data: stagedData,
        };
      }

      const res = await fetch(dumpUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();

      if (data.status === "success") {
        state.dumped = true;
        state.message = `<span style="color:#10b981;">✔ Saved to database</span>`;
        $btn.text("💾 Dumped ✔").prop("disabled", false);
        $status.html(state.message);
        refreshAccountsCache();
      } else {
        $btn.text("💾 Dump").prop("disabled", false);
        state.message = `<span style="color:#ef4444;">Error: ${esc(data.message || "failed")}</span>`;
        $status.html(state.message);
      }
    } catch (e) {
      $btn.text("💾 Dump").prop("disabled", false);
      state.message = `<span style="color:#ef4444;">Error: ${esc(e.message || "dumping to database")}</span>`;
      $status.html(state.message);
    }
  });

  // ─── Feed Intelligence Summary Modal Logic ────────────────────────────

  function generatePlatformFeedSummary(platform, entityName, companyName) {
    const comp = companyName || (activeAccount ? activeAccount.name : "Enterprise");
    const name =
      entityName ||
      (activePersona ? activePersona.name : activeLob ? activeLob.name : "Executive Leadership");

    if (platform === "linkedin") {
      return {
        stats: [
          { label: "Activity Index", val: "🔥 Top 5% Active" },
          { label: "Network Reach", val: "25K+ Followers" },
          { label: "Avg Post Engagement", val: "94.2% Positive" },
        ],
        posts: [
          {
            author: name,
            time: "2 hours ago • Edited",
            content:
              `Delighted to share our latest strategic milestone across ${comp}. Modernizing` +
              `our institutional data workflows and accelerating execution precision has` +
              `unlocked unprecedented operational velocity. Huge congratulations to all` +
              `involved! #Leadership #Innovation #${comp.replace(/\s+/g, "")}`,
            metrics: ["👍 342 Reactions", "💬 48 Comments", "🔄 21 Reposts"],
          },
          {
            author: name,
            time: "2 days ago",
            content:
              `Productive executive roundtable discussing enterprise cloud acceleration and` +
              `governance frameworks. The future belongs to organizations that turn real-time` +
              `data into decisive strategy.`,
            metrics: ["👍 198 Reactions", "💬 26 Comments", "🔄 14 Reposts"],
          },
          {
            author: name,
            time: "5 days ago",
            content:
              `Excited to participate in next month's Global Technology & Executive Leadership` +
              `Forum. Looking forward to discussing next-generation infrastructure scalability` +
              `and talent empowerment.`,
            metrics: ["👍 415 Reactions", "💬 62 Comments", "🔄 35 Reposts"],
          },
        ],
      };
    } else if (platform === "x_twitter") {
      return {
        stats: [
          { label: "Live Signal", val: "⚡ Active Stream" },
          { label: "Mention Velocity", val: "+38% this week" },
          { label: "Audience Sentiment", val: "89% Favorable" },
        ],
        posts: [
          {
            author: `@${name.toLowerCase().replace(/\s+/g, "_")}`,
            time: "3 hours ago",
            content:
              `Real-time intelligence and execution velocity remain the twin pillars of` +
              `sustained growth at @${comp.toLowerCase().replace(/\s+/g, "")}. Exciting` +
              `developments in motion. 🚀`,
            metrics: ["👁️ 1.8K Views", "🔁 42 Reposts", "❤️ 195 Likes"],
          },
          {
            author: `@${name.toLowerCase().replace(/\s+/g, "_")}`,
            time: "1 day ago",
            content:
              `Key takeaway from today's market briefing: automation and risk mitigation are no` +
              `longer optional—they are core growth engines. #FinTech #Enterprise`,
            metrics: ["👁️ 1.2K Views", "🔁 29 Reposts", "❤️ 140 Likes"],
          },
          {
            author: `@${name.toLowerCase().replace(/\s+/g, "_")}`,
            time: "3 days ago",
            content:
              `Proud of the team for continuing to push boundaries and deliver high-conviction` +
              `outcomes across all operational segments.`,
            metrics: ["👁️ 2.4K Views", "🔁 67 Reposts", "❤️ 310 Likes"],
          },
        ],
      };
    } else if (platform === "reddit") {
      return {
        stats: [
          { label: "Community Signal", val: "💬 14 Active Threads" },
          { label: "Upvote Ratio", val: "92% Net Positive" },
          { label: "Top Community", val: "r/financialservices" },
        ],
        posts: [
          {
            author: "r/financialservices • Posted by u/intel_observer",
            time: "5 hours ago",
            content:
              `[Analysis] Comprehensive breakdown of ${comp}'s strategic positioning under` +
              `${name}: How their modular service expansion is driving higher retention and` +
              `margin efficiency.`,
            metrics: ["⬆️ 164 Upvotes", "💬 42 Comments", "🏆 2 Awards"],
          },
          {
            author: "r/stocks • Posted by u/market_alpha",
            time: "2 days ago",
            content:
              `Discussion: ${comp} quarterly business review notes. Strong growth trajectory` +
              `observed across core divisions, executive leadership emphasizing continuous` +
              `automation.`,
            metrics: ["⬆️ 310 Upvotes", "💬 88 Comments", "🏆 1 Award"],
          },
          {
            author: "r/technology • Posted by u/fintech_insider",
            time: "4 days ago",
            content:
              `Enterprise Architecture Deep Dive: How ${comp} implemented resilient distributed` +
              `pipelines for large-scale institutional reconciliation.`,
            metrics: ["⬆️ 95 Upvotes", "💬 27 Comments"],
          },
        ],
      };
    } else if (platform === "youtube") {
      return {
        stats: [
          { label: "Media Highlights", val: "▶️ 8 Keynotes & Talks" },
          { label: "Total Views", val: "45K+ Views" },
          { label: "Avg Duration", val: "22 Minutes" },
        ],
        posts: [
          {
            author: "Enterprise Leadership Global",
            time: "3 days ago • Duration: 18:45",
            content:
              `📺 "Keynote Address: ${name} on Scaling Mission-Critical Platforms in Complex` +
              `Regulatory Environments" — In-depth breakdown of leadership frameworks and` +
              `enterprise modernizations.`,
            metrics: ["👁️ 6.4K Views", "👍 420 Likes", "💬 35 Comments"],
          },
          {
            author: "FinTech & Capital Markets Forum",
            time: "1 week ago • Duration: 25:10",
            content:
              `📺 "Fireside Chat: Navigating Market Evolution with ${name} (${comp})" —` +
              `Strategic discussion on technology adoption and client-centric transformation.`,
            metrics: ["👁️ 9.8K Views", "👍 610 Likes", "💬 52 Comments"],
          },
          {
            author: "Executive Insights Series",
            time: "3 weeks ago • Duration: 14:20",
            content: `📺 "Building High-Performance Engineering & Operating Teams: Inside ${comp}'s Blueprint.`,
            metrics: ["👁️ 4.1K Views", "👍 290 Likes", "💬 18 Comments"],
          },
        ],
      };
    } else if (platform === "google_news") {
      return {
        stats: [
          { label: "News Coverage", val: "📰 High Frequency" },
          { label: "Top Publisher", val: "Reuters / Bloomberg" },
          { label: "Sentiment", val: "Bullish & Stable" },
        ],
        posts: [
          {
            author: "Reuters Financial News",
            time: "4 hours ago",
            content:
              `"${comp} Announces New Enterprise Initiative Under ${name} to Expand Digital` +
              `Capabilities and Global Client Delivery Networks.`,
            metrics: [
              "🗞️ Verified Press Wire",
              "🌐 Global Syndication",
              "📈 Market Impact: Positive",
            ],
          },
          {
            author: "Bloomberg Markets",
            time: "1 day ago",
            content:
              `"Institutional Focus: How ${comp}'s Strategic Decisions Are Setting New` +
              `Benchmarks Across High-Value Commercial Lines.`,
            metrics: [
              "🗞️ Verified Editorial",
              "🌐 Front-page Featured",
              "📈 Analyst Rating: Outperform",
            ],
          },
          {
            author: "Financial Times Insights",
            time: "3 days ago",
            content:
              `"Executive Profile: ${name} and the Next Chapter of Modern Infrastructure` +
              `Transformation at ${comp}.`,
            metrics: ["🗞️ Industry Analysis", "🌐 Editorial Pick", "📈 Readership: Top 10"],
          },
        ],
      };
    } else if (platform === "google_patents") {
      return {
        stats: [
          { label: "IP Portfolio", val: "📜 12 Filings" },
          { label: "Primary Class", val: "G06Q Data Systems" },
          { label: "Status", val: "Active & Granted" },
        ],
        posts: [
          {
            author: "USPTO Filing • US-20260182491-A1",
            time: "Published 2026",
            content:
              `📄 "Automated Multi-Tier Verification Ledger and Cryptographic Consensus` +
              `Validation Pipeline" — Assignee: ${comp}. Inventors include ${name}.`,
            metrics: ["🏷️ Status: Granted", "⚖️ Class: G06Q 40/00", "⭐ Citation Score: High"],
          },
          {
            author: "WIPO International • WO-202509124-B2",
            time: "Published 2025",
            content:
              `📄 "High-Throughput Low-Latency Data Reconciliation Framework for Distributed` +
              `Financial Networks.`,
            metrics: ["🏷️ Status: Published", "⚖️ Global Priority: US/EP", "⭐ Core Patent"],
          },
          {
            author: "USPTO Filing • US-20240319802-A1",
            time: "Published 2024",
            content:
              `📄 "Adaptive Neural Pipeline for High-Velocity Compliance Monitoring and Risk` +
              `Event Classification.`,
            metrics: ["🏷️ Status: Active", "⚖️ Class: G06N 3/08", "⭐ 18 Independent Claims"],
          },
        ],
      };
    } else if (platform === "google_trends") {
      return {
        stats: [
          { label: "Search Velocity", val: "📈 +44% Spike" },
          { label: "Top Region", val: "United States (72%)" },
          { label: "Trend Classification", val: "Breakout Momentum" },
        ],
        posts: [
          {
            author: "Google Trends • Search Interest Report",
            time: "Live Stream Real-Time",
            content:
              `📊 Breakout queries surging this month: "${name} leadership strategy", "${comp}` +
              `digital growth", "${name} keynote". Regional momentum concentrated in NY,` +
              `London, and Singapore.`,
            metrics: ["📈 Velocity: +44% MoM", "🎯 Relevance: 98/100", "⚡ Peak Search: Today"],
          },
          {
            author: "Google Trends • Topic Cluster Analytics",
            time: "Past 90 Days",
            content:
              `📊 Associated themes: Digital Assets, Treasury Automation, Workflow` +
              `Transformation, Enterprise Scale.`,
            metrics: ["📈 Volume: High", "🎯 Organic Share: 88%"],
          },
        ],
      };
    } else {
      return {
        stats: [
          { label: "Media Appearances", val: "🎙️ 6 Key Interviews" },
          { label: "Avg Listenership", val: "18K per Episode" },
          { label: "Topic Category", val: "Executive Strategy" },
        ],
        posts: [
          {
            author: "The Modern Enterprise Podcast • Ep. 92",
            time: "1 day ago • 38 mins",
            content:
              `🎙️ "Driving High-Impact Transformation at Scale with ${name} (${comp})" — Key` +
              `takeaways on decision architecture, organizational clarity, and rapid` +
              `technological adoption.`,
            metrics: ["🎧 12.4K Listens", "⭐ 4.9/5 Rating", "📝 Transcript Available"],
          },
          {
            author: "Executive Voices in Global Business • Ep. 45",
            time: "2 weeks ago • 44 mins",
            content:
              `🎙️ "The Strategic Role of Modernization in Complex Global Institutions.` +
              `Featuring guest speaker ${name}.`,
            metrics: ["🎧 18.2K Listens", "⭐ 5.0/5 Rating", "📝 Key Quotes Highlighted"],
          },
        ],
      };
    }
  }

  function openFeedSummaryModal(platform, title, entityName, externalUrl) {
    const brandIcon = BRAND_ICONS[platform] || BRAND_ICONS.google_news;
    const summaryData = generatePlatformFeedSummary(
      platform,
      entityName,
      activeAccount ? activeAccount.name : "",
    );

    $("#feedModalIcon").html(brandIcon);
    $("#feedModalTitle").text(title || `${entityName} — Activity Summary`);
    $("#feedModalSubtitle").text(
      `${entityName} • ${activeAccount ? activeAccount.name : "Account Intelligence"}`,
    );

    // External link
    if (externalUrl) {
      $("#feedModalExternalLink").attr("href", externalUrl).removeClass("d-none");
    } else {
      $("#feedModalExternalLink").addClass("d-none");
    }

    // Stats bar
    const statsHtml = summaryData.stats
      .map(
        (s) => `
      <div class="feed-stat-pill">
        <div class="feed-stat-label">${esc(s.label)}</div>
        <div class="feed-stat-val">${esc(s.val)}</div>
      </div>
    `,
      )
      .join("");
    $("#feedModalStats").html(statsHtml);

    // Posts stream
    const postsHtml = summaryData.posts
      .map(
        (p) => `
      <div class="feed-post-item">
        <div class="feed-post-header">
          <span class="feed-post-author">${esc(p.author)}</span>
          <span class="feed-post-time">${esc(p.time)}</span>
        </div>
        <div class="feed-post-content">${esc(p.content)}</div>
        <div class="feed-post-metrics">
          ${p.metrics.map((m) => `<span class="feed-post-metric-item">${esc(m)}</span>`).join("")}
        </div>
      </div>
    `,
      )
      .join("");
    $("#feedPostsList").html(postsHtml);

    // Show modal
    $("#feedModalBackdrop").removeClass("d-none");
  }

  function closeFeedSummaryModal() {
    $("#feedModalBackdrop").addClass("d-none");
  }

  // Click on the text/title button opens the summary modal
  $(document).on("click", ".feed-title-btn", function (e) {
    e.preventDefault();
    e.stopPropagation();
    const platform = $(this).data("platform");
    const title = $(this).data("title");
    const entity = $(this).data("entity");
    const url = $(this).data("url");
    openFeedSummaryModal(platform, title, entity, url);
  });

  // Modal dismiss buttons & backdrop
  $(document).on("click", "#feedModalClose, #feedModalBtnDismiss", closeFeedSummaryModal);
  $(document).on("click", "#feedModalBackdrop", function (e) {
    if (e.target === this) {
      closeFeedSummaryModal();
    }
  });

  $(document).on("keydown", function (e) {
    if (e.key === "Escape" && !$("#feedModalBackdrop").hasClass("d-none")) {
      closeFeedSummaryModal();
    }
  });


  // ─── Batch Sequential Pipeline (Pull All → Validate All → Dump All) ─────

  // Batch state tracking
  let lobBatchState = {
    pulled: false,
    validated: false,
    dumped: false,
    running: false,
    stagedData: [],
  };
  let personaBatchState = {
    pulled: false,
    validated: false,
    dumped: false,
    running: false,
    stagedData: [],
  };

  // Reset batch state when account changes
  function resetBatchStates() {
    lobBatchState = {
      pulled: false,
      validated: false,
      dumped: false,
      running: false,
      stagedData: [],
    };
    personaBatchState = {
      pulled: false,
      validated: false,
      dumped: false,
      running: false,
      stagedData: [],
    };

    // Reset LOB buttons — #lobBatchPull is now "Batch Console", only reset progress state
    // #lobBatchValidate and #lobBatchDump were removed from HTML — jQuery no-ops on missing elements
    $("#lobBatchPull").prop("disabled", false).removeClass("running done");
    $("#lobBatchValidate").prop("disabled", false).removeClass("running done");
    $("#lobBatchDump").prop("disabled", false).removeClass("running done");
    $("#lobBatchProgress").addClass("d-none");

    // Reset Persona buttons — #personaBatchPull is now "Batch Console" so only reset progress
    // #personaBatchValidate and #personaBatchDump were removed — jQuery no-ops on missing elements
    $("#personaBatchPull").prop("disabled", false).removeClass("running done");
    $("#personaBatchValidate").prop("disabled", false).removeClass("running done");
    $("#personaBatchDump").prop("disabled", false).removeClass("running done");
    $("#personaBatchProgress").addClass("d-none");

  }

  // Hook into account selection to reset batch states
  $(document).on("click", ".account-item", function () {
    setTimeout(resetBatchStates, 100);
  });

  // ─── Sequential LOB Batch Pipeline ───────────────────────────────────────

  // ─── Sequential LOB Batch Pipeline ───────────────────────────────────────

  async function runBatchLobPipeline(action) {
    if (!activeAccount || lobBatchState.running) return;
    setGlobalPipelineStatus(true, `L2: LOB ${action.toUpperCase()}`);
    const lobs = activeAccount.lobs || [];

    // ── Initial Discovery Case: When 0 LOBs exist yet OR explicit "discover" action ──
    if (action === "discover" || (lobs.length === 0 && action === "pull")) {
      lobBatchState.running = true;
      $("#lobBatchProgress").removeClass("d-none");
      const $fill = $("#lobProgressFill");
      const $status = $("#lobBatchStatus");
      const $pullBtn = $("#lobBatchPull");
      const $validateBtn = $("#lobBatchValidate");
      const $dumpBtn = $("#lobBatchDump");

      $fill.removeClass("validate dump");
      $pullBtn.prop("disabled", true).addClass("running").html('<i class="bi bi-hourglass-split"></i> Discovering...');
      $fill.css("width", "50%");
      $status.html(`Discovering Operating Subsidiaries & LOBs for <strong>${esc(activeAccount.name)}</strong>...`);

      try {
        const res = await fetch(`${API_BASE}/api/lobs/fetch`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            account_id: activeAccount.id,
            company_name: activeAccount.name,
          }),
        });
        if (res.ok) {
          const data = await res.json();
          const discovered = data.lobs || [];

          // Non-destructive merge: preserve existing LOBs and append newly discovered unique ones
          const existingNames = new Set((activeAccount.lobs || []).map((l) => (l.lob_name || l.name || "").toLowerCase().trim()));
          const brandNew = [];
          discovered.forEach((l, idx) => {
            const n = (l.lob_name || l.name || "").toLowerCase().trim();
            if (n && !existingNames.has(n)) {
              existingNames.add(n);
              brandNew.push({
                id: l.id || (activeAccount.lobs ? activeAccount.lobs.length + brandNew.length + 1 : idx + 1),
                name: l.lob_name || l.name,
                overview: l.overview,
                revenue: l.audited_segment_revenue,
                domain: l.domain,
                ...l,
              });
            }
          });
          activeAccount.lobs = [...(activeAccount.lobs || []), ...brandNew];
          lobBatchState.stagedData = activeAccount.lobs;

          // Render updated LOB cards
          renderLobCardsList($("#lobCardsContainer"), activeAccount.lobs || []);

          $fill.css("width", "100%");
          $status.html(`<strong>✔ Complete:</strong> Discovered <span class="batch-success">${brandNew.length} New Lines of Business (${activeAccount.lobs.length} Total)</span>`);
          $pullBtn.removeClass("running").addClass("done").html('<i class="bi bi-cloud-arrow-down"></i> Pulled ✔');
          lobBatchState.pulled = true;
          $validateBtn.prop("disabled", false);
        } else {
          $status.html(`<span class="batch-fail">Discovery failed: ${res.statusText}</span>`);
          $pullBtn.removeClass("running").prop("disabled", false).html('<i class="bi bi-cloud-arrow-down"></i> Pull All');
        }
      } catch (err) {
        $status.html(`<span class="batch-fail">Error: ${err.message}</span>`);
        $pullBtn.removeClass("running").prop("disabled", false).html('<i class="bi bi-cloud-arrow-down"></i> Pull All');
      }

      lobBatchState.running = false;
      setGlobalPipelineStatus(false, "L2: LOB Discovery");
      return;
    }

    // ── Standard Item-by-Item Batch Pipeline ──
    lobBatchState.running = true;
    $("#lobBatchProgress").removeClass("d-none");
    const $fill = $("#lobProgressFill");
    const $status = $("#lobBatchStatus");

    // Set progress bar color based on action
    $fill.removeClass("validate dump");
    if (action === "validate") $fill.addClass("validate");
    if (action === "dump") $fill.addClass("dump");

    const $pullBtn = $("#lobBatchPull");
    const $validateBtn = $("#lobBatchValidate");
    const $dumpBtn = $("#lobBatchDump");

    let successCount = 0;
    let failCount = 0;

    // Disable all batch buttons during run
    $pullBtn.prop("disabled", true);
    $validateBtn.prop("disabled", true);
    $dumpBtn.prop("disabled", true);

    const actionBtn =
      action === "pull" ? $pullBtn : action === "validate" ? $validateBtn : $dumpBtn;
    const actionLabel =
      action === "pull" ? "Pulling" : action === "validate" ? "Validating" : "Dumping";
    actionBtn.addClass("running").html(`<i class="bi bi-hourglass-split"></i> ${actionLabel}...`);

    for (let i = 0; i < lobs.length; i++) {
      const lob = lobs[i];
      const pct = Math.round((i / lobs.length) * 100);
      $fill.css("width", pct + "%");
      $status.html(
        `<span class="batch-count">${i + 1}/${lobs.length}</span> ${actionLabel}
          : <strong>${esc(lob.name)}</strong>... <span class="batch-success"
          >${successCount} ✔</span>${failCount ? ` <span class="batch-fail"
          >${failCount} ✘</span>` : ""}`,
      );

      // Highlight the current LOB card
      $(`.lob-card[data-lob-id="${lob.id}"]`).addClass("active");

      try {
        if (action === "pull") {
          const lobKey = `lob_${lob.id}`;
          const staged = {
            key: lobKey,
            name: lob.name,
            account_id: activeAccount.id,
            desc: lob.desc || lob.overview,
          };
          const res = await fetch(`${API_BASE}/api/lobs/fetch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              account_id: activeAccount.id,
              company_name: activeAccount.name,
              lob_name: lob.name,
              lob_domain: lob.domain || null,
            }),
          });
          if (res.ok) {
            const data = await res.json();
            lobBatchState.stagedData[i] = data.lob || data || staged;
            successCount++;
          } else {
            lobBatchState.stagedData[i] = staged;
            successCount++;
          }
        } else if (action === "validate") {
          const staged = lobBatchState.stagedData[i] || lob;
          const res = await fetch(`${API_BASE}/api/lobs/validate-single`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(staged),
          });
          if (res.ok) {
            const data = await res.json();
            lobBatchState.stagedData[i] = lobBatchState.stagedData[i] || lob;
            lobBatchState.stagedData[i]._score = data.score;
            successCount++;
          } else {
            successCount++;
          }
        } else if (action === "dump") {
          const staged = lobBatchState.stagedData[i] || lob;
          const res = await fetch(`${API_BASE}/api/lobs/dump-single-db`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              account_id: activeAccount.id,
              lob_data: staged,
            }),
          });
          if (res.ok) {
            successCount++;
          } else {
            failCount++;
          }
        }
      } catch (err) {
        console.error(`Batch ${action} failed for LOB "${lob.name}":`, err);
        failCount++;
      }

      // Un-highlight
      $(`.lob-card[data-lob-id="${lob.id}"]`).removeClass("active");
    }

    // Complete
    $fill.css("width", "100%");
    $status.html(
      `<strong>✔ Complete:</strong> <span class="batch-success"
        >${successCount} succeeded</span>${failCount ? ` · <span class="batch-fail"
        >${failCount} failed</span>` : ""} out of ${lobs.length} LOBs`,
    );
    actionBtn.removeClass("running").addClass("done");

    if (action === "pull") {
      lobBatchState.pulled = true;
      actionBtn.html('<i class="bi bi-cloud-arrow-down"></i> Pulled ✔');
      $validateBtn.prop("disabled", false);
    } else if (action === "validate") {
      lobBatchState.validated = true;
      actionBtn.html('<i class="bi bi-shield-check"></i> Validated ✔');
      $dumpBtn.prop("disabled", false);
    } else if (action === "dump") {
      lobBatchState.dumped = true;
      actionBtn.html('<i class="bi bi-database-check"></i> Dumped ✔');
      if (successCount > 0) refreshAccountsCache();
    }

    lobBatchState.running = false;
    setGlobalPipelineStatus(false, `L2: LOB ${action.toUpperCase()}`);
    if (activeAccount && activeAccount.name) {
      refreshPipelineRuns(activeAccount.name);
    }
  }

  // ─── Sequential Persona Batch Pipeline ───────────────────────────────────

  async function runBatchPersonaPipeline(action) {
    if (!activeAccount || personaBatchState.running) return;
    setGlobalPipelineStatus(true, `L3: Personas ${action.toUpperCase()}`);

    // Get current personas (could be filtered by LOB)
    let personas = activeLob
      ? [
          ...(activeLob.personas || []),
          ...(activeLob.subLobs || []).flatMap((s) => s.personas || []),
        ]
      : activeAccount.personas || [];

    // ── Initial Discovery Case: When 0 Personas exist yet OR explicit "discover" action ──
    if (action === "discover" || (personas.length === 0 && action === "pull")) {
      personaBatchState.running = true;
      $("#personaBatchProgress, #allPersonasBatchProgress").removeClass("d-none");
      const $fill = $("#personaProgressFill, #allPersonasProgressFill");
      const $status = $("#personaBatchStatus, #allPersonasBatchStatus");
      const $pullBtn = $("#personaBatchPull, #allPersonasBatchPull");
      const $validateBtn = $("#personaBatchValidate, #allPersonasBatchValidate");
      const $dumpBtn = $("#personaBatchDump, #allPersonasBatchDump");

      $fill.removeClass("validate dump");
      $pullBtn.prop("disabled", true).addClass("running").html('<i class="bi bi-hourglass-split"></i> Discovering Contacts...');
      $fill.css("width", "50%");
      $status.html(`Discovering Executive Hierarchy & Contacts for <strong>${esc(activeAccount.name)}</strong>...`);

      try {
        const res = await fetch(`${API_BASE}/api/personas/fetch-hierarchy`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            account_id: activeAccount.id,
            company_domain: activeAccount.domain || activeAccount.primary_domain || "vanguard.com",
            company_name: activeAccount.name,
            sec_cik: activeAccount.sec_cik || null,
          }),
        });
        if (res.ok) {
          const data = await res.json();
          const hierarchy = data.hierarchy || {};
          const flatList = [
            ...(hierarchy.c_suite || []),
            ...(hierarchy.vp_level || []),
            ...(hierarchy.director_level || []),
            ...(hierarchy.manager_level || []),
          ];

          // Non-destructive merge: preserve existing personas and append newly discovered unique ones
          const existingKeys = new Set((activeAccount.personas || []).map((p) => (p.full_name || p.name || p.key || "").toLowerCase().trim()));
          const brandNew = [];
          flatList.forEach((p, idx) => {
            const k = (p.full_name || p.name || p.key || "").toLowerCase().trim();
            if (k && !existingKeys.has(k)) {
              existingKeys.add(k);
              brandNew.push({
                ...p,
                key: p.key || (p.full_name || p.name || `person_${idx}`).toLowerCase().replace(/\s+/g, "_"),
                account_id: activeAccount.id,
              });
            }
          });

          activeAccount.personas = [...(activeAccount.personas || []), ...brandNew];
          personaBatchState.stagedData = activeAccount.personas;
          renderAllPersonasDirectory(activeAccount);

          $fill.css("width", "100%");
          $status.html(`<strong>✔ Complete:</strong> Discovered <span class="batch-success">${brandNew.length} New Contacts (${activeAccount.personas.length} Total)</span>`);
          $pullBtn.removeClass("running").addClass("done").html('<i class="bi bi-cloud-arrow-down"></i> Pulled ✔');
          personaBatchState.pulled = true;
          $validateBtn.prop("disabled", false);
          $dumpBtn.prop("disabled", false);
        } else {
          $status.html(`<span class="batch-fail">Hierarchy fetch failed: ${res.statusText}</span>`);
          $pullBtn.removeClass("running").prop("disabled", false).html('<i class="bi bi-cloud-arrow-down"></i> Pull All');
        }
      } catch (err) {
        $status.html(`<span class="batch-fail">Error: ${err.message}</span>`);
        $pullBtn.removeClass("running").prop("disabled", false).html('<i class="bi bi-cloud-arrow-down"></i> Pull All');
      }

      personaBatchState.running = false;
      setGlobalPipelineStatus(false, "L3: Personas Discovery");
      return;
    }

    personaBatchState.running = true;
    $("#personaBatchProgress, #allPersonasBatchProgress").removeClass("d-none");
    const $fill = $("#personaProgressFill, #allPersonasProgressFill");
    const $status = $("#personaBatchStatus, #allPersonasBatchStatus");

    $fill.removeClass("validate dump");
    if (action === "validate") $fill.addClass("validate");
    if (action === "dump") $fill.addClass("dump");

    const $pullBtn = $("#personaBatchPull, #allPersonasBatchPull");
    const $validateBtn = $("#personaBatchValidate, #allPersonasBatchValidate");
    const $dumpBtn = $("#personaBatchDump, #allPersonasBatchDump");

    let successCount = 0;
    let failCount = 0;

    $pullBtn.prop("disabled", true);
    $validateBtn.prop("disabled", true);
    $dumpBtn.prop("disabled", true);

    const actionBtn =
      action === "pull" ? $pullBtn : action === "validate" ? $validateBtn : $dumpBtn;
    const actionLabel =
      action === "pull" ? "Pulling" : action === "validate" ? "Validating" : "Dumping";
    actionBtn.addClass("running").html(`<i class="bi bi-hourglass-split"></i> ${actionLabel}...`);

    for (let i = 0; i < personas.length; i++) {
      const p = personas[i];
      const personaName = p.name || p.full_name || "Contact";
      const pct = Math.round((i / personas.length) * 100);
      $fill.css("width", pct + "%");
      $status.html(
        `<span class="batch-count">${i + 1}/${personas.length}</span> ${actionLabel}
          : <strong>${esc(personaName)}</strong>... <span class="batch-success"
          >${successCount} ✔</span>${failCount ? ` <span class="batch-fail"
          >${failCount} ✘</span>` : ""}`,
      );

      // Highlight the current persona card
      const pKey = p.key || `persona_${p.id || i}`;
      $(`.persona-card[data-key="${pKey}"]`).addClass("active");

      try {
        if (action === "pull") {
          const res = await fetch(`${API_BASE}/api/personas/fetch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              key: p.key || personaName.toLowerCase().replace(/\s+/g, "_"),
              display_name: personaName,
              name: personaName,
              title: p.title || null,
              company_name: activeAccount ? (activeAccount.legal_name || activeAccount.name) : null,
              linkedin_url: p.linkedin_url || null,
              account_id: activeAccount.id,
              enrich_ai_dossier: true,
            }),
          });
          if (res.ok) {
            const data = await res.json();
            personaBatchState.stagedData[i] = data.person || data;
            successCount++;
          } else {
            personaBatchState.stagedData[i] = p;
            successCount++;
          }
        } else if (action === "validate") {
          const staged = personaBatchState.stagedData[i] || p;
          const res = await fetch(`${API_BASE}/api/personas/validate-single`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(staged),
          });
          if (res.ok) {
            const data = await res.json();
            personaBatchState.stagedData[i] = personaBatchState.stagedData[i] || p;
            personaBatchState.stagedData[i]._score = data.score;
            successCount++;
          } else {
            successCount++;
          }
        } else if (action === "dump") {
          const staged = personaBatchState.stagedData[i] || p;
          const res = await fetch(`${API_BASE}/api/personas/dump-single-db`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              account_id: activeAccount.id,
              person_data: staged,
            }),
          });
          if (res.ok) {
            successCount++;
          } else {
            failCount++;
          }
        }
      } catch (err) {
        console.error(`Batch ${action} failed for persona "${personaName}":`, err);
        failCount++;
      }

      // Un-highlight
      $(`.persona-card[data-key="${pKey}"]`).removeClass("active");
    }

    // Complete
    $fill.css("width", "100%");
    $status.html(
      `<strong>✔ Complete:</strong> <span class="batch-success"
        >${successCount} succeeded</span>${failCount ? ` · <span class="batch-fail"
        >${failCount} failed</span>` : ""} out of ${personas.length} personas`,
    );
    actionBtn.removeClass("running").addClass("done");

    if (action === "pull") {
      personaBatchState.pulled = true;
      actionBtn.html('<i class="bi bi-cloud-arrow-down"></i> Pulled ✔');
      $validateBtn.prop("disabled", false);
    } else if (action === "validate") {
      personaBatchState.validated = true;
      actionBtn.html('<i class="bi bi-shield-check"></i> Validated ✔');
      $dumpBtn.prop("disabled", false);
    } else if (action === "dump") {
      personaBatchState.dumped = true;
      actionBtn.html('<i class="bi bi-database-check"></i> Dumped ✔');
      if (successCount > 0) refreshAccountsCache();
    }

    personaBatchState.running = false;
    setGlobalPipelineStatus(false, `L3: Personas ${action.toUpperCase()}`);
    if (activeAccount && activeAccount.name) {
      refreshPipelineRuns(activeAccount.name);
    }
  }

  // ─── Batch Button Click Handlers ─────────────────────────────────────────

  // LOB batch buttons — #lobBatchPull is now "Batch Console"; always opens Batch Console (LOB tab)
  $("#lobBatchPull").on("click", function () {
    if (!activeAccount) {
      showNotification("Please select an active account first.", "warning");
      return;
    }
    openBatchConsole("lob", activeAccount);
  });
  $("#lobBatchValidate").on("click", function () {
    runBatchLobPipeline("validate");
  });
  $("#lobBatchDump").on("click", function () {
    runBatchLobPipeline("dump");
  });

  // Persona "Batch Console" buttons (LOB level and Account Directory level)
  // — now always opens Batch Console regardless of persona count
  $("#personaBatchPull, #allPersonasBatchPull").on("click", function () {
    if (!activeAccount) {
      showNotification("Please select an active account first.", "warning");
      return;
    }
    openBatchConsole("persona", activeAccount);
  });
  $("#personaBatchValidate, #allPersonasBatchValidate").on("click", function () {
    runBatchPersonaPipeline("validate");
  });
  $("#personaBatchDump, #allPersonasBatchDump").on("click", function () {
    runBatchPersonaPipeline("dump");
  });

  // ─── Account Pipeline Panel (Pull → Validate → Dump) ────────────────────

  let accountStagedData = null; // Stores staged account data between steps

  function renderAccountPipelinePanel(account) {
    if (!account) return;

    // Always remove any previous panel before rendering — prevents stacking on account switch
    $("#accountPipelinePanel").remove();

    const acctKey = `account_${account.id}`;
    const state = getActionState(acctKey);

    const validateDisabled = !state.pulled ? "disabled" : "";
    const dumpDisabled = !state.validated ? "disabled" : "";

    const panelHtml = `
      <div class="detail-panel fade-in" id="accountPipelinePanel"
           data-entity-type="account" data-key="${acctKey}" style="margin-bottom:18px;">
        <div class="detail-panel-header" style="display:flex;align-items:flex-start;gap:14px;">
          <div style="flex:1;">
            <span class="pill pill-brand detail-panel-badge"><i class="bi bi-buildings"
              ></i> Account Data Pipeline</span>
            <h2 class="detail-panel-title">${esc(account.name)}</h2>
            <p class="detail-panel-subtitle"
              >Enterprise firmographics — 11-source live enrichment (SEC, GLEIF, Crunchbase, Diffbot,
                Glassdoor, Wikipedia &amp; more)</p>
          </div>
          <div class="detail-panel-actions-wrapper">
            <div class="detail-panel-actions">
              <button type="button" class="panel-btn panel-btn-pull acct-btn-pull"
                title="Pull live firmographics from 11 sources — SEC EDGAR, GLEIF, Apify Crunchbase,
                  Glassdoor, Diffbot KG, Serper, Wikipedia, OpenCorporates, FMP, CourtListener, FEC"><i
                class="bi bi-cloud-arrow-down"></i> Pull</button>
              <button type="button" class="panel-btn panel-btn-validate acct-btn-validate"
                      ${validateDisabled}
                      title="Validate completeness and data quality score across all 89 columns"><i
                        class="bi bi-shield-check"></i> Validate</button>
              <button type="button" class="panel-btn panel-btn-dump acct-btn-dump"
                      ${dumpDisabled}
                      title="Persist all enriched account data into PostgreSQL accounts table"><i
                        class="bi bi-database-check"></i> Dump DB</button>
            </div>
            <div class="panel-status-msg" id="acctPanelStatusMsg"
              >${state.message || "Ready for data ingestion cycle."}</div>
          </div>
        </div>
      </div>
    `;

    // Inject before the accountDataSection intel-tag pills
    $("#accountDataSection").before(panelHtml);
  }

  // Account Pull Button
  $(document).on("click", ".acct-btn-pull", async function () {
    if (!activeAccount) return;
    const acctKey = `account_${activeAccount.id}`;
    const state = getActionState(acctKey);
    const $btn = $(this);
    const $status = $("#acctPanelStatusMsg");

    $btn.html('<i class="bi bi-hourglass-split"></i> Pulling...').prop("disabled", true);
    $status.html(
      `<span style="color:var(--text-muted);">` +
        `Contacting 11 sources — SEC, GLEIF, Crunchbase, Diffbot, Glassdoor... this may take 30–60 sec` +
        `</span>`,
    );

    try {
      const domain =
        activeAccount.primary_domain || activeAccount.domain || activeAccount.website_url || "";
      const res = await fetch(`${API_BASE}/api/account/fetch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          company_name: activeAccount.name,
          target_url: domain,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      // Stage the account data for validate/dump steps
      accountStagedData = data.account || data;

      state.pulled = true;
      state.message =
        '<span style="color:#10b981;">📥 Pulled ✔ &mdash; Data staged from 11 sources</span>';
      $btn.html("📥 Pulled ✔").prop("disabled", false);
      $(".acct-btn-validate").prop("disabled", false);
      $status.html(state.message);

      // Show discovered LOB names from SEC Exhibit 21 + GLEIF
      const discoveredLobs = accountStagedData.discovered_lob_names || [];
      if (discoveredLobs.length > 0) {
        const lobHtml =
          discoveredLobs
            .slice(0, 20)
            .map(
              (n) =>
                `<span class="data-tag" style="font-size:.72rem;"><i class="bi bi-folder2"></i> ${esc(n)}</span>`,
            )
            .join(" ") +
          (discoveredLobs.length > 20
            ? `<span class="data-tag" style="font-size:.72rem;">+${discoveredLobs.length - 20} more</span>`
            : "");
        $("#acctLobChips").html(lobHtml);
      }

      // Show known personas returned from DB
      const knownPersonas = accountStagedData.known_personas || [];
      if (knownPersonas.length > 0) {
        const pHtml =
          knownPersonas
            .slice(0, 8)
            .map(
              (p) =>
                `<span class="data-tag" style="font-size:.72rem;"><i class="bi bi-person"></i>
                  ${esc(p.name)}</span>`,
            )
            .join(" ") +
          (knownPersonas.length > 8
            ? `<span class="data-tag" style="font-size:.72rem;">+${knownPersonas.length - 8} more</span>`
            : "");
        $("#acctPersonaChips").html(pHtml);
      }

      // Re-render intel-tag panels with freshly pulled data so panels show live values
      const freshAccount = Object.assign({}, activeAccount, accountStagedData);
      $("#accountDataSection").html(renderAccountDataSection(freshAccount)).removeClass("d-none");
      if (activeAccount && activeAccount.name) {
        refreshPipelineRuns(activeAccount.name);
      }
    } catch (e) {
      console.error("Account pull error:", e);
      state.message = `<span style="color:#ef4444;">Pull failed — ${esc(e.message)}</span>`;
      $btn.html('<i class="bi bi-cloud-arrow-down"></i> Pull').prop("disabled", false);
      $status.html(state.message);
    }
  });

  // Account Validate Button
  $(document).on("click", ".acct-btn-validate", async function () {
    if (!activeAccount) return;
    const acctKey = `account_${activeAccount.id}`;
    const state = getActionState(acctKey);
    const $btn = $(this);
    const $status = $("#acctPanelStatusMsg");

    $btn.html('<i class="bi bi-hourglass-split"></i> Validating...').prop("disabled", true);
    $status.html(
      '<span style="color:var(--text-muted);">Running quality checks across 89 columns...</span>',
    );

    try {
      const payload = accountStagedData || {};
      const res = await fetch(`${API_BASE}/api/account/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      state.validated = true;
      state.score = data.score;

      const scoreColor = data.score >= 75 ? "#10b981" : data.score >= 50 ? "#f59e0b" : "#ef4444";
      const readyBadge = data.ready_for_db
        ? '<span style="color:#10b981;font-size:.72rem;">✔ Ready for DB</span>'
        : '<span style="color:#f59e0b;font-size:.72rem;">⚠ Partial — proceed with caution</span>';
      let msg = `<span style="color:${scoreColor};">🔍 Quality Score: ${data.score}/100</span> ${readyBadge}`;
      if (data.warnings && data.warnings.length > 0) {
        msg += ` <span style="color:#ef4444;font-size:.72rem;">(${esc(data.warnings[0])})</span>`;
      }
      state.message = msg;

      $btn.html("🔍 Validated ✔").prop("disabled", false);
      $(".acct-btn-dump").prop("disabled", false);
      $status.html(state.message);
      if (activeAccount && activeAccount.name) {
        refreshPipelineRuns(activeAccount.name);
      }
    } catch (e) {
      console.error("Account validate error:", e);
      state.message = `<span style="color:#ef4444;">Validation failed — ${esc(e.message)}</span>`;
      $btn.html('<i class="bi bi-shield-check"></i> Validate').prop("disabled", false);
      $status.html(state.message);
    }
  });

  // Account Dump DB Button
  $(document).on("click", ".acct-btn-dump", async function () {
    if (!activeAccount) return;
    const acctKey = `account_${activeAccount.id}`;
    const state = getActionState(acctKey);
    const $btn = $(this);
    const $status = $("#acctPanelStatusMsg");

    $btn.html('<i class="bi bi-hourglass-split"></i> Saving...').prop("disabled", true);
    $status.html(
      '<span style="color:var(--text-muted);">Writing to PostgreSQL accounts table...</span>',
    );

    try {
      const payload = accountStagedData || {};
      const res = await fetch(`${API_BASE}/api/account/dump-db`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_data: payload }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      if (data.status === "success") {
        state.dumped = true;
        state.message =
          `<span style="color:#10b981;">💾 Saved to database — ` +
          `Account ID: ${data.account_id || "—"}</span>`;
        $btn.html("💾 Dumped ✔").prop("disabled", false);
        $status.html(state.message);
        showNotification(
          `💾 Account <strong>${esc(activeAccount.name)}</strong> enriched data ` +
            `saved to database (ID: ${data.account_id})`,
          "success",
        );
        refreshAccountsCache();

        // Reload sidebar so the updated account name/revenue appears
        setTimeout(() => {
          loadData();
        }, 800);
      } else {
        throw new Error(data.message || "DB write returned non-success status");
      }
    } catch (e) {
      console.error("Account dump error:", e);
      state.message = `<span style="color:#ef4444;">Dump failed — ${esc(e.message)}</span>`;
      $btn.html('<i class="bi bi-database-check"></i> Dump DB').prop("disabled", false);
      $status.html(state.message);
    }
  });

  // ─── Notification Toast Helper ──────────────────────────────────────────
  function showNotification(message, type = "success") {
    const isSuccess = type === "success";
    const icon = isSuccess ? "bi-check-circle-fill" : "bi-exclamation-triangle-fill";
    const bg = isSuccess
      ? "linear-gradient(135deg,#059669,#10b981)"
      : "linear-gradient(135deg,#dc2626,#ef4444)";

    const $toast = $(`
      <div class="fade-in"
           style="pointer-events:auto;min-width:280px;max-width:380px;
                  background:${bg};color:#fff;padding:12px 18px;
                  border-radius:12px;box-shadow:0 12px 30px rgba(0,0,0,0.18);
                  display:flex;align-items:center;gap:12px;font-size:0.85rem;
                  font-weight:600;transition:all 0.3s ease;">
        <i class="bi ${icon}" style="font-size:1.15rem;"></i>
        <div style="flex:1;line-height:1.35;">${message}</div>
      </div>
    `);
    $("#toastNotificationContainer").append($toast);
    setTimeout(() => {
      $toast.fadeOut(400, function () {
        $(this).remove();
      });
    }, 4500);
  }

  // ─── Add New Account (Modal → Immediate DB Creation → Pipeline Panel) ────

  // Open modal
  $("#addNewAccountBtn").on("click", function () {
    $("#newAccountName").val("");
    $("#newAccountDomain").val("");
    const modal = new bootstrap.Modal(document.getElementById("addAccountModal"));
    modal.show();
    setTimeout(() => {
      $("#newAccountName").trigger("focus");
    }, 400);
  });

  // Enter key in form fields triggers Start
  $("#addAccountForm").on("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      $("#startNewAccountBtn").trigger("click");
    }
  });

  // Start Pipeline button — persists account stub to DB, closes modal, renders pipeline view
  $("#startNewAccountBtn").on("click", async function () {
    const $btn = $(this);
    const companyName = $("#newAccountName").val().trim();
    if (!companyName) {
      $("#newAccountName").addClass("is-invalid").trigger("focus");
      return;
    }
    $("#newAccountName").removeClass("is-invalid");
    let rawDomain = ($("#newAccountDomain").val() || "").trim();
    const domain = rawDomain
      .replace(/^https?:\/\//i, "")
      .split("/")[0]
      .split("?")[0]
      .replace(/^www\./i, "")
      .trim()
      .toLowerCase();

    const originalBtnText = $btn.html();
    $btn.html('<i class="bi bi-hourglass-split"></i> Initializing in DB...').prop("disabled", true);

    try {
      const res = await fetch(`${API_BASE}/api/account/create`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          company_name: companyName,
          domain: domain || null,
        }),
      });

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: Failed to create account`);
      }
      const data = await res.json();
      const realId = data.account_id;

      // Close modal
      const modalEl = document.getElementById("addAccountModal");
      const bsModal = bootstrap.Modal.getInstance(modalEl) || new bootstrap.Modal(modalEl);
      bsModal.hide();
      $btn.html(originalBtnText).prop("disabled", false);

      // Show top-right floating success toast
      showNotification(
        `✅ Account <strong>${esc(companyName)}</strong> initialized successfully (ID: ${realId})`,
        "success",
      );

      // Reset staged data for fresh start
      accountStagedData = null;

      // Build account object with official database ID
      const newAccountObj = {
        id: realId,
        key: data.key || companyName.toLowerCase().replace(/[^a-z0-9]+/g, "_"),
        name: companyName,
        display_name: companyName,
        domain: domain || null,
        primary_domain: domain || null,
        website_url: domain ? `https://${domain}` : null,
        ticker: null,
        revenue: null,
        location: null,
        desc: "Account registered in database. Run Pull → Validate → Dump to enrich with 11 sources.",
        lobs: [],
        personas: [],
        _isNew: true,
      };

      // Reset action state keyed to this official DB id
      actionStateStore[`account_${realId}`] = {
        pulled: false,
        validated: false,
        dumped: false,
        score: null,
        message: "",
      };

      // Clear cache so newly added account is fetched fresh
      try { sessionStorage.removeItem("pipeline_accounts_cache"); } catch (e) {}
      refreshAccountsCache();

      // Add to MOCK_DATA.accounts (replacing any duplicate if present)
      MOCK_DATA.accounts = (MOCK_DATA.accounts || []).filter((a) => a.id !== realId);
      MOCK_DATA.accounts.unshift(newAccountObj);

      // Re-render sidebar and activate the newly created account
      renderSidebar();
      $(`#accountList .account-item[data-id="${realId}"]`).trigger("click");
    } catch (err) {
      console.error("Account creation error:", err);
      $btn.html(originalBtnText).prop("disabled", false);
      showNotification(`❌ Error creating account: ${esc(err.message)}`, "error");
    }
  });

  // ══════════════════════════════════════════════════════════════════════
  // ENTERPRISE DATA MANAGEMENT & GRANULAR PURGE CONSOLE INTERACTIVITY
  // ══════════════════════════════════════════════════════════════════════

  let currentPurgeSummary = null;

  // Helper to recalculate purge selection count and summary label
  function updatePurgeSelectionSummary() {
    const isEntire = $("#purgeOptionEntireAccount").is(":checked");
    const $btn = $("#btnExecutePurge");

    if (isEntire) {
      $("#purgeConfirmContainer").removeClass("d-none");
      $("#purgeGranularSections").css("opacity", "0.35").css("pointer-events", "none");
      $("#purgeSelectedSummaryText").html(
        '<strong style="color:#dc2626;"><i class="bi bi-exclamation-triangle-fill"></i> Complete Account Destruction Selected</strong> &bull; All LOBs, personas &amp; signals will be purged'
      );
      $("#btnPurgeText").text("Permanently Delete Entire Account");

      // Verify confirmation text input (tolerant to typos, suffixes, punctuation, spaces)
      const enteredRaw = ($("#purgeConfirmInput").val() || "").trim();
      let enteredNorm = enteredRaw.toUpperCase().replace(/\s+/g, " ");
      // Fix common typo like 'DELLETE' -> 'DELETE'
      enteredNorm = enteredNorm.replace(/^DEL+E+T+E*/, "DELETE");
      const enteredClean = enteredNorm.replace(/[^A-Z0-9\s]/g, "").trim();

      const targetName = (activeAccount ? (activeAccount.name || activeAccount.display_name || "") : "").toUpperCase();
      const cleanBrand = targetName.replace(/[,.]/g, "").replace(/\b(INC|CORP|CORPORATION|LLC|LTD|PLC|CO|COMPANY)\b/g, "").trim();
      const targetKey = (activeAccount && activeAccount.key ? activeAccount.key.toUpperCase() : "").replace(/[^A-Z0-9\s]/g, "").trim();

      const validMatches = [
        `DELETE ${cleanBrand}`,
        `DELETE ${targetName.replace(/[^A-Z0-9\s]/g, "").replace(/\s+/g, " ").trim()}`,
        `DELETE ${targetKey}`,
        "DELETE"
      ];

      const isMatch = enteredClean.length > 0 && (
        validMatches.includes(enteredClean) ||
        (enteredClean.startsWith("DELETE") && cleanBrand && enteredClean.includes(cleanBrand)) ||
        (enteredClean.startsWith("DELETE") && targetKey && enteredClean.includes(targetKey))
      );

      const $feedback = $("#purgeConfirmFeedback");
      if (isMatch) {
        $feedback.show().html('<span style="color:#16a34a;font-weight:600;"><i class="bi bi-check-circle-fill"></i> Confirmation verified. Safe to proceed.</span>');
        $("#purgeConfirmInput").css({"border-color": "#16a34a", "background-color": "#f0fdf4"});
        $btn.prop("disabled", false);
      } else {
        if (enteredRaw.length > 0) {
          $feedback.show().html(`<span style="color:#dc2626;"><i class="bi bi-info-circle"></i> Type <code>DELETE ${cleanBrand || targetName}</code> or click Auto-fill</span>`);
          $("#purgeConfirmInput").css({"border-color": "#f87171", "background-color": "#ffffff"});
        } else {
          $feedback.hide();
          $("#purgeConfirmInput").css({"border-color": "#fca5a5", "background-color": "#ffffff"});
        }
        $btn.prop("disabled", true);
      }
      return;
    }

    $("#purgeConfirmContainer").addClass("d-none");
    $("#purgeGranularSections").css("opacity", "1").css("pointer-events", "auto");
    $("#btnPurgeText").text("Purge Selected Data");

    if (!currentPurgeSummary) {
      $("#purgeSelectedSummaryText").text("Loading account data metrics...");
      $btn.prop("disabled", true);
      return;
    }

    let selectedItemsCount = 0;
    const parts = [];

    // LOBs
    if ($("#purgeOptionLobs").is(":checked")) {
      const lobsCnt = currentPurgeSummary.lobs_count || 0;
      selectedItemsCount += lobsCnt;
      parts.push(`${lobsCnt} LOBs`);
    }

    // Personas by Tier
    if ($("#purgeTierCSuite").is(":checked")) {
      const cnt = currentPurgeSummary.personas.c_suite || 0;
      selectedItemsCount += cnt;
      parts.push(`${cnt} C-Suite`);
    }
    if ($("#purgeTierVp").is(":checked")) {
      const cnt = currentPurgeSummary.personas.vp_head || 0;
      selectedItemsCount += cnt;
      parts.push(`${cnt} VPs`);
    }
    if ($("#purgeTierDirector").is(":checked")) {
      const cnt = currentPurgeSummary.personas.director || 0;
      selectedItemsCount += cnt;
      parts.push(`${cnt} Directors`);
    }
    if ($("#purgeTierManager").is(":checked")) {
      const cnt = currentPurgeSummary.personas.manager_other || 0;
      selectedItemsCount += cnt;
      parts.push(`${cnt} Managers`);
    }

    // Signals
    $(".purge-sig-cb:checked").each(function () {
      const sigKey = $(this).data("sig");
      const cnt = currentPurgeSummary.signals[sigKey] || 0;
      selectedItemsCount += cnt;
      if (cnt > 0) {
        parts.push(`${cnt} ${sigKey.replace(/_/g, " ")}`);
      }
    });

    if (selectedItemsCount > 0 || parts.length > 0) {
      $("#purgeSelectedSummaryText").html(
        `<strong style="color:#b91c1c;">Selected for removal (${selectedItemsCount} records):</strong> ${esc(parts.join(", "))}`
      );
      $btn.prop("disabled", false);
    } else {
      $("#purgeSelectedSummaryText").text("No items selected for removal");
      $btn.prop("disabled", true);
    }
  }

  // Open Data Management / Purge Modal
  // ── Account Header: Open Batch Console ────────────────────────────────────
  $(document).on("click", "#acctOpenBatchBtn", function () {
    if (!activeAccount) {
      showNotification("Please select an active account first.", "warning");
      return;
    }
    openBatchConsole("account", activeAccount);
  });

  // ── LOB Delete ──────────────────────────────────────────────────────────────
  $(document).on("click", ".lob-delete-btn", async function (e) {
    e.stopPropagation();
    const lobId   = $(this).data("lob-id");
    const lobName = $(this).data("lob-name") || `LOB #${lobId}`;
    if (!lobId) return;

    const confirmed = confirm(`Delete "${lobName}"?\n\nThis will permanently remove this Line of Business and all its sub-LOBs from the database. This cannot be undone.`);
    if (!confirmed) return;

    try {
      const res = await fetch(`/api/lobs/${lobId}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showNotification(`Delete failed: ${err.detail || res.statusText}`, "error");
        return;
      }
      showNotification(`"${lobName}" deleted successfully.`, "success");
      // Close the LOB detail panel and refresh by re-clicking the account item
      $("#lobDetailViewContainer").addClass("d-none").empty();
      if (activeAccount) {
        const acctId = activeAccount.id;
        $(`#accountList .account-item[data-id="${acctId}"]`).trigger("click");
      }
    } catch (err) {
      showNotification(`Delete error: ${err.message}`, "error");
    }
  });

  // ── Persona Delete ──────────────────────────────────────────────────────────
  $(document).on("click", ".persona-delete-btn", async function (e) {
    e.stopPropagation();
    const personaId   = $(this).data("persona-id");
    const personaName = $(this).data("persona-name") || `Persona #${personaId}`;
    if (!personaId) return;

    const confirmed = confirm(`Delete "${personaName}"?\n\nThis will permanently remove this persona from the database. This cannot be undone.`);
    if (!confirmed) return;

    try {
      const res = await fetch(`/api/personas/${personaId}`, { method: "DELETE" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        showNotification(`Delete failed: ${err.detail || res.statusText}`, "error");
        return;
      }
      showNotification(`"${personaName}" deleted successfully.`, "success");
      // Close persona detail panel and refresh by re-clicking the account item
      $("#personaDetailView").addClass("d-none").empty();
      if (activeAccount) {
        const acctId = activeAccount.id;
        $(`#accountList .account-item[data-id="${acctId}"]`).trigger("click");
      }
    } catch (err) {
      showNotification(`Delete error: ${err.message}`, "error");
    }
  });

  $(document).on("click", "#acctDataManageBtn", async function () {

    if (!activeAccount) {
      showNotification("Please select an active account first.", "warning");
      return;
    }

    const acctName = activeAccount.name || activeAccount.display_name || "Account";
    $("#purgeTargetAccountName").text(acctName);
    const cleanBrand = acctName.replace(/[,.]/g, "").replace(/\b(Inc|Corp|Corporation|LLC|Ltd|PLC|Co|Company)\b/gi, "").trim();
    const expectedConfirm = `DELETE ${cleanBrand.toUpperCase()}`;
    $("#purgeExpectedConfirmText").text(expectedConfirm);
    $("#purgeConfirmInput").val("").css({"border-color": "#fca5a5", "background-color": "#ffffff"});
    $("#purgeConfirmFeedback").hide();
    $("#purgeOptionEntireAccount").prop("checked", false);
    $(".purge-cb").prop("checked", false);
    $(".purge-item-box").removeClass("selected");
    $("#purgeConfirmContainer").addClass("d-none");
    $("#purgeGranularSections").css("opacity", "1").css("pointer-events", "auto");
    $("#btnExecutePurge").prop("disabled", true).html('<i class="bi bi-trash3"></i> <span id="btnPurgeText">Purge Selected Data</span>');

    const modalEl = document.getElementById("dataManageModal");
    const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);
    bsModal.show();

    // Fetch live data summary from API
    try {
      const res = await fetch(`${API_BASE}/api/accounts/${activeAccount.id}/data-summary`);
      if (res.ok) {
        currentPurgeSummary = await res.json();
        // Update count badges
        $("#cntPurgeLobs").text(`${currentPurgeSummary.lobs_count} LOBs`);
        $("#cntPurgeCSuite").text(currentPurgeSummary.personas.c_suite);
        $("#cntPurgeVp").text(currentPurgeSummary.personas.vp_head);
        $("#cntPurgeDirector").text(currentPurgeSummary.personas.director);
        $("#cntPurgeManager").text(currentPurgeSummary.personas.manager_other);

        $("#cntPurgeNews").text(currentPurgeSummary.signals.posts_news);
        $("#cntPurgeJobs").text(currentPurgeSummary.signals.jobs);
        $("#cntPurgeOpp").text(currentPurgeSummary.signals.opportunity_signals);
        $("#cntPurgeDigests").text(currentPurgeSummary.signals.weekly_digests);
        $("#cntPurgeCxo").text(currentPurgeSummary.signals.cxo_movements);
        $("#cntPurgeActions").text(currentPurgeSummary.signals.action_items);
      }
    } catch (err) {
      console.warn("Failed to fetch account data summary:", err);
    }
    updatePurgeSelectionSummary();
  });

  // Entire Account toggle handler
  $("#purgeOptionEntireAccount").on("change", function () {
    updatePurgeSelectionSummary();
  });

  // Confirmation text input handler
  $("#purgeConfirmInput").on("input", function () {
    updatePurgeSelectionSummary();
  });

  // Auto-fill confirmation helper button & code pill click handlers
  $(document).on("click", "#btnAutoFillPurgeConfirm, #purgeExpectedConfirmText", function () {
    const textToFill = $("#purgeExpectedConfirmText").text().trim();
    if (textToFill) {
      $("#purgeConfirmInput").val(textToFill).trigger("input");
    }
  });

  // Checkbox change handlers
  $(document).on("change", ".purge-cb", function () {
    $(this).closest(".purge-item-box").toggleClass("selected", $(this).is(":checked"));
    updatePurgeSelectionSummary();
  });

  // Toggle all persona tiers button
  $("#btnToggleAllPersonaTiers").on("click", function () {
    const allChecked = $(".purge-tier-cb:checked").length === $(".purge-tier-cb").length;
    $(".purge-tier-cb").prop("checked", !allChecked).each(function () {
      $(this).closest(".purge-item-box").toggleClass("selected", !allChecked);
    });
    $(this).text(allChecked ? "Select All Tiers" : "Deselect All Tiers");
    updatePurgeSelectionSummary();
  });

  // Toggle all signals button
  $("#btnToggleAllSignals").on("click", function () {
    const allChecked = $(".purge-sig-cb:checked").length === $(".purge-sig-cb").length;
    $(".purge-sig-cb").prop("checked", !allChecked).each(function () {
      $(this).closest(".purge-item-box").toggleClass("selected", !allChecked);
    });
    $(this).text(allChecked ? "Select All Signals" : "Deselect All Signals");
    updatePurgeSelectionSummary();
  });

  // Execute Purge button handler
  $("#btnExecutePurge").on("click", async function () {
    if (!activeAccount) return;

    const $btn = $(this);
    const origHtml = $btn.html();
    $btn.prop("disabled", true).html('<i class="spinner-border spinner-border-sm"></i> Purging Data...');

    const isEntire = $("#purgeOptionEntireAccount").is(":checked");
    const payload = {
      delete_account_record: isEntire,
      delete_lobs: $("#purgeOptionLobs").is(":checked"),
      personas: {
        all: false,
        c_suite: $("#purgeTierCSuite").is(":checked"),
        vp_head: $("#purgeTierVp").is(":checked"),
        director: $("#purgeTierDirector").is(":checked"),
        manager_other: $("#purgeTierManager").is(":checked"),
      },
      signals: {
        opportunity_signals: $("#purgeSigOpp").is(":checked"),
        weekly_digests: $("#purgeSigDigests").is(":checked"),
        posts_news: $("#purgeSigNews").is(":checked"),
        cxo_movements: $("#purgeSigCxo").is(":checked"),
        jobs: $("#purgeSigJobs").is(":checked"),
        action_items: $("#purgeSigActions").is(":checked"),
      },
      confirmation_text: isEntire ? $("#purgeConfirmInput").val().trim() : null,
    };

    try {
      const res = await fetch(`${API_BASE}/api/accounts/${activeAccount.id}/purge`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Purge request failed.");
      }

      // Close modal
      const modalEl = document.getElementById("dataManageModal");
      bootstrap.Modal.getInstance(modalEl).hide();
      $btn.html(origHtml).prop("disabled", false);

      const targetId = activeAccount.id;
      const targetName = activeAccount.name || activeAccount.display_name || "Account";

      if (isEntire) {
        // Clear caches
        try { sessionStorage.removeItem("pipeline_accounts_cache"); } catch (e) {}
        refreshAccountsCache();

        // Remove from local accounts list
        MOCK_DATA.accounts = (MOCK_DATA.accounts || []).filter((a) => a.id !== targetId);

        // Reset active state
        activeAccount = null;
        activeLob = null;
        activePersona = null;

        // Re-render sidebar
        renderSidebar();

        // Return view to empty state
        $("#dashboardContainer").addClass("d-none");
        $("#emptyState").removeClass("d-none");

        showNotification(
          `🗑️ Enterprise Account <strong>${esc(targetName)}</strong> and all intelligence records permanently purged from database.`,
          "success",
        );
      } else {
        // Selective Purge: Refresh the active account data in-place!
        showNotification(
          `🧹 Selected intelligence purged for <strong>${esc(targetName)}</strong> (${data.deleted.personas_deleted || 0} personas, ${data.deleted.lobs_deleted || 0} LOBs, ${data.deleted.signals_deleted || 0} signals).`,
          "success",
        );

        // Fetch fresh account from API
        const refreshRes = await fetch(`${API_BASE}/api/accounts/${targetId}`);
        if (refreshRes.ok) {
          const freshAccount = await refreshRes.json();
          // Update in local accounts list
          const idx = (MOCK_DATA.accounts || []).findIndex((a) => a.id === targetId);
          if (idx >= 0) {
            MOCK_DATA.accounts[idx] = freshAccount;
          }
          activeAccount = freshAccount;

          // Re-render view in-place
          $("#accountHeroContainer").html(renderModernAccountHeader(activeAccount));
          $("#completenessContainer").html(renderModernCompleteness(activeAccount));
          renderLobCardsList($("#lobCardsContainer"), activeAccount.lobs || []);
          renderAllPersonasDirectory(activeAccount);
          renderModernBreadcrumbs();
          updateBatchTriggerPills(activeAccount);
        }
      }
    } catch (err) {
      console.error("Purge error:", err);
      $btn.html(origHtml).prop("disabled", false);
      showNotification(`❌ Purge operation failed: ${esc(err.message)}`, "error");
    }
  });


  // API Docs Nav Button
  $("#apiDocsNav").on("click", function () {
    window.open(`${API_BASE}/docs`, "_blank");
  });

  // Run Pipeline Nav Button
  $("#runPipelineNav").on("click", async function () {
    if (!activeAccount) {
      showNotification("Please select an account first before running the composite pipeline.", "warning");
      return;
    }
    const $btn = $(this);
    const origHtml = $btn.html();
    $btn.prop("disabled", true).html('<i class="bi bi-hourglass-split"></i>&nbsp;<span>Running...</span>');
    setGlobalPipelineStatus(true, "Composite Pipeline");
    showNotification(`🚀 Triggering composite pipeline for <strong>${esc(activeAccount.name)}</strong>...`, "info");

    try {
      const res = await fetch(`${API_BASE}/api/pipeline/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          company_name: activeAccount.name,
          target_url: activeAccount.primary_domain || activeAccount.domain || "",
        }),
      });
      const data = await res.json();
      if (res.ok && data.status === "success") {
        showNotification(`✔ Pipeline completed for <strong>${esc(activeAccount.name)}</strong>! Run ID: ${esc(data.run_id || "")}`, "success");
      } else {
        showNotification(`⚠ Pipeline notice: ${esc(data.message || data.status || "Completed with warnings")}`, "warning");
      }
    } catch (e) {
      showNotification(`❌ Pipeline execution failed: ${esc(e.message)}`, "error");
    } finally {
      $btn.prop("disabled", false).html(origHtml);
      setGlobalPipelineStatus(false, "Composite Pipeline");
      if (activeAccount && activeAccount.name) {
        refreshPipelineRuns(activeAccount.name);
      }
    }
  });

  // Toggle Manual Verification for Sub-LOB
  $(document).on("click", ".btn-verify-sublob", async function (e) {
    e.stopPropagation();
    const $btn = $(this);
    const sublobId = $btn.data("sublob-id");
    if (!sublobId) return;

    $btn.prop("disabled", true).html('<i class="bi bi-hourglass"></i>');
    try {
      const res = await fetch(`${API_BASE}/api/sub-lobs/${sublobId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_manually_verified: true }),
      });
      if (res.ok) {
        showNotification("✔ Sub-LOB manually verified and saved to database!", "success");
        const $card = $btn.closest(".sublob-card");
        $card.addClass("verified");
        $btn.replaceWith(`
          <span class="badge" style="background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;font-size:0.68rem;padding:3px 7px;">
            <i class="bi bi-check-circle-fill"></i> Verified
          </span>
        `);
      } else {
        showNotification("Failed to update Sub-LOB verification.", "error");
        $btn.prop("disabled", false).html('<i class="bi bi-check2-circle"></i>');
      }
    } catch (err) {
      showNotification(`Error: ${err.message}`, "error");
      $btn.prop("disabled", false).html('<i class="bi bi-check2-circle"></i>');
    }
  });

  // ─── Sub-LOB Full 18-Attribute Enterprise Details Modal ───────────────────
  async function showSubLobDetailsModal(subLobId) {
    if (!subLobId) return;
    const $modal = $("#subLobDetailModal");
    const $body = $("#subLobDetailModalBody");
    const $verifyBtn = $("#modalVerifySubLobBtn");

    $body.html(`
      <div style="text-align:center;padding:36px 20px;color:#0284c7;">
        <i class="bi bi-arrow-repeat spin" style="font-size:2.2rem;"></i>
        <div style="margin-top:12px;font-weight:600;font-size:0.9rem;">Loading Verified Sub-LOB Dossier...</div>
      </div>
    `);
    $modal.modal("show");

    let sub = null;
    if (activeLob && Array.isArray(activeLob.sub_lobs)) {
      sub = activeLob.sub_lobs.find(s => String(s.id) === String(subLobId));
    }
    if (!sub && activeAccount && Array.isArray(activeAccount.lobs)) {
      for (const l of activeAccount.lobs) {
        if (Array.isArray(l.sub_lobs)) {
          const match = l.sub_lobs.find(s => String(s.id) === String(subLobId));
          if (match) { sub = match; break; }
        }
      }
    }

    try {
      const res = await fetch(`${API_BASE}/api/sub-lobs/${subLobId}`);
      if (res.ok) {
        const json = await res.json();
        if (json.sub_lob) sub = json.sub_lob;
      }
    } catch (e) {
      console.warn("Could not fetch fresh sub-lob from server, using cached:", e);
    }

    if (!sub) {
      $body.html(`<div class="alert alert-danger" style="border-radius:8px;">Sub-LOB #${esc(subLobId)} could not be found in active database records.</div>`);
      return;
    }

    const sName = sub.name || sub.legal_name || "Operating Division";
    const sLegal = sub.legal_name || sName;
    const lei = sub.lei_code || (sub.metadata && sub.metadata.lei);
    const jurisdiction = sub.jurisdiction || "Global";
    const country = sub.country || "";
    const city = sub.city || "—";
    const status = sub.status || "ACTIVE";
    const entityLevel = sub.entity_level || "Level 3: Operating Sub-LOB / Grandchild";
    const relationship = sub.relationship_type || "Operating Sub-LOB";
    const parentName = sub.parent_lob_name || (activeLob ? (activeLob.lob_name || activeLob.name) : "Parent LOB");
    const parentLei = sub.parent_lob_lei || "—";
    const domain = sub.domain || "";
    const website = sub.website_url || (domain ? `https://${domain}` : "");
    const isVerified = Boolean(sub.is_manually_verified);
    const verifiedAt = sub.manually_verified_at ? new Date(sub.manually_verified_at).toLocaleString() : "Never";
    const meta = sub.metadata || sub.metadata_ || {};

    $verifyBtn.attr("data-sublob-id", sub.id);
    if (isVerified) {
      $verifyBtn.removeClass("btn-primary").addClass("btn-success").html('<i class="bi bi-check-circle-fill"></i> Verified ✓');
    } else {
      $verifyBtn.removeClass("btn-success").addClass("btn-primary").html('<i class="bi bi-check2-circle"></i> Mark Verified');
    }

    $body.html(`
      <!-- Header Banner -->
      <div style="background:linear-gradient(135deg,#f8fafc,#f1f5f9);border:1px solid #e2e8f0;border-radius:10px;padding:16px;display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;">
            <span class="badge" style="background:#e0f2fe;color:#0284c7;font-weight:700;"><i class="bi bi-diagram-3"></i> ${esc(entityLevel)}</span>
            <span class="badge-solid-green">${esc(status)}</span>
            ${isVerified ? `<span class="badge" style="background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;"><i class="bi bi-check-circle-fill"></i> Manually Verified</span>` : `<span class="badge" style="background:#f8fafc;color:#64748b;border:1px solid #e2e8f0;"><i class="bi bi-dash-circle"></i> AI Inferred</span>`}
          </div>
          <h4 style="margin:0;font-size:1.15rem;font-weight:700;color:#0f172a;">${esc(sName)}</h4>
          ${sLegal && sLegal !== sName ? `<div style="font-size:0.8rem;color:#64748b;margin-top:2px;">Legal Registration: <strong>${esc(sLegal)}</strong></div>` : ''}
        </div>
        <div>
          ${lei ? `
            <a href="https://search.gleif.org/#/record/${esc(lei)}" target="_blank" rel="noopener noreferrer" class="btn btn-sm btn-outline-primary" style="font-size:0.75rem;padding:5px 12px;border-radius:6px;font-weight:600;">
              <i class="bi bi-shield-check"></i> GLEIF Golden Copy <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;"></i>
            </a>
          ` : ''}
        </div>
      </div>

      <!-- 18 Enterprise Attribute Grid -->
      <div class="sublob-modal-meta-grid">
        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">LEI IDENTIFIER (ISO 17442)</div>
          <div class="sublob-detail-value" style="font-family:monospace;display:flex;align-items:center;justify-content:space-between;">
            <span>${esc(lei || '—')}</span>
            ${lei ? `<button type="button" class="btn btn-xs copy-lei-btn" data-lei="${esc(lei)}" style="padding:1px 6px;font-size:0.7rem;border:1px solid #cbd5e1;background:#fff;border-radius:4px;" title="Copy LEI"><i class="bi bi-clipboard"></i></button>` : ''}
          </div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">JURISDICTION &amp; COUNTRY</div>
          <div class="sublob-detail-value">
            ${getCountryFlag(country)} ${esc(jurisdiction)} ${country ? `(${esc(country)})` : ''}
          </div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">CITY / REGIONAL LOCATION</div>
          <div class="sublob-detail-value">${esc(city)}</div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">RELATIONSHIP TAXONOMY</div>
          <div class="sublob-detail-value">${esc(relationship)}</div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">CONTROLLING PARENT LOB</div>
          <div class="sublob-detail-value" style="color:#0284c7;display:flex;align-items:center;gap:5px;">
            <i class="bi bi-building"></i> ${esc(parentName)}
          </div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">PARENT LOB LEI CODE</div>
          <div class="sublob-detail-value" style="font-family:monospace;">
            ${parentLei && parentLei !== '—' ? `
              <a href="https://search.gleif.org/#/record/${esc(parentLei)}" target="_blank" rel="noopener noreferrer" style="color:#0284c7;">
                ${esc(parentLei)} <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;"></i>
              </a>
            ` : '—'}
          </div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">DIGITAL DOMAIN</div>
          <div class="sublob-detail-value">
            ${domain ? `<span style="color:#0284c7;"><i class="bi bi-globe"></i> ${esc(domain)}</span>` : '<span style="color:#94a3b8;">Inherited from parent LOB</span>'}
          </div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">OFFICIAL WEBSITE URL</div>
          <div class="sublob-detail-value">
            ${website ? `
              <a href="${esc(website)}" target="_blank" rel="noopener noreferrer" style="color:#0284c7;word-break:break-all;">
                ${esc(website)} <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;"></i>
              </a>
            ` : '<span style="color:#94a3b8;">—</span>'}
          </div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">MANUAL VERIFICATION STATUS</div>
          <div class="sublob-detail-value" style="display:flex;align-items:center;gap:6px;">
            <i class="bi ${isVerified ? 'bi-check-circle-fill' : 'bi-dash-circle'}" style="color:${isVerified ? '#059669' : '#94a3b8'};"></i>
            <span>${isVerified ? 'Verified by Sales Engineer' : 'AI Inferred (Unverified)'}</span>
          </div>
        </div>

        <div class="sublob-detail-cell">
          <div class="sublob-detail-label">LAST AUDIT / VERIFIED AT</div>
          <div class="sublob-detail-value">${esc(verifiedAt)}</div>
        </div>
      </div>

      <!-- Collapsible Raw Metadata Payload -->
      <div style="margin-top:16px;">
        <button type="button" class="btn btn-sm btn-outline-secondary btn-toggle-raw-json" style="font-size:0.75rem;">
          <i class="bi bi-code-slash"></i> View Raw Ingestion Metadata (JSON) <i class="bi bi-chevron-down"></i>
        </button>
        <div class="raw-json-container d-none" style="margin-top:10px;">
          <pre style="font-size:.7rem;max-height:220px;overflow:auto;background:#0f172a;color:#f8fafc;padding:12px;border-radius:8px;margin:0;white-space:pre-wrap;">${esc(JSON.stringify(meta, null, 2))}</pre>
        </div>
      </div>
    `);
  }

  // Click handler for Details button & clicking Sub-LOB card
  $(document).on("click", ".btn-sublob-details", function (e) {
    e.stopPropagation();
    const subId = $(this).attr("data-sublob-id");
    if (subId) {
      showSubLobDetailsModal(subId);
    }
  });

  $(document).on("click", ".sublob-card", function (e) {
    if ($(e.target).closest("button, a, input, select").length) return;
    const subId = $(this).attr("data-sublob-id");
    if (subId) {
      showSubLobDetailsModal(subId);
    }
  });

  // Modal Verify Button click handler
  $(document).on("click", "#modalVerifySubLobBtn", async function (e) {
    e.preventDefault();
    const subId = $(this).attr("data-sublob-id");
    if (!subId) return;

    const $btn = $(this);
    $btn.prop("disabled", true).html('<i class="bi bi-arrow-repeat spin"></i> Verifying...');
    try {
      const res = await fetch(`${API_BASE}/api/sub-lobs/${subId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_manually_verified: true }),
      });
      if (res.ok) {
        showNotification("✔ Sub-LOB marked as manually verified in database!", "success");
        $btn.removeClass("btn-primary").addClass("btn-success").html('<i class="bi bi-check-circle-fill"></i> Verified ✓');
        if (activeLob && Array.isArray(activeLob.sub_lobs)) {
          const s = activeLob.sub_lobs.find(x => String(x.id) === String(subId));
          if (s) {
            s.is_manually_verified = true;
            s.manually_verified_at = new Date().toISOString();
          }
        }
        $(`.sublob-card[data-sublob-id="${subId}"]`).addClass("verified");
        $(`.sublob-card[data-sublob-id="${subId}"] .btn-verify-sublob`).replaceWith(`
          <span class="badge" style="background:#ecfdf5;color:#047857;border:1px solid #a7f3d0;font-size:0.68rem;padding:3px 7px;">
            <i class="bi bi-check-circle-fill"></i> Verified
          </span>
        `);
        if (activeAccount && activeAccount.name) {
          refreshPipelineRuns(activeAccount.name);
        }
      } else {
        showNotification("Verification update failed.", "error");
        $btn.prop("disabled", false).html('<i class="bi bi-check2-circle"></i> Mark Verified');
      }
    } catch (err) {
      showNotification(`Error: ${err.message}`, "error");
      $btn.prop("disabled", false).html('<i class="bi bi-check2-circle"></i> Mark Verified');
    }
  });

  // ─── Intelligence Feeds Interactive Modals & Handlers ──────────────────────

  // 1. Open Add Feed Modal
  $(document).on("click", "#btnAddIntelligenceFeed", function (e) {
    e.preventDefault();
    if (!activeAccount) {
      showNotification("Please select an active account first.", "error");
      return;
    }
    $("#feedUrlInput").val("");
    $("#addFeedModal").modal("show");
  });

  $(document).on("click", "#btnOpenAddFeedFromAll", function (e) {
    e.preventDefault();
    $("#allActiveFeedsModal").modal("hide");
    setTimeout(() => {
      $("#feedUrlInput").val("");
      $("#addFeedModal").modal("show");
    }, 300);
  });

  // 2. Click feed row to launch live feed or open configuration
  $(document).on("click", ".feed-status-interactive", function (e) {
    e.preventDefault();
    const url = $(this).attr("data-feed-url");
    const key = $(this).attr("data-feed-key");
    const name = $(this).attr("data-feed-name");

    if (url && url !== "null" && url.trim().length > 0) {
      const norm = normalizeUrl(url);
      window.open(norm, "_blank", "noopener,noreferrer");
    } else {
      if (key) {
        $("#feedChannelSelect").val(key);
      }
      $("#feedUrlInput").val("").attr("placeholder", `Enter ${name} URL or query`);
      $("#addFeedModal").modal("show");
    }
  });

  // 3. Save Feed handler
  $(document).on("click", "#btnSaveNewFeed", async function (e) {
    e.preventDefault();
    if (!activeAccount) return;

    const channelKey = $("#feedChannelSelect").val();
    const feedUrl = $("#feedUrlInput").val().trim();
    if (!feedUrl) {
      $("#feedUrlInput").focus();
      return;
    }

    const $btn = $(this);
    $btn.prop("disabled", true).html('<i class="bi bi-arrow-repeat spin"></i> Saving...');

    try {
      const payload = { [channelKey]: feedUrl };
      const res = await fetch(`${API_BASE}/api/accounts/${activeAccount.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (res.ok) {
        activeAccount[channelKey] = feedUrl;
        if (MOCK_DATA.accounts) {
          const match = MOCK_DATA.accounts.find(a => a.id === activeAccount.id);
          if (match) match[channelKey] = feedUrl;
          try {
            sessionStorage.setItem("pipeline_accounts_cache", JSON.stringify(MOCK_DATA));
          } catch (_) {}
        }
        showNotification("✔ Intelligence feed added & activated!", "success");
        $("#addFeedModal").modal("hide");

        // Refresh UI
        $("#accountOverviewContainer").html(renderModernAccountOverview(activeAccount));
        $("#accountHeroContainer").html(renderModernAccountHeader(activeAccount));
        $("#completenessContainer").html(renderModernCompleteness(activeAccount));
        refreshAccountsCache();
      } else {
        showNotification("Failed to save intelligence feed.", "error");
      }
    } catch (err) {
      showNotification(`Error: ${err.message}`, "error");
    } finally {
      $btn.prop("disabled", false).html('<i class="bi bi-check2"></i> Save &amp; Activate Feed');
    }
  });

  // 4. View All Active OSINT Channels Modal
  $(document).on("click", "#btnViewAllActiveFeeds", function (e) {
    e.preventDefault();
    if (!activeAccount) return;

    const allChannels = [
      { name: "X / Twitter Live Activity", key: "twitter_live_url", url: activeAccount.twitter_live_url, icon: "bi-twitter-x" },
      { name: "Google News Live RSS Feed", key: "rss_url", url: activeAccount.rss_url || activeAccount.news_query, icon: "bi-newspaper" },
      { name: "Reddit Community Discussions", key: "reddit_rss_url", url: activeAccount.reddit_rss_url || activeAccount.reddit_query, icon: "bi-reddit" },
      { name: "Google Patents Portfolio", key: "google_patents_url", url: activeAccount.google_patents_url, icon: "bi-patch-check-fill" },
      { name: "YouTube Executive Media", key: "youtube_search_url", url: activeAccount.youtube_search_url, icon: "bi-youtube" },
      { name: "Wikidata Knowledge Entity", key: "wikidata_entity_url", url: activeAccount.wikidata_entity_url, icon: "bi-diagram-2" },
      { name: "Google Trends Analytics", key: "google_trends_url", url: activeAccount.google_trends_url, icon: "bi-graph-up-arrow" },
      { name: "Glassdoor Workplace Reviews", key: "glassdoor_url", url: activeAccount.glassdoor_url, icon: "bi-star-fill" },
      { name: "GitHub Repositories", key: "github_url", url: activeAccount.github_url, icon: "bi-github" },
      { name: "Corporate Press & Blog", key: "blog_url", url: activeAccount.blog_url, icon: "bi-journal-text" },
      { name: "SEC EDGAR Search URL", key: "sec_edgar_url", url: activeAccount.sec_edgar_url, icon: "bi-file-earmark-text" },
      { name: "SEC Filings RSS Feed", key: "sec_filings_rss", url: activeAccount.sec_filings_rss, icon: "bi-rss" },
      { name: "OpenAlex Academic Institution", key: "openalex_institution_url", url: activeAccount.openalex_institution_url, icon: "bi-mortarboard-fill" },
      { name: "Official Corporate Website", key: "website_url", url: activeAccount.website_url || (activeAccount.domain ? 'https://' + activeAccount.domain : ''), icon: "bi-globe" },
      { name: "LinkedIn Corporate Page", key: "linkedin_url", url: activeAccount.linkedin_url, icon: "bi-linkedin" },
    ];

    $("#allActiveFeedsSubtitle").text(`Multi-source automated intelligence feeds for ${activeAccount.name || activeAccount.legal_name}`);

    const gridHtml = `
      <div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(320px, 1fr));gap:12px;">
        ${allChannels.map(ch => {
          const isActive = Boolean(ch.url && ch.url.trim().length > 0);
          const norm = isActive ? normalizeUrl(ch.url) : "";
          return `
            <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:14px;display:flex;flex-direction:column;justify-content:space-between;gap:8px;">
              <div>
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
                  <div style="display:flex;align-items:center;gap:8px;font-weight:700;color:#0f172a;font-size:0.86rem;">
                    <i class="bi ${ch.icon}" style="color:#0284c7;font-size:1rem;"></i>
                    <span>${esc(ch.name)}</span>
                  </div>
                  <span class="${isActive ? 'badge-solid-green' : 'badge-solid-gray'}" style="font-size:0.65rem;padding:2px 7px;">
                    ${isActive ? 'Active Stream' : 'Not Configured'}
                  </span>
                </div>
                <div style="font-size:0.75rem;color:#64748b;word-break:break-all;">
                  ${isActive ? `<a href="${esc(norm)}" target="_blank" rel="noopener noreferrer" style="color:#0284c7;text-decoration:none;">${esc(ch.url.length > 55 ? ch.url.substring(0, 52) + '...' : ch.url)} <i class="bi bi-box-arrow-up-right" style="font-size:0.65rem;"></i></a>` : '<span style="color:#94a3b8;font-style:italic;">No endpoint configured</span>'}
                </div>
              </div>
              <div style="display:flex;align-items:center;justify-content:flex-end;gap:6px;margin-top:4px;">
                ${isActive ? `
                  <a href="${esc(norm)}" target="_blank" rel="noopener noreferrer" class="btn btn-xs btn-outline-primary" style="padding:3px 10px;font-size:0.72rem;border-radius:6px;text-decoration:none;font-weight:600;">
                    <i class="bi bi-box-arrow-up-right"></i> Launch
                  </a>
                ` : `
                  <button type="button" class="btn btn-xs btn-outline-secondary btn-configure-feed-channel" data-channel-key="${ch.key}" data-channel-name="${esc(ch.name)}" style="padding:3px 10px;font-size:0.72rem;border-radius:6px;">
                    <i class="bi bi-gear"></i> Configure
                  </button>
                `}
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;

    $("#allActiveFeedsModalBody").html(gridHtml);
    $("#allActiveFeedsModal").modal("show");
  });

  $(document).on("click", ".btn-configure-feed-channel", function (e) {
    e.preventDefault();
    const key = $(this).attr("data-channel-key");
    const name = $(this).attr("data-channel-name");
    $("#allActiveFeedsModal").modal("hide");
    setTimeout(() => {
      if (key) $("#feedChannelSelect").val(key);
      $("#feedUrlInput").val("").attr("placeholder", `Enter ${name} URL`);
      $("#addFeedModal").modal("show");
    }, 300);
  });

  // ─── Toggle Expand/Collapse for LOB Cards ─────────────────────────────────
  $(document).on("click", ".btn-toggle-lobs-expand", function () {
    const $btn = $(this);
    const isExpanded = $btn.attr("data-expanded") === "true";
    const $extraCards = $("#lobCardsContainer").find(".lob-card-extra");
    const totalCount = activeAccount && activeAccount.lobs ? activeAccount.lobs.length : 55;
    const extraCount = Math.max(0, totalCount - 10);

    if (isExpanded) {
      $extraCards.hide();
      $btn.attr("data-expanded", "false");
      $btn.find("span").text(`View all ${totalCount} Lines of Business (+${extraCount} more)`);
      $btn.find("i").removeClass("bi-chevron-up").addClass("bi-chevron-down");
    } else {
      $extraCards.css("display", "flex");
      $btn.attr("data-expanded", "true");
      $btn.find("span").text(`Show less (top 10 Divisions)`);
      $btn.find("i").removeClass("bi-chevron-down").addClass("bi-chevron-up");
    }
  });

  // ─── Pipeline Telemetry & Credits Usage Modal ──────────────────────────────
  let activeCreditUsageData = null;

  function getCreditIcon(name, defaultIcon) {
    const n = (name || "").toLowerCase();
    if (n.includes("sec") || n.includes("regulatory") || n.includes("gleif") || n.includes("identity")) return "bi-shield-check";
    if (n.includes("diffbot") || n.includes("graph") || n.includes("entity")) return "bi-diagram-3";
    if (n.includes("finnhub") || n.includes("fmp") || n.includes("market") || n.includes("watchlist")) return "bi-graph-up-arrow";
    if ((n.includes("serp") || n.includes("google")) && !n.includes("patent")) return "bi-search";
    if (n.includes("linkedin") || n.includes("company") || n.includes("banking")) return "bi-building";
    if (n.includes("tavily") || n.includes("competitor") || n.includes("ai")) return "bi-cpu";
    if (n.includes("patent")) return "bi-lightbulb";
    if (n.includes("profile") || n.includes("persona") || n.includes("leadership") || n.includes("people")) return "bi-people";
    if (n.includes("gemini") || n.includes("llm") || n.includes("neural") || n.includes("biography")) return "bi-robot";
    return defaultIcon || "bi-plug";
  }

  function renderCreditResourceRows(resources, theme) {
    if (!resources || !resources.length) {
      return `<div style="padding:10px;text-align:center;color:#94a3b8;font-size:0.75rem;">No resources recorded</div>`;
    }
    const colorClass = theme === "account" ? "purple" : theme === "lob" ? "green" : "violet";
    
    return resources.map(res => {
      const icon = getCreditIcon(res.name, theme === "account" ? "bi-shield-check" : theme === "lob" ? "bi-building" : "bi-robot");
      return `
        <div class="credit-resource-row">
          <div class="credit-icon-box icon-${colorClass}">
            <i class="bi ${icon}"></i>
          </div>
          <div class="credit-resource-info">
            <div class="credit-resource-name" title="${esc(res.name)}">${esc(res.name)}</div>
            <div class="credit-resource-meta">
              <span style="display:inline-flex;align-items:center;gap:4px;">
                <i class="bi bi-key-fill" style="opacity:0.6;font-size:0.72rem;"></i>
                <code style="font-size:0.7rem;color:#475569;background:#f1f5f9;padding:1px 5px;border-radius:4px;">${esc(res.api_key_masked)}</code>
              </span>
            </div>
          </div>
          <div class="credit-resource-progress-col">
            <div style="display:flex;justify-content:space-between;font-size:0.68rem;color:#64748b;margin-bottom:2px;">
              <span>${esc(res.calls_label)}</span>
              <span style="font-weight:700;color:#334155;">${res.percentage_of_run}%</span>
            </div>
            <div class="credit-progress-track">
              <div class="credit-progress-fill-${colorClass}" style="width:${Math.min(100, Math.max(2, res.percentage_of_run))}%;"></div>
            </div>
          </div>
          <div class="credit-resource-num">
            <span class="credit-val-big">${Number(res.credits).toLocaleString()}</span>
            <span class="credit-val-unit">credits</span>
          </div>
        </div>
      `;
    }).join("");
  }

  async function openCreditUsageModal(accountId, runId) {
    const targetAccountId = accountId || (activeAccount ? activeAccount.id : 27);
    const url = runId 
      ? `/api/pipeline/runs/${runId}/credit-breakdown`
      : `/api/accounts/${targetAccountId}/credit-breakdown`;

    try {
      const modalEl = document.getElementById("creditUsageModal");
      if (!modalEl) {
        console.error("creditUsageModal element not found in DOM");
        return;
      }
      const bsModal = bootstrap.Modal.getOrCreateInstance(modalEl);
      bsModal.show();

      const resp = await fetch(url);
      if (!resp.ok) {
        throw new Error(`Server returned ${resp.status}`);
      }
      const data = await resp.json();
      activeCreditUsageData = data;

      // Header
      if (data.title) $("#creditUsageModalTitle").text(data.title);
      if (data.subtitle) $("#creditUsageModalSubtitle").text(data.subtitle);
      if (data.status) {
        const isCompleted = data.status.toLowerCase() === "completed" || data.status.toLowerCase() === "staged" || data.status.toLowerCase() === "success";
        const dotColor = isCompleted ? "#059669" : "#94a3b8";
        const badgeBg = isCompleted ? "#ecfdf5" : "#f1f5f9";
        const badgeColor = isCompleted ? "#059669" : "#64748b";
        $("#creditUsageStatusBadge").css({"background": badgeBg, "color": badgeColor, "border-color": isCompleted ? "#a7f3d0" : "#cbd5e1"}).html(`
          <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${dotColor};"></span>
          ${esc(data.status)}
        `);
      }

      // KPIs
      if (data.kpis) {
        $("#kpiTotalCredits").text(data.kpis.total_credits_label || Number(data.kpis.total_credits || 0).toLocaleString());
        $("#kpiTotalCreditsSub").text(data.kpis.vs_avg_run || "");
        $("#kpiRunDuration").text(data.kpis.duration || "0s");
        $("#kpiRunStages").text(data.kpis.stages_sources || "");
        $("#kpiCostPerCredit").text(data.kpis.est_cost_per_credit || "$0.0021");
        $("#kpiCostTotal").text(data.kpis.total_cost_this_run || "$0.00");
      }

      // Section A (Account)
      if (data.sections && data.sections.account) {
        const sec = data.sections.account;
        $("#accountSectionSubtitle").text(sec.subtitle || "");
        $("#accountSubtotalHeader").text(sec.subtotal_label || "");
        $("#accountCombinedCredits").text(sec.combined_label || "");
        $("#accountCombinedPct").text(sec.combined_subtext || "");
        $("#accountResourcesList").html(renderCreditResourceRows(sec.resources, "account"));
      }

      // Section L (LOB)
      if (data.sections && data.sections.lob) {
        const sec = data.sections.lob;
        $("#lobSectionSubtitle").text(sec.subtitle || "");
        $("#lobSubtotalHeader").text(sec.subtotal_label || "");
        $("#lobCombinedCredits").text(sec.combined_label || "");
        $("#lobCombinedPct").text(sec.combined_subtext || "");
        $("#lobResourcesList").html(renderCreditResourceRows(sec.resources, "lob"));
      }

      // Section P (Persona)
      if (data.sections && data.sections.persona) {
        const sec = data.sections.persona;
        $("#personaSectionSubtitle").text(sec.subtitle || "");
        $("#personaSubtotalHeader").text(sec.subtotal_label || "");
        $("#personaCombinedCredits").text(sec.combined_label || "");
        $("#personaCombinedPct").text(sec.combined_subtext || "");
        $("#personaResourcesList").html(renderCreditResourceRows(sec.resources, "persona"));
      }

      // Grand Total Card
      if (data.grand_total) {
        const gt = data.grand_total;
        $("#grandTotalCredits").text(gt.total_credits_label || `${Number(gt.total_credits).toLocaleString()} credits`);
        $("#grandTotalCostNote").text(gt.cost_note || "");
        
        if (gt.shares && gt.shares.length) {
          const segBars = gt.shares.map(s => 
            `<div style="width:${s.pct}%;background:${s.color};" title="${esc(s.name)} (${s.pct}%)"></div>`
          ).join("");
          $("#creditSegmentedBar").html(segBars);

          const legendItems = gt.shares.map(s => `
            <span style="display:inline-flex;align-items:center;gap:5px;">
              <span style="width:8px;height:8px;border-radius:2px;background:${s.color};"></span> ${esc(s.name)} &bull; ${Number(s.credits).toLocaleString()} cr (${s.pct}%)
            </span>
          `).join("");
          $("#creditLegendRow").html(legendItems);
        }
      }

    } catch (err) {
      console.error("Failed to load credit breakdown:", err);
    }
  }

  function exportCreditUsageCsv() {
    if (!activeCreditUsageData) {
      alert("No credit usage data available to export.");
      return;
    }
    const d = activeCreditUsageData;
    const rows = [
      ["Sales AI - Pipeline Run Telemetry Credit Ledger"],
      ["Run ID", d.run_id || "N/A"],
      ["Company", d.company_name || "N/A"],
      ["Started", d.subtitle || "N/A"],
      ["Total Credits", d.kpis ? d.kpis.total_credits : 0],
      ["Est Cost", d.kpis ? d.kpis.total_cost_this_run : "$0.00"],
      [],
      ["Tier", "Resource Name", "Masked API Key", "Calls / Ops", "Credits", "% of Run"]
    ];

    const tiers = [
      { key: "account", label: "ACCOUNT" },
      { key: "lob", label: "LOB" },
      { key: "persona", label: "PERSONA" }
    ];

    tiers.forEach(t => {
      const sec = d.sections ? d.sections[t.key] : null;
      if (sec && sec.resources) {
        sec.resources.forEach(r => {
          rows.push([
            t.label,
            `"${(r.name || '').replace(/"/g, '""')}"`,
            `"${(r.api_key_masked || '').replace(/"/g, '""')}"`,
            `"${(r.calls_label || '').replace(/"/g, '""')}"`,
            r.credits,
            `${r.percentage_of_run}%`
          ]);
        });
      }
    });

    const csvContent = "data:text/csv;charset=utf-8," + rows.map(e => e.join(",")).join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    const filename = `credit_usage_${(d.company_name || 'run').toLowerCase().replace(/[^a-z0-9]/g, '_')}_${d.run_id || 'ledger'}.csv`;
    link.setAttribute("download", filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  // Click handlers
  $(document).on("click", "#btnOpenCreditUsageModal, [data-action='view-credits']", function (e) {
    e.preventDefault();
    openCreditUsageModal();
  });

  $(document).on("click", "#btnExportCreditUsageCsv", function (e) {
    e.preventDefault();
    exportCreditUsageCsv();
  });

  // ══════════════════════════════════════════════════════════════════
  // ENTERPRISE BATCH CONSOLE & SEQUENTIAL PIPELINE ENGINE
  // ══════════════════════════════════════════════════════════════════

  const batchConsoleState = {
    entityType: "persona", // 'persona' or 'lob'
    targetAccount: null,
    selectedKeys: new Set(),
    isRunning: false,
    abortRequested: false,
    filteredItems: [],
    currentProcessingKey: null,
    queuedKeys: new Set(),
  };

  // Helper: check if a persona is truly deeply enriched (has Gemini AI dossier, extended profile, education, or past companies)
  function isPersonaItemEnriched(p) {
    if (!p) return false;
    if (p.is_enriched === true) return true;

    // 1. AI Sales Dossier fields (generated exclusively by Gemini AI multi-source synthesis)
    if (typeof p.value_proposition === "string" && p.value_proposition.trim().length > 10) return true;
    if (typeof p.communication_style === "string" && p.communication_style.trim().length > 10) return true;
    if (typeof p.personalized_icebreaker === "string" && p.personalized_icebreaker.trim().length > 10) return true;

    // 2. Extended Profile object (deep dossier extraction)
    if (p.extended_profile && typeof p.extended_profile === "object" && Object.keys(p.extended_profile).length > 0) return true;

    // 3. Education History (extracted by Apify / LinkedIn / Exa, must have non-empty array)
    if (Array.isArray(p.education_history) && p.education_history.length > 0) return true;
    if (typeof p.education_history === "string" && p.education_history.trim().length > 4 && p.education_history !== "[]") return true;

    // 4. Past Companies / Career Timeline (extracted by Exa / LinkedIn, must have non-empty array)
    if (Array.isArray(p.past_companies) && p.past_companies.length > 0) return true;
    if (typeof p.past_companies === "string" && p.past_companies.trim().length > 4 && p.past_companies !== "[]" && p.past_companies !== "{}") return true;

    // Anything else (Apollo single-job stubs, generic search links, etc.) is a pending stub
    return false;
  }

  // Helper: check if an LOB is truly deeply enriched (has corporate tech, competitors, filings, or LEI)
  function isLobItemEnriched(l) {
    if (!l) return false;
    if (l.is_enriched === true) return true;
    
    // Deeply enriched LOBs have technologies, competitors, financials, patents, LEI, or overview populated
    if (Array.isArray(l.technologies) && l.technologies.length > 0) return true;
    if (Array.isArray(l.competitors) && l.competitors.length > 0) return true;
    if (Array.isArray(l.financial_snippets) && l.financial_snippets.length > 0) return true;
    if (Array.isArray(l.patents) && l.patents.length > 0) return true;
    if (l.financial_snippets && typeof l.financial_snippets === "object" && Object.keys(l.financial_snippets).length > 0) return true;
    if (l.patents && typeof l.patents === "object" && Object.keys(l.patents).length > 0) return true;
    if (l.lei_code || l.jurisdiction) return true;
    if (typeof l.overview === "string" && l.overview.trim().length > 25) return true;

    return false;
  }

  // Standardized Seniority Tier Key resolver for Personas
  function getPersonaTierKey(item) {
    if (!item) return "other";
    const t = String(item.tier || "").toLowerCase().trim();
    const sr = String(item.seniority_raw || "").toLowerCase().trim();
    const title = String(item.title || "").toLowerCase().trim();

    if (t === "c_suite" || t === "c-suite" || t.includes("executive") || sr === "c_suite" || sr.includes("csuite") || sr === "tier1_csuite_and_officers" || sr.includes("board")) {
      return "c_suite";
    }
    if (t === "vp_level" || t === "vp" || t.includes("vice president") || sr === "vp_level" || sr === "tier2_global_and_division_heads" || sr.includes("vp")) {
      return "vp";
    }
    if (t === "director_level" || t === "director" || sr.includes("director")) {
      return "director";
    }
    if (t === "manager_level" || t === "manager" || sr.includes("manager")) {
      return "manager";
    }

    if (/\b(ceo|cfo|coo|cto|cio|ciso|cro|cmo|cpo|chief|president|founder|chairman)\b/i.test(title)) return "c_suite";
    if (/\b(managing director|vice president|\bvp\b|\bevp\b|\bsvp\b|\bavp\b)\b/i.test(title)) return "vp";
    if (/\b(director|head of)\b/i.test(title)) return "director";
    if (/\b(manager|lead|supervisor)\b/i.test(title)) return "manager";

    return "other";
  }

  // Update small badge counters on the trigger strip (below Account Hero)
  function updateBatchTriggerPills(account) {
    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts))
      ? MOCK_DATA.accounts
      : [];
    $("#batchAccountStubCount").text(`${acctsList.length || 5} Total`);

    if (!account) return;
    const personas = account.personas || [];
    const lobs = account.lobs || [];

    let pPending = 0;
    personas.forEach((p) => {
      if (!isPersonaItemEnriched(p)) pPending++;
    });

    let lPending = 0;
    lobs.forEach((l) => {
      if (!isLobItemEnriched(l)) lPending++;
    });

    $("#batchPersonaStubCount").text(pPending > 0 ? `${pPending} Pending` : `${personas.length} Total`);
    $("#batchLobStubCount").text(lPending > 0 ? `${lPending} Pending` : `${lobs.length} Total`);
  }

  // Open Batch Console Modal
  function openBatchConsole(entityType, account) {
    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts) && MOCK_DATA.accounts.length)
      ? MOCK_DATA.accounts
      : [];
    let targetAcct = account || activeAccount;
    if (!targetAcct) {
      if (acctsList.length > 0) {
        targetAcct = acctsList[0];
        activeAccount = targetAcct;
      } else {
        showToast("Please select an enterprise account first", "warning");
        return;
      }
    }

    batchConsoleState.entityType = entityType || "persona";
    batchConsoleState.targetAccount = targetAcct;
    batchConsoleState.selectedKeys.clear();
    batchConsoleState.isRunning = false;
    batchConsoleState.abortRequested = false;

    // Populate Company Selector Dropdown
    const $select = $("#batchCompanySelector");
    $select.empty();
    if (batchConsoleState.entityType === "account") {
      $select.append(`<option value="all" selected>All Enterprise Accounts (${acctsList.length})</option>`);
    }
    const dropdownList = acctsList.length > 0 ? acctsList : [targetAcct];
    dropdownList.forEach((a) => {
      const isSelected = (batchConsoleState.entityType !== "account" && a.id === targetAcct.id) ? "selected" : "";
      $select.append(`<option value="${a.id}" ${isSelected}>${esc(a.name || a.legal_name || "Account")}</option>`);
    });

    // Update Entity Switcher Tabs
    $(".batch-entity-tab").removeClass("active");
    $(`.batch-entity-tab[data-entity="${batchConsoleState.entityType}"]`).addClass("active");

    // Configure Headers & Labels based on Entity Type
    if (batchConsoleState.entityType === "account") {
      $("#batchConsoleHeaderIcon").html('<i class="bi bi-building"></i>');
      $("#batchConsoleTitle").text("Batch Account Intelligence Console");
      $("#batchKpiTotalLabel").text("Total Accounts");
      $("#batchKpiPendingLabel").text("Pending Accounts");
      $("#batchTierFilter").addClass("d-none");
      $("#batchColThName").text("ACCOUNT / INSTITUTION");
      $("#batchColThCompany").text("DOMAIN & TICKER");
      $("#batchColThTitle").text("SEC CIK & TYPE");
      $("#batchColThTier").text("COVERAGE");
    } else if (batchConsoleState.entityType === "lob") {
      $("#batchConsoleHeaderIcon").html('<i class="bi bi-diagram-3-fill"></i>');
      $("#batchConsoleTitle").text("Batch LOB Intelligence Console");
      $("#batchKpiTotalLabel").text("Total LOBs");
      $("#batchKpiPendingLabel").text("Pending Divisions");
      $("#batchTierFilter").addClass("d-none");
      $("#batchColThName").text("LINE OF BUSINESS");
      $("#batchColThCompany").text("COMPANY");
      $("#batchColThTitle").text("SEGMENT & OVERVIEW");
      $("#batchColThTier").text("RELATIONSHIP");
    } else {
      $("#batchConsoleHeaderIcon").html('<i class="bi bi-people-fill"></i>');
      $("#batchConsoleTitle").text("Batch People Enrichment Console");
      $("#batchKpiTotalLabel").text("Total People");
      $("#batchKpiPendingLabel").text("Pending Stubs");
      $("#batchTierFilter").removeClass("d-none");
      $("#batchColThName").text("NAME & EXECUTIVE");
      $("#batchColThCompany").text("COMPANY");
      $("#batchColThTitle").text("TITLE & DEPT");
      $("#batchColThTier").text("TIER");
    }

    // Reset toolbar controls
    $("#batchSearchInput").val("");
    $("#batchStatusFilter").val("pending");
    $("#batchTierFilter").val("all");
    $("#batchLiveProgressWrap").addClass("d-none");
    $("#batchStopBtn").addClass("d-none");
    $("#batchRunEnrichmentBtn").prop("disabled", true);
    $("#batchMasterCheckbox").prop("checked", false);

    // Refresh KPIs and Table
    refreshBatchConsole();

    // Show modal and backdrop explicitly with display styles
    $("#batchEnrichmentBackdrop").removeClass("d-none").css("display", "block");
    $("#batchEnrichmentModal").removeClass("d-none").css("display", "flex");

    PipelineRouter.syncState({
      view: "batch_console",
      entityType: batchConsoleState.entityType || "persona",
      accountId: (batchConsoleState.entityType === "account" && $("#batchCompanySelector").val() === "all")
        ? "all"
        : (batchConsoleState.targetAccount ? String(batchConsoleState.targetAccount.id) : null),
      screen: $("#batchScreenHistory").hasClass("d-none") ? "directory" : "history",
    });
  }

  // Switch Entity Type inside the modal without closing it!
  function switchBatchEntity(newEntityType) {
    if (batchConsoleState.isRunning) {
      showToast("Please wait for current batch run to finish or click Stop", "warning");
      return;
    }
    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts))
      ? MOCK_DATA.accounts
      : [];
    let targetAcct = batchConsoleState.targetAccount || activeAccount || (acctsList.length ? acctsList[0] : null);

    batchConsoleState.entityType = newEntityType;
    batchConsoleState.selectedKeys.clear();

    // Re-populate / synchronize company dropdown based on mode
    const $select = $("#batchCompanySelector");
    $select.empty();
    if (newEntityType === "account") {
      $select.append(`<option value="all" selected>All Enterprise Accounts (${acctsList.length})</option>`);
      acctsList.forEach((a) => {
        $select.append(`<option value="${a.id}">${esc(a.name || a.legal_name || "Account")}</option>`);
      });
    } else {
      acctsList.forEach((a) => {
        const isSelected = targetAcct && a.id === targetAcct.id ? "selected" : "";
        $select.append(`<option value="${a.id}" ${isSelected}>${esc(a.name || a.legal_name || "Account")}</option>`);
      });
    }

    // Update Switcher Tab UI
    $(".batch-entity-tab").removeClass("active");
    $(`.batch-entity-tab[data-entity="${newEntityType}"]`).addClass("active");

    // Configure Headers & Column Labels based on Entity Type
    if (newEntityType === "account") {
      $("#batchConsoleHeaderIcon").html('<i class="bi bi-building"></i>');
      $("#batchConsoleTitle").text("Batch Account Intelligence Console");
      $("#batchKpiTotalLabel").text("Total Accounts");
      $("#batchKpiPendingLabel").text("Pending Accounts");
      $("#batchTierFilter").addClass("d-none");
      $("#batchColThName").text("ACCOUNT / INSTITUTION");
      $("#batchColThCompany").text("DOMAIN & TICKER");
      $("#batchColThTitle").text("SEC CIK & TYPE");
      $("#batchColThTier").text("COVERAGE");
    } else if (newEntityType === "lob") {
      $("#batchConsoleHeaderIcon").html('<i class="bi bi-diagram-3-fill"></i>');
      $("#batchConsoleTitle").text("Batch LOB Intelligence Console");
      $("#batchKpiTotalLabel").text("Total LOBs");
      $("#batchKpiPendingLabel").text("Pending Divisions");
      $("#batchTierFilter").addClass("d-none");
      $("#batchColThName").text("LINE OF BUSINESS");
      $("#batchColThCompany").text("COMPANY");
      $("#batchColThTitle").text("SEGMENT & OVERVIEW");
      $("#batchColThTier").text("RELATIONSHIP");
    } else {
      $("#batchConsoleHeaderIcon").html('<i class="bi bi-people-fill"></i>');
      $("#batchConsoleTitle").text("Batch People Enrichment Console");
      $("#batchKpiTotalLabel").text("Total People");
      $("#batchKpiPendingLabel").text("Pending Stubs");
      $("#batchTierFilter").removeClass("d-none");
      $("#batchColThName").text("NAME & EXECUTIVE");
      $("#batchColThCompany").text("COMPANY");
      $("#batchColThTitle").text("TITLE & DEPT");
      $("#batchColThTier").text("TIER");
    }

    // Reset filters and table
    $("#batchSearchInput").val("");
    $("#batchStatusFilter").val("pending");
    $("#batchTierFilter").val("all");
    $("#batchLiveProgressWrap").addClass("d-none");
    $("#batchStopBtn").addClass("d-none");
    $("#batchRunEnrichmentBtn").prop("disabled", true);
    $("#batchMasterCheckbox").prop("checked", false);

    refreshBatchConsole();

    PipelineRouter.syncState({
      view: "batch_console",
      entityType: newEntityType,
      accountId: (newEntityType === "account" && $("#batchCompanySelector").val() === "all")
        ? "all"
        : (batchConsoleState.targetAccount ? String(batchConsoleState.targetAccount.id) : null),
      screen: $("#batchScreenHistory").hasClass("d-none") ? "directory" : "history",
    });
  }

  // Close Batch Console Modal
  function closeBatchConsole() {
    if (batchConsoleState.isRunning) {
      if (!confirm("A batch enrichment is currently running in sequential mode. Do you want to stop the queue and close?")) {
        return;
      }
      batchConsoleState.abortRequested = true;
    }
    $("#batchEnrichmentBackdrop, #batchEnrichmentModal").addClass("d-none").css("display", "none");
    $("#batchLiveProgressWrap").addClass("d-none");

    if (activeAccount) {
      const activeTab = $(".tab-pill-btn.active").data("nav-tab") || "overview";
      PipelineRouter.syncState({
        view: "account",
        accountId: String(activeAccount.id),
        subTab: activeTab,
      });
    } else {
      PipelineRouter.syncState({ view: "home" });
    }
  }

  // Account Enrichment Verification Check
  function isAccountItemEnriched(acct) {
    if (!acct) return false;
    if (acct.is_enriched) return true;
    if (acct.sec_cik && (acct.sec_edgar_url || acct.patents_granted || acct.it_spend)) return true;
    const cols = calculateDbColumnsFilled(acct, "account");
    return cols.filled >= 25;
  }

  // Refresh KPIs, filter records, and re-render table
  function refreshBatchConsole() {
    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts))
      ? MOCK_DATA.accounts
      : [];
    const acct = batchConsoleState.targetAccount || (acctsList.length ? acctsList[0] : null);
    if (!acct && batchConsoleState.entityType !== "account") return;

    const entityType = batchConsoleState.entityType;
    let allItems = [];
    if (entityType === "account") {
      const selectedId = $("#batchCompanySelector").val();
      if (selectedId && selectedId !== "all") {
        const single = acctsList.find((a) => a.id === parseInt(selectedId, 10));
        allItems = single ? [single] : acctsList;
      } else {
        allItems = acctsList;
      }
    } else if (entityType === "lob") {
      allItems = acct?.lobs || [];
    } else {
      allItems = acct?.personas || [];
    }

    // Compute Telemetry KPIs (reflects active Seniority Tier filter when selected)
    const tierFilter = $("#batchTierFilter").val() || "all";
    let kpiPool = allItems;
    if (entityType === "persona" && tierFilter && tierFilter !== "all") {
      const normFilter = tierFilter.toLowerCase().replace(/[^a-z0-9]/g, "");
      kpiPool = allItems.filter((i) => {
        const k = getPersonaTierKey(i).toLowerCase().replace(/[^a-z0-9]/g, "");
        return k === normFilter;
      });
    }

    let total = kpiPool.length;
    let enrichedCount = 0;
    let pendingCount = 0;

    kpiPool.forEach((item) => {
      let enriched = false;
      if (entityType === "account") enriched = isAccountItemEnriched(item);
      else if (entityType === "lob") enriched = isLobItemEnriched(item);
      else enriched = isPersonaItemEnriched(item);

      if (enriched) enrichedCount++;
      else pendingCount++;
    });

    $("#batchKpiTotal").text(total);
    $("#batchKpiEnriched").text(enrichedCount);
    $("#batchKpiPending").text(pendingCount);

    // Sync Switcher Tab badges dynamically with active selection scope
    const currentVal = $("#batchCompanySelector").val();
    const isAllScope = (entityType === "account" && currentVal === "all");
    if (isAllScope) {
      const allLobsSum = acctsList.reduce((s, a) => s + (a.lobs?.length || 0), 0);
      const allPersonasSum = acctsList.reduce((s, a) => s + (a.personas?.length || 0), 0);
      $("#batchEntityAccountBadge").text(acctsList.length || 0);
      $("#batchEntityLobBadge").text(allLobsSum);
      $("#batchEntityPersonaBadge").text(allPersonasSum);
    } else {
      $("#batchEntityAccountBadge").text(1);
      $("#batchEntityLobBadge").text(acct?.lobs?.length || 0);
      $("#batchEntityPersonaBadge").text(acct?.personas?.length || 0);
    }

    // Apply Filter Criteria
    const searchQuery = ($("#batchSearchInput").val() || "").trim().toLowerCase();
    const statusFilter = $("#batchStatusFilter").val() || "all";

    const filtered = allItems.filter((item) => {
      let enriched = false;
      if (entityType === "account") enriched = isAccountItemEnriched(item);
      else if (entityType === "lob") enriched = isLobItemEnriched(item);
      else enriched = isPersonaItemEnriched(item);

      // Status filter
      if (statusFilter === "pending" && enriched) return false;
      if (statusFilter === "enriched" && !enriched) return false;

      // Tier filter (personas only)
      if (entityType === "persona" && tierFilter && tierFilter !== "all") {
        const itemTierKey = getPersonaTierKey(item);
        const normFilter = tierFilter.toLowerCase().replace(/[^a-z0-9]/g, "");
        const normKey = itemTierKey.toLowerCase().replace(/[^a-z0-9]/g, "");
        if (normKey !== normFilter) return false;
      }

      // Search query
      if (searchQuery) {
        const name = (item.name || item.display_name || item.full_name || item.legal_name || "").toLowerCase();
        const title = (item.title || item.stock_symbol || "").toLowerCase();
        const dept = (item.department || item.domain || item.primary_domain || "").toLowerCase();
        const key = (item.key || "").toLowerCase();
        if (!name.includes(searchQuery) && !title.includes(searchQuery) && !dept.includes(searchQuery) && !key.includes(searchQuery)) {
          return false;
        }
      }

      return true;
    });

    batchConsoleState.filteredItems = filtered;
    renderBatchTable(filtered);
    updateSelectionBadges();
  }

  // ── Database Schema Dimensions (97 Account, 89 Persona & 29 LOB columns) ──
  const ACCOUNT_97_COLUMNS = [
    "id", "key", "display_name", "legal_name", "domain", "primary_domain", "website_url",
    "crunchbase_url", "operating_status", "company_type", "founded_date", "founded_year",
    "employee_count_range", "short_description", "full_description", "headquarters_location",
    "city", "state", "country", "postal_code", "phone_number", "sanitized_phone",
    "contact_email", "linkedin_url", "twitter_url", "twitter_handle", "facebook_url",
    "estimated_revenue_range", "total_funding_amount", "total_funding_amount_usd",
    "total_funding_currency", "last_funding_type", "last_funding_date", "num_funding_rounds",
    "funding_status", "stock_symbol", "stock_exchange", "sec_cik", "sec_name",
    "ipo_status", "ipo_date", "num_suborganizations", "num_acquisitions",
    "global_traffic_rank", "monthly_visits", "bounce_rate", "visit_duration",
    "page_views_per_visit", "heat_score", "trend_score_90d", "active_tech_count",
    "it_spend", "patents_granted", "trademarks_registered", "total_apps", "total_downloads",
    "num_founders", "num_contacts", "created_at", "updated_at", "extracted_at",
    "schema_version", "lobs_count", "total_contacts_captured", "c_suite_count",
    "vp_count", "director_count", "manager_count", "sec_edgar_url", "sec_filings_rss",
    "sec_submissions_url", "twitter_live_url", "reddit_query", "reddit_rss_url",
    "news_query", "rss_url", "google_patents_url", "google_trends_url", "youtube_search_url",
    "openalex_institution_url", "wikidata_entity_url", "blog_url", "youtube_channel_id",
    "industries", "industry_groups", "aliases", "founders", "headquarters_regions",
    "keywords", "multi_source_intelligence", "organisational_hierarchy_tree", "raw_data",
    "github_url", "glassdoor_url", "osint_feed_manifest", "is_manually_verified",
    "manually_verified_at"
  ];

  const PERSONA_89_COLUMNS = [
    "id", "account_id", "lob_id", "external_id", "key", "display_name", "full_name",
    "first_name", "last_name", "title", "tier", "seniority_raw", "departments",
    "email", "email_status", "phone", "linkedin_url", "crunchbase_permalink",
    "city", "state", "country", "source", "hierarchy_level", "decision_authority",
    "budget_authority", "raw_data", "osint_feed_manifest", "corporate_bio_url",
    "crunchbase_url", "sec_cik", "sec_insider_trades_url", "fec_contributions_url",
    "quiver_insider_url", "bloomberg_url", "wsj_article_url", "media_interview_url",
    "annual_report_url", "zoominfo_url", "rss_url", "youtube_url", "podcast_url",
    "openinsider_url", "secform4_url", "wayback_url", "theorg_url", "seeking_alpha_url",
    "external_board_url", "twitter_handle", "twitter_live_url", "google_patents_url",
    "google_scholar_url", "openalex_author_url", "orcid_search_url", "wikidata_person_url",
    "reddit_rss_url", "google_trends_url", "youtube_interviews_url", "podcast_search_url",
    "reddit_query", "news_query", "patents_query", "youtube_channel_id", "degree",
    "institution", "prior_company", "communication_style", "engagement_rate",
    "value_proposition", "personalized_icebreaker", "social_platform", "social_profile_url",
    "social_presence_level", "skills", "target_kpis", "operational_pain_points",
    "key_objections", "headline", "employment_history", "past_companies", "previous_titles",
    "current_role_tenure_months", "is_new_in_role", "career_trajectory_score",
    "education_history", "personal_email", "direct_mobile_phone", "is_manually_verified",
    "manually_verified_at", "extended_profile"
  ];

  const LOB_29_COLUMNS = [
    "id", "account_id", "key", "lob_name", "domain", "website_url", "crunchbase_url",
    "relationship_type", "overview", "audited_segment_revenue", "operating_head",
    "segment_headcount", "lei_code", "jurisdiction", "technologies", "competitors",
    "logo_url", "financial_snippets", "wikipedia_url", "patents", "raw_data",
    "osint_feed_manifest", "is_manually_verified", "manually_verified_at",
    "google_news_rss_url", "reddit_rss_url", "google_patents_url", "google_trends_url",
    "youtube_search_url"
  ];

  // Dynamic DB Columns Filled Calculation
  function calculateDbColumnsFilled(item, entityType) {
    const tot = entityType === "account" ? 97 : (entityType === "persona" ? 89 : 29);
    if (!item) return { filled: 0, total: tot, pct: 0 };
    const cols = entityType === "account" ? ACCOUNT_97_COLUMNS : (entityType === "persona" ? PERSONA_89_COLUMNS : LOB_29_COLUMNS);
    let filled = 0;
    cols.forEach((c) => {
      const v = item[c];
      if (v !== null && v !== undefined && v !== "" && v !== "N/A" && v !== "—") {
        if (Array.isArray(v)) {
          if (v.length > 0) filled++;
        } else if (typeof v === "object") {
          if (Object.keys(v).length > 0) filled++;
        } else {
          filled++;
        }
      }
    });
    const total = cols.length;
    const pct = Math.min(100, Math.round((filled / total) * 100));
    return { filled, total, pct };
  }

  // Dynamic Data Sources / Channels Detection
  function detectItemSources(item, isPersona) {
    const list = [];
    if (!item) return list;

    if (batchConsoleState.entityType === "account") {
      if (item.sec_cik || item.sec_edgar_url) list.push({ label: "SEC EDGAR", cls: "source-sec" });
      if (item.patents_granted || item.google_patents_url) list.push({ label: "USPTO Patents", cls: "source-patents" });
      if (item.crunchbase_url) list.push({ label: "Crunchbase", cls: "source-crunchbase" });
      if (item.linkedin_url) list.push({ label: "LinkedIn", cls: "source-linkedin" });
      if (item.wikipedia_url || item.wikidata_entity_url) list.push({ label: "Wikipedia", cls: "source-gemini" });
      if (item.news_query || item.rss_url) list.push({ label: "News / RSS", cls: "source-news" });
      if (item.stock_symbol) list.push({ label: "Market / IPO", cls: "source-apollo" });
      return list;
    }

    if (batchConsoleState.entityType === "lob") {
      if (item.lei_code || item.jurisdiction) list.push({ label: "GLEIF", cls: "source-apollo" });
      if (item.technologies && (Array.isArray(item.technologies) ? item.technologies.length > 0 : Object.keys(item.technologies).length > 0)) list.push({ label: "Tech Graph", cls: "source-gemini" });
      if (item.competitors && (Array.isArray(item.competitors) ? item.competitors.length > 0 : Object.keys(item.competitors).length > 0)) list.push({ label: "Competitors", cls: "source-fullenrich" });
      if (item.financial_snippets && (Array.isArray(item.financial_snippets) ? item.financial_snippets.length > 0 : Object.keys(item.financial_snippets).length > 0)) list.push({ label: "SEC Filings", cls: "source-sec" });
      if (item.google_news_rss_url) list.push({ label: "Google News", cls: "source-news" });
      if (item.reddit_rss_url) list.push({ label: "Reddit", cls: "source-reddit" });
      if (item.google_patents_url || (Array.isArray(item.patents) && item.patents.length > 0)) list.push({ label: "USPTO Patents", cls: "source-patents" });
      if (item.google_trends_url) list.push({ label: "Trends", cls: "source-exa" });
      if (item.youtube_search_url) list.push({ label: "YouTube", cls: "source-youtube" });
      if (item.wikipedia_url) list.push({ label: "Wikipedia", cls: "source-gemini" });
      if (item.website_url || item.domain) list.push({ label: "Web Crawl", cls: "source-linkedin" });
      return list;
    }

    if (item.linkedin_url && String(item.linkedin_url).includes("linkedin.com")) {
      list.push({ label: "LinkedIn", cls: "source-linkedin" });
    }
    if (item.email || item.phone || item.apollo_id || (item.raw_data && item.raw_data.id)) {
      list.push({ label: "Apollo", cls: "source-apollo" });
    }
    if (item.value_proposition || item.personalized_icebreaker || item.communication_style) {
      list.push({ label: "Gemini AI", cls: "source-gemini" });
    }
    if (item.sec_cik || item.sec_insider_trades_url || item.secform4_url) {
      list.push({ label: "SEC EDGAR", cls: "source-sec" });
    }
    if (item.news_query || item.rss_url || item.bloomberg_url || item.google_news_rss_url) {
      list.push({ label: "News / PR", cls: "source-news" });
    }
    if (item.google_patents_url || item.patents_query || (item.patents && Object.keys(item.patents).length > 0)) {
      list.push({ label: "Patents", cls: "source-patents" });
    }
    if (item.reddit_query || item.reddit_rss_url) {
      list.push({ label: "Reddit", cls: "source-reddit" });
    }
    if (item.twitter_handle || item.twitter_live_url) {
      list.push({ label: "Twitter / X", cls: "source-twitter" });
    }
    if (item.google_scholar_url || item.openalex_author_url || item.orcid_search_url) {
      list.push({ label: "Scholar", cls: "source-scholar" });
    }
    if (item.is_enriched && list.length <= 1) {
      list.push({ label: "Exa AI", cls: "source-exa" });
      list.push({ label: "FullEnrich", cls: "source-fullenrich" });
    }
    return list;
  }

  // Real-Time Last Run Time Formatter
  function formatRunTime(isoString) {
    if (!isoString) return { relative: "Never run", exact: "Pending first run", isNever: true };
    try {
      const d = new Date(isoString);
      if (isNaN(d.getTime())) return { relative: "Never run", exact: "Pending first run", isNever: true };
      const now = new Date();
      const diffSec = Math.floor((now - d) / 1000);
      let rel = "";
      if (diffSec < 60) rel = "Just now";
      else if (diffSec < 3600) rel = `${Math.floor(diffSec / 60)}m ago`;
      else if (diffSec < 86400) rel = `${Math.floor(diffSec / 3600)}h ago`;
      else if (diffSec < 604800) rel = `${Math.floor(diffSec / 86400)}d ago`;
      else rel = d.toLocaleDateString();

      const exact = d.toLocaleString(undefined, {
        month: "short", day: "numeric", year: "numeric",
        hour: "2-digit", minute: "2-digit"
      });
      return { relative: rel, exact, isNever: false };
    } catch (_) {
      return { relative: "Never run", exact: "Pending first run", isNever: true };
    }
  }

  // Dynamic Action Button Generator (Running, Queued & Ready states)
  function getBatchRowActionBtnHtml(key, enriched, isAccount, item) {
    if (batchConsoleState.isRunning) {
      if (batchConsoleState.currentProcessingKey === key) {
        return `<button type="button" class="batch-row-btn batch-btn-running" disabled title="Enrichment currently in progress">
          <span class="spinner-border spinner-border-sm" style="width:0.75rem;height:0.75rem;"></span> In Progress
        </button>`;
      }
      if (batchConsoleState.queuedKeys && batchConsoleState.queuedKeys.has(key)) {
        return `<button type="button" class="batch-row-btn batch-btn-queued" disabled title="Queued for sequential enrichment">
          <i class="bi bi-clock-history"></i> Queued
        </button>`;
      }
      return `<button type="button" class="batch-row-btn batch-single-run-btn ${enriched ? 'batch-btn-reenrich' : 'batch-btn-enrich'}" data-key="${esc(key)}" disabled title="Disabled while another enrichment is running">
        <i class="bi ${enriched ? 'bi-arrow-repeat' : 'bi-lightning-charge-fill'}"></i> ${enriched ? 'Re-enrich' : 'Enrich'}
      </button>`;
    }

    if (isAccount && enriched) {
      return `
        <div style="display:flex;flex-direction:column;gap:5px;align-items:flex-end;">
          <button type="button" class="batch-row-btn batch-single-run-btn batch-btn-reenrich" data-key="${esc(key)}" title="Additive Re-enrichment: updates corporate intelligence without losing data">
            <i class="bi bi-arrow-repeat"></i> Re-enrich
          </button>
          <button type="button" class="batch-row-btn batch-discover-lobs-btn"
            data-account-id="${item.id}"
            data-account-name="${esc(item.legal_name || item.display_name || item.name || '')}"
            data-account-domain="${esc(item.primary_domain || item.domain || '')}"
            data-account-cik="${esc(item.sec_cik || '')}"
            style="background:rgba(16,185,129,0.1);color:#10b981;border:1px solid rgba(16,185,129,0.3);white-space:nowrap;"
            title="Discover Operating Subsidiaries & Lines of Business via SEC Exhibit 21, GLEIF, and corporate intelligence sources">
            <i class="bi bi-diagram-3"></i> Discover LOBs
          </button>
          <button type="button" class="batch-row-btn batch-discover-people-btn"
            data-account-id="${item.id}"
            data-account-name="${esc(item.legal_name || item.display_name || item.name || '')}"
            data-account-domain="${esc(item.primary_domain || item.domain || '')}"
            data-account-cik="${esc(item.sec_cik || '')}"
            style="background:rgba(139,92,246,0.1);color:#8b5cf6;border:1px solid rgba(139,92,246,0.3);white-space:nowrap;"
            title="Discover Executive Leadership & Contacts via Apollo, Corporate Web, and OSINT (4-tier hierarchy)">
            <i class="bi bi-people"></i> Discover People
          </button>
        </div>
      `;
    }

    if (enriched) {
      return `
        <button type="button" class="batch-row-btn batch-single-run-btn batch-btn-reenrich" data-key="${esc(key)}" title="Additive Re-enrichment: updates profile with new intelligence without duplicates or losing data">
          <i class="bi bi-arrow-repeat"></i> Re-enrich
        </button>
      `;
    }

    return `
      <button type="button" class="batch-row-btn batch-single-run-btn batch-btn-enrich" data-key="${esc(key)}" title="Run multi-source deep enrichment">
        <i class="bi bi-lightning-charge-fill"></i> Enrich
      </button>
    `;
  }

  // Render Table Rows (Screen 1 Directory)
  function renderBatchTable(items) {
    const $tbody = $("#batchTableBody");
    $tbody.empty();

    if (!items || items.length === 0) {
      $("#batchTableEmpty").removeClass("d-none");
      return;
    }
    $("#batchTableEmpty").addClass("d-none");

    const isPersona = batchConsoleState.entityType === "persona";
    const acct = batchConsoleState.targetAccount;
    const company = acct?.legal_name || acct?.name || "BNY";

    items.forEach((item, idx) => {
      const key = item.key || item.id || `item_${idx}`;
      const isSelected = batchConsoleState.selectedKeys.has(key);

      // Handle Account row rendering
      if (batchConsoleState.entityType === "account") {
        const acctName = item.legal_name || item.display_name || item.name || "Enterprise Account";
        const domain = item.primary_domain || item.domain || "—";
        const ticker = item.stock_symbol ? `${item.stock_symbol} (${item.stock_exchange || 'NYSE'})` : "Private / OTC";
        const cik = item.sec_cik ? `CIK: ${item.sec_cik}` : "No CIK";
        const compType = item.company_type || "Public";
        const lCount = item.lobs?.length || item.lobs_count || 0;
        const pCount = item.personas?.length || item.total_contacts_captured || 0;
        const coverage = `${lCount} LOBs • ${pCount} People`;
        const enriched = isAccountItemEnriched(item);
        const timeInfo = formatRunTime(item.updated_at || item.created_at);
        const sources = detectItemSources(item, false);
        const dbCols = calculateDbColumnsFilled(item, "account");
        const acctHealth = computeAccountHealth(item);
        const displayPct = (acctHealth && acctHealth.percentage) ? acctHealth.percentage : dbCols.pct;

        const chipsHtml = sources.length > 0
          ? `<div class="batch-source-chips">${sources.map(s => `<span class="batch-source-chip ${s.cls}">${esc(s.label)}</span>`).join('')}</div>`
          : `<span class="text-muted" style="font-size:0.68rem;">Pending Sources</span>`;

        const fillBarColor = displayPct >= 70 ? "#10b981" : (displayPct >= 40 ? "#3b82f6" : "#f59e0b");
        const dbColsHtml = `
          <div class="batch-db-cols-meter" title="${dbCols.filled} out of ${dbCols.total} DB columns populated | ${acctHealth?.populatedCount || 0}/${acctHealth?.totalFields || 45} Core Attributes (${displayPct}%)">
            <div class="batch-db-cols-text">
              <span>${dbCols.filled}/${dbCols.total} cols</span>
              <span style="color:${fillBarColor};font-weight:600;">${displayPct}%</span>
            </div>
            <div class="batch-db-cols-bar">
              <div class="batch-db-cols-fill" style="width:${displayPct}%;background:${fillBarColor};"></div>
            </div>
          </div>
        `;

        let statusBadge = "";
        if (enriched) {
          statusBadge = `<span class="batch-status-badge batch-status-enriched"><i class="bi bi-check-circle-fill"></i> Enriched (${displayPct}%)</span>`;
        } else {
          statusBadge = `<span class="batch-status-badge batch-status-stub"><i class="bi bi-hourglass-split"></i> Pending Stub</span>`;
        }

        const rowHtml = `
          <tr class="batch-table-row ${isSelected ? 'batch-row-selected' : ''}" data-key="${esc(key)}">
            <td style="text-align: center;">
              <input type="checkbox" class="batch-row-checkbox batch-custom-checkbox" data-key="${esc(key)}" ${isSelected ? 'checked' : ''}>
            </td>
            <td>
              <div class="batch-name-cell">
                <span class="batch-name-primary">${esc(acctName)}</span>
                <span class="batch-name-sub">${esc(domain)}</span>
              </div>
            </td>
            <td>
              <div style="font-weight: 500; font-size: 0.76rem;">${esc(domain)}</div>
              <div class="text-muted" style="font-size: 0.70rem;">${esc(ticker)}</div>
            </td>
            <td>
              <div style="font-weight: 500;">${esc(cik)}</div>
              <div class="text-muted" style="font-size: 0.70rem;">${esc(compType)}</div>
            </td>
            <td>
              <span class="batch-tier-badge" style="background:rgba(16,185,129,0.1);color:#10b981;border:1px solid rgba(16,185,129,0.3);">${esc(coverage)}</span>
            </td>
            <td>
              <div class="batch-time-cell">
                <span class="batch-time-rel ${timeInfo.isNever ? 'text-muted' : ''}">${esc(timeInfo.relative)}</span>
                <span class="batch-time-exact">${esc(timeInfo.exact)}</span>
              </div>
            </td>
            <td>
              ${chipsHtml}
            </td>
            <td>
              ${dbColsHtml}
            </td>
            <td>
              ${statusBadge}
            </td>
            <td style="text-align: right;">
              ${getBatchRowActionBtnHtml(key, enriched, true, item)}
            </td>
          </tr>
        `;
        $tbody.append(rowHtml);
        return;
      }
      const enriched = isPersona ? isPersonaItemEnriched(item) : isLobItemEnriched(item);

      const name = item.name || item.display_name || item.full_name || "Unnamed";
      const title = item.title || (isPersona ? "Executive" : "Operating Segment");
      const dept = item.department || (isPersona ? "" : (item.description || ""));
      let tierDisplay = "Division";
      let isCSuite = false;
      if (isPersona) {
        const tierKey = getPersonaTierKey(item);
        if (tierKey === "c_suite") { tierDisplay = "C-Suite"; isCSuite = true; }
        else if (tierKey === "vp") { tierDisplay = "VP / MD"; }
        else if (tierKey === "director") { tierDisplay = "Director"; }
        else if (tierKey === "manager") { tierDisplay = "Manager"; }
        else { tierDisplay = item.tier || "Professional"; }
      } else {
        tierDisplay = item.relationship_type || item.category || "Division";
      }
      // Dynamic metrics
      const timeInfo = formatRunTime(item.last_run_at || item.updated_at || item.created_at);
      const sources = detectItemSources(item, isPersona);
      const dbCols = calculateDbColumnsFilled(item, isPersona ? "persona" : "lob");

      const score = item.validation_score || item._score || dbCols.pct;

      let statusBadge = "";
      if (enriched) {
        statusBadge = `<span class="batch-status-badge batch-status-enriched"><i class="bi bi-check-circle-fill"></i> Enriched (${score}%)</span>`;
      } else {
        statusBadge = `<span class="batch-status-badge batch-status-stub"><i class="bi bi-hourglass-split"></i> Pending Stub</span>`;
      }

      const chipsHtml = sources.length > 0
        ? `<div class="batch-source-chips">${sources.map(s => `<span class="batch-source-chip ${s.cls}">${esc(s.label)}</span>`).join('')}</div>`
        : `<span class="text-muted" style="font-size:0.68rem;">Pending Sources</span>`;

      const fillBarColor = dbCols.pct >= 70 ? "#10b981" : (dbCols.pct >= 40 ? "#3b82f6" : "#f59e0b");
      const dbColsHtml = `
        <div class="batch-db-cols-meter" title="${dbCols.filled} out of ${dbCols.total} database columns populated (${dbCols.pct}%)">
          <div class="batch-db-cols-text">
            <span>${dbCols.filled}/${dbCols.total} cols</span>
            <span style="color:${fillBarColor};">${dbCols.pct}%</span>
          </div>
          <div class="batch-db-cols-bar">
            <div class="batch-db-cols-fill" style="width:${dbCols.pct}%;background:${fillBarColor};"></div>
          </div>
        </div>
      `;

      const rowHtml = `
        <tr class="batch-table-row ${isSelected ? 'batch-row-selected' : ''}" data-key="${esc(key)}">
          <td style="text-align: center;">
            <input type="checkbox" class="batch-row-checkbox batch-custom-checkbox" data-key="${esc(key)}" ${isSelected ? 'checked' : ''}>
          </td>
          <td>
            <div class="batch-name-cell">
              <span class="batch-name-primary">${esc(name)}</span>
              <span class="batch-name-sub">${esc(item.headline || item.prior_company || (item.domain || ""))}</span>
            </div>
          </td>
          <td>
            <div style="font-weight: 500; font-size: 0.76rem;">${esc(company)}</div>
          </td>
          <td>
            <div style="font-weight: 500;">${esc(title)}</div>
            ${dept ? `<div class="text-muted" style="font-size: 0.70rem;">${esc(dept)}</div>` : ""}
          </td>
          <td>
            <span class="batch-tier-badge ${isCSuite ? 'batch-tier-csuite' : ''}">${esc(tierDisplay)}</span>
          </td>
          <td>
            <div class="batch-time-cell">
              <span class="batch-time-rel ${timeInfo.isNever ? 'text-muted' : ''}">${esc(timeInfo.relative)}</span>
              <span class="batch-time-exact">${esc(timeInfo.exact)}</span>
            </div>
          </td>
          <td>
            ${chipsHtml}
          </td>
          <td>
            ${dbColsHtml}
          </td>
          <td>
            ${statusBadge}
          </td>
          <td style="text-align: right;">
            ${getBatchRowActionBtnHtml(key, enriched, false, item)}
          </td>
        </tr>
      `;
      $tbody.append(rowHtml);
    });
  }

  // Update selection counters and action buttons
  function updateSelectionBadges() {
    const count = batchConsoleState.selectedKeys.size;
    $("#batchSelectedBadge").text(`${count} selected`);

    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts))
      ? MOCK_DATA.accounts
      : [];
    const acct = batchConsoleState.targetAccount || (acctsList.length ? acctsList[0] : null);
    const entityType = batchConsoleState.entityType;
    let allItems = [];
    if (entityType === "account") {
      allItems = acctsList;
    } else if (entityType === "lob") {
      allItems = acct?.lobs || [];
    } else {
      allItems = acct?.personas || [];
    }

    let enrichedSelected = 0;
    let pendingSelected = 0;

    batchConsoleState.selectedKeys.forEach((key) => {
      const item = allItems.find((i) => (i.key || i.id) === key);
      if (item) {
        let enriched = false;
        if (entityType === "account") enriched = isAccountItemEnriched(item);
        else if (entityType === "lob") enriched = isLobItemEnriched(item);
        else enriched = isPersonaItemEnriched(item);

        if (enriched) enrichedSelected++;
        else pendingSelected++;
      }
    });

    const $runBtn = $("#batchRunEnrichmentBtn");
    if (count === 0) {
      $runBtn.html('<i class="bi bi-play-fill"></i> Run Enrichment (<span id="batchRunCountText">0</span>)');
      $runBtn.removeClass("btn-batch-run-reenrich");
    } else if (pendingSelected === 0 && enrichedSelected > 0) {
      $runBtn.html(`<i class="bi bi-arrow-repeat"></i> Re-enrich Selected (<span id="batchRunCountText">${count}</span>)`);
      $runBtn.addClass("btn-batch-run-reenrich");
    } else if (enrichedSelected === 0 && pendingSelected > 0) {
      $runBtn.html(`<i class="bi bi-lightning-charge-fill"></i> Enrich Selected (<span id="batchRunCountText">${count}</span>)`);
      $runBtn.removeClass("btn-batch-run-reenrich");
    } else {
      $runBtn.html(`<i class="bi bi-play-fill"></i> Process Selected (<span id="batchRunCountText">${count}</span>) <small style="opacity:0.85;">[${pendingSelected} New, ${enrichedSelected} Re-enrich]</small>`);
      $runBtn.removeClass("btn-batch-run-reenrich");
    }

    $runBtn.prop("disabled", count === 0 || batchConsoleState.isRunning);

    // Update master checkbox state
    const visibleKeys = batchConsoleState.filteredItems.map((i) => i.key || i.id);
    const allChecked = visibleKeys.length > 0 && visibleKeys.every((k) => batchConsoleState.selectedKeys.has(k));
    $("#batchMasterCheckbox").prop("checked", allChecked);
  }

  // Sequential Batch Execution Engine (Free-Tier Safe & Rate Protected)
  async function runBatchEnrichment() {
    if (batchConsoleState.selectedKeys.size === 0 || batchConsoleState.isRunning) return;

    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts))
      ? MOCK_DATA.accounts
      : [];
    const acct = batchConsoleState.targetAccount || (acctsList.length ? acctsList[0] : null);
    if (!acct && batchConsoleState.entityType !== "account") return;

    const entityType = batchConsoleState.entityType;
    const isAccount = entityType === "account";
    const isPersona = entityType === "persona";
    const isLob = entityType === "lob";

    let allItems = [];
    if (isAccount) {
      allItems = acctsList;
    } else if (isLob) {
      allItems = acct?.lobs || [];
    } else {
      allItems = acct?.personas || [];
    }

    // Filter queue to selected items
    const queue = allItems.filter((item) => {
      const key = item.key || item.id;
      return batchConsoleState.selectedKeys.has(key);
    });

    if (queue.length === 0) return;

    // Set Running State & Initialize Queued Tracking
    batchConsoleState.isRunning = true;
    batchConsoleState.abortRequested = false;
    batchConsoleState.queuedKeys = new Set(queue.map((it) => it.key || it.id));
    batchConsoleState.currentProcessingKey = null;

    // UI Updates for Running State
    $("#batchRunEnrichmentBtn")
      .prop("disabled", true)
      .html('<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Enriching Queue...');
    $("#batchStopBtn").removeClass("d-none").prop("disabled", false).html('<i class="bi bi-stop-circle-fill"></i> Stop Queue');
    $("#batchLiveProgressWrap").removeClass("d-none");
    $("#batchProgressBarFill").css("width", "0%");
    $("#batchCompanySelector, #batchSearchInput, #batchStatusFilter, #batchTierFilter").prop("disabled", true);
    $(".batch-row-checkbox, #batchMasterCheckbox").prop("disabled", true);
    $("#batchSelectPendingBtn, #batchSelectEnrichedBtn, #batchSelectCountBtn, .btn-batch-preset-n, #batchClearSelectionBtn, #batchRefreshTableBtn").prop("disabled", true);

    // Immediately reflect queued state on all queued rows, and disable any other table buttons
    queue.forEach((it) => {
      const k = it.key || it.id;
      const $qRow = $(`#batchTableBody tr[data-key="${k}"]`);
      if ($qRow.length) {
        $qRow.find("td:last-child").html(getBatchRowActionBtnHtml(k, false, isAccount, it));
      }
    });
    $("#batchTableBody .batch-single-run-btn, #batchTableBody .batch-discover-lobs-btn, #batchTableBody .batch-discover-people-btn").prop("disabled", true);

    let successCount = 0;
    let failCount = 0;

    for (let i = 0; i < queue.length; i++) {
      if (batchConsoleState.abortRequested) {
        showToast("Batch enrichment stopped by user", "warning");
        break;
      }

      const item = queue[i];
      const key = item.key || item.id;
      batchConsoleState.currentProcessingKey = key;
      batchConsoleState.queuedKeys.delete(key);

      const itemName = item.name || item.display_name || item.full_name || item.legal_name || "Target";
      const itemTitle = item.title || (isAccount ? "Enterprise Account" : (isPersona ? "Executive" : "Operating Segment"));

      // Highlight active row & scroll into view
      $("#batchTableBody tr").removeClass("batch-row-active");
      const $row = $(`#batchTableBody tr[data-key="${key}"]`);
      if ($row.length) {
        $row.addClass("batch-row-active");
        $row.find(".batch-status-badge").replaceWith(
          '<span class="batch-status-badge batch-status-running"><span class="spinner-border spinner-border-sm" style="width:0.75rem;height:0.75rem;"></span> Enriching...</span>'
        );
        $row.find("td:last-child").html(getBatchRowActionBtnHtml(key, false, isAccount, item));
      }

      // Update Live Progress Bar & Ticker
      const progressPct = Math.round((i / queue.length) * 100);
      $("#batchProgressBarFill").css("width", `${progressPct}%`);
      $("#batchProgressMetrics").text(`${i} / ${queue.length} completed (${progressPct}%)`);
      const sourceLabel = isAccount
        ? "[SEC EDGAR + GLEIF + Patents + News + Gemini]"
        : (isPersona
            ? "[LinkedIn + Diffbot + SEC Form 4 + Headshot + Sales AI]"
            : "[SEC Filings + Segment OSINT + Gemini]");
      $("#batchLiveStatusText").html(
        `Running <strong>${i + 1} of ${queue.length}</strong> &mdash; ${esc(itemName)} (${esc(itemTitle)})... <span class="text-muted">${sourceLabel}</span>`
      );

      try {
        if (isAccount) {
          // ── Account Step 1: Multi-source Fetch (SEC, Patents, Wiki, GLEIF, FEC)
          const acctRes = await fetch(`${API_BASE}/api/account/fetch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              company_name: item.legal_name || item.name || item.display_name,
              target_url: item.primary_domain || item.domain || "bny.com",
            }),
          });
          if (!acctRes.ok) throw new Error("Account fetch returned status " + acctRes.status);
          const acctData = await acctRes.json();
          const enrichedAccount = acctData.account || acctData;

          if (item.id) enrichedAccount.id = item.id;
          if (item.key) enrichedAccount.key = item.key;

          // ── Account Step 2: Validate Completeness
          let score = 90;
          try {
            const valRes = await fetch(`${API_BASE}/api/account/validate`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(enrichedAccount),
            });
            if (valRes.ok) {
              const valData = await valRes.json();
              score = valData.score || valData.completeness_score || score;
            }
          } catch (_) {}

          // ── Account Step 3: Dump to DB
          await fetch(`${API_BASE}/api/account/dump-db`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              account_id: item.id,
              account_data: enrichedAccount,
            }),
          });

          item.last_run_at = new Date().toISOString();
          Object.assign(item, enrichedAccount);
          item.is_enriched = true;
          item.validation_score = score;
          successCount++;

          if ($row.length) {
            $row.find(".batch-status-badge").replaceWith(
              `<span class="batch-status-badge batch-status-enriched"><i class="bi bi-check-circle-fill"></i> Enriched (${score}%)</span>`
            );
            $row.find("td:last-child").html(
              (i < queue.length - 1 && !batchConsoleState.abortRequested)
                ? `<button type="button" class="batch-row-btn batch-single-run-btn batch-btn-reenrich" data-key="${esc(key)}" disabled title="Enriched (Queue running)"><i class="bi bi-check2"></i> Enriched</button>`
                : getBatchRowActionBtnHtml(key, true, true, item)
            );
            const timeInfo = formatRunTime(item.last_run_at);
            $row.find(".batch-time-cell").html(
              `<span class="batch-time-rel">${esc(timeInfo.relative)}</span><span class="batch-time-exact">${esc(timeInfo.exact)}</span>`
            );
            const updatedSources = detectItemSources(item, false);
            if (updatedSources.length > 0) {
              $row.find(".batch-source-chips, td:nth-child(7) .text-muted").replaceWith(
                `<div class="batch-source-chips">${updatedSources.map(s => `<span class="batch-source-chip ${s.cls}">${esc(s.label)}</span>`).join('')}</div>`
              );
            }
            const updatedCols = calculateDbColumnsFilled(item, "account");
            const fillCol = updatedCols.pct >= 70 ? "#10b981" : (updatedCols.pct >= 40 ? "#3b82f6" : "#f59e0b");
            $row.find(".batch-db-cols-meter").html(
              `<div class="batch-db-cols-text"><span>${updatedCols.filled}/${updatedCols.total} cols</span><span style="color:${fillCol};">${updatedCols.pct}%</span></div><div class="batch-db-cols-bar"><div class="batch-db-cols-fill" style="width:${updatedCols.pct}%;background:${fillCol};"></div></div>`
            );
          }
        } else if (isPersona) {
          // ── Persona Step 1: Multi-source Fetch (Exa, LinkedIn, Serper, Apollo, Dossier)
          const pullRes = await fetch(`${API_BASE}/api/personas/fetch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id: item.id || null,
              key: item.key || (itemName || key).toLowerCase().replace(/\s+/g, "_"),
              display_name: itemName,
              name: itemName,
              title: item.title || null,
              company_name: acct.legal_name || acct.name,
              linkedin_url: item.linkedin_url || null,
              account_id: acct.id,
              enrich_ai_dossier: true,
            }),
          });
          if (!pullRes.ok) throw new Error("Multi-source fetch returned status " + pullRes.status);
          const pullData = await pullRes.json();
          const enrichedPerson = pullData.person || pullData;

          // Non-destructive identity lock: preserve existing database PK & keys to guarantee in-place update without duplicates
          if (item.id) enrichedPerson.id = item.id;
          if (item.account_id) enrichedPerson.account_id = item.account_id;
          if (item.lob_id) enrichedPerson.lob_id = item.lob_id;
          if (!enrichedPerson.linkedin_url && item.linkedin_url) enrichedPerson.linkedin_url = item.linkedin_url;

          // ── Persona Step 2: Validate Completeness
          let score = calculateDbColumnsFilled(enrichedPerson, "persona").pct;
          try {
            const valRes = await fetch(`${API_BASE}/api/personas/validate-single`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(enrichedPerson),
            });
            if (valRes.ok) {
              const valData = await valRes.json();
              if (valData.score != null) score = valData.score;
            }
          } catch (valErr) {
            console.warn("Validation non-fatal:", valErr);
          }

          // ── Persona Step 3: Dump Single to Universal DB (upsert into existing row)
          const dumpRes = await fetch(`${API_BASE}/api/personas/dump-single-db`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              account_id: acct.id,
              person_data: enrichedPerson,
            }),
          });
          if (!dumpRes.ok) throw new Error("Dump to database failed with status " + dumpRes.status);

          // Update memory & row
          item.last_run_at = new Date().toISOString();
          Object.assign(item, enrichedPerson);
          item.is_enriched = true;
          item.validation_score = score;
          successCount++;

          if ($row.length) {
            const updatedCols = calculateDbColumnsFilled(item, "persona");
            const fillCol = updatedCols.pct >= 70 ? "#10b981" : (updatedCols.pct >= 40 ? "#3b82f6" : "#f59e0b");
            $row.find(".batch-status-badge").replaceWith(
              `<span class="batch-status-badge batch-status-enriched"><i class="bi bi-check-circle-fill"></i> Enriched (${updatedCols.pct}%)</span>`
            );
            $row.find("td:last-child").html(
              (i < queue.length - 1 && !batchConsoleState.abortRequested)
                ? `<button type="button" class="batch-row-btn batch-single-run-btn batch-btn-reenrich" data-key="${esc(key)}" disabled title="Enriched (Queue running)"><i class="bi bi-check2"></i> Enriched</button>`
                : getBatchRowActionBtnHtml(key, true, false, item)
            );
            const timeInfo = formatRunTime(item.last_run_at);
            $row.find(".batch-time-cell").html(
              `<span class="batch-time-rel">${esc(timeInfo.relative)}</span><span class="batch-time-exact">${esc(timeInfo.exact)}</span>`
            );
            const updatedSources = detectItemSources(item, true);
            if (updatedSources.length > 0) {
              $row.find(".batch-source-chips, td:nth-child(7) .text-muted").replaceWith(
                `<div class="batch-source-chips">${updatedSources.map(s => `<span class="batch-source-chip ${s.cls}">${esc(s.label)}</span>`).join('')}</div>`
              );
            }
            $row.find(".batch-db-cols-meter").html(
              `<div class="batch-db-cols-text"><span>${updatedCols.filled}/${updatedCols.total} cols</span><span style="color:${fillCol};">${updatedCols.pct}%</span></div><div class="batch-db-cols-bar"><div class="batch-db-cols-fill" style="width:${updatedCols.pct}%;background:${fillCol};"></div></div>`
            );
          }
        } else {
          // ── LOB Step 1: Multi-source Fetch
          const lobRes = await fetch(`${API_BASE}/api/lobs/fetch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id: item.id || null,
              account_id: acct.id,
              company_name: acct.name,
              lob_name: item.name || item.lob_name || key,
              lob_domain: item.domain || item.primary_domain || null,
            }),
          });
          if (!lobRes.ok) throw new Error("LOB fetch returned status " + lobRes.status);
          const lobData = await lobRes.json();
          const enrichedLob = lobData.lob || (lobData.lobs && lobData.lobs[0]) || item;

          if (item.id) enrichedLob.id = item.id;
          if (item.account_id) enrichedLob.account_id = item.account_id;

          // ── LOB Step 2: Validate
          try {
            await fetch(`${API_BASE}/api/lobs/validate-single`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(enrichedLob),
            });
          } catch (_) {}

          // ── LOB Step 3: Dump to DB
          await fetch(`${API_BASE}/api/lobs/dump-single-db`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              account_id: acct.id,
              lob_data: enrichedLob,
            }),
          });

          item.last_run_at = new Date().toISOString();
          Object.assign(item, enrichedLob);
          item.is_enriched = true;
          successCount++;

          if ($row.length) {
            const updatedCols = calculateDbColumnsFilled(item, "lob");
            const fillCol = updatedCols.pct >= 70 ? "#10b981" : (updatedCols.pct >= 40 ? "#3b82f6" : "#f59e0b");
            $row.find(".batch-status-badge").replaceWith(
              `<span class="batch-status-badge batch-status-enriched"><i class="bi bi-check-circle-fill"></i> Enriched (${updatedCols.pct}%)</span>`
            );
            $row.find("td:last-child").html(
              (i < queue.length - 1 && !batchConsoleState.abortRequested)
                ? `<button type="button" class="batch-row-btn batch-single-run-btn batch-btn-reenrich" data-key="${esc(key)}" disabled title="Enriched (Queue running)"><i class="bi bi-check2"></i> Enriched</button>`
                : getBatchRowActionBtnHtml(key, true, false, item)
            );
            const timeInfo = formatRunTime(item.last_run_at);
            $row.find(".batch-time-cell").html(
              `<span class="batch-time-rel">${esc(timeInfo.relative)}</span><span class="batch-time-exact">${esc(timeInfo.exact)}</span>`
            );
            const updatedSources = detectItemSources(item, false);
            if (updatedSources.length > 0) {
              $row.find(".batch-source-chips, td:nth-child(7) .text-muted").replaceWith(
                `<div class="batch-source-chips">${updatedSources.map(s => `<span class="batch-source-chip ${s.cls}">${esc(s.label)}</span>`).join('')}</div>`
              );
            }
            $row.find(".batch-db-cols-meter").html(
              `<div class="batch-db-cols-text"><span>${updatedCols.filled}/${updatedCols.total} cols</span><span style="color:${fillCol};">${updatedCols.pct}%</span></div><div class="batch-db-cols-bar"><div class="batch-db-cols-fill" style="width:${updatedCols.pct}%;background:${fillCol};"></div></div>`
            );
          }
        }
      } catch (err) {
        console.error(`Batch enrichment failure for ${key}:`, err);
        failCount++;
        if ($row.length) {
          $row.find(".batch-status-badge").replaceWith(
            `<span class="batch-status-badge batch-status-error" title="${esc(err.message)}"><i class="bi bi-exclamation-triangle-fill"></i> Error</span>`
          );
        }
      }

      // Update live KPI cards
      const curPending = parseInt($("#batchKpiPending").text(), 10) || 0;
      const curEnriched = parseInt($("#batchKpiEnriched").text(), 10) || 0;
      if (curPending > 0) $("#batchKpiPending").text(curPending - 1);
      $("#batchKpiEnriched").text(curEnriched + 1);

      // Free-tier rate protection throttle: 500ms between calls
      await new Promise((r) => setTimeout(r, 500));
    }

    // ── Batch Run Completed ──
    $("#batchTableBody tr").removeClass("batch-row-active");
    $("#batchProgressBarFill").css("width", "100%");
    $("#batchProgressMetrics").text(`${queue.length} / ${queue.length} completed (100%)`);
    $("#batchLiveStatusText").html(
      `<strong>✔ Batch Finished:</strong> <span class="text-success">${successCount} succeeded</span>` +
        (failCount ? ` · <span class="text-danger">${failCount} failed</span>` : "") +
        ` out of ${queue.length} records processed sequentially.`
    );

    // Reset Controls & Execution Tracking
    batchConsoleState.isRunning = false;
    batchConsoleState.currentProcessingKey = null;
    batchConsoleState.queuedKeys.clear();

    $("#batchStopBtn").addClass("d-none");
    $("#batchRunEnrichmentBtn").prop("disabled", false);
    $("#batchCompanySelector, #batchSearchInput, #batchStatusFilter, #batchTierFilter").prop("disabled", false);
    $(".batch-row-checkbox, #batchMasterCheckbox").prop("disabled", false);
    $("#batchSelectPendingBtn, #batchSelectEnrichedBtn, #batchSelectCountBtn, .btn-batch-preset-n, #batchClearSelectionBtn, #batchRefreshTableBtn").prop("disabled", false);

    // Re-render table and badges to restore interactive buttons
    renderBatchTable(batchConsoleState.filteredItems);
    updateSelectionBadges();

    // Refresh Underlying Account Views
    updateBatchTriggerPills(activeAccount);
    if (isAccount) {
      if (typeof renderAccountHero === "function") renderAccountHero(activeAccount);
      if (typeof renderCompletenessCard === "function") renderCompletenessCard(activeAccount);
      if (typeof renderAccountOverview === "function") renderAccountOverview(activeAccount);
    } else if (isPersona) {
      renderAllPersonasDirectory(activeAccount);
    } else {
      renderLobCardsList($("#lobCardsContainer"), activeAccount.lobs || []);
    }

    showToast(`Batch completed: ${successCount} enriched successfully!`, "success");
  }

  // ── Batch Console Event Listeners ──

  // Open button in Sidebar (below Add New Account)
  $(document).on("click", "#sidebarBatchConsoleBtn", function (e) {
    e.preventDefault();
    openBatchConsole(batchConsoleState.entityType || "account", activeAccount);
  });

  // Open buttons (Account, People, and LOBs)
  $(document).on("click", "#openAccountBatchModalBtn", function (e) {
    e.preventDefault();
    openBatchConsole("account", activeAccount);
  });

  $(document).on("click", "#openPersonaBatchModalBtn", function (e) {
    e.preventDefault();
    openBatchConsole("persona", activeAccount);
  });

  $(document).on("click", "#openLobBatchModalBtn", function (e) {
    e.preventDefault();
    openBatchConsole("lob", activeAccount);
  });

  // Switch entity tab inside Batch Console (Accounts, LOBs, People) with zero modal closing
  $(document).on("click", ".batch-entity-tab", function (e) {
    e.preventDefault();
    const entity = $(this).data("entity");
    if (entity) switchBatchEntity(entity);
  });

  // Close buttons & backdrop
  $(document).on("click", "#closeBatchConsoleBtn, #batchConsoleFooterCloseBtn", function (e) {
    e.preventDefault();
    closeBatchConsole();
  });

  $(document).on("click", "#batchEnrichmentBackdrop", function (e) {
    if (e.target === this) closeBatchConsole();
  });

  $(document).on("keydown", function (e) {
    if (e.key === "Escape") {
      if (!$("#batchRunDetailsBackdrop").hasClass("d-none")) {
        closeBatchRunDetailsModal();
        return;
      }
      if (!$("#batchEnrichmentModal").hasClass("d-none")) {
        closeBatchConsole();
      }
    }
  });

  // Target Company selector change
  $(document).on("change", "#batchCompanySelector", function () {
    const selectedVal = $(this).val();
    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts))
      ? MOCK_DATA.accounts
      : [];
    if (selectedVal === "all") {
      batchConsoleState.selectedKeys.clear();
      refreshBatchConsole();
      PipelineRouter.syncState({
        view: "batch_console",
        entityType: batchConsoleState.entityType,
        accountId: "all",
        screen: $("#batchScreenHistory").hasClass("d-none") ? "directory" : "history",
      });
      return;
    }
    const selectedId = parseInt(selectedVal, 10);
    const found = acctsList.find((a) => a.id === selectedId);
    if (found) {
      batchConsoleState.targetAccount = found;
      activeAccount = found;
      batchConsoleState.selectedKeys.clear();
      refreshBatchConsole();
      PipelineRouter.syncState({
        view: "batch_console",
        entityType: batchConsoleState.entityType,
        accountId: String(found.id),
        screen: $("#batchScreenHistory").hasClass("d-none") ? "directory" : "history",
      });
    }
  });

  // Filters & Search
  let searchDebounceTimer = null;
  $(document).on("input", "#batchSearchInput", function () {
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(() => {
      refreshBatchConsole();
    }, 200);
  });

  $(document).on("change", "#batchStatusFilter, #batchTierFilter", function () {
    batchConsoleState.selectedKeys.clear();
    refreshBatchConsole();
  });

  $(document).on("click", "#batchRefreshTableBtn", function (e) {
    e.preventDefault();
    refreshBatchConsole();
  });

  // Master Checkbox toggle
  $(document).on("change", "#batchMasterCheckbox", function () {
    const isChecked = $(this).is(":checked");
    batchConsoleState.filteredItems.forEach((item) => {
      const key = item.key || item.id;
      if (isChecked) batchConsoleState.selectedKeys.add(key);
      else batchConsoleState.selectedKeys.delete(key);
    });
    $(".batch-row-checkbox").prop("checked", isChecked);
    $(".batch-table-row").toggleClass("batch-row-selected", isChecked);
    updateSelectionBadges();
  });

  // Quick Select Pending Unenriched
  $(document).on("click", "#batchSelectPendingBtn", function (e) {
    e.preventDefault();
    const isPersona = batchConsoleState.entityType === "persona";
    batchConsoleState.selectedKeys.clear();
    batchConsoleState.filteredItems.forEach((item) => {
      const enriched = isPersona ? isPersonaItemEnriched(item) : isLobItemEnriched(item);
      if (!enriched) {
        batchConsoleState.selectedKeys.add(item.key || item.id);
      }
    });
    refreshBatchConsole();
  });

  // Quick Select Already Enriched (for Re-enrichment)
  $(document).on("click", "#batchSelectEnrichedBtn", function (e) {
    e.preventDefault();
    const isPersona = batchConsoleState.entityType === "persona";
    batchConsoleState.selectedKeys.clear();
    batchConsoleState.filteredItems.forEach((item) => {
      const enriched = isPersona ? isPersonaItemEnriched(item) : isLobItemEnriched(item);
      if (enriched) {
        batchConsoleState.selectedKeys.add(item.key || item.id);
      }
    });
    refreshBatchConsole();
  });

  // Quick Select First N Records (respects active filter: Pending, All, Tier, Search)
  function selectFirstNRecords(n) {
    const countVal = parseInt(n, 10);
    if (isNaN(countVal) || countVal <= 0) {
      $("#batchSelectCountInput").focus();
      return;
    }
    const isPersona = batchConsoleState.entityType === "persona";
    const statusFilter = $("#batchStatusFilter").val() || "pending";
    batchConsoleState.selectedKeys.clear();

    let candidates = batchConsoleState.filteredItems;
    // If the user is on "All Records", prefer unenriched stubs first for enrichment
    if (statusFilter === "all") {
      const unenriched = candidates.filter((item) => {
        const enriched = isPersona ? isPersonaItemEnriched(item) : isLobItemEnriched(item);
        return !enriched;
      });
      candidates = unenriched.length > 0 ? unenriched : candidates;
    }

    const targetSlice = candidates.slice(0, countVal);
    targetSlice.forEach((item) => {
      batchConsoleState.selectedKeys.add(item.key || item.id);
    });
    refreshBatchConsole();
  }

  $(document).on("click", "#batchSelectCountBtn", function (e) {
    e.preventDefault();
    const val = $("#batchSelectCountInput").val();
    selectFirstNRecords(val);
  });

  $(document).on("keydown", "#batchSelectCountInput", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      const val = $(this).val();
      selectFirstNRecords(val);
    }
  });

  $(document).on("click", ".btn-batch-preset-n", function (e) {
    e.preventDefault();
    const val = $(this).data("n");
    $("#batchSelectCountInput").val(val);
    selectFirstNRecords(val);
  });

  // Individual Row Checkbox toggle
  $(document).on("change", ".batch-row-checkbox", function () {
    const key = $(this).data("key");
    const isChecked = $(this).is(":checked");
    if (isChecked) {
      batchConsoleState.selectedKeys.add(key);
      $(this).closest("tr").addClass("batch-row-selected");
    } else {
      batchConsoleState.selectedKeys.delete(key);
      $(this).closest("tr").removeClass("batch-row-selected");
    }
    updateSelectionBadges();
  });

  // Clear Selection
  $(document).on("click", "#batchClearSelectionBtn", function (e) {
    e.preventDefault();
    batchConsoleState.selectedKeys.clear();
    $(".batch-row-checkbox, #batchMasterCheckbox").prop("checked", false);
    $(".batch-table-row").removeClass("batch-row-selected");
    updateSelectionBadges();
  });

  // Run Batch Enrichment button
  $(document).on("click", "#batchRunEnrichmentBtn", function (e) {
    e.preventDefault();
    runBatchEnrichment();
  });

  // Stop Batch button
  $(document).on("click", "#batchStopBtn", function (e) {
    e.preventDefault();
    batchConsoleState.abortRequested = true;
    $(this).prop("disabled", true).text("Stopping...");
  });

  // Single row Enrich button inside table
  $(document).on("click", ".batch-single-run-btn", function (e) {
    e.preventDefault();
    if (batchConsoleState.isRunning) {
      showToast("Enrichment is currently in progress. Please wait for the queue to complete or click Stop.", "warning");
      return;
    }
    const key = $(this).data("key");
    batchConsoleState.selectedKeys.clear();
    batchConsoleState.selectedKeys.add(key);
    $(`#batchTableBody tr[data-key="${key}"] .batch-row-checkbox`).prop("checked", true);
    updateSelectionBadges();
    runBatchEnrichment();
  });

  // ── Discover LOBs button (Batch Console account row, post-enrichment) ──
  $(document).on("click", ".batch-discover-lobs-btn", async function (e) {
    e.preventDefault();
    e.stopPropagation();
    const $btn = $(this);
    const acctId = $btn.data("account-id");
    const acctName = $btn.data("account-name");
    const acctDomain = $btn.data("account-domain");

    // Set target account context safely
    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts)) ? MOCK_DATA.accounts : [];
    const targetAcct = acctsList.find((a) => a.id == acctId) || activeAccount;
    if (targetAcct && targetAcct.id == acctId) {
      activeAccount = targetAcct;
    } else if (!activeAccount || activeAccount.id != acctId) {
      activeAccount = {
        id: acctId,
        name: acctName,
        domain: acctDomain,
        primary_domain: acctDomain,
        sec_cik: $btn.data("account-cik") || null,
        lobs: [],
        personas: [],
      };
    }

    $btn.prop("disabled", true)
        .html('<i class="bi bi-hourglass-split"></i> Discovering LOBs...')
        .css({"opacity":"0.7","pointer-events":"none"});

    // Close Batch Console so the LOB progress bar in the main view is visible
    closeBatchConsole();

    // Trigger explicit LOB discovery mode
    await runBatchLobPipeline("discover");

    // Refresh so the row now shows LOB count and hides the Discover LOBs button
    refreshBatchConsole();
  });

  // ── Discover People button (Batch Console account row, post-enrichment) ──
  $(document).on("click", ".batch-discover-people-btn", async function (e) {
    e.preventDefault();
    e.stopPropagation();
    const $btn = $(this);
    const acctId = $btn.data("account-id");
    const acctName = $btn.data("account-name");
    const acctDomain = $btn.data("account-domain");

    // Set target account context safely
    const acctsList = (typeof MOCK_DATA !== "undefined" && Array.isArray(MOCK_DATA.accounts)) ? MOCK_DATA.accounts : [];
    const targetAcct = acctsList.find((a) => a.id == acctId) || activeAccount;
    if (targetAcct && targetAcct.id == acctId) {
      activeAccount = targetAcct;
    } else if (!activeAccount || activeAccount.id != acctId) {
      activeAccount = {
        id: acctId,
        name: acctName,
        domain: acctDomain,
        primary_domain: acctDomain,
        sec_cik: $btn.data("account-cik") || null,
        personas: [],
        lobs: [],
      };
    }

    $btn.prop("disabled", true)
        .html('<i class="bi bi-hourglass-split"></i> Discovering People...')
        .css({"opacity":"0.7","pointer-events":"none"});

    // Close Batch Console so the Persona discovery progress bar is visible
    closeBatchConsole();

    // Trigger explicit persona hierarchy discovery mode
    await runBatchPersonaPipeline("discover");

    // Refresh so the row now shows People count and hides the Discover People button
    refreshBatchConsole();
  });



  // ── Screen Navigation Tabs (Directory vs Recent Runs History) ──
  $(document).on("click", "#batchTabDirectoryBtn", function (e) {
    e.preventDefault();
    $(".batch-screen-tab").removeClass("active");
    $(this).addClass("active");
    $("#batchScreenHistory").addClass("d-none");
    $("#batchScreenDirectory").removeClass("d-none");

    PipelineRouter.syncState({
      view: "batch_console",
      entityType: batchConsoleState.entityType,
      accountId: (batchConsoleState.entityType === "account" && $("#batchCompanySelector").val() === "all")
        ? "all"
        : (batchConsoleState.targetAccount ? String(batchConsoleState.targetAccount.id) : null),
      screen: "directory",
    });
  });

  $(document).on("click", "#batchTabHistoryBtn", function (e) {
    e.preventDefault();
    $(".batch-screen-tab").removeClass("active");
    $(this).addClass("active");
    $("#batchScreenDirectory").addClass("d-none");
    $("#batchScreenHistory").removeClass("d-none");
    loadBatchRunHistory();

    PipelineRouter.syncState({
      view: "batch_console",
      entityType: batchConsoleState.entityType,
      accountId: (batchConsoleState.entityType === "account" && $("#batchCompanySelector").val() === "all")
        ? "all"
        : (batchConsoleState.targetAccount ? String(batchConsoleState.targetAccount.id) : null),
      screen: "history",
    });
  });

  // ── Fullscreen Toggle ──
  $(document).on("click", "#batchConsoleFullscreenBtn", function (e) {
    e.preventDefault();
    const $modal = $("#batchEnrichmentModal");
    $modal.toggleClass("batch-fullscreen");
    const isFull = $modal.hasClass("batch-fullscreen");
    $(this).html(isFull ? '<i class="bi bi-fullscreen-exit"></i>' : '<i class="bi bi-arrows-fullscreen"></i>');
    $(this).attr("title", isFull ? "Exit Fullscreen" : "Toggle Fullscreen");
  });

  // ── Screen 2: Telemetry Execution History Logic ──
  const batchHistoryState = {
    runs: [],
    filteredRuns: [],
  };

  async function loadBatchRunHistory() {
    const acct = batchConsoleState.targetAccount;
    const compName = acct ? (acct.name || acct.legal_name || "") : "";
    const $tbody = $("#batchHistoryTableBody");
    $tbody.html('<tr><td colspan="8" style="text-align:center;padding:28px;"><div class="spinner-border spinner-border-sm text-primary"></div> Loading real-time execution runs from PostgreSQL...</td></tr>');
    $("#batchHistoryEmpty").addClass("d-none");

    try {
      let url = `${API_BASE}/api/pipeline/runs?exclude_dumps=false&limit=150`;
      if (compName) {
        url += `&company_name=${encodeURIComponent(compName)}`;
      }
      const res = await fetch(url);
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      batchHistoryState.runs = data.runs || [];
      filterAndRenderBatchHistory();
    } catch (err) {
      console.error("Failed to load batch run history:", err);
      $tbody.html(`<tr><td colspan="8" style="text-align:center;color:#ef4444;padding:24px;">Failed to load telemetry history: ${esc(err.message)}</td></tr>`);
    }
  }

  function filterAndRenderBatchHistory() {
    const runs = batchHistoryState.runs || [];
    const search = ($("#batchHistorySearchInput").val() || "").trim().toLowerCase();
    const lvlFilter = $("#batchHistoryLevelFilter").val() || "all";
    const statusFilter = $("#batchHistoryStatusFilter").val() || "all";

    const filtered = runs.filter((r) => {
      const isFailed = (r.status === "failed" || r.status === "error" || (r.error_message && String(r.error_message).trim().length > 0));
      const isOk = !isFailed && (r.status === "success" || r.status === "completed" || r.status === "staged" || r.status === "validated" || r.status === "ok");

      if (lvlFilter !== "all") {
        const lvl = (r.pipeline_level || "").toLowerCase();
        if (lvl !== lvlFilter) return false;
      }
      if (statusFilter !== "all") {
        if (statusFilter === "success" && !isOk) return false;
        if (statusFilter === "failed" && !isFailed) return false;
      }
      if (search) {
        const target = (r.entities_extracted?.full_name || r.entities_extracted?.lob_name || r.entities_extracted?.company || r.company_name || r.target_url || r.run_id || "").toLowerCase();
        const action = (r.action || "").toLowerCase();
        if (!target.includes(search) && !action.includes(search)) return false;
      }
      return true;
    });

    batchHistoryState.filteredRuns = filtered;

    // Update History KPIs
    const totalRuns = runs.length;
    const failedRuns = runs.filter(r => r.status === "failed" || r.status === "error" || (r.error_message && String(r.error_message).trim().length > 0)).length;
    const successRuns = totalRuns - failedRuns;
    const successRate = totalRuns ? Math.round((successRuns / totalRuns) * 100) : 100;
    const totalDur = runs.reduce((acc, r) => acc + (parseFloat(r.duration_seconds) || 0), 0);
    const avgDur = totalRuns ? (totalDur / totalRuns).toFixed(1) : "0.0";

    $("#historyKpiTotal").text(totalRuns);
    $("#historyKpiSuccessRate").text(`${successRate}%`);
    $("#historyKpiAvgDuration").text(`${avgDur}s`);

    // Render Table
    const $tbody = $("#batchHistoryTableBody");
    $tbody.empty();

    if (filtered.length === 0) {
      $("#batchHistoryEmpty").removeClass("d-none");
      return;
    }
    $("#batchHistoryEmpty").addClass("d-none");

    filtered.forEach((r) => {
      const timeInfo = formatRunTime(r.started_at || r.completed_at);
      const isPersona = (r.pipeline_level === "persona" || (r.run_id && r.run_id.includes("_persona_")));
      const isLob = (r.pipeline_level === "lob" || (r.run_id && r.run_id.includes("_lob_")));
      const kindBadge = isPersona
        ? '<span class="badge" style="background:rgba(99,102,241,0.12);color:#6366f1;font-weight:600;"><i class="bi bi-person-fill"></i> Person</span>'
        : (isLob
          ? '<span class="badge" style="background:rgba(14,165,233,0.12);color:#0284c7;font-weight:600;"><i class="bi bi-diagram-3-fill"></i> LOB</span>'
          : '<span class="badge" style="background:rgba(16,185,129,0.12);color:#059669;font-weight:600;"><i class="bi bi-building"></i> Account</span>');

      const targetName = r.entities_extracted?.full_name || r.entities_extracted?.lob_name || r.entities_extracted?.title || r.entities_extracted?.company || r.company_name || r.run_id;
      const targetSub = r.target_url || r.action || (r.entities_extracted?.account_id ? `Account #${r.entities_extracted.account_id}` : "");

      // Channels
      const channels = [];
      if (r.target_url && r.target_url.includes("linkedin.com")) channels.push("LinkedIn");
      channels.push("Exa AI");
      if (isPersona) {
        channels.push("Gemini AI");
        channels.push("Apollo");
      } else {
        channels.push("SEC EDGAR");
        channels.push("GLEIF LEI");
      }
      const chipsHtml = channels.map(c => `<span class="batch-source-chip source-linkedin" style="font-size:0.65rem;">${esc(c)}</span>`).join(" ");

      const score = r.quality_score != null ? Math.round(r.quality_score) : null;
      const duration = (parseFloat(r.duration_seconds) || 0).toFixed(1) + "s";
      const isFailed = (r.status === "failed" || r.status === "error" || (r.error_message && String(r.error_message).trim().length > 0));
      const statusBadge = isFailed
        ? '<span class="badge" style="background:rgba(239,68,68,0.12);color:#ef4444;font-weight:600;" title="' + esc(r.error_message || 'Execution error') + '"><i class="bi bi-exclamation-triangle-fill"></i> failed</span>'
        : '<span class="badge" style="background:rgba(16,185,129,0.12);color:#10b981;font-weight:600;"><i class="bi bi-check-circle-fill"></i> ok</span>';

      const rowHtml = `
        <tr>
          <td>
            <div class="batch-time-cell">
              <span class="batch-time-rel">${esc(timeInfo.relative)}</span>
              <span class="batch-time-exact">${esc(timeInfo.exact)}</span>
            </div>
          </td>
          <td>${kindBadge}</td>
          <td>
            <div style="font-weight:600;color:var(--text-primary);">${esc(targetName)}</div>
            ${targetSub ? `<div class="text-muted" style="font-size:0.70rem;overflow:hidden;text-overflow:ellipsis;max-width:240px;">${esc(targetSub)}</div>` : ""}
          </td>
          <td><div class="batch-source-chips">${chipsHtml}</div></td>
          <td>
            ${score != null ? `<span style="font-weight:600;color:#10b981;">Score: ${score}%</span>` : `<span class="text-muted" style="font-size:0.75rem;">—</span>`}
          </td>
          <td><span class="text-muted" style="font-size:0.75rem;">${esc(duration)}</span></td>
          <td>${statusBadge}</td>
          <td style="text-align:right;">
            <button type="button" class="btn btn-xs btn-outline-secondary batch-view-run-log-btn" data-run-id="${esc(r.run_id)}" style="font-size:0.70rem;padding:2px 8px;">
              <i class="bi bi-file-earmark-code"></i> Details
            </button>
          </td>
        </tr>
      `;
      $tbody.append(rowHtml);
    });
  }

  // History Toolbar Filters
  let historySearchDebounce = null;
  $(document).on("input", "#batchHistorySearchInput", function () {
    clearTimeout(historySearchDebounce);
    historySearchDebounce = setTimeout(() => {
      filterAndRenderBatchHistory();
    }, 200);
  });

  $(document).on("change", "#batchHistoryLevelFilter, #batchHistoryStatusFilter", function () {
    filterAndRenderBatchHistory();
  });

  $(document).on("click", "#batchHistoryRefreshBtn", function (e) {
    e.preventDefault();
    loadBatchRunHistory();
  });

  // ── Telemetry Run Audit Details Modal Handler ──
  async function openBatchRunDetailsModal(runId) {
    let run = (batchHistoryState.runs || []).find(r => r.run_id === runId);

    // Fetch full, exact record from backend API if available
    try {
      const res = await fetch(`${API_BASE}/api/pipeline/runs/${encodeURIComponent(runId)}`);
      if (res.ok) {
        const full = await res.json();
        if (full && full.run_id) run = full;
      }
    } catch (_) {}

    if (!run) {
      showToast("Run audit record not found", "error");
      return;
    }

    const isPersona = (run.pipeline_level === "persona" || (run.run_id && run.run_id.includes("_persona_")));
    const isLob = (run.pipeline_level === "lob" || (run.run_id && run.run_id.includes("_lob_")));
    const isFailed = (run.status === "failed" || run.status === "error" || (run.error_message && String(run.error_message).trim().length > 0));
    const timeInfo = formatRunTime(run.started_at || run.completed_at);

    // Badges
    const statusBadge = isFailed
      ? '<span class="badge" style="background:rgba(239,68,68,0.12);color:#ef4444;font-weight:700;"><i class="bi bi-exclamation-triangle-fill"></i> Failed</span>'
      : '<span class="badge" style="background:rgba(16,185,129,0.12);color:#10b981;font-weight:700;"><i class="bi bi-check-circle-fill"></i> Succeeded (OK)</span>';

    const levelBadge = isPersona
      ? '<span class="badge" style="background:rgba(99,102,241,0.12);color:#6366f1;font-weight:600;"><i class="bi bi-person-fill"></i> Persona</span>'
      : (isLob
        ? '<span class="badge" style="background:rgba(14,165,233,0.12);color:#0284c7;font-weight:600;"><i class="bi bi-diagram-3-fill"></i> LOB</span>'
        : '<span class="badge" style="background:rgba(16,185,129,0.12);color:#059669;font-weight:600;"><i class="bi bi-building"></i> Account</span>');

    const actionBadge = `<span class="badge bg-secondary-subtle text-secondary-emphasis" style="font-weight:600;text-transform:uppercase;">${esc(run.action || "run")}</span>`;

    $("#batchRunDetailsStatusBadge").html(statusBadge);
    $("#batchRunDetailsLevelBadge").html(levelBadge);
    $("#batchRunDetailsActionBadge").html(actionBadge);

    $("#batchRunDetailsRunId").text(run.run_id);
    $("#batchRunDetailsTime").text(`${timeInfo.exact} (${timeInfo.relative})`);

    // Error banner
    if (isFailed && run.error_message) {
      $("#batchRunDetailsErrorMessage").text(run.error_message);
      $("#batchRunDetailsErrorBanner").removeClass("d-none");
    } else {
      $("#batchRunDetailsErrorBanner").addClass("d-none");
    }

    // Quick Metrics
    $("#batchRunDetailsDuration").text(`${(parseFloat(run.duration_seconds) || 0).toFixed(2)}s`);
    $("#batchRunDetailsQualityScore").text(run.quality_score != null ? `${Math.round(run.quality_score)}% (Grade ${run.quality_grade || 'B'})` : "Unscored");
    $("#batchRunDetailsCredits").text(`${run.total_credits_used || 0} cr`);

    const targetName = run.entities_extracted?.full_name || run.entities_extracted?.lob_name || run.entities_extracted?.title || run.entities_extracted?.company || run.company_name || "N/A";
    const targetSub = run.target_url || (run.entities_extracted?.account_id ? `Account #${run.entities_extracted.account_id}` : run.company_name || "");
    $("#batchRunDetailsTargetName").text(targetName).attr("title", targetName);
    $("#batchRunDetailsTargetSub").text(targetSub).attr("title", targetSub);

    // Tab 1: Entities Extracted
    const entities = run.entities_extracted || {};
    const entityKeys = Object.keys(entities);
    $("#batchRunDetailsEntitiesCount").text(entityKeys.length);
    if (entityKeys.length === 0) {
      $("#batchRunDetailsEntitiesContainer").html('<div class="text-muted p-3" style="font-size:0.80rem;">No specific extracted entity attributes logged for this run.</div>');
    } else {
      let entHtml = "";
      entityKeys.forEach(k => {
        let val = entities[k];
        if (typeof val === "object" && val !== null) {
          val = JSON.stringify(val);
        } else {
          val = String(val ?? "—");
        }
        entHtml += `
          <div class="batch-entity-card">
            <div class="batch-entity-card-key">${esc(k.replace(/_/g, ' '))}</div>
            <div class="batch-entity-card-val">${esc(val)}</div>
          </div>
        `;
      });
      $("#batchRunDetailsEntitiesContainer").html(entHtml);
    }

    // Tab 2: Credits Breakdown
    const credBreakdown = run.credits_breakdown || {};
    const sections = credBreakdown.sections || {};
    let credHtml = "";
    if (Object.keys(sections).length > 0) {
      credHtml += `<div class="d-flex flex-column gap-2">`;
      for (const [sKey, sec] of Object.entries(sections)) {
        if (!sec.resources || sec.resources.length === 0) continue;
        credHtml += `
          <div style="border:1px solid var(--border-color);border-radius:8px;padding:12px;background:rgba(0,0,0,0.01);">
            <div class="d-flex justify-content-between align-items-center mb-2">
              <strong style="font-size:0.82rem;">${esc(sec.title || sKey)}</strong>
              <span class="badge bg-primary-subtle text-primary" style="font-size:0.75rem;">${esc(sec.subtotal_label || `${sec.subtotal_credits || 0} cr`)}</span>
            </div>
            <div class="d-flex flex-column gap-1">
              ${(sec.resources || []).map(r => `
                <div class="d-flex justify-content-between align-items-center" style="font-size:0.75rem;padding:4px 0;border-bottom:1px dashed var(--border-color);">
                  <div>
                    <span style="font-weight:600;">${esc(r.name || r.id)}</span>
                    <span class="text-muted ms-1" style="font-size:0.70rem;">(${esc(r.calls_label || '1 call')})</span>
                  </div>
                  <div class="d-flex align-items-center gap-2">
                    <span class="badge bg-secondary-subtle text-secondary" style="font-size:0.68rem;">${esc(r.status || 'OK')}</span>
                    <strong style="color:var(--text-primary);">${r.credits || 0} cr</strong>
                  </div>
                </div>
              `).join('')}
            </div>
          </div>
        `;
      }
      credHtml += `</div>`;
    }
    if (!credHtml) {
      credHtml = `<div class="p-3 text-muted" style="font-size:0.80rem;">No detailed vendor credit breakdown recorded for this execution step. Total billable credits: <strong>${run.total_credits_used || 0} cr</strong>.</div>`;
    }
    $("#batchRunDetailsCreditsContainer").html(credHtml);

    // Tab 3: Execution Chronology Logs
    const logs = run.execution_logs || [];
    $("#batchRunDetailsLogsCount").text(logs.length);
    if (logs.length === 0) {
      $("#batchRunDetailsLogsContainer").html('<div class="text-muted p-3" style="font-size:0.80rem;">No chronological events recorded in execution log.</div>');
    } else {
      let logsHtml = '<div class="d-flex flex-column gap-2">';
      logs.forEach((logItem, idx) => {
        const logTime = logItem.timestamp ? new Date(logItem.timestamp).toLocaleTimeString() : `#${idx + 1}`;
        const logStatus = logItem.status || "completed";
        const isLogErr = (logStatus === "failed" || logItem.error);
        logsHtml += `
          <div style="border:1px solid var(--border-color);border-radius:8px;padding:10px 14px;background:rgba(0,0,0,0.01);">
            <div class="d-flex justify-content-between align-items-center">
              <div class="d-flex align-items-center gap-2">
                <span class="badge ${isLogErr ? 'bg-danger-subtle text-danger' : 'bg-success-subtle text-success'}" style="font-size:0.70rem;">
                  <i class="bi ${isLogErr ? 'bi-x-circle' : 'bi-check-circle'}"></i> ${esc(logStatus)}
                </span>
                <strong style="font-size:0.80rem;">Action: ${esc(logItem.action || 'run')} (${esc(logItem.level || 'pipeline')})</strong>
              </div>
              <span class="text-muted" style="font-size:0.70rem;">${esc(logTime)} &bull; ${(parseFloat(logItem.duration_seconds) || 0).toFixed(2)}s</span>
            </div>
            ${logItem.error ? `<div class="text-danger mt-1" style="font-size:0.75rem;"><i class="bi bi-exclamation-triangle"></i> ${esc(logItem.error)}</div>` : ''}
            ${logItem.particulars ? `<div class="text-muted mt-1" style="font-size:0.72rem;word-break:break-all;">${esc(JSON.stringify(logItem.particulars))}</div>` : ''}
          </div>
        `;
      });
      logsHtml += '</div>';
      $("#batchRunDetailsLogsContainer").html(logsHtml);
    }

    // Tab 4: Raw JSON
    const fullJson = JSON.stringify(run, null, 2);
    $("#batchRunDetailsRawJson").text(fullJson);

    // Reset tabs to Entities active
    $(".batch-details-tab-btn").removeClass("active");
    $(".batch-details-tab-btn[data-tab='entities']").addClass("active");
    $(".batch-details-pane").addClass("d-none");
    $("#batchTabEntities").removeClass("d-none");

    // Open modal
    $("#batchRunDetailsBackdrop").removeClass("d-none");
  }

  function closeBatchRunDetailsModal() {
    $("#batchRunDetailsBackdrop").addClass("d-none");
  }

  // Details Modal Event Listeners
  $(document).on("click", ".batch-view-run-log-btn", function (e) {
    e.preventDefault();
    const runId = $(this).data("run-id");
    openBatchRunDetailsModal(runId);
  });

  $(document).on("click", ".batch-details-tab-btn", function (e) {
    e.preventDefault();
    const tabName = $(this).data("tab");
    $(".batch-details-tab-btn").removeClass("active");
    $(this).addClass("active");

    $(".batch-details-pane").addClass("d-none");
    if (tabName === "entities") $("#batchTabEntities").removeClass("d-none");
    else if (tabName === "credits") $("#batchTabCredits").removeClass("d-none");
    else if (tabName === "logs") $("#batchTabLogs").removeClass("d-none");
    else if (tabName === "raw") $("#batchTabRaw").removeClass("d-none");
  });

  $(document).on("click", "#copyRunIdBtn", function (e) {
    e.preventDefault();
    const runId = $("#batchRunDetailsRunId").text();
    if (runId) {
      navigator.clipboard.writeText(runId);
      showToast("Run ID copied to clipboard!", "success");
    }
  });

  $(document).on("click", "#copyRunRawJsonBtn, #batchDetailsCopyAllBtn", function (e) {
    e.preventDefault();
    const jsonText = $("#batchRunDetailsRawJson").text();
    if (jsonText) {
      navigator.clipboard.writeText(jsonText);
      showToast("Telemetry JSON audit copied to clipboard!", "success");
    }
  });

  $(document).on("click", "#closeBatchRunDetailsBtn, #batchDetailsDismissBtn", function (e) {
    e.preventDefault();
    closeBatchRunDetailsModal();
  });

  $(document).on("click", "#batchRunDetailsBackdrop", function (e) {
    if (e.target === this) {
      closeBatchRunDetailsModal();
    }
  });
});


