"""Builders for synthetic facts, intervals and snapshots (Step 5). No real site text."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal

from kk.ingest.dedupe import Interval, make_chunk_id
from kk.ingest.tables import make_fact_key
from kk.query.normalise import parse_quantity
from kk.types import Chunk, Fact, FactVersion, SnapshotRef

DATES = ("2026-05-12", "2026-05-19", "2026-06-09", "2026-08-27", "2026-09-28")


def snaps(*dates: str) -> tuple[SnapshotRef, ...]:
    """Snapshot refs ``s1``, ``s2``, ... with the given ``taken_on`` dates."""
    return tuple(
        SnapshotRef(ordinal=i, snapshot_id=f"s{i}", taken_on=d) for i, d in enumerate(dates, 1)
    )


SNAPS = snaps(*DATES)


def encode(qualifiers: dict[str, str | int]) -> str:
    """Qualifier JSON as the table extractor writes it."""
    return json.dumps(qualifiers, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_fact(  # noqa: PLR0913
    *,
    value: str = "3.50%",
    entity: str = "certificate",
    label: str = "Certificate",
    attribute: str = "apy",
    qualifiers: dict[str, str | int] | None = None,
    chunk_id: str = "chunk-1",
    effective: str | None = None,
    expiry: str | None = None,
    rank: int = 6,
    origin: Literal["table_rule", "prose_llm"] = "table_rule",
    inferred: bool = False,
    none: bool = False,
    quote: str | None = None,
) -> Fact:
    """A synthetic ``Fact``; ``value_num`` is parsed from ``value`` unless ``none``."""
    encoded = encode(qualifiers or {})
    quantity = None if none else parse_quantity(value)
    number: Decimal | None = quantity.value if quantity is not None else None
    return Fact(
        entity=entity,
        entity_label=label,
        attribute=attribute,
        qualifiers=encoded,
        fact_key=make_fact_key(entity, attribute, encoded),
        value_text=value,
        value_num=number,
        unit="percent" if value.endswith("%") else "usd" if value.startswith("$") else None,
        none_value=none,
        quote=quote if quote is not None else f"{label} | {value}",
        chunk_id=chunk_id,
        effective_date=effective,
        expiry_date=expiry,
        authority_rank=rank,
        entity_inferred=inferred,
        origin=origin,
    )


def make_version(  # noqa: PLR0913
    *,
    value: str = "3.50%",
    url: str = "https://example.test/rates/certificates",
    title: str = "Certificate Rates",
    first: int = 1,
    last: int | None = None,
    effective: str | None = None,
    expiry: str | None = None,
    rank: int = 6,
    source_class: str = "rate_sheet",
    fetched_at: str = "2026-05-12T10:00:00.000Z",
    origin: Literal["table_rule", "prose_llm"] = "table_rule",
    inferred: bool = False,
    none: bool = False,
    entity: str = "certificate",
    label: str = "Certificate",
    attribute: str = "apy",
    qualifiers: dict[str, str | int] | None = None,
) -> FactVersion:
    """A ``FactVersion`` whose chunk lives on ``url`` and is visible from ``first`` to ``last``."""
    last = first if last is None else last
    text = f"{label} | {value} | {url} | {first}"
    headers = ("Page", "Section")
    chunk_id = make_chunk_id(url, headers, text)
    chunk = Chunk(
        chunk_id=chunk_id,
        url=url,
        file_name=url.rsplit("/", 1)[-1],
        title=title,
        parent_headers=headers,
        chunk_type="table_row",
        parent_id=None,
        text=text,
        effective_date=effective,
        expiry_date=expiry,
        source_class=source_class,
        authority_rank=rank,
        fetched_at=fetched_at,
        position=0,
    )
    interval = Interval(
        chunk=chunk,
        first_seen_ord=first,
        last_seen_ord=last,
        first_seen_date=None,
        last_seen_date=None,
        fetched_at=fetched_at,
    )
    fact = make_fact(
        value=value,
        entity=entity,
        label=label,
        attribute=attribute,
        qualifiers=qualifiers,
        chunk_id=chunk_id,
        effective=effective,
        expiry=expiry,
        rank=rank,
        origin=origin,
        inferred=inferred,
        none=none,
        quote=f"{label} {value}",
    )
    return FactVersion(fact, interval)
