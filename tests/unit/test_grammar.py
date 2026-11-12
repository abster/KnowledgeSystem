"""The fast-path pattern registry (SPEC 8.6, 8.7; GR-1 to GR-5).

Skeletons are words plus the slots ``<e>`` (entity), ``<m>`` (measure) and ``<q>`` (qualifier).
Each pattern has at least five positive and five near-miss negative cases written from
shapes, not from any public question.
"""

from __future__ import annotations

import pytest

from kk.query.grammar import PATTERN_NAMES, match_skeleton
from kk.types import Shape

Case = tuple[str, str | None]  # (skeleton, measure attribute)

# name -> (shape, positives, negatives)
CASES: dict[str, tuple[Shape, list[Case], list[Case]]] = {
    "wh_be": (
        "what",
        [
            ("what is the <m> for a <q> <e>", "apr"),
            ("what are the <e> <m>", "rate"),
            ("what's the <m> on the <e> with a <q>", "apy"),
            ("what is the current <m> for <q> and <q> <e>", "rate"),
            ("what is the <e> <m> today", "fee"),
            ("what is the <m> of the <e> in the <q>", "min_to_open"),
            ("what is the <e> <m>", "address"),
            ("what are the <e> <m>", "hours"),
            ("what is the <e> <m>", "eligibility"),
            ("what is the <e> <m> for a <q> loan", "rate"),
        ],
        [
            ("what is the <m> for", "apr"),
            ("what is the <m> for <e> <e>", "apr"),
            ("what is the <m> for about <e>", "apr"),
            ("what is the <m> for <e> please", "apr"),
            ("what is the <m> and <m> for <e>", "apr"),
            ("what is the <e>", None),
            ("what is the <m> for the <e> roughly", "apr"),
            ("what the <m> for <e>", "apr"),
            ("what is the <m> for <e>", "phone"),
        ],
    ),
    "tell_me": (
        "what",
        [
            ("tell me the <m> for a <q> <e>", "apr"),
            ("please tell me the <e> <m>", "rate"),
            ("show me the <m> for the <e>", "apy"),
            ("give me the current <m> for <e> with a <q>", "rate"),
            ("tell me <e> <m>", "fee"),
            ("please show me the <m> of the <e>", "dividend_rate"),
        ],
        [
            ("tell me about the <e> <m>", "rate"),
            ("tell me the <m>", "rate"),
            ("can you tell me the <m> for <e>", "rate"),
            ("tell me the <m> for <e> <e>", "rate"),
            ("tell the <m> for <e>", "rate"),
            ("tell me the <m> for <e> thanks", "rate"),
        ],
    ),
    "how_much": (
        "what",
        [
            ("how much is the <m> for the <e>", "fee"),
            ("how much are the <e> <m>", "fee"),
            ("how much is the <m> for a <e>", "min_to_open"),
            ("how much is the <e> <m> today", "fee"),
            ("how much is the <m> for the <e> with a <q>", "min_to_earn"),
            ("how much is the <m> of the <e>", "fee"),
        ],
        [
            ("how much is the <m> for the <e>", "apr"),
            ("how much is the <m> for the <e>", "address"),
            ("how much does the <m> cost for <e>", "fee"),
            ("how much is the <m>", "fee"),
            ("how much is it for <e>", None),
            ("how much is the <m> for <e> per month", "fee"),
        ],
    ),
    "how_many": (
        "count",
        [
            ("how many <e> are there", None),
            ("how many <e> do you have", None),
            ("how many <e> do you offer", None),
            ("how many <e>", None),
            ("number of <e>", None),
            ("how many of the <e> are there", None),
        ],
        [
            ("how many <e> <e>", None),
            ("how many are there", None),
            ("how many <e> does dupaco have", None),
            ("how many <m> for <e>", "rate"),
            ("how many <e> have <q>", None),
            ("total number of <e>", None),
        ],
    ),
    "where_is": (
        "what",
        [
            ("where is the <e>", None),
            ("where are the <e>", None),
            ("where is the <e> located", None),
            ("where is a <e> located", None),
            ("where is the <q> <e>", None),
            ("where are the <e> located", None),
        ],
        [
            ("where is the <e> <e>", None),
            ("where is the", None),
            ("where is the <m> for <e>", "address"),
            ("where do i find the <e>", None),
            ("where is the <e> located near me", None),
            ("where is the <e> open", None),
        ],
    ),
    "status_yn": (
        "yes_no",
        [
            ("is the <e> open", None),
            ("is the <e> still open", None),
            ("are the <e> open", None),
            ("is <e> still open", None),
            ("is the <q> <e> open", None),
        ],
        [
            ("is the <e> closed", None),
            ("is the <e> open late", None),
            ("is the <e> <e> open", None),
            ("is the open", None),
            ("is the <e> still", None),
            ("was the <e> open", None),
            ("is the <m> for <e> open", "apr"),
        ],
    ),
    "status_when": (
        "date",
        [
            ("when does the <e> open", None),
            ("when does the <e> close", None),
            ("when is the <e> open", None),
            ("when will the <e> close", None),
            ("when did the <e> open", None),
        ],
        [
            ("when does the <e> reopen", None),
            ("when does the <e> <e> close", None),
            ("when does close", None),
            ("when does the <e> close early", None),
            ("when does the <e> open again", None),
            ("when should the <e> close", None),
        ],
    ),
    "list_imperative": (
        "list",
        [
            ("list the <e>", None),
            ("please list the <e>", None),
            ("list all the <e>", None),
            ("list the <m> for the <e>", "rate"),
            ("list all <e>", None),
            ("list the <m> for <e> with a <q>", "fee"),
        ],
        [
            ("list <e> <e>", None),
            ("list the", None),
            ("list the <e> and more", None),
            ("show a list of the <e>", None),
            ("list the <m> for the <e>", "phone"),
            ("lists the <e>", None),
        ],
    ),
    "list_offer": (
        "list",
        [
            ("what <e> do you offer", None),
            ("which <e> do you offer", None),
            ("what <q> <e> do you offer", None),
            ("what the <e> do you offer", None),
            ("which of the <e> do you offer", None),
        ],
        [
            ("what <e> do you have", None),
            ("what <e> do you offer members", None),
            ("what <e> <e> do you offer", None),
            ("what do you offer", None),
            ("what <m> do you offer", "rate"),
            ("what <e> do they offer", None),
        ],
    ),
    "history_h1": (
        "history",
        [
            ("has the <m> for the <e> changed", "apr"),
            ("have the <e> <m> changed", "rate"),
            ("has the <e> changed", None),
            ("did the <e> <m> change", "apy"),
            ("has the <e> <m> for a <q> gone up", "apr"),
            ("did the <e> <m> go down", "rate"),
            ("have the <e> <m> increased", "rate"),
        ],
        [
            ("has the <e> changed recently", None),
            ("did the <e> <m> changed", "rate"),
            ("has the <e> <e> changed", None),
            ("has the changed", None),
            ("has the <e> <m> been changed", "rate"),
            ("has the <e> <m> change", "rate"),
            ("has the <e> <m> changed since", "rate"),
        ],
    ),
    "history_h2": (
        "history",
        [
            ("what was the <m> for the <e> before", "apr"),
            ("what were the <e> <m> previously", "rate"),
            ("which was the <m> of the <e> earlier", "apy"),
            ("what was the <e> <m> for a <q> previously", "apr"),
            ("what was the <e> before", None),
        ],
        [
            ("what was the <m> for the <e>", "apr"),
            ("what was the <m> for <e> long before", "apr"),
            ("what was the <m> for <e> <e> before", "apr"),
            ("what is the <m> for the <e> before", "apr"),
            ("when was the <m> for the <e> before", "apr"),
            ("what was before the <e>", None),
        ],
    ),
}


