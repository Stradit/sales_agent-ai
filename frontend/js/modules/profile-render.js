// Pure, DOM-side-effect-free rendering helpers shared between the sliding
// contact drawer (contact-drawer.js) and the standalone full-page profile
// (full-profile.js).
import { CHANNEL_ICON, CHANNEL_LABEL, STRENGTH_PILL } from './constants.js';
import { esc, isDryRunDigest } from './utils.js';
import { renderChannelCard } from './content-panel.js';

// executive_summary is documented (see PERSONALITY_PROFILE_SYSTEM /
// PSYCHOLOGICAL_PROFILE_SYSTEM in apps/content_pipeline/digest/prompts.py)
// as a plain string, unlike every other section in the same schema — but
// some models return it as a {summary, evidence_strength, basis} object
// like its neighbors anyway. The pipeline now normalizes this at generation
// time (selection.py), but older already-saved digests can still have the
// stale shape — unwrap defensively here so esc() never prints "[object
// Object]" instead of the actual text.
function summaryText(value) {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map(String).join(' ');
  if (value && typeof value === 'object') return value.summary || value.text || '';
  return value || '';
}

export function hasDossier(p) {
  return !!(p.personalized_icebreaker || p.value_proposition || p.communication_style ||
    (p.target_kpis && p.target_kpis.length) || (p.operational_pain_points && p.operational_pain_points.length) ||
    (p.key_objections && p.key_objections.length) || p.prior_company || p.degree || p.institution);
}

export function renderDossier(p) {
  if (!hasDossier(p)) {
    return `<div class="dossier-empty">No AI call-prep dossier generated yet for ${esc(p.name || 'this contact')}.</div>`;
  }
  const chipGroup = (title, icon, items) => (items && items.length)
    ? `<div class="dossier-block"><div class="dossier-label"><i class="${icon}"></i> ${esc(title)}</div><div class="chip-row">${items.map(i => `<span class="chip">${esc(i)}</span>`).join('')}</div></div>`
    : '';
  const background = [p.prior_company ? `Previously at ${p.prior_company}` : '', (p.degree || p.institution) ? `${p.degree || 'Degree'}${p.institution ? ', ' + p.institution : ''}` : '']
    .filter(Boolean).join(' • ');

  return `
    ${p.personalized_icebreaker ? `<div class="dossier-block"><div class="dossier-label"><i class="fa-solid fa-comment-dots"></i> Icebreaker</div><div class="dossier-quote">"${esc(p.personalized_icebreaker)}"</div></div>` : ''}
    ${p.value_proposition ? `<div class="dossier-block"><div class="dossier-label"><i class="fa-solid fa-bullseye"></i> Value Proposition</div><div class="dossier-text">${esc(p.value_proposition)}</div></div>` : ''}
    ${p.communication_style ? `<div class="dossier-block"><div class="dossier-label"><i class="fa-solid fa-comment-dots"></i> Communication Style</div><div class="dossier-text">${esc(p.communication_style)}</div></div>` : ''}
    ${chipGroup('Target KPIs', 'fa-solid fa-flag', p.target_kpis)}
    ${chipGroup('Operational Pain Points', 'fa-solid fa-triangle-exclamation', p.operational_pain_points)}
    ${chipGroup('Likely Objections', 'fa-solid fa-shield-halved', p.key_objections)}
    ${background ? `<div class="dossier-block"><div class="dossier-label"><i class="fa-solid fa-graduation-cap"></i> Background</div><div class="dossier-text">${esc(background)}</div></div>` : ''}
  `;
}

// Call-prep specific (unlike hasDossier, background fields alone don't count —
// otherwise the tabs below would render with every pane empty).
export function hasCallPrep(p) {
  return !!(p.personalized_icebreaker || p.value_proposition ||
    (p.target_kpis && p.target_kpis.length) || (p.operational_pain_points && p.operational_pain_points.length) ||
    (p.key_objections && p.key_objections.length));
}

const CALLPREP_CONFIDENCE = {
  high: 'Personal evidence: career history + recent posts',
  medium: 'Partial personal evidence',
  low: 'Role-based: little personal data, built from role + account context',
};

