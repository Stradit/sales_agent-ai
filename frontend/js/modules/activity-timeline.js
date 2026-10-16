// Shared activity timeline (apps/sales_crm/activities.py, README §4.3). Mounted on the deal room,
// introduction drawer, profile page and account view:
//   mountTimeline(el, { objectType, objectId, accountId, dealId, introductionId, personaId, onChange })
// Log a meeting / call / email / note / LinkedIn touch, upload a meeting transcript (.vtt .srt .txt
// .docx — summary and action items are extracted without AI), filter by type, make private, delete,
// and turn transcript action items into deal tasks. Styles: css/activity-timeline.css.
import { esc } from './utils.js';
import { showToast } from './toast.js';

const TYPE_ICON = { email: 'fa-envelope', meeting: 'fa-people-group', call: 'fa-phone', note: 'fa-note-sticky',
  transcript: 'fa-file-lines', linkedin: 'fa-linkedin', task_done: 'fa-circle-check' };
const LOG_TYPES = [['meeting', 'Meeting'], ['call', 'Call'], ['email', 'Email'], ['note', 'Note'], ['linkedin', 'LinkedIn']];
const OBJ_ICON = { deal: 'fa-diagram-next', introduction: 'fa-handshake', persona: 'fa-user', account: 'fa-building' };
let metaPromise = null;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

function readOnlyUser() {
  metaPromise = metaPromise || api('/api/crm/meta').catch(() => ({}));
  return metaPromise.then(m => !!(m.me && m.me.read_only && m.auth_enforced));
}

function iconClass(type) { return `${type === 'linkedin' ? 'fa-brands' : 'fa-solid'} ${TYPE_ICON[type] || 'fa-circle'}`; }

function dayLabel(ts) {
  const d = new Date(ts);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  if (diff === -1) return 'Tomorrow';
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric', year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}
const timeLabel = (ts) => new Date(ts).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
function localInput(d = new Date()) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1] || '');
    r.onerror = () => reject(new Error('Could not read the file'));
    r.readAsDataURL(file);
  });
}

