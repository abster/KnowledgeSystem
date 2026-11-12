"""Question understanding (SPEC 8.2 to 8.8): shape, entity, measure, qualifiers, coverage.

``parse`` is deterministic, calls no model and never raises. It fails closed: a question may
use the fast path only if every word is explained (COV-1 to COV-5) and one grammar pattern
matches the whole question (GR-1). The target date is never read from the text (CHG-4);
a date in the question is simply unexplained words.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from kk.ingest.aliases import AliasIndex
from kk.query.grammar import GrammarMatch, match_skeleton
from kk.query.normalise import (
    AmountConstraint,
    clean_text,
    find_amount_constraints,
    find_model_years,
    find_terms,
    fold_dashes,
)
from kk.types import ParsedQuestion, QualifierSpan, Shape, Span

# COV-3 with two Step 6 changes. "about" is removed: COV-4 requires it (with "roughly",
# "around", "approximately") to fail the coverage check. The brand word and its possessive are
# added by the owner's decision, because they never change which fact a question means.
NEUTRAL_WORDS: frozenset[str] = frozenset(
    "a an the is are was were be do does did can could will would should what which who where "
    "when how much many for of on in at to my our your me i we you it its this that these those "
    "please tell give show find there any current currently now right today latest new rate "
    "rates loan loans account accounts card cards fee fees apply with and or "
    "dupaco dupaco's".split()
)

# CHG-1: a deliberately broad list. A cue alone never selects the fast path.
CHANGE_CUES: tuple[str, ...] = (
    "changed",
    "change",
    "changes",
    "still",
    "used to",
    "no longer",
    "any longer",
    "anymore",
    "since",
    "history",
    "previously",
    "before",
    "earlier",
    "ever",
    "new",
    "increase",
    "decrease",
    "went up",
    "went down",
    "gone up",
    "gone down",
    "closed",
    "closing",
    "open again",
)

MAX_QUESTION_CHARS = 1000  # longer input is not a member question; it fails closed

# ENT-3
LABEL_SCORE = 1.0
ALIAS_SCORE = 0.7

_AGGREGATE = re.compile(
    r"(?<!\w)(?:sum|total|totals|average|averages|avg|compare|compared|comparison|combined|"
    r"difference|versus|vs)(?!\w)"
)
_CHANGE_CUE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(sorted((c.replace(" ", r"\s+") for c in CHANGE_CUES), key=len, reverse=True))
    + r")(?!\w)"
)
_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*|[^\w\s.,?!;:'\"()\-]|_")
_ONLY_SEPARATORS = re.compile(r"^[\s-]*$")
_TERM_SUFFIX = re.compile(r"\s+terms?(?!\w)")
_LEADING_WORD = re.compile(r"^[^\w]*(\w+)")
_IMPERATIVE = re.compile(r"^[^\w]*(?:please\s+)?(?:tell|show|give)\s+me(?!\w)")
_LIST_CUE = re.compile(
    r"(?<!\w)list(?!\w)|types of|(?<!\w)all the(?!\w)|(?:which|what)\b.*\bdo you offer"
)
_YES_NO_STARTS = frozenset("is are does do can was were will has have".split())  # SPEC 8.3 rule 3
_WH_STARTS = ("what", "how much", "which", "where", "who")

# measure phrases, most specific first; the generic "rate" yields to a specific rate word
_MEASURES: tuple[tuple[str, str], ...] = (
    (r"annual percentage yield", "apy"),
    (r"annual percentage rate", "apr"),
    (r"dividend rate", "dividend_rate"),
    (r"interest rate", "rate"),
    (r"minimum to open", "min_to_open"),
    (r"min\.? to open", "min_to_open"),
    (r"minimum to earn", "min_to_earn"),
    (r"min\.? to earn", "min_to_earn"),
    (r"apy", "apy"),
    (r"apr", "apr"),
    (r"rates?", "rate"),
    (r"fees?", "fee"),
    (r"address(?:es)?", "address"),
    (r"hours", "hours"),
    (r"eligibility", "eligibility"),
)
_MEASURE_RES = tuple((re.compile(rf"(?<!\w)(?:{body})(?!\w)"), attr) for body, attr in _MEASURES)
_SPECIFIC_RATES = frozenset({"apy", "apr", "dividend_rate"})
_MEASURE_WORDS = frozenset(
    {"apy", "apr", "address", "addresses", "hours", "eligibility", "interest", "dividend"}
)

# product words: (key, value)
_PRODUCT_WORDS: dict[str, tuple[str, str]] = {
    "purchase": ("product", "purchase"),
    "refinance": ("product", "refinance"),
    "fixed": ("rate_type", "fixed"),
    "adjustable": ("rate_type", "arm"),
    "arm": ("rate_type", "arm"),
    "conventional": ("loan_type", "conventional"),
    "jumbo": ("loan_type", "jumbo"),
}
_PRODUCT_RE = re.compile(r"(?<!\w)(" + "|".join(_PRODUCT_WORDS) + r")(?!\w)")

_AMBIGUITY_PREFIXES = (
    "entity_ambiguous",
    "measure_ambiguous",
    "qualifier_conflict",
    "amount_conflict",
)


# --- entity linking --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Word:
    text: str  # lower case, apostrophes removed (the alias form)
    start: int
    end: int


@dataclass(frozen=True)
class _EntityMatch:
    start: int
    end: int
    ids: frozenset[str]
    label: bool  # the phrase is an entity's own canonical id (ENT-3)


class EntityLinker:
    """Longest-match entity linking over the alias list (ENT-1, ENT-2)."""

    def __init__(self, index: AliasIndex) -> None:
        """Keep the alias index and the longest alias, in words."""
        self.index = index
        self._longest = max((len(a.split()) for a in index.aliases), default=1)

    def link(self, words: Sequence[_Word], text: str) -> list[_EntityMatch]:
        """Longest alias matches over consecutive words (``text`` supplies the separators)."""
        found: list[_EntityMatch] = []
        i = 0
        while i < len(words):
            match = self._longest_at(words, i, text)
            if match is None:
                i += 1
                continue
            found.append(match[0])
            i += match[1]
        return found

    def _longest_at(
        self, words: Sequence[_Word], i: int, text: str
    ) -> tuple[_EntityMatch, int] | None:
        for n in range(min(self._longest, len(words) - i), 0, -1):
            window = words[i : i + n]
            if any(
                not _ONLY_SEPARATORS.match(text[a.end : b.start])
                for a, b in zip(window, window[1:], strict=False)
            ):
                continue
            phrase = " ".join(w.text for w in window)
            if all(w.text in NEUTRAL_WORDS or w.text in _MEASURE_WORDS for w in window):
                continue  # a phrase of words that say nothing about the product
            if phrase in self.index.entities:
                ids, label = frozenset({phrase}), True
            else:
                ids, label = self.index.aliases.get(phrase, frozenset()), False
            if ids:
                return _EntityMatch(window[0].start, window[-1].end, ids, label), n
        return None


# --- helpers ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    start: int
    end: int
    kind: str  # amount, term, model_year, product, measure, entity
    mark: str  # <q>, <m>, <e>


def _lower(text: str) -> str:
    """Lower-case character by character so spans keep their positions."""
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in text).replace("’", "'")


def _free(start: int, end: int, taken: Iterable[tuple[int, int]]) -> bool:
    return all(end <= a or start >= b for a, b in taken)


def _model_year_value(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}-{last}"


def _amount_text(amount: AmountConstraint) -> str:
    def bound(value: Decimal | None) -> str:
        return "" if value is None else format(value.normalize(), "f")

    opening = "[" if amount.lower_inclusive else "("
    closing = "]" if amount.upper_inclusive else ")"
    return f"{opening}{bound(amount.lower)},{bound(amount.upper)}{closing}"


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _classify_shape(low: str) -> Shape:
    """SPEC 8.3 rules 2 to 7 (rule 1, ``history``, needs the grammar and runs first)."""
    stripped = low.lstrip()
    if stripped.startswith(("how many", "number of")):
        return "count"
    first = _LEADING_WORD.match(low)
    word = first.group(1) if first else ""
    if word in _YES_NO_STARTS:
        return "yes_no"
    if word == "when":
        return "date"
    if _LIST_CUE.search(low):
        return "list"
    if stripped.startswith(_WH_STARTS) or _IMPERATIVE.match(low):
        return "what"
    return "other"


# --- extraction ------------------------------------------------------------------------------


@dataclass
class _Found:
    slots: list[_Slot]
    qualifiers: list[QualifierSpan]
    amounts: list[tuple[AmountConstraint, int, int]]
    reasons: list[str]


def _numeric_slots(low: str, found: _Found) -> None:
    for amount in find_amount_constraints(low):
        found.slots.append(_Slot(amount.start, amount.end, "amount", "<q>"))
        found.amounts.append((amount.constraint, amount.start, amount.end))
    for term in find_terms(low):
        end = term.end
        suffix = _TERM_SUFFIX.match(low, end)
        if suffix is not None:
            end = suffix.end()
        if _free(term.start, end, [(s.start, s.end) for s in found.slots]):
            found.slots.append(_Slot(term.start, end, "term", "<q>"))
            found.qualifiers.append(QualifierSpan("term", term.months, term.start, end))
    for year in find_model_years(low):
        if _free(year.start, year.end, [(s.start, s.end) for s in found.slots]):
            found.slots.append(_Slot(year.start, year.end, "model_year", "<q>"))
            value = _model_year_value(year.years.first, year.years.last)
            found.qualifiers.append(QualifierSpan("model_year", value, year.start, year.end))


def _words_in_gaps(low: str, slots: Sequence[_Slot]) -> list[list[_Word]]:
    """Runs of words between the numeric slots, for entity linking."""
    bounds = sorted((s.start, s.end) for s in slots)
    runs: list[list[_Word]] = []
    position = 0
    for start, end in [*bounds, (len(low), len(low))]:
        run = [
            _Word(m.group(0).replace("'", ""), position + m.start(), position + m.end())
            for m in re.finditer(r"[^\W_]+(?:'[^\W_]+)*", low[position:start])
        ]
        if run:
            runs.append(run)
        position = end
    return runs


def _link_entities(
    low: str, found: _Found, linker: EntityLinker
) -> tuple[list[_EntityMatch], bool]:
    matches: list[_EntityMatch] = []
    for run in _words_in_gaps(low, found.slots):
        matches.extend(linker.link(run, low))
    for match in matches:
        found.slots.append(_Slot(match.start, match.end, "entity", "<e>"))
    return matches, bool(matches)


def _measure_slots(low: str, found: _Found) -> str | None:
    taken = [(s.start, s.end) for s in found.slots]
    hits: list[tuple[int, int, str]] = []
    for pattern, attr in _MEASURE_RES:
        hits.extend((m.start(), m.end(), attr) for m in pattern.finditer(low))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    chosen: list[tuple[int, int, str]] = []
    for hit in hits:
        if _free(hit[0], hit[1], taken) and (not chosen or hit[0] >= chosen[-1][1]):
            chosen.append(hit)
    attrs = {h[2] for h in chosen}
    if "rate" in attrs and attrs & _SPECIFIC_RATES:
        attrs.discard("rate")
    for start, end, _ in chosen:
        found.slots.append(_Slot(start, end, "measure", "<m>"))
    if len(attrs) > 1:
        found.reasons.append("measure_ambiguous")
        return None
    return next(iter(attrs), None)


def _product_slots(low: str, found: _Found) -> None:
    taken = [(s.start, s.end) for s in found.slots]
    for m in _PRODUCT_RE.finditer(low):
        if _free(m.start(), m.end(), taken):
            key, value = _PRODUCT_WORDS[m.group(1)]
            found.slots.append(_Slot(m.start(), m.end(), "product", "<q>"))
            found.qualifiers.append(QualifierSpan(key, value, m.start(), m.end()))


def _merge_measures(slots: list[_Slot], low: str) -> list[_Slot]:
    """Adjacent measure words (``apr rate``) are one ``<m>`` slot."""
    merged: list[_Slot] = []
    for slot in sorted(slots, key=lambda s: s.start):
        last = merged[-1] if merged else None
        if (
            last is not None
            and last.kind == "measure"
            and slot.kind == "measure"
            and low[last.end : slot.start].strip() == ""
        ):
            merged[-1] = _Slot(last.start, slot.end, "measure", "<m>")
        else:
            merged.append(slot)
    return merged


@dataclass(frozen=True)
class _Tok:
    text: str
    start: int
    end: int
    slot: str | None  # ``<q>``, ``<m>``, ``<e>``, or ``None`` for a word


def _tokens(low: str, slots: Sequence[_Slot]) -> list[_Tok]:
    out: list[_Tok] = []
    position = 0
    for slot in [*slots, _Slot(len(low), len(low), "", "")]:
        for m in _TOKEN.finditer(low, position, slot.start):
            out.append(_Tok(m.group(0), m.start(), m.end(), None))
        if slot.kind:
            out.append(_Tok(slot.mark, slot.start, slot.end, slot.mark))
        position = slot.end
    return out


def _entity_result(
    matches: Sequence[_EntityMatch], reasons: list[str]
) -> tuple[tuple[str, ...], str | None, float]:
    candidates = tuple(sorted({i for m in matches for i in m.ids}))
    if not matches:
        return (), None, 0.0
    if len(candidates) != 1:
        reasons.append("entity_ambiguous")
        return candidates, None, 0.0
    score = min(LABEL_SCORE if m.label else ALIAS_SCORE for m in matches)
    return candidates, candidates[0], score


def _qualifier_conflicts(
    qualifiers: Sequence[QualifierSpan], reasons: list[str]
) -> list[QualifierSpan]:
    kept: dict[str, QualifierSpan] = {}
    for q in qualifiers:
        if q.key not in kept:
            kept[q.key] = q
        elif kept[q.key].value != q.value:
            reasons.append(f"qualifier_conflict:{q.key}")
    return sorted(kept.values(), key=lambda q: q.start)


def _canonical_key(parsed: ParsedQuestion, target_ord: int) -> str:
    """SPEC 8.8: shape, entity, measure, sorted qualifier items and ``target_ord`` as JSON."""
    items: list[list[str | int]] = [[q.key, q.value] for q in parsed.qualifiers]
    if parsed.amount is not None:
        items.append(["amount", _amount_text(parsed.amount)])
    payload = {
        "shape": parsed.shape,
        "entity": parsed.entity,
        "measure": parsed.measure,
        "qualifiers": sorted(items, key=lambda kv: str(kv[0])),
        "target_ord": target_ord,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _too_long(text: str) -> ParsedQuestion:
    return ParsedQuestion(
        text=text[:MAX_QUESTION_CHARS], shape="other", entity_candidates=(), entity=None,
        entity_score=0.0, measure=None, qualifiers=(), amount=None, change_cue=False,
        aggregate=False, consumed=(), residual="", covered=False, uncertain=False,
        reasons=("too_long",), pattern=None, canonical_key=None,
    )  # fmt: skip


def _cue_spans(match: GrammarMatch | None, toks: Sequence[_Tok]) -> list[Span]:
    if match is None:
        return []
    return [Span(toks[i].start, toks[i].end, "cue") for i in sorted(match.cues)]


def _change_cue(low: str, match: GrammarMatch | None, cues: Sequence[Span]) -> bool:
    """CHG-1, minus cue words that a non-history pattern consumed (``still`` in STATUS)."""
    consumed = [(c.start, c.end) for c in cues] if match and match.family != "history" else []
    return any(
        not any(a <= m.start() and m.end() <= b for a, b in consumed)
        for m in _CHANGE_CUE.finditer(low)
    )


def parse(question: str, linker: EntityLinker, *, target_ord: int | None = None) -> ParsedQuestion:
    """Parse a question without a model. Deterministic, and never raises.

    ``target_ord`` only goes into the canonical key; it is the caller's, taken from ``as_of``.
    """
    if len(question) > MAX_QUESTION_CHARS:
        return _too_long(question)
    low = _lower(fold_dashes(clean_text(question)))
    found = _Found([], [], [], [])
    _numeric_slots(low, found)
    matches, _ = _link_entities(low, found, linker)
    measure = _measure_slots(low, found)
    _product_slots(low, found)
    candidates, entity, entity_score = _entity_result(matches, found.reasons)
    qualifiers = _qualifier_conflicts(found.qualifiers, found.reasons)
    amount = found.amounts[0][0] if found.amounts else None
    if len({a[0] for a in found.amounts}) > 1:
        found.reasons.append("amount_conflict")

    slots = _merge_measures(found.slots, low)
    toks = _tokens(low, slots)
    skeleton = [t.slot or t.text for t in toks]
    match = match_skeleton(skeleton, shape="history", measure=measure)
    shape: Shape = "history" if match else _classify_shape(low)
    if match is None:
        match = match_skeleton(skeleton, shape=shape, measure=measure)

    cues = _cue_spans(match, toks)
    cue_starts = {c.start for c in cues}
    residual_toks = [t for t in toks if t.slot is None and t.start not in cue_starts]
    bad = [t.text for t in residual_toks if t.text not in NEUTRAL_WORDS]
    reasons = _dedupe([*found.reasons, *(f"residual:{w}" for w in bad)])
    if shape == "other":
        reasons = _dedupe([*reasons, "shape_other"])
    elif match is None:
        reasons = _dedupe([*reasons, "no_pattern"])
    covered = not bad
    uncertain = any(r.startswith(_AMBIGUITY_PREFIXES) for r in reasons)
    final_measure = (match.measure if match else None) or measure
    consumed = sorted(
        [Span(s.start, s.end, s.kind) for s in slots] + cues, key=lambda s: (s.start, s.end)
    )
    parsed = ParsedQuestion(
        text=low, shape=shape, entity_candidates=candidates, entity=entity,
        entity_score=entity_score, measure=final_measure, qualifiers=tuple(qualifiers),
        amount=amount, change_cue=_change_cue(low, match, cues),
        aggregate=_AGGREGATE.search(low) is not None, consumed=tuple(consumed),
        residual=" ".join(t.text for t in residual_toks), covered=covered, uncertain=uncertain,
        reasons=reasons, pattern=match.pattern if match else None, canonical_key=None,
    )  # fmt: skip
    if parsed.fast_eligible and target_ord is not None:
        return dataclasses.replace(parsed, canonical_key=_canonical_key(parsed, target_ord))
    return parsed
