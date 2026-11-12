"""Deterministic table fact extraction and plausibility gates (SPEC 6.7.2, TBL-1 to TBL-11).

Principle: precision first (TBL-10). Whenever a rule cannot decide, no fact is emitted for that
cell, row or table, and the reason is part of the table's logged decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from contract import Page
from kk.ingest.aliases import AliasIndex, canonical_id
from kk.ingest.chunk import carry_down, chunk_blocks, is_layout_table, table_row_text
from kk.ingest.parse import Heading, ParseError, TableBlock, parse_markdown
from kk.log import get_logger
from kk.query.normalise import (
    AmountConstraint,
    clean_text,
    find_terms,
    fold_dashes,
    parse_amount_constraint,
    parse_model_years,
    parse_quantity,
)
from kk.types import Fact

Outcome = Literal["ACCEPT", "ACCEPT_INFERRED", "QUARANTINE", "REJECT"]
KeyFamilies = dict[tuple[str, str], dict[str, str]]
QualifierValue = str | int

P4_MIN_PERCENT = 80  # SPEC 6.7.2, gate P4: share of a measure column's cells that must parse
MAX_VANISHED_KEYS = 3  # TBL-11: more than this many vanished keys of one family is an anomaly

_log = get_logger("tables")


@dataclass(frozen=True)
class HeaderRole:
    """What a lexicon header means: an entity column, a qualifier column or a measure."""

    role: Literal["entity", "qualifier", "measure"]
    attribute: str | None = None
    bound: str | None = None


_ENTITY = HeaderRole("entity")
_QUALIFIER = HeaderRole("qualifier")
LEXICON: dict[str, HeaderRole] = {
    **{h: _ENTITY for h in ("program", "account type", "certificate", "ira type", "loan program")},
    **{h: _QUALIFIER for h in ("balance", "balance tier", "term")},
    "dividend rate": HeaderRole("measure", "dividend_rate"),
    "apy": HeaderRole("measure", "apy"),
    "annual percentage yield (apy)": HeaderRole("measure", "apy"),
    "apr": HeaderRole("measure", "apr"),
    "annual percentage rate": HeaderRole("measure", "apr"),
    "annual percentage rate (as low as)": HeaderRole("measure", "apr", "as_low_as"),
    "rate": HeaderRole("measure", "rate"),
    "min. to open": HeaderRole("measure", "min_to_open"),
    "min. to earn": HeaderRole("measure", "min_to_earn"),
}
_UNITS = {
    "dividend_rate": ("percent", "percent"),
    "apy": ("percent", "percent"),
    "apr": ("percent", "percent"),
    "rate": ("percent", "percent"),
    "min_to_open": ("usd", "usd"),
    "min_to_earn": ("usd", "usd"),
}
_BALANCE_HEADERS = frozenset({"balance", "balance tier"})


def _is_unset(cell: str) -> bool:
    """A blank cell, a dash or ``None``: no qualifier value (TBL-6 explicit none)."""
    return fold_dashes(clean_text(cell)).lower() in {"", "-", "none"}


@dataclass(frozen=True)
class RowRef:
    """What a fact takes from the chunk of its table row."""

    chunk_id: str
    effective_date: str | None
    expiry_date: str | None
    authority_rank: int


@dataclass(frozen=True)
class TableDecision:
    """The one logged decision per table, with the facts it emitted."""

    outcome: Outcome
    gate: str | None
    reason: str
    heading: str
    facts: tuple[Fact, ...]
    unmapped_headers: tuple[str, ...]
    dropped_rows: int


@dataclass(frozen=True)
class PageExtraction:
    """All table decisions of one page."""

    url: str
    decisions: tuple[TableDecision, ...]

    @property
    def facts(self) -> tuple[Fact, ...]:
        """Facts of every accepted table."""
        return tuple(f for d in self.decisions for f in d.facts)


@dataclass(frozen=True)
class DiffResult:
    """TBL-11 outcome: anomalies found and the ``(entity, attribute)`` pairs marked degraded."""

    anomalies: tuple[tuple[str, str, int], ...]
    degraded: frozenset[tuple[str, str]]


# --- TBL-2 headers ---------------------------------------------------------------------------

_TRAILING_FOOTNOTE = re.compile(r"(?<=[A-Za-z)])\d+$|(?<=\))\s+\d+$")


def normalise_header(text: str) -> str:
    """TBL-2: strip footnote digits and markers, unescape, collapse whitespace, lowercase."""
    cleaned = clean_text(text.replace("\\", "").replace("*", ""))
    return _TRAILING_FOOTNOTE.sub("", cleaned).strip().lower()


def make_fact_key(entity: str, attribute: str, qualifiers: str) -> str:
    """``sha256(entity + "|" + attribute + "|" + qualifiers)`` as hex (SPEC 5.2)."""
    return hashlib.sha256(f"{entity}|{attribute}|{qualifiers}".encode()).hexdigest()


def _qualifier_json(qualifiers: Mapping[str, QualifierValue]) -> str:
    return json.dumps(dict(qualifiers), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# --- TBL-5 labels ----------------------------------------------------------------------------

_N = r"\d[\d,]*(?:\.\d+)?"
_AMOUNT_PHRASE = re.compile(
    rf"\$\s*{_N}\s*(?:-|to)\s*\$\s*{_N}|\$\s*{_N}\s*(?:\+|or\s+(?:less|more))|\$\s*{_N}",
    re.IGNORECASE,
)
_MODEL_YEAR_PHRASE = re.compile(
    r"(?<![\w,.$])(?:\d{4}\s*-\s*(?:\d{4}|\d{2})(?:\s+model(?:\s+years?)?)?"
    r"|\d{4}\s+model(?:\s+year)?)(?!\w)",
    re.IGNORECASE,
)
_PRODUCT_WORD = re.compile(
    r"\b(purchase|refinance|fixed|arm|adjustable|conventional|jumbo)\b", re.IGNORECASE
)
_PRODUCT_QUALIFIER = {
    "purchase": ("product", "purchase"),
    "refinance": ("product", "refinance"),
    "fixed": ("rate_type", "fixed"),
    "arm": ("rate_type", "arm"),
    "adjustable": ("rate_type", "arm"),
    "conventional": ("loan_type", "conventional"),
    "jumbo": ("loan_type", "jumbo"),
}
_BAD_RESIDUAL = re.compile(r"\$|(?<!\w)\d[\d,.]*(?!\w)")
_PUNCTUATION = re.compile(r"[^\w\s]|_")


@dataclass(frozen=True)
class _Label:
    """A parsed row label or qualifier cell. ``ok`` is false when it cannot be decided."""

    ok: bool
    qualifiers: dict[str, QualifierValue] = field(default_factory=dict)
    residual: str = ""
    amount: AmountConstraint | None = None


_FAILED = _Label(ok=False)


def _fmt(number: Decimal) -> str:
    return format(number.normalize(), "f")


def _amount_text(constraint: AmountConstraint) -> str | None:
    kind = constraint.kind
    if kind == "band" and constraint.lower is not None and constraint.upper is not None:
        return f"{_fmt(constraint.lower)}-{_fmt(constraint.upper)}"
    if kind == "point" and constraint.lower is not None:
        return f"={_fmt(constraint.lower)}"
    if kind == "floor" and constraint.lower is not None:
        return f"{'>=' if constraint.lower_inclusive else '>'}{_fmt(constraint.lower)}"
    if kind == "cap" and constraint.upper is not None:
        return f"{'<=' if constraint.upper_inclusive else '<'}{_fmt(constraint.upper)}"
    return None


def _cut(text: str, spans: Sequence[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    return clean_text(text)


def _parse_label(text: str, *, amount_key: str) -> _Label:  # noqa: PLR0911, PLR0912
    """TBL-5: split a label into qualifiers and a residual name; fail on anything unclear."""
    rest = clean_text(fold_dashes(text.replace("*", " ")))
    qualifiers: dict[str, QualifierValue] = {}

    amounts = list(_AMOUNT_PHRASE.finditer(rest))
    if len(amounts) > 1:
        return _FAILED
    constraint: AmountConstraint | None = None
    if amounts:
        constraint = parse_amount_constraint(amounts[0].group(0))
        label_text = _amount_text(constraint) if constraint is not None else None
        if constraint is None or label_text is None:
            return _FAILED
        qualifiers[amount_key] = label_text
        rest = _cut(rest, [(amounts[0].start(), amounts[0].end())])

    years = []
    for match in _MODEL_YEAR_PHRASE.finditer(rest):
        parsed = parse_model_years(match.group(0))
        if parsed is not None:
            years.append((match, parsed))
    if len(years) > 1:
        return _FAILED
    if years:
        match, parsed = years[0]
        qualifiers["model_year"] = (
            str(parsed.first) if parsed.first == parsed.last else f"{parsed.first}-{parsed.last}"
        )
        rest = _cut(rest, [(match.start(), match.end())])

    terms = find_terms(rest)
    if len(terms) > 1:
        return _FAILED
    if terms:
        qualifiers["term"] = terms[0].months
        rest = _cut(rest, [(terms[0].start, terms[0].end)])

    for match in _PRODUCT_WORD.finditer(rest):
        key, value = _PRODUCT_QUALIFIER[match.group(1).lower()]
        if qualifiers.setdefault(key, value) != value:
            return _FAILED
    rest = clean_text(_PRODUCT_WORD.sub(" ", rest))

    residual = clean_text(_PUNCTUATION.sub(" ", rest))
    if _BAD_RESIDUAL.search(residual):
        return _FAILED
    return _Label(ok=True, qualifiers=qualifiers, residual=residual, amount=constraint)


# --- TBL-6 values ----------------------------------------------------------------------------

_PERCENT_FOOTNOTE = re.compile(r"(%)\d+$")


@dataclass(frozen=True)
class _Value:
    text: str
    number: Decimal | None
    none: bool


def _parse_value(cell: str, attribute: str) -> _Value | None:
    """A percent, an amount or an explicit none, matching the attribute's type, else ``None``."""
    shown = cell.strip()
    if fold_dashes(clean_text(shown)).lower() in {"-", "none"}:
        return _Value(shown, None, True)
    shown = _PERCENT_FOOTNOTE.sub(r"\1", shown)
    quantity = parse_quantity(shown)
    if quantity is None or quantity.kind != _UNITS[attribute][0]:
        return None
    return _Value(shown, quantity.value, False)


