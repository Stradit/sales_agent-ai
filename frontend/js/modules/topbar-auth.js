// Small topbar widget: "Sign In" link when logged out, or the user's email
// + an Admin link (super_admin only) + a "My Tasks" cross-account action-item
// count + Logout when logged in. Not enforced anywhere yet (AUTH_ENFORCED=false
// server-side, see AUTH_JWT_IMPLEMENTATION_PLAN.md §9) — this just makes the
// feature discoverable and usable while that's the case.
import { esc, initials } from './utils.js';
import { getCurrentUser, logout, refreshAccessToken } from './auth-client.js';
import { renderMyTasksPanel } from './action-items.js';
import { myTasksDrawer, myTasksDrawerBackdrop, myTasksDrawerBody } from './dom.js';
import { initSearchPalette } from './search-palette.js';

function openMyTasksDrawer() {
  const drawer = document.getElementById('myTasksDrawer');
  const backdrop = document.getElementById('myTasksDrawerBackdrop');
  const body = document.getElementById('myTasksDrawerBody');
  if (!drawer || !backdrop || !body) return;
  body.innerHTML = '<div class="empty-block-text" style="padding:16px 4px;">Loading…</div>';
  drawer.classList.add('open');
  backdrop.classList.add('open');
}

function closeMyTasksDrawer() {
  const drawer = document.getElementById('myTasksDrawer');
  const backdrop = document.getElementById('myTasksDrawerBackdrop');
  if (drawer) drawer.classList.remove('open');
  if (backdrop) backdrop.classList.remove('open');
}

document.getElementById('myTasksDrawerClose')?.addEventListener('click', closeMyTasksDrawer);
document.getElementById('myTasksDrawerBackdrop')?.addEventListener('click', closeMyTasksDrawer);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeMyTasksDrawer(); });

const ROLE_NAMES = { super_admin: 'Super Administrator', sales_manager: 'Sales Manager', user: 'Sales Rep',
  viewer: 'Viewer (read-only)', partner: 'Partner / Advisor' };

function formatUserRole(role) {
  if (ROLE_NAMES[role]) return ROLE_NAMES[role];
  if (!role) return 'Standard User';
  const r = String(role).toLowerCase().replace(/_/g, ' ');
  if (r === 'super admin') return 'Super Administrator';
  if (r === 'ae' || r === 'sales rep') return 'Account Executive';
  if (r === 'leader' || r === 'sales leader') return 'Sales Leader';
  return r.charAt(0).toUpperCase() + r.slice(1);
}

function getUserInitials(user) {
  if (!user) return 'U';
  if (user.full_name) {
    const parts = user.full_name.trim().split(/\s+/);
    if (parts.length >= 2) {
      return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }
    return parts[0].substring(0, 2).toUpperCase();
  }
  if (user.email) {
    return user.email.substring(0, 2).toUpperCase();
  }
  return 'U';
}

let currentOptions = {};

