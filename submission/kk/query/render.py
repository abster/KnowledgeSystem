"""Hand-written answer templates and the qualifier phrase map (SPEC 12.3, 12.5, TPL-1 to TPL-7).

Slot values come only from stored fact fields. Every ``render_*`` function returns ``None``
when a slot is missing, blank or unknown (TPL-4), and the same facts always give the same text
(TPL-6). Numbers appear in the source's format (TPL-3).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from contract import Answer
from kk.query.normalise import AmountConstraint, parse_tier_text
from kk.query.validators import extract_numbers
from kk.types import Fact

if TYPE_CHECKING:
    from kk.query.candidates import History

RATE_ATTRIBUTES = ("rate", "dividend_rate", "apy", "apr", "min_to_open", "min_to_earn")

_ATTRIBUTE_LABELS = {
    "rate": "rate",
    "dividend_rate": "dividend rate",
    "apy": "APY",
    "apr": "APR",
    "min_to_open": "minimum to open",
    "min_to_earn": "minimum to earn",
    "fee": "fee",
    "count": "count",
    "address": "address",
    "phone": "phone number",
    "hours": "hours",
    "status": "status",
    "eligibility": "eligibility",
    "prose": "details",
}
_WORD_KEYS = {
    "product": {"purchase": "purchase", "refinance": "refinance"},
    "rate_type": {"fixed": "fixed rate", "arm": "adjustable rate"},
    "loan_type": {"conventional": "conventional", "jumbo": "jumbo"},
}
_BOUND_LABEL = {"as_low_as": " (as low as)"}  # part of the attribute label, not the phrase
_BOUND_TOPIC = {"as_low_as": "as low as"}
# TPL-1: the fixed key order of every phrase.
_KEY_ORDER = (
    "product",
    "rate_type",
    "loan_type",
    "term",
    "model_year",
    "amount_tier",
    "balance_band",
    "bound",
)
_AMOUNT_NOUN = {"amount_tier": "loans", "balance_band": "balances"}
_MODEL_YEAR = re.compile(r"^\d{4}(?:-\d{2}(?:\d{2})?)?$")
_MONTHS_PER_YEAR = 12
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_CHANGE_COUNT_WORDS = {2: "twice", 3: "three times", 4: "four times"}
_REFUSAL_SUFFIX = " on Dupaco's website. Please contact Dupaco."
_FALLBACK_PHRASE = "that information"


def attribute_label(attribute: str) -> str | None:
    """Display words for an attribute id, or ``None`` if the id is not in the vocabulary."""
    return _ATTRIBUTE_LABELS.get(attribute)


# --- qualifier phrases -----------------------------------------------------------------------


def _money(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"${value:,.0f}"
    cents = value.quantize(Decimal("0.01"))
    return f"${cents:,.2f}" if cents == value else f"${value.normalize():,f}"


def _amount_core(constraint: AmountConstraint) -> str | None:
    kind = constraint.kind
    lower, upper = constraint.lower, constraint.upper
    if kind == "point" and lower is not None:
        return f"exactly {_money(lower)}"
    if kind == "cap" and upper is not None:
        return (
            f"{_money(upper)} or less" if constraint.upper_inclusive else f"under {_money(upper)}"
        )
    if kind == "floor" and lower is not None:
        return f"{_money(lower)} or more" if constraint.lower_inclusive else f"over {_money(lower)}"
    if kind == "band" and lower is not None and upper is not None:
        return f"{_money(lower)} to {_money(upper)}"
    return None


def _item(key: str, value: object, *, compact: bool) -> str | None:  # noqa: PLR0911
    """One qualifier as words, or ``None`` if the key or value is not recognised."""
    if key in _WORD_KEYS:
        return _WORD_KEYS[key].get(value) if isinstance(value, str) else None
    if key == "bound":
        if not isinstance(value, str) or value not in _BOUND_LABEL:
            return None
        return _BOUND_TOPIC[value] if compact else ""
    if key == "term":
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None
        if not compact:
            return f"a {value}-month term"
        years = f" ({value // _MONTHS_PER_YEAR}-year)" if value % _MONTHS_PER_YEAR == 0 else ""
        return f"{value}-month{years}"
    if key == "model_year":
        if not isinstance(value, str) or not _MODEL_YEAR.match(value):
            return None
        if compact:
            return value
        return f"{value} model years" if "-" in value else f"{value} model year"
    if key in _AMOUNT_NOUN:
        constraint = parse_tier_text(value) if isinstance(value, str) else None
        core = _amount_core(constraint) if constraint is not None else None
        if core is None:
            return None
        if compact:
            return core
        joiner = "" if core.startswith(("under", "over")) else "of "
        return f"{_AMOUNT_NOUN[key]} {joiner}{core}"
    return None


def _qualifier_items(qualifiers_json: str, *, compact: bool, strict: bool) -> list[str] | None:
    """Items in the TPL-1 order. ``strict`` makes an unknown key or value fail the whole list."""
    try:
        data = json.loads(qualifiers_json)
    except ValueError:
        return None if strict else []
    if not isinstance(data, dict):
        return None if strict else []
    items: list[str] = []
    for key in _KEY_ORDER:
        if key in data:
            item = _item(key, data[key], compact=compact)
            if item is None:
                if strict:
                    return None
                continue
            if item:
                items.append(item)
    if strict and any(key not in _KEY_ORDER for key in data):
        return None
    return items


def _bound_suffix(qualifiers_json: str) -> str:
    """`` (as low as)`` for a fact that states the lowest advertised rate, else nothing."""
    try:
        data = json.loads(qualifiers_json)
    except ValueError:
        return ""
    bound = data.get("bound") if isinstance(data, dict) else None
    return _BOUND_LABEL.get(bound, "") if isinstance(bound, str) else ""


def qualifier_phrase(qualifiers_json: str) -> str | None:
    """TPL-1: the fixed-order phrase for a fact's qualifiers; ``None`` if any is unknown."""
    items = _qualifier_items(qualifiers_json, compact=False, strict=True)
    return None if items is None else ", ".join(items)