# --- table analysis --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Columns:
    roles: tuple[HeaderRole | None, ...]
    names: tuple[str, ...]
    entity: int | None
    qualifiers: tuple[int, ...]
    measures: tuple[int, ...]
    unmapped: tuple[str, ...]


def _columns(table: TableBlock) -> _Columns:
    names = tuple(normalise_header(h) for h in table.header)
    roles = tuple(LEXICON.get(name) for name in names)
    entities = [i for i, r in enumerate(roles) if r is not None and r.role == "entity"]
    return _Columns(
        roles,
        names,
        entities[0] if entities else None,
        tuple(i for i, r in enumerate(roles) if r is not None and r.role == "qualifier"),
        tuple(i for i, r in enumerate(roles) if r is not None and r.role == "measure"),
        tuple(sorted({n for n, r in zip(names, roles, strict=True) if r is None and n})),
    )


@dataclass(frozen=True)
class _Row:
    index: int
    cells: tuple[str, ...]
    entity: str
    entity_label: str
    inferred: bool
    qualifiers: dict[str, QualifierValue]
    band: AmountConstraint | None


def _build_row(
    index: int,
    cells: tuple[str, ...],
    columns: _Columns,
    heading: str,
    aliases: AliasIndex,
) -> _Row | None:
    """Entity and qualifiers of one row, or ``None`` if the row cannot be decided."""
    qualifiers: dict[str, QualifierValue] = {}
    band: AmountConstraint | None = None
    label = _Label(ok=True)
    if columns.entity is not None:
        label = _parse_label(cells[columns.entity], amount_key="amount_tier")
        if not label.ok:
            return None
        qualifiers.update(label.qualifiers)
    for col in columns.qualifiers:
        if _is_unset(cells[col]):
            continue
        key = "balance_band" if columns.names[col] in _BALANCE_HEADERS else "amount_tier"
        parsed = _parse_label(cells[col], amount_key=key)
        if not parsed.ok or parsed.residual or set(parsed.qualifiers) & set(qualifiers):
            return None
        qualifiers.update(parsed.qualifiers)
        if key == "balance_band":
            band = parsed.amount
    if label.residual:
        entity, entity_label, inferred = canonical_id(label.residual), label.residual, False
    else:
        resolved = aliases.resolve(heading)
        if resolved is None:
            return None
        entity, entity_label, inferred = resolved, heading, True
    return _Row(index, cells, entity, entity_label, inferred, qualifiers, band)


