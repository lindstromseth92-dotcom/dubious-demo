"""Step 1: pull the checkable claims out of a page.

The model sees the page as numbered blocks and returns, for each claim, the
exact words that state it (so the extension can underline them), a standalone
restatement (so it can be researched out of context), and which of the page's
own links are offered as support.
"""

import logging
import re

from pydantic import BaseModel, Field, field_validator

from . import llm
from .textmatch import locate

log = logging.getLogger(__name__)

PROMPT_VERSION = "extract-v5"

# The values these three fields can take are part of the private scoring design.
ClaimType = str
AssertionMode = str
AttributionKind = str


class PageLink(BaseModel):
    text: str = Field(default="", max_length=300)
    href: str | None = Field(default=None, max_length=2000)
    note: str | None = Field(default=None, max_length=600)  # footnote text, when reached through a footnote marker
    at: int | None = None  # offset in the block text where the link's anchor text ends


class PageBlock(BaseModel):
    """One paragraph of the page as the browser read it.

    Offsets that cross the wire (PageLink.at coming in, Claim.start/end going
    out) are in UTF-16 code units, because that is how JavaScript indexes
    strings. Python indexes by code point, so they are converted at the edge
    (`_from_u16`, `_to_u16`); they only differ when the text has emoji or other
    characters outside the Basic Multilingual Plane.
    """

    id: str = Field(max_length=16)
    tag: str = "p"
    text: str = Field(max_length=20000)
    links: list[PageLink] = Field(default_factory=list, max_length=12)

    @field_validator("tag", mode="before")
    @classmethod
    def _clip_tag(cls, v: object) -> object:
        return v[:16] if isinstance(v, str) else v  # custom element names can be long; only a few tags matter


def _to_u16(text: str, index: int) -> int:
    """Code-point index into `text` -> UTF-16 code-unit index."""
    return len(text[:index].encode("utf-16-le", errors="surrogatepass")) // 2


def _from_u16(text: str, units: int) -> int:
    """UTF-16 code-unit index into `text` -> code-point index."""
    raw = text.encode("utf-16-le", errors="surrogatepass")
    return len(raw[: max(0, units) * 2].decode("utf-16-le", errors="ignore"))


class RawClaim(BaseModel):
    """One claim as the model returns it, before it is located on the page."""

    block_id: str
    quote: str
    claim: str
    claim_type: ClaimType
    assertion_mode: AssertionMode
    attributed_to: str | None = None
    attribution_kind: AttributionKind
    cited_links: list[int] = Field(default_factory=list)
    central: bool = False


class Extraction(BaseModel):
    claims: list[RawClaim]


class Claim(BaseModel):
    """A claim located on the page, ready for assessment."""

    id: str
    block_id: str
    start: int
    end: int
    quote: str
    claim: str
    claim_type: ClaimType
    assertion_mode: AssertionMode
    attributed_to: str | None = None
    attribution_kind: AttributionKind
    central: bool = False
    links: list[PageLink] = Field(default_factory=list)
    group: str = ""  # claims with the same group share one assessment


# The extraction prompt is private. Without it extract_claims raises; everything else here works.
SYSTEM: str | None = None
PROMPT: str | None = None

_WS = re.compile(r"\s+")
_MARKER = re.compile(r"\s*\{\d+\}")


def _flat(text: str) -> str:
    return _WS.sub(" ", text).strip()


def render_blocks(blocks: list[PageBlock]) -> tuple[str, dict[int, tuple[str, PageLink]]]:
    """Format blocks for the prompt. Links are numbered across the whole page."""
    lines: list[str] = []
    numbered: dict[int, tuple[str, PageLink]] = {}
    n = 0
    for b in blocks:
        label = f"[{b.id}]" + (f" ({b.tag})" if b.tag in ("h1", "h2", "h3", "h4", "li", "blockquote", "td") else "")
        text = b.text
        legend: list[str] = []
        inserts: list[tuple[int, str]] = []
        for link in b.links:
            n += 1
            numbered[n] = (b.id, link)
            if link.at is not None and link.at >= 0:
                inserts.append((_from_u16(b.text, link.at), f"{{{n}}}"))  # marker goes right after the anchor text
            target = link.href or "(no link)"
            note = f' footnote: "{_flat(link.note)[:240]}"' if link.note else ""
            legend.append(f'    {{{n}}} "{_flat(link.text)[:100]}" -> {target}{note}')
        for at, marker in sorted(inserts, reverse=True):
            text = text[:at] + marker + text[at:]
        lines.append(f"{label} {_flat(text)}")
        lines.extend(legend)
    return "\n".join(lines), numbered


