// Public landing page — served at / to signed-out visitors (and always at /welcome).
// The search box only carries the typed name through sign-in to the dashboard, which
// opens the matching account (main.js already understands ?account_key=).

// Already signed in but without the session-hint cookie (e.g. signed in before it
// existed): a silent refresh sets it, and reloading / then shows the dashboard.
// Once per browser session, so a cookie the browser refuses can never cause a reload loop.
let triedResume = false;
try { triedResume = sessionStorage.getItem('lpResumeTried') === '1'; sessionStorage.setItem('lpResumeTried', '1'); } catch { /* storage blocked */ }
if (window.location.pathname === '/' && !triedResume) {
  fetch('/api/auth/refresh', { method: 'POST', credentials: 'same-origin' })
    .then(res => { if (res.ok) window.location.replace('/'); })
    .catch(() => { /* signed out — stay on the landing page */ });
}

const search = document.getElementById('lpSearch');
const input = document.getElementById('lpSearchInput');
const tabs = document.querySelectorAll('.lp-tab');
let mode = 'account';

tabs.forEach(tab => tab.addEventListener('click', () => {
  mode = tab.dataset.mode;
  tabs.forEach(t => {
    const on = t === tab;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  input.placeholder = mode === 'person' ? 'Enter a person\'s name' : 'Enter a company name';
  input.focus();
}));

search.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = input.value.trim();
  // Accounts open directly; people go to the copilot with the name ready to ask about.
  const target = !q ? '/'
    : mode === 'person' ? `/copilot?q=${encodeURIComponent(`Who is ${q}?`)}`
      : `/?account_key=${encodeURIComponent(q)}`;
  window.location.href = `/login?next=${encodeURIComponent(target)}`;
});

const menuBtn = document.getElementById('lpMenuBtn');
const nav = document.getElementById('lpNav');
menuBtn.addEventListener('click', () => {
  const open = nav.classList.toggle('open');
  menuBtn.setAttribute('aria-expanded', String(open));
});
nav.addEventListener('click', (e) => {
  if (e.target.closest('a')) { nav.classList.remove('open'); menuBtn.setAttribute('aria-expanded', 'false'); }
});

document.getElementById('lpYear').textContent = new Date().getFullYear();
