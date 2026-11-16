"""Thin async wrapper around the Gemini API.

Two entry points:
  research  a search-grounded, free-text call. Returns notes plus the queries run
            and the pages Google Search actually retrieved.
  generate  a tool-free call that returns a validated pydantic object.

Only pages the search retrieved are ever shown to users as links; a URL the
model merely writes in its answer is never trusted.
"""

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from .config import settings

log = logging.getLogger(__name__)

# USD. Source: https://aistudio.google.com/docs/pricing (read 2026-11-15).
# Flash token prices are scheduled to double on 2027-01-01.
PRICE_PER_M_TOKENS = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}
SEARCH_PRICE_PER_REQUEST = 14.0 / 1000  # after the 5,000 free requests per month

_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.api_key)
    return _client


@dataclass
class Source:
    """A web page Google Search retrieved while grounding an answer."""

    index: int
    domain: str
    uri: str  # Google's redirect link until resolved


@dataclass
class Support:
    """A span of the answer text and the sources that back it."""

    text: str
    source_indexes: list[int]
    end: int | None = None  # where the span ends in the answer, as a UTF-8 byte offset


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0  # includes thinking tokens, which are billed as output
    searches: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.searches += other.searches
        self.calls += other.calls

    def cost_usd(self, model: str) -> float:
        p_in, p_out = PRICE_PER_M_TOKENS.get(model, PRICE_PER_M_TOKENS["gemini-3.8-flash"])
        return (self.tokens_in * p_in + self.tokens_out * p_out) / 1e6 + self.searches * SEARCH_PRICE_PER_REQUEST

    def as_dict(self, model: str) -> dict:
        return {
            "model": model,
            "llm_calls": self.calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "search_requests": self.searches,
            "cost_usd_estimate": round(self.cost_usd(model), 4),
            # Searches are free up to Google's monthly allowance; this is the part you pay regardless.
            "token_cost_usd_estimate": round(self.cost_usd(model) - self.searches * SEARCH_PRICE_PER_REQUEST, 4),
        }


@dataclass
class Result[T]:
    value: T
    usage: Usage


class LLMError(RuntimeError):
    pass


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def _parse[T: BaseModel](resp: types.GenerateContentResponse, schema: type[T]) -> T:
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    text = resp.text or ""
    if not text.strip():
        raise LLMError("model returned an empty answer")
    try:
        return schema.model_validate_json(_FENCE.sub("", text))
    except ValidationError as e:
        raise LLMError(f"model answer did not match the schema: {e.errors()[:2]}") from e


def _grounding(resp: types.GenerateContentResponse) -> tuple[list[str], list[Source], list[Support]]:
    cand = resp.candidates[0] if resp.candidates else None
    gm = cand.grounding_metadata if cand else None
    if not gm:
        return [], [], []
    sources = []
    for i, ch in enumerate(gm.grounding_chunks or []):
        web = getattr(ch, "web", None)
        if web and web.uri:
            sources.append(Source(index=i, domain=(web.title or "").strip().lower(), uri=web.uri))
    supports = []
    for s in gm.grounding_supports or []:
        if s.segment and s.segment.text and s.grounding_chunk_indices:
            supports.append(Support(text=s.segment.text, source_indexes=list(s.grounding_chunk_indices), end=s.segment.end_index))
    return list(gm.web_search_queries or []), sources, supports


def _usage(resp: types.GenerateContentResponse, n_queries: int) -> Usage:
    u = resp.usage_metadata
    if not u:
        return Usage(calls=1, searches=n_queries)
    return Usage(
        tokens_in=(u.prompt_token_count or 0) + (u.tool_use_prompt_token_count or 0),
        tokens_out=(u.candidates_token_count or 0) + (u.thoughts_token_count or 0),
        searches=n_queries,
        calls=1,
    )


def _retryable(err: Exception) -> bool:
    if isinstance(err, errors.APIError):
        return err.code in (408, 429, 500, 502, 503, 504)
    return isinstance(err, (LLMError, asyncio.TimeoutError, json.JSONDecodeError))


