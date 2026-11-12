"""Property tests for conflicts, selection and rendering (Step 5): no crashes, stable output."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from scoring import _numbers

from kk.query.candidates import select_siblings
from kk.query.normalise import AmountConstraint
from kk.query.render import qualifier_phrase, refusal_text
from tests.fixtures.facts import make_fact

_TIERS = [
    make_fact(entity="m", attribute="rate", qualifiers={"amount_tier": t}, chunk_id=t)
    for t in ("<=100000", "<=300000", ">300000")
]
_amounts = st.decimals(min_value=0, max_value=10_000_000, places=2)


@given(st.text())
def test_qualifier_phrase_never_raises(text: str) -> None:
    result = qualifier_phrase(text)
    assert result is None or isinstance(result, str)


@given(st.one_of(st.none(), st.text()), st.text())
def test_refusal_never_adds_digits(label: str | None, question: str) -> None:
    text = refusal_text(label, question)
    assert {c for c in text if c.isdigit()} <= {c for c in question if c.isdigit()}
    assert _numbers(text) <= _numbers(question)  # the scorer's own rule for should_refuse


def _constraint(  # noqa: PLR0913, PLR0917
    lower: Decimal,
    upper: Decimal,
    has_lower: bool,
    has_upper: bool,
    lower_incl: bool,
    upper_incl: bool,
) -> AmountConstraint:
    low, high = min(lower, upper), max(lower, upper)
    return AmountConstraint(
        lower=low if has_lower else None,
        lower_inclusive=lower_incl,
        upper=high if has_upper else None,
        upper_inclusive=upper_incl,
    )


_constraints = st.builds(
    _constraint, _amounts, _amounts, st.booleans(), st.booleans(), st.booleans(), st.booleans()
)


@given(_constraints)
def test_select_siblings_never_raises_and_is_deterministic(amount: AmountConstraint) -> None:
    first = select_siblings(_TIERS, amount=amount, qualifiers={})
    second = select_siblings(list(reversed(_TIERS)), amount=amount, qualifiers={})
    assert first.status in {"one", "none", "several"}
    assert (first.status, set(first.facts)) == (second.status, set(second.facts))
    assert first.status != "one" or len(first.facts) == 1
    assert first.status != "none" or first.facts == ()