def _column_coherent(columns: _Columns, rows: Sequence[_Row]) -> bool:
    """P5, first half: each qualifier column holds one kind of value."""
    for col in columns.qualifiers:
        for row in rows:
            if _is_unset(row.cells[col]):
                continue
            if columns.names[col] in _BALANCE_HEADERS:
                if row.band is None:
                    return False
            elif "term" not in row.qualifiers:
                return False
    return True


def _interval(band: AmountConstraint) -> tuple[Decimal | None, Decimal | None]:
    return band.lower, band.upper


def _overlap(a: AmountConstraint, b: AmountConstraint) -> bool:
    (alo, ahi), (blo, bhi) = _interval(a), _interval(b)
    below = (
        ahi is not None
        and blo is not None
        and (ahi < blo or (ahi == blo and not (a.upper_inclusive and b.lower_inclusive)))
    )
    above = (
        bhi is not None
        and alo is not None
        and (bhi < alo or (bhi == alo and not (b.upper_inclusive and a.lower_inclusive)))
    )
    return not (below or above)


def _bands_ordered(bands: Sequence[AmountConstraint]) -> bool:
    """Bands of one entity do not overlap and run in one direction."""
    for i, first in enumerate(bands):
        if any(_overlap(first, other) for other in bands[i + 1 :]):
            return False
    lows = [b.lower if b.lower is not None else Decimal("-Infinity") for b in bands]
    return lows == sorted(lows) or lows == sorted(lows, reverse=True)


