"""Question understanding (SPEC 8.2 to 8.8): goldens with spans, residuals and fail-closed cases."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kk.query.question import CHANGE_CUES, NEUTRAL_WORDS, parse
from kk.types import ParsedQuestion
from tests.fixtures.questions import LINKER


def P(question: str, target: int | None = None) -> ParsedQuestion:  # noqa: N802
    return parse(question, LINKER, target_ord=target)


# --- word lists (the Step 6 exit review looks at these) ---------------------------------------


def test_neutral_words_are_the_spec_list_without_about() -> None:
    spec = (
        "a an the is are was were be do does did can could will would should what which who "
        "where when how much many about for of on in at to my our your me i we you it its this "
        "that these those please tell give show find there any current currently now right "
        "today latest new rate rates loan loans account accounts card cards fee fees apply "
        "with and or"
    ).split()
    # COV-4 requires "about" to fail coverage, so Step 6 removes it from the COV-3 list.
    # Owner decision (Step 6): the brand word and its possessive never change which fact is meant.
    assert set(NEUTRAL_WORDS) == {w for w in spec if w != "about"} | {"dupaco", "dupaco's"}


def test_about_and_unrecognised_modifiers_are_not_neutral() -> None:
    for word in ("about", "roughly", "around", "approximately", "over", "between", "half"):
        assert word not in NEUTRAL_WORDS


def test_change_cues_are_the_spec_list() -> None:
    assert set(CHANGE_CUES) == {
        "changed", "change", "changes", "still", "used to", "no longer", "any longer", "anymore",
        "since", "history", "previously", "before", "earlier", "ever", "new", "increase",
        "decrease", "went up", "went down", "gone up", "gone down", "closed", "closing",
        "open again",
    }  # fmt: skip


def test_new_is_neutral_and_a_change_cue_so_it_covers_but_never_goes_fast() -> None:
    p = P("What is the rate for a new auto loan?", target=2)
    assert p.covered
    assert p.change_cue
    assert not p.fast_eligible
    assert p.canonical_key is None


# --- a fully understood question --------------------------------------------------------------


def test_value_question_with_term_year_and_measure() -> None:
    p = P("What is the APR for a 60-month auto loan on a 2025 model?", target=3)
    assert p.shape == "what"
    assert p.entity == "auto loan"
    assert p.entity_score == 1.0
    assert p.measure == "apr"
    assert p.qualifier_map == {"term": 60, "model_year": "2025"}
    assert p.amount is None
    assert p.covered
    assert not p.uncertain
    assert p.pattern == "wh_be"
    assert p.fast_eligible
    assert p.canonical_key is not None
    assert not p.change_cue
    assert not p.aggregate
    assert p.reasons == ()


def test_spans_are_in_range_sorted_and_do_not_overlap() -> None:
    p = P("What is the APR for a 60-month auto loan on a 2025 model?")
    last = 0
    for span in p.consumed:
        assert 0 <= span.start < span.end <= len(p.text)
        assert span.start >= last
        last = span.end
    kinds = {s.kind for s in p.consumed}
    assert {"term", "model_year", "measure", "entity"} <= kinds
    for q in p.qualifiers:
        assert p.text[q.start : q.end]  # non-empty slice


def test_residual_holds_only_unconsumed_words() -> None:
    p = P("What is the APR for a 60-month auto loan on a 2025 model?")
    assert p.residual.split() == ["what", "is", "the", "for", "a", "on", "a"]


def test_plural_alias_links_with_the_alias_score() -> None:
    p = P("What are the auto loans rates?")
    assert p.entity == "auto loan"
    assert p.entity_score == 0.7


def test_longest_alias_wins() -> None:
    p = P("How many Hy-Vee locations are there?")
    assert p.entity == "hy vee location"
    assert p.shape == "count"
    assert p.measure == "count"
    assert p.pattern == "how_many"
    assert p.fast_eligible


# --- shape classifier (SPEC 8.3) --------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "shape"),
    [
        ("Has the share certificate APY changed?", "history"),
        ("Have the auto loan rates changed?", "history"),
        ("Did the auto loan APR go up?", "history"),
        ("What was the share certificate APY previously?", "history"),
        ("How many locations are there?", "count"),
        ("Number of locations", "count"),
        ("How many savings accounts do you offer?", "count"),  # rule 2 before rule 5
        ("Is the Hy-Vee location open?", "yes_no"),
        ("Are there any locations?", "yes_no"),
        ("Does the mortgage have fees?", "yes_no"),
        ("Do you offer an auto loan?", "yes_no"),
        ("Can I open a savings account?", "yes_no"),
        ("Was the auto loan rate lower?", "yes_no"),
        ("Will the mortgage rate change?", "yes_no"),
        ("When does the Hy-Vee location close?", "date"),
        ("List the savings accounts", "list"),
        ("What types of savings accounts exist?", "list"),
        ("What savings accounts do you offer?", "list"),
        ("Which accounts do you offer?", "list"),
        ("Show me all the accounts", "list"),
        ("What is the auto loan rate?", "what"),
        ("How much is the fee?", "what"),
        ("Which location is closest?", "what"),
        ("Where is the location?", "what"),
        ("Who runs the location?", "what"),
        ("Tell me the auto loan rate", "what"),
        ("What is the number of locations?", "what"),
        ("Compare auto loan rates", "other"),
        ("Auto loan APR", "other"),
        ("", "other"),
    ],
)
def test_shape_classifier(question: str, shape: str) -> None:
    assert P(question).shape == shape


# --- other shapes reach the grammar -----------------------------------------------------------


def test_status_question_consumes_still() -> None:
    p = P("Is the Hy-Vee location still open?")
    assert (p.shape, p.pattern, p.measure) == ("yes_no", "status_yn", "status")
    assert p.entity == "hy vee location"
    assert not p.change_cue  # CHG-3: the STATUS pattern consumes "still"
    assert p.fast_eligible
    assert {"still", "open"} <= {p.text[s.start : s.end] for s in p.consumed if s.kind == "cue"}


def test_when_question() -> None:
    p = P("When does the Hy-Vee location close?")
    assert (p.shape, p.pattern, p.measure) == ("date", "status_when", "status")
    assert p.fast_eligible


def test_history_questions_use_h1_and_h2() -> None:
    h1 = P("Has the share certificate APY changed?", target=4)
    assert (h1.shape, h1.pattern, h1.measure) == ("history", "history_h1", "apy")
    assert h1.change_cue
    assert h1.fast_eligible
    assert h1.canonical_key is not None
    h2 = P("What was the share certificate APY previously?", target=4)
    assert (h2.shape, h2.pattern) == ("history", "history_h2")
    assert h2.fast_eligible


def test_list_questions() -> None:
    offer = P("What savings accounts do you offer?")
    assert (offer.shape, offer.pattern) == ("list", "list_offer")
    assert offer.fast_eligible
    imperative = P("List all the savings accounts")
    assert (imperative.shape, imperative.pattern) == ("list", "list_imperative")
    assert imperative.fast_eligible
    assert not P("What types of savings accounts exist?").fast_eligible  # "types" is not neutral


def test_where_question_implies_an_address() -> None:
    p = P("Where is the Hy-Vee location located?")
    assert (p.pattern, p.measure) == ("where_is", "address")
    assert p.fast_eligible


# --- amounts, terms, years --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "lower", "upper"),
    [
        ("What is the mortgage rate for a loan of $100,000 or less?", None, Decimal(100000)),
        ("What is the mortgage rate for a loan of up to $50,000?", None, Decimal(50000)),
        ("What is the mortgage rate for a loan of under $100,000?", None, Decimal(100000)),
        ("What is the mortgage rate for a loan of over $250,000?", Decimal(250000), None),
        ("What is the mortgage rate for a loan of at least $5,000?", Decimal(5000), None),
        ("What is the mortgage rate for a $150,000 loan?", Decimal(150000), Decimal(150000)),
    ],
)
def test_amount_constraints_are_recognised_and_covered(
    question: str, lower: Decimal | None, upper: Decimal | None
) -> None:
    p = P(question)
    assert p.amount is not None
    assert (p.amount.lower, p.amount.upper) == (lower, upper)
    assert p.covered
    assert p.fast_eligible
    assert "amount" in {s.kind for s in p.consumed}


def test_amount_strictness_is_kept() -> None:
    p = P("What is the mortgage rate for a loan of under $100,000?")
    assert p.amount is not None
    assert p.amount.upper_inclusive is False


@pytest.mark.parametrize(
    ("question", "months"),
    [
        ("What is the share certificate APY for a five year term?", 60),
        ("What is the share certificate APY for a 5-year term?", 60),
        ("What is the share certificate APY for a 5 yr term?", 60),
        ("What is the share certificate APY for 60 months?", 60),
        ("What is the share certificate APY for a 17-Month term?", 17),
        ("What is the mortgage rate for a 30-Year term?", 360),
    ],
)
def test_terms(question: str, months: int) -> None:
    p = P(question)
    assert p.qualifier_map["term"] == months
    assert p.covered
    assert p.fast_eligible


def test_the_word_term_after_a_term_is_consumed_with_it() -> None:
    p = P("What is the share certificate APY for a 60-month term?")
    term = next(q for q in p.qualifiers if q.key == "term")
    assert p.text[term.start : term.end] == "60-month term"


def test_model_year_range() -> None:
    p = P("What is the auto loan APR for 2024-27 model years?")
    assert p.qualifier_map["model_year"] == "2024-2027"
    p = P("What is the auto loan APR for the 2024 to 2027 models?")
    assert p.qualifier_map["model_year"] == "2024-2027"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is the rate for a 30-year fixed mortgage?", {"term": 360, "rate_type": "fixed"}),
        ("What is the rate for a jumbo mortgage?", {"loan_type": "jumbo"}),
        ("What is the rate for a conventional mortgage?", {"loan_type": "conventional"}),
        ("What is the rate for an adjustable mortgage?", {"rate_type": "arm"}),
        ("What is the rate for a purchase mortgage?", {"product": "purchase"}),
        ("What is the rate for a refinance mortgage?", {"product": "refinance"}),
    ],
)
def test_product_words(question: str, expected: dict[str, str | int]) -> None:
    p = P(question)
    assert p.qualifier_map == expected
    assert p.fast_eligible


def test_conflicting_terms_are_uncertain() -> None:
    p = P("What is the share certificate APY for 12 months or 24 months?")
    assert p.uncertain
    assert "qualifier_conflict:term" in p.reasons
    assert not p.fast_eligible


def test_the_same_term_twice_is_fine() -> None:
    p = P("What is the share certificate APY for a 12 month term, that is 12 months?")
    assert p.qualifier_map["term"] == 12
    assert not p.uncertain


# --- measures ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "measure"),
    [
        ("What is the auto loan rate?", "rate"),
        ("What is the auto loan interest rate?", "rate"),
        ("What are the auto loan rates?", "rate"),
        ("What is the savings account dividend rate?", "dividend_rate"),
        ("What is the share certificate APY?", "apy"),
        ("What is the share certificate annual percentage yield?", "apy"),
        ("What is the auto loan APR?", "apr"),
        ("What is the auto loan annual percentage rate?", "apr"),
        ("What is the auto loan APR rate?", "apr"),  # the generic word yields to the specific one
        ("What is the minimum to open a savings account?", "min_to_open"),
        ("What is the minimum to earn on a savings account?", "min_to_earn"),
        ("What is the auto loan fee?", "fee"),
        ("What is the savings account eligibility?", "eligibility"),
        ("What are the Hy-Vee location hours?", "hours"),
        ("What is the Hy-Vee location address?", "address"),
    ],
)
def test_measure_words(question: str, measure: str) -> None:
    p = P(question)
    assert p.measure == measure
    assert p.fast_eligible


def test_two_different_measures_are_uncertain() -> None:
    p = P("What is the share certificate APY and APR?")
    assert p.uncertain
    assert "measure_ambiguous" in p.reasons
    assert not p.fast_eligible


def test_a_question_with_no_measure_has_no_value_pattern() -> None:
    p = P("What is the auto loan?")
    assert p.measure is None
    assert p.pattern is None
    assert not p.fast_eligible


# --- fail closed (the Step 6 set) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "token"),
    [
        ("What is the auto loan rate over 60 months?", "over"),
        ("What is the auto loan rate between 2 and 5 years?", "between"),
        ("What is the auto loan APR for about 60 months?", "about"),
        ("What is the auto loan rate for roughly 60 months?", "roughly"),
        ("What is the auto loan rate for around 60 months?", "around"),
        ("What is the auto loan rate for approximately 60 months?", "approximately"),
        ("What is the auto loan rate for a year and a half?", "half"),
        ("What is the auto loan APR in 2022?", "2022"),
        ("What is the mortgage rate for roughly $50,000?", "roughly"),
        ("What is the mortgage rate for ~$50,000?", "~"),
        ("What is the auto loan rate for 5%?", "%"),
        ("What is the rate for a 7/1 ARM mortgage?", "7"),
        ("What is the auto loan rate for 60 months or so?", "so"),
    ],
)
def test_unexplained_words_fail_the_coverage_check(question: str, token: str) -> None:
    p = P(question, target=3)
    assert not p.covered
    assert f"residual:{token}" in p.reasons
    assert not p.fast_eligible
    assert p.canonical_key is None


def test_ambiguous_alias_is_uncertain() -> None:
    p = P("What is the CD APY?")
    assert p.uncertain
    assert "entity_ambiguous" in p.reasons
    assert p.entity is None
    assert set(p.entity_candidates) == {"share certificate", "certificate of deposit"}
    assert not p.fast_eligible


def test_two_entities_are_ambiguous() -> None:
    p = P("What is the rate for an auto loan and a mortgage?")
    assert p.uncertain
    assert "entity_ambiguous" in p.reasons
    assert not p.fast_eligible


def test_unknown_entity_gives_no_fast_path() -> None:
    p = P("What is the APR for a boat loan?")
    assert p.entity is None
    assert p.entity_score == 0.0
    assert not p.fast_eligible


def test_a_date_in_the_text_is_never_used_and_fails_coverage() -> None:
    p = P("What was the auto loan APR on September 25, 2026?", target=3)
    assert not p.covered
    assert not p.fast_eligible
    assert p.canonical_key is None
    p = P("What is the auto loan APR on 9/25/2026?", target=3)
    assert not p.covered
    assert p.qualifier_map == {}  # no date or year qualifier appears


def test_change_cue_without_a_history_pattern() -> None:
    p = P("Was the auto loan rate higher before?")
    assert p.change_cue
    assert not p.fast_eligible
    p = P("Is the mortgage rate still lower?")
    assert p.change_cue


@pytest.mark.parametrize(
    "question",
    [
        "Compare auto loan rates",
        "What is the total of the auto loan fees?",
        "What is the average mortgage rate?",
    ],
)
def test_aggregates_are_flagged_and_never_fast(question: str) -> None:
    p = P(question)
    assert p.aggregate
    assert not p.fast_eligible


# --- canonical key (SPEC 8.8) -----------------------------------------------------------------


def test_paraphrases_with_the_same_slots_share_a_key() -> None:
    a = P("What is the APR for a 60-month auto loan?", target=3)
    b = P("Tell me the auto loan APR for a 60 month term", target=3)
    c = P("What's the auto loan APR for a five year term?", target=3)
    assert a.canonical_key is not None
    assert a.canonical_key == b.canonical_key == c.canonical_key


def test_key_changes_with_target_ordinal_entity_measure_and_qualifiers() -> None:
    base = P("What is the APR for a 60-month auto loan?", target=3)
    keys = {
        base.canonical_key,
        P("What is the APR for a 60-month auto loan?", target=4).canonical_key,
        P("What is the APY for a 60-month share certificate?", target=3).canonical_key,
        P("What is the rate for a 60-month auto loan?", target=3).canonical_key,
        P("What is the APR for a 36-month auto loan?", target=3).canonical_key,
        P("What is the APR for a auto loan?", target=3).canonical_key,
    }
    assert None not in keys
    assert len(keys) == 6


def test_key_does_not_depend_on_qualifier_order() -> None:
    a = P("What is the APR for a 60-month auto loan on a 2025 model?", target=3)
    b = P("What is the APR for a 2025 model auto loan with a 60-month term?", target=3)
    assert a.canonical_key == b.canonical_key


def test_no_key_without_a_target_or_when_not_fast() -> None:
    assert P("What is the APR for a 60-month auto loan?").canonical_key is None
    assert P("What is the CD APY?", target=3).canonical_key is None


def test_amount_is_part_of_the_key() -> None:
    a = P("What is the mortgage rate for a loan of $100,000 or less?", target=2)
    b = P("What is the mortgage rate for a loan of under $100,000?", target=2)
    c = P("What is the mortgage rate for a loan of $100,000 or less?", target=2)
    assert a.canonical_key == c.canonical_key
    assert a.canonical_key != b.canonical_key


# --- determinism ------------------------------------------------------------------------------


def test_parse_is_deterministic() -> None:
    q = "What is the APR for a 60-month auto loan on a 2025 model?"
    assert P(q, target=2) == P(q, target=2)


def test_case_and_unicode_do_not_change_the_parse() -> None:
    a = P("WHAT IS THE APR FOR A 60-MONTH AUTO LOAN?", target=2)
    b = P("what is the apr for a 60–month  auto loan ?", target=2)  # en dash, extra spaces
    assert a.canonical_key == b.canonical_key
    assert a.canonical_key is not None


@pytest.mark.parametrize(
    "question",
    [
        "What is the APR for a Dupaco 60-month auto loan?",
        "What is the Dupaco auto loan APR?",
        "What is the APR on Dupaco's auto loan?",
        "What is Dupaco's auto loan APR for 60 months?",
    ],
)
def test_the_brand_word_does_not_block_the_fast_path(question: str) -> None:
    p = P(question, target=2)
    assert p.covered
    assert p.fast_eligible
    assert p.entity == "auto loan"


def test_the_brand_word_adds_nothing_to_the_key() -> None:
    a = P("What is the auto loan APR for 60 months?", target=2)
    b = P("What is Dupaco's auto loan APR for 60 months?", target=2)
    assert a.canonical_key == b.canonical_key is not None
