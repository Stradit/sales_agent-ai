// Entry point for the super-admin dashboard (/admin) — Command Center.
import './fetch-instrumentation.js';
import { getCurrentUser, logout, refreshAccessToken } from './auth-client.js';
import { initTopbarAuth } from './topbar-auth.js';
import { initThemeToggle } from './theme.js';
import { showToast } from './toast.js';
import { mountCrmSettings } from './admin-crm-settings.js';
import { mountJobs } from './admin-jobs.js';

initThemeToggle();
initTopbarAuth();

// Escapes for HTML text AND attribute values (quotes too: many callers build href="${esc(x)}").
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const main = document.getElementById('adminMain');
let usersCache = [];
let statsCache = null;
let currentFilter = 'all';
let searchQuery = '';
let activeAdminTab = 'users';
let apiConfigsCache = [];
let rolesGuideCache = null;   // GET /api/crm/roles (apps/sales_crm/role_guide.py)
let dirtyConfigs = {};

// CRM roles + team (apps/sales_crm/README.md §5). 'user' is the Sales Rep role.
const ROLE_OPTIONS = [['user', 'Sales Rep'], ['sales_manager', 'Sales Manager'], ['viewer', 'Viewer (read-only)'],
  ['partner', 'Partner / Advisor'], ['super_admin', 'Super Admin']];
const ROLE_LABEL = Object.fromEntries(ROLE_OPTIONS.map(([k, l]) => [k, l.replace(' (read-only)', '')]));
let crmTeam = {};            // user id -> {manager_id, manager_name, business_line_ids}
let crmBusinessLines = [];
let authEnforced = true;
const roleOptions = (selected) => ROLE_OPTIONS.map(([k, l]) => `<option value="${k}" ${k === selected ? 'selected' : ''}>${l}</option>`).join('');

let auditLogs = [];
let auditOffset = 0;
const AUDIT_PAGE_SIZE = 5;
let auditTotal = 0;
let auditHasMore = false;
let auditLoading = false;

