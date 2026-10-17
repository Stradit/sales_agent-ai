// Escapes for HTML text AND attribute values (quotes too: many callers build href="${esc(x)}").
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function formatMoney(n) {
  if (n == null) return '—';
  if (n >= 1000000) return `$${(n / 1000000).toFixed(1).replace(/\.0$/, '')}M`;
  if (n >= 1000) return `$${Math.round(n / 1000)}K`;
  return `$${n}`;
}

export function relativeTime(date) {
  const ms = Date.now() - date.getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 60) return `${Math.max(mins, 1)}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function ageInDays(date) {
  return (Date.now() - date.getTime()) / 86400000;
}

/** fresh (<3d) | aging (3-7d) | stale (>7d) */
export function signalStatus(date) {
  const d = ageInDays(date);
  if (d < 3) return 'fresh';
  if (d <= 7) return 'aging';
  return 'stale';
}

export function clamp(n, min, max) {
  return Math.min(max, Math.max(min, n));
}