def _bands_coherent(rows: Sequence[_Row]) -> bool:
    by_entity: dict[str, list[AmountConstraint]] = {}
    for row in rows:
        if row.band is not None:
            by_entity.setdefault(row.entity, []).append(row.band)
    return all(_bands_ordered(bands) for bands in by_entity.values())


def _emit(row: _Row, columns: _Columns, values: Mapping[int, _Value], ref: RowRef) -> list[Fact]:
    facts = []
    for col in columns.measures:
        value = values.get(col)
        role = columns.roles[col]
        if value is None or role is None or role.attribute is None:
            continue
        qualifiers = dict(row.qualifiers)
        if role.bound is not None:
            qualifiers["bound"] = role.bound
        encoded = _qualifier_json(qualifiers)
        facts.append(
            Fact(
                entity=row.entity,
                entity_label=row.entity_label,
                attribute=role.attribute,
                qualifiers=encoded,
                fact_key=make_fact_key(row.entity, role.attribute, encoded),
                value_text=value.text,
                value_num=value.number,
                unit=_UNITS[role.attribute][1],
                none_value=value.none,
                quote=" | ".join(row.cells),
                chunk_id=ref.chunk_id,
                effective_date=ref.effective_date,
                expiry_date=ref.expiry_date,
                authority_rank=ref.authority_rank,
                entity_inferred=row.inferred,
                origin="table_rule",
            )
        )
    return facts


