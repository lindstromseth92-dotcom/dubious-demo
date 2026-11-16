// Dubious content script.
//
// Injected on demand by the popup (activeTab + scripting), never automatically.
// It does three things:
//   1. Reads the page into ordered text "runs" (a run = a stretch of inline text
//      inside one block element), and sends them to the backend for analysis.
//   2. Wraps each claim the backend finds in <dubious-mark>, and paints it as
//      pending / flagged / clean as verdicts arrive.
//   3. Shows a hover card with the score and a link to the full explanation.
//
// All strings that come back from the backend are inserted with textContent,
// never innerHTML.

(() => {
  'use strict';
  if (window.__dubiousLens) return; // already injected in this page
  window.__dubiousLens = true;

  const MAX_RUNS = 160;
  const MAX_CHARS = 32000;
  const POLL_MS = 1500;
  const MAX_POLL_FAILURES = 5;
  const DEFAULT_THRESHOLD = 40;
  // Limits of the backend's request schema; anything longer is clipped or dropped here.
  const MAX_BLOCK_CHARS = 20000;
  const MAX_HREF_CHARS = 2000;
  const MAX_FOOTNOTE_CHARS = 1500;

  const SKIP_TAGS = new Set([
    'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'CANVAS', 'IFRAME', 'OBJECT', 'EMBED', 'VIDEO',
    'AUDIO', 'NAV', 'FOOTER', 'ASIDE', 'BUTTON', 'SELECT', 'TEXTAREA', 'INPUT', 'LABEL', 'CODE',
    'PRE', 'DUBIOUS-UI', 'MATH', 'MENU', 'DIALOG',
  ]);
  const SKIP_SELECTOR =
    '[aria-hidden="true"], [hidden], [role="navigation"], [role="search"], [role="dialog"], [role="alertdialog"], ' +
    '[role="banner"] nav, #comments, .comments, .comment-list, #disqus_thread, .sharedaddy, .share-buttons';
  const HEADING_TAGS = new Set(['H1', 'H2', 'H3', 'H4']);

  const state = {
    token: 0, // bumps on every start/teardown; stale async work checks it and stops
    scanId: null,
    status: 'idle', // idle | collecting | extracting | scoring | done | error
    error: null,
    threshold: DEFAULT_THRESHOLD,
    total: 0,
    scored: 0,
    reportUrl: null,
    truncated: false,
    runs: new Map(), // run id -> { id, el, k, text, links }
    claims: new Map(), // claim id -> { data, marks, state }
  };

  // ---------------------------------------------------------------- backend

  function api(method, path, body) {
    return new Promise((resolve, reject) => {
      try {
        chrome.runtime.sendMessage({ type: 'dubious:api', method, path, body }, (res) => {
          if (chrome.runtime.lastError) {
            reject(new Error('Dubious was reloaded. Refresh this page and scan again.'));
          } else if (!res || !res.ok) {
            reject(new Error((res && res.error) || 'The Dubious backend did not answer.'));
          } else {
            resolve(res.data);
          }
        });
      } catch (_) {
        reject(new Error('Dubious was reloaded. Refresh this page and scan again.'));
      }
    });
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const collapse = (s) => s.replace(/\s+/g, ' ').trim();

  // ------------------------------------------------------------ page reading

  function isHidden(cs) {
    return cs.display === 'none' || cs.visibility === 'hidden' || cs.visibility === 'collapse';
  }

  function isInline(cs) {
    return cs.display.startsWith('inline') || cs.display === 'contents' || cs.display.startsWith('ruby');
  }

  // Walk one block element and push its text runs to `out` in document order.
  // A run ends wherever a nested block element starts. With recurse=false the
  // nested blocks are skipped, which gives the element's own runs only; that
  // is how a run is re-read after the DOM has been split by earlier marks.
  //
  // The walk must be deterministic under text-node splitting, so it never
  // skips a text node based on its content.
  function scanBlock(blockEl, out, recurse) {
    let cur = null;
    let k = 0;
    const visit = (node) => {
      for (let child = node.firstChild; child; child = child.nextSibling) {
        if (child.nodeType === Node.TEXT_NODE) {
          const value = child.nodeValue;
          if (!value) continue;
          if (!cur) {
            cur = { el: blockEl, k: k++, segs: [], text: '' };
            out.push(cur);
          }
          cur.segs.push({ node: child, start: cur.text.length, end: cur.text.length + value.length });
          cur.text += value;
        } else if (child.nodeType === Node.ELEMENT_NODE) {
          const tag = child.tagName.toUpperCase();
          if (SKIP_TAGS.has(tag)) continue;
          if (tag === 'BR') {
            // One <br> is a line break inside the run (a virtual character with
            // no text node behind it). A second <br> in a row is a paragraph
            // break, which is how older pages separate paragraphs: end the run.
            if (cur) {
              // brAt marks where the text after the previous <br> starts. Only blank
              // text between two <br>s makes a paragraph break; a newline in the HTML
              // source before a lone <br> does not.
              if (cur.brAt !== undefined && !/\S/.test(cur.text.slice(cur.brAt))) {
                cur = null;
              } else {
                cur.text += '\n';
                cur.brAt = cur.text.length;
              }
            }
            continue;
          }
          if (child.matches(SKIP_SELECTOR)) continue;
          const cs = getComputedStyle(child);
          if (isHidden(cs)) continue;
          if (isInline(cs)) {
            visit(child);
          } else {
            cur = null;
            if (recurse) scanBlock(child, out, true);
          }
        }
      }
    };
    visit(blockEl);
  }

  // Prefer the article body when the page marks one up; otherwise the whole body.
  function chooseRoot() {
    const body = document.body;
    if (!body) return null;
    const total = (body.innerText || '').length || 1;
    let best = null;
    let bestLen = 0;
    for (const el of document.querySelectorAll('article, main, [role="main"]')) {
      const len = (el.innerText || '').length;
      if (len > bestLen) {
        best = el;
        bestLen = len;
      }
    }
    return best && (bestLen >= 1500 || bestLen / total >= 0.5) ? best : body;
  }

  function samePage(a) {
    return a.origin === location.origin && a.pathname === location.pathname && a.search === location.search;
  }

  // What a same-page anchor points at, if it is a footnote: its text and the
  // external links inside it. Returns null for jumps to headings or sections,
  // and for targets whose "box" would be most of the page.
  function footnoteFor(target, citingEl) {
    if (target.closest('h1, h2, h3, h4, h5, h6')) return null;
    const isLink = (x) => (x.protocol === 'http:' || x.protocol === 'https:') && !samePage(x) && x.href.length <= MAX_HREF_CHARS;
    let box = target.matches('li, p, dd, tr, td, div') ? target : target.closest('li, p, dd');
    if (box && (box.contains(citingEl) || (box.textContent || '').length > MAX_FOOTNOTE_CHARS)) box = null;
    if (box) {
      const note = collapse(box.textContent || '').slice(0, 300);
      return note ? { note, links: [...box.querySelectorAll('a[href]')].filter(isLink).slice(0, 3) } : null;
    }
    // A bare <a name="fn1"> with the footnote text after it: read up to the next
    // line break or the next anchor target.
    let text = '';
    const links = [];
    for (let n = target.nextSibling; n && text.length < 300; n = n.nextSibling) {
      if (n.nodeType === Node.ELEMENT_NODE) {
        if (n.tagName === 'BR' || n.id || n.getAttribute('name') || !isInline(getComputedStyle(n))) break;
        if (n.matches('a[href]') && isLink(n)) links.push(n);
        links.push(...[...n.querySelectorAll('a[href]')].filter(isLink));
      }
      text += n.textContent || '';
    }
    const note = collapse((target.textContent || '') + ' ' + text).slice(0, 300);
    return note.length >= 8 ? { note, links: links.slice(0, 3) } : null;
  }

  // Links inside a run are what the page offers as sourcing. Same-page anchors
  // are treated as footnotes: follow them and report what the footnote cites.
  function linksFor(run) {
    // Where each anchor's text ends inside the run, so the backend can tell
    // which sentence a citation is attached to.
    const anchors = new Map();
    for (const seg of run.segs) {
      const parent = seg.node.parentElement;
      const a = parent && parent.closest('a[href]');
      if (a && run.el.contains(a)) anchors.set(a, seg.end);
    }
    const out = [];
    for (const [a, at] of anchors) {
      if (out.length >= 8) break;
      const text = collapse(a.textContent || '').slice(0, 120);
      if (a.protocol !== 'http:' && a.protocol !== 'https:') continue;
      if (samePage(a)) {
        if (!a.hash || a.hash.length < 2) continue;
        let id = a.hash.slice(1);
        try {
          id = decodeURIComponent(id);
        } catch (_) {
          /* keep raw */
        }
        const target = document.getElementById(id) || document.getElementsByName(id)[0];
        if (!target) continue;
        const foot = footnoteFor(target, run.el);
        if (!foot) continue; // a jump to a heading or section, not a citation
        const note = foot.note;
        const cited = foot.links;
        if (cited.length) {
          for (const x of cited) out.push({ text, href: x.href, note, at });
        } else {
          out.push({ text, href: null, note, at });
        }
      } else if (a.href.length <= MAX_HREF_CHARS) {
        // An over-long URL is dropped, not cut: a truncated URL is a different URL.
        out.push({ text, href: a.href, at });
      }
    }
    return out.slice(0, 8);
  }

  // True when nearly all of a run's text sits inside links. Real prose links a
  // few words; a run that is one big link is navigation or a teaser headline.
  function mostlyLink(run) {
    let total = 0;
    let linked = 0;
    for (const seg of run.segs) {
      const n = seg.node.nodeValue.replace(/\s+/g, '').length;
      total += n;
      const parent = seg.node.parentElement;
      if (parent && parent.closest('a[href]') && run.el.contains(parent.closest('a[href]'))) linked += n;
      else if (parent && run.el.closest('a[href]')) linked += n; // the whole block is inside a link
    }
    return total > 0 && linked / total >= 0.8;
  }

  function collectRuns() {
    const root = chooseRoot();
    if (!root) return [];
    const all = [];
    scanBlock(root, all, true);
    const runs = [];
    let chars = 0;
    state.truncated = false;
    for (const run of all) {
      const flat = collapse(run.text);
      const words = flat ? flat.split(' ').length : 0;
      const heading = HEADING_TAGS.has(run.el.tagName);
      if (heading ? flat.length < 12 || words < 3 : flat.length < 40 || words < 6) continue;
      if (run.el.getClientRects().length === 0) continue;
      if (mostlyLink(run)) continue; // headline links, menus, "related articles"
      if (runs.length >= MAX_RUNS || chars + flat.length > MAX_CHARS) {
        state.truncated = true;
        break;
      }
      chars += flat.length;
      runs.push({ id: 'b' + runs.length, el: run.el, k: run.k, text: run.text, links: linksFor(run) });
    }
    return runs;
  }

  // ---------------------------------------------------------------- marking

  function escapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  // Find a quote in text, tolerating any difference in whitespace.
  function flexFind(text, quote) {
    const tokens = collapse(quote).split(' ').filter(Boolean);
    if (!tokens.length) return null;
    let m = null;
    try {
      m = new RegExp(tokens.map(escapeRe).join('\\s+')).exec(text);
    } catch (_) {
      return null;
    }
    return m ? [m.index, m.index + m[0].length] : null;
  }

  function wrapClaim(claim) {
    const run = state.runs.get(claim.block_id);
    if (!run || !run.el.isConnected) return [];
    const own = [];
    scanBlock(run.el, own, false);
    const fresh = own[run.k];
    if (!fresh) return [];

    let start = claim.start;
    let end = claim.end;
    const offsetsOk =
      fresh.text === run.text &&
      Number.isInteger(start) &&
      Number.isInteger(end) &&
      start >= 0 &&
      end > start &&
      fresh.text.slice(start, end) === claim.quote;
    if (!offsetsOk) {
      // The page changed under us since it was read; fall back to searching.
      const found = flexFind(fresh.text, claim.quote || '');
      if (!found) return [];
      [start, end] = found;
    }

    const marks = [];
    for (const seg of fresh.segs) {
      const a = Math.max(start, seg.start);
      const b = Math.min(end, seg.end);
      if (a >= b) continue;
      const piece = seg.node.nodeValue.slice(a - seg.start, b - seg.start);
      if (!/\S/.test(piece)) continue;
      if (seg.node.parentElement && seg.node.parentElement.closest('dubious-mark')) continue; // no nesting
      try {
        const range = document.createRange();
        range.setStart(seg.node, a - seg.start);
        range.setEnd(seg.node, b - seg.start);
        const mark = document.createElement('dubious-mark');
        mark.dataset.claim = claim.id;
        range.surroundContents(mark);
        marks.push(mark);
      } catch (_) {
        /* node vanished mid-wrap; skip this piece */
      }
    }
    return marks;
  }

  function bandOf(score) {
    if (score >= 80) return 'severe';
    if (score >= 60) return 'high';
    return 'medium';
  }

  function claimState(c) {
    if (c.status === 'pending') return 'pending';
    if (c.status === 'scored' && typeof c.score === 'number' && c.score >= state.threshold) return 'flagged';
    return 'clean';
  }

  function paint(entry) {
    const c = entry.data;
    const st = claimState(c);
    entry.state = st;
    entry.marks.forEach((m, i) => {
      m.dataset.state = st;
      if (st === 'flagged') m.dataset.band = bandOf(c.score);
      else delete m.dataset.band;
      if (st === 'flagged' && i === 0) {
        m.tabIndex = 0;
        m.setAttribute('role', 'button');
        m.setAttribute('aria-label', `Dubious claim, score ${Math.round(c.score)} percent. Press Enter for details.`);
      } else {
        m.removeAttribute('tabindex');
        m.removeAttribute('role');
        m.removeAttribute('aria-label');
      }
    });
    if (st === 'clean' && ui.activeClaim === c.id) hideTip(true);
    else if (ui.activeClaim === c.id) fillTip(entry); // verdict arrived while hovering
  }

  function applyScan(scan) {
    state.status = scan.status;
    state.error = scan.error || null;
    state.total = scan.total_claims || 0;
    state.scored = scan.scored_claims || 0;
    state.reportUrl = scan.report_url || null;
    for (const c of scan.claims || []) {
      const sig = `${c.status}|${c.score}|${c.verdict_label}|${c.headline}`;
      let entry = state.claims.get(c.id);
      if (!entry) {
        entry = { data: c, marks: wrapClaim(c), state: 'pending', sig: null };
        state.claims.set(c.id, entry);
      }
      if (entry.sig === sig) continue; // nothing new for this claim since the last poll
      entry.sig = sig;
      entry.data = c;
      paint(entry);
    }
    renderPill();
  }

  function flaggedCount() {
    let n = 0;
    for (const e of state.claims.values()) if (e.state === 'flagged') n++;
    return n;
  }

  // Claims whose check failed. They are not underlined, but they did not pass either.
  function failedCount() {
    let n = 0;
    for (const e of state.claims.values()) if (e.data.status === 'error') n++;
    return n;
  }

  // Report links carry the threshold in force here, so the report agrees with the underlines.
  function reportLink(url) {
    return `${url}?t=${encodeURIComponent(state.threshold)}`;
  }

  function unwrapAll() {
    const parents = new Set();
    for (const m of document.querySelectorAll('dubious-mark')) {
      const p = m.parentNode;
      if (!p) continue;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      parents.add(p);
    }
    for (const p of parents) p.normalize();
  }

  // ------------------------------------------------------------- scan flow

  function teardown() {
    state.token++;
    hideTip(true);
    unwrapAll();
    state.scanId = null;
    state.status = 'idle';
    state.error = null;
    state.total = 0;
    state.scored = 0;
    state.reportUrl = null;
    ui.pillDismissedAt = null;
    state.runs.clear();
    state.claims.clear();
    renderPill();
  }

  function fail(message) {
    state.status = 'error';
    state.error = message;
    renderPill();
  }

  async function start() {
    teardown();
    const token = state.token;
    state.status = 'collecting';
    renderPill();
    try {
      const stored = await chrome.storage.sync.get({ threshold: DEFAULT_THRESHOLD });
      state.threshold = Number(stored.threshold) || DEFAULT_THRESHOLD;
    } catch (_) {
      state.threshold = DEFAULT_THRESHOLD;
    }
    if (token !== state.token) return;

    const runs = collectRuns();
    if (!runs.length) {
      fail("Couldn't find any article text to check on this page.");
      return;
    }
    for (const r of runs) state.runs.set(r.id, r);
    state.status = 'extracting';
    renderPill();

    try {
      const res = await api('POST', '/api/scan', {
        url: location.href.slice(0, 4000),
        title: (document.title || '').slice(0, 1000),
        truncated: state.truncated,
        threshold: state.threshold,
        blocks: runs.map((r) => ({
          id: r.id,
          tag: r.el.tagName.toLowerCase().slice(0, 16),
          text: r.text.slice(0, MAX_BLOCK_CHARS),
          links: r.links.filter((l) => l.at <= MAX_BLOCK_CHARS),
        })),
      });
      if (token !== state.token) return;
      state.scanId = res.scan_id;
    } catch (err) {
      if (token === state.token) fail(err.message);
      return;
    }

    let failures = 0;
    while (token === state.token) {
      try {
        const scan = await api('GET', `/api/scan/${encodeURIComponent(state.scanId)}`);
        if (token !== state.token) return;
        failures = 0;
        applyScan(scan);
        if (scan.status === 'done' || scan.status === 'error') return;
      } catch (err) {
        if (token !== state.token) return;
        if (++failures >= MAX_POLL_FAILURES) {
          fail(err.message);
          return;
        }
      }
      await sleep(POLL_MS);
    }
  }

  function summary() {
    return {
      status: state.status,
      error: state.error,
      total: state.total,
      scored: state.scored,
      flagged: flaggedCount(),
      failed: failedCount(),
      threshold: state.threshold,
      reportUrl: state.reportUrl,
      truncated: state.truncated,
    };
  }

  // -------------------------------------------------------------------- UI

  const ui = {
    host: null, root: null, tip: null, pill: null, activeClaim: null, pinned: false, hideTimer: 0, switchTimer: 0,
    anchor: null, pillDismissedAt: null,
  };

  const UI_CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; }
    .tip, .pill {
      --bg: #ffffff; --fg: #14171c; --muted: #5b6472; --line: #e3e6ea; --chip: #f1f3f5; --link: #2f54c9;
      font: 13px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      color: var(--fg); background: var(--bg); border: 1px solid var(--line);
      box-shadow: 0 6px 24px rgba(15, 20, 30, 0.18), 0 1px 3px rgba(15, 20, 30, 0.12);
      position: fixed; z-index: 2147483647;
    }
    @media (prefers-color-scheme: dark) {
      .tip, .pill { --bg: #1d2026; --fg: #eef0f3; --muted: #a3acb9; --line: #343943; --chip: #2a2e36; --link: #8fa8ff; }
    }
    .tip { width: 340px; max-width: calc(100vw - 16px); border-radius: 10px; padding: 12px 14px; display: none; }
    .tip.on { display: block; }
    .head { display: flex; align-items: center; gap: 10px; }
    .score {
      flex: none; min-width: 54px; text-align: center; padding: 5px 8px; border-radius: 8px;
      font-size: 18px; font-weight: 700; line-height: 1.1; color: #fff; font-variant-numeric: tabular-nums;
    }
    .score.medium { background: #b87200; } .score.high { background: #d14e08; } .score.severe { background: #cc2a2a; }
    .score.pending { background: #7b8492; font-size: 13px; }
    .kicker { font-size: 11px; letter-spacing: 0.04em; text-transform: uppercase; color: var(--muted); }
    .verdict { font-weight: 650; }
    .headline { margin: 10px 0 0; }
    .reasons { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
    .reasons li { background: var(--chip); border-radius: 999px; padding: 2px 9px; font-size: 12px; color: var(--fg); }
    .more {
      display: inline-block; margin-top: 12px; color: var(--link); font-weight: 600; text-decoration: none;
      border-radius: 4px;
    }
    .more:hover { text-decoration: underline; }
    .more:focus-visible, .x:focus-visible, .report:focus-visible { outline: 2px solid #4c6ef5; outline-offset: 2px; }
    .pill {
      right: 16px; bottom: 16px; border-radius: 999px; padding: 8px 10px 8px 14px;
      display: none; align-items: center; gap: 10px; max-width: min(520px, calc(100vw - 32px));
    }
    .pill.on { display: flex; }
    .dot { flex: none; width: 9px; height: 9px; border-radius: 50%; background: #7b8492; }
    .pill.busy .dot { background: #4c6ef5; animation: pulse 1.1s ease-in-out infinite; }
    .pill.flag .dot { background: #cc2a2a; } .pill.ok .dot { background: #2f9e44; } .pill.err .dot { background: #cc2a2a; }
    @keyframes pulse { 50% { opacity: 0.35; } }
    @media (prefers-reduced-motion: reduce) { .pill.busy .dot { animation: none; } }
    .msg { min-width: 0; }
    .brand { font-weight: 700; margin-right: 4px; }
    .report { flex: none; color: var(--link); font-weight: 600; text-decoration: none; white-space: nowrap; border-radius: 4px; }
    .report:hover { text-decoration: underline; }
    .x {
      flex: none; all: unset; cursor: pointer; width: 24px; height: 24px; border-radius: 50%; text-align: center;
      font: 16px/24px system-ui, sans-serif; color: var(--muted);
    }
    .x:hover { background: var(--chip); color: var(--fg); }
  `;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function ensureUi() {
    if (ui.host && ui.host.isConnected) return;
    ui.host = document.createElement('dubious-ui');
    ui.root = ui.host.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = UI_CSS;
    ui.tip = el('div', 'tip');
    ui.tip.setAttribute('role', 'dialog');
    ui.tip.setAttribute('aria-label', 'Dubious claim details');
    ui.tip.addEventListener('mouseenter', () => {
      cancelHide();
      cancelSwitch();
    });
    ui.tip.addEventListener('mouseleave', () => {
      if (!ui.pinned) scheduleHide();
    });
    ui.pill = el('div', 'pill');
    ui.pill.setAttribute('role', 'status');
    ui.pill.setAttribute('aria-live', 'polite');
    ui.root.append(style, ui.tip, ui.pill);
    document.documentElement.appendChild(ui.host);
  }

  function renderPill() {
    if (state.status === 'idle') {
      if (ui.pill) ui.pill.className = 'pill';
      return;
    }
    ensureUi();
    if (ui.pillDismissedAt === state.status) return; // dismissed; come back when the scan reaches a new stage
    ui.pillDismissedAt = null;
    const flagged = flaggedCount();
    let cls = 'busy';
    let text = '';
    switch (state.status) {
      case 'collecting':
        text = 'Reading the page…';
        break;
      case 'extracting':
        text = 'Finding claims…';
        break;
      case 'scoring':
        text = `Checking claims: ${state.scored} of ${state.total}` + (flagged ? ` · ${flagged} dubious so far` : '');
        break;
      case 'done': {
        const failed = failedCount();
        const tail = failed ? ` ${failed} could not be checked.` : '';
        if (!state.total) {
          cls = 'ok';
          text = 'No checkable claims found on this page.';
        } else if (flagged) {
          cls = 'flag';
          text = `${flagged} of ${state.total} claims look dubious. Hover an underline for details.${tail}`;
        } else if (failed) {
          cls = 'err';
          text = `Checked ${state.total - failed} of ${state.total} claims; none scored ${state.threshold}% or higher.${tail}`;
        } else {
          cls = 'ok';
          text = `Checked ${state.total} claims. None scored ${state.threshold}% or higher.`;
        }
        if (state.truncated) text += ' Long page: only the first part was checked.';
        break;
      }
      default:
        cls = 'err';
        text = state.error || 'Something went wrong.';
    }
    ui.pill.className = `pill on ${cls}`;
    ui.pill.replaceChildren();
    const msg = el('span', 'msg');
    msg.append(el('span', 'brand', 'Dubious'), document.createTextNode(text));
    ui.pill.append(el('span', 'dot'), msg);
    if (state.reportUrl && state.status === 'done' && state.total) {
      const a = el('a', 'report', 'Full report');
      a.href = reportLink(state.reportUrl);
      a.target = '_blank';
      a.rel = 'noopener';
      ui.pill.append(a);
    }
    const x = el('button', 'x', '×');
    x.setAttribute('aria-label', 'Dismiss');
    x.addEventListener('click', () => {
      ui.pillDismissedAt = state.status;
      ui.pill.className = 'pill';
    });
    ui.pill.append(x);
  }

  function setActive(claimId) {
    if (ui.activeClaim && ui.activeClaim !== claimId) {
      const prev = state.claims.get(ui.activeClaim);
      if (prev) prev.marks.forEach((m) => m.removeAttribute('data-active'));
    }
    ui.activeClaim = claimId;
    const cur = claimId && state.claims.get(claimId);
    if (cur) cur.marks.forEach((m) => m.setAttribute('data-active', ''));
  }

  function fillTip(entry) {
    const c = entry.data;
    ui.tip.replaceChildren();
    const head = el('div', 'head');
    const words = el('div');
    if (entry.state === 'pending') {
      head.append(el('span', 'score pending', '…'));
      words.append(el('div', 'kicker', 'Dubious'), el('div', 'verdict', 'Checking this claim'));
      head.append(words);
      ui.tip.append(head, el('p', 'headline', 'Looking for evidence and checking what the page cites.'));
      return;
    }
    head.append(el('span', `score ${bandOf(c.score)}`, `${Math.round(c.score)}%`));
    words.append(el('div', 'kicker', 'Dubious score'), el('div', 'verdict', c.verdict_label || 'Dubious claim'));
    head.append(words);
    ui.tip.append(head);
    if (c.headline) ui.tip.append(el('p', 'headline', c.headline));
    if (Array.isArray(c.reasons) && c.reasons.length) {
      const ul = el('ul', 'reasons');
      for (const r of c.reasons.slice(0, 4)) ul.append(el('li', null, r.label));
      ui.tip.append(ul);
    }
    if (c.report_url) {
      const a = el('a', 'more', 'See why this was flagged →');
      a.href = reportLink(c.report_url);
      a.target = '_blank';
      a.rel = 'noopener';
      ui.tip.append(a);
    }
  }

  function placeTip(mark, x, y) {
    const rects = [...mark.getClientRects()];
    if (!rects.length) return;
    const rect =
      (x != null && rects.find((r) => x >= r.left - 2 && x <= r.right + 2 && y >= r.top - 2 && y <= r.bottom + 2)) || rects[0];
    const vw = document.documentElement.clientWidth;
    const vh = window.innerHeight;
    ui.tip.style.left = '0px';
    ui.tip.style.top = '0px';
    const w = ui.tip.offsetWidth;
    const h = ui.tip.offsetHeight;
    const anchorX = x != null ? x - 24 : rect.left;
    const left = Math.max(8, Math.min(anchorX, vw - w - 8));
    let top = rect.bottom + 8;
    if (top + h > vh - 8 && rect.top - h - 8 >= 8) top = rect.top - h - 8;
    ui.tip.style.left = `${Math.round(left)}px`;
    ui.tip.style.top = `${Math.round(top)}px`;
  }

  function showTip(mark, x, y) {
    const entry = state.claims.get(mark.dataset.claim);
    if (!entry || entry.state === 'clean') return;
    ensureUi();
    cancelHide();
    const same = ui.activeClaim === mark.dataset.claim && ui.tip.classList.contains('on');
    setActive(mark.dataset.claim);
    ui.anchor = mark;
    if (!same) {
      fillTip(entry);
      ui.tip.classList.add('on');
      placeTip(mark, x, y);
    }
  }

  function hideTip(force) {
    cancelHide();
    cancelSwitch();
    if (!ui.tip) return;
    if (ui.pinned && !force) return;
    ui.pinned = false;
    ui.anchor = null;
    ui.tip.classList.remove('on');
    setActive(null);
  }

  function scheduleHide() {
    cancelHide();
    ui.hideTimer = setTimeout(() => hideTip(false), 220);
  }

  function cancelHide() {
    if (ui.hideTimer) clearTimeout(ui.hideTimer);
    ui.hideTimer = 0;
  }

  function cancelSwitch() {
    if (ui.switchTimer) clearTimeout(ui.switchTimer);
    ui.switchTimer = 0;
  }

  function markFrom(target) {
    const node = target instanceof Element ? target : target && target.parentElement;
    const mark = node && node.closest ? node.closest('dubious-mark') : null;
    return mark && mark.dataset.state !== 'clean' ? mark : null;
  }

  document.addEventListener(
    'mouseover',
    (e) => {
      const mark = markFrom(e.target);
      if (!mark || (ui.pinned && ui.activeClaim !== mark.dataset.claim)) return;
      const showing = ui.tip && ui.tip.classList.contains('on');
      if (showing && ui.activeClaim && ui.activeClaim !== mark.dataset.claim) {
        // Another claim's card is open. The pointer may just be crossing this
        // claim on its way to that card, so switch only if it lingers here.
        cancelSwitch();
        const { clientX, clientY } = e;
        ui.switchTimer = setTimeout(() => {
          ui.switchTimer = 0;
          if (mark.isConnected && !ui.pinned) showTip(mark, clientX, clientY);
        }, 220);
        cancelHide();
        return;
      }
      cancelSwitch();
      showTip(mark, e.clientX, e.clientY);
    },
    true,
  );

  document.addEventListener(
    'mouseout',
    (e) => {
      if (!markFrom(e.target)) return;
      cancelSwitch();
      if (!ui.pinned) scheduleHide();
    },
    true,
  );

  document.addEventListener(
    'click',
    (e) => {
      if (ui.host && e.target === ui.host) return; // click inside the hover card
      const mark = markFrom(e.target);
      if (mark) {
        // Pin the card so it can be reached by touch or a slow mouse.
        // If the text is itself a link, let the link work as normal.
        cancelSwitch();
        if (ui.activeClaim !== mark.dataset.claim) hideTip(true);
        showTip(mark, e.clientX, e.clientY);
        ui.pinned = true;
      } else if (ui.pinned) {
        hideTip(true);
      }
    },
    true,
  );

  document.addEventListener(
    'keydown',
    (e) => {
      if (e.key === 'Escape' && ui.tip && ui.tip.classList.contains('on')) {
        const back = ui.anchor;
        hideTip(true);
        if (back && back.isConnected && back.tabIndex === 0) back.focus();
        return;
      }
      if ((e.key === 'Enter' || e.key === ' ') && e.target instanceof Element && e.target.matches('dubious-mark[tabindex]')) {
        e.preventDefault();
        showTip(e.target, null, null);
        ui.pinned = true;
        const link = ui.tip.querySelector('.more');
        if (link) link.focus();
      }
    },
    true,
  );

  window.addEventListener(
    'scroll',
    () => {
      if (!ui.tip || !ui.tip.classList.contains('on')) return;
      if (ui.pinned && ui.anchor && ui.anchor.isConnected) placeTip(ui.anchor, null, null);
      else hideTip(true);
    },
    { passive: true, capture: true },
  );

  // ------------------------------------------------------- popup messaging

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || typeof msg.type !== 'string') return undefined;
    if (msg.type === 'dubious:start') {
      start();
      sendResponse(summary());
    } else if (msg.type === 'dubious:status') {
      sendResponse(summary());
    } else if (msg.type === 'dubious:clear') {
      teardown();
      sendResponse(summary());
    }
    return undefined;
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'sync' || !changes.threshold) return;
    state.threshold = Number(changes.threshold.newValue) || DEFAULT_THRESHOLD;
    for (const entry of state.claims.values()) paint(entry);
    renderPill();
  });
})();
