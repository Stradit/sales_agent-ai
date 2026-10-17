// Close plan (#10): the dated steps to signature both sides agree on — a "mutual action plan".
// API: /api/deals/{id}/plan (apps/sales_deals/api.py). Rendered as a tab in the deal room.
const SIDE_LABEL = { us: 'StradIT', customer: 'Customer', both: 'Both' };
const STATUS_LABEL = { todo: 'To do', done: 'Done', blocked: 'Blocked' };

let c = null;          // { api, esc, dateLabel, showToast, getDeal, refresh }
let editing = null;    // id of the milestone being edited

const readOnly = () => document.body.classList.contains('role-viewer');

export function planTabLabel(d) {
  const p = d.plan_progress || { done: 0, total: 0 };
  return p.total ? `Close plan (${p.done}/${p.total})` : 'Close plan';
}

export function planPane(d, ctx) {
  c = ctx;
  const items = d.close_plan || [];
  const ro = readOnly();
  const done = items.filter(i => i.status === 'done').length;
  const overdue = items.filter(i => i.overdue).length;
  const pct = items.length ? Math.round((100 * done) / items.length) : 0;
  const head = items.length ? `<div class="dl-plan-head">
      <div class="dl-stage-progress" role="progressbar" aria-valuemin="0" aria-valuemax="${items.length}" aria-valuenow="${done}" aria-label="Milestones done"><span style="width:${pct}%"></span></div>
      <div class="dl-plan-sum">${done} of ${items.length} done${overdue ? ` · <span class="dl-overdue">${overdue} overdue</span>` : ''}
        ${d.expected_close ? ` · expected close ${c.esc(c.dateLabel(d.expected_close))}` : ''}
        <button type="button" class="dl-btn dl-plan-copy" data-plan-copy title="Copy the plan as text to share with the customer"><i class="fa-regular fa-copy"></i> Copy for the customer</button></div>
    </div>` : '';
  const empty = `<div class="dl-plan-empty">
      <p><strong>Agree the steps to signature with your champion.</strong> A shared, dated plan — security review, legal,
        procurement, signature — shows early when a deal is slipping.</p>
      ${ro ? '' : `<button type="button" class="dl-btn dl-btn-primary" data-plan-template><i class="fa-solid fa-wand-magic-sparkles"></i> Start with the standard plan</button>
        <span class="dl-muted">7 steps, dated back from the expected close${d.expected_close ? '' : ' (30 days out — set an expected close first for better dates)'}. Edit them freely.</span>`}
    </div>`;
  return `${head}
    ${items.length ? `<ol class="dl-plan-list">${items.map(i => (editing === i.id && !ro ? editRow(i) : row(i, ro))).join('')}</ol>` : empty}
    ${ro ? '' : `<form class="dl-plan-form" id="dlPlanForm" autocomplete="off">
      <label class="dl-plan-title">New step<input id="dlPlanTitle" maxlength="200" placeholder="e.g. Data protection review" required></label>
      <label>Whose step<select id="dlPlanSide">${Object.entries(SIDE_LABEL).map(([k, l]) => `<option value="${k}">${l}</option>`).join('')}</select></label>
      <label>Owner<input id="dlPlanOwner" maxlength="120" placeholder="Name"></label>
      <label>Due<input id="dlPlanDue" type="date"></label>
      <div class="dl-task-submit"><button type="submit" class="dl-btn"><i class="fa-solid fa-plus"></i> Add step</button></div>
    </form>`}`;
}

function row(i, ro) {
  return `<li class="dl-plan-item s-${i.status}${i.overdue ? ' is-overdue' : ''}">
    <input type="checkbox" data-plan-done="${i.id}" aria-label="Mark “${c.esc(i.title)}” done"${i.status === 'done' ? ' checked' : ''}${ro ? ' disabled' : ''}>
    <div class="dl-plan-main">
      <div class="dl-plan-name">${c.esc(i.title)}</div>
      <div class="dl-plan-meta"><span class="dl-plan-side side-${i.side}">${SIDE_LABEL[i.side]}</span>
        ${i.owner_name ? `<span><i class="fa-regular fa-user"></i> ${c.esc(i.owner_name)}</span>` : ''}
        ${i.due_date ? `<span class="${i.overdue ? 'dl-overdue' : ''}"><i class="fa-regular fa-calendar"></i> ${i.overdue ? 'Overdue · ' : ''}${c.esc(c.dateLabel(i.due_date))}</span>` : '<span class="dl-muted">No date</span>'}
        ${i.status === 'blocked' ? '<span class="dl-plan-blocked"><i class="fa-solid fa-ban"></i> Blocked</span>' : ''}</div>
      ${i.note ? `<div class="dl-plan-note">${c.esc(i.note)}</div>` : ''}
    </div>
    ${ro ? '' : `<div class="dl-plan-ctl">
      <button type="button" class="dl-icon" data-plan-edit="${i.id}" aria-label="Edit “${c.esc(i.title)}”"><i class="fa-solid fa-pen"></i></button>
      <button type="button" class="dl-icon" data-plan-delete="${i.id}" aria-label="Remove “${c.esc(i.title)}”"><i class="fa-regular fa-trash-can"></i></button></div>`}
  </li>`;
}

