"""Run the engine on a saved page without a browser, and print what it concludes.

  uv run python scripts/scan_fixture.py [FIXTURE] [--limit N]

Reads tests/fixtures/FIXTURE.html, splits it into paragraphs roughly the way
the extension does, runs the full pipeline (live Gemini calls, cached), and
prints one line per claim. Handy for tuning prompts and checking cost.
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup, NavigableString, Tag  # noqa: E402

from dubious.config import settings  # noqa: E402
from dubious.extract import PageBlock, PageLink  # noqa: E402
from dubious.pipeline import ScanManager  # noqa: E402
from dubious.store import Store  # noqa: E402

BLOCK_TAGS = {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "td", "dd", "div", "article", "section", "body"}
SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "aside", "button", "select", "textarea", "input", "header", "iframe"}


def blocks_from_html(html: str) -> list[PageBlock]:
    """A rough stand-in for the extension's reader: runs of inline text, split at blocks and double <br>."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article") or soup.find(id="Article") or soup.body or soup
    runs: list[tuple[str, str, list[PageLink]]] = []

    def walk(el: Tag) -> None:
        text, links, br = "", [], 0

        def flush() -> None:
            nonlocal text, links
            if text.strip():
                runs.append((el.name, text, links))
            text, links = "", []

        for child in el.children:
            if isinstance(child, NavigableString):
                if str(child).strip():
                    br = 0
                text += str(child)
            elif isinstance(child, Tag):
                if child.name in SKIP_TAGS:
                    continue
                if child.name == "br":
                    br += 1
                    if br >= 2:
                        flush()
                    else:
                        text += "\n"
                elif child.name in BLOCK_TAGS:
                    flush()
                    walk(child)
                else:
                    inner = child.get_text()
                    text += inner
                    br = 0
                    for a in ([child] if child.name == "a" else child.find_all("a")):
                        href = a.get("href", "")
                        if href.startswith("http"):
                            links.append(PageLink(text=a.get_text(" ", strip=True)[:120], href=href, at=len(text)))
        flush()

    walk(root)
    out = []
    for tag, text, links in runs:
        flat = re.sub(r"\s+", " ", text).strip()
        if len(flat) < 40 or len(flat.split()) < 6:
            continue
        linked = sum(len(link.text) for link in links)
        if linked / max(len(flat), 1) >= 0.8:
            continue
        out.append(PageBlock(id=f"b{len(out)}", tag=tag, text=text, links=links[:8]))
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", nargs="?", default="demo-mixed")
    ap.add_argument("--limit", type=int, default=0, help="only scan the first N blocks")
    ap.add_argument("--json", help="write the full results to this file")
    args = ap.parse_args()

    html = (ROOT / "tests" / "fixtures" / f"{args.fixture}.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"name": "dubious-snapshot-of"})
    url = meta["content"] if meta else f"https://example.org/{args.fixture}"
    title = soup.title.get_text(strip=True) if soup.title else args.fixture
    blocks = blocks_from_html(html)
    if args.limit:
        blocks = blocks[: args.limit]
    print(f"{len(blocks)} blocks, {sum(len(b.text) for b in blocks)} chars from {url}")

    manager = ScanManager(Store(settings.db_path))
    scan_id = await manager.start(url, title, blocks, False)
    while True:
        view = await manager.summary(scan_id, "http://localhost:8787")
        if view["status"] in ("done", "error"):
            break
        await asyncio.sleep(1)
    if view["status"] == "error":
        print("SCAN FAILED:", view["error"])
    print(f"\n{'score':>5}  {'label':<17} claim")
    details = []
    for row in sorted(view["claims"], key=lambda r: -(r["score"] or 0)):
        detail = await manager.detail(scan_id, row["id"], "http://localhost:8787")
        details.append(detail)
        score = row["score"] if row["score"] is not None else "--"
        print(f"{score:>5}  {(row['verdict_label'] or row['status']):<17} {row['claim'][:110]}")
        chips = ", ".join(r["label"] for r in row["reasons"])
        if chips:
            print(f"{'':>5}  {'':<17} [{chips}]")
    print(f"\nusage: {json.dumps(view['meta'])}")
    if args.json:
        Path(args.json).write_text(json.dumps({"scan": view, "details": details}, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"wrote {args.json}")
    await manager.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
