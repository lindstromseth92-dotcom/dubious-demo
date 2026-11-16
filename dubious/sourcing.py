"""Deterministic sourcing signals: which tier a cited domain belongs to, whether
a cited link is alive, and what the cited page actually says.

Everything fetched here is third-party content chosen by the page being
scanned, so fetching is defensive: public http(s) hosts only, redirects
re-checked hop by hop, small size and time limits.
"""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import zlib
from dataclasses import asdict, dataclass
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from .config import ROOT

log = logging.getLogger(__name__)



def _load_registry() -> dict:
    """The source-tier registry: the full one if present, otherwise the small example that ships publicly."""
    for name in ("registry.json", "registry.example.json"):
        path = ROOT / "ontology" / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {"tiers": {}, "suffix_tiers": {}}


REGISTRY = _load_registry()
_TIER_BY_DOMAIN: dict[str, str] = {d: tier for tier, doms in REGISTRY["tiers"].items() for d in doms}

UA = {
    # Honest about being a tool. Sites that refuse automated readers answer 403, which is
    # recorded as "blocked": the link exists, but its text was not read.
    "User-Agent": "Mozilla/5.0 (compatible; Dubious/0.1; reads pages cited by an article a user is checking)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    "Accept-Language": "en-US,en;q=0.8",
    "Accept-Encoding": "gzip, identity",  # the encodings _read_capped can inflate under a size cap
}
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 5
FETCH_DEADLINE_S = 20
GOOGLE_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset(
    "the and for that with this from have has had are was were been not but they their there which what when "
    "who whom whose will would could should can may might about into than then them these those such also more "
    "most some any all one two its our your his her she him you said says per over under after before between".split()
)


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def tier_of(domain: str) -> str:
    """Registry tier for a domain; subdomains inherit (data.cdc.gov -> cdc.gov)."""
    parts = domain.lower().split(".")
    for i in range(len(parts) - 1):
        tier = _TIER_BY_DOMAIN.get(".".join(parts[i:]))
        if tier:
            return tier
    suffix = "." + ".".join(parts[-2:]) if len(parts) >= 2 else ""
    for pattern, tier in REGISTRY.get("suffix_tiers", {}).items():
        if domain.endswith(pattern) or suffix == pattern:
            return tier
    return "unknown"


def same_site(url_a: str, url_b: str) -> bool:
    a, b = domain_of(url_a), domain_of(url_b)
    return bool(a) and (a == b or a.endswith("." + b) or b.endswith("." + a))


# ------------------------------------------------------------------ fetching


def _public_ip(ip: str) -> bool:
    """True only for globally routable addresses.

    `is_global` is required as well as the individual exclusions because some
    non-routable ranges are neither "private" nor "reserved" to Python: the
    100.64.0.0/10 carrier-grade NAT block (where Tailscale peers and some
    cloud metadata services live) is the important one.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:  # ::ffff:10.0.0.1 is 10.0.0.1
        addr = mapped
    return addr.is_global and not (
        addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
        or addr.is_reserved or addr.is_unspecified
    )


async def resolve_public(url: str) -> tuple[str | None, str]:
    """Resolve a URL's host once. Returns (ip, "") when it may be fetched, else (None, why).

    why is "skipped" for anything that is not a public http(s) address (the
    link is ignored), or "unreachable" when the name does not resolve right
    now (which may be a transient failure rather than a dead link).
    The returned address is the one to connect to: resolving again at connect
    time would let a hostile DNS server answer differently the second time.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None, "skipped"
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return None, "unreachable"
    ips = [info[4][0] for info in infos]
    if not ips:
        return None, "unreachable"
    if not all(_public_ip(ip) for ip in ips):
        return None, "skipped"
    v4 = [ip for ip in ips if ":" not in ip]
    return (v4[0] if v4 else ips[0]), ""


async def is_public_url(url: str) -> bool:
    """True only for http(s) URLs whose host resolves exclusively to public addresses."""
    ip, _ = await resolve_public(url)
    return ip is not None