def _flat(which: int) -> list[tuple[str, Shape, str, str | None]]:
    out = []
    for name, (shape, pos, neg) in CASES.items():
        for skeleton, measure in (pos, neg)[which]:
            out.append((name, shape, skeleton, measure))
    return out


def test_registry_has_the_expected_patterns() -> None:
    assert set(PATTERN_NAMES) == set(CASES)
    assert len(PATTERN_NAMES) == len(set(PATTERN_NAMES))


@pytest.mark.parametrize("name", sorted(CASES))
def test_each_pattern_has_five_positive_and_five_negative_cases(name: str) -> None:
    _, pos, neg = CASES[name]
    assert len(pos) >= 5
    assert len(neg) >= 5


@pytest.mark.parametrize(("name", "shape", "skeleton", "measure"), _flat(0))
def test_positive(name: str, shape: Shape, skeleton: str, measure: str | None) -> None:
    match = match_skeleton(skeleton.split(), shape=shape, measure=measure)
    assert match is not None, skeleton
    assert match.pattern == name
    assert match.shape == shape


@pytest.mark.parametrize(("name", "shape", "skeleton", "measure"), _flat(1))
def test_near_miss_is_not_a_match(
    name: str, shape: Shape, skeleton: str, measure: str | None
) -> None:
    # GR-5: a partial match is a non-match. No pattern of the shape accepts these.
    assert match_skeleton(skeleton.split(), shape=shape, measure=measure) is None, (name, skeleton)


