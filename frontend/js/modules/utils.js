import { state } from './state.js';

// Escapes for HTML text AND attribute values (quotes too: many callers build href="${esc(x)}").
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

export function initials(name) {
  return (name || '?').split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
}

export function slugify(s) {
  return (s || '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

export function isDryRunDigest(entry) {
  if (!entry) return true;
  if (/dry.?run/i.test(entry.llm || '')) return true;
  if (entry.digest && entry.digest.email && entry.digest.email.dry_run === true) return true;
  return false;
}

export function resolveTargetKey(candidates) {
  for (const c of candidates) {
    if (c && (state.contentStore.digests[c] || state.contentStore.posts[c])) return c;
  }
  return null;
}

export function resolveAccountTargetKey(account) {
  return resolveTargetKey([
    account.key,
    (account.ticker || '').toLowerCase(),
    slugify(account.name),
    slugify(account.legal_name)
  ]);
}

// Drops single-letter tokens (middle initials like "S.") before slugifying —
// the content pipeline's own people_targets.py registers "Ranjit S. Samra"
// under the key "ranjit_samra" (initial omitted), so plain slugify() alone
// ("ranjit_s_samra") never matches and his real, already-scraped posts
// silently render as "no recent posts" despite existing in Postgres.
function slugifyDroppingInitials(name) {
  return (name || '')
    .trim()
    .split(/\s+/)
    .filter(w => w.replace(/[^a-zA-Z0-9]/g, '').length > 1)
    .join(' ');
}

export function resolvePersonaTargetKey(p) {
  return resolveTargetKey([p.key, slugify(p.name), slugify(slugifyDroppingInitials(p.name))]);
}

export function findPersonaLob(account, persona) {
  return (account.lobs || []).find(l => (l.personas || []).some(p => p.name === persona.name)) || null;
}

export function tierLabel(p) {
  const t = (p.tier || '').toLowerCase().replace(/[_\s-]+/g, ' ').trim();
  if (t.includes('c suite') || t === 'csuite') return 'C-Suite';
  if (t.includes('vp') || t.includes('vice president')) return 'VP';
  if (t.includes('director')) return 'Director';
  if (t.includes('manager')) return 'Manager';
  return t ? t.replace(/\b\w/g, c => c.toUpperCase()) : 'Other';
}

export function getPersonasFor(account, lob) {
  if (lob) return lob.personas || [];
  const list = account.personas && account.personas.length
    ? account.personas
    : (account.lobs || []).flatMap(l => l.personas || []);
  return dedupePersonas(list);
}

export function getTechFor(account, lob) {
  let base;
  if (lob) base = lob.technologies || [];
  else {
    const set = new Set();
    (account.lobs || []).forEach(l => (l.technologies || []).forEach(t => set.add(t)));
    base = [...set];
  }
  const extra = state.extraTech[account.id] || [];
  return [...new Set([...base, ...extra])];
}

export function dedupePersonas(list) {
  const seen = new Set();
  const out = [];
  list.forEach(p => {
    const k = p.id != null ? p.id : (p.key || p.name);
    if (seen.has(k)) return;
    seen.add(k);
    out.push(p);
  });
  return out;
}

export function parseSnippetTable(text) {
  if (!text || text.indexOf('|') === -1) return null;
  const cells = text.split('|').map(c => c.trim()).filter(Boolean);
  for (const cols of [5, 4, 6, 3]) {
    if (cells.length >= cols * 2 && cells.length % cols === 0) {
      const rows = [];
      for (let i = 0; i < cells.length; i += cols) rows.push(cells.slice(i, i + cols));
      return rows;
    }
  }
  return null;
}

export function formatWeekOf(dateStr) {
  if (!dateStr) return '';
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return '';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export function priorityPill(priority) {
  if (!priority) return '';
  const cls = /high/i.test(priority) ? 'pill-danger' : /medium/i.test(priority) ? 'pill-warning' : 'pill-success';
  return `<span class="pill ${cls}">${esc(priority)} priority</span>`;
}

export function formatJobSalary(salary) {
  if (!salary) return null;
  if (salary.text) return salary.text;
  const fmt = n => `$${Number(n).toLocaleString()}`;
  if (salary.min != null && salary.max != null) return `${fmt(salary.min)}–${fmt(salary.max)}`;
  if (salary.min != null) return fmt(salary.min);
  if (salary.max != null) return fmt(salary.max);
  return null;
}

export function timeAgo(isoDate) {
  if (!isoDate) return null;
  const days = Math.floor((Date.now() - new Date(isoDate).getTime()) / 86400000);
  if (days <= 0) return 'today';
  if (days === 1) return '1 day ago';
  if (days < 14) return `${days} days ago`;
  const weeks = Math.floor(days / 7);
  return `${weeks} week${weeks !== 1 ? 's' : ''} ago`;
}
