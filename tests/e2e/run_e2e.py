"""End-to-end check: real Chromium + the unpacked extension + the real backend + live Gemini.

  uv run python tests/e2e/run_e2e.py [FIXTURE] [--shots DIR] [--timeout SECONDS]

FIXTURE is the name of a page in tests/fixtures (default demo-mixed, the demonstration
article that ships with the code; other pages are saved there with scripts/snapshot.py).
The script starts the backend on port 8787 with the fixtures mounted, loads the
extension, triggers a scan exactly the way the popup does, waits for it to
finish, then exercises the underline, the hover card and the report pages.
Exits non-zero if any check fails. This spends real API credit (cached on re-runs).
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

EXT = ROOT / "extension"
FIXTURES = ROOT / "tests" / "fixtures"
# Optional: Chromium's system libraries, unpacked here on machines where they are not installed.
LIBS = ROOT / ".cache" / "chromium-libs" / "root" / "usr" / "lib" / "x86_64-linux-gnu"
BASE = "http://localhost:8787"

TRIGGER = """async (url) => {
  const [tab] = await chrome.tabs.query({ url });
  if (!tab) throw new Error('tab not found for ' + url);
  await chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ['content.css'] });
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
  const first = await chrome.tabs.sendMessage(tab.id, { type: 'dubious:start' });
  return { tabId: tab.id, first };
}"""

STATUS = "async (tabId) => chrome.tabs.sendMessage(tabId, { type: 'dubious:status' })"

MARKS = """() => {
  const byClaim = new Map();
  for (const m of document.querySelectorAll('dubious-mark')) {
    const e = byClaim.get(m.dataset.claim) || { claim: m.dataset.claim, state: m.dataset.state, band: m.dataset.band || null, text: '' };
    e.text += m.textContent;
    byClaim.set(m.dataset.claim, e);
  }
  return [...byClaim.values()];
}"""


def start_server() -> uvicorn.Server:
    from server.app import app

    app.mount("/fixtures", StaticFiles(directory=FIXTURES), name="fixtures")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8787, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("backend did not start (is port 8787 already in use?)")
        time.sleep(0.1)
    return server


class Checks:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def ok(self, cond: bool, label: str, detail: str = "") -> bool:
        print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))
        if not cond:
            self.failed.append(label)
        return cond


async def run(fixture: str, shots: Path, timeout: int) -> int:
    shots.mkdir(parents=True, exist_ok=True)
    checks = Checks()
    url = f"{BASE}/fixtures/{fixture}.html"
    env = dict(os.environ)
    if LIBS.exists():
        env["LD_LIBRARY_PATH"] = f"{LIBS}:{env.get('LD_LIBRARY_PATH', '')}"

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="dubious-e2e-"),
            channel="chromium",
            headless=True,
            env=env,
            viewport={"width": 1280, "height": 900},
            args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
        )
        try:
            sw = ctx.service_workers[0] if ctx.service_workers else await ctx.wait_for_event("serviceworker", timeout=20000)
            ext_id = sw.url.split("/")[2]
            page = await ctx.new_page()
            console: list[str] = []
            page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: console.append(f"pageerror: {e}"))
            await page.goto(url, wait_until="domcontentloaded")
            dom_before = await page.evaluate("() => document.body.innerText")

            print(f"\nScanning {url}")
            started = await sw.evaluate(TRIGGER, url)
            tab_id = started["tabId"]
            checks.ok(started["first"]["status"] in ("collecting", "extracting", "scoring"), "scan started", started["first"]["status"])

            t0 = time.time()
            status = started["first"]
            saw_pending = False
            while time.time() - t0 < timeout:
                status = await sw.evaluate(STATUS, tab_id)
                if status["status"] == "scoring" and not saw_pending:
                    saw_pending = await page.locator("dubious-mark[data-state='pending']").count() > 0
                    if saw_pending:
                        await page.screenshot(path=shots / f"{fixture}-1-in-progress.png")
                if status["status"] in ("done", "error"):
                    break
                await asyncio.sleep(1.5)
            elapsed = round(time.time() - t0, 1)
            print(f"  finished in {elapsed}s: {json.dumps(status)}")
            if not checks.ok(status["status"] == "done", "scan completed", status.get("error") or ""):
                return 1
            checks.ok(status["total"] >= 5, "found a reasonable number of claims", str(status["total"]))
            checks.ok(status["flagged"] >= 1, "at least one claim flagged", f"{status['flagged']} of {status['total']}")

            scan_id = status["reportUrl"].rstrip("/").split("/")[-1]
            scan = await (await ctx.request.get(f"{BASE}/api/scan/{scan_id}")).json()
            (shots / f"{fixture}-scan.json").write_text(json.dumps(scan, indent=2, ensure_ascii=False))
            print("\n  score  verdict                          quote")
            for c in sorted(scan["claims"], key=lambda c: -(c.get("score") or 0)):
                print(f"  {c.get('score') if c.get('score') is not None else '--':>5}  {(c.get('verdict_label') or c['status'])[:32]:<32} {c['quote'][:90]!r}")
            print(f"\n  usage: {json.dumps(scan.get('meta', {}))}")

            marks = await page.evaluate(MARKS)
            by_id = {c["id"]: c for c in scan["claims"]}
            flagged = [m for m in marks if m["state"] == "flagged"]
            checks.ok(len(marks) == len(scan["claims"]), "every claim was located in the page", f"{len(marks)} marks for {len(scan['claims'])} claims")
            checks.ok(all(" ".join(m["text"].split()) == " ".join(by_id[m["claim"]]["quote"].split()) for m in marks if m["claim"] in by_id),
                      "underlined text equals the quoted claim text")
            checks.ok(len(flagged) == status["flagged"], "flagged underlines match the reported count", f"{len(flagged)}")
            checks.ok(all(by_id[m["claim"]]["score"] >= scan["threshold"] for m in flagged), "only claims at or above the threshold are underlined")
            checks.ok(await page.locator("dubious-mark[data-state='pending']").count() == 0, "no claim left pending")
            dom_after = await page.evaluate("() => document.body.innerText")
            checks.ok(" ".join(dom_before.split()) == " ".join(dom_after.split()), "page text is unchanged by the underlines")
            style = await page.locator("dubious-mark[data-state='flagged']").first.evaluate(
                "el => { const s = getComputedStyle(el); return [s.textDecorationLine, s.textDecorationStyle].join(' '); }")
            checks.ok(style == "underline wavy", "flagged claims get a wavy underline", style)

            first = page.locator("dubious-mark[data-state='flagged']").first
            await first.scroll_into_view_if_needed()
            await first.hover()
            tip = page.locator("dubious-ui .tip.on")
            await tip.wait_for(state="visible", timeout=5000)
            tip_text = " ".join((await tip.inner_text()).split())
            first_claim = by_id[await first.get_attribute("data-claim")]
            checks.ok(f"{round(first_claim['score'])}%" in tip_text, "hover card shows the dubious score", tip_text[:140])
            await page.screenshot(path=shots / f"{fixture}-2-hover.png")

            async with ctx.expect_page() as new_page:
                await tip.locator("a.more").click()
            report = await new_page.value
            await report.wait_for_selector("[data-ready]", timeout=15000)
            checks.ok(f"/report/{scan_id}/{first_claim['id']}" in report.url, "hover link opens that claim's report page", report.url)
            body = await report.inner_text("body")
            checks.ok(first_claim["quote"][:40] in body, "report page quotes the claim")
            checks.ok(await report.locator("[data-section='evidence'] li, [data-section='evidence'] article").count() > 0, "report page lists evidence")
            await report.screenshot(path=shots / f"{fixture}-3-claim-report.png", full_page=True)

            await report.goto(f"{BASE}/report/{scan_id}")
            await report.wait_for_selector("[data-ready]", timeout=15000)
            checks.ok(await report.locator("[data-claim-row]").count() == len(scan["claims"]), "scan report lists every claim")
            await report.screenshot(path=shots / f"{fixture}-4-scan-report.png", full_page=True)

            popup = await ctx.new_page()
            await popup.set_viewport_size({"width": 320, "height": 420})
            await popup.goto(f"chrome-extension://{ext_id}/popup.html?tabId={tab_id}")
            await popup.wait_for_function("document.getElementById('status').textContent.length > 0", timeout=8000)
            popup_status = await popup.inner_text("#status")
            checks.ok("dubious" in popup_status.lower() or "checked" in popup_status.lower(), "popup shows the result", popup_status)
            await popup.screenshot(path=shots / f"{fixture}-5-popup.png")

            await sw.evaluate("async (tabId) => chrome.tabs.sendMessage(tabId, { type: 'dubious:clear' })", tab_id)
            checks.ok(await page.locator("dubious-mark").count() == 0, "clearing removes every underline")
            checks.ok(not [c for c in console if "dubious" in c.lower()], "no extension errors in the page console", "; ".join(console[:3]))
        finally:
            await ctx.close()

    print(f"\n{'ALL CHECKS PASSED' if not checks.failed else 'FAILED: ' + ', '.join(checks.failed)}")
    print(f"screenshots and scan JSON in {shots}")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", nargs="?", default="demo-mixed")
    ap.add_argument("--shots", default=str(ROOT / ".cache" / "e2e"))
    ap.add_argument("--timeout", type=int, default=420)
    args = ap.parse_args()
    if not (FIXTURES / f"{args.fixture}.html").exists():
        sys.exit(f"no fixture named {args.fixture}; create one with scripts/snapshot.py")
    start_server()
    sys.exit(asyncio.run(run(args.fixture, Path(args.shots), args.timeout)))