function renderCallPrepMeta(meta) {
  if (!meta || !meta.generated_at) return '';
  const when = new Date(meta.generated_at);
  const whenText = isNaN(when) ? '' : when.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  const conf = meta.confidence || 'low';
  const group = (meta.method || '').startsWith('llm_group') ? ` · shared with ${meta.group_size || 'similar'} similar contacts` : '';
  return `<div class="dossier-text" style="font-size:.75rem; color:var(--text-muted); margin-bottom:8px;">
      <i class="fa-solid fa-circle-info"></i> ${esc(conf.charAt(0).toUpperCase() + conf.slice(1))} confidence — ${esc(CALLPREP_CONFIDENCE[conf] || '')}${esc(group)}${whenText ? ` · generated ${esc(whenText)}` : ''}
    </div>`;
}

// Interactive tabbed version of the call-prep dossier for the full profile page
export function renderDossierTabs(p) {
  if (!hasCallPrep(p)) {
    return `<div class="dossier-empty">No AI call-prep dossier generated yet for ${esc(p.name || 'this contact')}.</div>`;
  }

  const kpis = p.target_kpis || [];
  const pains = p.operational_pain_points || [];
  const meta = (p.extended_profile && p.extended_profile.callprep) || null;
  // Newer generations keep objection + counter pairs in extended_profile.callprep;
  // older rows only have the plain key_objections strings.
  const objections = (meta && Array.isArray(meta.objections) && meta.objections.length)
    ? meta.objections
    : (p.key_objections || []).map(o => ({ objection: o, counter: '' }));

  return `
    ${renderCallPrepMeta(meta)}
    <div class="profile-tabs-header">
      <button type="button" class="profile-tab-btn active" data-tab-target="tab-pitch"><i class="fa-solid fa-bolt"></i> Pitch &amp; Icebreaker</button>
      <button type="button" class="profile-tab-btn" data-tab-target="tab-pains"><i class="fa-solid fa-crosshairs"></i> Pain Points &amp; KPIs</button>
      <button type="button" class="profile-tab-btn" data-tab-target="tab-objections"><i class="fa-solid fa-shield-halved"></i> Objections (${objections.length})</button>
    </div>

    <div class="profile-tab-pane active" id="tab-pitch">
      ${p.personalized_icebreaker ? `
        <div class="icebreaker-card">
          <div class="icebreaker-label"><i class="fa-solid fa-comment-dots"></i> Personalized Icebreaker</div>
          <div class="icebreaker-quote">"${esc(p.personalized_icebreaker)}"</div>
        </div>
      ` : ''}

      ${p.value_proposition ? `
        <div class="dossier-block">
          <div class="dossier-label"><i class="fa-solid fa-bullseye"></i> Targeted Value Proposition</div>
          <div class="dossier-text" style="font-size:.88rem; line-height:1.5;">${esc(p.value_proposition)}</div>
        </div>
      ` : ''}

      ${p.communication_style ? `
        <div class="dossier-block">
          <div class="dossier-label"><i class="fa-solid fa-comment-dots"></i> Recommended Communication Tone</div>
          <div class="dossier-text">${esc(p.communication_style)}</div>
        </div>
      ` : ''}
    </div>

    <div class="profile-tab-pane" id="tab-pains">
      ${pains.length ? `
        <div class="dossier-block">
          <div class="dossier-label"><i class="fa-solid fa-triangle-exclamation"></i> Operational Pain Points</div>
          <div class="chip-row">${pains.map(pain => `<span class="chip" style="background:rgba(255,77,79,0.08); color:var(--danger); border-color:rgba(255,77,79,0.2);">${esc(pain)}</span>`).join('')}</div>
        </div>
      ` : '<div class="empty-block-text">No operational pain points recorded.</div>'}

      ${kpis.length ? `
        <div class="dossier-block" style="margin-top:10px;">
          <div class="dossier-label"><i class="fa-solid fa-flag"></i> Target KPIs &amp; Mandates</div>
          <div class="chip-row">${kpis.map(k => `<span class="chip" style="background:rgba(0,186,136,0.08); color:var(--success); border-color:rgba(0,186,136,0.2);">${esc(k)}</span>`).join('')}</div>
        </div>
      ` : ''}
    </div>

    <div class="profile-tab-pane" id="tab-objections">
      ${objections.length ? `
        <div style="display:flex; flex-direction:column; gap:10px;">
          ${objections.map(obj => `
            <div class="battlecard-item">
              <div class="battlecard-header"><i class="fa-solid fa-shield-halved"></i> Potential Objection</div>
              <div class="battlecard-body">${esc(obj.objection)}</div>
              ${obj.counter ? `
                <div class="battlecard-header" style="margin-top:6px; color:var(--success);"><i class="fa-solid fa-reply"></i> Suggested Response</div>
                <div class="battlecard-body">${esc(obj.counter)}</div>
              ` : ''}
            </div>
          `).join('')}
        </div>
      ` : '<div class="empty-block-text">No recorded objections for this contact.</div>'}
    </div>
  `;
}

