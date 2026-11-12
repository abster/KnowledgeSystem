"""Property tests for Step 6: the parser never raises and is deterministic; routing is monotone."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from kk.config import load_config
from kk.query.asof import target_ordinal
from kk.query.confidence import CAP_MARGIN, Signals, confidence, route
from kk.query.grammar import PATTERN_NAMES, match_skeleton
from kk.query.question import parse
from tests.fixtures.facts import SNAPS
from tests.fixtures.questions import LINKER

CFG = load_config().confidence

VOCAB = [
    "what", "is", "the", "apr", "apy", "rate", "rates", "fee", "for", "a", "an", "60-month",
    "five", "year", "years", "month", "auto", "loan", "mortgage", "share", "certificate", "cd",
    "location", "hy-vee", "$100,000", "or", "less", "over", "under", "between", "and", "about",
    "2025", "model", "2024-27", "has", "changed", "previously", "still", "open", "how", "many",
    "list", "do", "you", "offer", "when", "does", "close", "5%", "~", "7/1", "arm", "fixed",
    "jumbo", "?", ",", "'", "tell", "me", "new", "total", "compare",
]  # fmt: skip


@given(st.text())
def test_parse_never_raises_on_any_text(text: str) -> None:
    parsed = parse(text, LINKER, target_ord=3)
    assert parsed.shape in {"history", "count", "yes_no", "date", "list", "what", "other"}


@given(st.lists(st.sampled_from(VOCAB), max_size=14).map(" ".join))
@settings(max_examples=400)
def test_parse_is_deterministic_and_internally_consistent(text: str) -> None:
    a = parse(text, LINKER, target_ord=3)
    b = parse(text, LINKER, target_ord=3)
    assert a == b
    last = 0
    for span in a.consumed:
        assert 0 <= span.start < span.end <= len(a.text)
        assert span.start >= last
        last = span.end
    if a.fast_eligible:
        assert a.covered
        assert not a.uncertain
        assert a.shape != "other"
        assert a.pattern in PATTERN_NAMES
        assert a.entity is not None
        assert a.canonical_key is not None
    else:
        assert a.canonical_key is None
    if not a.covered:
        assert any(r.startswith("residual:") for r in a.reasons)
    assert parse(text, LINKER).canonical_key is None  # no target, no key


@given(
    st.lists(
        st.sampled_from(["what", "is", "the", "<e>", "<m>", "<q>", "for", "a", "changed"]),
        max_size=12,
    )
)
def test_grammar_never_raises_and_cues_are_in_range(tokens: list[str]) -> None:
    for shape in ("what", "list", "count", "yes_no", "date", "history", "other"):
        for measure in (None, "apr", "fee", "address"):
            match = match_skeleton(tokens, shape=shape, measure=measure)  # type: ignore[arg-type]
            if match is not None:
                assert match.pattern in PATTERN_NAMES
                assert all(0 <= i < len(tokens) for i in match.cues)


_unit = st.floats(min_value=-1, max_value=2, allow_nan=False)


@given(
    _unit,
    _unit,
    _unit,
    _unit,
    st.sampled_from(["unique", "strategy_1_2", "strategy_3", "unresolved"]),
    st.booleans(),
)
def test_confidence_is_bounded_and_a_cap_never_reaches_the_fast_path(  # noqa: PLR0913, PLR0917
    sim: float, mar: float, qual: float, ent: float, kind: str, capped: bool
) -> None:
    caps = ("aggregate",) if capped else ()
    c = confidence(Signals(sim, mar, qual, ent, kind), caps, CFG)  # type: ignore[arg-type]
    assert 0.0 <= c <= 1.0
    if capped:
        assert c <= CFG.tau_accept - CAP_MARGIN + 1e-12
        assert route(c, target_ok=True, selection_none_covered=False, cfg=CFG).route != "fast"


@given(st.one_of(st.none(), st.text(), st.integers()))
def test_target_ordinal_never_raises(value: object) -> None:
    result = target_ordinal(value, SNAPS)  # type: ignore[arg-type]
    assert (result.ordinal is None) == (result.reason != "ok")
    if result.ordinal is not None:
        assert 1 <= result.ordinal <= len(SNAPS)