function render() {
  const el = document.getElementById('topbarAuthWidget');
  if (!el) return;
  const user = getCurrentUser();

  if (!user) {
    el.innerHTML = `<a href="/login" class="topbar-link"><i class="fa-solid fa-right-to-bracket"></i> Sign In</a>`;
    return;
  }

  const isOnAdminPage = window.location.pathname.startsWith('/admin');
  const showTasks = !isOnAdminPage && (user.role === 'super_admin' || user.has_tasks_access !== false);
  const displayName = user.full_name || (user.email ? user.email.split('@')[0] : 'User');
  const initials = getUserInitials(user);
  const isSuperAdmin = user.role === 'super_admin';

  el.innerHTML = `
    ${showTasks ? '<button type="button" id="topbarMyTasksBtn" class="topbar-link topbar-link-btn" title="View assigned action items"><i class="bi bi-list-check"></i> My Tasks <span class="tab-badge" id="topbarMyTasksBadge">…</span></button>' : ''}
    
    <div class="topbar-profile-container" id="topbarProfileContainer">
      <button type="button" class="topbar-profile-btn" id="topbarProfileBtn" aria-expanded="false" aria-haspopup="true" title="User profile for ${esc(displayName)}">
        <div class="topbar-profile-avatar">${esc(initials)}</div>
        <span class="topbar-profile-label">${esc(displayName)}</span>
        <i class="bi bi-chevron-down topbar-profile-arrow"></i>
      </button>

      <div class="topbar-profile-dropdown" id="topbarProfileDropdown" role="menu">
        <div class="profile-dropdown-header">
          <div class="profile-dropdown-avatar">${esc(initials)}</div>
          <div class="profile-dropdown-info">
            <div class="profile-dropdown-name" title="${esc(displayName)}">${esc(displayName)}</div>
            <div class="profile-dropdown-email" title="${esc(user.email || '')}">${esc(user.email || '')}</div>
          </div>
        </div>

        <div class="profile-dropdown-role-row">
          <span class="profile-role-tag ${isSuperAdmin ? 'role-super-admin' : 'role-user'}">
            <i class="bi ${isSuperAdmin ? 'bi-shield-check' : 'bi-person-badge'}"></i>
            ${isSuperAdmin ? 'Super Admin' : 'User'}
          </span>
        </div>

        <div class="profile-dropdown-divider"></div>

        <button type="button" class="profile-dropdown-item profile-dropdown-logout" id="topbarLogoutBtn" role="menuitem">
          <i class="bi bi-box-arrow-right"></i>
          <span>Logout</span>
        </button>
      </div>
    </div>
  `;

  const profileBtn = document.getElementById('topbarProfileBtn');
  const profileDropdown = document.getElementById('topbarProfileDropdown');
  const profileContainer = document.getElementById('topbarProfileContainer');

  if (profileBtn && profileDropdown) {
    profileBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = profileDropdown.classList.contains('show');
      if (isOpen) {
        profileDropdown.classList.remove('show');
        profileBtn.classList.remove('active');
        profileBtn.setAttribute('aria-expanded', 'false');
      } else {
        profileDropdown.classList.add('show');
        profileBtn.classList.add('active');
        profileBtn.setAttribute('aria-expanded', 'true');
      }
    });

    document.addEventListener('click', (e) => {
      if (profileContainer && !profileContainer.contains(e.target)) {
        profileDropdown.classList.remove('show');
        profileBtn.classList.remove('active');
        profileBtn.setAttribute('aria-expanded', 'false');
      }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        profileDropdown.classList.remove('show');
        profileBtn.classList.remove('active');
        profileBtn.setAttribute('aria-expanded', 'false');
      }
    });
  }

  const logoutBtn = document.getElementById('topbarLogoutBtn');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      await logout();
      window.location.href = '/login';
    });
  }
  if (showTasks) {
    const tasksBtn = document.getElementById('topbarMyTasksBtn');
    if (tasksBtn) {
      tasksBtn.addEventListener('click', async () => {
        openMyTasksDrawer();
        const html = await renderMyTasksPanel();
        const body = document.getElementById('myTasksDrawerBody');
        if (body) body.innerHTML = html;
        refreshMyTasksBadge();
      });
      refreshMyTasksBadge();
    }
  }
}

async function refreshMyTasksBadge() {
  const badge = document.getElementById('topbarMyTasksBadge');
  if (!badge) return;
  try {
    const res = await fetch('/api/me/action-items');
    if (!res.ok) return;
    const data = await res.json();
    const open = (data.action_items || []).filter(i => i.status !== 'done' && i.status !== 'cancelled');
    badge.textContent = open.length;
  } catch (err) {
    console.error('Failed to load My Tasks count', err);
  }
}

// Ensure pages restored via browser Back/Forward (bfcache) revalidate session
window.addEventListener('pageshow', async (event) => {
  if (event.persisted) {
    const user = await refreshAccessToken();
    if (!user) {
      document.body.style.display = 'none';
      window.location.replace(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
    } else {
      render();
    }
  }
});

export async function initTopbarAuth(options = {}) {
  currentOptions = options;
  await refreshAccessToken(); // silent — restores a session from the refresh cookie on page load
  const user = getCurrentUser();
  // Partners only ever see the partner portal (the API refuses everything else anyway).
  if (user && user.role === 'partner' && location.pathname !== '/partner') {
    document.body.style.display = 'none';
    location.replace('/partner');
    return null;
  }
  // Viewers: CSS hides edit controls app-wide (shell.css `.role-viewer`), pages can check the class.
  document.body.classList.toggle('role-viewer', !!(user && user.role === 'viewer'));
  render();
  if (user && user.role === 'viewer') {
    const w = document.getElementById('topbarAuthWidget');
    if (w && !w.querySelector('.topbar-readonly')) {
      w.insertAdjacentHTML('afterbegin', '<span class="topbar-readonly" title="Your role can view but not change data"><i class="fa-solid fa-eye"></i> Read-only</span>');
    }
  }
  if (user) initSearchPalette();
  return user;
}
