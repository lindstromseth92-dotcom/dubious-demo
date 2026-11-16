// Dubious report pages.
//   /report/{scan}          every claim found on a page, worst first
//   /report/{scan}/{claim}  the full reasoning behind one claim's score
// All content is rendered with textContent; URLs are only linked when http(s).

(() => {
  'use strict';

  const app = document.getElementById('app');
  const crumbs = document.getElementById('crumbs');
  const parts = location.pathname.split('/').filter(Boolean); // ["report", scan, claim?]
  const scanId = parts[1];
  const claimId = parts[2] || null;
  // ?t=NN is the flagging threshold in force in the extension when the link was
  // opened; it is passed on to the API and to every link between report pages.
  const tParam = new URLSearchParams(location.search).get('t') || '';
  const query = /^\d{1,3}$/.test(tParam) && Number(tParam) <= 100 ? `?t=${tParam}` : '';
  const scanHref = () => `/report/${encodeURIComponent(scanId)}${query}`;
  const claimHref = (id) => `/report/${encodeURIComponent(scanId)}/${encodeURIComponent(id)}${query}`;

  function h(tag, attrs, ...children) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('data-')) n.setAttribute(k, v === true ? '' : v);
      else n[k] = v;
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      n.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return n;
  }

  function safeUrl(url) {
    try {
      const u = new URL(url);
      return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null;
    } catch (_) {
      return null;
    }
  }

  function link(url, text, cls) {
    const href = safeUrl(url);
    if (!href) return h('span', { class: cls || 'name', text });
    return h('a', { href, target: '_blank', rel: 'noopener noreferrer', class: cls, text });
  }

  function host(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, '');
    } catch (_) {
      return '';
    }
  }

  function bandOf(score) {
    if (typeof score !== 'number') return 'none';
    if (score >= 80) return 'severe';
    if (score >= 60) return 'high';
    if (score >= 40) return 'medium';
    return 'low';
  }

  function fmtPoints(p) {
    const r = Math.round(p * 10) / 10;
    return (r > 0 ? '+' : r < 0 ? '−' : '') + Math.abs(r);
  }

  function section(title, name, ...children) {
    return h('section', { class: 'card', 'data-section': name }, h('h2', { text: title }), ...children);
  }

  async function getJson(path) {
    const res = await fetch(path, { headers: { Accept: 'application/json' } });
    if (!res.ok) throw new Error(res.status === 404 ? 'This report was not found. It may be from an older scan.' : `The backend answered ${res.status}.`);
    return res.json();
  }

  // ------------------------------------------------------------ claim page

  function hero(d) {
    const band = d.status === 'scored' ? bandOf(d.score) : 'none';
    const dial = h('div', { class: `dial band-${band}`, role: 'img', 'aria-label': d.status === 'scored' ? `Dubious score ${Math.round(d.score)} percent` : 'Not scored' },
      h('div', null, h('div', null, h('strong', { text: d.status === 'scored' ? `${Math.round(d.score)}%` : '–' }), h('span', { text: 'dubious' }))));
    dial.style.setProperty('--pct', d.status === 'scored' ? String(Math.max(0, Math.min(100, d.score))) : '0');
    const words = h('div', { class: `band-${band}` },
      h('p', { class: 'kicker', text: d.status === 'scored' ? (d.flagged ? 'Flagged as dubious' : 'Below the flagging threshold') : 'Not scored' }),
      h('h1', { text: d.status === 'skipped' ? ((d.reasons || [])[0] || {}).label || 'Not scored' : d.verdict_label || (d.status === 'pending' ? 'Still being checked' : 'Could not be assessed') }));
    const quote = h('blockquote', { class: `band-${band}`, text: `“${d.claim.quote}”` });
    const restated = d.claim.claim && d.claim.claim !== d.claim.quote
      ? h('p', { class: 'restated' }, h('span', { class: 'muted', text: 'The claim, restated: ' }), d.claim.claim)
      : null;
    const from = h('p', { class: 'from' }, 'From ', link(d.scan.url, d.scan.title || host(d.scan.url) || 'the scanned page'),
      host(d.scan.url) ? ` (${host(d.scan.url)})` : '', d.claim.speaker ? ` · attributed to ${d.claim.speaker}` : '');
    return h('section', { class: 'card', 'data-section': 'summary' }, h('div', { class: 'hero' }, dial, words), quote, restated, from);
  }

  function reasonsSection(d) {
    if (!d.headline && !(d.reasons || []).length) return null;
    // The headline is often the lead reason's own sentence; do not say it twice.
    const repeated = (d.reasons || []).some((r) => r.explanation && d.headline && r.explanation.includes(d.headline));
    return section(d.flagged ? 'Why this was flagged' : 'What the check found', 'reasons',
      d.headline && !repeated ? h('p', { text: d.headline }) : null,
      (d.reasons || []).length
        ? h('ul', { class: 'reasons' }, d.reasons.map((r) => h('li', null, h('strong', { text: r.label }), h('span', { class: 'muted', text: r.explanation || '' }))))
        : null);
  }

  function breakdownSection(d) {
    if (!(d.breakdown || []).length) return null;
    const rows = d.breakdown.map((b) =>
      h('tr', null,
        h('td', { class: `pts ${b.points > 0 ? 'up' : b.points < 0 ? 'down' : ''}`, text: fmtPoints(b.points) }),
        h('td', null, b.label, b.detail ? h('span', { class: 'detail', text: b.detail }) : null)));
    rows.push(h('tr', { class: 'total' }, h('td', { class: 'pts', text: String(Math.round(d.score)) }), h('td', { text: `Dubious score (flagged at ${d.threshold} or higher)` })));
    return section('How the score adds up', 'breakdown',
      h('p', { class: 'muted small', text: 'The model only labels what it finds. The score is computed from those labels by fixed rules, so the same findings always give the same score.' }),
      h('table', { class: 'breakdown' }, h('tbody', null, rows)));
  }

  const STANCE = { contradicts: 'Contradicts the claim', supports: 'Supports the claim', context: 'Adds context' };

  function evidenceSection(d) {
    const v = d.validity || {};
    const items = d.evidence || [];
    if (!v.summary && !items.length) return null;
    const order = { contradicts: 0, supports: 1, context: 2 };
    const sorted = [...items].sort((a, b) => (order[a.stance] ?? 3) - (order[b.stance] ?? 3));
    return section('What the evidence says', 'evidence',
      v.summary ? h('p', { text: v.summary }) : null,
      (v.facts || []).length ? h('ul', { class: 'chips' }, v.facts.map((f) => h('li', { text: f }))) : null,
      sorted.length
        ? h('ul', { class: 'items' }, sorted.map((e) =>
            h('li', null,
              h('div', { class: 'item-head' },
                h('span', { class: 'name', text: e.source_name || 'Source' }),
                h('span', { class: `badge ${e.stance || ''}`, text: STANCE[e.stance] || 'Evidence' }),
                e.source_type_label ? h('span', { class: 'badge', text: e.source_type_label }) : null),
              h('p', { text: e.finding || '' }),
              // The link is the page the search returned for this finding, which is
              // not always the original publisher, so it is labelled by its own site.
              e.url
                ? h('p', { class: 'small' }, h('span', { class: 'muted', text: 'Found via ' }), link(e.url, `${e.domain || host(e.url)} ↗`))
                : h('p', { class: 'muted small', text: 'No link: the search did not tie this finding to a specific page.' }))))
        : h('p', { class: 'muted', text: 'The search did not return usable evidence for this claim.' }));
  }

  function sourcingSection(d) {
    const s = d.sourcing || {};
    const srcs = d.page_sources || [];
    return section('What the page offers as support', 'sourcing',
      s.summary ? h('p', { text: s.summary }) : null,
      srcs.length
        ? h('ul', { class: 'items' }, srcs.map((p) =>
            h('li', null,
              h('div', { class: 'item-head' },
                p.url ? link(p.url, p.title || p.domain || p.url) : h('span', { class: 'name', text: p.anchor_text || 'Unlinked reference' }),
                p.tier_label ? h('span', { class: `badge ${p.tier_good === true ? 'good' : p.tier_good === false ? 'bad' : ''}`, text: p.tier_label }) : null,
                p.status_label ? h('span', { class: `badge ${p.status_ok === false ? 'bad' : ''}`, text: p.status_label }) : null,
                p.fidelity_label ? h('span', { class: `badge ${p.fidelity_ok === true ? 'good' : p.fidelity_ok === false ? 'bad' : ''}`, text: p.fidelity_label }) : null),
              p.note ? h('p', { class: 'small', text: `Footnote: ${p.note}` }) : null,
              p.fidelity_note ? h('p', { text: p.fidelity_note }) : null,
              p.url ? h('p', { class: 'muted small', text: p.domain || host(p.url) }) : null)))
        : h('p', { class: 'muted', text: 'The page gives no link or reference for this claim.' }));
  }

  function methodSection(d) {
    const s = d.search || {};
    const m = d.meta || {};
    const when = m.assessed_at ? new Date(m.assessed_at * 1000).toLocaleString() : '';
    const how = s.searched === false
      ? h('p', { class: 'small' }, `The live web search did not run for this claim, so ${m.model || 'the model'} judged it from what it already knew`, when ? ` on ${when}` : '', '. Nothing above was checked against a live source; treat the verdict as unverified.')
      : h('p', { class: 'small' }, `The claim was researched with live Google Search by ${m.model || 'the model'}`, when ? ` on ${when}` : '', m.cached ? ' (result reused from an earlier identical check)' : '', '. Only pages the search actually returned are linked above.');
    return section('How this was checked', 'method',
      how,
      (s.queries || []).length ? h('div', null, h('h3', { text: 'Searches run' }), h('ul', { class: 'chips' }, s.queries.map((q) => h('li', { text: q })))) : null,
      (s.sources || []).length
        ? h('details', null, h('summary', { text: `All ${s.sources.length} pages consulted` }),
            h('ul', { class: 'items' }, s.sources.map((x) => h('li', null, link(x.url, x.domain || host(x.url) || x.url)))))
        : null);
  }

  function renderClaim(d) {
    document.title = `${d.status === 'scored' ? Math.round(d.score) + '% dubious' : 'Claim'} · Dubious`;
    crumbs.replaceChildren(h('a', { href: scanHref(), text: '← All claims on this page' }));
    const pager = h('nav', { class: 'pager', 'aria-label': 'Other flagged claims' },
      d.prev_id ? h('a', { href: claimHref(d.prev_id), text: '← Previous flagged claim' }) : h('span'),
      d.next_id ? h('a', { href: claimHref(d.next_id), text: 'Next flagged claim →' }) : h('span'));
    const body = [hero(d)];
    if (d.status === 'scored') body.push(reasonsSection(d), breakdownSection(d), evidenceSection(d), sourcingSection(d), methodSection(d));
    else if (d.status === 'skipped') body.push(reasonsSection(d), evidenceSection(d), methodSection(d));
    else if (d.status === 'error') body.push(section('This claim could not be checked', 'error', h('p', { text: d.error || 'The check failed.' })));
    else if (d.status === 'pending') body.push(section('Still working', 'pending', h('p', { text: 'This claim is still being checked. The page will update by itself.' })));
    body.push(pager);
    app.replaceChildren(...body.filter(Boolean));
    app.setAttribute('data-ready', '');
  }

  // ------------------------------------------------------------- scan page

  function renderScan(s) {
    document.title = `${s.title || 'Page'} · Dubious report`;
    crumbs.replaceChildren();
    const scored = s.claims.filter((c) => c.status === 'scored');
    const flagged = scored.filter((c) => c.score >= s.threshold);
    const busy = s.status !== 'done' && s.status !== 'error';
    const head = h('section', { class: 'card', 'data-section': 'summary' },
      h('p', { class: 'kicker', text: 'Dubious report' }),
      h('h1', { text: s.title || host(s.url) || 'Scanned page' }),
      h('p', { class: 'from' }, link(s.url, s.url)),
      h('div', { class: 'stats' },
        h('div', { class: 'stat' }, h('strong', { text: String(s.total_claims) }), h('span', { text: 'claims found' })),
        h('div', { class: 'stat band-severe' }, h('strong', { text: String(flagged.length) }), h('span', { text: `flagged (${s.threshold}% or higher)` })),
        h('div', { class: 'stat' }, h('strong', { text: String(scored.length - flagged.length) }), h('span', { text: 'held up' })),
        s.skipped_claims ? h('div', { class: 'stat' }, h('strong', { text: String(s.skipped_claims) }), h('span', { text: 'not scored' })) : null,
        s.failed_claims ? h('div', { class: 'stat band-severe' }, h('strong', { text: String(s.failed_claims) }), h('span', { text: 'could not be checked' })) : null),
      busy ? h('p', { class: 'muted small', text: `Still checking: ${s.scored_claims} of ${s.total_claims} done. This page updates by itself.` }) : null,
      s.status === 'error' ? h('p', { class: 'error', text: s.error || 'The scan failed.' }) : null,
      s.truncated ? h('p', { class: 'muted small', text: 'This was a long page; only the first part was checked.' }) : null,
      s.meta && s.meta.cost_usd_estimate != null
        ? h('p', { class: 'muted small', text: s.meta.llm_calls
            ? `${s.meta.model} · ${s.meta.llm_calls} model calls · ${s.meta.search_requests} searches · about $${s.meta.cost_usd_estimate.toFixed(2)} at list prices ($${(s.meta.token_cost_usd_estimate ?? 0).toFixed(2)} while searches are within the free monthly allowance) · ${s.meta.seconds}s`
            : `${s.meta.model} · reused earlier results for this page, no new model calls` })
        : null);

    const sorted = [...s.claims].sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
    const row = (c) => {
      const band = c.status === 'scored' ? bandOf(c.score) : 'none';
      const label = c.status === 'scored' ? c.verdict_label
        : c.status === 'pending' ? 'Checking…'
        : c.status === 'skipped' ? ((c.reasons || [])[0] || {}).label || 'Not scored'
        : 'Could not be checked';
      return h('li', { 'data-claim-row': c.id },
        h('a', { class: `row band-${band}`, href: claimHref(c.id) },
          h('span', { class: 'chip', text: c.status === 'scored' ? `${Math.round(c.score)}%` : '–' }),
          h('span', null,
            h('span', { class: 'verdict', text: label || '' }),
            h('p', { class: 'quote', text: `“${c.quote}”` }),
            (c.reasons || []).length ? h('ul', { class: 'chips' }, c.reasons.slice(0, 4).map((r) => h('li', { text: r.label }))) : null)));
    };
    const isFlagged = (c) => c.status === 'scored' && c.score >= s.threshold;
    const top = sorted.filter(isFlagged);
    const rest = sorted.filter((c) => !isFlagged(c));
    const body = [head];
    if (top.length) body.push(section('Flagged claims', 'flagged', h('ul', { class: 'rows' }, top.map(row))));
    if (rest.length) body.push(section(top.length ? 'Everything else that was checked' : 'Claims checked', 'rest', h('ul', { class: 'rows' }, rest.map(row))));
    if (!s.claims.length && !busy) body.push(section('Nothing to check', 'empty', h('p', { text: 'No checkable factual claims were found on this page.' })));
    app.replaceChildren(...body);
    if (!busy || s.claims.length) app.setAttribute('data-ready', '');
    return busy;
  }

  // ------------------------------------------------------------------ boot

  async function load() {
    try {
      if (!scanId) throw new Error('No report specified.');
      if (claimId) {
        const d = await getJson(`/api/scan/${encodeURIComponent(scanId)}/claim/${encodeURIComponent(claimId)}${query}`);
        renderClaim(d);
        if (d.status === 'pending') setTimeout(load, 2500);
      } else {
        const s = await getJson(`/api/scan/${encodeURIComponent(scanId)}${query}`);
        if (renderScan(s)) setTimeout(load, 2500);
      }
    } catch (err) {
      app.replaceChildren(h('p', { class: 'error', text: err.message || 'Could not load the report.' }));
    }
  }

  document.getElementById('brand').href = scanId ? scanHref() : '#';
  load();
})();
