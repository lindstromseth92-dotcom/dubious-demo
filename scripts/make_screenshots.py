"""Capture the screenshots used to show the project publicly.

  uv run python scripts/make_screenshots.py [--out DIR]

Runs the real extension and backend in headless Chromium against two saved
pages (the demo page written for this project, and the NaturalNews snapshot),
then saves:

  demo-hover.png      our own demo page: false claims underlined, true ones left alone, hover card open
  article-hover.png   a real article with its claims underlined and a hover card open
  report-claim.png    the report page behind one flagged claim on the demo page
  report-scan.png     the report listing every claim found on the demo page
  popup.png           the extension popup after a scan

Two parts of the claim report are removed before report-claim.png is captured:
the "How the score adds up" section and the row of labels above the evidence.
They print the scoring arithmetic and the labels it runs on, which are the
parts of this project that are not meant to be published. The article's own
side columns are hidden too, so the image is not carrying another site's logos.

Uses live Gemini calls for any page not already in the cache.
"""

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

from playwright.async_api import async_playwright  # noqa: E402
from run_e2e import BASE, EXT, LIBS, STATUS, TRIGGER, start_server  # noqa: E402

DEMO_HOVER = "MMR vaccine causes autism"
# The report shown is for a claim that names nobody: these images are public, and the
# report's generated wording should not be seen passing judgement on an individual.
DEMO_REPORT = "unvaccinated children are healthier"
# The article frame starts at this sentence. Its hover card says "retracted", which is a
# formal, documented status, and the paragraph above it (naming another writer) stays out of frame.
ARTICLE_HOVER = "Andrew Wakefield's study back"
# The site's own side columns: other headlines and network logos. They are hidden in the
# capture so the image shows the article and our marks, not the site's branding.
ARTICLE_HIDE = "#ColC, #ArticleCol2"


async def scan(ctx, sw, fixture: str, width: int, height: int):
    """Open a fixture, run a scan the way the popup does, and wait for it to finish."""
    page = await ctx.new_page()
    await page.set_viewport_size({"width": width, "height": height})
    url = f"{BASE}/fixtures/{fixture}.html"
    await page.goto(url, wait_until="domcontentloaded")
    started = await sw.evaluate(TRIGGER, url)
    deadline = time.time() + 420
    status = started["first"]
    while time.time() < deadline:
        status = await sw.evaluate(STATUS, started["tabId"])
        if status["status"] in ("done", "error"):
            break
        await asyncio.sleep(1.5)
    if status["status"] != "done":
        raise RuntimeError(f"scan of {fixture} did not finish: {status}")
    print(f"  {fixture}: {status['flagged']} of {status['total']} claims flagged")
    return page, started["tabId"], status


async def find_claim(page, text: str):
    mark = page.locator("dubious-mark[data-state='flagged']", has_text=text).first
    if not await mark.count():
        mark = page.locator("dubious-mark[data-state='flagged']").first
    return mark


async def hover_claim(page, text: str, at_top: bool = False):
    """Bring a flagged claim on screen (centred, or near the top) and open its hover card. Returns the claim id."""
    mark = await find_claim(page, text)
    if at_top:
        await mark.evaluate("el => { el.scrollIntoView({ block: 'start' }); window.scrollBy(0, -70); }")
    else:
        await mark.evaluate("el => el.scrollIntoView({ block: 'center' })")
    await asyncio.sleep(0.3)
    await mark.hover()
    await page.locator("dubious-ui .tip.on").wait_for(state="visible", timeout=5000)
    return await mark.get_attribute("data-claim")


async def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if LIBS.exists():
        env["LD_LIBRARY_PATH"] = f"{LIBS}:{env.get('LD_LIBRARY_PATH', '')}"
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="dubious-shots-"),
            channel="chromium",
            headless=True,
            env=env,
            device_scale_factor=2,  # crisp on high-density screens
            viewport={"width": 1200, "height": 760},
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
        )
        try:
            sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker", timeout=20000)
            ext_id = sw.url.split("/")[2]

            demo, demo_tab, demo_status = await scan(ctx, sw, "demo-mixed", 1200, 760)
            claim_id = await (await find_claim(demo, DEMO_REPORT)).get_attribute("data-claim")
            await hover_claim(demo, DEMO_HOVER)
            await demo.screenshot(path=out / "demo-hover.png")

            article, _, _ = await scan(ctx, sw, "naturalnews-042293", 1200, 760)
            # Hidden only after the scan, so the scan reads the page exactly as a visitor's would.
            await article.evaluate("sel => document.querySelectorAll(sel).forEach(e => { e.style.visibility = 'hidden'; })", ARTICLE_HIDE)
            await hover_claim(article, ARTICLE_HOVER, at_top=True)
            await article.screenshot(path=out / "article-hover.png")

            report = await ctx.new_page()
            await report.set_viewport_size({"width": 900, "height": 900})
            await report.goto(f"{demo_status['reportUrl']}/{claim_id}?t=40")
            await report.wait_for_selector("[data-ready]", timeout=15000)
            # Two parts of the report are left out of the public image: the score arithmetic,
            # and the row of raw labels the score is computed from.
            await report.evaluate(
                "() => { document.querySelector('[data-section=\"breakdown\"]')?.remove();"
                " document.querySelector('[data-section=\"evidence\"] ul.chips')?.remove(); }"
            )
            await report.screenshot(path=out / "report-claim.png", full_page=True)

            await report.goto(f"{demo_status['reportUrl']}?t=40")
            await report.wait_for_selector("[data-ready]", timeout=15000)
            await report.screenshot(path=out / "report-scan.png", full_page=True)

            popup = await ctx.new_page()
            await popup.set_viewport_size({"width": 300, "height": 330})
            await popup.goto(f"chrome-extension://{ext_id}/popup.html?tabId={demo_tab}")
            await popup.wait_for_function("document.getElementById('status').textContent.length > 0", timeout=8000)
            await popup.screenshot(path=out / "popup.png", full_page=True)
        finally:
            await ctx.close()
    for f in sorted(out.glob("*.png")):
        print(f"  wrote {f.name} ({f.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "screenshots"))  # where the README looks for them
    args = ap.parse_args()
    start_server()
    sys.exit(asyncio.run(main(Path(args.out))))