function timeAgo(iso) {
  if (!iso) return 'Never';
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

function getInitials(name, email) {
  if (name && name.trim()) {
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return name.slice(0, 2).toUpperCase();
  }
  return (email || 'U').slice(0, 2).toUpperCase();
}



function renderHero() {
  return `
    <div class="admin-hero">
      <div>
        <h1 class="admin-hero-title">Platform Administration & Governance</h1>
        <p class="admin-hero-sub">Manage user accounts, assign corporate sales access permissions, and audit real-time platform security.</p>
      </div>
    </div>
  `;
}

function renderKPIBanner(stats) {
  return `
    <div class="admin-kpi-grid">
      <div class="admin-kpi-card">
        <div class="admin-kpi-icon blue"><i class="fa-solid fa-users"></i></div>
        <div>
          <div class="admin-kpi-value">${stats.total_users || 0}</div>
          <div class="admin-kpi-label">Total Users</div>
        </div>
      </div>
      <div class="admin-kpi-card">
        <div class="admin-kpi-icon green"><i class="fa-solid fa-user-check"></i></div>
        <div>
          <div class="admin-kpi-value">${stats.active_users || 0}</div>
          <div class="admin-kpi-label">Active Users</div>
        </div>
      </div>
      <div class="admin-kpi-card">
        <div class="admin-kpi-icon purple"><i class="fa-solid fa-shield-halved"></i></div>
        <div>
          <div class="admin-kpi-value">${stats.super_admin_count || 0}</div>
          <div class="admin-kpi-label">Super Admins</div>
        </div>
      </div>
      <div class="admin-kpi-card">
        <div class="admin-kpi-icon amber"><i class="fa-solid fa-city"></i></div>
        <div>
          <div class="admin-kpi-value">${stats.total_accounts || 0}</div>
          <div class="admin-kpi-label">Tracked Accounts</div>
        </div>
      </div>
    </div>
  `;
}

function renderCreateForm() {
  return `
    <div class="admin-panel admin-create-panel">
      <div class="admin-panel-header">
        <div class="admin-panel-title"><i class="fa-solid fa-user-plus"></i> Create New User Account</div>
        <span class="context-badge live"><i class="fa-solid fa-shield-halved"></i> Instant Provisioning</span>
      </div>
      <div class="admin-panel-body">
        <form id="createUserForm" class="admin-form-horizontal">
          <div class="admin-form-group">
            <label>Full Name</label>
            <input type="text" name="full_name" placeholder="e.g. Jane Doe" autocomplete="off">
          </div>
          <div class="admin-form-group">
            <label>Work Email Address</label>
            <input type="email" name="email" required placeholder="name@company.com" autocomplete="off">
          </div>
          <div class="admin-form-group">
            <label>Initial Password</label>
            <input type="password" name="password" required minlength="8" placeholder="Min 8 characters" autocomplete="new-password">
          </div>
          <div class="admin-form-group">
            <label>Assigned Role</label>
            <select name="role">${roleOptions('user')}</select>
          </div>
          <div class="admin-form-group btn-group">
            <button type="submit" class="admin-submit-btn" id="createUserSubmit">
              <i class="fa-solid fa-circle-plus"></i> Create User
            </button>
          </div>
        </form>
        <div class="admin-form-error" id="createUserError" style="display:none; margin-top:12px;"></div>
      </div>
    </div>
  `;
}

function getFilteredUsers() {
  let list = usersCache;
  if (currentFilter === 'active') list = list.filter(u => u.is_active);
  else if (currentFilter === 'inactive') list = list.filter(u => !u.is_active);
  else if (ROLE_LABEL[currentFilter]) list = list.filter(u => u.role === currentFilter);

  if (searchQuery) {
    const q = searchQuery.toLowerCase();
    list = list.filter(u =>
      (u.full_name && u.full_name.toLowerCase().includes(q)) ||
      (u.email && u.email.toLowerCase().includes(q)) ||
      (u.role && (u.role.toLowerCase().includes(q) || (ROLE_LABEL[u.role] || '').toLowerCase().includes(q)))
    );
  }
  return list;
}

function renderUsersTable(currentUserId) {
  const filtered = getFilteredUsers();
  const rows = filtered.map(u => {
    const initials = getInitials(u.full_name, u.email);
    const isAdmin = u.role === 'super_admin';
    const isSelf = u.id === currentUserId;

    return `
      <tr data-user-id="${u.id}">
        <td>
          <div class="admin-user-cell">
            <div class="admin-avatar ${isAdmin ? 'admin-role' : ''}">${initials}</div>
            <div>
              <div class="admin-user-name">${esc(u.full_name || '—')}${isSelf ? ' <span style="font-size:0.68rem; color:var(--brand); font-weight:700;">(You)</span>' : ''}</div>
              <div class="admin-user-email">${esc(u.email)}</div>
            </div>
          </div>
        </td>
        <td>
          <select class="admin-role-select" data-action="role" ${isSelf ? 'disabled title="You cannot change your own role"' : ''}>${roleOptions(u.role)}</select>
          ${teamLine(u.id)}
        </td>
        <td>
          <span class="admin-status-pill ${u.is_active ? 'active' : 'inactive'}">
            <span class="dot"></span> ${u.is_active ? 'Active' : 'Inactive'}
          </span>
        </td>
        <td style="color:var(--text-muted); font-size:0.75rem;">
          ${timeAgo(u.last_login_at)}
        </td>
        <td>
          <div class="admin-row-actions">
            <button type="button" class="admin-btn admin-access-btn" data-action="access" title="Manage account visibility">
              <i class="fa-solid fa-shield-halved"></i> Access
            </button>
            <div class="admin-kebab-wrap">
              <button type="button" class="admin-btn admin-kebab-btn" data-action="kebab-toggle" title="More options" aria-haspopup="true">
                <i class="fa-solid fa-ellipsis-vertical"></i>
              </button>
              <div class="admin-kebab-menu d-none">
                <button type="button" class="admin-kebab-item" data-action="edit-user">
                  <i class="fa-solid fa-pen-to-square"></i> Edit User
                </button>
                ${!isAdmin ? `
                  <button type="button" class="admin-kebab-item" data-action="toggle-active">
                    <i class="fa-solid fa-power-off"></i> ${u.is_active ? 'Deactivate' : 'Activate'}
                  </button>
                  <div class="admin-kebab-divider"></div>
                  <button type="button" class="admin-kebab-item danger" data-action="delete">
                    <i class="fa-solid fa-trash"></i> Delete User
                  </button>
                ` : ''}
              </div>
            </div>
          </div>
        </td>
      </tr>
    `;
  }).join('');

  return `
    <div class="admin-panel">
      <div class="admin-panel-header">
        <div class="admin-panel-title">
          <i class="fa-solid fa-users"></i> User Directory & Access Control
        </div>
        <span class="context-badge live" id="userCountBadge">${filtered.length} of ${usersCache.length} users</span>
      </div>

      <div class="admin-filter-bar">
        <div class="admin-search-box">
          <i class="fa-solid fa-magnifying-glass search-icon"></i>
          <input type="text" class="admin-search-input" id="adminUserSearch" placeholder="Search by name, email, or role..." value="${esc(searchQuery)}">
          <button type="button" class="admin-search-clear" id="adminUserSearchClear"><i class="fa-solid fa-circle-xmark"></i></button>
        </div>
        <div class="admin-filter-chips">
          <button type="button" class="admin-filter-chip ${currentFilter === 'all' ? 'active' : ''}" data-filter="all">All</button>
          <button type="button" class="admin-filter-chip ${currentFilter === 'active' ? 'active' : ''}" data-filter="active">Active</button>
          <button type="button" class="admin-filter-chip ${currentFilter === 'inactive' ? 'active' : ''}" data-filter="inactive">Inactive</button>
          ${[['super_admin', 'Super Admins'], ['sales_manager', 'Managers'], ['user', 'Sales Reps'], ['viewer', 'Viewers'], ['partner', 'Partners']]
            .map(([k, l]) => `<button type="button" class="admin-filter-chip ${currentFilter === k ? 'active' : ''}" data-filter="${k}">${l}</button>`).join('')}
        </div>
      </div>

      <div class="admin-table-wrap">
        <table class="admin-table">
          <thead>
            <tr>
              <th>User</th>
              <th>Role</th>
              <th>Status</th>
              <th>Last Active</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="adminTableBody">
            ${rows || `<tr><td colspan="5" style="text-align:center; padding:36px; color:var(--text-muted);"><i class="fa-solid fa-magnifying-glass" style="font-size:1.5rem; display:block; margin-bottom:8px;"></i> No users matched your search criteria.</td></tr>`}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function renderAuditRow(e) {
  return `
    <div class="admin-activity-row">
      <div class="admin-activity-icon-wrap"><i class="fa-solid fa-shield-halved"></i></div>
      <div class="admin-activity-content">
        <div class="admin-activity-text">
          <strong>${esc(e.actor)}</strong> — ${esc((e.action || '').replace(/_/g, ' '))}
          ${e.target ? ` on <strong>${esc(e.target)}</strong>` : ''}
        </div>
        <div class="admin-activity-time">${timeAgo(e.created_at)}</div>
      </div>
    </div>
  `;
}

function renderActivity() {
  return `
    <div class="admin-panel">
      <div class="admin-panel-header">
        <div class="admin-panel-title"><i class="fa-solid fa-clock-rotate-left"></i> Security & Audit Trail</div>
        <span class="context-badge live" id="auditCountBadge">Loading…</span>
      </div>
      <div class="admin-panel-body">
        <div class="admin-activity-list" id="adminActivityList">
          <div class="admin-audit-loading"><div class="spinner-sm"></div> Loading audit records…</div>
        </div>
        <div class="admin-audit-footer" id="adminAuditFooter" style="display:none;">
          <button type="button" class="admin-btn" id="loadMoreAuditBtn">
            <i class="fa-solid fa-circle-down"></i> Load More Logs (<span id="auditRemainingCount">0</span> remaining)
          </button>
        </div>
      </div>
    </div>
  `;
}

async function loadAuditLogs(reset = false) {
  if (auditLoading) return;
  if (reset) {
    auditOffset = 0;
    auditLogs = [];
  }
  auditLoading = true;
  const footer = document.getElementById('adminAuditFooter');
  const loadBtn = document.getElementById('loadMoreAuditBtn');
  const listEl = document.getElementById('adminActivityList');
  const badge = document.getElementById('auditCountBadge');

  if (loadBtn) {
    loadBtn.disabled = true;
    loadBtn.innerHTML = '<div class="spinner-sm"></div> Loading…';
  }

  try {
    const res = await fetch(`/api/admin/audit-logs?limit=${AUDIT_PAGE_SIZE}&offset=${auditOffset}`);
    if (res.ok) {
      const data = await res.json();
      const newLogs = data.audit_logs || [];
      auditLogs = reset ? newLogs : [...auditLogs, ...newLogs];
      auditTotal = data.total || 0;
      auditHasMore = data.has_more;
      auditOffset += newLogs.length;

      if (badge) badge.textContent = `${auditLogs.length} of ${auditTotal}`;

      if (!auditLogs.length) {
        if (listEl) listEl.innerHTML = '<div style="color:var(--text-muted); font-size:0.8rem; text-align:center; padding:16px;">No activity logged yet.</div>';
        if (footer) footer.style.display = 'none';
      } else {
        if (listEl) listEl.innerHTML = auditLogs.map(renderAuditRow).join('');
        if (footer) {
          if (auditHasMore) {
            footer.style.display = 'block';
            footer.innerHTML = `
              <button type="button" class="admin-btn" id="loadMoreAuditBtn">
                <i class="fa-solid fa-circle-down"></i> Load More Logs (<span id="auditRemainingCount">${auditTotal - auditLogs.length}</span> remaining)
              </button>
            `;
            const newBtn = document.getElementById('loadMoreAuditBtn');
            if (newBtn) {
              newBtn.addEventListener('click', () => loadAuditLogs(false));
            }
          } else {
            footer.style.display = 'block';
            footer.innerHTML = '<div style="font-size:0.72rem; color:var(--text-muted); text-align:center; padding:4px 0;"><i class="fa-solid fa-check-double"></i> All audit records loaded</div>';
          }
        }
      }
    } else {
      throw new Error(`Server returned ${res.status}`);
    }
  } catch (err) {
    console.error('Failed to load audit logs', err);
    if (listEl && !auditLogs.length) {
      listEl.innerHTML = `<div class="admin-form-error">Could not load audit logs — ${esc(err.message)}</div>`;
    }
  } finally {
    auditLoading = false;
  }
}

function renderModalShell() {
  return `
    <div class="admin-modal-backdrop" id="accessModalBackdrop">
      <div class="admin-modal">
        <div class="admin-modal-header">
          <div class="admin-modal-title" id="accessModalTitle">Manage Account &amp; Dashboard Access</div>
          <button type="button" class="admin-modal-close" id="accessModalClose"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <div id="accessModalDashboardToggles"></div>
        <div class="admin-access-section-label" id="accessModalDashboardLabel" style="display:none;">
          <i class="fa-solid fa-city"></i> Assigned Company Accounts &amp; Dossiers
        </div>
        <div style="font-size:0.75rem; color:var(--text-muted); padding:0 24px 8px 24px; margin-top:-4px; display:none;" id="accessModalAccountSubLabel">
          Assign individual company accounts (e.g. BNY Mellon, BlackRock) to grant access to company intelligence, org charts, hiring trends, and financials.
        </div>
        <div class="admin-modal-toolbar" id="accessModalToolbar">
          <input type="text" class="admin-modal-search" id="accessModalSearch" placeholder="Filter company accounts (e.g. BNY, BlackRock)...">
        </div>
        <div class="admin-modal-body" id="accessModalBody"></div>
      </div>
    </div>

    <div class="admin-modal-backdrop" id="editUserModalBackdrop">
      <div class="admin-modal" style="max-width: 480px;">
        <div class="admin-modal-header">
          <div class="admin-modal-title" id="editUserModalTitle">
            <i class="fa-solid fa-user-gear"></i> Edit User Account
          </div>
          <button type="button" class="admin-modal-close" id="editUserModalClose"><i class="fa-solid fa-xmark"></i></button>
        </div>
        <form class="admin-modal-form" id="editUserForm">
          <input type="hidden" name="user_id" id="editUserId">
          <div class="admin-modal-body" style="padding:20px; display:flex; flex-direction:column; gap:14px;">
            <div class="admin-form-group">
              <label class="admin-form-label" for="editUserFullName"><i class="fa-solid fa-user"></i> Full Name</label>
              <input type="text" class="admin-form-input" id="editUserFullName" name="full_name" placeholder="e.g. Robin Vince">
            </div>
            <div class="admin-form-group">
              <label class="admin-form-label" for="editUserEmail"><i class="fa-solid fa-envelope"></i> Email Address <span style="font-weight:400; font-size:0.7rem; color:var(--text-muted); margin-left:4px;">(Read-only)</span></label>
              <input type="email" class="admin-form-input" id="editUserEmail" name="email" readonly disabled style="opacity:0.75; cursor:not-allowed; background:var(--input-bg);" placeholder="user@company.com" title="Email address cannot be modified">
            </div>
            <div class="admin-form-row" style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
              <div class="admin-form-group">
                <label class="admin-form-label" for="editUserRole"><i class="fa-solid fa-shield-halved"></i> System Role</label>
                <select class="admin-form-select" id="editUserRole" name="role">${roleOptions('user')}</select>
              </div>
              <div class="admin-form-group">
                <label class="admin-form-label" for="editUserStatus"><i class="fa-solid fa-heart-pulse"></i> Account Status</label>
                <select class="admin-form-select" id="editUserStatus" name="is_active">
                  <option value="true">Active</option>
                  <option value="false">Inactive</option>
                </select>
              </div>
            </div>
            <div class="admin-form-group">
              <label class="admin-form-label" for="editUserManager"><i class="fa-solid fa-sitemap"></i> Reports to <span style="font-weight:400; font-size:0.7rem; color:var(--text-muted);">(managers see their team's accounts)</span></label>
              <select class="admin-form-select" id="editUserManager" name="manager_id"></select>
            </div>
            <div class="admin-form-group">
              <span class="admin-form-label"><i class="fa-solid fa-layer-group"></i> Business lines</span>
              <div id="editUserBusinessLines" style="display:flex; flex-wrap:wrap; gap:6px 14px; font-size:0.8rem;"></div>
            </div>
            <div class="admin-form-group">
              <label class="admin-form-label" for="editUserPassword">
                <i class="fa-solid fa-key"></i> New Password <span style="font-weight:400; font-size:0.7rem; color:var(--text-muted);">(Leave blank to keep unchanged)</span>
              </label>
              <input type="password" class="admin-form-input" id="editUserPassword" name="password" placeholder="••••••••" autocomplete="new-password">
            </div>
            <div class="admin-form-error" id="editUserError" style="display:none;"></div>
          </div>
          <div class="admin-modal-footer" style="display:flex; justify-content:flex-end; gap:10px; padding:14px 20px; border-top:1px solid var(--panel-border); background:var(--panel-bg);">
            <button type="button" class="admin-btn" id="editUserCancelBtn">Cancel</button>
            <button type="submit" class="admin-btn primary" id="editUserSubmitBtn">
              <i class="fa-solid fa-circle-check"></i> Save Changes
            </button>
          </div>
        </form>
      </div>
    </div>
  `;
}

async function loadCrmTeam() {
  try {
    const [teamRes, metaRes] = await Promise.all([fetch('/api/crm/team'), fetch('/api/crm/meta')]);
    if (teamRes.ok) crmTeam = Object.fromEntries((await teamRes.json()).map(r => [r.id, r]));
    if (metaRes.ok) {
      const meta = await metaRes.json();
      crmBusinessLines = meta.business_lines || [];
      authEnforced = meta.auth_enforced !== false;
    }
  } catch { /* team info is optional on this page */ }
}

function teamLine(userId) {
  const t = crmTeam[userId];
  if (!t) return '';
  const bls = (t.business_line_ids || []).map(id => (crmBusinessLines.find(b => b.id === id) || {}).name).filter(Boolean);
  const bits = [t.manager_name ? `Reports to ${esc(t.manager_name)}` : '', bls.length ? esc(bls.join(', ')) : ''].filter(Boolean);
  return bits.length ? `<div style="font-size:0.68rem; color:var(--text-muted); margin-top:3px;">${bits.join(' · ')}</div>` : '';
}

function renderAdminTabsNav() {
  return `
    <div class="admin-tabs-nav">
      <button type="button" class="admin-tab-btn ${activeAdminTab === 'users' ? 'active' : ''}" data-admin-tab="users">
        <i class="bi bi-people-fill"></i> User Directory & Access Control
        <span class="admin-tab-badge">${usersCache.length}</span>
      </button>
      <button type="button" class="admin-tab-btn ${activeAdminTab === 'roles' ? 'active' : ''}" data-admin-tab="roles">
        <i class="fa-solid fa-user-shield"></i> Roles & Permissions
      </button>
      <button type="button" class="admin-tab-btn ${activeAdminTab === 'api-config' ? 'active' : ''}" data-admin-tab="api-config">
        <i class="bi bi-sliders"></i> API & Model Configuration
      </button>
      <button type="button" class="admin-tab-btn ${activeAdminTab === 'audit' ? 'active' : ''}" data-admin-tab="audit">
        <i class="bi bi-shield-check"></i> Security & Audit Trail
      </button>
      <button type="button" class="admin-tab-btn ${activeAdminTab === 'jobs' ? 'active' : ''}" data-admin-tab="jobs">
        <i class="fa-solid fa-clock-rotate-left"></i> Scheduled jobs
      </button>
    </div>
  `;
}

// ── Roles & permissions guide ───────────────────────────────────────────────
const ACCESS_ICON = { yes: 'fa-circle-check', no: 'fa-circle-minus', scoped: 'fa-building', team: 'fa-people-group',
  own: 'fa-user', read: 'fa-eye' };

async function loadRolesGuide() {
  if (rolesGuideCache) return;
  const res = await fetch('/api/crm/roles');
  if (!res.ok) throw new Error(`Couldn't load the roles guide (${res.status})`);
  rolesGuideCache = await res.json();
}

function renderRolesPanel() {
  const g = rolesGuideCache;
  if (!g) return '<div class="admin-panel"><div class="admin-panel-body">Couldn’t load the roles guide.</div></div>';
  const count = (key) => usersCache.filter(u => u.role === key && u.is_active !== false).length;
  const label = Object.fromEntries(g.roles.map(r => [r.key, r.label]));
  const cards = g.roles.map(r => `
    <article class="role-card" aria-labelledby="role-${r.key}">
      <header class="role-card-head">
        <span class="role-card-icon"><i class="fa-solid ${esc(r.icon)}"></i></span>
        <div><h3 id="role-${r.key}">${esc(r.label)}</h3>
          <button type="button" class="role-count" data-role-users="${r.key}" title="Show these users">${count(r.key)} active user${count(r.key) === 1 ? '' : 's'} <i class="fa-solid fa-arrow-right"></i></button></div>
      </header>
      <p class="role-summary">${esc(r.summary)}</p>
      <div class="role-block"><h4><i class="fa-solid fa-database"></i> Data access</h4><p>${esc(r.data_scope)}</p></div>
      <div class="role-block"><h4><i class="fa-solid fa-list-check"></i> Responsibilities</h4>
        <ul>${r.responsibilities.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>
      ${r.limits.length ? `<div class="role-block role-limits"><h4><i class="fa-solid fa-ban"></i> Can't</h4>
        <ul>${r.limits.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>` : ''}
    </article>`).join('');
  const cell = (row, role) => {
    const v = row.access[role];
    const note = (row.notes || {})[role];
    const text = note || g.access_labels[v] || v;
    return `<td class="role-cell ${esc(v)}"><span title="${esc(text)}"><i class="fa-solid ${ACCESS_ICON[v] || 'fa-circle'}" aria-hidden="true"></i>
      <span>${esc(text)}</span></span></td>`;
  };
  const matrix = `<div class="admin-table-wrap role-matrix-wrap"><table class="admin-table role-matrix">
    <caption class="sr-only">What each role can do</caption>
    <thead><tr><th scope="col">Capability</th>${g.order.map(k => `<th scope="col">${esc(label[k] || k)}</th>`).join('')}</tr></thead>
    <tbody>${g.capabilities.map(grp => `<tr class="role-group"><th colspan="${g.order.length + 1}" scope="colgroup">${esc(grp.group)}</th></tr>
      ${grp.items.map(row => `<tr><th scope="row">${esc(row.label)}</th>${g.order.map(k => cell(row, k)).join('')}</tr>`).join('')}`).join('')}
    </tbody></table></div>`;
  const legend = Object.entries(g.access_labels).map(([k, l]) => `<span class="role-cell ${esc(k)}"><span><i class="fa-solid ${ACCESS_ICON[k]}" aria-hidden="true"></i> ${esc(l)}</span></span>`).join('');
  return `
    <div class="admin-panel">
      <div class="admin-panel-header"><div class="admin-panel-title"><i class="fa-solid fa-user-shield"></i> Roles & responsibilities</div>
        <span class="role-note">Change someone's role in User Directory. The app enforces these rules; this page describes them.</span></div>
      <div class="admin-panel-body"><div class="role-cards">${cards}</div></div>
    </div>
    <div class="admin-panel" style="margin-top:16px;">
      <div class="admin-panel-header"><div class="admin-panel-title"><i class="fa-solid fa-table-cells"></i> Access by role</div>
        <div class="role-legend">${legend}</div></div>
      <div class="admin-panel-body" style="padding:0;">${matrix}</div>
    </div>`;
}

function findConfig(key) {
  return apiConfigsCache.find(c => c.config_key === key) || { config_key: key, value: '', is_configured: false, is_secret: false };
}

function renderFieldInput(c) {
  const meta = c.extra_metadata || {};
  const currentVal = dirtyConfigs[c.config_key] !== undefined ? dirtyConfigs[c.config_key] : (c.is_secret ? '' : (c.value || ''));

  if (meta.options && Array.isArray(meta.options)) {
    return `
      <select class="api-input" data-config-key="${c.config_key}">
        ${meta.options.map(opt => `<option value="${esc(opt)}" ${currentVal === opt ? 'selected' : ''}>${esc(opt)}</option>`).join('')}
      </select>
    `;
  }

  if (c.is_secret) {
    let cleanPlaceholder = meta.placeholder || (c.is_configured ? 'Key configured — paste new key to replace...' : 'Enter API key...');
    cleanPlaceholder = cleanPlaceholder.replace(/[•*]+/g, '').trim() || 'Enter API key...';

    // If dirty show user typed text, else if configured show masked dots
    let displayVal = '';
    if (dirtyConfigs[c.config_key] !== undefined) {
      displayVal = dirtyConfigs[c.config_key];
    } else if (c.is_configured) {
      displayVal = '••••••••••••••••••••••••';
    }

    const maskedSnippet = c.masked_value || (c.is_configured ? '••••••••••••••••' : '');

    return `
      <div class="api-input-wrap">
        <input type="password" class="api-input api-secret-input" data-config-key="${c.config_key}" data-masked-val="${esc(maskedSnippet)}" data-is-configured="${c.is_configured ? 'true' : 'false'}" value="${esc(displayVal)}" placeholder="${esc(cleanPlaceholder)}" autocomplete="off" onfocus="if(this.value.includes('••••')) this.select();">
        <button type="button" class="api-input-toggle-btn" title="Toggle visibility" onclick="
          const input = this.previousElementSibling;
          const masked = input.dataset.maskedVal || '';
          if (input.type === 'password') {
            input.type = 'text';
            if (input.value.includes('••••') && masked) { input.value = masked; }
            this.innerHTML = '<i class=\\'bi bi-eye-slash\\'></i>';
          } else {
            input.type = 'password';
            if (input.value === masked) { input.value = '••••••••••••••••••••••••'; }
            this.innerHTML = '<i class=\\'bi bi-eye\\'></i>';
          }
        "><i class="bi bi-eye"></i></button>
      </div>
    `;
  }

  return `
    <input type="${meta.type === 'number' ? 'number' : 'text'}" class="api-input" data-config-key="${c.config_key}" value="${esc(currentVal)}" placeholder="${esc(meta.placeholder || '')}">
  `;
}

function renderApiConfigPanel() {
  const llmProvider = findConfig('PRIMARY_LLM_PROVIDER');
  const openaiKey = findConfig('OPENAI_API_KEY');
  const openaiModel = findConfig('OPENAI_MODEL');
  const geminiKey = findConfig('GEMINI_API_KEY');
  const monidKey = findConfig('MONID_API_KEY');
  const monidUrl = findConfig('MONID_BASE_URL');

  const tavily = findConfig('TAVILY_API_KEY');
  const apify = findConfig('APIFY_TOKEN');
  const serper = findConfig('SERPER_API_KEY');
  const exa = findConfig('EXA_API_KEY');
  const firecrawl = findConfig('FIRECRAWL_API_KEY');
  const diffbot = findConfig('DIFFBOT_TOKEN');
  const fullenrich = findConfig('FULLENRICH_API_KEY');
  const dataGov = findConfig('DATA_GOV_API_KEY');
  const finnhub = findConfig('FINNHUB_API_KEY');

  const smtpHost = findConfig('SMTP_HOST');
  const smtpPort = findConfig('SMTP_PORT');
  const smtpUser = findConfig('SMTP_USERNAME');
  const smtpPass = findConfig('SMTP_PASSWORD');
  const smtpFrom = findConfig('SMTP_FROM');

  const jwtSecret = findConfig('JWT_SECRET_KEY');
  const timeoutSec = findConfig('HTTP_TIMEOUT_SECONDS');
  const retries = findConfig('MAX_API_RETRIES');

  const dirtyCount = Object.keys(dirtyConfigs).length;

  return `
    <div class="api-config-container">
      
      <!-- 1. AI & LLM Models -->
      <div class="api-config-section">
        <div class="api-config-section-header">
          <div class="api-config-section-title">
            <i class="bi bi-cpu-fill" style="color:var(--brand);"></i> AI &amp; LLM Intelligence Engines
          </div>
        </div>
        <div class="api-config-grid">
          
          <!-- OpenAI Direct / Primary LLM Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon purple"><i class="bi bi-stars"></i></div>
                <div>
                  <div class="api-config-card-title">OpenAI LLM Engine</div>
                  <div class="api-config-card-desc">Direct OpenAI API integration for standalone completions and intelligence pipelines.</div>
                </div>
              </div>
              <span class="api-status-badge ${openaiKey.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${openaiKey.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">OpenAI API Key</label>
              ${renderFieldInput(openaiKey)}
            </div>
            <div class="api-field-group">
              <label class="api-field-label">OpenAI Model</label>
              ${renderFieldInput(openaiModel)}
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Active Engine Routing</label>
              ${renderFieldInput(llmProvider)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);"><i class="bi bi-info-circle"></i> Direct connection to api.openai.com</span>
              <button type="button" class="api-test-btn" data-test-provider="openai">
                <i class="bi bi-lightning-charge"></i> Test OpenAI
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-openai"></div>
          </div>

          <!-- Google Gemini AI Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon cyan"><i class="bi bi-gem"></i></div>
                <div>
                  <div class="api-config-card-title">Google Gemini AI</div>
                  <div class="api-config-card-desc">Google AI Studio API key for Gemini 1.5 Pro and Flash extraction.</div>
                </div>
              </div>
              <span class="api-status-badge ${geminiKey.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${geminiKey.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Gemini API Key</label>
              ${renderFieldInput(geminiKey)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">aistudio.google.com</span>
              <button type="button" class="api-test-btn" data-test-provider="gemini">
                <i class="bi bi-lightning-charge"></i> Test Gemini
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-gemini"></div>
          </div>

          <!-- Monid.ai Gateway Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon green"><i class="bi bi-hdd-network"></i></div>
                <div>
                  <div class="api-config-card-title">Monid.ai Gateway</div>
                  <div class="api-config-card-desc">Monid enterprise data &amp; telemetry gateway.</div>
                </div>
              </div>
              <span class="api-status-badge ${monidKey.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${monidKey.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Monid API Key</label>
              ${renderFieldInput(monidKey)}
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Monid Base URL</label>
              ${renderFieldInput(monidUrl)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">api.monid.ai</span>
              <button type="button" class="api-test-btn" data-test-provider="monid">
                <i class="bi bi-lightning-charge"></i> Ping Monid
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-monid"></div>
          </div>

        </div>
      </div>

      <!-- 2. Web Search, Scraping & Entity Intelligence -->
      <div class="api-config-section">
        <div class="api-config-section-header">
          <div class="api-config-section-title">
            <i class="bi bi-search" style="color:#10b981;"></i> Web Search, Scraping &amp; Entity Intelligence
          </div>
        </div>
        <div class="api-config-grid">

          <!-- Tavily Search Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon green"><i class="bi bi-globe"></i></div>
                <div>
                  <div class="api-config-card-title">Tavily Search API</div>
                  <div class="api-config-card-desc">AI web research &amp; contextual signal discovery.</div>
                </div>
              </div>
              <span class="api-status-badge ${tavily.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${tavily.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">API Key</label>
              ${renderFieldInput(tavily)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">tavily.com</span>
              <button type="button" class="api-test-btn" data-test-provider="tavily">
                <i class="bi bi-lightning-charge"></i> Ping Search
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-tavily"></div>
          </div>

          <!-- Apify Scraping Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon amber"><i class="bi bi-robot"></i></div>
                <div>
                  <div class="api-config-card-title">Apify Web Scraping</div>
                  <div class="api-config-card-desc">Cloud actors for LinkedIn &amp; corporate portal extraction.</div>
                </div>
              </div>
              <span class="api-status-badge ${apify.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${apify.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">API Token</label>
              ${renderFieldInput(apify)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">apify.com</span>
              <button type="button" class="api-test-btn" data-test-provider="apify">
                <i class="bi bi-lightning-charge"></i> Test Token
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-apify"></div>
          </div>

          <!-- Serper Google Search Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon cyan"><i class="bi bi-google"></i></div>
                <div>
                  <div class="api-config-card-title">Serper Google Search</div>
                  <div class="api-config-card-desc">Google News and organic search indexing for CXO moves.</div>
                </div>
              </div>
              <span class="api-status-badge ${serper.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${serper.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">API Key</label>
              ${renderFieldInput(serper)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">serper.dev</span>
              <button type="button" class="api-test-btn" data-test-provider="serper">
                <i class="bi bi-lightning-charge"></i> Test Serper
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-serper"></div>
          </div>

          <!-- Firecrawl Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon purple"><i class="bi bi-fire"></i></div>
                <div>
                  <div class="api-config-card-title">Firecrawl Web Extraction</div>
                  <div class="api-config-card-desc">Deep web markdown extraction &amp; dynamic JS rendering.</div>
                </div>
              </div>
              <span class="api-status-badge ${firecrawl.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${firecrawl.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Firecrawl API Key</label>
              ${renderFieldInput(firecrawl)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">firecrawl.dev</span>
              <button type="button" class="api-test-btn" data-test-provider="firecrawl">
                <i class="bi bi-lightning-charge"></i> Test Firecrawl
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-firecrawl"></div>
          </div>

          <!-- Exa Neural Search Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon cyan"><i class="bi bi-compass"></i></div>
                <div>
                  <div class="api-config-card-title">Exa Neural Search</div>
                  <div class="api-config-card-desc">Embeddings-based semantic search &amp; company research.</div>
                </div>
              </div>
              <span class="api-status-badge ${exa.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${exa.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Exa Search API Key</label>
              ${renderFieldInput(exa)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">exa.ai</span>
              <button type="button" class="api-test-btn" data-test-provider="exa">
                <i class="bi bi-lightning-charge"></i> Test Exa
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-exa"></div>
          </div>

          <!-- FullEnrich Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon purple"><i class="bi bi-person-badge-fill"></i></div>
                <div>
                  <div class="api-config-card-title">FullEnrich Persona Enrichment</div>
                  <div class="api-config-card-desc">Waterfall email &amp; phone enrichment for executive contacts.</div>
                </div>
              </div>
              <span class="api-status-badge ${fullenrich.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${fullenrich.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">FullEnrich API Key</label>
              ${renderFieldInput(fullenrich)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">fullenrich.com</span>
              <button type="button" class="api-test-btn" data-test-provider="fullenrich">
                <i class="bi bi-lightning-charge"></i> Test FullEnrich
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-fullenrich"></div>
          </div>

          <!-- Diffbot Knowledge Graph Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon blue"><i class="bi bi-diagram-3-fill"></i></div>
                <div>
                  <div class="api-config-card-title">Diffbot Knowledge Graph</div>
                  <div class="api-config-card-desc">Autonomous AI entity extraction &amp; corporate org structure mapping.</div>
                </div>
              </div>
              <span class="api-status-badge ${diffbot.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${diffbot.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Diffbot Knowledge Graph Token</label>
              ${renderFieldInput(diffbot)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">diffbot.com</span>
              <button type="button" class="api-test-btn" data-test-provider="diffbot">
                <i class="bi bi-lightning-charge"></i> Test Diffbot
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-diffbot"></div>
          </div>

          <!-- Data.gov / SEC EDGAR Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon green"><i class="bi bi-bank2"></i></div>
                <div>
                  <div class="api-config-card-title">Data.gov &amp; SEC EDGAR</div>
                  <div class="api-config-card-desc">Federal open data registry &amp; regulatory filings intelligence.</div>
                </div>
              </div>
              <span class="api-status-badge ${dataGov.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${dataGov.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Data.gov / SEC EDGAR API Key</label>
              ${renderFieldInput(dataGov)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">data.gov</span>
              <button type="button" class="api-test-btn" data-test-provider="data_gov">
                <i class="bi bi-lightning-charge"></i> Test Data.gov
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-data_gov"></div>
          </div>

          <!-- Finnhub Financial Market Card -->
          <div class="api-config-card">
            <div class="api-config-card-header">
              <div class="api-config-card-title-wrap">
                <div class="api-config-card-icon amber"><i class="bi bi-graph-up"></i></div>
                <div>
                  <div class="api-config-card-title">Finnhub Market Data</div>
                  <div class="api-config-card-desc">Real-time stock quotes, institutional sentiment &amp; company news.</div>
                </div>
              </div>
              <span class="api-status-badge ${finnhub.is_configured ? 'configured' : 'not-configured'}">
                <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${finnhub.is_configured ? 'Active' : 'Unset'}
              </span>
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Finnhub API Key</label>
              ${renderFieldInput(finnhub)}
            </div>
            <div class="api-card-actions">
              <span style="font-size:0.72rem; color:var(--text-muted);">finnhub.io</span>
              <button type="button" class="api-test-btn" data-test-provider="finnhub">
                <i class="bi bi-lightning-charge"></i> Test Finnhub
              </button>
            </div>
            <div class="api-test-result d-none" id="testResult-finnhub"></div>
          </div>

        </div>
      </div>

      <!-- 3. Email & Alert Delivery (SMTP) -->
      <div class="api-config-section">
        <div class="api-config-section-header">
          <div class="api-config-section-title">
            <i class="bi bi-envelope-check-fill" style="color:#0061ff;"></i> Email &amp; Daily News Digest Delivery
          </div>
        </div>
        <div class="api-config-card">
          <div class="api-config-card-header">
            <div class="api-config-card-title-wrap">
              <div class="api-config-card-icon blue"><i class="bi bi-send-fill"></i></div>
              <div>
                <div class="api-config-card-title">SMTP Mailer Configuration</div>
                <div class="api-config-card-desc">Transactional mailer used for user onboarding, password resets, and automated daily intelligence alerts.</div>
              </div>
            </div>
            <span class="api-status-badge ${smtpHost.is_configured ? 'configured' : 'not-configured'}">
              <i class="bi bi-circle-fill" style="font-size:0.45rem;"></i> ${smtpHost.is_configured ? 'Configured' : 'Unset'}
            </span>
          </div>
          
          <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:10px;">
            <div class="api-field-group">
              <label class="api-field-label">SMTP Host</label>
              ${renderFieldInput(smtpHost)}
            </div>
            <div class="api-field-group">
              <label class="api-field-label">SMTP Port</label>
              ${renderFieldInput(smtpPort)}
            </div>
            <div class="api-field-group">
              <label class="api-field-label">SMTP Username</label>
              ${renderFieldInput(smtpUser)}
            </div>
            <div class="api-field-group">
              <label class="api-field-label">SMTP Password</label>
              ${renderFieldInput(smtpPass)}
            </div>
            <div class="api-field-group" style="grid-column:1 / -1;">
              <label class="api-field-label">Sender 'From' Address</label>
              ${renderFieldInput(smtpFrom)}
            </div>
          </div>

          <div class="api-card-actions">
            <span style="font-size:0.70rem; color:var(--text-muted);"><i class="bi bi-lock-fill"></i> TLS 587 / SSL 465 supported</span>
            <button type="button" class="api-test-btn" data-test-provider="smtp">
              <i class="bi bi-send-check"></i> Test SMTP Handshake
            </button>
          </div>
          <div class="api-test-result d-none" id="testResult-smtp"></div>
        </div>
      </div>

      <!-- 4. Security & System Parameters -->
      <div class="api-config-section">
        <div class="api-config-section-header">
          <div class="api-config-section-title">
            <i class="bi bi-shield-lock-fill" style="color:#64748b;"></i> Security &amp; Operational Controls
          </div>
        </div>
        <div class="api-config-card">
          <div class="api-field-group" style="margin-bottom:8px;">
            <label class="api-field-label">JWT Master Secret Key <span style="font-weight:normal; color:var(--text-muted);">(Used to cryptographically sign tokens)</span></label>
            ${renderFieldInput(jwtSecret)}
          </div>
          <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:10px;">
            <div class="api-field-group">
              <label class="api-field-label">HTTP Timeout Threshold (Seconds)</label>
              ${renderFieldInput(timeoutSec)}
            </div>
            <div class="api-field-group">
              <label class="api-field-label">Max Network Retries on Failure</label>
              ${renderFieldInput(retries)}
            </div>
          </div>
        </div>
      </div>

    </div>

    <!-- Sticky Floating Action Bar -->
    <div class="api-sticky-save-bar ${dirtyCount > 0 ? 'visible' : ''}" id="apiStickySaveBar">
      <div class="api-sticky-text">
        <i class="bi bi-exclamation-circle-fill" style="color:#f59e0b; font-size:1.1rem;"></i>
        <span id="apiDirtyCountText">${dirtyCount} unsaved configuration change${dirtyCount === 1 ? '' : 's'}</span>
      </div>
      <div class="api-sticky-actions">
        <button type="button" class="api-btn-discard" id="apiDiscardBtn">Discard</button>
        <button type="button" class="api-btn-save" id="apiSaveBtn">
          <i class="bi bi-check2-circle"></i> Save All Changes
        </button>
      </div>
    </div>
  `;
}

async function loadApiConfigs() {
  try {
    const res = await fetch('/api/admin/api-config');
    if (res.ok) {
      const data = await res.json();
      apiConfigsCache = data.configs || [];
    }
  } catch (err) {
    console.error('Failed to load API configs', err);
  }
}

async function loadAndRender() {
  const [statsRes, usersRes] = await Promise.all([
    fetch('/api/admin/stats'),
    fetch('/api/admin/users'),
    loadApiConfigs()
  ]);
  if (!statsRes.ok || !usersRes.ok) throw new Error('Failed to load admin data');
  statsCache = await statsRes.json();
  const usersData = await usersRes.json();
  usersCache = usersData.users || [];
  await loadCrmTeam();

  const me = getCurrentUser();   // the topbar user menu is drawn by initTopbarAuth()

  let tabContentHtml = '';
  if (activeAdminTab === 'users') {
    tabContentHtml = `
      ${renderCreateForm()}
      <div class="admin-grid">
        <div class="admin-col-main">
          ${renderUsersTable(me ? me.id : null)}
        </div>
        <div class="admin-col-side">
          ${renderActivity()}
        </div>
      </div>
    `;
  } else if (activeAdminTab === 'roles') {
    try { await loadRolesGuide(); } catch (err) { console.error(err); }
    tabContentHtml = renderRolesPanel();
  } else if (activeAdminTab === 'api-config') {
    tabContentHtml = renderApiConfigPanel();
  } else if (activeAdminTab === 'jobs') {
    tabContentHtml = '<div id="adminJobsMount"></div>';   // admin-jobs.js
  } else if (activeAdminTab === 'audit') {
    tabContentHtml = `
      <div class="admin-grid" style="grid-template-columns: 1fr;">
        <div class="admin-col-main">
          ${renderActivity()}
        </div>
      </div>
    `;
  }

  main.innerHTML = `
    ${authEnforced ? '' : `<div class="admin-form-error" style="display:flex; margin-bottom:14px;">
      <i class="fa-solid fa-triangle-exclamation"></i>&nbsp; AUTH_ENFORCED is off — every request runs as a super admin, so roles
      (manager scope, viewer read-only, partner isolation) are not applied. Set AUTH_ENFORCED=true in production.</div>`}
    ${renderHero()}
    ${renderKPIBanner(statsCache)}
    ${renderAdminTabsNav()}
    <div id="adminTabContent">
      ${tabContentHtml}
    </div>
    <div id="crmSettingsMount" style="margin-top:16px;"></div>
    ${renderModalShell()}
  `;
  wireEvents();
  mountCrmSettings(document.getElementById('crmSettingsMount'));
  if (activeAdminTab === 'jobs') mountJobs(document.getElementById('adminJobsMount'));
  if (activeAdminTab === 'users' || activeAdminTab === 'audit') {
    loadAuditLogs(true);
  }
}

function wireEvents() {
  const me = getCurrentUser();
  const currentUserId = me ? me.id : null;

  // Admin Tabs Navigation
  main.querySelectorAll('[data-admin-tab]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const targetTab = btn.dataset.adminTab;
      if (targetTab === activeAdminTab) return;

      if (Object.keys(dirtyConfigs).length > 0) {
        if (!confirm('You have unsaved API configuration changes. Discard and switch tabs?')) {
          return;
        }
        dirtyConfigs = {};
      }

      activeAdminTab = targetTab;
      await loadAndRender();
    });
  });

  // Roles guide: "N active users" jumps to the User Directory filtered by that role
  main.querySelectorAll('[data-role-users]').forEach(btn => {
    btn.addEventListener('click', async () => {
      activeAdminTab = 'users';
      currentFilter = btn.dataset.roleUsers;
      searchQuery = '';
      await loadAndRender();
    });
  });

  // API Config inputs & dirty tracking
  main.querySelectorAll('.api-input').forEach(input => {
    const handleInput = () => {
      const key = input.dataset.configKey;
      if (input.value.includes('••••') && input.dataset.isConfigured === 'true' && (input.value === '••••••••••••••••••••••••' || input.value === input.dataset.maskedVal)) {
        delete dirtyConfigs[key];
      } else {
        dirtyConfigs[key] = input.value;
      }
      const bar = document.getElementById('apiStickySaveBar');
      const text = document.getElementById('apiDirtyCountText');
      const dirtyCount = Object.keys(dirtyConfigs).length;
      if (bar) {
        if (dirtyCount > 0) {
          bar.classList.add('visible');
          if (text) text.textContent = `${dirtyCount} unsaved configuration change${dirtyCount === 1 ? '' : 's'}`;
        } else {
          bar.classList.remove('visible');
        }
      }
    };
    input.addEventListener('input', handleInput);
    input.addEventListener('change', handleInput);
  });

  // API Config Test Buttons
  main.querySelectorAll('[data-test-provider]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const provider = btn.dataset.testProvider;
      const resultEl = document.getElementById(`testResult-${provider}`);
      btn.disabled = true;
      const originalText = btn.innerHTML;
      btn.innerHTML = '<div class="spinner-sm"></div> Testing…';
      if (resultEl) {
        resultEl.classList.remove('d-none', 'success', 'error');
        resultEl.innerHTML = 'Connecting to provider…';
      }

      try {
        const credentials = {};
        main.querySelectorAll('.api-config-card .api-input').forEach(inp => {
          if (inp.dataset.configKey && !inp.value.includes('••••')) {
            credentials[inp.dataset.configKey] = inp.value;
          }
        });

        const res = await fetch('/api/admin/api-config/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ provider, credentials }),
        });
        const data = await res.json();
        if (resultEl) {
          if (data.success) {
            resultEl.className = 'api-test-result success';
            resultEl.innerHTML = `<i class="bi bi-check-circle-fill"></i> ${esc(data.message)} (${data.latency_ms || 0}ms)`;
          } else {
            resultEl.className = 'api-test-result error';
            resultEl.innerHTML = `<i class="bi bi-exclamation-triangle-fill"></i> ${esc(data.error || 'Connection failed')}`;
          }
        }
      } catch (err) {
        if (resultEl) {
          resultEl.className = 'api-test-result error';
          resultEl.innerHTML = `<i class="bi bi-exclamation-triangle-fill"></i> Network error: ${esc(err.message)}`;
        }
      } finally {
        btn.disabled = false;
        btn.innerHTML = originalText;
      }
    });
  });

  // Discard Changes
  const discardBtn = document.getElementById('apiDiscardBtn');
  if (discardBtn) {
    discardBtn.addEventListener('click', () => {
      dirtyConfigs = {};
      loadAndRender();
    });
  }

  // Save Changes
  const saveBtn = document.getElementById('apiSaveBtn');
  if (saveBtn) {
    saveBtn.addEventListener('click', async () => {
      saveBtn.disabled = true;
      saveBtn.innerHTML = '<div class="spinner-sm"></div> Saving…';
      try {
        const res = await fetch('/api/admin/api-config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ configs: dirtyConfigs }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Failed to save configuration');
        dirtyConfigs = {};
        showToast(data.message || 'API configurations saved successfully');
        await loadAndRender();
      } catch (err) {
        alert(err.message);
      } finally {
        saveBtn.disabled = false;
        saveBtn.innerHTML = '<i class="bi bi-check2-circle"></i> Save All Changes';
      }
    });
  }

  // Search input
  const searchInput = document.getElementById('adminUserSearch');
  const searchClear = document.getElementById('adminUserSearchClear');
  if (searchInput) {
    if (searchQuery) searchClear.style.display = 'block';
    searchInput.addEventListener('input', (e) => {
      searchQuery = e.target.value.trim();
      searchClear.style.display = searchQuery ? 'block' : 'none';
      updateTableOnly(currentUserId);
    });
  }
  if (searchClear) {
    searchClear.addEventListener('click', () => {
      searchQuery = '';
      searchInput.value = '';
      searchClear.style.display = 'none';
      updateTableOnly(currentUserId);
    });
  }

  // Filter chips
  main.querySelectorAll('.admin-filter-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      main.querySelectorAll('.admin-filter-chip').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentFilter = btn.dataset.filter;
      updateTableOnly(currentUserId);
    });
  });

  // Create User Form
  const form = document.getElementById('createUserForm');
  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const errorEl = document.getElementById('createUserError');
      const submitBtn = document.getElementById('createUserSubmit');
      errorEl.style.display = 'none';
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<div class="spinner-sm"></div> Creating Account…';
      try {
        const body = {
          full_name: form.full_name.value.trim() || null,
          email: form.email.value.trim(),
          password: form.password.value,
          role: form.role.value,
        };
        const res = await fetch('/api/admin/users', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Could not create user');
        form.reset();
        showToast(`User ${data.email} created successfully`);
        await loadAndRender();
      } catch (err) {
        errorEl.textContent = err.message;
        errorEl.style.display = 'flex';
      } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-circle-plus"></i> Create User Account';
      }
    });
  }

  // User Actions (Role, Toggle Active, Delete, Access)
  const table = main.querySelector('.admin-table');
  if (table) {
    table.addEventListener('change', async (e) => {
      const select = e.target.closest('[data-action="role"]');
      if (!select) return;
      const userId = select.closest('tr').dataset.userId;
      try {
        const res = await fetch(`/api/admin/users/${userId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ role: select.value }),
        });
        if (!res.ok) throw new Error('Failed to update role');
        showToast('User role updated');
        await loadAndRender();
      } catch (err) {
        alert(err.message);
      }
    });

    // Kebab menu toggle & item click delegation
    table.addEventListener('click', async (e) => {
      const kebabBtn = e.target.closest('[data-action="kebab-toggle"]');
      if (kebabBtn) {
        e.stopPropagation();
        const wrap = kebabBtn.closest('.admin-kebab-wrap');
        const menu = wrap.querySelector('.admin-kebab-menu');
        const isHidden = menu.classList.contains('d-none');
        // Close all other open kebab menus and remove open class
        document.querySelectorAll('.admin-kebab-menu').forEach(m => m.classList.add('d-none'));
        document.querySelectorAll('.admin-kebab-wrap').forEach(w => w.classList.remove('open'));
        if (isHidden) {
          wrap.classList.add('open');
          const rect = kebabBtn.getBoundingClientRect();
          if (window.innerHeight - rect.bottom < 170) {
            menu.classList.add('dropup');
          } else {
            menu.classList.remove('dropup');
          }
          menu.classList.remove('d-none');
        }
        return;
      }

      const actionItem = e.target.closest('button[data-action]');
      if (!actionItem || actionItem.disabled) return;
      const tr = actionItem.closest('tr');
      if (!tr) return;
      const userId = parseInt(tr.dataset.userId, 10);
      const user = usersCache.find(u => u.id === userId);
      if (!user) return;
      const isSelf = currentUserId === userId;

      // Close open kebab menu on action
      document.querySelectorAll('.admin-kebab-menu').forEach(m => m.classList.add('d-none'));
      document.querySelectorAll('.admin-kebab-wrap').forEach(w => w.classList.remove('open'));

      if (actionItem.dataset.action === 'edit-user') {
        openEditUserModal(user, isSelf);
      } else if (actionItem.dataset.action === 'toggle-active') {
        if (isSelf && user.is_active) {
          showToast('You cannot deactivate your own logged-in account.');
          return;
        }
        try {
          const res = await fetch(`/api/admin/users/${userId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_active: !user.is_active }),
          });
          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || 'Failed to update user status');
          }
          showToast(`User ${user.email} is now ${!user.is_active ? 'Active' : 'Inactive'}`);
          await loadAndRender();
        } catch (err) {
          alert(err.message);
        }
      } else if (actionItem.dataset.action === 'delete') {
        if (isSelf) {
          showToast('You cannot delete your own logged-in account.');
          return;
        }
        if (!window.confirm(`Are you sure you want to permanently delete user "${user.email}"? This action cannot be undone.`)) return;
        try {
          const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
          if (!res.ok) {
            const errData = await res.json().catch(() => ({}));
            throw new Error(errData.detail || 'Failed to delete user');
          }
          showToast(`User ${user.email} deleted`);
          await loadAndRender();
        } catch (err) {
          alert(err.message);
        }
      } else if (actionItem.dataset.action === 'access') {
        openAccessModal(userId, user.email);
      }
    });
  }

  // Close open kebab menus on document click
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.admin-kebab-wrap')) {
      document.querySelectorAll('.admin-kebab-menu').forEach(m => m.classList.add('d-none'));
      document.querySelectorAll('.admin-kebab-wrap').forEach(w => w.classList.remove('open'));
    }
  });

  // Edit User Form submission
  const editForm = document.getElementById('editUserForm');
  if (editForm) {
    editForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const userId = document.getElementById('editUserId').value;
      const errorEl = document.getElementById('editUserError');
      const submitBtn = document.getElementById('editUserSubmitBtn');

      errorEl.style.display = 'none';
      submitBtn.disabled = true;
      submitBtn.innerHTML = '<div class="spinner-sm"></div> Saving…';

      try {
        const payload = {
          full_name: editForm.full_name.value.trim() || null,
          role: editForm.role.value,
          is_active: editForm.is_active.value === 'true',
        };
        const passVal = editForm.password.value;
        if (passVal) {
          payload.password = passVal;
        }

        const res = await fetch(`/api/admin/users/${userId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Failed to update user profile');
        const mgrVal = document.getElementById('editUserManager').value;
        const crmBody = {
          business_line_ids: [...document.querySelectorAll('#editUserBusinessLines input:checked')].map(i => Number(i.value)),
          ...(mgrVal ? { manager_id: Number(mgrVal) } : { clear_manager: true }),
        };
        const crmRes = await fetch(`/api/crm/users/${userId}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(crmBody),
        });
        const crmData = await crmRes.json().catch(() => ({}));
        if (!crmRes.ok) throw new Error(crmData.detail || 'Saved profile, but could not update manager / business lines');

        closeEditUserModal();
        showToast(`User ${data.email} updated successfully`);
        await loadAndRender();
      } catch (err) {
        errorEl.textContent = err.message;
        errorEl.style.display = 'flex';
      } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '<i class="fa-solid fa-circle-check"></i> Save Changes';
      }
    });
  }

  // Edit Modal close handlers
  const editCloseBtn = document.getElementById('editUserModalClose');
  if (editCloseBtn) editCloseBtn.addEventListener('click', closeEditUserModal);

  const editCancelBtn = document.getElementById('editUserCancelBtn');
  if (editCancelBtn) editCancelBtn.addEventListener('click', closeEditUserModal);

  const editBackdrop = document.getElementById('editUserModalBackdrop');
  if (editBackdrop) {
    editBackdrop.addEventListener('click', (e) => {
      if (e.target.id === 'editUserModalBackdrop') closeEditUserModal();
    });
  }

  // Access Modal close handlers
  document.getElementById('accessModalClose').addEventListener('click', closeAccessModal);
  document.getElementById('accessModalBackdrop').addEventListener('click', (e) => {
    if (e.target.id === 'accessModalBackdrop') closeAccessModal();
  });
}