def test_pattern_shape_must_agree_with_the_classified_shape() -> None:
    skeleton = "what is the <m> for the <e>".split()
    assert match_skeleton(skeleton, shape="what", measure="apr") is not None
    for other in ("list", "count", "yes_no", "date", "other"):
        assert match_skeleton(skeleton, shape=other, measure="apr") is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("skeleton", "shape", "measure", "family"),
    [
        ("what is the <m> for the <e>", "what", "apr", "value"),
        ("what is the <m> for the <e>", "what", "min_to_open", "value"),
        ("what is the <m> for the <e>", "what", "fee", "fee"),
        ("what is the <m> for the <e>", "what", "address", "address"),
        ("what is the <m> for the <e>", "what", "hours", "hours"),
        ("what is the <m> for the <e>", "what", "eligibility", "prose"),
        ("how many <e> are there", "count", None, "count"),
        ("is the <e> open", "yes_no", None, "status"),
        ("list the <e>", "list", None, "list"),
        ("has the <e> changed", "history", None, "history"),
    ],
)
def test_family_follows_the_measure(
    skeleton: str, shape: Shape, measure: str | None, family: str
) -> None:
    match = match_skeleton(skeleton.split(), shape=shape, measure=measure)
    assert match is not None
    assert match.family == family


@pytest.mark.parametrize(
    ("skeleton", "shape", "implied"),
    [
        ("where is the <e>", "what", "address"),
        ("is the <e> open", "yes_no", "status"),
        ("when does the <e> close", "date", "status"),
        ("how many <e> are there", "count", "count"),
    ],
)
def test_patterns_without_a_measure_word_imply_one(
    skeleton: str, shape: Shape, implied: str
) -> None:
    match = match_skeleton(skeleton.split(), shape=shape, measure=None)
    assert match is not None
    assert match.measure == implied


def test_cue_words_are_reported_by_token_index() -> None:
    match = match_skeleton("has the <e> changed".split(), shape="history", measure=None)
    assert match is not None
    assert match.cues == frozenset({0, 3})  # "has" is not a neutral word, so it is a cue too
    match = match_skeleton("is the <e> still open".split(), shape="yes_no", measure=None)
    assert match is not None
    assert match.cues == frozenset({3, 4})
    match = match_skeleton("what is the <m> for the <e>".split(), shape="what", measure="apr")
    assert match is not None
    assert match.cues == frozenset()
    match = match_skeleton("what's the <m> for the <e>".split(), shape="what", measure="apr")
    assert match is not None
    assert match.cues == frozenset({0})


def test_empty_and_slotless_skeletons_never_match() -> None:
    assert match_skeleton([], shape="what", measure=None) is None
    assert match_skeleton("what is the".split(), shape="what", measure=None) is None
