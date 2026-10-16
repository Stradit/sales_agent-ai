// Full-page executive contact profile — modern modular Bento-Grid dashboard
import { state } from './state.js';
import { esc, initials, resolvePersonaTargetKey } from './utils.js';
import {
  hasDossier,
  renderDossierTabs,
  renderFullPersonalityProfile,
  renderFullPsychologicalProfile,
  renderProfileDownloadBtn,
  renderTabbedSignalsWidget
} from './profile-render.js';
import { renderCallPrepButton } from './callprep-generate.js';
import { photoAttr } from './persona-photo.js';

function renderHeroCard(p) {
  const isCSuite = (p.tier === 'c_suite') || (p.hierarchy_level === 1);
  const dossierReady = hasDossier(p);
  const location = [p.city, p.state, p.country].filter(Boolean).join(', ');

  return `
    <div class="profile-hero-card">
      <div class="profile-hero-identity">
        <div class="profile-avatar-xl" ${photoAttr(p)}>${esc(initials(p.name))}</div>
        <div class="profile-hero-info">
          <div class="profile-hero-name">
            ${esc(p.name || 'Executive Contact')}
            ${dossierReady ? '<i class="fa-solid fa-star" title="AI call-prep dossier available"></i>' : ''}
          </div>
          <div class="profile-hero-title">${esc(p.title || 'Executive Title')}</div>
          ${p.company_name ? `<div class="profile-hero-company"><i class="fa-solid fa-building"></i> ${esc(p.company_name)}</div>` : ''}
          <div class="profile-hero-meta-chips">
            ${isCSuite ? '<span class="meta-badge badge-csuite"><i class="fa-solid fa-award"></i> C-Suite</span>' : ''}
            ${p.decision_authority ? `<span class="meta-badge badge-authority"><i class="fa-solid fa-circle-check"></i> ${esc(p.decision_authority)} Authority</span>` : ''}
            ${p.budget_authority ? `<span class="meta-badge"><i class="fa-solid fa-wallet"></i> ${esc(p.budget_authority)} Budget</span>` : ''}
            ${location ? `<span class="meta-badge"><i class="fa-solid fa-location-dot"></i> ${esc(location)}</span>` : ''}
          </div>
        </div>
      </div>
      <div class="profile-hero-actions">
        <a class="profile-action-btn ${p.email ? '' : 'disabled'}" ${p.email ? `href="mailto:${esc(p.email)}"` : ''}><i class="fa-solid fa-envelope"></i> Email</a>
        <a class="profile-action-btn ${p.phone ? '' : 'disabled'}" ${p.phone ? `href="tel:${esc(p.phone)}"` : ''}><i class="fa-solid fa-phone"></i> Call</a>
        <a class="profile-action-btn ${p.linkedin_url ? '' : 'disabled'}" ${p.linkedin_url ? `href="${esc(p.linkedin_url)}" target="_blank" rel="noopener"` : ''}><i class="fa-brands fa-linkedin"></i> LinkedIn</a>
        <button type="button" class="profile-action-btn" data-edit-contact title="Edit this contact's work details"><i class="fa-solid fa-pen"></i> Edit</button>
        <a class="profile-action-btn btn-primary" href="/copilot?persona_id=${encodeURIComponent(p.id)}&account_id=${encodeURIComponent(p.account_id || '')}" title="Ask the Sales Copilot about ${esc(p.name || 'this contact')}"><i class="fa-solid fa-wand-magic-sparkles"></i> Ask Copilot</a>
      </div>
    </div>
  `;
}

// Widget: Sales Call-Prep & Battlecards
function renderCallPrepWidget(persona) {
  return `
    <div class="profile-widget">
      <div class="profile-widget-header">
        <div class="profile-widget-title"><i class="fa-solid fa-comment"></i> Sales Call-Prep &amp; Battlecards</div>
        ${renderCallPrepButton(persona)}
      </div>
      <div id="callPrepStatus" class="dossier-text" style="font-size:.8rem;"></div>
      <div data-callprep-body>${renderDossierTabs(persona)}</div>
    </div>
  `;
}