// Actionable Sales Engagement Playbook
export function renderApproachPlaybook(persona) {
  const commStyle = persona.communication_style || 'Strategic, executive-level, ROI & shareholder-value oriented';
  return `
    <div class="playbook-box">
      <div class="playbook-title"><i class="fa-solid fa-compass"></i> Executive Engagement Strategy</div>
      <ul class="playbook-list">
        <li><strong>Structural Clarity:</strong> Lead with concrete ROI, scalable architecture, and measurable business outcomes.</li>
        <li><strong>Executive Tone:</strong> ${esc(commStyle)}. Focus on enterprise impact rather than raw technical jargon.</li>
        <li><strong>Team &amp; Client Alignment:</strong> Highlight how the solution enhances operational efficiency and empowers key stakeholders.</li>
      </ul>
    </div>
  `;
}

export const PERSONALITY_SECTIONS = [
  { key: 'leadership_character', title: 'Leadership Character', icon: 'fa-solid fa-flag' },
  { key: 'decision_making_style', title: 'Decision-Making Style', icon: 'fa-solid fa-signs-post' },
  { key: 'values_and_motivation', title: 'Values and Motivation', icon: 'fa-solid fa-compass' },
  { key: 'public_reputation', title: 'Public Reputation', icon: 'fa-solid fa-bullhorn' }
];

export function renderProfileSubsection(title, icon, section) {
  if (!section || (!section.summary && !(section.basis || []).length)) return '';
  const pill = STRENGTH_PILL[section.evidence_strength] || 'pill-muted';
  const basis = section.basis || [];
  return `
    <div class="personality-section-card">
      <div class="personality-sec-header">
        <div class="personality-sec-title"><i class="${icon}"></i> ${esc(title)}</div>
        ${section.evidence_strength ? `<span class="pill ${pill}">${esc(section.evidence_strength)} evidence</span>` : ''}
      </div>
      ${section.summary ? `<p class="personality-sec-body">${esc(section.summary)}</p>` : ''}
      ${basis.length ? `
        <ul class="personality-basis-list">
          ${basis.map(b => `<li><span>${esc(b.point || '')}${(b.source_url && b.source_url !== 'bio') ? ` — <a href="${esc(b.source_url)}" target="_blank" rel="noopener">Source <i class="fa-solid fa-arrow-up-right-from-square" style="font-size:.7rem;"></i></a>` : ''}</span></li>`).join('')}
        </ul>
      ` : ''}
    </div>
  `;
}

// ── Dedicated Executive Personality Profile Renderer ──
// No fabricated fallback content: if this contact's digest hasn't produced
// a real personality_profile yet, say so plainly (renderPlaceholderProfile)
// instead of showing plausible-looking canned text as if it were synthesized.
const PROFILE_DOWNLOADS = {
  personality: { digestKey: 'personality_profile', btnId: 'drawerDownloadPdfBtn', label: 'Download Personality Report' },
  psychological: { digestKey: 'psychological_profile', btnId: 'downloadPsychologicalPdfBtn', label: 'Download Psychological Report' },
};

// The PDF download button for a profile widget header — empty until that
// profile has actually been generated (there's nothing to put in the report
// before then). profile-generate.js re-renders this after a successful
// "Generate now" so the button appears without a page reload.
export function renderProfileDownloadBtn(kind, digestEntry) {
  const cfg = PROFILE_DOWNLOADS[kind];
  const profile = (digestEntry && digestEntry.digest) ? digestEntry.digest[cfg.digestKey] : null;
  if (!profile) return '';
  return `<button type="button" class="profile-action-btn btn-primary" id="${cfg.btnId}" style="padding: 5px 12px; font-size: .8rem; font-weight: 600;"><i class="fa-solid fa-file-pdf"></i> ${cfg.label}</button>`;
}

