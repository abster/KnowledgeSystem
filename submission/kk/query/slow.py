"""The slow path: payload, one structured call, acceptance rules and the deadline (SPEC 13).

The slow model sees only resolved facts, capped parent chunks, conflict notes without loser
values, and (for change-cue questions) the fact-history view. Its reply is accepted only if it
is well formed, answerable, cites supplied evidence, mentions no loser and passes the
validators (SLOW-3). Every other outcome is "no answer", which the caller turns into the safe
refusal. Nothing here raises.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from contract import Answer, Source
from kk.conflicts import normalised_value, resolve_key_at
from kk.llm.client import LlmError, LlmOutputError, LlmTransientError, StructuredLlmClient
from kk.log import get_logger
from kk.query.candidates import AMOUNT_KEYS
from kk.query.normalise import parse_tier_text
from kk.query.render import qualifier_phrase
from kk.query.retrieve import CandidateGroup
from kk.query.validators import (
    CitedEvidence,
    Number,
    ValidationContext,
    numbers_of,
    spelled_numbers,
    validate,
)
from kk.types import Chunk, Fact, FactVersion, SnapshotRef

MAX_CHUNK_CHARS = 12000  # SPEC 13.1: parent chunks in total
MAX_HISTORY_LINES = 40  # HIST-4
MAX_FACT_LINES = 30  # resolved facts shown to the model
MAX_TEXT_CANDIDATES = 5  # differing statements shown for one unresolved text key
MAX_SOURCES = 3  # SPEC 4.1
MAX_QUOTE_CHARS = 200  # SPEC 4.1
QUOTE_LEAD = 80  # characters kept before the value when a quote is cut
MIN_CHUNK_TAIL = 200  # a chunk is cut to fit only if at least this much room is left
SLOW_MAX_TOKENS = 1024
DEFAULT_RETRY_PAUSE_S = 1.0  # SLOW-4: wait before the single retry when none is stated
MAX_RETRY_PAUSE_S = 10.0

SLOW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answerable": {"type": "boolean"},
        "answer": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "loser_values_mentioned": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},  # why not answerable: logged, never shown to the member
    },
    "required": ["answerable", "answer", "evidence_ids", "loser_values_mentioned"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You answer member questions about Dupaco Community Credit Union from the evidence supplied "
    "in the user message, as of the snapshot date given there.\n"
    "Rules:\n"
    "- RECENCY: when pieces of evidence state different values for the same thing (a count, a "
    "rate, an hour), the value in the evidence with the latest date is the current one. Use the "
    "latest effective date, or the latest publication date for news and blog pages marked "
    "published, and state only that value. Do not mention older values, earlier years or "
    "superseded figures, even for context, unless the question asks how something changed. Do "
    "not prefer a value because a page states it more directly or more often, or because more "
    "pages repeat it. Evidence that is undated, or that does not mention the thing, does not "
    "count against a dated value.\n"
    "- Answer only from the supplied evidence. If the evidence does not contain the answer, "
    "set answerable to false and leave answer empty.\n"
    "- Never state a value that the evidence marks as stale, expired, ended or superseded, and "
    "never state a value from a conflict that is marked unresolved. A value is otherwise current: "
    "an effective date only says when it started, so do not call a value stale because that date "
    "is earlier than the snapshot date.\n"
    "- If the evidence answers the question only for particular branches, products or programs "
    "and the question is general (hours listed branch by branch, for example), do not refuse: "
    "give what the evidence states, name the branch, product or program each part applies to, "
    "and say that it can vary. When sources differ, prefer the newest. Set answerable to false "
    "only when nothing in the evidence addresses the question.\n"
    "- If the question does not say which tier, balance band or term it means and several are "
    "listed, state the assumed tier in the answer, using the one marked ENTRY TIER as the "
    "default. For one named product without an amount you may give the range from the entry "
    "tier to the top tier. Do not make another tier the headline.\n"
    "- Use numbers exactly as written in the evidence. Do not add numbers that are not in it.\n"
    "- Cite every piece of evidence you used in evidence_ids, using the ids in square brackets "
    "(F1, C1, ...). Leave loser_values_mentioned empty.\n"
    "- If you cannot answer, set answerable to false and say in reason, in one sentence, what is "
    "missing.\n"
    "- Keep the answer short and member-facing."
)

_BYLINE_DATE = re.compile(
    r"(?:\A|\n|\))\s*(?P<month>[A-Z][a-z]+) (?P<day>\d{1,2}), (?P<year>\d{4})\s*\Z"
)
_BYLINE_CHARS = 200  # a byline chunk is short; a sentence that ends on a date is longer
_PUBLISHED_WITHIN = 6  # a byline date sits in the first few chunks of a page
_PAGE_UNITS = frozenset({"", "$", "%"})
_THRESHOLD_UNITS = frozenset(
    {"", "$"}
)  # beside tier siblings a rate must come from a fact (TIER-5)
_NUMBER_TOKEN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
_log = get_logger("slow")


@dataclass(frozen=True)
class ParentChunk:
    """A matched parent chunk offered as evidence (SPEC 13.1)."""

    chunk_id: str
    url: str
    title: str
    text: str
    score: float
    authority_rank: int
    headers: tuple[str, ...] = ()  # page title down to the section (SPEC 6.3, CH-1)
    effective_date: str | None = None
    expiry_date: str | None = None
    published: str | None = None  # publication date of a news or blog page (SRET-5)
    member_ids: tuple[str, ...] = ()  # every chunk this block stands for (V2 scopes)


@dataclass(frozen=True)
class SlowInputs:
    """Everything the payload is built from."""

    question: str
    taken_on: str
    snapshot_id: str | None
    groups: Sequence[CandidateGroup]
    chunks: Sequence[ParentChunk] = ()
    conflict_notes: Sequence[str] = ()
    history_lines: Sequence[str] = ()
    losers: frozenset[Number] = frozenset()
    loser_scopes: Mapping[Number, frozenset[str]] = field(default_factory=dict)
    default_tier: str = "entry"
    conflict_note: str | None = None


@dataclass(frozen=True)
class Evidence:
    """One citable piece of evidence: a resolved fact (``F<n>``) or a chunk (``C<n>``)."""

    id: str
    url: str
    snapshot_id: str | None
    text: str
    authority_rank: int
    needle: str = ""
    grounding: str = ""  # extra text whose numbers a cited fact makes printable (row, dates)
    chunk_ids: frozenset[str] = frozenset()  # the chunks this evidence stands for (V2 scopes)


@dataclass
class SlowRequest:
    """The prompt and what the validators need to judge the reply."""

    system: str
    user: str
    evidence: dict[str, Evidence]
    allowed: frozenset[Number]
    winners: frozenset[Number]
    losers: frozenset[Number]
    loser_scopes: Mapping[Number, frozenset[str]] = field(default_factory=dict)
    confident: bool = True
    chunk_units: frozenset[str] = frozenset()  # units of page-text numbers the answer may print
    relax_stale: bool = False
    conflict_note: str | None = None


@dataclass(frozen=True)
class SlowOutcome:
    """An accepted ``answer``, or ``None`` with the ``reason`` and the failed validators."""

    answer: Answer | None
    reason: str
    failed: tuple[str, ...] = field(default_factory=tuple)


# --- the payload ------------------------------------------------------------------------------


def excerpt(text: str, needle: str) -> str:
    """A verbatim excerpt of at most 200 characters, centred on ``needle`` when cut."""
    text = text.strip()
    if len(text) <= MAX_QUOTE_CHARS:
        return text
    at = max(text.find(needle), 0) if needle else 0
    start = max(0, at - QUOTE_LEAD)
    return text[start : start + MAX_QUOTE_CHARS]


def _upper_bound(fact: Fact) -> Decimal | None:
    """The cap of a tier qualifier, or ``None`` when it has none (an open top tier)."""
    for key in AMOUNT_KEYS:
        marker = f'"{key}":"'
        start = fact.qualifiers.find(marker)
        if start < 0:
            continue
        begin = start + len(marker)
        constraint = parse_tier_text(fact.qualifiers[begin : fact.qualifiers.find('"', begin)])
        if constraint is not None:
            return constraint.upper
    return None


def _has_tiers(fact: Fact) -> bool:
    return any(f'"{key}"' in fact.qualifiers for key in AMOUNT_KEYS)


def entry_tier_keys(facts: Sequence[Fact], default_tier: str) -> frozenset[str]:
    """TIER-2: the fact key of the lowest tier for each (entity, attribute) with several tiers."""
    if default_tier != "entry":
        return frozenset()
    by_pair: dict[tuple[str, str], list[Fact]] = {}
    for fact in facts:
        if _has_tiers(fact):
            by_pair.setdefault((fact.entity, fact.attribute), []).append(fact)
    chosen: set[str] = set()
    for members in by_pair.values():
        if len(members) < 2:  # noqa: PLR2004 - one tier is not a choice
            continue
        lowest = min(
            members,
            key=lambda f: (_upper_bound(f) is None, _upper_bound(f) or Decimal(0), f.fact_key),
        )
        chosen.add(lowest.fact_key)
    return frozenset(chosen)


def loser_numbers(
    versions: Sequence[FactVersion], winner_values: Sequence[str]
) -> frozenset[Number]:
    """V2 input: numbers of numeric facts that are not a current value (stale, expired, loser).

    The digits of a free-text statement (a phone or routing number, an address) are not a stale
    value, so they never count as a loser.
    """
    numeric = [v.fact.value_text for v in versions if v.fact.value_num is not None]
    return numbers_of(numeric) - numbers_of(winner_values)


STALE_STRATEGIES = frozenset({"effective_date", "source_authority"})  # SPEC 10.3 steps 1 and 2


def _version_ids(version: FactVersion) -> frozenset[str]:
    """The chunk of a fact and its parent table: what a citation of the fact stands for."""
    chunk = version.interval.chunk
    return frozenset(filter(None, (version.fact.chunk_id, chunk.parent_id)))


def loser_scopes(
    groups: Sequence[CandidateGroup], versions: Sequence[FactVersion]
) -> dict[Number, frozenset[str]]:
    """For each numeric loser of an in-scope key, the chunks that hold the key's current value.

    A loser is a violation only when an answer draws on those chunks (V2). A key without a
    winner (an unresolved conflict) is scoped to every one of its versions, so any citation of
    it counts.
    """
    scopes: dict[Number, set[str]] = {}
    for group in groups:
        winner = group.winner
        rows = [v for v in versions if v.fact.fact_key == group.fact_key]
        ids: set[str] = set()
        for version in [winner] if winner is not None else rows:
            ids |= _version_ids(version)
        keep = numbers_of([winner.fact.value_text]) if winner is not None else frozenset()
        for version in rows:
            if version.fact.value_num is None:
                continue
            for number in numbers_of([version.fact.value_text]) - keep:
                scopes.setdefault(number, set()).update(ids)
    return {number: frozenset(ids) for number, ids in scopes.items()}


def superseded(
    by_key: Mapping[str, Sequence[FactVersion]], *, ordinal: int, snapshots: Sequence[SnapshotRef]
) -> tuple[frozenset[str], frozenset[Number]]:
    """Chunks and numbers that hold a value which lost its key on evidence of being replaced."""
    chunks, numbers, _ = superseded_detail(by_key, ordinal=ordinal, snapshots=snapshots)
    return chunks, numbers


def superseded_detail(
    by_key: Mapping[str, Sequence[FactVersion]], *, ordinal: int, snapshots: Sequence[SnapshotRef]
) -> tuple[frozenset[str], frozenset[Number], dict[Number, frozenset[str]]]:
    """``superseded`` plus, for each stale number, the chunks of the value that replaced it.

    A version counts as superseded only when it lost on effective date or on source authority.
    Losing on snapshot recency says nothing about staleness: two unrelated articles can share a
    key (for example two years of one statistic), so neither is dropped for it. A number that
    is current for any key is never a loser.
    """
    stale_chunks: set[str] = set()
    stale_numbers: set[Number] = set()
    scopes: dict[Number, set[str]] = {}
    current: set[Number] = set()
    for versions in by_key.values():
        resolution = resolve_key_at(versions, ordinal=ordinal, snapshots=snapshots)
        winner = resolution.winner
        if winner is None:
            continue
        current |= numbers_of([winner.fact.value_text])
        if resolution.strategy not in STALE_STRATEGIES:
            continue
        for version in versions:
            if normalised_value(version) == normalised_value(winner):
                continue
            if version.visible_at(ordinal):
                stale_chunks.add(version.fact.chunk_id)
            if version.fact.value_num is not None:
                gone = numbers_of([version.fact.value_text])
                stale_numbers |= gone
                for number in gone:
                    scopes.setdefault(number, set()).update(_version_ids(winner))
    kept = stale_numbers - current
    return (
        frozenset(stale_chunks),
        frozenset(kept),
        {n: frozenset(ids) for n, ids in scopes.items() if n in kept},
    )


MIN_SHARED_ROW_LABELS = 3  # fewer shared rows is a coincidence, not one table twice
MIN_ROW_OVERLAP = 0.7  # share of the rows (Jaccard) that two generations have in common


def _row_labels(text: str) -> frozenset[str]:
    """The first cell of each data row of a rendered table (the header line is skipped)."""
    lines = [line for line in text.splitlines()[1:] if line.count(" | ") >= 1]
    return frozenset(line.split(" | ", 1)[0].strip().casefold() for line in lines)


def _shape(table: Chunk) -> tuple[str, tuple[str, ...]]:
    """The section heading and the column names after the first (the first is often renamed)."""
    header = table.text.splitlines()[0] if table.text else ""
    columns = tuple(c.strip().casefold() for c in header.split(" | ")[1:])
    leaf = table.parent_headers[-1].casefold() if table.parent_headers else ""
    return leaf, columns


def stale_tables(tables: Sequence[Chunk]) -> tuple[frozenset[str], frozenset[Number]]:
    """Tables replaced by a later generation of themselves, and the numbers only they hold."""
    stale, numbers, _ = stale_tables_detail(tables)
    return stale, numbers


def stale_tables_detail(
    tables: Sequence[Chunk],
) -> tuple[frozenset[str], frozenset[Number], dict[Number, frozenset[str]]]:
    """``stale_tables`` plus, for each stale number, the tables that replaced it.

    Rate tables are often kept on old pages after a new rate set is published, and the rows may
    give no facts to compare. Two tables with the same section heading and the same columns
    whose row labels overlap strongly (at least three shared rows) are generations of one
    table: the one with the earlier effective date is stale. A number that the later
    generation also holds is never a loser.
    """
    dated = [(t, _row_labels(t.text), _shape(t)) for t in tables if t.effective_date]
    stale: set[str] = set()
    stale_numbers: set[Number] = set()
    current: set[Number] = set()
    replaced_by: list[tuple[frozenset[Number], str]] = []
    for old, old_labels, old_shape in dated:
        for new, new_labels, new_shape in dated:
            shared = len(old_labels & new_labels)
            union = len(old_labels | new_labels)
            if (
                old_shape == new_shape
                and (old.effective_date or "") < (new.effective_date or "")
                and shared >= MIN_SHARED_ROW_LABELS
                and shared / union >= MIN_ROW_OVERLAP
            ):
                stale.add(old.chunk_id)
                gone = numbers_of([old.text])
                stale_numbers |= gone
                current |= numbers_of([new.text])
                replaced_by.append((gone, new.chunk_id))
    kept = stale_numbers - current
    scopes: dict[Number, set[str]] = {}
    for gone, new_id in replaced_by:
        for number in gone & kept:
            scopes.setdefault(number, set()).add(new_id)
    return frozenset(stale), frozenset(kept), {n: frozenset(ids) for n, ids in scopes.items()}


def _distinct(versions: Sequence[FactVersion]) -> list[FactVersion]:
    """One version per distinct value text, in a stable order."""
    seen: dict[str, FactVersion] = {}
    for version in sorted(versions, key=lambda v: (v.fact.value_text, v.url)):
        seen.setdefault(version.fact.value_text, version)
    return list(seen.values())


def _fact_evidence(index: int, version: FactVersion, snapshot_id: str | None) -> Evidence:
    fact = version.fact
    grounding = " ".join(
        filter(
            None,
            (fact.quote, fact.effective_date, fact.expiry_date, qualifier_phrase(fact.qualifiers)),
        )
    )
    return Evidence(
        f"F{index}",
        version.url,
        snapshot_id,
        fact.quote or version.interval.chunk.text,
        fact.authority_rank,
        fact.value_text,
        grounding,
        _version_ids(version),
    )


def _has_tier_siblings(facts: Sequence[Fact]) -> bool:
    """True if two facts of one entity and attribute differ in an amount tier (TIER-1)."""
    seen: dict[tuple[str, str], int] = {}
    for fact in facts:
        if _has_tiers(fact):
            pair = (fact.entity, fact.attribute)
            seen[pair] = seen.get(pair, 0) + 1
    return any(n >= 2 for n in seen.values())  # noqa: PLR2004


def _fact_line(index: int, version: FactVersion, entry: bool) -> str:
    fact = version.fact
    qualifiers = qualifier_phrase(fact.qualifiers) or "no qualifier"
    parts = [
        f"[F{index}] {fact.entity_label}",
        fact.attribute,
        qualifiers,
        fact.value_text,
        f"effective {fact.effective_date or '-'}",
        f"expires {fact.expiry_date or '-'}",
        f"authority {fact.authority_rank}",
        f"page {version.url}",
    ]
    line = " | ".join(parts)
    return f"{line}  <- ENTRY TIER" if entry else line


PROMOTED_SCORE = 2.0  # above every ranking score (1 / (place + 1) is at most 1)
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _sentence_key(sentence: str) -> str:
    return _SPACES.sub(" ", _DIGITS.sub("#", sentence)).strip().lower()


def promote_newest_copies(chunks: Sequence[ParentChunk]) -> list[ParentChunk]:
    """Put the newest page's copy of a changing sentence first, so the cap cannot drop it (SRET-7).

    A closing paragraph is copied onto many news pages and each copy carries the figures of its
    day ("23 branch office locations" until March, "22" after). The chunks are ranked by
    relevance, not date, so with a 12,000 character cap the newest copy can fall outside while
    older copies are read. A sentence that holds a digit and appears on at least two dated pages
    with different figures (the same sentence once digits are masked) is a changing statement;
    the chunk with its newest dated copy (ties: the better ranked) is promoted to the front.
    Nothing is removed: older copies stay, so history and as-of questions lose no evidence.
    Undated chunks and tables are never compared.

    Args:
        chunks: The chunks offered to the model.

    Returns:
        The same chunks, with the newest copies given the highest score.
    """
    copies: dict[str, list[tuple[ParentChunk, str]]] = {}
    for chunk in chunks:
        if not chunk.published:
            continue
        for sentence in _SENTENCE_END.split(chunk.text):
            if _DIGITS.search(sentence):
                copies.setdefault(_sentence_key(sentence), []).append((chunk, sentence.strip()))
    promoted: set[str] = set()
    for members in copies.values():
        if len({c.url for c, _ in members}) < 2 or len({t for _, t in members}) < 2:  # noqa: PLR2004
            continue
        newest = max((c for c, _ in members), key=lambda c: (c.published or "", c.score))
        promoted.add(newest.chunk_id)
    return [replace(c, score=PROMOTED_SCORE) if c.chunk_id in promoted else c for c in chunks]


def _select_chunks(chunks: Sequence[ParentChunk]) -> list[ParentChunk]:
    """Best score first, within ``MAX_CHUNK_CHARS`` in total (the last one may be cut)."""
    chosen: list[ParentChunk] = []
    used = 0
    for chunk in sorted(chunks, key=lambda c: (-c.score, c.chunk_id)):
        room = MAX_CHUNK_CHARS - used
        if len(chunk.text) <= room:
            chosen.append(chunk)
            used += len(chunk.text)
            continue
        if room >= MIN_CHUNK_TAIL:
            chosen.append(replace(chunk, text=chunk.text[:room]))
        break
    return chosen


def build_request(inputs: SlowInputs) -> SlowRequest:  # noqa: PLR0914
    """SPEC 13.1: the system prompt, the user message and the validator inputs."""
    evidence: dict[str, Evidence] = {}
    fact_lines: list[str] = []
    resolved: list[CandidateGroup] = []
    unresolved = False
    text_values: list[str] = []  # candidate statements of an unresolved text key (not a conflict)
    shown = list(inputs.groups)[:MAX_FACT_LINES]
    facts = [g.winner.fact for g in shown if g.winner is not None]
    entry = entry_tier_keys(facts, inputs.default_tier)
    for group in shown:
        winner = group.winner
        if winner is None and all(v.fact.value_num is None for v in group.versions):
            # Conflicts are numeric only (SPEC decision 27): differing text statements are
            # offered side by side, each with its page, and the model judges between them.
            for version in _distinct(group.versions)[:MAX_TEXT_CANDIDATES]:
                resolved.append(group)
                index = len(resolved)
                fact_lines.append(_fact_line(index, version, False))
                evidence[f"F{index}"] = _fact_evidence(index, version, inputs.snapshot_id)
                text_values.append(version.fact.value_text)
            continue
        if winner is None:
            unresolved = True
            fact = group.versions[0].fact
            label = qualifier_phrase(fact.qualifiers) or "no qualifier"
            fact_lines.append(
                f"UNRESOLVED CONFLICT: {fact.entity_label} | {fact.attribute} | {label}: the "
                "sources disagree and no value is current. Do not state any value for it."
            )
            continue
        resolved.append(group)
        index = len(resolved)
        fact_lines.append(_fact_line(index, winner, winner.fact.fact_key in entry))
        evidence[f"F{index}"] = _fact_evidence(index, winner, inputs.snapshot_id)
    chunk_blocks: list[str] = []
    dated_at: list[tuple[str, str]] = []
    for number, chunk in enumerate(_select_chunks(inputs.chunks), start=1):
        if chunk.effective_date or chunk.published:
            dated_at.append((f"C{number}", chunk.effective_date or chunk.published or ""))
        path = " > ".join(chunk.headers) if chunk.headers else chunk.title
        dated = [
            f"{word} {value}"
            for word, value in (
                ("effective", chunk.effective_date),
                ("expires", chunk.expiry_date),
                ("published", chunk.published),
            )
            if value
        ]
        evidence[f"C{number}"] = Evidence(
            f"C{number}",
            chunk.url,
            inputs.snapshot_id,
            chunk.text,
            chunk.authority_rank,
            grounding=" ".join(dated),  # the label is part of what the model read
            chunk_ids=frozenset({chunk.chunk_id, *chunk.member_ids}),
        )
        when = f" [{', '.join(dated)}]" if dated else ""
        chunk_blocks.append(f"[C{number}] {path} ({chunk.url}){when}\n{chunk.text}")
    sections = [
        f"Question: {inputs.question}",
        f"Snapshot date: {inputs.taken_on} (answer as of this date)",
        "Resolved facts (current values; each line is citable by its id):\n"
        + ("\n".join(fact_lines) if fact_lines else "(none)"),
    ]
    if inputs.conflict_notes:
        sections.append("Conflict notes:\n" + "\n".join(f"- {n}" for n in inputs.conflict_notes))
    if inputs.history_lines:
        sections.append(
            "Value history (oldest first; dates are snapshot dates):\n"
            + "\n".join(f"- {line}" for line in inputs.history_lines)
        )
    sections.append("Page text:\n" + ("\n\n".join(chunk_blocks) if chunk_blocks else "(none)"))
    note = _recency_note(dated_at)
    if note:
        sections.append(note)
    winner_texts = [g.winner.fact.value_text for g in resolved if g.winner is not None]
    phrases = [qualifier_phrase(g.winner.fact.qualifiers) or "" for g in resolved if g.winner]
    winners = numbers_of([*winner_texts, *text_values, *phrases, inputs.question, inputs.taken_on])
    allowed = winners | numbers_of(inputs.history_lines)
    return SlowRequest(
        system=SYSTEM_PROMPT,
        user="\n\n".join(sections),
        evidence=evidence,
        allowed=allowed,
        winners=allowed,
        losers=inputs.losers - numbers_of(text_values),
        loser_scopes=inputs.loser_scopes,
        confident=not unresolved,
        chunk_units=_PAGE_UNITS if not _has_tier_siblings(facts) else _THRESHOLD_UNITS,
        relax_stale=bool(inputs.history_lines),
        conflict_note=inputs.conflict_note,
    )


RECENCY_LISTED = 6  # newest dated pages named in the recency note


def _recency_note(dated_at: Sequence[tuple[str, str]]) -> str:
    """Name the newest dated evidence (SRET-5), or ``""`` unless two different dates are shown.

    Each item is a chunk id and its effective date, or its publication date when it has none.
    Pages that state a changing figure (a branch count) disagree by date, and a model tends to
    prefer the page that states it most directly. The note ranks the dated evidence so the
    newest is plain to see. It only labels what the pages say; it is not a resolution step.
    """
    if len({date for _, date in dated_at}) < 2:  # noqa: PLR2004 - two dates make a conflict
        return ""
    newest = sorted(dated_at, key=lambda item: item[1], reverse=True)[:RECENCY_LISTED]
    listed = ", ".join(f"{cid} ({date})" for cid, date in newest)
    return (
        f"Dated evidence, newest first: {listed}.\n"
        "If this evidence states different values for the same thing, the current value is the "
        "one in the newest evidence that states it, even when older evidence states a different "
        "value more directly or more often. Evidence that does not mention the thing does not "
        "count."
    )


# --- the fact-history view (SPEC 13.3) --------------------------------------------------------


def history_view(
    versions: Sequence[FactVersion], snapshots: Sequence[SnapshotRef], target_ordinal: int
) -> list[str]:
    """HIST-1 to HIST-4: values per key with dates, then pages added or removed, capped at 40."""
    dates = {s.ordinal: s.taken_on for s in snapshots}
    visible = [v for v in versions if v.first_seen_ord <= target_ordinal]  # HIST-2
    by_key: dict[str, list[FactVersion]] = {}
    for version in visible:
        by_key.setdefault(version.fact.fact_key, []).append(version)
    rows: list[tuple[int, str, str]] = []
    for key, mine in sorted(by_key.items()):
        runs: list[list[Any]] = []  # [value_text, first, last, label, url]
        for ordinal in range(1, target_ordinal + 1):
            winner = resolve_key_at(mine, ordinal=ordinal, snapshots=snapshots).winner
            if winner is None:
                continue
            value = winner.fact.value_text
            if runs and runs[-1][0] == value and runs[-1][2] == ordinal - 1:
                runs[-1][2] = ordinal
            else:
                label = f"{winner.fact.entity_label} {winner.fact.attribute}"
                runs.append([value, ordinal, ordinal, label, winner.url])
        for value, first, last, label, url in runs:
            line = (
                f"{value} | {label} | first seen {dates.get(first, '?')} | "
                f"last seen {dates.get(last, '?')} | {url}"
            )
            rows.append((first, key, line))
    pages: dict[str, tuple[int, int]] = {}
    for version in visible:
        first, last = pages.get(version.url, (version.first_seen_ord, 0))
        pages[version.url] = (
            min(first, version.first_seen_ord),
            max(last, min(version.last_seen_ord, target_ordinal)),
        )
    for url, (first, last) in sorted(pages.items()):
        if first > 1:
            rows.append((first, url, f"page added | first seen {dates.get(first, '?')} | {url}"))
        if last < target_ordinal:
            rows.append((last + 1, url, f"page removed | last seen {dates.get(last, '?')} | {url}"))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [line for _, _, line in rows][-MAX_HISTORY_LINES:]


# --- the call ---------------------------------------------------------------------------------


class _DeadlineError(LlmError):
    """The deadline passed before a usable reply arrived."""


def _needle(answer: str, text: str) -> str:
    """The first number of ``answer`` that also occurs in ``text`` (to centre a quote)."""
    for match in _NUMBER_TOKEN.finditer(answer):
        if match.group() in text:
            return match.group()
    return ""


class SlowPath:
    """One structured call to the slow model within a deadline (SLOW-1 to SLOW-5)."""

    def __init__(  # noqa: PLR0913
        self,
        client: StructuredLlmClient | None,
        model: str,
        deadline_s: float,
        *,
        effort: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """``client`` is ``None`` without an API key (SLOW-5)."""
        self._client = client
        self._model = model
        self._deadline_s = deadline_s
        self._effort = effort
        self._clock = clock
        self.sleep = sleep
        self._warned = False

    @property
    def available(self) -> bool:
        """True if a client exists, so a payload is worth building."""
        return self._client is not None

    def unavailable(self) -> SlowOutcome:
        """SLOW-5: no client, so no call; one warning per run."""
        if not self._warned:
            self._warned = True
            _log.warning("no API key: slow-path questions are refused")
        return SlowOutcome(None, "no API key")

    def answer(self, request: SlowRequest, question: str) -> SlowOutcome:
        """Call the model and apply SLOW-3; never raises."""
        if self._client is None:
            return self.unavailable()
        try:
            reply = self._call(self._client, request)
        except _DeadlineError:
            return SlowOutcome(None, "deadline exceeded")
        except LlmOutputError:
            return SlowOutcome(None, "malformed output")
        except LlmError as exc:
            return SlowOutcome(None, f"call failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - the slow path never raises
            return SlowOutcome(None, f"call failed: {type(exc).__name__}")
        return self._accept(reply, request, question)

    def _call(self, client: StructuredLlmClient, request: SlowRequest) -> dict[str, Any]:
        deadline = self._clock() + self._deadline_s
        retried = False
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise _DeadlineError("deadline")
            try:
                reply = client.call_structured(
                    model=self._model,
                    system=request.system,
                    user=request.user,
                    schema=SLOW_SCHEMA,
                    max_tokens=SLOW_MAX_TOKENS,
                    timeout_s=remaining,
                    effort=self._effort,
                )
            except LlmTransientError as exc:
                left = deadline - self._clock()
                if retried or left <= self._deadline_s / 2:  # SLOW-4
                    raise
                pause = min(max(exc.retry_after or DEFAULT_RETRY_PAUSE_S, 0.0), MAX_RETRY_PAUSE_S)
                if pause >= left:
                    raise
                self.sleep(pause)
                retried = True
                continue
            if self._clock() > deadline:
                raise _DeadlineError("deadline")
            return reply

    def _accept(  # noqa: PLR0911
        self, reply: object, request: SlowRequest, question: str
    ) -> SlowOutcome:
        if not isinstance(reply, dict):
            return SlowOutcome(None, "malformed output")
        answerable = reply.get("answerable")
        text = reply.get("answer")
        ids = reply.get("evidence_ids")
        losers = reply.get("loser_values_mentioned")
        if (
            not isinstance(answerable, bool)
            or not isinstance(text, str)
            or not isinstance(ids, list)
            or not all(isinstance(i, str) for i in ids)
            or not isinstance(losers, list)
        ):
            return SlowOutcome(None, "malformed output")
        if not answerable:
            why = reply.get("reason")
            detail = f": {why.strip()[:200]}" if isinstance(why, str) and why.strip() else ""
            return SlowOutcome(None, f"not answerable{detail}")
        text = text.strip()
        if not text:
            return SlowOutcome(None, "malformed output: empty answer")
        if losers:
            return SlowOutcome(None, "model mentioned loser values")
        if not ids or any(i not in request.evidence for i in ids):
            return SlowOutcome(None, "unknown or missing evidence id")
        cited = [request.evidence[i] for i in dict.fromkeys(ids)]
        sources = self._sources(cited, text)
        answer = Answer(
            text=text,
            sources=sources,
            confident=request.confident,
            conflict_note=request.conflict_note,
        )
        allowed = (
            request.allowed
            | numbers_of(e.grounding for e in cited)
            | spelled_numbers(e.text for e in cited)
        )
        if request.chunk_units:
            page = numbers_of(e.text for e in cited if e.id.startswith("C"))
            allowed = allowed | {n for n in page if n[0] in request.chunk_units}
        ctx = ValidationContext(
            question=question,
            allowed=allowed,
            losers=request.losers,
            winners=request.winners,
            loser_scopes=request.loser_scopes,
            cited=tuple(
                CitedEvidence(e.chunk_ids, numbers_of([e.text, e.grounding])) for e in cited
            ),
            evidence_urls=frozenset(e.url for e in request.evidence.values()),
            relax_stale=request.relax_stale,
        )
        result = validate(answer, ctx)
        if not result.ok:
            return SlowOutcome(
                None, "validators failed: " + ", ".join(result.failed), result.failed
            )
        return SlowOutcome(answer, "accepted")

    @staticmethod
    def _sources(cited: Sequence[Evidence], text: str) -> list[Source]:
        sources: list[Source] = []
        for item in sorted(cited, key=lambda e: (-e.authority_rank, e.id)):
            needle = item.needle or _needle(text, item.text)
            source = Source(
                url=item.url, snapshot_id=item.snapshot_id, quote=excerpt(item.text, needle)
            )
            if source not in sources:
                sources.append(source)
        return sources[:MAX_SOURCES]


def published_date(texts: Sequence[str]) -> str | None:
    """The publication date of a news or blog page, as ISO text, or ``None`` (SRET-5).

    A page states it as a short chunk that ends with a date ("June 20, 2025"), among its first
    chunks, as the byline of a press release does. A date inside a sentence, or deeper in the
    page, is not a publication date. It is shown to the model as a label only and is never an
    effective date.
    """
    for text in texts[:_PUBLISHED_WITHIN]:
        found = _BYLINE_DATE.search(text) if len(text) <= _BYLINE_CHARS else None
        if found is None:
            continue
        try:
            parsed = datetime.strptime(  # noqa: DTZ007 - a calendar date, no zone
                f"{found['month']} {found['day']} {found['year']}", "%B %d %Y"
            )
        except ValueError:
            continue
        return parsed.date().isoformat()
    return None
