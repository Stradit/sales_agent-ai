import { state } from './state.js';
import { el, dashPeople } from './dom.js';
import { esc, initials, getPersonasFor } from './utils.js';
import { showToast } from './toast.js';
import { hasDossier, openContactDrawer } from './contact-drawer.js';
import { downloadFile } from './download.js';
import { openContactEditor } from './crm-extras.js';
import { photoAttr } from './persona-photo.js';
// Circular with selection.js (which imports renderPeople from here) — fine: only called on click.
import { renderSelection } from './selection.js';

export function renderContactsList(personas) {
  state.currentPersonas = personas;
  if (!personas.length) return '<div class="people-empty">No contacts match.</div>';
  return personas.slice(0, 12).map((p, idx) => {
    const tag = p.tier || p.decision_authority || (p.departments && p.departments[0]) || null;
    const dossierReady = hasDossier(p);
    return `
      <div class="contact-card">
        <button type="button" class="contact-main" data-contact-idx="${idx}" title="View full contact details">
          <div class="contact-avatar" ${photoAttr(p)}>${esc(initials(p.name))}</div>
          <div class="contact-body">
            <div class="contact-name">${esc(p.name || 'Unnamed')} ${dossierReady ? '<i class="fa-solid fa-star dossier-badge" title="AI call-prep dossier available"></i>' : ''}</div>
            <div class="contact-title">${esc(p.title || 'Title unknown')}</div>
            ${tag ? `<div class="contact-tags"><span class="tag">${esc(tag)}</span></div>` : ''}
          </div>
          <span class="contact-chevron"><i class="fa-solid fa-chevron-right"></i></span>
        </button>
        <div class="contact-actions">
          <a class="icon-btn ${p.email ? '' : 'disabled'}" ${p.email ? `href="mailto:${esc(p.email)}"` : ''} title="${p.email ? 'Email ' + esc(p.name) : 'No email on file'}"><i class="fa-solid fa-envelope"></i></a>
          <a class="icon-btn ${p.phone ? '' : 'disabled'}" ${p.phone ? `href="tel:${esc(p.phone)}"` : ''} title="${p.phone ? 'Call ' + esc(p.name) : 'No phone on file'}"><i class="fa-solid fa-phone"></i></a>
          <a class="icon-btn ${p.linkedin_url ? '' : 'disabled'}" ${p.linkedin_url ? `href="${esc(p.linkedin_url)}" target="_blank"` : ''} title="${p.linkedin_url ? 'LinkedIn' : 'No LinkedIn on file'}"><i class="fa-brands fa-linkedin"></i></a>
        </div>
      </div>`;
  }).join('') + (personas.length > 12 ? `<div class="people-empty">+${personas.length - 12} more contacts — refine your search to narrow it down</div>` : '');
}

export function filterContacts(personas, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return personas;
  return personas.filter(p => (p.name || '').toLowerCase().includes(q) || (p.title || '').toLowerCase().includes(q));
}

export function renderPeople(account, lob) {
  const personas = getPersonasFor(account, lob);
  state.allAccountPersonas = personas;

  const socialLinks = [
    account.linkedin_url ? { label: 'LinkedIn', icon: 'fa-brands fa-linkedin', url: account.linkedin_url } : null,
    account.twitter_url ? { label: 'X / Twitter', icon: 'fa-brands fa-x-twitter', url: account.twitter_url } : (account.twitter_handle ? { label: 'X / Twitter', icon: 'fa-brands fa-x-twitter', url: `https://twitter.com/${account.twitter_handle}` } : null),
    account.blog_url ? { label: 'Blog', icon: 'fa-solid fa-book-open', url: account.blog_url } : null,
    account.github_url ? { label: 'GitHub', icon: 'fa-brands fa-github', url: account.github_url } : null,
    account.glassdoor_url ? { label: 'Glassdoor', icon: 'fa-solid fa-building', url: account.glassdoor_url } : null,
    account.website_url ? { label: 'Website', icon: 'fa-solid fa-earth-americas', url: account.website_url } : null
  ].filter(Boolean);

  return `
    <!-- Key Contacts Section -->
    <div class="panel-title" style="margin-top:2px;">
      <span><i class="fa-solid fa-address-card"></i> Key Contacts</span>
      <span style="display:flex; gap:6px; align-items:center;">
        <span class="context-badge live">${personas.length} mapped</span>
        <button type="button" class="context-badge export-btn cx-add-btn" data-add-contact="${account.id}" title="Add a contact by hand"><i class="fa-solid fa-user-plus"></i> Add</button>
        ${personas.length ? `<button type="button" class="context-badge export-btn" data-export-people="${account.id}" title="Download these contacts as Excel"><i class="fa-regular fa-file-excel"></i> Excel</button>` : ''}
      </span>
    </div>
    <p class="section-desc" style="margin-bottom:8px;">Executive stakeholders &amp; decision makers. Click any card to open the AI Call-Prep Dossier.</p>

    <div class="contact-search">
      <i class="fa-solid fa-magnifying-glass"></i>
      <input type="text" id="contactSearchInput" placeholder="Filter contacts by name or title..." autocomplete="off">
    </div>
    <div id="contactsListContainer">${renderContactsList(personas)}</div>

    <!-- Social & Web Footprint Section -->
    <div class="panel-title" style="margin-top:18px;">
      <span><i class="fa-solid fa-globe"></i> Social &amp; Web Footprint</span>
    </div>
    <p class="section-desc" style="margin-bottom:8px;">Verified corporate web properties and active public discourse channels.</p>
    ${socialLinks.length ? `<div class="social-links">${socialLinks.map(s => `<a class="social-link" href="${esc(s.url)}" target="_blank" title="Open ${esc(s.label)} profile"><i class="${s.icon}"></i> ${esc(s.label)}</a>`).join('')}</div>`
      : '<div class="people-empty">No social/web links on file.</div>'}
  `;
}

dashPeople.addEventListener('input', function (e) {
  if (e.target.id !== 'contactSearchInput') return;
  const filtered = filterContacts(state.allAccountPersonas, e.target.value);
  el('contactsListContainer').innerHTML = renderContactsList(filtered);
});

dashPeople.addEventListener('click', async function (e) {
  const addBtn = e.target.closest('[data-add-contact]');
  if (addBtn) {
    const account = state.accounts.find(a => a.id === Number(addBtn.dataset.addContact));
    if (!account) return;
    openContactEditor({
      accountId: account.id, accountName: account.name,
      onSaved: (c) => {
        if (!c) return;
        (account.personas = account.personas || []).push({ id: c.id, account_id: c.account_id, name: c.name, title: c.title,
          email: c.email, phone: c.phone, linkedin_url: c.linkedin_url, source: 'manual' });
        renderSelection();
      },
    });
    return;
  }
  const exportBtn = e.target.closest('[data-export-people]');
  if (exportBtn) {
    try {
      await downloadFile(`/api/accounts/${exportBtn.dataset.exportPeople}/people/export`, 'contacts.xlsx');
      showToast('Contacts downloaded');
    } catch (err) { showToast(err.message); }
    return;
  }
  const contactBtn = e.target.closest('[data-contact-idx]');
  if (contactBtn) {
    const p = state.currentPersonas[Number(contactBtn.dataset.contactIdx)];
    if (p) openContactDrawer(p);
    return;
  }
});
