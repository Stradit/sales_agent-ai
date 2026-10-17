// Next best action (#12): one suggestion at the top of the deal room, with the one-click fix and an
// optional AI-drafted email. Rules: apps/sales_deals/next_action.py; draft: POST /{id}/next-action/draft.
let c = null;              // { api, esc, showToast, getDeal, runGapAction, openTab, rerender }
const nb = { dealId: null, code: null, draft: null, busy: false };

export function initNba(ctx) { c = ctx; }

export function nbaCard(d) {
  const a = d.next_action;
  if (!a) return '';
  if (nb.dealId !== d.id || nb.code !== a.code) { nb.dealId = d.id; nb.code = a.code; nb.draft = null; }
  const ro = document.body.classList.contains('role-viewer');
  const fixBtn = !ro && (a.fix || a.tab)
    ? `<button type="button" class="dl-btn dl-btn-primary" data-nba-fix><i class="fa-solid fa-arrow-right"></i> Do it</button>` : '';
  const draftBtn = !ro && a.can_draft
    ? `<button type="button" class="dl-btn" data-nba-draft${nb.busy ? ' disabled' : ''}><i class="fa-solid fa-wand-magic-sparkles"></i> ${nb.busy ? 'Drafting…' : nb.draft ? 'Redraft' : 'Draft the email'}</button>` : '';
  return `<section class="dl-nba" aria-labelledby="dlNbaTitle">
    <div class="dl-nba-label"><i class="fa-solid fa-bullseye"></i> Next best action</div>
    <h3 id="dlNbaTitle">${c.esc(a.title)}</h3>
    <p class="dl-nba-why">${c.esc(a.why)}</p>
    ${fixBtn || draftBtn ? `<div class="dl-nba-actions">${fixBtn}${draftBtn}
      ${a.can_draft && !nb.draft ? '<span class="dl-muted dl-nba-note">Uses one AI request from the team\'s daily allowance.</span>' : ''}</div>` : ''}
    ${nb.draft ? draftHtml(nb.draft) : ''}
  </section>`;
}

function draftHtml(x) {
  return `<div class="dl-nba-draft">
    <label>Subject<input id="dlNbaSubject" maxlength="200" value="${c.esc(x.subject)}"></label>
    <label>Email${x.to ? ` <span class="dl-muted">to ${c.esc(x.to)}</span>` : ''}<textarea id="dlNbaBody" rows="9">${c.esc(x.body)}</textarea></label>
    <div class="dl-nba-actions">
      <button type="button" class="dl-btn" data-nba-copy><i class="fa-regular fa-copy"></i> Copy</button>
      <a class="dl-btn" href="#" data-nba-mail><i class="fa-regular fa-envelope"></i> Open in email</a>
      <span class="dl-muted">AI draft — check the facts before you send it.</span>
    </div>
  </div>`;
}

export async function nbaClick(e) {
  const t = e.target;
  const d = c.getDeal();
  if (!d || !d.next_action) return false;
  if (t.closest('[data-nba-fix]')) {
    const a = d.next_action;
    if (a.fix) c.runGapAction(a.fix); else if (a.tab) c.openTab(a.tab);
    return true;
  }
  if (t.closest('[data-nba-draft]')) {
    if (nb.busy) return true;
    nb.busy = true; c.rerender();
    try {
      nb.draft = await c.api(`/${d.id}/next-action/draft`, { method: 'POST' });
      nb.code = d.next_action.code;
    } catch (err) { c.showToast(err.message); }
    nb.busy = false; c.rerender();
    const body = document.getElementById('dlNbaBody'); if (body) body.focus();
    return true;
  }
  if (t.closest('[data-nba-copy]')) {
    const txt = `${document.getElementById('dlNbaSubject').value}\n\n${document.getElementById('dlNbaBody').value}`;
    try { await navigator.clipboard.writeText(txt); c.showToast('Draft copied'); } catch { c.showToast('Copy failed — select the text and press Ctrl+C'); }
    return true;
  }
  const mail = t.closest('[data-nba-mail]');
  if (mail) {   // built from what's in the boxes now, edits included
    mail.href = `mailto:${encodeURIComponent((nb.draft && nb.draft.to) || '')}?subject=${encodeURIComponent(document.getElementById('dlNbaSubject').value)}&body=${encodeURIComponent(document.getElementById('dlNbaBody').value)}`;
    return true;    // let the link open the mail app
  }
  return false;
}

// Keep edits to the draft across re-renders of the deal room
export function nbaInput(e) {
  if (!nb.draft) return;
  if (e.target.id === 'dlNbaSubject') nb.draft.subject = e.target.value;
  if (e.target.id === 'dlNbaBody') nb.draft.body = e.target.value;
}
