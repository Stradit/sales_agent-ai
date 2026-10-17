// Buyer room (#11): the read-only page a StradIT rep shares with the customer. No sign-in — the
// token in the URL is the access. Data: GET /api/room/{token} (apps/sales_deals/buyer_room.py).
import { esc } from '../utils.js';

const main = document.getElementById('brMain');
const token = decodeURIComponent(location.pathname.split('/').filter(Boolean)[1] || '');
const day = (d) => (d ? new Date(`${String(d).slice(0, 10)}T00:00:00`).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '');
const today = new Date().toISOString().slice(0, 10);

function stepHtml(p) {
  const late = p.status !== 'done' && p.due_date && p.due_date < today;
  return `<li class="br-step s-${esc(p.status)}${late ? ' late' : ''}">
    <span class="br-check" aria-hidden="true">${p.status === 'done' ? '<i class="fa-solid fa-check"></i>' : ''}</span>
    <div><div class="br-step-title">${esc(p.title)}${p.status === 'done' ? ' <span class="sr-only">(done)</span>' : ''}</div>
      <div class="br-step-meta"><span class="br-side side-${esc(p.side)}">${esc(p.side_label)}</span>
        ${p.owner_name ? `<span>${esc(p.owner_name)}</span>` : ''}
        ${p.due_date ? `<span class="br-when">${p.status === 'done' ? 'Done' : `By ${esc(day(p.due_date))}`}</span>` : ''}
        ${p.status === 'blocked' ? '<span class="br-blocked">On hold</span>' : ''}</div></div>
  </li>`;
}

function render(r) {
  document.title = `${r.title} | StradIT`;
  const done = r.plan.filter(p => p.status === 'done').length;
  const pct = r.plan.length ? Math.round((100 * done) / r.plan.length) : 0;
  const next = r.plan.find(p => p.status !== 'done');
  const plan = r.plan.length
    ? `<div class="br-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${r.plan.length}" aria-valuenow="${done}" aria-label="Steps done"><span style="width:${pct}%"></span></div>
       <p class="br-muted">${done} of ${r.plan.length} steps done${next ? ` · next: <strong>${esc(next.title)}</strong>${next.due_date ? ` by ${esc(day(next.due_date))}` : ''}` : ''}</p>
       <ol class="br-steps">${r.plan.map(stepHtml).join('')}</ol>`
    : '<p class="br-muted">Your StradIT contact is putting the plan together — check back soon.</p>';
  const links = r.resources.length
    ? `<section class="br-card" aria-labelledby="brResTitle"><h2 id="brResTitle">Resources</h2>
        <ul class="br-links">${r.resources.map(x => `<li><a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-arrow-up-right-from-square"></i> ${esc(x.title)}</a></li>`).join('')}</ul></section>`
    : '';
  main.innerHTML = `
    <section class="br-hero">
      <h1>${esc(r.title)}</h1>
      ${r.welcome_note ? `<p class="br-note">${esc(r.welcome_note).replace(/\n/g, '<br>')}</p>` : ''}
      ${r.closed ? '<p class="br-muted"><i class="fa-solid fa-circle-info"></i> This plan is complete — thank you.</p>' : ''}
    </section>
    <div class="br-grid">
      <section class="br-card br-plan" aria-labelledby="brPlanTitle"><h2 id="brPlanTitle">Our plan together</h2>${plan}</section>
      <aside class="br-side-col">
        <section class="br-card" aria-labelledby="brContactTitle">
          <h2 id="brContactTitle">Your StradIT contact</h2>
          <p class="br-contact"><strong>${esc(r.contact.name || 'StradIT')}</strong>
            ${r.contact.email ? `<a href="mailto:${esc(r.contact.email)}"><i class="fa-regular fa-envelope"></i> ${esc(r.contact.email)}</a>` : ''}</p>
        </section>
        ${links}
      </aside>
    </div>`;
}

async function load() {
  try {
    const res = await fetch(`/api/room/${encodeURIComponent(token)}`, { credentials: 'omit', referrerPolicy: 'no-referrer' });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || 'This page is not available.');
    render(data);
  } catch (err) {
    main.innerHTML = `<section class="br-card br-gone"><i class="fa-solid fa-link-slash"></i><h1>Link not available</h1><p>${esc(err.message)}</p></section>`;
  }
}

load();
