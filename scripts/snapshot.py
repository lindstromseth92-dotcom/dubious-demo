"""Save a static, script-free snapshot of a web page for repeatable testing.

  uv run python -I scripts/snapshot.py URL NAME            # fetch URL
  uv run python -I scripts/snapshot.py URL NAME --from FILE # reuse an already saved copy

Writes tests/fixtures/NAME.html. Scripts, iframes, inline event handlers and
meta refreshes are removed; a <base> tag points relative links, images and
stylesheets back at the original site. The snapshot is third-party content:
it is only ever opened in the throwaway test browser.
"""

import argparse
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, Comment

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
UA = {"User-Agent": "Mozilla/5.0"}


def sanitize(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "iframe", "object", "embed", "noscript", "base"]):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()  # conditional comments can smuggle markup
    for meta in soup.find_all("meta"):
        if (meta.get("http-equiv") or "").lower() in ("refresh", "content-security-policy"):
            meta.decompose()
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower()
        if "stylesheet" not in rel and "icon" not in rel:
            link.decompose()
    for el in soup.find_all(True):
        for attr in [a for a in el.attrs if a.lower().startswith("on")]:
            del el[attr]
        for attr in ("href", "src", "action"):
            if str(el.get(attr, "")).strip().lower().startswith("javascript:"):
                del el[attr]
    head = soup.head
    if head is None:  # a fragment, or a page with no <head>
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)
    head.insert(0, soup.new_tag("base", href=url))
    head.insert(1, soup.new_tag("meta", attrs={"name": "dubious-snapshot-of", "content": url}))
    return str(soup)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("name")
    ap.add_argument("--from", dest="source", help="use this saved HTML file instead of fetching")
    args = ap.parse_args()
    if args.source:
        html = Path(args.source).read_bytes().decode("utf-8", errors="replace")
    else:
        resp = httpx.get(args.url, headers=UA, follow_redirects=True, timeout=30)
        resp.raise_for_status()
        html = resp.text
    FIXTURES.mkdir(parents=True, exist_ok=True)
    out = FIXTURES / f"{args.name}.html"
    out.write_text(sanitize(html, args.url), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
