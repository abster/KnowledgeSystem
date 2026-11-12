"""Template, qualifier phrase and refusal goldens (SPEC 12.3, 12.5, TPL-1 to TPL-7; Step 5)."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from contract import Answer
from kk.query.candidates import History, Segment
from kk.query.render import (
    attribute_label,
    qualifier_phrase,
    refusal_text,
    render_address,
    render_count,
    render_fee,
    render_history,
    render_hours,
    render_list,
    render_quoted_prose,
    render_rate,
    render_status,
    safe_refusal,
    topic_text,
)
from kk.types import Fact
from tests.fixtures.facts import encode, make_fact

# --- attribute labels ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attribute", "label"),
    [
        ("rate", "rate"),
        ("dividend_rate", "dividend rate"),
        ("apy", "APY"),
        ("apr", "APR"),
        ("min_to_open", "minimum to open"),
        ("min_to_earn", "minimum to earn"),
        ("fee", "fee"),
        ("count", "count"),
        ("address", "address"),
        ("phone", "phone number"),
        ("hours", "hours"),
        ("status", "status"),
        ("eligibility", "eligibility"),
        ("prose", "details"),
    ],
)
def test_attribute_labels_cover_the_vocabulary(attribute: str, label: str) -> None:
    assert attribute_label(attribute) == label


def test_unknown_attribute_has_no_label() -> None:
    assert attribute_label("colour") is None


# --- qualifier phrase (TPL-1) ----------------------------------------------------------------


def _q(**kw: str | int) -> str:
    return encode(dict(kw))


@pytest.mark.parametrize(
    ("qualifiers", "phrase"),
    [
        ({}, ""),
        ({"term": 60}, "a 60-month term"),
        ({"term": 17}, "a 17-month term"),
        ({"term": 60, "model_year": "2024-27"}, "a 60-month term, 2024-27 model years"),
        ({"model_year": "2025"}, "2025 model year"),
        ({"amount_tier": "<=100000"}, "loans of $100,000 or less"),
        ({"amount_tier": "<100000"}, "loans under $100,000"),
        ({"amount_tier": ">=100000"}, "loans of $100,000 or more"),
        ({"amount_tier": ">100000"}, "loans over $100,000"),
        ({"amount_tier": "=100000"}, "loans of exactly $100,000"),
        ({"amount_tier": "25000-99999.99"}, "loans of $25,000 to $99,999.99"),
        ({"balance_band": ">=25000"}, "balances of $25,000 or more"),
        ({"balance_band": "0.01-4999.99"}, "balances of $0.01 to $4,999.99"),
        ({"product": "refinance"}, "refinance"),
        ({"rate_type": "arm"}, "adjustable rate"),
        ({"loan_type": "jumbo"}, "jumbo"),
        ({"term": 60, "bound": "as_low_as"}, "a 60-month term"),  # the bound is in the label
        (
            {
                "product": "purchase",
                "rate_type": "fixed",
                "loan_type": "conventional",
                "term": 360,
                "amount_tier": "<=100000",
            },
            "purchase, fixed rate, conventional, a 360-month term, loans of $100,000 or less",
        ),
    ],
)
def test_qualifier_phrase(qualifiers: dict[str, str | int], phrase: str) -> None:
    assert qualifier_phrase(encode(qualifiers)) == phrase


@pytest.mark.parametrize(
    "bad",
    [
        '{"colour":"red"}',
        '{"amount_tier":"bogus"}',
        '{"term":"sixty"}',
        '{"model_year":"nineteen"}',
        '{"rate_type":"floating"}',
        "not json",
        "[1, 2]",
    ],
)
def test_qualifier_phrase_unknown_input_has_no_phrase(bad: str) -> None:
    assert qualifier_phrase(bad) is None


def test_qualifier_phrase_ignores_key_order_in_the_json() -> None:
    a = json.dumps({"term": 60, "model_year": "2024-27"})
    b = json.dumps({"model_year": "2024-27", "term": 60})
    assert qualifier_phrase(a) == qualifier_phrase(b)


# --- topic -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "qualifiers", "attribute", "topic"),
    [
        ("6-month certificate", {"term": 6}, "apy", "6-month certificate APY"),
        ("Certificate", {"term": 6}, "apy", "Certificate 6-month APY"),
        (
            "Auto Loan",
            {"term": 60, "model_year": "2024-27"},
            "apr",
            "Auto Loan 60-month (5-year) 2024-27 APR",
        ),
        (
            "Mortgage",
            {"term": 360, "amount_tier": "<=100000"},
            "rate",
            "Mortgage 360-month (30-year) $100,000 or less rate",
        ),
        ("Share Savings", {"balance_band": ">=25000"}, "apy", "Share Savings $25,000 or more APY"),
        (
            "Mortgage",
            {"product": "purchase", "rate_type": "fixed", "loan_type": "conventional"},
            "rate",
            "Mortgage purchase fixed rate conventional rate",
        ),
        ("Lost card", {}, "fee", "Lost card fee"),
        ("X", {"colour": "red"}, "rate", "X rate"),
    ],
)
def test_topic_text(
    label: str, qualifiers: dict[str, str | int], attribute: str, topic: str
) -> None:
    assert topic_text(label, encode(qualifiers), attribute) == topic


# --- rate ------------------------------------------------------------------------------------


def test_rate_without_and_with_qualifiers() -> None:
    plain = make_fact(attribute="apr", label="Auto Loan", value="5.39%")
    tiered = make_fact(
        attribute="apr",
        label="Auto Loan",
        value="5.39%",
        qualifiers={"term": 60, "model_year": "2024-27"},
    )
    assert render_rate(plain) == "The Auto Loan APR is 5.39%."
    assert render_rate(tiered) == (
        "The Auto Loan APR for a 60-month term, 2024-27 model years is 5.39%."
    )


def test_rate_with_companion_shows_both_in_fixed_order() -> None:
    quals: dict[str, str | int] = {"product": "purchase", "term": 360, "amount_tier": "<=100000"}
    rate = make_fact(attribute="rate", label="Mortgage", value="7.13%", qualifiers=quals)
    apr = make_fact(attribute="apr", label="Mortgage", value="7.27%", qualifiers=quals)
    expected = (
        "The Mortgage for purchase, a 360-month term, loans of $100,000 or less "
        "is 7.13% rate / 7.27% APR."
    )
    assert render_rate(rate, apr) == expected
    assert render_rate(apr, rate) == expected


def test_rate_companion_must_be_the_other_half_of_the_same_row() -> None:
    quals: dict[str, str | int] = {"term": 60}
    rate = make_fact(attribute="rate", value="7.13%", qualifiers=quals)
    other_row = make_fact(attribute="apr", value="7.27%", qualifiers={"term": 36})
    not_apr = make_fact(attribute="apy", value="7.27%", qualifiers=quals)
    assert render_rate(rate, other_row) is None
    assert render_rate(rate, not_apr) is None


def test_rate_other_rate_like_attributes() -> None:
    f = make_fact(
        attribute="min_to_open",
        label="Business Checking",
        value="$25.00",
        qualifiers={"balance_band": ">=25000"},
    )
    assert render_rate(f) == (
        "The Business Checking minimum to open for balances of $25,000 or more is $25.00."
    )
    assert render_rate(make_fact(attribute="apy", label="Certificate", value="3.50%")) == (
        "The Certificate APY is 3.50%."
    )


def test_rate_as_low_as_wording() -> None:
    f = make_fact(
        attribute="apr",
        label="Auto Loan",
        value="5.39%",
        qualifiers={"term": 60, "bound": "as_low_as"},
    )
    assert render_rate(f) == "The Auto Loan APR (as low as) for a 60-month term is 5.39%."


# --- fee, count, address, hours --------------------------------------------------------------


def test_simple_templates() -> None:
    fee = make_fact(attribute="fee", label="Lost debit card replacement", value="$2.50")
    assert render_fee(fee) == "The Lost debit card replacement fee is $2.50."
    assert (
        render_fee(fee, per="per month")
        == "The Lost debit card replacement fee is $2.50 per month."
    )
    count = make_fact(attribute="count", label="branch office locations", value="22")
    assert render_count(count) == "There are 22 branch office locations."
    address = make_fact(
        attribute="address", label="Galena Branch", value="11375 Oldenburg Lane, Galena, IL 61036"
    )
    assert (
        render_address(address) == "The Galena Branch is at 11375 Oldenburg Lane, Galena, IL 61036."
    )
    hours = make_fact(attribute="hours", label="Drive-up", value="Monday-Friday 8 a.m.-6 p.m.")
    assert render_hours(hours) == "Drive-up hours are Monday-Friday 8 a.m.-6 p.m."


# --- status (TPL-2) --------------------------------------------------------------------------


def _status(date_iso: str, taken_on: str, *, yes_when_past: bool = False) -> str | None:
    return render_status(
        "Hy-Vee branch",
        date_iso=date_iso,
        taken_on=taken_on,
        phrase_past="was scheduled to close",
        phrase_future="is scheduled to close",
        yes_when_past=yes_when_past,
    )


def test_status_compares_with_the_snapshot_date_not_the_clock() -> None:
    assert _status("2026-03-31", "2026-09-28") == (
        "No - Hy-Vee branch was scheduled to close March 31, 2026."
    )
    assert _status("2026-12-31", "2026-09-28") == (
        "Yes - Hy-Vee branch is scheduled to close December 31, 2026."
    )
    assert _status("2026-03-31", "2026-03-31") == (
        "No - Hy-Vee branch was scheduled to close March 31, 2026."
    )
    assert _status("2026-03-31", "2026-09-28", yes_when_past=True) == (
        "Yes - Hy-Vee branch was scheduled to close March 31, 2026."
    )


def test_status_with_an_invalid_date_has_no_template() -> None:
    assert _status("soon", "2026-09-28") is None
    assert _status("2026-03-31", "later") is None


# --- list, quoted prose ----------------------------------------------------------------------


def test_list_keeps_order() -> None:
    assert render_list("Credit cards", ["B item.", "A item."]) == (
        "Credit cards:\n- B item.\n- A item."
    )
    assert render_list("Credit cards", []) is None
    assert render_list("", ["x"]) is None


def test_quoted_prose() -> None:
    f = make_fact(
        attribute="prose", label="Membership", value="x", quote="People who live or work here."
    )
    assert render_quoted_prose(f) == 'Membership: "People who live or work here."'
    assert render_quoted_prose(make_fact(attribute="prose", quote="")) is None


# --- history (TPL-7) -------------------------------------------------------------------------


def _seg(value: str, first: str, last: str, n: int = 1) -> Segment:
    return Segment(
        value_text=value, first_date=first, last_date=last, first_ordinal=n, last_ordinal=n
    )


def _history(*segments: Segment, trusted: bool = True) -> History:
    return History(segments=segments, trusted=trusted, reasons=() if trusted else ("gap",))


def _render(history: History, quals: dict[str, str | int] | None = None) -> str | None:
    return render_history(
        entity_label="17-month certificate",
        attribute="apy",
        qualifiers_json=encode(quals or {}),
        history=history,
    )


def test_history_changed_once() -> None:
    h = _history(
        _seg("3.50%", "2026-05-12", "2026-06-09"), _seg("3.90%", "2026-08-27", "2026-09-28")
    )
    assert _render(h) == (
        "Yes - the 17-month certificate APY was 3.50% as of 2026-06-09 "
        "and had changed to 3.90% by 2026-08-27."
    )
    assert _render(h, {"term": 60}) == (
        "Yes - the 17-month certificate APY for a 60-month term was 3.50% as of 2026-06-09 "
        "and had changed to 3.90% by 2026-08-27."
    )


def test_history_keeps_the_as_low_as_label() -> None:
    h = _history(_seg("5.39%", "2026-05-12", "2026-09-28"))
    text = render_history(
        entity_label="Auto Loan",
        attribute="apr",
        qualifiers_json=encode({"term": 60, "bound": "as_low_as"}),
        history=h,
    )
    assert text == (
        "No - the Auto Loan APR (as low as) for a 60-month term has been 5.39% "
        "in every snapshot from 2026-05-12 to 2026-09-28."
    )


def test_history_changed_several_times_uses_words_for_the_count() -> None:
    s = [
        _seg("3.50%", "2026-05-12", "2026-05-19"),
        _seg("3.90%", "2026-06-09", "2026-08-27"),
        _seg("3.50%", "2026-09-28", "2026-09-28"),
    ]
    assert _render(_history(*s)) == (
        "Yes - the 17-month certificate APY has changed twice:\n"
        "- 3.50% from 2026-05-12 to 2026-05-19\n"
        "- 3.90% from 2026-06-09 to 2026-08-27\n"
        "- 3.50% from 2026-09-28 to 2026-09-28"
    )
    four = _render(_history(*s, _seg("4.00%", "2026-10-01", "2026-10-02")))
    assert four is not None and "has changed three times:" in four
    five = _render(
        _history(
            *s, _seg("4.00%", "2026-10-01", "2026-10-02"), _seg("4.10%", "2026-11-01", "2026-11-02")
        )
    )
    assert five is not None and "has changed four times:" in five


def test_history_unchanged() -> None:
    h = _history(_seg("3.50%", "2026-05-12", "2026-09-28"))
    assert _render(h) == (
        "No - the 17-month certificate APY has been 3.50% in every snapshot "
        "from 2026-05-12 to 2026-09-28."
    )


def test_history_untrusted_or_empty_has_no_template() -> None:
    assert _render(_history(_seg("3.50%", "2026-05-12", "2026-09-28"), trusted=False)) is None
    assert _render(_history()) is None


# --- refusal (SPEC 12.3) ---------------------------------------------------------------------

SUFFIX = " on Dupaco's website. Please contact Dupaco."


def _digits(text: str) -> set[str]:
    return {c for c in text if c.isdigit()}


def test_refusal_text_variants() -> None:
    assert refusal_text(None, "What is the rate?") == "I couldn't find that information" + SUFFIX
    assert refusal_text("", "What is the rate?") == "I couldn't find that information" + SUFFIX
    assert refusal_text("Share Certificate", "What is the CD rate?") == (
        "I couldn't find Share Certificate" + SUFFIX
    )


def test_refusal_never_adds_a_digit_the_question_lacks() -> None:
    question = "What is the six month CD rate?"
    text = refusal_text("6-month certificate", question)
    assert text == "I couldn't find that information" + SUFFIX
    assert _digits(text) <= _digits(question)
    assert (
        refusal_text("7/1 ARM", "What is the 7/1 ARM rate?") == "I couldn't find 7/1 ARM" + SUFFIX
    )


def test_safe_refusal_answer() -> None:
    answer = safe_refusal("What is the rate?", "Share Certificate")
    assert isinstance(answer, Answer)
    assert (answer.confident, answer.sources, answer.conflict_note) == (False, [], None)
    assert answer.text == refusal_text("Share Certificate", "What is the rate?")
    assert safe_refusal("What is the rate?").text == "I couldn't find that information" + SUFFIX


# --- cross-cutting ---------------------------------------------------------------------------

Renderer = Callable[[Fact], str | None]
TEMPLATES: list[tuple[Renderer, str, str]] = [
    (render_rate, "apr", "Auto Loan"),
    (render_fee, "fee", "Card fee"),
    (render_count, "count", "branches"),
    (render_address, "address", "Branch"),
    (render_hours, "hours", "Drive-up"),
]


@pytest.mark.parametrize(("render", "attribute", "label"), TEMPLATES)
def test_no_template_renders_with_a_missing_slot(
    render: Renderer, attribute: str, label: str
) -> None:
    ok = make_fact(attribute=attribute, label=label, value="5.39%")
    assert render(ok) is not None
    assert render(make_fact(attribute=attribute, label="", value="5.39%")) is None
    assert render(make_fact(attribute=attribute, label=label, value="")) is None
    assert render(make_fact(attribute=attribute, label=label, value="—", none=True)) is None
    assert render(make_fact(attribute="prose", label=label, value="5.39%")) is None


@pytest.mark.parametrize(("render", "attribute", "label"), TEMPLATES)
def test_same_facts_give_the_same_text(render: Renderer, attribute: str, label: str) -> None:
    fact = make_fact(attribute=attribute, label=label, value="5.39%", qualifiers={"term": 60})
    assert render(fact) == render(fact)


def test_rate_rejects_non_rate_attributes_and_unknown_qualifiers() -> None:
    assert render_rate(make_fact(attribute="address", value="1 Main St")) is None
    assert (
        render_rate(make_fact(attribute="apr", value="5.39%", qualifiers={"colour": "red"})) is None
    )