def _decision(  # noqa: PLR0913
    outcome: Outcome,
    gate: str | None,
    reason: str,
    heading: str,
    columns: _Columns,
    *,
    facts: Sequence[Fact] = (),
    dropped: int = 0,
) -> TableDecision:
    return TableDecision(outcome, gate, reason, heading, tuple(facts), columns.unmapped, dropped)


def _pick_ref(refs: Sequence[RowRef | None] | RowRef, index: int) -> RowRef | None:
    if isinstance(refs, RowRef):
        return refs
    return refs[index] if index < len(refs) else None


_INVARIANTS = (("apy", "dividend_rate"), ("apr", "rate"))


def _invariants_hold(facts: Sequence[Fact]) -> bool:
    """P7: ``apy >= dividend_rate`` and ``apr >= rate`` within each row."""
    by_row: dict[tuple[str, str], dict[str, Decimal]] = {}
    for fact in facts:
        if fact.value_num is not None:
            by_row.setdefault((fact.quote, fact.qualifiers), {})[fact.attribute] = fact.value_num
    return all(
        values[high] >= values[low]
        for values in by_row.values()
        for high, low in _INVARIANTS
        if high in values and low in values
    )


def extract_table(  # noqa: PLR0911, PLR0912
    table: TableBlock,
    *,
    heading: str,
    aliases: AliasIndex,
    refs: Sequence[RowRef | None] | RowRef,
) -> TableDecision:
    """Run layout detection, the lexicon, the gates P1 to P7 and fact emission on one table."""
    columns = _columns(table)
    if is_layout_table(table):
        return _decision("REJECT", "TBL-1", "layout table", heading, columns)
    rows = carry_down(table)
    if len(columns.names) < 2 or not rows:  # noqa: PLR2004
        return _decision("REJECT", "P1", "no data rows or fewer than two headers", heading, columns)
    if columns.entity is None and aliases.resolve(heading) is None:
        return _decision(
            "REJECT", "P2", "no entity column and the heading is not an alias", heading, columns
        )
    if not columns.measures:
        return _decision("REJECT", "P3", "no header maps to a measure", heading, columns)

    parsed: dict[int, dict[int, _Value]] = {i: {} for i in range(len(rows))}
    for col in columns.measures:
        role = columns.roles[col]
        attribute = role.attribute if role is not None and role.attribute else "rate"
        for i, record in enumerate(rows):
            value = _parse_value(record[col], attribute)
            if value is not None:
                parsed[i][col] = value
        good = sum(1 for i in range(len(rows)) if col in parsed[i])
        if good * 100 < P4_MIN_PERCENT * len(rows):
            reason = f"column {columns.names[col]!r}: {good} of {len(rows)} cells parse"
            return _decision("REJECT", "P4", reason, heading, columns)

    built: list[_Row] = []
    dropped = 0
    for i, cells in enumerate(rows):
        row = _build_row(i, cells, columns, heading, aliases)
        if row is None or _pick_ref(refs, i) is None:
            dropped += 1
        else:
            built.append(row)
    if not built:
        return _decision(
            "REJECT", "TBL-10", "no row could be decided", heading, columns, dropped=dropped
        )

    if not _column_coherent(columns, built) or not _bands_coherent(built):
        return _decision(
            "QUARANTINE", "P5", "qualifier column is incoherent", heading, columns, dropped=dropped
        )

    facts: list[Fact] = []
    seen: dict[str, Fact] = {}
    for row in built:
        ref = _pick_ref(refs, row.index)
        if ref is None:  # pragma: no cover - such rows were dropped above
            continue
        for fact in _emit(row, columns, parsed[row.index], ref):
            earlier = seen.get(fact.fact_key)
            if earlier is None:
                seen[fact.fact_key] = fact
                facts.append(fact)
            elif (earlier.value_num, earlier.none_value) != (fact.value_num, fact.none_value):
                return _decision(
                    "QUARANTINE",
                    "P6",
                    "one fact key has two values",
                    heading,
                    columns,
                    dropped=dropped,
                )
    if not _invariants_hold(facts):
        return _decision(
            "QUARANTINE",
            "P7",
            "apy < dividend rate or apr < rate",
            heading,
            columns,
            dropped=dropped,
        )
    if not facts:
        return _decision(
            "REJECT", "TBL-10", "no cell gave a fact", heading, columns, dropped=dropped
        )
    outcome: Outcome = "ACCEPT_INFERRED" if any(f.entity_inferred for f in facts) else "ACCEPT"
    return _decision(outcome, None, "ok", heading, columns, facts=facts, dropped=dropped)


