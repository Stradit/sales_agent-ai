import { showToast } from '../toast.js';
import { getCurrentUser } from '../auth-client.js';
import { resolveRealAccount } from './real-accounts.js';

function priorityFromScore(score) {
  if (score == null) return 'medium';
  if (score >= 80) return 'high';
  if (score >= 50) return 'medium';
  return 'low';
}

/** Creates a real ActionItem (POST /api/accounts/{id}/action-items,
 * self-assigned) so it actually shows up in the signed-in user's My Tasks —
 * not a simulated/local-only toast. Returns true on success; on any failure
 * (account not in the DB, not granted to this user, network error) it shows
 * an explanatory toast and returns false instead of pretending it worked. */
async function createRealActionItem({ accountName, accountId, title, description, priority, source }) {
  const user = getCurrentUser();
  if (!user) {
    showToast('Your session expired — sign in again to create tasks.');
    return false;
  }
  if (user.role === 'viewer') {
    showToast('Your role is read-only — ask a sales rep or manager to create this task.');
    return false;
  }
  const account = accountId ? { id: accountId } : await resolveRealAccount(accountName).catch(() => null);
  if (!account) {
    showToast(`"${accountName}" isn't in the database (or isn't shared with you yet) — task not created.`);
    return false;
  }
  const isAdmin = user.role === 'super_admin' || user.role === 'admin';
  try {
    const res = await fetch(`/api/accounts/${account.id}/action-items`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title,
        description,
        priority,
        assigned_to_id: isAdmin ? null : user.id,
        source: source || 'manual',
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      showToast(body.detail || `Could not create the task for ${accountName} (${res.status}).`);
      return false;
    }
    refreshMyTasksBadge();
    return true;
  } catch (e) {
    showToast('Could not reach the server to create the task.');
    return false;
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
  } catch (e) {
    // best-effort — the badge just stays at its previous count
  }
}

export function logTouch(accountName, context) {
  showToast(`Touch logged for ${accountName}${context ? ` — ${context}` : ''}`);
}

export async function createTask(accountName, title, opts = {}) {
  const ok = await createRealActionItem({
    accountName,
    accountId: opts.accountId || null,
    title,
    description: opts.description || null,
    priority: opts.priority || priorityFromScore(opts.score),
    source: opts.source,
  });
  if (ok) showToast(`Task created for ${accountName} — see My Tasks`);
  return ok;
}

export async function pushToCrm(accountName, title, opts = {}) {
  showToast('Coming soon');
  return false;
}