function closeEditUserModal() {
  const backdrop = document.getElementById('editUserModalBackdrop');
  if (backdrop) backdrop.classList.remove('open');
}

function openEditUserModal(user, isSelf) {
  const backdrop = document.getElementById('editUserModalBackdrop');
  const title = document.getElementById('editUserModalTitle');
  const idInput = document.getElementById('editUserId');
  const nameInput = document.getElementById('editUserFullName');
  const emailInput = document.getElementById('editUserEmail');
  const roleSelect = document.getElementById('editUserRole');
  const statusSelect = document.getElementById('editUserStatus');
  const passInput = document.getElementById('editUserPassword');
  const errorEl = document.getElementById('editUserError');

  if (!backdrop || !user) return;

  if (title) title.innerHTML = `<i class="fa-solid fa-user-gear"></i> Edit User — ${esc(user.email)}`;
  if (idInput) idInput.value = user.id;
  if (nameInput) nameInput.value = user.full_name || '';
  if (emailInput) emailInput.value = user.email || '';
  if (roleSelect) {
    roleSelect.value = user.role || 'user';
    roleSelect.disabled = isSelf;
    roleSelect.title = isSelf ? 'You cannot change your own role' : '';
  }
  if (statusSelect) {
    statusSelect.value = user.is_active ? 'true' : 'false';
    statusSelect.disabled = isSelf;
    statusSelect.title = isSelf ? 'You cannot deactivate your own account' : '';
  }
  if (passInput) passInput.value = '';
  const team = crmTeam[user.id] || {};
  const mgr = document.getElementById('editUserManager');
  if (mgr) {
    const candidates = usersCache.filter(x => x.id !== user.id && ['sales_manager', 'super_admin'].includes(x.role));
    mgr.innerHTML = '<option value="">— No manager —</option>' + candidates.map(x =>
      `<option value="${x.id}" ${x.id === team.manager_id ? 'selected' : ''}>${esc(x.full_name || x.email)} · ${esc(ROLE_LABEL[x.role] || x.role)}</option>`).join('');
  }
  const bls = document.getElementById('editUserBusinessLines');
  if (bls) {
    bls.innerHTML = crmBusinessLines.filter(b => b.active).map(b => `<label style="display:inline-flex; gap:5px; align-items:center;">
      <input type="checkbox" value="${b.id}" ${(team.business_line_ids || []).includes(b.id) ? 'checked' : ''}> ${esc(b.name)}</label>`).join('')
      || '<span style="color:var(--text-muted)">No business lines configured</span>';
  }
  if (errorEl) {
    errorEl.textContent = '';
    errorEl.style.display = 'none';
  }

  backdrop.classList.add('open');
}

