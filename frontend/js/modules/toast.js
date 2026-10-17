import { toastEl } from './dom.js';

let hideTimer = null;

function hideToast() {
  toastEl.classList.remove('show', 'has-action');
}

// showToast('Saved') — or with one action button and a longer life:
// showToast('Moved to Proposal', { action: { label: 'Undo', onClick: () => … } })
export function showToast(msg, opts = {}) {
  if (!toastEl) return;
  toastEl.setAttribute('role', 'status');          // announced by screen readers
  toastEl.textContent = msg;
  toastEl.classList.toggle('has-action', !!opts.action);
  if (opts.action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'toast-action';
    btn.textContent = opts.action.label;
    btn.addEventListener('click', () => { clearTimeout(hideTimer); hideToast(); opts.action.onClick(); });
    toastEl.append(' ', btn);
  }
  toastEl.classList.add('show');
  // A new toast restarts the timer, so an older one can't hide it early
  clearTimeout(hideTimer);
  hideTimer = setTimeout(hideToast, opts.duration || (opts.action ? 7000 : 2600));
}
