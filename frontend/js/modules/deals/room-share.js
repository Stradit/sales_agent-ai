// Buyer room sharing (#11), shown at the top of the deal room's Close plan tab: create the
// customer link, copy it, see whether the customer opened it, edit what the page says, replace or
// turn off the link. API: /api/deals/{id}/room (apps/sales_deals/buyer_room.py).
let c = null;              // { api, esc, dateLabel, showToast, getDeal }
const rs = { dealId: null, room: undefined, editing: false, loading: false };

const readOnly = () => document.body.classList.contains('role-viewer');
const ago = (ts) => {
  if (!ts) return '';
  const d = Math.floor((Date.now() - new Date(ts).getTime()) / 86400000);
  return d <= 0 ? 'today' : d === 1 ? 'yesterday' : `${d} days ago`;
};

export function initRoomShare(ctx) { c = ctx; }

// Called on render; loads the room once per deal, then re-renders through ctx.rerender()
export function roomShareBox(d) {
  if (rs.dealId !== d.id) { rs.dealId = d.id; rs.room = undefined; rs.editing = false; }
  if (rs.room === undefined) {
    if (!rs.loading) {
      rs.loading = true;
      c.api(`/${d.id}/room`).then((out) => { rs.room = out.room; }).catch(() => { rs.room = null; })
        .finally(() => { rs.loading = false; if (rs.dealId === d.id) c.rerender(); });
    }
    return '<section class="dl-share"><p class="dl-muted">Loading sharing…</p></section>';
  }
  const ro = readOnly();
  const r = rs.room;
  if (!r) {
    return `<section class="dl-share" aria-labelledby="dlShareTitle">
      <h3 id="dlShareTitle"><i class="fa-solid fa-share-nodes"></i> Share with the customer</h3>
      <p class="dl-muted">A private page your champion can open without signing in: this close plan, a welcome note, useful links and you as their contact.
        Never shown: value, stage, health, notes or the buying committee.</p>
      ${ro ? '' : '<button type="button" class="dl-btn dl-btn-primary" data-room-create><i class="fa-solid fa-link"></i> Create customer link</button>'}
    </section>`;
  }
function formatRoomLink(link) {
  if (!link) return '';
  try {
    const u = new URL(link);
    if (location.hostname === 'localhost' || location.hostname === '127.0.0.1') {
      return `${location.origin}${u.pathname}`;
    }
    return link;
  } catch {
    return link;
  }
}

  const viewed = r.view_count
    ? `<span class="dl-share-seen"><i class="fa-solid fa-eye"></i> Opened ${r.view_count} time${r.view_count === 1 ? '' : 's'} · last ${c.esc(ago(r.last_viewed_at))}</span>`
    : '<span class="dl-muted"><i class="fa-regular fa-eye-slash"></i> Not opened yet</span>';
  const displayLink = formatRoomLink(r.link);
  return `<section class="dl-share" aria-labelledby="dlShareTitle">
    <h3 id="dlShareTitle"><i class="fa-solid fa-share-nodes"></i> Shared with the customer ${r.expired ? '<span class="dl-overdue">· link expired</span>' : ''}</h3>
    ${displayLink ? `<div class="dl-share-link"><input readonly value="${c.esc(displayLink)}" aria-label="Customer link" id="dlShareLink">
      <button type="button" class="dl-btn" data-room-copy><i class="fa-regular fa-copy"></i> Copy</button>
      <a class="dl-btn" href="${c.esc(displayLink)}" target="_blank" rel="noopener noreferrer"><i class="fa-solid fa-arrow-up-right-from-square"></i> Preview</a></div>`
      : '<p class="dl-warn">This link can no longer be shown (the encryption key changed) — create a new link.</p>'}
    <div class="dl-share-meta">${viewed}<span class="dl-muted">Expires ${c.esc(c.dateLabel(r.expires_at))}</span></div>
    ${ro ? '' : rs.editing ? editForm(r) : `<div class="dl-share-actions">
      <button type="button" class="dl-btn" data-room-edit><i class="fa-solid fa-pen"></i> Edit page</button>
      <button type="button" class="dl-btn" data-room-renew title="The old link stops working"><i class="fa-solid fa-rotate"></i> New link</button>
      <button type="button" class="dl-btn dl-btn-danger" data-room-off><i class="fa-solid fa-link-slash"></i> Turn off</button></div>`}
  </section>`;
}

function editForm(r) {
  const res = [...(r.resources || []), { title: '', url: '' }];
  return `<form class="dl-share-form" id="dlShareForm" autocomplete="off">
    <label>Page title<input name="title" maxlength="160" required value="${c.esc(r.title)}"></label>
    <label>Welcome note<textarea name="welcome_note" maxlength="2000" rows="3" placeholder="e.g. Thanks for the time on Tuesday — here's the plan we agreed.">${c.esc(r.welcome_note || '')}</textarea></label>
    <fieldset><legend>Links for the customer <span class="dl-muted">(https only — e.g. proposal, case study, security pack)</span></legend>
      ${res.map((x, i) => `<div class="dl-share-res"><input name="rt${i}" maxlength="120" placeholder="Title" value="${c.esc(x.title)}" aria-label="Link ${i + 1} title">
        <input name="ru${i}" maxlength="1000" placeholder="https://…" value="${c.esc(x.url)}" aria-label="Link ${i + 1} address"></div>`).join('')}
    </fieldset>
    <div class="dl-share-actions"><button type="submit" class="dl-btn dl-btn-primary">Save page</button>
      <button type="button" class="dl-btn" data-room-cancel>Cancel</button></div>
  </form>`;
}

async function save(body, method = 'PATCH') {
  const out = await c.api(`/${c.getDeal().id}/room`, { method, body });
  rs.room = out.room;
  c.rerender();
}

// Returns true if the click was handled here
export async function roomShareClick(e) {
  const t = e.target;
  try {
    if (t.closest('[data-room-create]')) { await save({}, 'POST'); c.showToast('Customer link created — copy it into your email'); return true; }
    if (t.closest('[data-room-copy]')) {
      try { await navigator.clipboard.writeText(document.getElementById('dlShareLink').value); c.showToast('Link copied'); }
      catch { document.getElementById('dlShareLink').select(); c.showToast('Press Ctrl+C to copy'); }
      return true;
    }
    if (t.closest('[data-room-edit]')) { rs.editing = true; c.rerender(); const f = document.querySelector('#dlShareForm input'); if (f) f.focus(); return true; }
    if (t.closest('[data-room-cancel]')) { rs.editing = false; c.rerender(); return true; }
    if (t.closest('[data-room-renew]')) { await save({}, 'POST'); c.showToast('New link created — the old one no longer works'); return true; }
    if (t.closest('[data-room-off]')) {
      await c.api(`/${c.getDeal().id}/room`, { method: 'DELETE' });
      rs.room = null; c.rerender(); c.showToast('Link turned off — the customer can no longer open it');
      return true;
    }
  } catch (err) { c.showToast(err.message); return true; }
  return false;
}

export async function roomShareSubmit(e) {
  if (e.target.id !== 'dlShareForm') return false;
  const f = e.target;
  const resources = [];
  for (let i = 0; f.elements[`rt${i}`]; i += 1) {
    const title = f.elements[`rt${i}`].value.trim(); const url = f.elements[`ru${i}`].value.trim();
    if (title || url) resources.push({ title: title || url, url });
  }
  try {
    rs.editing = false;
    await save({ title: f.elements.title.value.trim(), welcome_note: f.elements.welcome_note.value, resources });
    c.showToast('Customer page saved');
  } catch (err) { rs.editing = true; c.showToast(err.message); }
  return true;
}
