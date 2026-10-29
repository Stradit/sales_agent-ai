// Network layer for the Sales Copilot (workspace page + dock). fetch() here is
// already patched by fetch-instrumentation.js to attach the bearer token, which
// is why streaming uses fetch + ReadableStream instead of EventSource (which can
// neither POST nor send an Authorization header).

export function api(path, opts = {}) { return request(`/api/copilot${path}`, opts); }

// Other app endpoints (CRM tasks, deals, introductions) — used when the user confirms a Copilot proposal.
export function crm(url, opts = {}) { return request(url, opts); }

async function request(url, opts) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) {
    window.location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    throw new Error('unauthenticated');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;   // FastAPI validation errors come as a list
    throw new Error((Array.isArray(d) ? d.map(x => x.msg).join('; ') : d) || `Request failed (${res.status})`);
  }
  return data;
}

// POST a question and receive server-sent events: status → meta → token* → done | error.
export async function streamChat(body, handlers, signal) {
  const res = await fetch('/api/copilot/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  });
  if (res.status === 401) {
    window.location.replace(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    throw new Error('unauthenticated');
  }
  if (res.status === 404 || res.status === 405) {
    // Server without the streaming route (older build) — same answer, delivered in one piece.
    const data = await api('/chat', { method: 'POST', body, signal });
    const m = data.message;
    handlers.meta && handlers.meta({ session_id: data.session_id, mode: m.mode, intent: m.intent, citations: m.citations, extras: m.extras });
    handlers.token && handlers.token({ t: m.content });
    handlers.done && handlers.done(data);
    return;
  }
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Request failed (${res.status})`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let event = 'message';
      const dataLines = [];
      frame.split('\n').forEach(line => {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
      });
      if (!dataLines.length) continue;
      let data;
      try { data = JSON.parse(dataLines.join('\n')); } catch { continue; }
      if (handlers[event]) handlers[event](data);
    }
  }
}

// Download a server-generated file (PDF / Excel / Markdown) with auth.
export async function download(path, fallbackName) {
  const res = await fetch(`/api/copilot${path}`);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `Download failed (${res.status})`);
  }
  const blob = await res.blob();
  const cd = res.headers.get('Content-Disposition') || '';
  const m = cd.match(/filename="?([^";]+)"?/);
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = m ? m[1] : fallbackName;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}