def pinned_request(url: str, ip: str) -> tuple[str, dict, dict]:
    """Rewrite a request so it connects to `ip` while still asking for the original host.

    Returns (url with the address in place of the name, headers, httpx extensions).
    The Host header and the TLS server name (used for SNI and for checking the
    certificate) stay the original hostname, so HTTPS verification is unchanged.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    try:
        wire_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        wire_host = host
    literal = f"[{ip}]" if ":" in ip else ip
    netloc = f"{literal}:{parsed.port}" if parsed.port else literal
    host_header = f"{wire_host}:{parsed.port}" if parsed.port else wire_host
    headers = {**UA, "Host": host_header}
    return urlunparse(parsed._replace(netloc=netloc)), headers, {"sni_hostname": wire_host}


@dataclass
class CitedSource:
    """What the scanned page pointed at in support of a claim."""

    url: str
    domain: str
    tier: str
    status: str  # ok | dead | blocked | unreachable | not_html | skipped
    http_status: int | None = None
    final_url: str | None = None
    title: str = ""
    excerpt: str = ""
    anchor_text: str = ""
    note: str = ""  # footnote text, when the link was reached through a footnote
    self_citation: bool = False

    def public(self) -> dict:
        d = asdict(self)
        d.pop("excerpt")
        return d


@dataclass
class _Fetched:
    status_code: int
    content_type: str
    text: str


async def _read_capped(resp: httpx.Response) -> bytes | None:
    """Read at most MAX_BYTES of *decoded* body. None if the encoding is one we do not decode.

    The raw stream is read and inflated here, with a ceiling on the output,
    rather than letting httpx decompress: a few kilobytes of gzip can expand
    to gigabytes, and a cap applied after decompression is no cap at all.
    """
    encoding = resp.headers.get("content-encoding", "").strip().lower()
    if encoding in ("gzip", "x-gzip", "deflate"):
        inflater = zlib.decompressobj(47)  # 32 + 15: accept a gzip or a zlib header
    elif encoding in ("", "identity"):
        inflater = None
    else:
        return None
    body = bytearray()
    try:
        async for chunk in resp.aiter_raw():
            if inflater is None:
                body += chunk
            else:
                data = chunk
                while data and len(body) < MAX_BYTES:
                    body += inflater.decompress(data, MAX_BYTES - len(body))
                    data = inflater.unconsumed_tail
            if len(body) >= MAX_BYTES:
                break
    except zlib.error:
        pass  # a damaged stream: keep what was read
    return bytes(body[:MAX_BYTES])


async def _get(http: httpx.AsyncClient, url: str) -> tuple[_Fetched | None, str, str]:
    """GET with manual redirects, each hop resolved once and pinned to a public address.

    Returns (fetched, final_url, failure_status).
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            # Inside the try: a malformed URL (a redirect to port 99999, say) raises ValueError
            # while it is being parsed, and that must end this fetch, not the whole check.
            ip, why = await resolve_public(current)
            if ip is None:
                return None, current, why
            target, headers, extensions = pinned_request(current, ip)
            async with http.stream("GET", target, headers=headers, extensions=extensions, follow_redirects=False) as resp:
                location = resp.headers.get("location")
                if resp.is_redirect and location:
                    current = urljoin(current, location)
                    continue
                content_type = resp.headers.get("content-type", "").lower()
                body: bytes | None = b""
                if resp.status_code < 400 and "html" in content_type:
                    body = await _read_capped(resp)
                if body is None:
                    return _Fetched(resp.status_code, "application/octet-stream", ""), current, ""  # readable by a browser, not by us
                text = body.decode(resp.charset_encoding or "utf-8", errors="replace")
                return _Fetched(resp.status_code, content_type, text), current, ""
        except (httpx.HTTPError, LookupError, ValueError, OSError) as e:
            log.info("fetch failed for %s: %s", current, type(e).__name__)
            return None, current, "unreachable"
    return None, current, "unreachable"


