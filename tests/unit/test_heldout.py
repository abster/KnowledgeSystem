"""The held-out question generator (Step 10): deterministic, gold taken from the passage."""

from __future__ import annotations

from kk.types import Chunk
from tests.fixtures.llm import FakeLlm
from tools.heldout import (
    HeldoutItem,
    generate_item,
    is_close,
    is_correct,
    part_of,
    passage_of,
    reason_class,
    select_passages,
)


def _chunk(n: int, text: str, kind: str = "text_paragraph", url: str | None = None) -> Chunk:
    return Chunk(
        chunk_id=f"c{n:03d}",
        url=url or f"https://example.test/p{n % 5}",
        file_name="p",
        title="Page",
        parent_headers=("Page",),
        chunk_type=kind,  # type: ignore[arg-type]
        parent_id=None,
        text=text,
        effective_date=None,
        expiry_date=None,
        source_class="other",
        authority_rank=3,
        fetched_at="x",
        position=n,
    )


def _many() -> list[Chunk]:
    chunks = [
        _chunk(i, f"Paragraph number {i} says the fee is ${i}.00 per month for members.")
        for i in range(60)
    ]
    chunks += [
        _chunk(100 + i, f"List item {i} with enough words to be a passage here", "list_item")
        for i in range(30)
    ]
    chunks += [
        _chunk(200, "short", "text_paragraph"),
        _chunk(201, "Row text that is long enough to count", "table_row"),
    ]
    return chunks


def test_selection_is_deterministic_and_skips_short_chunks_and_rows() -> None:
    first = select_passages(_many(), 20)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in select_passages(_many(), 20)]
    assert len(first) == 20
    assert all(len(c.text) >= 40 and c.chunk_type != "table_row" for c in first)


def test_selection_mixes_paragraphs_and_list_items() -> None:
    kinds = {c.chunk_type for c in select_passages(_many(), 20)}
    assert kinds == {"text_paragraph", "list_item"}


def test_the_split_is_independent_of_the_selection_order() -> None:
    picked = select_passages(_many(), 40)
    parts = {part_of(c.chunk_id) for c in picked}
    assert parts == {"dev", "held"}  # the sampling hash must not decide the part


def test_the_split_is_stable_and_roughly_even() -> None:
    parts = [part_of(f"group-{i}") for i in range(400)]
    assert parts == [part_of(f"group-{i}") for i in range(400)]
    assert 0.35 < parts.count("held") / len(parts) < 0.65


def test_a_generated_item_keeps_only_phrases_found_in_the_passage() -> None:
    passage = "The Elm Street branch lobby is open Monday to Friday, 9 a.m. to 5 p.m."
    llm = FakeLlm(
        lambda _r: {
            "question": "When can I visit the Elm Street office?",
            "must_include": ["9 a.m.", "5 p.m.", "invented phrase"],
        }
    )
    item = generate_item(_chunk(1, passage), passage, llm, "m")
    assert item is not None
    assert item.must_include == ("9 a.m.", "5 p.m.")
    assert item.group == "c001"


def test_an_item_without_a_usable_question_or_phrase_is_dropped() -> None:
    passage = "Some passage that is long enough to be used for a question."
    assert (
        generate_item(
            _chunk(1, passage),
            passage,
            FakeLlm(lambda _r: {"question": "", "must_include": ["x"]}),
            "m",
        )
        is None
    )
    assert (
        generate_item(
            _chunk(1, passage),
            passage,
            FakeLlm(lambda _r: {"question": "Q?", "must_include": ["nope"]}),
            "m",
        )
        is None
    )


def test_the_passage_includes_neighbours_on_the_same_page() -> None:
    chunks = [
        _chunk(i, f"Line {i} of the same section of this page", url="https://example.test/same")
        for i in range(5)
    ]
    text = passage_of(chunks[2], chunks)
    assert "Line 1" in text and "Line 3" in text


def test_correctness_uses_the_scorers_value_aware_matching() -> None:
    item = HeldoutItem("g", "g", "q?", ("3.5%",), "paragraph", "u")
    assert is_correct(item, "The rate is 3.50%.", confident=True)
    assert not is_correct(item, "I couldn't find that.", confident=False)
    assert not is_correct(item, "The rate is 3.6%.", confident=True)


def test_failure_classes_group_refusals_by_stage_and_reason() -> None:
    assert reason_class("slow", "x; slow path answered") == "answered"
    assert (
        reason_class("refusal", "a; slow path refused: not answerable: long text")
        == "not answerable"
    )
    assert (
        reason_class("refusal", "a; slow path refused: validators failed: V1, V2")
        == "validators V1, V2"
    )
    assert reason_class("refusal", "a; slow path refused: call failed: HTTP 429") == "call failed"
    assert reason_class("refusal", "a; slow path unavailable: no API key") == "unavailable"
    assert reason_class("fast", "at or above") == "fast"
    assert reason_class("refusal", "below the relevance floor") == "below floor"


def _item(*phrases: str) -> HeldoutItem:
    return HeldoutItem("g", "g", "q?", tuple(phrases), "paragraph", "u")


def test_close_match_treats_number_words_and_digits_alike() -> None:
    assert is_close(_item("eight steps"), "There are 8 steps.", confident=True)
    assert is_close(_item("10 employees", "$5 each"), "Ten employees gave $5 each.", confident=True)
    assert is_close(_item("Twenty"), "20 students benefited.", confident=True)


def test_close_match_accepts_a_reworded_phrase_but_not_a_different_number() -> None:
    phrase = _item("Are the brakes firm and quiet")
    assert is_close(phrase, "Check that the brakes feel firm and quiet.", confident=True)
    assert not is_close(_item("a 24 month term"), "A 36 month term is offered.", confident=True)
    assert not is_close(_item("eight years"), "It takes 9 years.", confident=True)


def test_close_match_rejects_unrelated_text_and_refusals() -> None:
    assert not is_close(_item("laddering strategy"), "Pick a term that suits you.", confident=True)
    assert not is_close(_item("eight steps"), "There are 8 steps.", confident=False)


def test_close_match_needs_every_phrase() -> None:
    assert not is_close(
        _item("eight steps", "marketing plan"), "There are 8 steps.", confident=True
    )


def test_strict_match_is_unchanged_by_the_close_rule() -> None:
    assert not is_correct(_item("eight steps"), "There are 8 steps.", confident=True)