# --- pages -----------------------------------------------------------------------------------


def _page_title(page: Page, h1: str) -> str:
    return clean_text(page.title) or h1


def entity_labels(page: Page) -> set[str]:
    """Labels for the alias builder: the page title and entity-column row labels (AL-1)."""
    try:
        blocks = parse_markdown(page.markdown)
    except ParseError:
        return set()
    h1 = next((b.text for b in blocks if isinstance(b, Heading) and b.level == 1), "")
    labels = {_page_title(page, h1)} - {""}
    for block in blocks:
        if not isinstance(block, TableBlock) or is_layout_table(block):
            continue
        columns = _columns(block)
        if columns.entity is None:
            continue
        for row in carry_down(block):
            label = _parse_label(row[columns.entity], amount_key="amount_tier")
            if label.ok and label.residual:
                labels.add(label.residual)
    return labels


def extract_page(page: Page, aliases: AliasIndex) -> PageExtraction:
    """Parse and chunk ``page`` and extract facts from each of its tables.

    Raises:
        ParseError: the page is empty, too large or cannot be parsed (ING-6).
    """
    blocks = parse_markdown(page.markdown)
    chunked = chunk_blocks(blocks, url=page.url, page_title=page.title, fetched_at=page.fetched_at)
    rows_by_text = {c.text: c for c in chunked.chunks if c.chunk_type == "table_row"}
    title = chunked.chunks[0].title if chunked.chunks else ""
    heading = ""
    decisions: list[TableDecision] = []
    for block in blocks:
        if isinstance(block, Heading):
            heading = block.text
        elif isinstance(block, TableBlock):
            refs: list[RowRef | None] = []
            for row in carry_down(block):
                chunk = rows_by_text.get(table_row_text(title, heading, block.header, row))
                refs.append(
                    RowRef(
                        chunk.chunk_id,
                        chunk.effective_date,
                        chunk.expiry_date,
                        chunk.authority_rank,
                    )
                    if chunk is not None
                    else None
                )
            decision = extract_table(block, heading=heading, aliases=aliases, refs=refs)
            decisions.append(decision)
            _log.info(
                "table decision",
                extra={
                    "url": page.url,
                    "heading": heading,
                    "outcome": decision.outcome,
                    "gate": decision.gate,
                    "reason": decision.reason,
                    "facts": len(decision.facts),
                    "unmapped_headers": list(decision.unmapped_headers),
                },
            )
    return PageExtraction(page.url, tuple(decisions))


# --- TBL-11 snapshot diff --------------------------------------------------------------------


def family_index(facts: Sequence[Fact], urls: Mapping[str, str]) -> KeyFamilies:
    """Group fact keys by ``(entity, attribute)``, each with the url of its page."""
    families: KeyFamilies = {}
    for fact in facts:
        url = urls.get(fact.chunk_id)
        if url is not None:
            families.setdefault((fact.entity, fact.attribute), {})[fact.fact_key] = url
    return families


def diff_fact_keys(
    previous: KeyFamilies, current: KeyFamilies, live_urls: frozenset[str]
) -> DiffResult:
    """TBL-11: mark families that lost more than three keys while their page still exists."""
    anomalies: list[tuple[str, str, int]] = []
    for (entity, attribute), keys in sorted(previous.items()):
        now = current.get((entity, attribute), {})
        vanished = sum(1 for key, url in keys.items() if key not in now and url in live_urls)
        if vanished > MAX_VANISHED_KEYS:
            anomalies.append((entity, attribute, vanished))
    return DiffResult(tuple(anomalies), frozenset((e, a) for e, a, _ in anomalies))