function updateTableOnly(currentUserId) {
  const filtered = getFilteredUsers();
  const countBadge = document.getElementById('userCountBadge');
  if (countBadge) countBadge.textContent = `${filtered.length} of ${usersCache.length} users`;

  const tbody = document.getElementById('adminTableBody');
  if (!tbody) return;

  const rows = filtered.map(u => {
    const initials = getInitials(u.full_name, u.email);
    const isAdmin = u.role === 'super_admin';
    const isSelf = u.id === currentUserId;

    return `
      <tr data-user-id="${u.id}">
        <td>
          <div class="admin-user-cell">
            <div class="admin-avatar ${isAdmin ? 'admin-role' : ''}">${initials}</div>
            <div>
              <div class="admin-user-name">${esc(u.full_name || '—')}${isSelf ? ' <span style="font-size:0.68rem; color:var(--brand); font-weight:700;">(You)</span>' : ''}</div>
              <div class="admin-user-email">${esc(u.email)}</div>
            </div>
          </div>
        </td>
        <td>
          <select class="admin-role-select" data-action="role" ${isSelf ? 'disabled title="You cannot change your own role"' : ''}>${roleOptions(u.role)}</select>
          ${teamLine(u.id)}
        </td>
        <td>
          <span class="admin-status-pill ${u.is_active ? 'active' : 'inactive'}">
            <span class="dot"></span> ${u.is_active ? 'Active' : 'Inactive'}
          </span>
        </td>
        <td style="color:var(--text-muted); font-size:0.75rem;">
          ${timeAgo(u.last_login_at)}
        </td>
        <td>
          <div class="admin-row-actions">
            <button type="button" class="admin-btn admin-access-btn" data-action="access" title="Manage account visibility">
              <i class="fa-solid fa-shield-halved"></i> Access
            </button>
            <div class="admin-kebab-wrap">
              <button type="button" class="admin-btn admin-kebab-btn" data-action="kebab-toggle" title="More options" aria-haspopup="true">
                <i class="fa-solid fa-ellipsis-vertical"></i>
              </button>
              <div class="admin-kebab-menu d-none">
                <button type="button" class="admin-kebab-item" data-action="edit-user">
                  <i class="fa-solid fa-pen-to-square"></i> Edit User
                </button>
                ${!isAdmin ? `
                  <button type="button" class="admin-kebab-item" data-action="toggle-active">
                    <i class="fa-solid fa-power-off"></i> ${u.is_active ? 'Deactivate' : 'Activate'}
                  </button>
                  <div class="admin-kebab-divider"></div>
                  <button type="button" class="admin-kebab-item danger" data-action="delete">
                    <i class="fa-solid fa-trash"></i> Delete User
                  </button>
                ` : ''}
              </div>
            </div>
          </div>
        </td>
      </tr>
    `;
  }).join('');

  tbody.innerHTML = rows || `<tr><td colspan="5" style="text-align:center; padding:36px; color:var(--text-muted);"><i class="fa-solid fa-magnifying-glass" style="font-size:1.5rem; display:block; margin-bottom:8px;"></i> No users matched your search criteria.</td></tr>`;
}

