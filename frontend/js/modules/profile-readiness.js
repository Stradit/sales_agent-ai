// "What this profile is generated from" checklist shown under the
// Personality/Psychological "Generate now" buttons (the
// [data-profile-readiness] slot in profile-render.js's
// renderPlaceholderProfile). Data comes from
// GET /api/personas/{id}/profile-readiness — read-only, no LLM calls.
import { CHANNEL_LABEL } from './constants.js';
import { esc } from './utils.js';

const OK = '<i class="fa-solid fa-circle-check" style="color:var(--success);"></i>';
const MISSING = '<i class="fa-solid fa-circle-xmark" style="color:var(--danger);"></i>';
const OPTIONAL_MISSING = '<i class="fa-regular fa-circle" style="color:var(--text-muted);"></i>';

function row(icon, label, detail) {
  return `<div style="display:flex; gap:8px; align-items:flex-start; margin-top:6px;">
      <span style="flex:none; width:16px;">${icon}</span>
      <div><div style="font-weight:600;">${label}</div>${detail ? `<div style="color:var(--text-muted);">${detail}</div>` : ''}</div>
    </div>`;
}

export function renderProfileReadiness(r) {
  const channelList = r.channels.length
    ? r.channels.map(c => `${esc(CHANNEL_LABEL[c.channel] || c.channel)} ${c.captured}` +
        (c.used < c.captured ? ` <span title="The newest ${c.used} are used">(uses ${c.used})</span>` : '')).join(' · ')
    : '';

  const tracked = r.registered
    ? row(OK, 'Tracked contact', `Content is being captured for this contact.`)
    : row(MISSING, 'Tracked contact', 'Not tracked yet — add this contact as a person target in the content pipeline so their public activity gets captured.');

  const activity = r.total_captured > 0
    ? row(OK, `Captured public activity — ${r.total_captured} post${r.total_captured !== 1 ? 's' : ''}`, `${channelList}<br>Minimum: 1 post on any channel.`)
    : row(MISSING, 'Captured public activity — 0 posts', 'Needs at least 1 captured post (LinkedIn, news, X, Reddit…). Run a scrape for this contact first.');

  const bgPresent = r.background.filter(b => b.present).length;
  const background = row(
    bgPresent ? OK : OPTIONAL_MISSING,
    `Profile background — ${bgPresent}/${r.background.length} <span style="font-weight:400; color:var(--text-muted);">(optional, improves accuracy)</span>`,
    r.background.map(b => `${b.present ? OK : OPTIONAL_MISSING} ${esc(b.label)}`).join('&nbsp;&nbsp;')
  );

  const cost = r.ready
    ? `<div style="margin-top:8px; color:var(--text-muted);"><i class="fa-solid fa-bolt"></i> Uses about ${r.llm_requests} AI requests (one per channel + two profile syntheses).</div>`
    : '';

  return `<div style="font-size:.78rem; line-height:1.45; border:1px solid var(--border-color); border-radius:0; padding:10px 12px;">
      <div style="font-weight:700; margin-bottom:2px;"><i class="fa-solid fa-list-check"></i> What this profile is generated from</div>
      ${tracked}${activity}${background}${cost}
    </div>`;
}

// Fills every readiness slot in `container` (both profile widgets share one
// fetch). Safe to call again, e.g. after a failed generate.
export async function loadProfileReadiness(container, persona) {
  const slots = container.querySelectorAll('[data-profile-readiness]');
  if (!slots.length || !persona || persona.id == null) return;
  try {
    const res = await fetch(`/api/personas/${persona.id}/profile-readiness`);
    if (!res.ok) return;
    const html = renderProfileReadiness(await res.json());
    container.querySelectorAll('[data-profile-readiness]').forEach(slot => { slot.innerHTML = html; });
  } catch (err) {
    console.error('Profile readiness fetch failed', err);
  }
}
