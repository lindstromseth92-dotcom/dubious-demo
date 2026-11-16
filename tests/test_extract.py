"""Placing the model's claims on the page. No model calls."""

from dubious.extract import PageBlock, PageLink, RawClaim, _from_u16, _to_u16, place_claims, render_blocks

LABELS = dict(claim_type="x", assertion_mode="x", attribution_kind="x")  # placement does not depend on the values


def raw(**kw) -> RawClaim:
    base = dict(block_id="b0", quote="", claim="") | LABELS
    return RawClaim(**(base | kw))


def js_slice(text: str, start: int, end: int) -> str:
    """What JavaScript's text.slice(start, end) returns: offsets are UTF-16 code units."""
    return text.encode("utf-16-le")[start * 2 : end * 2].decode("utf-16-le")


def test_offsets_are_utf16_units_so_emoji_do_not_shift_the_underline():
    text = "\U0001F489\U0001F6A8 Wake up! Vaccines cause autism in 1 of 36 kids. More text follows here."
    block = PageBlock(id="b0", text=text)
    [c] = place_claims([raw(quote="Vaccines cause autism in 1 of 36 kids.", claim="Vaccines cause autism.")], [block], {}, 30)
    assert c.quote == "Vaccines cause autism in 1 of 36 kids."
    assert js_slice(text, c.start, c.end) == c.quote  # what the browser will underline
    assert text[c.start : c.end] != c.quote  # and not what Python's own indexing would give


def test_utf16_round_trip():
    text = "a\U0001F489béc"
    for i in range(len(text) + 1):
        assert _from_u16(text, _to_u16(text, i)) == i
    assert _to_u16(text, 2) == 3 and _from_u16(text, 99) == len(text)


def test_link_markers_land_after_the_anchor_text_even_after_emoji():
    text = "\U0001F489 Autism has risen sharply, says the CDC, since 1990."
    at_js = len("\U0001F489 Autism has risen sharply, says the CDC".encode("utf-16-le")) // 2
    block = PageBlock(id="b0", text=text, links=[PageLink(text="the CDC", href="https://www.cdc.gov/x", at=at_js)])
    rendered, numbered = render_blocks([block])
    assert "says the CDC{1}, since 1990." in rendered and numbered[1][1].href == "https://www.cdc.gov/x"


def test_quotes_the_model_did_not_copy_faithfully_are_dropped():
    block = PageBlock(id="b0", text="The schedule has tripled since 1983. Measles is contagious.")
    claims = place_claims(
        [raw(quote="The schedule has tripled since 1983.", claim="A"), raw(quote="Something the page never says at all.", claim="B"),
         raw(block_id="b9", quote="Measles is contagious.", claim="C")],
        [block], {}, 30,
    )
    assert [c.claim for c in claims] == ["A"]


def test_overlapping_quotes_are_made_disjoint():
    text = "Vaccines cause autism and the CDC has hidden the evidence for decades, critics say."
    block = PageBlock(id="b0", text=text)
    claims = place_claims(
        [raw(quote="Vaccines cause autism and the CDC has hidden the evidence", claim="A"),
         raw(quote="the CDC has hidden the evidence for decades, critics say.", claim="B")],
        [block], {}, 30,
    )
    assert len(claims) == 2 and claims[0].end <= claims[1].start
    assert claims[1].quote == " for decades, critics say.".strip()


def test_repeats_share_an_assessment_only_when_sourcing_matches():
    blocks = [PageBlock(id="b0", text="Vaccines cause autism, plain and simple."),
              PageBlock(id="b1", text="As one study showed, vaccines cause autism in some children."),
              PageBlock(id="b2", text="Once more: vaccines cause autism, plain and simple.")]
    link = PageLink(text="one study", href="https://journal.example.org/retracted")
    claims = place_claims(
        [raw(block_id="b0", quote="Vaccines cause autism", claim="Vaccines cause autism."),
         raw(block_id="b1", quote="vaccines cause autism in some children", claim="Vaccines cause autism.", cited_links=[1]),
         raw(block_id="b2", quote="vaccines cause autism", claim="Vaccines cause autism.")],
        blocks, {1: ("b1", link)}, 30,
    )
    assert [c.group for c in claims] == ["c1", "c2", "c1"]
    assert claims[1].links == [link]


def test_long_custom_element_names_are_accepted():
    assert PageBlock(id="b0", tag="shreddit-post-text-body-content", text="x").tag == "shreddit-post-te"