// Widget: Professional Background & Competencies
function renderBackgroundWidget(persona) {
  const background = [
    persona.prior_company ? `Previously at ${persona.prior_company}` : '',
    (persona.degree || persona.institution) ? `${persona.degree || 'Degree'}${persona.institution ? ', ' + persona.institution : ''}` : ''
  ].filter(Boolean).join(' • ');

  return `
    <div class="profile-widget">
      <div class="profile-widget-header">
        <div class="profile-widget-title"><i class="fa-solid fa-briefcase"></i> Professional Background &amp; Competencies</div>
        <span class="profile-widget-tag">Profile Data</span>
      </div>

      ${background ? `
        <div class="dossier-block">
          <div class="dossier-label"><i class="fa-solid fa-graduation-cap"></i> Academic &amp; Prior Corporate Career</div>
          <div class="dossier-text">${esc(background)}</div>
        </div>
      ` : ''}

      ${(persona.skills && persona.skills.length) ? `
        <div class="dossier-block">
          <div class="dossier-label"><i class="fa-solid fa-bolt"></i> Core Competencies &amp; Focus Areas</div>
          <div class="chip-row">${persona.skills.map(s => `<span class="chip">${esc(s)}</span>`).join('')}</div>
        </div>
      ` : ''}

      ${(!background && !(persona.skills && persona.skills.length)) ? `
        <div class="empty-block" style="padding:20px 4px; text-align:center;">
          <div class="empty-block-text">No additional academic background or skills tags recorded yet.</div>
        </div>
      ` : ''}
    </div>
  `;
}

// Widget: Executive Personality Profile
function renderPersonalityProfileWidget(digestEntry, persona) {
  return `
    <div class="profile-widget">
      <div class="profile-widget-header">
        <div class="profile-widget-title"><i class="fa-solid fa-address-card"></i> Executive Personality Profile</div>
        <span data-profile-download="personality">${renderProfileDownloadBtn('personality', digestEntry)}</span>
      </div>
      <div data-profile-widget="personality">${renderFullPersonalityProfile(digestEntry, persona)}</div>
    </div>
  `;
}

// Widget: Executive Psychological Profile
function renderPsychologicalProfileWidget(digestEntry, persona) {
  return `
    <div class="profile-widget">
      <div class="profile-widget-header">
        <div class="profile-widget-title"><i class="fa-solid fa-heart-pulse"></i> Executive Psychological Profile</div>
        <span data-profile-download="psychological">${renderProfileDownloadBtn('psychological', digestEntry)}</span>
      </div>
      <div data-profile-widget="psychological">${renderFullPsychologicalProfile(digestEntry, persona)}</div>
    </div>
  `;
}

// Widget: Multi-Channel Signals & Public Activity
function renderSignalsWidget(digestEntry, posts) {
  return `
    <div class="profile-widget">
      <div class="profile-widget-header">
        <div class="profile-widget-title"><i class="fa-solid fa-tower-broadcast"></i> Captured Public Signals &amp; Activity</div>
        <span class="profile-widget-tag">${posts.length} Captured Signal${posts.length !== 1 ? 's' : ''}</span>
      </div>
      ${renderTabbedSignalsWidget(digestEntry, posts)}
    </div>
  `;
}

export function renderFullProfile(p) {
  const targetKey = resolvePersonaTargetKey(p);
  const posts = targetKey ? (state.contentStore.posts[targetKey] || []) : [];
  const digestEntry = targetKey ? state.contentStore.digests[targetKey] : null;

  return `
    ${renderHeroCard(p)}

    <!-- Parallel Pair 1: Call-Prep & Battlecards || Background & Competencies -->
    <div class="profile-bento-grid">
      ${renderCallPrepWidget(p)}
      ${renderBackgroundWidget(p)}
    </div>

    <!-- Parallel Pair 2: Executive Personality Profile || Psychological Profile -->
    <div class="profile-bento-grid">
      ${renderPersonalityProfileWidget(digestEntry, p)}
      ${renderPsychologicalProfileWidget(digestEntry, p)}
    </div>

    <!-- Customer interactions logged by the team (CRM activities) -->
    <div class="profile-widget profile-activity-widget">
      <div class="profile-widget-header">
        <div class="profile-widget-title"><i class="fa-solid fa-clock-rotate-left"></i> Customer Activity</div>
        <span class="profile-widget-tag">Meetings, calls, emails &amp; transcripts</span>
      </div>
      <div id="profileActivityTimeline"></div>
    </div>

    <!-- Multi-Channel Signals & Public Activity Feed -->
    ${renderSignalsWidget(digestEntry, posts)}
  `;
}