def _text_blocks(html: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True)[:200] if soup.title else ""
    # Form controls are dropped, but not <form> itself: some sites wrap the whole page in one.
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "aside", "svg", "button", "select", "textarea", "input"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = []
    for el in root.find_all(["p", "li", "blockquote", "h1", "h2", "h3", "td", "dd"]):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(text) >= 40:
            blocks.append(text)
    if not blocks:
        text = re.sub(r"\s+", " ", root.get_text(" ", strip=True))
        blocks = [text[i : i + 600] for i in range(0, min(len(text), 12000), 600)]
    return title, blocks


def _keywords(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


MIN_READABLE_CHARS = 400  # less than this is a cookie wall, a stub or an error page, not an article


def relevant_excerpt(blocks: list[str], claim: str, budget: int = 6000) -> str:
    """The opening of the page plus the passages that share most words with the claim."""
    if not blocks:
        return ""
    keys = _keywords(claim)
    scored = sorted(
        ((len(keys & _keywords(b)) / (1 + len(b) / 800), i) for i, b in enumerate(blocks)),
        reverse=True,
    )
    chosen = {0}
    used = len(blocks[0])
    for score, i in scored:
        if score <= 0 or used >= budget:
            break
        if i not in chosen:
            chosen.add(i)
            used += len(blocks[i])
    return "\n".join(blocks[i][:1200] for i in sorted(chosen))[: budget + 600]


async def fetch_cited(
    http: httpx.AsyncClient, url: str, claim: str, page_url: str, anchor_text: str = "", note: str = ""
) -> CitedSource:
    """Fetch one link the page offered as support and summarise what is there."""
    src = CitedSource(
        url=url,
        domain=domain_of(url),
        tier=tier_of(domain_of(url)),
        status="unreachable",
        anchor_text=anchor_text,
        note=note,
        self_citation=same_site(url, page_url),
    )
    try:
        # One deadline for the whole fetch: DNS, every redirect hop, headers and body. The
        # per-read timeout alone would let a server that drips a byte at a time hold a slot for hours.
        async with asyncio.timeout(FETCH_DEADLINE_S):
            resp, final_url, failure = await _get(http, url)
    except TimeoutError:
        resp, final_url, failure = None, url, "unreachable"
    src.final_url = final_url if final_url != url else None
    if resp is None:
        src.status = failure
        return src
    src.http_status = resp.status_code
    if resp.status_code in (404, 410):
        src.status = "dead"
    elif resp.status_code in (401, 403, 429, 451) or resp.status_code >= 500:
        src.status = "blocked"
    elif resp.status_code >= 400:
        src.status = "dead"
    elif "html" not in resp.content_type:
        src.status = "not_html"
    else:
        src.status = "ok"
        try:
            title, blocks = await asyncio.to_thread(_text_blocks, resp.text)
            src.title = title
            if sum(len(b) for b in blocks) >= MIN_READABLE_CHARS:
                src.excerpt = relevant_excerpt(blocks, claim)
            else:
                src.status = "blocked"  # the link works, but there is no article text to compare against
        except Exception as e:  # noqa: BLE001 - malformed third-party HTML must never fail a scan
            log.info("could not parse %s: %s", url, type(e).__name__)
    if final_url != url:
        # A link that redirects to another site is recorded under the site it lands on.
        src.domain = domain_of(final_url) or src.domain
        src.tier = tier_of(src.domain)
    return src


async def resolve_search_link(http: httpx.AsyncClient, uri: str) -> str | None:
    """Turn one of Google's grounding redirect links into the real page URL.

    The redirect links expire, so the real URL is what gets stored and shown.
    """
    if urlparse(uri).hostname != GOOGLE_REDIRECT_HOST:
        return uri
    try:
        resp = await asyncio.wait_for(http.get(uri, headers=UA, follow_redirects=False), timeout=10)
    except (httpx.HTTPError, TimeoutError):
        return None
    target = resp.headers.get("location")
    if resp.is_redirect and target and urlparse(target).scheme in ("http", "https"):
        return target
    return None


def http_client() -> httpx.AsyncClient:
    # No keep-alive. Requests are addressed to an IP with the real host in the Host header and
    # in the TLS name, and the connection pool keys on the address alone. A kept-alive
    # connection could therefore be reused for a different site that shares the address.
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=0)
    return httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=6.0), limits=limits)