export function renderFullPersonalityProfile(digestEntry, persona) {
  const profile = (digestEntry && digestEntry.digest) ? digestEntry.digest.personality_profile : null;
  if (!profile) {
    return renderPlaceholderProfile(
      `Executive Personality Profile hasn't been generated yet for ${esc(persona.name || 'this contact')}.`,
      { generateBtnId: 'generatePersonalityProfileBtn', personaId: persona.id }
    );
  }

  const execProfile = profile.executive_profile || {};
  const renderedSections = PERSONALITY_SECTIONS
    .map(s => renderProfileSubsection(s.title, s.icon, execProfile[s.key]))
    .filter(Boolean)
    .join('');

  return `
    ${profile.executive_summary ? `
      <div class="personality-summary-card">
        <div class="personality-summary-title"><i class="fa-solid fa-id-badge"></i> Executive Summary</div>
        <p class="personality-summary-text">${esc(summaryText(profile.executive_summary))}</p>
      </div>` : ''}

    ${renderedSections ? `<div class="personality-sections-list">${renderedSections}</div>` : ''}

    ${renderApproachPlaybook(persona)}

    ${(profile.caveats && profile.caveats.length) ? `
      <div class="personality-section-card" style="border-left: 3px solid var(--warning);">
        <div class="personality-sec-header">
          <div class="personality-sec-title" style="color:#c07a00;"><i class="fa-solid fa-triangle-exclamation"></i> Observation Caveats</div>
        </div>
        <ul class="personality-basis-list">
          ${profile.caveats.map(c => `<li><span>${esc(c)}</span></li>`).join('')}
        </ul>
      </div>` : ''}
  `;
}

export function renderPlaceholderProfile(reason, opts = {}) {
  const { generateBtnId, personaId } = opts;
  const showGenerate = generateBtnId && personaId != null;
  return `
    <div class="empty-block" style="padding:28px 12px; text-align:center;">
      <div class="empty-block-icon" style="font-size:1.8rem; color:var(--text-muted); margin-bottom:8px;"><i class="fa-solid fa-ban"></i></div>
      <div class="empty-block-text" style="font-size:.84rem; color:var(--text-muted); max-width:400px; margin:0 auto; line-height:1.5;">${esc(reason)}</div>
      ${showGenerate ? `
        <div class="profile-generate-actions" style="margin-top:14px;">
          <button type="button" class="profile-action-btn btn-primary" id="${generateBtnId}" data-generate-profile data-persona-id="${personaId}" style="display:inline-flex; margin:0 auto;">
            <i class="fa-solid fa-wand-magic-sparkles"></i> Generate now
          </button>
          <div class="profile-generate-status" id="${generateBtnId}Status" style="margin-top:10px; font-size:.8rem; color:var(--text-muted); max-width:400px; margin-left:auto; margin-right:auto;"></div>
          <div data-profile-readiness style="margin:14px auto 0; max-width:420px; text-align:left;"></div>
        </div>
      ` : ''}
    </div>
  `;
}

