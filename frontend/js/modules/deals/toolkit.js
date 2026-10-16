// Stage toolkit pane for the deal room (README §21.1, D3). Pure rendering — data comes
// from GET /api/deals/{id}/toolkit?stage=… and is computed without any AI requests.
// Anything that needs writing links to the Copilot with a prefilled question.
import { esc } from '../utils.js';

const ROLE_LABEL = { champion: 'Champion', economic_buyer: 'Economic buyer', technical_evaluator: 'Technical evaluator',
  influencer: 'Influencer', blocker: 'Blocker', user: 'User' };
const KIND_ICON = { signal: 'fa-bolt', leadership: 'fa-user-tie', hiring: 'fa-briefcase' };
const DOC_LABEL = { signal: 'Signal', news: 'News', blog: 'Blog', linkedin_post: 'LinkedIn', job_theme: 'Hiring', digest_channel: 'Digest' };

function day(d) {
  if (!d) return '';
  const dt = /^\d{4}-\d{2}-\d{2}$/.test(d) ? new Date(`${d}T00:00:00`) : new Date(d);
  return Number.isNaN(dt.getTime()) ? '' : dt.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

export function copilotHref(deal, q) {
  const p = new URLSearchParams({ account_id: deal.account_id, deal_id: deal.id, q });
  return `/copilot?${p}`;
}

function ask(deal, q, label = 'Draft with Copilot') {
  return `<a class="tk-ask" href="${esc(copilotHref(deal, q))}" title="${esc(q)}"><i class="fa-solid fa-wand-magic-sparkles"></i> ${esc(label)}</a>`;
}

function block(title, icon, body, extra = '') {
  return `<section class="tk-block"><h4 class="tk-h"><i class="fa-solid ${icon}"></i> ${esc(title)}${extra}</h4>${body}</section>`;
}

function empty(text) { return `<p class="tk-empty">${esc(text)}</p>`; }

function intro(d, deal) {
  const why = d.why_now.map(w => `<li class="tk-why">
      <span class="tk-why-ic ${w.kind}"><i class="fa-solid ${KIND_ICON[w.kind] || 'fa-bolt'}"></i></span>
      <div><div class="tk-why-title">${w.url ? `<a href="${esc(w.url)}" target="_blank" rel="noopener">${esc(w.title)}</a>` : esc(w.title)}</div>
        ${w.detail ? `<div class="tk-why-detail">${esc(w.detail)}</div>` : ''}
        <div class="tk-meta"><span class="tk-tag">${esc(w.label)}</span>${w.date ? ` ${esc(day(w.date))}` : ''}</div></div></li>`).join('');
  const who = d.who_to_approach.map(p => `<li class="tk-person">
      <div><a href="/profile?account=${deal.account_id}&persona_id=${p.id}" target="_blank" rel="noopener">${esc(p.name)}</a>
        <div class="tk-meta">${esc(p.title || '')}</div>
        ${p.why.length ? `<div class="tk-chips">${p.why.map(w => `<span class="tk-tag">${esc(w)}</span>`).join('')}</div>` : ''}</div>
      <div class="tk-person-ctl">
        <button type="button" class="dl-btn dl-btn-sm" data-tk-add="${p.id}" title="Add to the buying committee as an influencer — change the role later"><i class="fa-solid fa-user-plus"></i> Add</button>
        ${ask(deal, `Draft a short intro email to ${p.name}`, 'Intro email')}
      </div></li>`).join('');
  return block('Why now', 'fa-bolt', why ? `<ul class="tk-list">${why}</ul>` : empty('No recent signals, leadership moves or hiring themes for this account yet.'),
    d.why_now.length ? ' <button type="button" class="tk-link" data-tk-check="intro|trigger_noted">Mark “trigger noted”</button>' : '')
    + block('Who to approach', 'fa-bullseye', who ? `<ul class="tk-list">${who}</ul>` : empty('Everyone suggested is already on the committee.'));
}

function discovery(d, deal) {
  const banks = d.question_bank.map(b => `<details class="tk-bank" open><summary>${esc(b.name)} <span class="tk-tag">${esc(ROLE_LABEL[b.role] || b.role)}</span></summary>
      <ol>${b.questions.map(q => `<li>${esc(q)}</li>`).join('')}</ol></details>`).join('');
  const meddicc = d.meddicc.map(m => `<div class="tk-med${m.value ? ' filled' : ''}">
      <label for="tkMed_${m.key}"><strong>${esc(m.label)}</strong><span>${esc(m.hint)}</span></label>
      <textarea id="tkMed_${m.key}" data-med="${m.key}" rows="2" maxlength="1000" placeholder="${esc(m.suggested ? `Suggested: ${m.suggested}` : 'Not known yet')}">${esc(m.value)}</textarea>
      ${m.suggested && !m.value ? `<button type="button" class="tk-link" data-med-use="${m.key}" data-med-text="${esc(m.suggested)}">Use suggestion</button>` : ''}
    </div>`).join('');
  return block('MEDDICC', 'fa-clipboard-check', `<div class="tk-med-grid">${meddicc}</div>`, ` <span class="tk-score">${esc(d.meddicc_score)}</span>`)
    + block('Question bank', 'fa-circle-question', (banks || empty('Add people to the buying committee to get questions built from their KPIs, pains and objections.'))
      + `<details class="tk-bank"><summary>Questions for anyone</summary><ol>${d.generic_questions.map(q => `<li>${esc(q)}</li>`).join('')}</ol></details>`);
}

function proposal(d, deal) {
  const vm = d.value_map.map(v => `<div class="tk-value">
      <div class="tk-value-head"><strong>${esc(v.label)}</strong><span class="tk-meta">${v.evidence_count} matching item${v.evidence_count === 1 ? '' : 's'}</span></div>
      ${v.evidence.length ? `<ul>${v.evidence.map(e => `<li><span class="tk-tag">${esc(DOC_LABEL[e.doc_type] || e.doc_type)}</span>
        ${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title)}</a>` : esc(e.title)}</li>`).join('')}</ul>`
        : empty('No evidence at this account yet — validate the fit in discovery.')}</div>`).join('');
  const comp = d.battlecard.competitors.map(c => `<li><strong>${esc(c.name)}</strong><span class="tk-meta">named in ${c.lobs} LOB${c.lobs === 1 ? '' : 's'} · ${c.mentions} mention${c.mentions === 1 ? '' : 's'} in our data</span></li>`).join('');
  const pack = d.objection_pack.map(o => `<li class="tk-obj"><div class="tk-obj-q">“${esc(o.objection)}”<span class="tk-meta"> — ${esc(o.who)}</span></div>
      ${o.response ? `<div class="tk-obj-a"><i class="fa-solid fa-reply"></i> ${esc(o.response)}</div>` : ''}</li>`).join('');
  const bc = d.business_case;
  return block('Value map', 'fa-link', vm || empty('Pick StradIT offerings on the deal to map them to evidence.'),
    d.value_map.length ? ' <button type="button" class="tk-link" data-tk-check="proposal|offerings_mapped">Mark mapped</button>' : '')
    + block('Business case inputs', 'fa-scale-balanced', (bc.pains.length || bc.kpis.length) ? `<div class="tk-two">
        <div><strong>Pains</strong><ul>${bc.pains.map(x => `<li>${esc(x)}</li>`).join('') || '<li>—</li>'}</ul></div>
        <div><strong>KPIs they own</strong><ul>${bc.kpis.map(x => `<li>${esc(x)}</li>`).join('') || '<li>—</li>'}</ul></div></div>`
      : empty('Add committee members with call-prep to fill this in.'))
    + block('Battlecard', 'fa-chess-knight', `${comp ? `<ul class="tk-list tk-comp">${comp}</ul>` : empty('No competitors recorded for this account’s LOBs.')}
        <ul class="tk-positioning">${d.battlecard.positioning.map(p => `<li>${esc(p)}</li>`).join('')}</ul>`,
      comp ? ' <button type="button" class="tk-link" data-tk-check="proposal|competition">Mark identified</button>' : '')
    + block('Objection handling', 'fa-shield-halved', pack ? `<ul class="tk-list">${pack}</ul>` : empty('No objections recorded for the committee yet.'))
    + `<div class="tk-cta">${d.prompts.map(q => ask(deal, q, 'Draft proposal outline')).join('')}</div>`;
}

function pilot(d) {
  return block('Success criteria', 'fa-flag-checkered', `<ul class="tk-checks">${d.success_criteria.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`)
    + block('Suggested 6-week plan', 'fa-calendar-days', `<ol class="tk-plan">${d.plan.map(p => `<li><span class="tk-plan-when">${esc(p.week)}<small>${esc(day(p.date))}</small></span><span>${esc(p.milestone)}</span></li>`).join('')}</ol>`)
    + block('Risk alerts', 'fa-triangle-exclamation', `<ul class="tk-risks">${d.risks.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`);
}

function contract(d) {
  const chain = d.approval_chain.map((p, i) => `<li><span class="tk-step">${i + 1}</span><div><strong>${esc(p.name)}</strong>
      <span class="tk-tag">${esc(ROLE_LABEL[p.role] || p.role)}</span><div class="tk-meta">${esc(p.title || '')}${p.budget ? ` · budget: ${esc(p.budget)}` : ''}</div></div></li>`).join('');
  const lobs = d.expansion.other_lobs.map(l => `<li>${esc(l.lob_name)} <span class="tk-meta">${l.contacts} contact${l.contacts === 1 ? '' : 's'}</span></li>`).join('');
  return block('Approval chain', 'fa-stamp', chain ? `<ol class="tk-chain">${chain}</ol>` : empty('Add the buying committee to see the approval order.'))
    + block('Procurement prep', 'fa-file-signature', `<ul class="tk-checks">${d.procurement_prep.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`)
    + block('Expansion map', 'fa-arrows-split-up-and-left', `<div class="tk-two">
        <div><strong>Other offerings</strong><div class="tk-chips">${d.expansion.other_offerings.map(o => `<span class="tk-tag">${esc(o.label)}</span>`).join('') || '—'}</div></div>
        <div><strong>Other lines of business</strong><ul>${lobs || '<li>—</li>'}</ul></div></div>`);
}

const RENDER = { intro, discovery, proposal, pilot, contract };

export function renderToolkit(tk, deal, stages) {
  const nav = `<div class="tk-stages" role="group" aria-label="Toolkit stage">${stages.map(s =>
    `<button type="button" class="tk-stage${s.key === (tk && tk.stage) ? ' active' : ''}${s.key === deal.stage ? ' current' : ''}" data-tk-stage="${s.key}" aria-pressed="${s.key === (tk && tk.stage)}">${esc(s.label)}</button>`).join('')}</div>`;
  if (!tk) return `${nav}<div class="tk-loading" aria-busy="true"><span></span><span></span><span></span></div>`;
  if (tk.error) return `${nav}${empty(tk.error)}`;
  return `${nav}<p class="tk-note"><i class="fa-solid fa-database"></i> Built from your account data — no AI requests used.</p>${RENDER[tk.stage](tk.data, deal)}`;
}
