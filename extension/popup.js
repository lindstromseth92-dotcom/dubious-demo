// Dubious popup: one button to scan the active tab, plus live status.
// The scan itself runs in the page's content script, so it carries on
// after this popup closes.

const DEFAULT_THRESHOLD = 40;
const $ = (id) => document.getElementById(id);

const scanBtn = $('scan');
const statusEl = $('status');
const linksEl = $('links');
const reportEl = $('report');
const clearEl = $('clear');
const thresholdEl = $('threshold');
const thresholdOut = $('thresholdOut');
const backendEl = $('backend');

let tabId = null;
let pollTimer = 0;
let backendUp = false;

function setStatus(text, isError = false) {
  statusEl.textContent = text;
  statusEl.classList.toggle('err', isError);
}

function backendCall(path) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'dubious:api', method: 'GET', path }, (res) => {
      if (chrome.runtime.lastError) resolve({ ok: false, error: chrome.runtime.lastError.message });
      else resolve(res || { ok: false, error: 'No response' });
    });
  });
}

function askTab(type) {
  return new Promise((resolve) => {
    if (tabId == null) return resolve(null);
    chrome.tabs.sendMessage(tabId, { type }, (res) => {
      if (chrome.runtime.lastError) resolve(null); // content script not injected yet
      else resolve(res || null);
    });
  });
}

function render(s) {
  const busy = s && ['collecting', 'extracting', 'scoring'].includes(s.status);
  scanBtn.disabled = busy || !backendUp || tabId == null;
  scanBtn.textContent = busy ? 'Scanning…' : s && s.status !== 'idle' ? 'Scan again' : 'Scan this page';
  linksEl.hidden = !(s && s.status === 'done' && s.total > 0);
  if (!linksEl.hidden && s.reportUrl) reportEl.href = `${s.reportUrl}?t=${encodeURIComponent(s.threshold)}`;

  if (!s || s.status === 'idle') {
    if (backendUp) setStatus('');
    return;
  }
  switch (s.status) {
    case 'collecting':
      setStatus('Reading the page…');
      break;
    case 'extracting':
      setStatus('Finding claims…');
      break;
    case 'scoring':
      setStatus(`Checking claims: ${s.scored} of ${s.total}` + (s.flagged ? ` · ${s.flagged} dubious so far` : ''));
      break;
    case 'done': {
      const tail = (s.failed ? ` ${s.failed} could not be checked.` : '') + (s.truncated ? ' Long page: only the first part was checked.' : '');
      if (!s.total) setStatus(`No checkable claims found on this page.${s.truncated ? ' Long page: only the first part was read.' : ''}`);
      else
      if (s.flagged) setStatus(`${s.flagged} of ${s.total} claims look dubious.${tail}`);
      else if (s.failed) setStatus(`Checked ${s.total - s.failed} of ${s.total} claims; none scored ${s.threshold}% or higher.${tail}`, true);
      else setStatus(`Checked ${s.total} claims. None scored ${s.threshold}% or higher.${tail}`);
      break;
    }
    default:
      setStatus(s.error || 'Something went wrong.', true);
  }
}

async function refresh() {
  render(await askTab('dubious:status'));
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 1000);
}

async function scan() {
  if (tabId == null) return;
  scanBtn.disabled = true;
  setStatus('Starting…');
  try {
    await chrome.scripting.insertCSS({ target: { tabId }, files: ['content.css'] });
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
  } catch (_) {
    setStatus("This page can't be scanned. Browser pages, the Web Store and PDFs are off limits to extensions.", true);
    scanBtn.disabled = false;
    return;
  }
  const res = await askTab('dubious:start');
  if (!res) {
    setStatus("Couldn't reach the page. Reload it and try again.", true);
    scanBtn.disabled = false;
    return;
  }
  render(res);
  startPolling();
}

async function init() {
  const stored = await chrome.storage.sync.get({ threshold: DEFAULT_THRESHOLD });
  thresholdEl.value = stored.threshold;
  thresholdOut.textContent = stored.threshold;

  // popup.html?tabId=N targets a specific tab; used by the end-to-end test,
  // which opens the popup as an ordinary page.
  const forced = Number(new URLSearchParams(location.search).get('tabId'));
  if (Number.isInteger(forced) && forced > 0) {
    tabId = forced;
  } else {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    tabId = tab ? tab.id : null;
  }

  const health = await backendCall('/health');
  backendUp = Boolean(health.ok);
  backendEl.replaceChildren();
  if (backendUp) {
    backendEl.classList.remove('down');
    backendEl.textContent = `Backend connected · ${health.data.model || 'model unknown'}`;
  } else {
    backendEl.classList.add('down');
    const code = document.createElement('code');
    code.textContent = './scripts/run.sh';
    backendEl.append('Backend not running. In the project folder, run ', code, ' and reopen this popup.');
  }

  await refresh();
  startPolling();
}

scanBtn.addEventListener('click', scan);

clearEl.addEventListener('click', async () => {
  render(await askTab('dubious:clear'));
});

thresholdEl.addEventListener('input', () => {
  thresholdOut.textContent = thresholdEl.value;
});

thresholdEl.addEventListener('change', () => {
  chrome.storage.sync.set({ threshold: Number(thresholdEl.value) });
});

init();