// ── Widget 2: Dedicated Executive Psychological Profile Renderer ──
// Matches PSYCHOLOGICAL_PROFILE_SYSTEM's actual schema exactly (see
// apps/content_pipeline/digest/prompts.py) — every section here really is
// {summary, evidence_strength, basis}, big_five_traits really is just
// {score, summary} per trait, potential_blind_spots really is plain
// strings. No fabricated fallback content: if nothing's been generated
// yet, say so plainly instead of showing a fake report.
export function renderFullPsychologicalProfile(digestEntry, persona, psychData) {
  const profile = psychData || ((digestEntry && digestEntry.digest) ? digestEntry.digest.psychological_profile : null);
  if (!profile) {
    return renderPlaceholderProfile(
      `Executive Psychological Profile hasn't been generated yet for ${esc(persona.name || 'this contact')}.`,
      { generateBtnId: 'generatePsychologicalProfileBtn', personaId: persona.id }
    );
  }

  const synthesis = profile.psychological_synthesis || {};
  const bigFive = profile.big_five_traits || {};
  const blindSpots = profile.potential_blind_spots || [];
  const playbook = profile.engagement_playbook || {};

  const renderBigFiveMeter = (label, traitObj) => {
    if (!traitObj || traitObj.score == null) return '';
    const score = traitObj.score;
    const pct = Math.min(Math.max((score / 10) * 100, 10), 100);
    return `
      <div class="b5-trait-row">
        <div class="b5-trait-header">
          <span class="b5-trait-name">${esc(label)}</span>
          <span class="b5-trait-score">${score.toFixed(1)} <span style="font-size:.7rem; color:var(--text-muted); font-weight:500;">/ 10</span></span>
        </div>
        <div class="b5-meter-track">
          <div class="b5-meter-fill" style="width:${pct}%;"></div>
        </div>
        ${traitObj.summary ? `<div class="b5-trait-desc">${esc(traitObj.summary)}</div>` : ''}
      </div>
    `;
  };
  const bigFiveMeters = [
    renderBigFiveMeter('Openness to Experience', bigFive.openness),
    renderBigFiveMeter('Conscientiousness', bigFive.conscientiousness),
    renderBigFiveMeter('Extraversion', bigFive.extraversion),
    renderBigFiveMeter('Agreeableness', bigFive.agreeableness),
    renderBigFiveMeter('Emotional Stability', bigFive.emotional_stability),
  ].filter(Boolean).join('');

  return `
    ${(synthesis.archetype || synthesis.summary) ? `
      <div class="psych-archetype-hero">
        <div class="psych-hero-kicker"><i class="fa-solid fa-user-large"></i> Executive Psychological Archetype</div>
        ${synthesis.archetype ? `<div class="psych-hero-title">${esc(synthesis.archetype)}</div>` : ''}
        ${synthesis.summary ? `<p class="psych-hero-summary">${esc(synthesis.summary)}</p>` : ''}
      </div>` : ''}

    ${profile.executive_summary ? `
      <div class="personality-summary-card" style="margin-top:16px;">
        <div class="personality-summary-title"><i class="fa-solid fa-book"></i> Executive Summary &amp; Career Trajectory</div>
        <p class="personality-summary-text">${esc(summaryText(profile.executive_summary))}</p>
      </div>` : ''}

    ${bigFiveMeters ? `
      <div class="psych-card" style="margin-top:16px;">
        <div class="psych-card-header">
          <div class="psych-card-title"><i class="fa-solid fa-bars-progress"></i> Big Five Personality Modeling (1.0 — 10.0 Scale)</div>
          <span class="pill pill-muted">Estimated from Public Record</span>
        </div>
        <div class="b5-grid">${bigFiveMeters}</div>
      </div>` : ''}

    <div class="psych-dual-grid" style="margin-top:16px;">
      ${renderProfileSubsection('Cognitive Style', 'fa-solid fa-microchip', profile.cognitive_style)}
      ${renderProfileSubsection('Leadership & Scale Management', 'fa-solid fa-users', profile.leadership_patterns)}
    </div>

    <div style="display:flex; flex-direction:column; gap:12px; margin-top:12px;">
      ${renderProfileSubsection('Core Values & Motivations', 'fa-solid fa-compass', profile.core_values_and_motivations)}
      ${renderProfileSubsection('Interpersonal Traits', 'fa-solid fa-heart', profile.interpersonal_traits)}
    </div>

    ${blindSpots.length ? `
      <div class="psych-card" style="margin-top:16px; border-left:3px solid #F5A623;">
        <div class="psych-card-header">
          <div class="psych-card-title" style="color:#d97706;"><i class="fa-solid fa-shield-halved"></i> Inferred Operational &amp; Cognitive Blind Spots</div>
          <span class="pill pill-warning">Risk Mitigation</span>
        </div>
        <ul class="personality-basis-list" style="margin-top:8px;">
          ${blindSpots.map(bs => `<li><span>${esc(bs)}</span></li>`).join('')}
        </ul>
      </div>` : ''}

    ${(playbook.dos || playbook.donts || playbook.opening_hook) ? `
      <div class="psych-card" style="margin-top:16px;">
        <div class="psych-card-header">
          <div class="psych-card-title"><i class="fa-solid fa-briefcase"></i> Actionable Executive Engagement Playbook</div>
          <span class="pill pill-success">Sales Strategy</span>
        </div>

        ${playbook.opening_hook ? `
          <div class="icebreaker-card" style="margin:10px 0 14px;">
            <div class="icebreaker-label"><i class="fa-solid fa-comment-dots"></i> Recommended Opening Hook</div>
            <div class="icebreaker-quote">"${esc(playbook.opening_hook)}"</div>
          </div>` : ''}
        ${playbook.recommended_tone ? `<div class="psych-sub-block" style="margin-bottom:10px;"><strong>Recommended tone:</strong> ${esc(playbook.recommended_tone)}</div>` : ''}

        <div class="playbook-dos-donts-grid">
          ${(playbook.dos || []).length ? `
            <div style="background:rgba(0,186,136,0.05); border:1px solid rgba(0,186,136,0.2); border-radius:0; padding:12px 14px;">
              <div style="font-weight:700; font-size:.82rem; color:var(--success); margin-bottom:8px; text-transform:uppercase; letter-spacing:0.04em;"><i class="fa-solid fa-circle-check"></i> Recommended (Do This)</div>
              <ul style="padding-left:18px; margin:0; font-size:.82rem; color:var(--text-primary); line-height:1.55;">
                ${playbook.dos.map(d => `<li>${esc(d)}</li>`).join('')}
              </ul>
            </div>` : ''}
          ${(playbook.donts || []).length ? `
            <div style="background:rgba(255,77,79,0.05); border:1px solid rgba(255,77,79,0.2); border-radius:0; padding:12px 14px;">
              <div style="font-weight:700; font-size:.82rem; color:var(--danger); margin-bottom:8px; text-transform:uppercase; letter-spacing:0.04em;"><i class="fa-solid fa-circle-xmark"></i> Avoid (Don't Do This)</div>
              <ul style="padding-left:18px; margin:0; font-size:.82rem; color:var(--text-primary); line-height:1.55;">
                ${playbook.donts.map(d => `<li>${esc(d)}</li>`).join('')}
              </ul>
            </div>` : ''}
        </div>
      </div>` : ''}

    ${(profile.caveats && profile.caveats.length) ? `
      <div class="personality-section-card" style="border-left: 3px solid var(--warning); margin-top:16px;">
        <div class="personality-sec-header">
          <div class="personality-sec-title" style="color:#c07a00;"><i class="fa-solid fa-triangle-exclamation"></i> Observation Caveats</div>
        </div>
        <ul class="personality-basis-list">
          ${profile.caveats.map(c => `<li><span>${esc(c)}</span></li>`).join('')}
        </ul>
      </div>` : ''}
  `;
}

