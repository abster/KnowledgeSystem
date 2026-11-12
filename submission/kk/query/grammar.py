"""Fast-path pattern registry (SPEC 8.6, 8.7; GR-1 to GR-5).

A pattern describes a question as a *skeleton*: its words, with the parts that question
parsing already understood replaced by slots (``<e>`` entity, ``<m>`` measure, ``<q>``
qualifier). A pattern is an anchored regular expression over the skeleton, so a partial
match is a non-match (GR-5). Between slots only a small set of glue words is allowed, and
those are all neutral words, so a question that matches never hides an unexplained word.

Patterns are written from question *shapes*, one family per template type (GR-2, GR-3).
Cue words (``still``, ``changed``, ``located``...) are not neutral; a pattern that accepts
them reports their token indices so the coverage check can count them as consumed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from kk.types import Shape

Family = Literal["value", "fee", "count", "address", "hours", "status", "list", "prose", "history"]

RATE_LIKE = frozenset({"rate", "dividend_rate", "apy", "apr", "min_to_open", "min_to_earn"})
ANY_MEASURE = RATE_LIKE | {"fee", "address", "hours", "eligibility"}
_FEE_LIKE = frozenset({"fee", "min_to_open", "min_to_earn"})
_LISTABLE = RATE_LIKE | {"fee"}
_FAMILY_OF_MEASURE: dict[str, Family] = {
    **{m: "value" for m in RATE_LIKE},
    "fee": "fee",
    "address": "address",
    "hours": "hours",
    "eligibility": "prose",
}

_GLUE_WORDS = (
    "the|a|an|for|on|of|in|at|with|and|to|current|currently|latest|now|today|right|"
    "loan|loans|account|accounts|card|cards|dupaco|dupaco's"
)
_G = rf"(?:(?:{_GLUE_WORDS}) )*"
_BODY = rf"{_G}(?:(?:<m>|<e>|<q>) {_G})+"
_BE = r"(?:is|are)"


@dataclass(frozen=True)
class GrammarMatch:
    """A pattern that matched the whole skeleton (GR-5: a partial match is no match).

    ``cues`` are the token indices of cue words the pattern consumed. ``measure`` is the
    measure the question stated, or the one the pattern implies (``address`` for a
    ``where`` question, ``status``, ``count``), or ``None``.
    """

    pattern: str
    family: Family
    shape: Shape
    cues: frozenset[int]
    measure: str | None


@dataclass(frozen=True)
class _Pattern:
    name: str
    shape: Shape
    regex: re.Pattern[str]
    measure_use: Literal["required", "optional", "forbidden"]
    allowed: frozenset[str]
    family: Family | None  # ``None``: the family follows the measure
    implied: str | None = None


def _p(  # noqa: PLR0913
    name: str,
    shape: Shape,
    body: str,
    *,
    use: Literal["required", "optional", "forbidden"],
    allowed: frozenset[str] = frozenset(),
    family: Family | None = None,
    implied: str | None = None,
) -> _Pattern:
    return _Pattern(name, shape, re.compile(body), use, allowed, family, implied)


_PATTERNS: tuple[_Pattern, ...] = (
    _p(
        "history_h1",
        "history",
        rf"(?:(?P<cue_lead>has |have ){_BODY}"
        rf"(?P<cue_a>(?:changed|increased|decreased|gone up|gone down) )"
        rf"|did {_BODY}(?P<cue_b>(?:change|increase|decrease|go up|go down) ))",
        use="optional",
        allowed=ANY_MEASURE,
        family="history",
    ),
    _p(
        "history_h2",
        "history",
        rf"(?:what|which) (?:was|were) {_BODY}(?P<cue>(?:before|previously|earlier) )",
        use="optional",
        allowed=ANY_MEASURE,
        family="history",
    ),
    _p(
        "how_many",
        "count",
        rf"(?:how many |(?P<cue_a>number of )){_BODY}"
        rf"(?P<cue_b>(?:(?:are|is) there |do you (?:have|offer) ))?",
        use="forbidden",
        family="count",
        implied="count",
    ),
    _p(
        "status_yn",
        "yes_no",
        rf"{_BE} {_BODY}(?P<cue_a>still )?(?P<cue_b>open )",
        use="forbidden",
        family="status",
        implied="status",
    ),
    _p(
        "status_when",
        "date",
        rf"when (?:is|are|does|did|will) {_BODY}(?P<cue>(?:opens?|closes?) )",
        use="forbidden",
        family="status",
        implied="status",
    ),
    _p(
        "list_imperative",
        "list",
        rf"(?:please )?(?P<cue_a>list )(?:(?P<cue_b>all ))?{_BODY}",
        use="optional",
        allowed=_LISTABLE,
        family="list",
    ),
    _p(
        "list_offer",
        "list",
        rf"(?:what|which) {_BODY}do you (?P<cue>offer )",
        use="forbidden",
        family="list",
    ),
    _p(
        "where_is",
        "what",
        rf"where {_BE} {_BODY}(?P<cue>located )?",
        use="forbidden",
        family="address",
        implied="address",
    ),
    _p(
        "how_much",
        "what",
        rf"how much {_BE} {_BODY}",
        use="required",
        allowed=_FEE_LIKE,
    ),
    _p(
        "tell_me",
        "what",
        rf"(?:please )?(?:tell|show|give) me {_BODY}",
        use="required",
        allowed=ANY_MEASURE,
    ),
    _p(
        "wh_be",
        "what",
        rf"(?:what {_BE} |(?P<cue_s>what's )){_BODY}",
        use="required",
        allowed=ANY_MEASURE,
    ),
)

PATTERN_NAMES: tuple[str, ...] = tuple(p.name for p in _PATTERNS)


def _cue_tokens(match: re.Match[str], starts: Sequence[int], count: int) -> frozenset[int]:
    cues: set[int] = set()
    for name, span in match.re.groupindex.items():
        if not name.startswith("cue") or match.start(span) < 0:
            continue
        lo, hi = match.start(span), match.end(span)
        cues.update(i for i in range(count) if lo <= starts[i] < hi)
    return frozenset(cues)


def _accepts(pattern: _Pattern, measure: str | None) -> bool:
    if pattern.measure_use == "forbidden":
        return measure is None
    if measure is None:
        return pattern.measure_use == "optional"
    return measure in pattern.allowed


def match_skeleton(
    tokens: Sequence[str], *, shape: Shape, measure: str | None
) -> GrammarMatch | None:
    """Match a skeleton (words plus ``<e>``, ``<m>``, ``<q>`` slots) against the registry.

    The skeleton must hold exactly one entity, and one measure slot exactly when a measure
    was stated. Only patterns of the classified ``shape`` are tried.
    """
    wanted_measures = 0 if measure is None else 1
    if not tokens or tokens.count("<e>") != 1 or tokens.count("<m>") != wanted_measures:
        return None
    text = "".join(f"{token} " for token in tokens)
    starts: list[int] = []
    position = 0
    for token in tokens:
        starts.append(position)
        position += len(token) + 1
    for pattern in _PATTERNS:
        if pattern.shape != shape or not _accepts(pattern, measure):
            continue
        found = pattern.regex.fullmatch(text)
        if found is None:
            continue
        family = pattern.family or _FAMILY_OF_MEASURE.get(measure or "")
        if family is None:  # pragma: no cover - ``allowed`` only holds known measures
            continue
        return GrammarMatch(
            pattern=pattern.name,
            family=family,
            shape=shape,
            cues=_cue_tokens(found, starts, len(tokens)),
            measure=measure or pattern.implied,
        )
    return None
