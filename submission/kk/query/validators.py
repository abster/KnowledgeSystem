"""Validators V1 to V5 (SPEC 14), run on both paths after generation.

Numbers are extracted with the scorer's own rule (value and unit), copied here so the
submission does not import the harness; ``tests/unit/test_validators.py`` checks the copy
against ``harness/scoring.py`` so any drift fails the build.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from contract import Answer

Number = tuple[str, float]

# Same expression and rounding as ``harness/scoring.py`` (``NUM_RE`` and ``_numbers``).
_NUM_RE = re.compile(r"(\$)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|percent)?", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")
_ROUND_PLACES = 4
# "(1)", "(2)" and a line that starts "3." or "4)" number the items of a list; they hold no value.
# An inline "1)" counts only at the start or after ": ", ". ", "; ", so "(ages 18 to 24)" stays.
_LIST_MARKER = re.compile(
    r"\(\d{1,2}\)|(?m:^[ \t]*\d{1,2}[.)](?=\s))|(?:(?<=[.:;!?] ))\d{1,2}\)(?=\s)"
)
_SPELLED = {
    word: float(value)
    for value, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
_SPELLED.update({"thirty": 30.0, "forty": 40.0, "fifty": 50.0, "sixty": 60.0, "seventy": 70.0})
_SPELLED.update({"eighty": 80.0, "ninety": 90.0, "hundred": 100.0})
_WORD = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class CitedEvidence:
    """One piece of evidence an answer cites: the chunks it stands for and the numbers it holds."""

    chunk_ids: frozenset[str]
    numbers: frozenset[Number]


@dataclass(frozen=True)
class ValidationContext:
    """What the validators compare an answer against.

    ``allowed`` is the V1 set, ``losers``, ``winners``, ``loser_scopes`` and ``cited`` feed V2,
    ``evidence_urls`` V4 and
    ``quotes`` (quote, chunk text) V5. ``relax_stale`` skips V2 for history answers, where
    earlier values are the point. ``refusal`` marks an answer that must be a safe refusal.
    """

    question: str
    allowed: frozenset[Number] = frozenset()
    losers: frozenset[Number] = frozenset()
    winners: frozenset[Number] = frozenset()
    loser_scopes: Mapping[Number, frozenset[str]] = field(default_factory=dict)
    cited: Sequence[CitedEvidence] = ()
    evidence_urls: frozenset[str] = frozenset()
    quotes: Sequence[tuple[str, str]] = field(default_factory=tuple)
    refusal: bool = False
    relax_stale: bool = False


@dataclass(frozen=True)
class ValidationResult:
    """``failed`` holds the ids (``V1`` ...) of every check that failed, in order."""

    ok: bool
    failed: tuple[str, ...]


def extract_numbers(text: str) -> set[Number]:
    """The scorer's number extraction: ``(unit, value)`` pairs, unit ``$``, ``%`` or empty."""
    found: set[Number] = set()
    for m in _NUM_RE.finditer(text or ""):
        unit = "$" if m.group(1) else ("%" if m.group(4) else "")
        value = float(m.group(2).replace(",", "") + ("." + m.group(3) if m.group(3) else ""))
        found.add((unit, round(value, _ROUND_PLACES)))
    return found


def numbers_of(texts: Iterable[str]) -> frozenset[Number]:
    """Union of ``extract_numbers`` over several texts."""
    found: set[Number] = set()
    for text in texts:
        found |= extract_numbers(text)
    return frozenset(found)


def spelled_numbers(texts: Iterable[str]) -> frozenset[Number]:
    """Numbers written as words ("eight", "Twenty") in ``texts``, as bare values.

    Used only to ground a digit in an answer ("8 years") that the evidence spells out. It is not
    applied to answers, so a word such as "one" in a sentence never needs grounding.
    """
    found: set[Number] = set()
    for text in texts:
        for word in _WORD.findall((text or "").lower()):
            if word in _SPELLED:
                found.add(("", _SPELLED[word]))
    return frozenset(found)