export function renderPostCard(post) {
  const icon = CHANNEL_ICON[post.channel] || 'fa-solid fa-earth-americas';
  const label = CHANNEL_LABEL[post.channel] || post.channel;
  const eng = post.engagement || {};
  const engBits = [
    eng.likes != null ? `<span><i class="fa-solid fa-thumbs-up"></i> ${eng.likes}</span>` : '',
    eng.comments != null ? `<span><i class="fa-solid fa-comment"></i> ${eng.comments}</span>` : '',
    eng.shares != null ? `<span><i class="fa-solid fa-share-nodes"></i> ${eng.shares}</span>` : ''
  ].filter(Boolean).join('');
  const body = (post.body || '').length > 260 ? post.body.slice(0, 260) + '…' : (post.body || '');

  return `
    <div class="post-card" data-post-channel="${esc(post.channel || 'all')}">
      <div class="post-card-header">
        <span class="post-card-channel"><i class="${icon}"></i> ${esc(label)}</span>
        ${post.published_at ? `<span class="post-card-date">${esc(post.published_at)}</span>` : ''}
      </div>
      ${post.author ? `<div class="post-card-author">${esc(post.author)}</div>` : ''}
      ${body ? `<p class="post-card-body">${esc(body)}</p>` : ''}
      <div class="post-card-footer">
        ${engBits ? `<span class="post-card-engagement">${engBits}</span>` : '<span></span>'}
        ${post.post_url ? `<a href="${esc(post.post_url)}" target="_blank" rel="noopener">Source <i class="fa-solid fa-arrow-up-right-from-square"></i></a>` : ''}
      </div>
    </div>
  `;
}

export function renderPersonaContentSummary(digestEntry, posts) {
  if (!posts.length && !digestEntry) return '';

  if (digestEntry && !isDryRunDigest(digestEntry)) {
    const channels = (digestEntry.digest && digestEntry.digest.channels) || [];
    if (channels.length) {
      return `<div class="content-channel-grid" style="margin-bottom:12px;">${channels.map(renderChannelCard).join('')}</div>`;
    }
  }

  if (!posts.length) return '';

  const byChannel = {};
  posts.forEach(post => { (byChannel[post.channel] = byChannel[post.channel] || []).push(post); });
  const dates = posts.map(post => post.published_at).filter(Boolean);

  return `
    <div class="chip-row" style="margin-bottom:12px;">
      ${Object.entries(byChannel).map(([ch, arr]) => `<span class="chip"><i class="${CHANNEL_ICON[ch] || 'fa-solid fa-earth-americas'}"></i> ${esc(CHANNEL_LABEL[ch] || ch)}: ${arr.length}</span>`).join('')}
      ${dates.length ? `<span class="chip"><i class="fa-solid fa-calendar-days"></i> Latest: ${esc(dates[0])}</span>` : ''}
    </div>
  `;
}