function editRow(i) {
  return `<li class="dl-plan-item editing"><form class="dl-plan-edit" data-plan-edit-form="${i.id}" autocomplete="off">
    <label class="dl-plan-title">Step<input name="title" maxlength="200" required value="${c.esc(i.title)}"></label>
    <label>Whose step<select name="side">${Object.entries(SIDE_LABEL).map(([k, l]) => `<option value="${k}"${k === i.side ? ' selected' : ''}>${l}</option>`).join('')}</select></label>
    <label>Owner<input name="owner_name" maxlength="120" value="${c.esc(i.owner_name || '')}"></label>
    <label>Due<input name="due_date" type="date" value="${c.esc(i.due_date || '')}"></label>
    <label>Status<select name="status">${Object.entries(STATUS_LABEL).map(([k, l]) => `<option value="${k}"${k === i.status ? ' selected' : ''}>${l}</option>`).join('')}</select></label>
    <label class="dl-plan-title">Note<input name="note" maxlength="1000" value="${c.esc(i.note || '')}" placeholder="e.g. waiting on their CISO"></label>
    <div class="dl-task-submit"><button type="submit" class="dl-btn dl-btn-primary">Save</button>
      <button type="button" class="dl-btn" data-plan-cancel>Cancel</button></div>
  </form></li>`;
}

async function call(path, opts) {
  const d = c.getDeal();
  const out = await c.api(`/${d.id}/plan${path}`, opts);
  d.close_plan = out.close_plan;
  await c.refresh();     // health, progress and the board pick up the change
}

function planText(d) {
  const lines = [`Close plan — ${d.name} (${d.account_name})`, ''];
  (d.close_plan || []).forEach((i, n) => {
    lines.push(`${n + 1}. [${i.status === 'done' ? 'x' : ' '}] ${i.title} — ${SIDE_LABEL[i.side]}${i.owner_name ? ` (${i.owner_name})` : ''}`
      + `${i.due_date ? ` — by ${c.dateLabel(i.due_date)}` : ''}${i.status === 'blocked' ? ' — BLOCKED' : ''}`);
  });
  return lines.join('\n');
}

// Returns true when the event was a close-plan action (so main.js stops handling it)
export async function planClick(e) {
  const t = e.target;
  try {
    if (t.closest('[data-plan-template]')) { await call('/template', { method: 'POST' }); c.showToast('Standard plan added — adjust the dates with your champion'); return true; }
    const ed = t.closest('[data-plan-edit]');
    if (ed) { editing = Number(ed.dataset.planEdit); await c.refresh(); focusEdit(); return true; }
    if (t.closest('[data-plan-cancel]')) { editing = null; await c.refresh(); return true; }
    const del = t.closest('[data-plan-delete]');
    if (del) { await call(`/${del.dataset.planDelete}`, { method: 'DELETE' }); c.showToast('Step removed'); return true; }
    if (t.closest('[data-plan-copy]')) {
      const txt = planText(c.getDeal());
      try { await navigator.clipboard.writeText(txt); c.showToast('Plan copied — paste it into your email to the customer'); }
      catch { c.showToast('Copy failed — your browser blocked the clipboard'); }
      return true;
    }
  } catch (err) { c.showToast(err.message); return true; }
  return false;
}

export async function planChange(e) {
  const t = e.target;
  if (!t.dataset.planDone) return false;
  t.disabled = true;
  try { await call(`/${t.dataset.planDone}`, { method: 'PATCH', body: { status: t.checked ? 'done' : 'todo' } }); c.showToast(t.checked ? 'Step done' : 'Step reopened'); }
  catch (err) { c.showToast(err.message); t.checked = !t.checked; t.disabled = false; }
  return true;
}

export async function planSubmit(e) {
  const f = e.target;
  if (f.id === 'dlPlanForm') {
    const title = f.querySelector('#dlPlanTitle').value.trim();
    if (!title) return true;
    try {
      await call('', { method: 'POST', body: { title, side: f.querySelector('#dlPlanSide').value,
        owner_name: f.querySelector('#dlPlanOwner').value.trim() || null, due_date: f.querySelector('#dlPlanDue').value || null } });
      const next = document.getElementById('dlPlanTitle'); if (next) next.focus();
    } catch (err) { c.showToast(err.message); }
    return true;
  }
  if (f.dataset.planEditForm) {
    const v = (n) => f.elements[n].value.trim();
    try {
      editing = null;
      await call(`/${f.dataset.planEditForm}`, { method: 'PATCH', body: { title: v('title'), side: v('side'), owner_name: v('owner_name') || null,
        due_date: v('due_date') || null, status: v('status'), note: v('note') || null } });
      c.showToast('Step saved');
    } catch (err) { c.showToast(err.message); }
    return true;
  }
  return false;
}

function focusEdit() { const el = document.querySelector('[data-plan-edit-form] input'); if (el) el.focus(); }
export function resetPlanEditing() { editing = null; }