def amount_numbers(value: Decimal) -> frozenset[Number]:
    """A money amount as both ``("$", v)`` and ``("", v)``, as an answer may print either."""
    number = round(float(value), _ROUND_PLACES)
    return frozenset({("$", number), ("", number)})


def check_grounding(text: str, allowed: frozenset[Number]) -> bool:
    """V1: every number in ``text`` is allowed.

    A number matches by value and unit. A bare number (no ``$`` or ``%``) also matches by
    value alone, as in the scorer, because an answer prints "60" for a 60-month term.
    """
    values = {value for _, value in allowed}
    return all(
        (unit, value) in allowed or (unit == "" and value in values)
        for unit, value in extract_numbers(_LIST_MARKER.sub(" ", text))
    )


def _held_by(number: Number, cited: CitedEvidence) -> bool:
    """True if the cited evidence prints ``number`` (a bare number matches by value alone)."""
    if number in cited.numbers:
        return True
    return number[0] == "" and any(value == number[1] for _, value in cited.numbers)


def check_stale(
    text: str,
    losers: frozenset[Number],
    winners: frozenset[Number],
    scopes: Mapping[Number, frozenset[str]] | None = None,
    cited: Sequence[CitedEvidence] = (),
) -> bool:
    """V2: no loser number that is not also a winner number for some in-scope key.

    A loser belongs to the key whose value replaced it. With a ``scopes`` entry (the chunks that
    replaced it) the number is excused only when the answer does not cite any of those chunks
    and cited evidence from elsewhere prints it: then it is another fact's number that happens
    to equal a stale one (0.00% on two different products). Without a scope, with an empty
    scope, or when no cited evidence prints the number, it stays a violation (fail closed).
    """
    for number in extract_numbers(_LIST_MARKER.sub(" ", text)):
        if number not in losers or number in winners:
            continue
        scope = (scopes or {}).get(number)
        if not scope:
            return False
        if any(item.chunk_ids & scope for item in cited):
            return False
        if not any(_held_by(number, item) for item in cited):
            return False
    return True


def check_refusal(answer: Answer, question: str) -> bool:
    """V3: not confident, no sources, and no number or digit that the question lacks.

    The scorer checks numbers (value and unit); SPEC 12.3 says digits. Both are required.
    """
    if answer.confident or answer.sources:
        return False
    if not extract_numbers(answer.text) <= extract_numbers(question):
        return False
    return set(_DIGIT_RUN.findall(answer.text)) <= set(_DIGIT_RUN.findall(question))


def _url_key(url: str) -> str:
    return url.rstrip("/")


def check_citations(answer: Answer, evidence_urls: frozenset[str]) -> bool:
    """V4: every cited URL belongs to the evidence (a trailing slash does not matter)."""
    evidence = {_url_key(u) for u in evidence_urls}
    return all(_url_key(s.url) in evidence for s in answer.sources)


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def check_quotes(quotes: Sequence[tuple[str, str]]) -> bool:
    """V5: each ``(quote, chunk_text)`` quote appears verbatim in its chunk.

    Runs of whitespace are collapsed on both sides, since the chunk keeps line breaks that a
    quoted sentence does not. An empty quote is never verbatim evidence.
    """
    return all((q := _collapse(quote)) != "" and q in _collapse(chunk) for quote, chunk in quotes)


def validate(answer: Answer, ctx: ValidationContext) -> ValidationResult:
    """Run V1 to V5 in order (a refusal is checked by V3 only)."""
    failed: list[str] = []
    if ctx.refusal:
        if not check_refusal(answer, ctx.question):
            failed.append("V3")
        return ValidationResult(not failed, tuple(failed))
    if not check_grounding(answer.text, ctx.allowed):
        failed.append("V1")
    if not ctx.relax_stale and not check_stale(
        answer.text, ctx.losers, ctx.winners, ctx.loser_scopes, ctx.cited
    ):
        failed.append("V2")
    if not check_citations(answer, ctx.evidence_urls):
        failed.append("V4")
    if not check_quotes(ctx.quotes):
        failed.append("V5")
    return ValidationResult(not failed, tuple(failed))
