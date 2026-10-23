// Shared auth state for the browser side. The access token lives in a
// module-level variable only (never localStorage) — see
// AUTH_JWT_IMPLEMENTATION_PLAN.md §3 for why: it meaningfully limits what an
// XSS bug could exfiltrate, at the cost of needing a silent refresh on every
// page load (the refresh token itself lives in an HttpOnly cookie the page
// can't read at all).
//
// Deliberately uses the native fetch (via a reference captured before
// fetch-instrumentation.js patches window.fetch), not window.fetch, so the
// 401-retry logic in fetch-instrumentation.js never has to reason about its
// own login/refresh/logout calls recursing into itself.
import { clearAccountsCache } from './accounts-cache.js';

const nativeFetch = window.fetch.bind(window);

let accessToken = null;
let currentUser = null;

export function getAccessToken() {
  return accessToken;
}

export function getCurrentUser() {
  return currentUser;
}

function _applySession(data) {
  accessToken = data.access_token || null;
  currentUser = data.user || null;
  try {
    if (currentUser && (currentUser.id || currentUser.email)) {
      localStorage.setItem('doshin_user_id', String(currentUser.id || ''));
      localStorage.setItem('doshin_user_email', String(currentUser.email || ''));
    }
  } catch (_) {}
  return data;
}

export function clearSession() {
  accessToken = null;
  currentUser = null;
  try {
    localStorage.removeItem('doshin_user_id');
    localStorage.removeItem('doshin_user_email');
  } catch (_) {}
}

async function _parseOrThrow(res) {
  let body = null;
  try { body = await res.json(); } catch (e) { /* no body */ }
  if (!res.ok) {
    const message = (body && body.detail) || `Request failed (${res.status})`;
    throw new Error(message);
  }
  return body;
}

export async function login(email, password) {
  const res = await nativeFetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ email, password }),
  });
  // Clears out anything cached under a previous session on this same tab —
  // e.g. a different user signed in here before, or this account's own list
  // of visible accounts changed since — before adopting the new session, not
  // after, so nothing else can race a read of stale cached data in between.
  clearAccountsCache();
  return _applySession(await _parseOrThrow(res));
}

export async function logout() {
  try {
    await nativeFetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
  } catch (err) {
    console.warn('Logout request failed, continuing client purge:', err);
  } finally {
    clearSession();
    clearAccountsCache();
    try {
      sessionStorage.clear();
      localStorage.removeItem('access_token');
      localStorage.removeItem('user');
    } catch (e) {
      /* ignore storage access restrictions */
    }
  }
}

// Attempts to mint a fresh access token from the HttpOnly refresh cookie —
// called once on app boot (so a reloaded page doesn't force a re-login) and
// by fetch-instrumentation.js whenever an API call comes back 401.
export async function refreshAccessToken() {
  try {
    const res = await nativeFetch('/api/auth/refresh', { method: 'POST', credentials: 'include' });
    if (!res.ok) {
      clearSession();
      return null;
    }
    _applySession(await res.json());
    return accessToken;
  } catch (e) {
    clearSession();
    return null;
  }
}

export async function forgotPassword(email) {
  const res = await nativeFetch('/api/auth/forgot-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  });
  return _parseOrThrow(res);
}

export async function resetPassword(token, newPassword) {
  const res = await nativeFetch('/api/auth/reset-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, new_password: newPassword }),
  });
  return _parseOrThrow(res);
}
