// Talking to the HQ server: every request carries the launch token; the live stream reconnects by itself.
//
// The token comes from this page's own address (?token=...). It is only ever sent back to this same server.

const TOKEN = new URLSearchParams(window.location.search).get('token') || '';
export const BASE = document.querySelector('meta[name="hq-base"]')?.content || './';

export class HttpError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
  }
}

async function send(path, options) {
  // An optional time limit (timeoutMs): past it the request is given up and the caller says so.
  const { timeoutMs, ...rest } = options;
  const controller = timeoutMs ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  let response;
  try {
    response = await fetch(path, {
      ...rest,
      signal: controller?.signal,
      headers: { 'X-Town-Token': TOKEN, 'Content-Type': 'application/json', ...(rest.headers || {}) },
      cache: 'no-store',
    });
  } catch (err) {
    if (err?.name === 'AbortError') throw new HttpError(0, 'no answer in time');
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const detail = body?.detail || body?.error || `The server answered ${response.status}.`;
    throw new HttpError(response.status, typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body;
}

export const get = (path) => send(path, { method: 'GET' });

/** Where HQ shows a build's own files (read-only; the token rides in the path like the page's own files). */
export const siteUrl = (buildId) => `/site/${encodeURIComponent(TOKEN)}/${Number(buildId)}/`;

/** One line in Reaper's own voice, from the server: the WAV as an ArrayBuffer. */
export async function speech(text) {
  const response = await fetch('/api/speak', {
    method: 'POST', cache: 'no-store', body: JSON.stringify({ text: String(text).slice(0, 600) }),
    headers: { 'X-Town-Token': TOKEN, 'Content-Type': 'application/json' },
  });
  if (!response.ok) throw new HttpError(response.status, 'no voice');
  return response.arrayBuffer();
}
export const post = (path, body, { timeoutMs } = {}) =>
  send(path, { method: 'POST', body: JSON.stringify(body || {}), timeoutMs });

/**
 * The live stream (server-sent events) with catch-up. `after` is the last event id already shown.
 * On a drop it waits, asks /api/flows for everything after the last flow seen (so nothing is lost), then
 * reopens the stream from there. A 401 means HQ restarted with a new link: it stops and says so.
 * @param {{after: number, onFlow: Function, onFacts: Function, onStatus: Function}} handlers
 */
export function openStream({ after, onFlow, onFacts, onStatus }) {
  let lastId = after;
  let source = null;
  let retry = 0;
  let closed = false;
  const seen = new Set();

  const deliver = (flow) => {
    const key = `${flow.event_id}:${flow.from_node}:${flow.to_node}:${flow.kind}`;
    if (seen.has(key)) return;
    seen.add(key);
    if (seen.size > 2000) seen.delete(seen.values().next().value);
    lastId = Math.max(lastId, flow.event_id);
    onFlow(flow);
  };

  function connect() {
    if (closed) return;
    source = new EventSource(`/api/stream?token=${encodeURIComponent(TOKEN)}&after=${lastId}`);
    source.addEventListener('open', () => { retry = 0; onStatus('live'); });
    source.addEventListener('flow', (e) => deliver(JSON.parse(e.data)));
    source.addEventListener('facts', (e) => onFacts(JSON.parse(e.data)));
    source.addEventListener('error', () => {
      source.close();
      if (!closed) reconnect();
    });
  }

  async function reconnect() {
    onStatus('reconnecting');
    retry += 1;
    await new Promise((r) => setTimeout(r, Math.min(1000 * retry, 8000)));
    try {
      const missed = await get(`/api/flows?after=${lastId}`);
      missed.flows.forEach(deliver);
      lastId = Math.max(lastId, missed.last_event_id);
      connect();
    } catch (err) {
      if (err instanceof HttpError && err.status === 401) {
        closed = true;
        onStatus('expired');
        return;
      }
      reconnect();   // the server isn't back yet: keep trying, a little slower each time
    }
  }

  connect();
  return {
    close() { closed = true; source?.close(); },
    get lastId() { return lastId; },
  };
}
