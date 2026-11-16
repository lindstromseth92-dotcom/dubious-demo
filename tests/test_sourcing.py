"""Guards on fetching links the scanned page chose. No network."""

import asyncio
import gzip
import socket

import httpx
import pytest

from dubious import sourcing


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses(ip):
    assert sourcing._public_ip(ip)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",  # loopback, private, link-local metadata
    "100.64.0.1", "100.100.100.200",  # carrier-grade NAT: Tailscale peers, some cloud metadata
    "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "0.0.0.0", "224.0.0.1", "not-an-ip",
])
def test_non_public_addresses(ip):
    assert not sourcing._public_ip(ip)


def fake_dns(monkeypatch, answers: dict[str, list[str]]):
    def getaddrinfo(host, *args, **kwargs):
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answers[host]]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


async def test_resolution_outcomes(monkeypatch):
    fake_dns(monkeypatch, {"good.example": ["93.184.216.34"], "mixed.example": ["93.184.216.34", "10.0.0.1"], "lan.example": ["192.168.0.9"]})
    assert await sourcing.resolve_public("https://good.example/x") == ("93.184.216.34", "")
    assert await sourcing.resolve_public("https://mixed.example/x") == (None, "skipped")  # one private answer taints the name
    assert await sourcing.resolve_public("http://lan.example/") == (None, "skipped")
    assert await sourcing.resolve_public("https://gone.example/") == (None, "unreachable")  # a name that does not resolve may only be failing for now
    assert await sourcing.resolve_public("ftp://good.example/") == (None, "skipped")
    assert await sourcing.resolve_public("javascript:alert(1)") == (None, "skipped")


def test_pinned_request_keeps_host_and_tls_name():
    url, headers, ext = sourcing.pinned_request("https://www.example.org:8443/a/b?q=1#frag", "93.184.216.34")
    assert url == "https://93.184.216.34:8443/a/b?q=1#frag"
    assert headers["Host"] == "www.example.org:8443" and ext == {"sni_hostname": "www.example.org"}
    url, headers, _ = sourcing.pinned_request("http://example.org/", "2606:4700::1111")
    assert url == "http://[2606:4700::1111]/" and headers["Host"] == "example.org"


def serve(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_the_connection_goes_to_the_address_that_was_checked(monkeypatch):
    """DNS rebinding: a second lookup at connect time must not be able to pick a different address."""
    fake_dns(monkeypatch, {"rebind.example": ["93.184.216.34"]})
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"<p>" + b"Some article text. " * 40 + b"</p>"))

    async with serve(handler) as http:
        src = await sourcing.fetch_cited(http, "https://rebind.example/page", "a claim", "https://blog.example.net/post")
    assert src.status == "ok"
    assert seen == [("93.184.216.34", "rebind.example", "rebind.example")]


async def test_redirect_to_a_private_address_is_not_followed(monkeypatch):
    fake_dns(monkeypatch, {"start.example": ["93.184.216.34"], "inside.example": ["10.1.2.3"]})
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "http://inside.example/admin"})

    async with serve(handler) as http:
        src = await sourcing.fetch_cited(http, "http://start.example/", "a claim", "https://blog.example.net/post")
    assert src.status == "skipped" and hosts == ["start.example"]


async def test_compressed_body_is_capped_after_inflation(monkeypatch):
    fake_dns(monkeypatch, {"bomb.example": ["93.184.216.34"]})
    bomb = gzip.compress(b"<p>" + b"a" * 60_000_000 + b"</p>")  # about 58 KB on the wire, 60 MB inflated
    assert len(bomb) < 200_000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "gzip"}, stream=httpx.ByteStream(bomb))

    async with serve(handler) as http:
        fetched, _, failure = await sourcing._get(http, "http://bomb.example/")
    assert failure == "" and len(fetched.text) <= sourcing.MAX_BYTES


async def test_gzip_pages_are_read(monkeypatch):
    fake_dns(monkeypatch, {"gz.example": ["93.184.216.34"]})
    html = ("<html><head><title>Measles facts</title></head><body><article>" + "<p>Measles is highly contagious and vaccination prevents it.</p>" * 20 + "</article></body></html>").encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"}, stream=httpx.ByteStream(gzip.compress(html)))

    async with serve(handler) as http:
        src = await sourcing.fetch_cited(http, "https://gz.example/measles", "Measles is contagious", "https://blog.example.net/post")
    assert src.status == "ok" and src.title == "Measles facts" and "highly contagious" in src.excerpt


async def test_statuses(monkeypatch):
    fake_dns(monkeypatch, {"site.example": ["93.184.216.34"]})
    cases = {
        "/missing": (httpx.Response(404), "dead"),
        "/forbidden": (httpx.Response(403), "blocked"),
        "/pdf": (httpx.Response(200, headers={"content-type": "application/pdf"}, stream=httpx.ByteStream(b"%PDF")), "not_html"),
        "/wall": (httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"<p>Please accept cookies to continue.</p>")), "blocked"),
        "/brotli": (httpx.Response(200, headers={"content-type": "text/html", "content-encoding": "br"}, content=b"\x00\x01"), "not_html"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return cases[request.url.path][0]

    async with serve(handler) as http:
        for path, (_, expected) in cases.items():
            src = await sourcing.fetch_cited(http, f"http://site.example{path}", "a claim", "https://blog.example.net/post")
            assert src.status == expected, path


async def test_a_slow_server_cannot_hold_a_fetch_past_the_deadline(monkeypatch):
    fake_dns(monkeypatch, {"tarpit.example": ["93.184.216.34"]})
    monkeypatch.setattr(sourcing, "FETCH_DEADLINE_S", 0.2)

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200)

    async with serve(handler) as http:
        src = await asyncio.wait_for(sourcing.fetch_cited(http, "http://tarpit.example/", "a claim", "https://blog.example.net/post"), timeout=2)
    assert src.status == "unreachable"


async def test_a_malformed_redirect_ends_the_fetch_not_the_scan(monkeypatch):
    fake_dns(monkeypatch, {"start.example": ["93.184.216.34"]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://start.example:99999/x"})  # not a valid port

    async with serve(handler) as http:
        src = await sourcing.fetch_cited(http, "http://start.example/", "a claim", "https://blog.example.net/post")
    assert src.status == "unreachable"


def test_a_page_wrapped_in_one_form_is_still_read():
    para = "Measles is contagious and vaccination prevents it. " * 3
    html = f"<html><head><title>T</title></head><body><form id=\"f\"><p>{para}</p><input name=\"q\"><button>Go</button></form></body></html>"
    title, blocks = sourcing._text_blocks(html)
    assert title == "T" and len(blocks) == 1 and "Go" not in blocks[0]


def test_connections_are_not_kept_alive_across_pinned_requests():
    """The pool keys on the address, so a kept-alive connection could serve the wrong site."""
    pool = sourcing.http_client()._transport._pool
    assert pool._max_keepalive_connections == 0
