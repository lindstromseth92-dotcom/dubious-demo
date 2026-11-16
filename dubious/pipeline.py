"""Scan orchestration: extract claims, assess each one, expose progress.

A scan's state is a plain JSON-serialisable dict so the same view functions
work on a live scan and on one reloaded from SQLite after a restart.
"""

import asyncio
import logging
import re
import secrets
import time

from . import assess, extract, llm, sourcing
from .config import settings
from .extract import Claim, PageBlock
from .store import Store, digest

log = logging.getLogger(__name__)

MAX_LIVE_SCANS = 40
CACHE_TTL_S = 14 * 24 * 3600  # evidence moves; re-check claims after two weeks
BENIGN_CODES: set[str] = set()  # reason codes left off a flagged claim's hover card; defined privately
_WS = re.compile(r"\s+")


def _flat(text: str) -> str:
    return _WS.sub(" ", text).strip()


def band_of(score: float) -> str:
    if score >= 80:
        return "severe"
    if score >= 60:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


class ScanManager:
    def __init__(self, store: Store):
        self.store = store
        self.live: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self.http = sourcing.http_client()
        self.sem = asyncio.Semaphore(settings.concurrency)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.http.aclose()
        self.store.close()

    # ------------------------------------------------------------- running

    async def start(self, url: str, title: str, blocks: list[PageBlock], truncated: bool, threshold: int | None = None) -> str:
        scan_id = secrets.token_urlsafe(9)
        state = {
            "id": scan_id,
            "url": url,
            "title": _flat(title)[:300],
            "created": time.time(),
            "status": "extracting",
            "error": None,
            "truncated": bool(truncated),
            "threshold": threshold if threshold is not None else settings.threshold,
            "claims": [],
            "contexts": {},
            "results": {},
            "meta": {},
        }
        self.live[scan_id] = state
        self._evict()
        task = asyncio.create_task(self._run(state, blocks))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return scan_id

    def _evict(self) -> None:
        if len(self.live) <= MAX_LIVE_SCANS:
            return
        finished = sorted((s for s in self.live.values() if s["status"] in ("done", "error")), key=lambda s: s["created"])
        for state in finished[: len(self.live) - MAX_LIVE_SCANS]:
            self.live.pop(state["id"], None)  # still readable from SQLite

    async def _run(self, state: dict, blocks: list[PageBlock]) -> None:
        usage = llm.Usage()
        started = time.time()
        try:
            claims = await self._extract(state, blocks, usage)
            state["claims"] = [c.model_dump() for c in claims]
            state["contexts"], state["paragraphs"] = self._contexts(claims, blocks)
            state["status"] = "scoring"
            reps: dict[str, Claim] = {}
            for c in claims:
                reps.setdefault(c.group, c)
            await asyncio.gather(*(self._assess(state, c, usage) for c in reps.values()))
            failures = [r for r in state["results"].values() if r.get("status") == "error"]
            if reps and len(failures) == len(reps):
                # Nothing was actually checked: say so, rather than report a page with no flags.
                state["status"] = "error"
                state["error"] = f"None of the {len(claims)} claims could be checked. {failures[0].get('error', '')}".strip()
            else:
                state["status"] = "done"
        except asyncio.CancelledError:
            state["status"] = "error"
            state["error"] = "The backend was stopped before this scan finished."
            raise
        except llm.LLMError as e:
            log.warning("scan %s failed: %s", state["id"], e)
            state["status"] = "error"
            state["error"] = f"The language model call failed: {e}"
        except Exception:
            log.exception("scan %s crashed", state["id"])
            state["status"] = "error"
            state["error"] = "The scan hit an internal error. See the backend log."
        finally:
            state["meta"] = usage.as_dict(settings.model) | {"seconds": round(time.time() - started, 1)}
            await asyncio.to_thread(self.store.save_scan, state["id"], state["url"], state["title"], state)

    @staticmethod
    def _contexts(claims: list[Claim], blocks: list[PageBlock]) -> tuple[dict[str, str], dict[str, str]]:
        """For each claim: (the context passed to assessment, the claim's own paragraph)."""
        index = {b.id: i for i, b in enumerate(blocks)}
        contexts, paragraphs = {}, {}
        for c in claims:
            i = index[c.block_id]
            before = _flat(blocks[i - 1].text)[-500:] if i > 0 else ""
            here = _flat(blocks[i].text)[:1800]
            after = _flat(blocks[i + 1].text)[:500] if i + 1 < len(blocks) else ""
            parts = [("Paragraph before", before), ("Paragraph containing the quote", here), ("Paragraph after", after)]
            contexts[c.id] = "\n".join(f"[{label}] {text}" for label, text in parts if text)
            paragraphs[c.id] = here
        return contexts, paragraphs

    async def _extract(self, state: dict, blocks: list[PageBlock], usage: llm.Usage) -> list[Claim]:
        key = digest(
            extract.PROMPT_VERSION, settings.model, settings.max_claims, state["url"], state["title"],
            [(b.id, b.tag, b.text, [link.model_dump() for link in b.links]) for b in blocks],
        )
        cached = await asyncio.to_thread(self.store.cache_get, "extract", key, CACHE_TTL_S)
        if cached:
            return [Claim(**c) for c in cached["claims"]]
        claims = await extract.extract_claims(state["url"], state["title"], blocks, settings.max_claims, usage)
        if claims:  # an empty result may be a bad day for the model; do not pin it for two weeks
            await asyncio.to_thread(self.store.cache_put, "extract", key, {"claims": [c.model_dump() for c in claims]})
        return claims

    async def _assess(self, state: dict, claim: Claim, usage: llm.Usage) -> None:
        context = state["contexts"].get(claim.id, "")
        key = digest(
            assess.VERSION, settings.model, claim.claim, claim.quote, claim.claim_type, claim.assertion_mode,
            claim.attributed_to, claim.attribution_kind, [(link.href, link.note) for link in claim.links],
            sourcing.domain_of(state["url"]), context,
        )
        used = llm.Usage()  # filled in as calls happen, so failed attempts are still counted
        try:
            cached = await asyncio.to_thread(self.store.cache_get, "assess", key, CACHE_TTL_S)
            if cached:
                result = cached
                result["meta"] = {**result.get("meta", {}), "cached": True}
            else:
                async with self.sem:
                    result = await assess.assess_claim(
                        claim, page_url=state["url"], page_title=state["title"], context=context,
                        paragraph=state.get("paragraphs", {}).get(claim.id, ""), http=self.http, usage=used,
                    )
                if result.get("search", {}).get("searched"):  # never pin a verdict that had no live search behind it
                    await asyncio.to_thread(self.store.cache_put, "assess", key, result)
        except asyncio.CancelledError:
            raise
        except llm.LLMError as e:
            log.warning("claim %s could not be assessed: %s", claim.id, e)
            result = {"status": "error", "error": f"The check for this claim failed: {e}"}
        except Exception:
            log.exception("claim %s crashed during assessment", claim.id)
            result = {"status": "error", "error": "The check for this claim hit an internal error."}
        finally:
            usage.add(used)
        state["results"][claim.group] = result

    # --------------------------------------------------------------- views

    async def _state(self, scan_id: str) -> dict | None:
        state = self.live.get(scan_id)
        if state is None:
            state = await asyncio.to_thread(self.store.load_scan, scan_id)
        return state

    @staticmethod
    def _claim_status(state: dict, claim: dict) -> tuple[str, dict | None]:
        result = state["results"].get(claim["group"])
        if result is None:
            return ("pending" if state["status"] in ("extracting", "scoring") else "error"), None
        status = result.get("status", "scored")
        return (status if status in ("error", "skipped") else "scored"), result

    def _claim_row(self, state: dict, claim: dict, base: str, threshold: int) -> dict:
        status, result = self._claim_status(state, claim)
        row = {
            "id": claim["id"],
            "block_id": claim["block_id"],
            "start": claim["start"],
            "end": claim["end"],
            "quote": claim["quote"],
            "claim": claim["claim"],
            "status": status,
            "score": None,
            "verdict_label": None,
            "headline": None,
            "reasons": [],
            "report_url": f"{base}/report/{state['id']}/{claim['id']}",
        }
        if status == "scored":
            row.update(
                score=result["score"],
                verdict_label=result["verdict_label"],
                headline=result["headline"],
                reasons=[
                    {"code": r["code"], "label": r["label"]}
                    for r in result.get("reasons", [])
                    if r["points"] > 0 and not (result["score"] >= threshold and r["code"] in BENIGN_CODES)
                ][:4],
            )
        elif status == "skipped":
            row.update(verdict_label=result.get("verdict_label"), headline=result.get("headline"),
                       reasons=[{"code": r["code"], "label": r["label"]} for r in result.get("reasons", [])[:2]])
        return row

    async def summary(self, scan_id: str, base: str, threshold: int | None = None) -> dict | None:
        """Progress and per-claim rows. `threshold` overrides the one the scan started with,
        so the report agrees with the popup slider if the user moved it afterwards."""
        state = await self._state(scan_id)
        if state is None:
            return None
        threshold = state["threshold"] if threshold is None else threshold
        rows = [self._claim_row(state, c, base, threshold) for c in state["claims"]]
        return {
            "scan_id": state["id"],
            "status": state["status"],
            "error": state["error"],
            "url": state["url"],
            "title": state["title"],
            "created": state["created"],
            "truncated": state["truncated"],
            "threshold": threshold,
            "total_claims": len(rows),
            "scored_claims": sum(1 for r in rows if r["status"] != "pending"),  # finished, whatever the outcome
            "failed_claims": sum(1 for r in rows if r["status"] == "error"),
            "skipped_claims": sum(1 for r in rows if r["status"] == "skipped"),
            "report_url": f"{base}/report/{state['id']}",
            "claims": rows,
            "meta": state.get("meta", {}),
        }

    async def detail(self, scan_id: str, claim_id: str, base: str, threshold: int | None = None) -> dict | None:
        state = await self._state(scan_id)
        if state is None:
            return None
        claim = next((c for c in state["claims"] if c["id"] == claim_id), None)
        if claim is None:
            return None
        status, result = self._claim_status(state, claim)
        threshold = state["threshold"] if threshold is None else threshold

        def is_flagged(c: dict) -> bool:
            st, res = self._claim_status(state, c)
            return st == "scored" and res["score"] >= threshold

        flagged_ids = [c["id"] for c in state["claims"] if is_flagged(c)]
        prev_id = next_id = None
        if claim_id in flagged_ids:
            i = flagged_ids.index(claim_id)
            prev_id = flagged_ids[i - 1] if i > 0 else None
            next_id = flagged_ids[i + 1] if i + 1 < len(flagged_ids) else None
        elif flagged_ids:
            next_id = flagged_ids[0]

        out = {
            "scan": {"id": state["id"], "url": state["url"], "title": state["title"], "created": state["created"]},
            "claim": {
                "id": claim["id"],
                "quote": claim["quote"],
                "claim": claim["claim"],
                "claim_type": claim["claim_type"],
                "speaker": claim.get("attributed_to"),
                "context": state.get("paragraphs", {}).get(claim["id"]) or state["contexts"].get(claim["id"], ""),
            },
            "status": status,
            "threshold": threshold,
            "score": None,
            "flagged": False,
            "prev_id": prev_id,
            "next_id": next_id,
        }
        if status == "scored":
            out.update({k: v for k, v in result.items() if k != "status"})
            out["band"] = band_of(result["score"])
            out["flagged"] = result["score"] >= threshold
        elif status == "skipped":
            out.update({k: v for k, v in result.items() if k not in ("status", "score")})
        elif status == "error":
            out["error"] = (result or {}).get("error") or state.get("error") or "This claim was not checked."
        return out