def topic_text(entity_label: str, qualifiers_json: str, attribute: str) -> str:
    """SPEC 10.4 topic: entity label, term or tier words, then the attribute label.

    Unknown qualifiers are left out, and a word the label already contains is not repeated.
    """
    items = _qualifier_items(qualifiers_json, compact=True, strict=False) or []
    label_lower = entity_label.lower()
    words = [i for i in items if i.split(" (")[0].lower() not in label_lower]
    attr = attribute_label(attribute) or attribute.replace("_", " ")
    return " ".join(part for part in (entity_label.strip(), *words, attr) if part)


# --- fact templates --------------------------------------------------------------------------


def _usable(fact: Fact, attributes: Sequence[str]) -> bool:
    return (
        fact.attribute in attributes
        and not fact.none_value
        and bool(fact.entity_label.strip())
        and bool(fact.value_text.strip())
    )


def _end(text: str) -> str:
    """End a sentence with a period unless the text already ends with sentence punctuation."""
    return text if text.endswith((".", "!", "?")) else text + "."


def _for(phrase: str) -> str:
    return f" for {phrase}" if phrase else ""


def render_rate(fact: Fact, companion: Fact | None = None) -> str | None:
    """The ``rate`` template; ``companion`` adds the APR of the same row."""
    if not _usable(fact, RATE_ATTRIBUTES):
        return None
    phrase = qualifier_phrase(fact.qualifiers)
    if phrase is None:
        return None
    label = fact.entity_label.strip()
    if companion is None:
        words = f"{attribute_label(fact.attribute)}{_bound_suffix(fact.qualifiers)}"
        return f"The {label} {words}{_for(phrase)} is {fact.value_text}."
    pair = {fact.attribute: fact, companion.attribute: companion}
    same_row = (
        fact.entity == companion.entity
        and fact.qualifiers == companion.qualifiers
        and set(pair) == {"rate", "apr"}
        and _usable(companion, RATE_ATTRIBUTES)
        and not _bound_suffix(fact.qualifiers)
    )
    if not same_row:
        return None
    return (
        f"The {label}{_for(phrase)} is {pair['rate'].value_text} rate / "
        f"{pair['apr'].value_text} APR."
    )


def render_fee(fact: Fact, *, per: str | None = None) -> str | None:
    """The ``fee`` template."""
    if not _usable(fact, ("fee",)):
        return None
    suffix = f" {per.strip()}" if per and per.strip() else ""
    return _end(f"The {fact.entity_label.strip()} fee is {fact.value_text}{suffix}")


def render_count(fact: Fact) -> str | None:
    """The ``count`` template."""
    if not _usable(fact, ("count",)):
        return None
    return _end(f"There are {fact.value_text} {fact.entity_label.strip()}")