function closeAccessModal() {
  document.getElementById('accessModalBackdrop').classList.remove('open');
}

function renderDashboardAccessToggles(userId, permissions, disabled) {
  const wrap = document.getElementById('accessModalDashboardToggles');
  if (!wrap) return;

  const dashboards = [
    {
      key: 'has_dashboard_access',
      name: 'Global Executive Digest & Radar',
      icon: 'fa-solid fa-chart-line',
      path: '/',
      sub: 'Cross-account portfolio overview, executive briefing & macro sales alerts',
    },
    {
      key: 'has_command_center_access',
      name: 'Sales Command Center',
      icon: 'fa-solid fa-arrow-trend-up',
      path: '/command-center',
      sub: 'Action-first rep & manager daily command dashboard',
    },
    {
      key: 'has_tasks_access',
      name: 'My Tasks & Action Items',
      icon: 'fa-solid fa-list-check',
      path: '/tasks',
      sub: 'Personal task manager, reminders & cross-account deliverables',
    },
    {
      key: 'has_pipeline_access',
      name: 'Data Pipeline Console',
      icon: 'fa-solid fa-compass',
      path: '/pipeline',
      sub: 'Raw data ingestion pipeline & automated scraper runs',
    },
  ];

  wrap.innerHTML = `
    <div class="admin-access-section-label">
      <i class="fa-solid fa-table-cells"></i> Dashboard &amp; View Permissions
    </div>
    <div class="admin-access-dashboards-grid">
      ${dashboards.map(d => {
    const hasAccess = !!permissions[d.key];
    return `
          <div class="admin-access-row">
            <label class="admin-access-toggle">
              <input type="checkbox" data-perm-key="${d.key}" ${hasAccess ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
              <div>
                <div><i class="${d.icon}" style="margin-right:4px; color:var(--brand);"></i> <strong>${esc(d.name)}</strong> <span style="font-size:0.7rem; color:var(--text-muted);">(${d.path})</span></div>
                <div style="font-size:0.72rem; color:var(--text-muted); font-weight:normal;">${esc(d.sub)}</div>
              </div>
            </label>
            <span style="font-size:0.7rem; color:var(--text-muted); white-space:nowrap; margin-left:8px;">
              ${disabled ? 'Always on for Super Admin' : (hasAccess ? '<i class="fa-solid fa-circle-check" style="color:var(--success);"></i> Granted' : '<i class="fa-solid fa-ban" style="color:var(--danger);"></i> Restricted')}
            </span>
          </div>
        `;
  }).join('')}
    </div>
  `;

  if (disabled) return;

  wrap.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.addEventListener('change', async () => {
      const permKey = cb.dataset.permKey;
      cb.disabled = true;
      try {
        const res = await fetch(`/api/admin/users/${userId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ [permKey]: cb.checked }),
        });
        if (!res.ok) throw new Error('Request failed');
        permissions[permKey] = cb.checked;
        showToast(`Updated dashboard permission`);
        renderDashboardAccessToggles(userId, permissions, false);
      } catch (err) {
        showToast('Failed to update dashboard access permission.');
        cb.checked = !cb.checked;
        cb.disabled = false;
      }
    });
  });
}

async function openAccessModal(userId, email) {
  const backdrop = document.getElementById('accessModalBackdrop');
  const body = document.getElementById('accessModalBody');
  const searchInput = document.getElementById('accessModalSearch');
  const toolbar = document.getElementById('accessModalToolbar');
  const dashboardLabel = document.getElementById('accessModalDashboardLabel');
  const accountSubLabel = document.getElementById('accessModalAccountSubLabel');
  const togglesWrap = document.getElementById('accessModalDashboardToggles');

  document.getElementById('accessModalTitle').textContent = `Account & Dashboard Access — ${email}`;
  if (togglesWrap) togglesWrap.innerHTML = '';
  if (dashboardLabel) dashboardLabel.style.display = 'none';
  if (accountSubLabel) accountSubLabel.style.display = 'none';
  body.innerHTML = '<div class="admin-page-loading" style="margin:20px auto;"><div class="spinner-sm"></div> Loading permissions &amp; accounts…</div>';
  backdrop.classList.add('open');

  try {
    const res = await fetch(`/api/admin/users/${userId}/accounts`);
    const data = await res.json();
    if (data.role === 'super_admin') {
      toolbar.style.display = 'none';
      renderDashboardAccessToggles(userId, {
        has_dashboard_access: true,
        has_command_center_access: true,
        has_tasks_access: true,
        has_pipeline_access: true,
      }, true);
      body.innerHTML = `
        <div style="padding:24px; text-align:center; color:var(--text-secondary);">
          <i class="fa-solid fa-shield-halved" style="font-size:2rem; color:var(--brand); display:block; margin-bottom:12px;"></i>
          <strong>Super Admin Global Access</strong>
          <p style="font-size:0.8rem; color:var(--text-muted); margin-top:6px;">
            This user is a Super Admin and automatically holds unrestricted access to all dashboards and accounts.
          </p>
        </div>
      `;
      return;
    }

    renderDashboardAccessToggles(userId, {
      has_dashboard_access: data.has_dashboard_access !== false,
      has_command_center_access: data.has_command_center_access !== false,
      has_tasks_access: data.has_tasks_access !== false,
      has_pipeline_access: data.has_pipeline_access !== false,
    }, false);

    if (dashboardLabel) dashboardLabel.style.display = 'flex';
    if (accountSubLabel) accountSubLabel.style.display = 'block';
    toolbar.style.display = 'flex';
    searchInput.value = '';

    const allAccounts = data.accounts || [];

    const renderAccountList = (filterText = '') => {
      const filtered = allAccounts.filter(a => (a.name || '').toLowerCase().includes(filterText.toLowerCase()));
      body.innerHTML = filtered.map(a => `
        <div class="admin-access-row">
          <label class="admin-access-toggle">
            <input type="checkbox" data-account-id="${a.id}" ${a.granted ? 'checked' : ''}>
            <span>${esc(a.name)}</span>
          </label>
          <span style="font-size:0.7rem; color:var(--text-muted);">${a.granted ? '<i class="fa-solid fa-circle-check" style="color:var(--success);"></i> Granted' : 'Restricted'}</span>
        </div>
      `).join('') || '<div style="color:var(--text-muted); text-align:center; padding:16px;">No matching accounts found.</div>';

      body.querySelectorAll('input[type="checkbox"]').forEach(cb => {
        cb.addEventListener('change', async () => {
          const accountId = cb.dataset.accountId;
          const method = cb.checked ? 'POST' : 'DELETE';
          cb.disabled = true;
          try {
            await fetch(`/api/admin/users/${userId}/accounts/${accountId}`, { method });
            const acc = allAccounts.find(x => x.id === parseInt(accountId, 10));
            if (acc) acc.granted = cb.checked;
            showToast(`Access ${cb.checked ? 'granted' : 'revoked'}`);
          } catch (err) {
            showToast('Failed to update access grant.');
          } finally {
            cb.disabled = false;
          }
        });
      });
    };

    renderAccountList('');

    searchInput.addEventListener('input', (e) => {
      renderAccountList(e.target.value.trim());
    });
  } catch (err) {
    body.innerHTML = `<div class="admin-form-error">Could not load accounts — ${esc(err.message)}</div>`;
  }
}

// ── Topbar Organization Accounts Dropdown ──────────────────────
let topbarAccountsCache = null;

async function loadTopbarAccounts() {
  if (topbarAccountsCache) return topbarAccountsCache;
  try {
    const res = await fetch('/api/accounts');
    if (res.ok) {
      const data = await res.json();
      topbarAccountsCache = data.accounts || [];
    } else {
      topbarAccountsCache = [];
    }
  } catch (err) {
    console.error('Failed to load accounts for topbar dropdown', err);
    topbarAccountsCache = [];
  }
  return topbarAccountsCache;
}

function renderTopbarAccountsList(accounts) {
  const listEl = document.getElementById('dropdownAccountList');
  const countEl = document.getElementById('dropdownAccountCount');
  if (!listEl) return;

  if (countEl) countEl.textContent = accounts.length;

  if (!accounts.length) {
    listEl.innerHTML = '<div class="dropdown-empty">No organisation accounts found.</div>';
    return;
  }

  listEl.innerHTML = accounts.map(a => {
    const ticker = a.ticker || a.stock_symbol || '';
    const score = a.heat_score != null ? `${a.heat_score} SCORE` : '';
    const name = a.name || a.display_name || a.legal_name || 'Account';
    const initStr = getInitials(name, '');
    const industry = (a.industries || [])[0] || a.company_type || 'Enterprise';
    const contacts = a.total_contacts_captured || (a.personas || []).length || 0;

    return `
      <div class="dropdown-account-item" data-account-id="${a.id}" style="cursor:pointer;" onclick="window.location.href='/?account=${a.id}'">
        <div class="dropdown-account-avatar">${esc(initStr)}</div>
        <div class="dropdown-account-info">
          <div class="dropdown-account-name">${esc(name)}</div>
          <div class="dropdown-account-sub">
            ${ticker ? `<span style="font-weight:600; color:var(--brand);">${esc(ticker)}</span> · ` : ''}
            <span>${esc(industry)}</span>
            ${contacts ? `<span>· ${contacts} contacts</span>` : ''}
          </div>
        </div>
        ${score ? `<span class="dropdown-account-badge">${esc(score)}</span>` : ''}
      </div>
    `;
  }).join('');
}

function initTopbarAccountsDropdown() {
  const container = document.getElementById('topbarAccountsDropdown');
  const btn = document.getElementById('globalAccountsBtn');
  const menu = document.getElementById('globalAccountsMenu');
  if (!container || !btn || !menu) return;

  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    // Close pipelines dropdown if open
    const pipeMenu = document.getElementById('pipelinesDropdownMenu');
    const pipeContainer = document.getElementById('topbarPipelinesDropdown');
    if (pipeMenu && pipeContainer) {
      pipeContainer.classList.remove('active');
      pipeMenu.classList.add('d-none');
    }

    const isOpening = menu.classList.contains('d-none');
    if (isOpening) {
      container.classList.add('active');
      menu.classList.remove('d-none');

      const accounts = await loadTopbarAccounts();
      renderTopbarAccountsList(accounts);
    } else {
      container.classList.remove('active');
      menu.classList.add('d-none');
    }
  });

  // Close when clicking outside
  document.addEventListener('click', (e) => {
    if (!container.contains(e.target)) {
      container.classList.remove('active');
      menu.classList.add('d-none');
    }
  });

  // Close when ESC is pressed
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.classList.contains('d-none')) {
      container.classList.remove('active');
      menu.classList.add('d-none');
    }
  });
}

function initTopbarPipelinesDropdown() {
  const container = document.getElementById('topbarPipelinesDropdown');
  const btn = document.getElementById('pipelinesDropdownBtn');
  const menu = document.getElementById('pipelinesDropdownMenu');
  if (!container || !btn || !menu) return;

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    // Close accounts dropdown if open
    const acctMenu = document.getElementById('globalAccountsMenu');
    const acctContainer = document.getElementById('topbarAccountsDropdown');
    if (acctMenu && acctContainer) {
      acctContainer.classList.remove('active');
      acctMenu.classList.add('d-none');
    }

    const isOpening = menu.classList.contains('d-none');
    if (isOpening) {
      container.classList.add('active');
      menu.classList.remove('d-none');
    } else {
      container.classList.remove('active');
      menu.classList.add('d-none');
    }
  });

  // Close when clicking outside
  document.addEventListener('click', (e) => {
    if (!container.contains(e.target)) {
      container.classList.remove('active');
      menu.classList.add('d-none');
    }
  });

  // Close when ESC is pressed
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.classList.contains('d-none')) {
      container.classList.remove('active');
      menu.classList.add('d-none');
    }
  });
}

initTopbarAccountsDropdown();
initTopbarPipelinesDropdown();

async function init() {
  await refreshAccessToken();
  const user = getCurrentUser();
  if (!user) {
    document.body.style.display = 'none';
    window.location.replace('/login?next=/admin');
    return;
  }
  if (user.role !== 'super_admin') {
    main.innerHTML = `
      <div class="admin-page-error">
        <i class="fa-solid fa-shield-halved" style="font-size:2.5rem; color:var(--danger); display:block; margin-bottom:12px;"></i>
        <h2>Access Restricted</h2>
        <p>This console is restricted to Super Administrators only.</p>
        <button type="button" class="admin-btn" id="restrictedLogoutBtn" style="margin-top:16px;"><i class="fa-solid fa-right-from-bracket"></i> Sign Out</button>
      </div>
    `;
    const rBtn = document.getElementById('restrictedLogoutBtn');
    if (rBtn) {
      rBtn.addEventListener('click', async () => {
        await logout();
        window.location.replace('/login');
      });
    }
    return;
  }
  await initTopbarAuth({ showTasks: false });
  try {
    await loadAndRender();
  } catch (err) {
    main.innerHTML = `<div class="admin-page-error">Could not load the admin dashboard — ${esc(err.message)}</div>`;
  }
}

// Ensure pages restored via browser Back/Forward (bfcache) revalidate session
window.addEventListener('pageshow', async (event) => {
  if (event.persisted) {
    const user = await refreshAccessToken();
    if (!user || user.role !== 'super_admin') {
      window.location.replace('/login?next=/admin');
    }
  }
});

init();
