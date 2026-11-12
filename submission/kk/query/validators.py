"""Validators V1 to V5 (SPEC 14), run on both paths after generation.

Numbers are extracted with the scorer's own rule (value and unit), copied here so the
submission does not import the harness; ``tests/unit/test_validators.py`` checks the copy
against ``harness/scoring.py`` so any drift fails the build.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from contract import Answer

Number = tuple[str, float]

# Same expression and rounding as ``harness/scoring.py`` (``NUM_RE`` and ``_numbers``).
_NUM_RE = re.compile(r"(\$)?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*(%|percent)?", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"\d+")
_WHITESPACE = re.compile(r"\s+")
_ROUND_PLACES = 4


@dataclass(frozen=True)
class ValidationContext:
    """What the validators compare an answer against.

    ``allowed`` is the V1 set, ``losers`` and ``winners`` feed V2, ``evidence_urls`` V4 and
    ``quotes`` (quote, chunk text) V5. ``relax_stale`` skips V2 for history answers, where
    earlier values are the point. ``refusal`` marks an answer that must be a safe refusal.
    """

    question: str
    allowed: frozenset[Number] = frozenset()
    losers: frozenset[Number] = frozenset()
    winners: frozenset[Number] = frozenset()
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
        for unit, value in extract_numbers(text)
    )


def check_stale(text: str, losers: frozenset[Number], winners: frozenset[Number]) -> bool:
    """V2: no loser number that is not also a winner number for some in-scope key."""
    return not any(n in losers and n not in winners for n in extract_numbers(text))


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
    if not ctx.relax_stale and not check_stale(answer.text, ctx.losers, ctx.winners):
        failed.append("V2")
    if not check_citations(answer, ctx.evidence_urls):
        failed.append("V4")
    if not check_quotes(ctx.quotes):
        failed.append("V5")
    return ValidationResult(not failed, tuple(failed))