// Filterable Multi-Channel Post Feed
export function renderTabbedSignalsWidget(digestEntry, posts) {
  if (!posts.length) {
    return `
      <div class="empty-block" style="padding:24px 8px;">
        <div class="empty-block-icon"><i class="fa-solid fa-inbox"></i></div>
        <div class="empty-block-text">No captured public signals or posts available for this contact.</div>
      </div>
    `;
  }

  const byChannel = {};
  posts.forEach(post => {
    const ch = post.channel || 'other';
    (byChannel[ch] = byChannel[ch] || []).push(post);
  });

  return `
    <div class="channel-filters-bar">
      <button type="button" class="channel-filter-pill active" data-filter="all">
        <i class="fa-solid fa-layer-group"></i> All Signals <span class="pill-count">${posts.length}</span>
      </button>
      ${Object.entries(byChannel).map(([ch, arr]) => `
        <button type="button" class="channel-filter-pill" data-filter="${esc(ch)}">
          <i class="${CHANNEL_ICON[ch] || 'fa-solid fa-earth-americas'}"></i> ${esc(CHANNEL_LABEL[ch] || ch)} <span class="pill-count">${arr.length}</span>
        </button>
      `).join('')}
    </div>

    <div class="profile-feed-grid" id="profilePostFeed">
      ${posts.slice(0, 12).map(renderPostCard).join('')}
    </div>

    <div class="profile-pagination-bar" id="profileFeedPagination"></div>
  `;
}

export function renderAllPostsByChannel(posts) {
  if (!posts.length) return '';
  const byChannel = {};
  posts.forEach(post => { (byChannel[post.channel] = byChannel[post.channel] || []).push(post); });

  return Object.entries(byChannel).map(([ch, arr]) => `
    <div class="profile-post-group">
      <div class="dossier-label"><i class="${CHANNEL_ICON[ch] || 'fa-solid fa-earth-americas'}"></i> ${esc(CHANNEL_LABEL[ch] || ch)} <span class="pill pill-muted" style="margin-left:6px;">${arr.length}</span></div>
      <div class="post-card-list">${arr.map(renderPostCard).join('')}</div>
    </div>
  `).join('');
}

export function renderPersonalityProfile(digestEntry) {
  if (!digestEntry || isDryRunDigest(digestEntry)) {
    return renderPlaceholderProfile('Not available yet — this contact\'s AI digest hasn\'t run with a real model. Regenerate the digest to build a Personality Profile.');
  }
  const profile = (digestEntry.digest || {}).personality_profile;
  if (!profile) {
    return renderPlaceholderProfile('Not available — this digest predates the Personality Profile feature. Re-run the digest to generate one.');
  }

  const sub = PERSONALITY_SECTIONS
    .map(s => renderProfileSubsection(s.title, s.icon, (profile.executive_profile || {})[s.key]))
    .filter(Boolean)
    .join('');

  return `
    <div class="content-provenance" style="margin-bottom:12px;">
      <i class="fa-solid fa-circle-info"></i> AI-synthesized from public posts, filings, and career history — hedged and cited.
    </div>
    ${profile.executive_summary ? `
      <div class="personality-summary-card" style="margin-bottom:12px;">
        <div class="personality-summary-title"><i class="fa-solid fa-id-badge"></i> Executive Summary</div>
        <p class="personality-summary-text">${esc(summaryText(profile.executive_summary))}</p>
      </div>` : ''}
    ${sub ? `<div class="personality-sections-list">${sub}</div>` : ''}
    ${(profile.caveats && profile.caveats.length) ? `
      <div class="personality-section-card" style="border-left: 3px solid var(--warning); margin-top:12px;">
        <div class="personality-sec-header">
          <div class="personality-sec-title" style="color:#c07a00;"><i class="fa-solid fa-triangle-exclamation"></i> Observation Caveats</div>
        </div>
        <ul class="personality-basis-list">
          ${profile.caveats.map(c => `<li><span>${esc(c)}</span></li>`).join('')}
        </ul>
      </div>` : ''}
  `;
}
