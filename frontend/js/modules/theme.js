import { el } from './dom.js';

// Shared localStorage key with the Account Explorer page.
export function initThemeToggle() {
  let saved = null;
  try { saved = localStorage.getItem('scraperTheme'); } catch (e) { /* private browsing / storage disabled */ }
  if (saved) {
    document.documentElement.setAttribute('data-theme', saved);
  }

  const btn = el('themeToggle');
  if (!btn) return;

  const icon = btn.querySelector('i');
  function setIcon(theme) {
    if (icon) icon.className = theme === 'dark' ? 'fa-solid fa-sun' : 'fa-solid fa-moon';
  }

  if (saved) {
    setIcon(saved);
  }

  btn.addEventListener('click', function () {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    setIcon(next);
    try { localStorage.setItem('scraperTheme', next); } catch (e) { /* private browsing / storage disabled */ }
  });
}