export function mountTimeline(el, ctx) {
  const st = { items: [], counts: {}, filter: '', open: null, readOnly: false, people: [], picked: new Map() };
  const selfKey = `${ctx.objectType}:${ctx.objectId}`;
  const accountId = ctx.accountId || (ctx.objectType === 'account' ? ctx.objectId : null);

  function linkChips(a) {
    return a.links.filter(l => `${l.object_type}:${l.object_id}` !== selfKey && !(l.object_type === 'account' && ctx.objectType !== 'account' && l.matched_by === 'derived'))
      .map(l => {
        const href = l.object_type === 'deal' ? `/deals?deal=${l.object_id}` : l.object_type === 'introduction' ? `/introductions?intro=${l.object_id}`
          : l.object_type === 'persona' ? `/profile?account=${l.persona_account_id}&persona_id=${l.object_id}` : `/?account=${l.object_id}`;
        return `<a class="at-chip" href="${href}"><i class="fa-solid ${OBJ_ICON[l.object_type]}"></i> ${esc(l.name || l.object_type)}</a>`;
      }).join('');
  }

  function itemHtml(a) {
    const speakers = a.speakers.length ? `<div class="at-speakers" aria-label="Talk time">${a.speakers.slice(0, 6).map(sp =>
      `<span class="${sp.is_internal ? 'us' : sp.is_internal === false ? 'them' : ''}" style="flex:${Math.max(sp.share_pct, 4)}"
        title="${esc(sp.name)} — ${sp.share_pct}% of the words${sp.persona_id ? '' : sp.is_internal ? ' (StradIT)' : ' (not matched to a contact)'}">${esc(sp.name.split(' ')[0])} ${sp.share_pct}%</span>`).join('')}</div>` : '';
    const actions = a.action_items.length ? `<div class="at-actions"><strong>Action items</strong><ul>${a.action_items.map((x, i) => `<li>
      <span><em>${esc(x.speaker)}:</em> ${esc(x.text)}</span>${ctx.dealId && !st.readOnly ? `<button type="button" class="at-link" data-task="${a.id}:${i}">Add as task</button>` : ''}</li>`).join('')}</ul></div>` : '';
    return `<li class="at-item${a.visibility === 'private' ? ' private' : ''}" data-activity="${a.id}">
      <span class="at-icon ${a.type}"><i class="${iconClass(a.type)}"></i></span>
      <div class="at-main">
        <div class="at-head"><strong>${esc(a.subject || a.type_label)}</strong>
          <span class="at-meta">${esc(a.type_label)}${a.direction && a.direction !== 'internal' ? ` · ${esc(a.direction)}` : ''}${a.duration_min ? ` · ${a.duration_min} min` : ''} · ${esc(timeLabel(a.occurred_at))} · ${esc(a.owner_name || 'Someone')}
            ${a.visibility === 'private' ? '<span class="at-private"><i class="fa-solid fa-lock"></i> Private</span>' : ''}</span></div>
        ${a.summary ? `<div class="at-summary">${esc(a.summary).replace(/\n/g, '<br>')}</div>` : ''}
        ${speakers}${actions}
        ${a.links.length ? `<div class="at-chips">${linkChips(a)}</div>` : ''}
        <div class="at-tools">
          ${a.has_body ? `<button type="button" class="at-link" data-body="${a.id}">${st.open === a.id ? 'Hide transcript' : 'Show transcript'}</button>` : ''}
          ${a.can_edit ? `<button type="button" class="at-link" data-private="${a.id}">${a.visibility === 'private' ? 'Share with team' : 'Make private'}</button>
            <button type="button" class="at-link danger" data-delete="${a.id}">Delete</button>` : ''}
        </div>
        <div class="at-body" id="atBody${a.id}" hidden></div>
      </div></li>`;
  }

  function listHtml() {
    const items = st.filter ? st.items.filter(a => a.type === st.filter) : st.items;
    if (!items.length) return `<p class="at-empty">${st.items.length ? 'Nothing of this type yet.' : 'No interactions logged yet. Log a meeting, call or email, or upload a meeting transcript.'}</p>`;
    let last = '';
    return `<ol class="at-list">${items.map(a => {
      const d = dayLabel(a.occurred_at);
      const head = d !== last ? `<li class="at-day">${esc(d)}</li>` : '';
      last = d;
      return head + itemHtml(a);
    }).join('')}</ol>`;
  }

  function render() {
    const types = Object.keys(st.counts);
    el.innerHTML = `<div class="at">
      <div class="at-bar">
        <div class="at-filters" role="group" aria-label="Filter by type">
          <button type="button" class="at-filter${st.filter ? '' : ' on'}" data-filter="">All ${st.items.length}</button>
          ${types.map(t => `<button type="button" class="at-filter${st.filter === t ? ' on' : ''}" data-filter="${t}"><i class="${iconClass(t)}"></i> ${st.counts[t]}</button>`).join('')}
        </div>
        ${st.readOnly ? '' : `<div class="at-buttons">
          <button type="button" class="at-btn primary" data-open-form="log"><i class="fa-solid fa-plus"></i> Log activity</button>
          ${accountId ? '<button type="button" class="at-btn" data-open-form="transcript"><i class="fa-solid fa-file-arrow-up"></i> Upload transcript</button>' : ''}
        </div>`}
      </div>
      <form class="at-form" data-form="log" hidden autocomplete="off">
        <div class="at-types" role="radiogroup" aria-label="Type">${LOG_TYPES.map(([k, l], i) =>
          `<label><input type="radio" name="atType" value="${k}"${i === 0 ? ' checked' : ''}> <i class="${iconClass(k)}"></i> ${l}</label>`).join('')}</div>
        <div class="at-row">
          <input name="subject" maxlength="300" placeholder="Subject — e.g. Discovery call with the CIO" aria-label="Subject">
          <input name="when" type="datetime-local" value="${localInput()}" aria-label="When">
          <input name="duration" type="number" min="0" max="1440" step="5" placeholder="Min" aria-label="Duration in minutes" class="at-dur">
          <select name="direction" aria-label="Direction"><option value="">—</option><option value="outbound">Outbound</option><option value="inbound">Inbound</option></select>
        </div>
        <textarea name="summary" rows="3" maxlength="20000" placeholder="What happened? Decisions, pains, next steps…" aria-label="Notes"></textarea>
        ${accountId && ctx.objectType !== 'persona' ? `<div class="at-people"><input name="peopleSearch" placeholder="Who was involved? Search contacts…" aria-label="Search contacts">
          <div class="at-picked" data-picked></div><div class="at-suggest" data-suggest hidden></div></div>` : ''}
        <div class="at-row at-foot"><label class="at-check"><input type="checkbox" name="private"> Private (only me)</label>
          <span class="at-spacer"></span><button type="button" class="at-btn" data-cancel>Cancel</button><button type="submit" class="at-btn primary">Save</button></div>
      </form>
      <form class="at-form" data-form="transcript" hidden>
        <p class="at-hint">Teams, Zoom or Meet transcript (.vtt, .srt, .txt or .docx, up to 3 MB). Speakers are matched to contacts at this account;
          key points and action items are pulled out automatically — no AI is used.</p>
        <div class="at-row">
          <input name="file" type="file" accept=".vtt,.srt,.txt,.docx" required aria-label="Transcript file">
          <input name="when" type="datetime-local" value="${localInput()}" aria-label="Meeting date">
          <input name="duration" type="number" min="0" max="1440" step="5" placeholder="Min" aria-label="Duration in minutes" class="at-dur">
        </div>
        <input name="subject" maxlength="300" placeholder="Subject (optional)" aria-label="Subject">
        <div class="at-row at-foot"><label class="at-check"><input type="checkbox" name="private"> Private (only me)</label>
          <span class="at-spacer"></span><button type="button" class="at-btn" data-cancel>Cancel</button><button type="submit" class="at-btn primary">Upload</button></div>
      </form>
      <div class="at-list-wrap">${listHtml()}</div>
    </div>`;
  }

  async function load() {
    try {
      const data = await api(`/api/crm/activities?object_type=${ctx.objectType}&object_id=${ctx.objectId}`);
      st.items = data.activities;
      st.counts = data.counts;
      render();
    } catch (err) { el.innerHTML = `<p class="at-empty">${esc(err.message)}</p>`; }
  }

  function renderPicked() {
    const box = el.querySelector('[data-picked]');
    if (box) box.innerHTML = [...st.picked].map(([id, name]) => `<span class="at-chip">${esc(name)} <button type="button" data-unpick="${id}" aria-label="Remove ${esc(name)}">×</button></span>`).join('');
  }

  let searchTimer = null;
  async function searchPeople(q) {
    const box = el.querySelector('[data-suggest]');
    if (!box) return;
    try {
      const people = await api(`/api/crm/people?account_id=${accountId}&q=${encodeURIComponent(q)}`);
      box.innerHTML = people.filter(p => !st.picked.has(p.id)).slice(0, 8).map(p =>
        `<button type="button" data-pick="${p.id}" data-name="${esc(p.name)}">${esc(p.name)}<small>${esc(p.title || '')}</small></button>`).join('')
        || '<span class="at-empty">No matches</span>';
      box.hidden = false;
    } catch { box.hidden = true; }
  }

  function baseLinks() {
    const links = [{ object_type: ctx.objectType, object_id: ctx.objectId }];
    if (ctx.dealId && ctx.objectType !== 'deal') links.push({ object_type: 'deal', object_id: ctx.dealId });
    if (ctx.introductionId && ctx.objectType !== 'introduction') links.push({ object_type: 'introduction', object_id: ctx.introductionId });
    if (ctx.personaId && ctx.objectType !== 'persona') links.push({ object_type: 'persona', object_id: ctx.personaId });
    return links;
  }

  el.addEventListener('click', async (e) => {
    const t = e.target;
    const f = t.closest('[data-filter]');
    if (f) { st.filter = f.dataset.filter; render(); return; }
    const open = t.closest('[data-open-form]');
    if (open) {
      el.querySelectorAll('.at-form').forEach(x => { x.hidden = x.dataset.form !== open.dataset.openForm || !x.hidden; });
      const form = el.querySelector(`[data-form="${open.dataset.openForm}"]`);
      if (!form.hidden) (form.querySelector('input[name="subject"]') || form.querySelector('input')).focus();
      return;
    }
    if (t.closest('[data-cancel]')) { t.closest('.at-form').hidden = true; return; }
    const pick = t.closest('[data-pick]');
    if (pick) { st.picked.set(Number(pick.dataset.pick), pick.dataset.name); renderPicked(); el.querySelector('[data-suggest]').hidden = true; el.querySelector('input[name="peopleSearch"]').value = ''; return; }
    const unpick = t.closest('[data-unpick]');
    if (unpick) { st.picked.delete(Number(unpick.dataset.unpick)); renderPicked(); return; }
    const body = t.closest('[data-body]');
    if (body) {
      const id = Number(body.dataset.body);
      const box = el.querySelector(`#atBody${id}`);
      if (!box.hidden) { box.hidden = true; st.open = null; body.textContent = 'Show transcript'; return; }
      try {
        const a = await api(`/api/crm/activities/${id}`);
        box.innerHTML = `<pre>${esc(a.body || '')}</pre>`;
        box.hidden = false; st.open = id; body.textContent = 'Hide transcript';
      } catch (err) { showToast(err.message); }
      return;
    }
    const priv = t.closest('[data-private]');
    if (priv) {
      const a = st.items.find(x => x.id === Number(priv.dataset.private));
      try { await api(`/api/crm/activities/${a.id}`, { method: 'PATCH', body: { visibility: a.visibility === 'private' ? 'team' : 'private' } }); load(); }
      catch (err) { showToast(err.message); }
      return;
    }
    const del = t.closest('[data-delete]');
    if (del) {
      if (del.dataset.confirm !== '1') { del.dataset.confirm = '1'; del.textContent = 'Click again to delete'; setTimeout(() => { if (del.isConnected) { del.dataset.confirm = ''; del.textContent = 'Delete'; } }, 4000); return; }
      try { await api(`/api/crm/activities/${del.dataset.delete}`, { method: 'DELETE' }); showToast('Activity deleted'); load(); if (ctx.onChange) ctx.onChange(); }
      catch (err) { showToast(err.message); }
      return;
    }
    const task = t.closest('[data-task]');
    if (task) {
      const [aid, idx] = task.dataset.task.split(':').map(Number);
      const item = st.items.find(x => x.id === aid).action_items[idx];
      try {
        await api(`/api/deals/${ctx.dealId}/tasks`, { method: 'POST', body: { title: item.text.slice(0, 500), priority: 'medium' } });
        task.replaceWith(Object.assign(document.createElement('span'), { className: 'at-done', textContent: '✓ Task added' }));
        if (ctx.onChange) ctx.onChange();
      } catch (err) { showToast(err.message); }
    }
  });

  el.addEventListener('input', (e) => {
    if (e.target.name !== 'peopleSearch') return;
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    searchTimer = setTimeout(() => searchPeople(q), 200);
  });

  el.addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    try {
      const when = form.when.value ? new Date(form.when.value).toISOString() : null;
      const duration = form.duration.value === '' ? null : Number(form.duration.value);
      const visibility = form.private.checked ? 'private' : 'team';
      if (form.dataset.form === 'log') {
        const res = await api('/api/crm/activities', { method: 'POST', body: {
          type: form.querySelector('input[name="atType"]:checked').value, direction: form.direction.value || null,
          subject: form.subject.value.trim() || null, summary: form.summary.value.trim() || null, occurred_at: when,
          duration_min: duration, visibility, links: baseLinks(), participant_persona_ids: [...st.picked.keys()],
        } });
        st.picked.clear();
        showToast(`${res.type_label} logged`);
      } else {
        const file = form.file.files[0];
        if (!file) throw new Error('Choose a transcript file');
        if (file.size > 3 * 1024 * 1024) throw new Error('Transcripts are limited to 3 MB');
        const res = await api('/api/crm/activities/transcript', { method: 'POST', body: {
          file_name: file.name, content_base64: await fileToBase64(file), account_id: accountId,
          deal_id: ctx.dealId || (ctx.objectType === 'deal' ? ctx.objectId : null),
          introduction_id: ctx.introductionId || (ctx.objectType === 'introduction' ? ctx.objectId : null),
          subject: form.subject.value.trim() || null, occurred_at: when, duration_min: duration, visibility,
        } });
        showToast(`Transcript added — ${res.matched_contacts} contact${res.matched_contacts === 1 ? '' : 's'} matched, ${res.action_items.length} action item${res.action_items.length === 1 ? '' : 's'}`
          + (res.unmatched_speakers.length ? `; not matched: ${res.unmatched_speakers.slice(0, 3).join(', ')}` : ''));
      }
      await load();
      if (ctx.onChange) ctx.onChange();
    } catch (err) { showToast(err.message); btn.disabled = false; }
  });

  readOnlyUser().then(ro => { st.readOnly = ro; load(); });
  return { reload: load };
}
