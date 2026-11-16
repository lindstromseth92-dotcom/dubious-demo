// Dubious background service worker.
//
// Its only job is to be a narrow fetch proxy to the local Dubious backend.
// Content scripts cannot call http://localhost from an https page without
// tripping CORS / local-network checks, but the service worker can, because
// the manifest grants it host permission for the backend origin.
//
// Requests are short (start a scan, poll a scan), so the worker is never
// asked to hold a connection open across its 30 second idle limit.

const BACKEND = 'http://localhost:8787';
const ALLOWED_PATH = /^\/(health$|api\/)/;
const TIMEOUT_MS = 20000;

function friendlyError(err) {
  const msg = String(err && err.message ? err.message : err);
  if (err && (err.name === 'TimeoutError' || err.name === 'AbortError')) {
    return `The Dubious backend at ${BACKEND} took too long to answer.`;
  }
  if (/Failed to fetch|NetworkError|ERR_CONNECTION/i.test(msg)) {
    return `Can't reach the Dubious backend at ${BACKEND}. Start it with ./scripts/run.sh in the project folder.`;
  }
  return msg;
}

async function callBackend({ method = 'GET', path, body }) {
  if (typeof path !== 'string' || !ALLOWED_PATH.test(path)) {
    throw new Error('Path not allowed');
  }
  const res = await fetch(BACKEND + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    /* non-JSON body */
  }
  if (!res.ok) {
    // FastAPI reports validation failures as an array of objects; do not show that to the user.
    const detail = data && typeof data.detail === 'string' ? data.detail : '';
    if (res.status === 422) throw new Error(detail || 'The backend could not read this page (it sent something the scanner does not accept).');
    throw new Error(detail || `The backend answered with an error (HTTP ${res.status}).`);
  }
  return data;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== 'dubious:api') return undefined;
  // Only our own popup and content scripts may use the proxy.
  if (sender.id !== chrome.runtime.id) return undefined;
  callBackend(msg)
    .then((data) => sendResponse({ ok: true, data, backend: BACKEND }))
    .catch((err) => sendResponse({ ok: false, error: friendlyError(err), backend: BACKEND }));
  return true; // keep the channel open for the async response
});
