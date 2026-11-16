"""Claim assessment: the part of Dubious that is not published.

In the full project this module researches one claim with a search-grounded
model call, reads what the page itself cites, labels what was found, and turns
those labels into the 1 to 99 score with fixed arithmetic. The prompts, the
labels and the scoring rules are private. This file is a stand-in: it keeps the
rest of the code importable and shows where the private part plugs in.
"""

import httpx

from . import llm
from .extract import Claim

VERSION = "public-stub"


async def assess_claim(
    claim: Claim,
    *,
    page_url: str,
    page_title: str,
    context: str,
    http: httpx.AsyncClient,
    paragraph: str = "",
    usage: llm.Usage | None = None,
) -> dict:
    """Research and score one claim.

    The real implementation returns a JSON-serialisable dict that the pipeline
    stores and the report page renders. Its keys are `status`, `score`,
    `verdict_label`, `headline`, `reasons`, `breakdown`, `validity`, `evidence`,
    `sourcing`, `page_sources`, `search` and `meta`. Model spend is added to `usage` as it
    happens, so it is counted even when a later step fails.
    """
    raise llm.LLMError("claim assessment is not part of this public copy of the code")