def _resolve_overlaps(found: list[tuple[int, int, RawClaim]]) -> list[tuple[int, int, RawClaim]]:
    """Within one block, make spans disjoint. Earlier spans win; later ones are trimmed or dropped."""
    out: list[tuple[int, int, RawClaim]] = []
    for start, end, raw in sorted(found, key=lambda t: (t[0], -t[1])):
        if out and start < out[-1][1]:
            start = out[-1][1]
            if end - start < 20:
                continue
        out.append((start, end, raw))
    return out


def place_claims(
    raw_claims: list[RawClaim], blocks: list[PageBlock], numbered: dict[int, tuple[str, PageLink]], max_claims: int
) -> list[Claim]:
    """Locate each raw claim on the page; drop any the model did not quote faithfully."""
    by_id = {b.id: b for b in blocks}
    order = {b.id: i for i, b in enumerate(blocks)}
    per_block: dict[str, list[tuple[int, int, RawClaim]]] = {}
    for raw in raw_claims:
        block = by_id.get(raw.block_id.strip().strip("[]"))
        if not block or not raw.claim.strip():
            continue
        hit = locate(_MARKER.sub("", raw.quote), block.text)
        if hit is None:
            log.info("dropping claim whose quote is not in %s: %r", block.id, raw.quote[:80])
            continue
        per_block.setdefault(block.id, []).append((hit[0], hit[1], raw))

    placed: list[Claim] = []
    groups: dict[str, str] = {}
    for block_id in sorted(per_block, key=order.__getitem__):
        text = by_id[block_id].text
        for start, end, raw in _resolve_overlaps(per_block[block_id]):
            while start < end and text[start].isspace():
                start += 1
            while end > start and text[end - 1].isspace():
                end -= 1
            if end - start < 8:
                continue
            links = [numbered[n][1] for n in dict.fromkeys(raw.cited_links) if n in numbered][:4]
            # A repeat shares one assessment only if everything the assessment depends on is the
            # same: wording, how it is put, who it is credited to, and the sources offered.
            key = repr((
                _flat(raw.claim).lower().rstrip("."), raw.claim_type, raw.assertion_mode, raw.attribution_kind,
                (raw.attributed_to or "").strip().lower(), sorted((link.href or "", link.note or "") for link in links),
            ))
            cid = f"c{len(placed) + 1}"
            placed.append(
                Claim(
                    id=cid,
                    block_id=block_id,
                    start=_to_u16(text, start),  # the browser indexes text in UTF-16 units
                    end=_to_u16(text, end),
                    quote=text[start:end],
                    claim=_flat(raw.claim)[:600],
                    claim_type=raw.claim_type,
                    assertion_mode=raw.assertion_mode,
                    attributed_to=(raw.attributed_to or "").strip()[:200] or None,
                    attribution_kind=raw.attribution_kind,
                    central=raw.central,
                    links=links,
                    group=groups.setdefault(key, cid),
                )
            )
            if len(placed) >= max_claims:
                return placed
    return placed


async def extract_claims(
    url: str, title: str, blocks: list[PageBlock], max_claims: int, usage: llm.Usage | None = None
) -> list[Claim]:
    """Extract and place the page's claims. Model spend is added to `usage`, even if the call fails."""
    if PROMPT is None:
        raise llm.LLMError("the extraction prompt is not part of this copy of the code")
    rendered, numbered = render_blocks(blocks)
    prompt = PROMPT.format(max_claims=max_claims, title=_flat(title)[:200], url=url[:300], blocks=rendered)
    result = await llm.generate(prompt, Extraction, system=SYSTEM, thinking="low", usage=usage)
    return place_claims(result.value.claims, blocks, numbered, max_claims)