def render_address(fact: Fact) -> str | None:
    """The ``address`` template."""
    if not _usable(fact, ("address",)):
        return None
    return _end(f"The {fact.entity_label.strip()} is at {fact.value_text}")


def render_hours(fact: Fact) -> str | None:
    """The ``hours`` template."""
    if not _usable(fact, ("hours",)):
        return None
    return _end(f"{fact.entity_label.strip()} hours are {fact.value_text}")


def _long_date(iso: str) -> tuple[date, str] | None:
    try:
        parsed = date.fromisoformat(iso)
    except ValueError:
        return None
    return parsed, f"{_MONTH_NAMES[parsed.month - 1]} {parsed.day}, {parsed.year}"


def render_status(  # noqa: PLR0913
    entity_label: str,
    *,
    date_iso: str,
    taken_on: str,
    phrase_past: str,
    phrase_future: str,
    yes_when_past: bool,
) -> str | None:
    """The ``status`` template; compares ``date_iso`` with the snapshot's ``taken_on`` (TPL-2).

    A date equal to ``taken_on`` counts as past.
    """
    fact_date = _long_date(date_iso)
    snapshot = _long_date(taken_on)
    label = entity_label.strip()
    if fact_date is None or snapshot is None or not label:
        return None
    passed = fact_date[0] <= snapshot[0]
    phrase = (phrase_past if passed else phrase_future).strip()
    if not phrase:
        return None
    answer = "Yes" if passed == yes_when_past else "No"
    return f"{answer} - {label} {phrase} {fact_date[1]}."


def render_list(intro: str, items: Sequence[str]) -> str | None:
    """The ``list`` template: the intro, then one ``- item`` line per row, in order."""
    if not intro.strip() or not items or any(not item.strip() for item in items):
        return None
    return f"{intro.strip()}:\n" + "\n".join(f"- {item}" for item in items)


def render_quoted_prose(fact: Fact) -> str | None:
    """The ``quoted_prose`` template."""
    if not fact.entity_label.strip() or not fact.quote.strip():
        return None
    return f'{fact.entity_label.strip()}: "{fact.quote}"'


def render_history(
    *, entity_label: str, attribute: str, qualifiers_json: str, history: History
) -> str | None:
    """The ``history`` template (TPL-7); ``None`` unless the history is trusted."""
    words = attribute_label(attribute)
    phrase = qualifier_phrase(qualifiers_json)
    label = entity_label.strip()
    segments = history.segments
    if not history.trusted or not segments or words is None or phrase is None or not label:
        return None
    subject = f"the {label} {words}{_bound_suffix(qualifiers_json)}{_for(phrase)}"
    if len(segments) == 1:
        only = segments[0]
        return (
            f"No - {subject} has been {only.value_text} in every snapshot "
            f"from {only.first_date} to {only.last_date}."
        )
    if len(segments) == 2:  # noqa: PLR2004
        old, new = segments
        return (
            f"Yes - {subject} was {old.value_text} as of {old.last_date} "
            f"and had changed to {new.value_text} by {new.first_date}."
        )
    count = _CHANGE_COUNT_WORDS.get(len(segments) - 1)
    if count is None:
        return None
    lines = [f"- {s.value_text} from {s.first_date} to {s.last_date}" for s in segments]
    return f"Yes - {subject} has changed {count}:\n" + "\n".join(lines)


# --- refusal ---------------------------------------------------------------------------------


def _digits(text: str) -> set[str]:
    return {c for c in text if c.isdigit()}


def refusal_text(entity_label: str | None, question: str) -> str:
    """The safe refusal text (SPEC 12.3): no digit that is not in the question.

    The entity label is named only if every digit in it is also in the question and every
    number in it (value and unit, as the scorer reads it) is also a number of the question.
    """
    label = (entity_label or "").strip()
    safe = (
        label
        and _digits(label) <= _digits(question)
        and extract_numbers(label) <= extract_numbers(question)
    )
    phrase = label if safe else _FALLBACK_PHRASE
    return f"I couldn't find {phrase}{_REFUSAL_SUFFIX}"


def safe_refusal(question: str, entity_label: str | None = None) -> Answer:
    """The safe refusal as an ``Answer``."""
    return Answer(text=refusal_text(entity_label, question), sources=[], confident=False)
