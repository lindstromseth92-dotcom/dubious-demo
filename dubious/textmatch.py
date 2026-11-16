"""Locate an LLM-returned quote inside the raw page text it came from.

The extension needs exact character offsets into the text it sent so it can
underline the right words. Models are asked to quote verbatim, but in practice
they normalise whitespace, swap curly quotes for straight ones, drop a trailing
full stop, or elide the middle of a long sentence. `locate` tolerates all of
those and always answers in offsets of the *raw* text.
"""

import re
from difflib import SequenceMatcher

_CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ", " ": " ",
}
_ZERO_WIDTH = {"​", "‌", "‍", "﻿", "­"}
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.+|…)\s*")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+")
_EDGE_JUNK = " \t\n\"'.,;:!?()[]-"


def normalize(text: str, lower: bool = False) -> tuple[str, list[int]]:
    """Return (normalised text, map from normalised index to raw index).

    Whitespace runs collapse to one space, typographic quotes and dashes become
    ASCII, zero-width characters vanish, leading whitespace is dropped.
    """
    out: list[str] = []
    idx: list[int] = []
    pending_space = False
    for i, ch in enumerate(text):
        if ch in _ZERO_WIDTH:
            continue
        ch = _CHAR_MAP.get(ch, ch)
        if ch.isspace():
            pending_space = bool(out)
            continue
        if pending_space:
            out.append(" ")
            idx.append(i - 1 if i > 0 else 0)
            pending_space = False
        out.append(ch.lower() if lower else ch)
        idx.append(i)
    return "".join(out), idx


def _span(idx: list[int], start: int, end: int) -> tuple[int, int]:
    return idx[start], idx[end - 1] + 1


def _find(norm_text: str, norm_quote: str) -> tuple[int, int] | None:
    if not norm_quote:
        return None
    at = norm_text.find(norm_quote)
    return (at, at + len(norm_quote)) if at >= 0 else None


def _find_elided(norm_text: str, norm_quote: str) -> tuple[int, int] | None:
    """Match 'first part ... last part' where the model skipped the middle."""
    parts = [p for p in _ELLIPSIS.split(norm_quote) if len(p) >= 8]
    if len(parts) < 2:
        return None
    head = norm_text.find(parts[0])
    if head < 0:
        return None
    tail = norm_text.find(parts[-1], head + len(parts[0]))
    if tail < 0:
        return None
    end = tail + len(parts[-1])
    if end - head > max(3 * len(norm_quote), 400):
        return None
    return head, end


def _find_fuzzy(norm_text: str, norm_quote: str, min_ratio: float = 0.82) -> tuple[int, int] | None:
    """Best sentence (or pair of adjacent sentences) by similarity to the quote."""
    bounds = [0] + [m.end() for m in _SENTENCE_END.finditer(norm_text)] + [len(norm_text)]
    bounds = sorted(set(bounds))
    best: tuple[float, int, int] | None = None
    for i in range(len(bounds) - 1):
        for j in (i + 1, i + 2):
            if j >= len(bounds):
                break
            s, e = bounds[i], bounds[j]
            cand = norm_text[s:e].rstrip()
            if not cand or abs(len(cand) - len(norm_quote)) > 0.5 * len(norm_quote):
                continue
            ratio = SequenceMatcher(None, cand, norm_quote, autojunk=False).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, s, s + len(cand))
    if best and best[0] >= min_ratio:
        return best[1], best[2]
    return None


def locate(quote: str, text: str) -> tuple[int, int] | None:
    """Return (start, end) offsets of `quote` in raw `text`, or None if it is not there."""
    norm_text, idx = normalize(text)
    norm_quote, _ = normalize(quote)
    if not norm_text or not norm_quote:
        return None

    hit = _find(norm_text, norm_quote)
    if hit is None:
        low_text, low_idx = normalize(text, lower=True)
        low_quote = norm_quote.lower()
        # lower() keeps length for the characters we care about; guard anyway
        if len(low_text) == len(norm_text):
            hit = _find(low_text, low_quote)
            if hit is None:
                hit = _find(low_text, low_quote.strip(_EDGE_JUNK))
            if hit is None:
                hit = _find_elided(low_text, low_quote)
            if hit is None:
                hit = _find_fuzzy(low_text, low_quote)
            idx = low_idx
    if hit is None:
        return None
    return _span(idx, hit[0], hit[1])
