from dubious.textmatch import locate, normalize

RAW = (
    "  Intro text.\n   Vaccines  “cause” autism — says   nobody credible. "
    "Second sentence here, with more words!  Third. Fourth one ends the block."
)


def span(quote: str) -> str | None:
    hit = locate(quote, RAW)
    return RAW[hit[0] : hit[1]] if hit else None


def test_exact_substring_keeps_raw_offsets():
    assert span("Intro text.") == "Intro text."
    start, end = locate("Third.", RAW)
    assert RAW[start:end] == "Third."


def test_whitespace_and_typography_are_tolerated():
    assert span('Vaccines "cause" autism - says nobody credible.') == (
        "Vaccines  “cause” autism — says   nobody credible."
    )


def test_case_and_trailing_punctuation():
    assert span('vaccines "cause" autism - says nobody credible') == (
        "Vaccines  “cause” autism — says   nobody credible"
    )


def test_elided_middle():
    assert span("Second sentence here ... more words!") == "Second sentence here, with more words!"


def test_light_paraphrase_falls_back_to_best_sentence():
    assert span("Second sentence here with some more words!") == "Second sentence here, with more words!"


def test_missing_quote_is_none():
    assert locate("nothing like this appears anywhere", RAW) is None
    assert locate("", RAW) is None
    assert locate("anything", "") is None


def test_nbsp_between_sentences():
    assert span("Third. Fourth one ends the block.") == "Third. Fourth one ends the block."


def test_normalize_index_map_points_at_raw_characters():
    norm, idx = normalize(RAW)
    assert len(norm) == len(idx)
    for n, i in zip(norm, idx, strict=True):
        if n != " ":
            assert RAW[i] == n or RAW[i] in "“”—"