@dataclass
class Research:
    """Free-text research notes from a search-grounded call, plus what was searched and found."""

    text: str
    usage: Usage
    queries: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    supports: list[Support] = field(default_factory=list)

    @property
    def searched(self) -> bool:
        return bool(self.queries or self.sources)


async def research(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    attempts: int = 3,
    timeout_s: float = 90,
    usage: Usage | None = None,
) -> Research:
    """Ask the model to research something with live Google Search and write notes.

    This call deliberately has no JSON schema. With structured output switched
    on, gemini-3.8-flash usually answers from memory and skips the search
    (measured 2026-11: 1 search in 7 schema calls, 9 in 9 without). If a reply
    still comes back with no search behind it, one further try is made with
    more thinking; after that the caller gets `searched == False` and must
    treat the notes as unverified.
    """
    total = usage if usage is not None else Usage()  # the caller's object keeps the spend even if this raises
    last: Exception | None = None
    best: Research | None = None
    for attempt in range(attempts):
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            thinking_config=types.ThinkingConfig(thinking_level="LOW" if best is None else "MEDIUM"),
        )
        try:
            resp = await asyncio.wait_for(
                client().aio.models.generate_content(model=model or settings.model, contents=prompt, config=config),
                timeout=timeout_s,
            )
            queries, sources, supports = _grounding(resp)
            total.add(_usage(resp, len(queries)))
            text = (resp.text or "").rstrip()  # leading text is kept: the support offsets count from the start
            if not text.strip():
                raise LLMError("model returned empty research notes")
            found = Research(text=text, usage=total, queries=queries, sources=sources, supports=supports)
            if found.searched or best is not None:
                return found
            best = found  # no search happened: try once more with more thinking
            log.info("research reply had no search behind it; retrying once")
        except Exception as e:  # noqa: BLE001 - classified just below
            last = e
            if not _retryable(e) or attempt == attempts - 1:
                break
            delay = (2**attempt) * 1.5 + random.random()
            log.warning("research call failed (%s); retry %d in %.1fs", type(e).__name__, attempt + 1, delay)
            await asyncio.sleep(delay)
    if best is not None:
        return best
    if isinstance(last, errors.APIError):
        raise LLMError(f"Gemini API error {last.code}: {last.message}") from last
    raise LLMError(str(last) or type(last).__name__) from last


async def generate[T: BaseModel](
    prompt: str,
    schema: type[T],
    *,
    system: str | None = None,
    thinking: str | None = None,
    model: str | None = None,
    attempts: int = 3,
    timeout_s: float = 120,
    usage: Usage | None = None,
) -> Result[T]:
    """Call the model (no tools) and return a validated `schema` instance.

    thinking is one of "low", "medium", "high" (None = model default).
    For anything that needs live search, use `research` first and pass its notes in.
    """
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0,
        response_mime_type="application/json",
        response_schema=schema,
        thinking_config=types.ThinkingConfig(thinking_level=thinking.upper()) if thinking else None,
    )
    total = usage if usage is not None else Usage()  # the caller's object keeps the spend even if this raises
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await asyncio.wait_for(
                client().aio.models.generate_content(model=model or settings.model, contents=prompt, config=config),
                timeout=timeout_s,
            )
            total.add(_usage(resp, 0))
            return Result(value=_parse(resp, schema), usage=total)
        except Exception as e:  # noqa: BLE001 - classified just below
            last = e
            if not _retryable(e) or attempt == attempts - 1:
                break
            delay = (2**attempt) * 1.5 + random.random()
            log.warning("LLM call failed (%s); retry %d in %.1fs", type(e).__name__, attempt + 1, delay)
            await asyncio.sleep(delay)
    if isinstance(last, errors.APIError):
        raise LLMError(f"Gemini API error {last.code}: {last.message}") from last
    raise LLMError(str(last) or type(last).__name__) from last
