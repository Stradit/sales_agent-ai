// Command Center · deal context — one fetch of GET /api/command-center/my-day shared by My Day and the cards below
// it, so the matrix, coverage, signal feed and exec moves can say which open deals an account has.
// my-day.js loads it; cards read it with dealsFor() and redraw on the 'cc:deal-context' event.
let pending = null;
let data = null;
const byId = new Map();
const byName = new Map();

const norm = (s) => (s || '').toLowerCase().replace(/\b(the|corporation|corp|inc|llc|ltd|company|co)\b/g, '').replace(/[^a-z0-9]+/g, '');

export function loadMyDay(force = false) {
  if (force || !pending) {
    pending = fetch('/api/command-center/my-day').then(async (res) => {
      if (!res.ok) throw new Error(`My Day failed to load (${res.status})`);
      data = await res.json();
      byId.clear(); byName.clear();
      (data.accounts || []).forEach(a => { byId.set(String(a.account_id), a); byName.set(norm(a.name), a); });
      document.dispatchEvent(new CustomEvent('cc:deal-context'));
      return data;
    });
    pending.catch(() => { pending = null; });
  }
  return pending;
}

/** The account's open-pipeline entry ({open_count, open_value, eb_gaps, deals: [...]}) or null until loaded. */
export function dealsFor(accountId, accountName) {
  if (!data) return null;
  return byId.get(String(accountId)) || (accountName ? byName.get(norm(accountName)) : null) || null;
}

export function hasDealContext() { return Boolean(data); }
export function dealContext() { return data; }
