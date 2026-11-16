"""LanceDB access: schema, interval persistence, filtered search, joins (SPEC 5, 6.4, 6.8).

Three tables live in one embedded database under the work directory: ``chunks`` (one row per
chunk interval), ``facts`` (one row per fact and chunk interval) and ``conflicts`` (rebuilt at
the end of every ingest). Every search applies the validity filter as a pre-filter (SPEC 5.4).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import lancedb
import numpy as np
import pyarrow as pa
from lancedb.index import FTS, BTree

from contract import Source
from kk.ingest.dedupe import Interval, IntervalStore
from kk.ingest.embed import DIM, EmbedderLike, embed_in_batches
from kk.log import get_logger
from kk.types import Chunk, ConflictRecord, Fact, FactVersion, SnapshotRef

EMBEDDED_TYPES = frozenset({"table_row", "text_paragraph", "list_item"})  # EMB-2
_IN_BATCH = 400  # ids per ``IN (...)`` clause
_ALL_ROWS = 10_000_000
_TOKEN = re.compile(r"[A-Za-z0-9]+")
_VALUE_PRECISION = 28
_VALUE_SCALE = 10

_log = get_logger("store")

_CHUNKS = pa.schema(
    [
        ("chunk_id", pa.string()),
        ("url", pa.string()),
        ("file_name", pa.string()),
        ("title", pa.string()),
        ("parent_headers", pa.list_(pa.string())),
        ("chunk_type", pa.string()),
        ("parent_id", pa.string()),
        ("text", pa.string()),
        ("vector", pa.list_(pa.float32(), DIM)),
        ("first_seen_ord", pa.int32()),
        ("last_seen_ord", pa.int32()),
        ("first_seen_date", pa.date32()),
        ("last_seen_date", pa.date32()),
        ("effective_date", pa.date32()),
        ("expiry_date", pa.date32()),
        ("source_class", pa.string()),
        ("authority_rank", pa.int32()),
        ("fetched_at", pa.string()),
        ("position", pa.int32()),
        ("row_index", pa.int32()),
        ("row_count", pa.int32()),
        ("footer_count", pa.int32()),
    ]
)
_CHUNK_COLUMNS = [f.name for f in _CHUNKS if f.name != "vector"]
_FACTS = pa.schema(
    [
        ("fact_id", pa.string()),
        ("entity", pa.string()),
        ("entity_label", pa.string()),
        ("attribute", pa.string()),
        ("qualifiers", pa.string()),
        ("fact_key", pa.string()),
        ("value_text", pa.string()),
        ("value_num", pa.decimal128(_VALUE_PRECISION, _VALUE_SCALE)),
        ("unit", pa.string()),
        ("none_value", pa.bool_()),
        ("quote", pa.string()),
        ("chunk_id", pa.string()),
        ("first_seen_ord", pa.int32()),
        ("last_seen_ord", pa.int32()),
        ("effective_date", pa.date32()),
        ("expiry_date", pa.date32()),
        ("authority_rank", pa.int32()),
        ("entity_inferred", pa.bool_()),
        ("origin", pa.string()),
    ]
)
_SOURCE = pa.struct([("url", pa.string()), ("snapshot_id", pa.string()), ("quote", pa.string())])
_CONFLICTS = pa.schema(
    [
        ("conflict_id", pa.string()),
        ("topic", pa.string()),
        ("fact_key", pa.string()),
        ("values", pa.list_(pa.string())),
        ("sources", pa.list_(_SOURCE)),
        ("resolution", pa.string()),
        ("strategy", pa.string()),
        ("reason", pa.string()),
        ("ords", pa.list_(pa.int32())),
    ]
)
_EXTEND_COLUMNS = (
    "last_seen_ord",
    "last_seen_date",
    "fetched_at",
    "position",
    "row_index",
    "row_count",
    "footer_count",
)


@dataclass(frozen=True)
class StoredHit:
    """A chunk returned by a search, with its interval and the raw score of that search."""

    chunk: Chunk
    first_seen_ord: int
    last_seen_ord: int
    score: float
    vector: tuple[float, ...] | None


@dataclass(frozen=True)
class StoredInterval:
    """The interval columns of one chunk row."""

    first_seen_ord: int
    last_seen_ord: int
    first_seen_date: str | None
    last_seen_date: str | None
    fetched_at: str


@dataclass(frozen=True)
class StoreDelta:
    """What one ``add_snapshot`` wrote."""

    new_chunks: int
    extended_chunks: int
    embedded: int
    new_facts: int
    extended_facts: int
    embed_seconds: float


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _in_list(values: Iterable[str]) -> str:
    return ", ".join(_quote(v) for v in values)


def _batches(values: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), _IN_BATCH):
        yield values[start : start + _IN_BATCH]


def _to_date(text: str | None) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _visible(ordinal: int) -> str:
    return f"first_seen_ord <= {int(ordinal)} AND last_seen_ord >= {int(ordinal)}"


def fact_id_of(fact: Fact, first_seen_ord: int) -> str:
    """SPEC 5.2: ``sha256(fact_key + value + chunk_id + first_seen_ord)``."""
    payload = f"{fact.fact_key}{fact.value_text}{fact.chunk_id}{first_seen_ord}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _chunk_from_row(row: dict[str, Any]) -> Chunk:
    return Chunk(
        chunk_id=row["chunk_id"],
        url=row["url"],
        file_name=row["file_name"],
        title=row["title"],
        parent_headers=tuple(row["parent_headers"]),
        chunk_type=row["chunk_type"],
        parent_id=row["parent_id"],
        text=row["text"],
        effective_date=_iso(row["effective_date"]),
        expiry_date=_iso(row["expiry_date"]),
        source_class=row["source_class"],
        authority_rank=row["authority_rank"],
        fetched_at=row["fetched_at"],
        position=row["position"],
        row_index=row["row_index"],
        row_count=row["row_count"],
        footer_count=row["footer_count"],
    )


def _fact_from_row(row: dict[str, Any]) -> Fact:
    number: Decimal | None = row["value_num"]
    return Fact(
        entity=row["entity"],
        entity_label=row["entity_label"],
        attribute=row["attribute"],
        qualifiers=row["qualifiers"],
        fact_key=row["fact_key"],
        value_text=row["value_text"],
        value_num=number,
        unit=row["unit"],
        none_value=row["none_value"],
        quote=row["quote"],
        chunk_id=row["chunk_id"],
        effective_date=_iso(row["effective_date"]),
        expiry_date=_iso(row["expiry_date"]),
        authority_rank=row["authority_rank"],
        entity_inferred=row["entity_inferred"],
        origin=row["origin"],
    )


class Store:
    """The embedded vector database and the in-memory interval state of the current ingest."""

    def __init__(self, path: Path, *, max_batch_chars: int) -> None:
        """Open (or create) the database under ``path`` with its three empty tables.

        ``max_batch_chars`` is the embedding batch limit (``embed.max_batch_chars``).
        """
        self.max_batch_chars = max_batch_chars
        path.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(path))
        self._chunks = self._open("chunks", _CHUNKS)
        self._facts = self._open("facts", _FACTS)
        self._conflicts = self._open("conflicts", _CONFLICTS)
        self._intervals = IntervalStore()
        self._vectors: dict[str, np.ndarray] = {}
        self._fact_ids: set[str] = set()
        self._snapshots: list[SnapshotRef] = []

    def _open(self, name: str, schema: pa.Schema) -> Any:  # noqa: ANN401 - lancedb table type
        if name in self._db.list_tables().tables:
            return self._db.open_table(name)
        return self._db.create_table(name, schema=schema)

    @property
    def snapshots(self) -> tuple[SnapshotRef, ...]:
        """The ``(ord, snapshot_id, taken_on)`` list (ING-1)."""
        return tuple(self._snapshots)

    def row_count(self, table: str) -> int:
        """Rows in ``chunks``, ``facts`` or ``conflicts``."""
        tables = {"chunks": self._chunks, "facts": self._facts, "conflicts": self._conflicts}
        return int(tables[table].count_rows())

    # --- writing -----------------------------------------------------------------------------

    def add_snapshot(
        self,
        ref: SnapshotRef,
        chunks: Sequence[Chunk],
        facts: Sequence[Fact],
        embedder: EmbedderLike,
    ) -> StoreDelta:
        """Persist snapshot ``ref``: extend or open intervals, embed new text, write facts.

        Raises:
            EmbeddingError: A batch failed twice (the index would be incomplete).
        """
        import time  # noqa: PLC0415

        delta = self._intervals.add_snapshot(ref.ordinal, ref.taken_on, chunks)
        pending = {
            i.chunk.chunk_id: i.chunk.text
            for i in delta.new
            if i.chunk.chunk_type in EMBEDDED_TYPES and i.chunk.chunk_id not in self._vectors
        }
        started = time.perf_counter()
        ids = list(pending)
        vectors = embed_in_batches(
            embedder, [pending[i] for i in ids], max_batch_chars=self.max_batch_chars
        )
        embed_seconds = time.perf_counter() - started
        if len(ids) != len(vectors) or (ids and vectors.shape[1] != DIM):
            raise ValueError("embedder returned the wrong number of vectors")
        self._vectors.update(zip(ids, vectors, strict=True))
        if delta.new:
            self._chunks.add(
                pa.Table.from_pylist([self._chunk_row(i) for i in delta.new], schema=_CHUNKS),
                on_bad_vectors="null",
            )
        if delta.extended:
            self._chunks.merge_insert(
                ["chunk_id", "first_seen_ord"]
            ).when_matched_update_all().execute(self._extension_table(delta.extended))
        new_facts, extended_facts = self._write_facts(ref.ordinal, facts)
        self._snapshots.append(ref)
        self._build_indices()
        return StoreDelta(
            len(delta.new),
            len(delta.extended),
            len(ids),
            new_facts,
            extended_facts,
            embed_seconds,
        )

    def _chunk_row(self, interval: Interval) -> dict[str, Any]:
        chunk = interval.chunk
        vector = self._vectors.get(chunk.chunk_id)
        return {
            "chunk_id": chunk.chunk_id,
            "url": chunk.url,
            "file_name": chunk.file_name,
            "title": chunk.title,
            "parent_headers": list(chunk.parent_headers),
            "chunk_type": chunk.chunk_type,
            "parent_id": chunk.parent_id,
            "text": chunk.text,
            "vector": vector.tolist() if vector is not None else None,
            "first_seen_ord": interval.first_seen_ord,
            "last_seen_ord": interval.last_seen_ord,
            "first_seen_date": interval.first_seen_date,
            "last_seen_date": interval.last_seen_date,
            "effective_date": _to_date(chunk.effective_date),
            "expiry_date": _to_date(chunk.expiry_date),
            "source_class": chunk.source_class,
            "authority_rank": chunk.authority_rank,
            "fetched_at": interval.fetched_at,
            "position": chunk.position,
            "row_index": chunk.row_index,
            "row_count": chunk.row_count,
            "footer_count": chunk.footer_count,
        }

    @staticmethod
    def _extension_table(extended: Sequence[Interval]) -> pa.Table:
        fields = [_CHUNKS.field(n) for n in ("chunk_id", "first_seen_ord", *_EXTEND_COLUMNS)]
        rows = [
            {
                "chunk_id": i.chunk.chunk_id,
                "first_seen_ord": i.first_seen_ord,
                "last_seen_ord": i.last_seen_ord,
                "last_seen_date": i.last_seen_date,
                "fetched_at": i.fetched_at,
                "position": i.chunk.position,
                "row_index": i.chunk.row_index,
                "row_count": i.chunk.row_count,
                "footer_count": i.chunk.footer_count,
            }
            for i in extended
        ]
        return pa.Table.from_pylist(rows, schema=pa.schema(fields))

    def _write_facts(self, ordinal: int, facts: Sequence[Fact]) -> tuple[int, int]:
        by_id: dict[str, tuple[Fact, int]] = {}
        for fact in facts:
            interval = self._intervals.current(fact.chunk_id)
            if interval is None or interval.last_seen_ord != ordinal:
                _log.warning("fact without a chunk in this snapshot; skipped")
                continue
            by_id[fact_id_of(fact, interval.first_seen_ord)] = (fact, interval.first_seen_ord)
        fresh = {k: v for k, v in by_id.items() if k not in self._fact_ids}
        extend = [k for k in by_id if k in self._fact_ids]
        if fresh:
            rows = [self._fact_row(k, f, first, ordinal) for k, (f, first) in fresh.items()]
            self._facts.add(pa.Table.from_pylist(rows, schema=_FACTS))
            self._fact_ids.update(fresh)
        if extend:
            update = pa.Table.from_pylist(
                [{"fact_id": k, "last_seen_ord": ordinal} for k in extend],
                schema=pa.schema([_FACTS.field("fact_id"), _FACTS.field("last_seen_ord")]),
            )
            self._facts.merge_insert("fact_id").when_matched_update_all().execute(update)
        return len(fresh), len(extend)

    @staticmethod
    def _fact_row(fact_id: str, fact: Fact, first: int, ordinal: int) -> dict[str, Any]:
        return {
            "fact_id": fact_id,
            "entity": fact.entity,
            "entity_label": fact.entity_label,
            "attribute": fact.attribute,
            "qualifiers": fact.qualifiers,
            "fact_key": fact.fact_key,
            "value_text": fact.value_text,
            "value_num": fact.value_num,
            "unit": fact.unit,
            "none_value": fact.none_value,
            "quote": fact.quote,
            "chunk_id": fact.chunk_id,
            "first_seen_ord": first,
            "last_seen_ord": ordinal,
            "effective_date": _to_date(fact.effective_date),
            "expiry_date": _to_date(fact.expiry_date),
            "authority_rank": fact.authority_rank,
            "entity_inferred": fact.entity_inferred,
            "origin": fact.origin,
        }

    def _build_indices(self) -> None:
        """SPEC 5.4: BM25 on the text, scalar indices on the lookup columns."""
        if self._chunks.count_rows() > 0:
            self._chunks.create_index("text", config=FTS(), replace=True)
            self._chunks.create_index("chunk_id", config=BTree(), replace=True)
        if self._facts.count_rows() > 0:
            for column in ("fact_key", "chunk_id", "attribute"):
                self._facts.create_index(column, config=BTree(), replace=True)

    def replace_conflicts(self, records: Sequence[ConflictRecord]) -> None:
        """Rebuild the ``conflicts`` table in full (SPEC 5.3)."""
        rows = [
            {
                "conflict_id": r.conflict_id,
                "topic": r.topic,
                "fact_key": r.fact_key,
                "values": list(r.values),
                "sources": [
                    {"url": s.url, "snapshot_id": s.snapshot_id, "quote": s.quote}
                    for s in r.sources
                ],
                "resolution": r.resolution,
                "strategy": r.strategy,
                "reason": r.reason,
                "ords": list(r.ords),
            }
            for r in records
        ]
        self._conflicts = self._db.create_table(
            "conflicts", data=pa.Table.from_pylist(rows, schema=_CONFLICTS), mode="overwrite"
        )

    # --- reading -----------------------------------------------------------------------------

    def _rows(
        self, table: Any, where: str, columns: list[str] | None = None
    ) -> list[dict[str, Any]]:  # noqa: ANN401
        query = table.search().where(where)
        if columns is not None:
            query = query.select(columns)
        result: list[dict[str, Any]] = query.limit(_ALL_ROWS).to_arrow().to_pylist()
        return result

    def conflict_records(self) -> list[ConflictRecord]:
        """The stored conflicts, ordered by topic, first value and id (SPEC 10.6)."""
        rows = self._conflicts.search().limit(_ALL_ROWS).to_arrow().to_pylist()
        records = [
            ConflictRecord(
                conflict_id=r["conflict_id"],
                topic=r["topic"],
                fact_key=r["fact_key"],
                values=tuple(r["values"]),
                sources=tuple(Source(s["url"], s["snapshot_id"], s["quote"]) for s in r["sources"]),
                resolution=r["resolution"],
                strategy=r["strategy"],
                reason=r["reason"],
                ords=tuple(r["ords"]),
            )
            for r in rows
        ]
        return sorted(
            records, key=lambda r: (r.topic, r.values[0] if r.values else "", r.conflict_id)
        )

    def get_chunk(self, chunk_id: str, target_ord: int) -> Chunk | None:
        """The chunk row visible at ``target_ord``, or ``None``."""
        where = f"chunk_id = {_quote(chunk_id)} AND {_visible(target_ord)}"
        rows = self._rows(self._chunks, where, _CHUNK_COLUMNS)
        return _chunk_from_row(rows[0]) if rows else None

    def page_chunks(self, url: str, target_ord: int) -> list[Chunk]:
        """Every chunk of the page at ``url`` visible at ``target_ord``, in page order."""
        where = f"url = {_quote(url)} AND {_visible(target_ord)}"
        rows = self._rows(self._chunks, where, _CHUNK_COLUMNS)
        return sorted((_chunk_from_row(r) for r in rows), key=lambda c: (c.position, c.chunk_id))

    def page_heads(
        self, urls: Sequence[str], target_ord: int, *, limit: int
    ) -> dict[str, list[str]]:
        """The texts of the first ``limit`` chunks of each page, by url, visible at ``target_ord``.

        One query for many pages; used to read the byline date of news pages (SRET-5).
        """
        heads: dict[str, list[Chunk]] = {}
        for batch in _batches(sorted(set(urls))):
            where = (
                f"url IN ({_in_list(batch)}) AND position < {int(limit)} AND {_visible(target_ord)}"
            )
            for row in self._rows(self._chunks, where, _CHUNK_COLUMNS):
                chunk = _chunk_from_row(row)
                heads.setdefault(chunk.url, []).append(chunk)
        return {
            url: [c.text for c in sorted(found, key=lambda c: (c.position, c.chunk_id))]
            for url, found in heads.items()
        }

    def table_chunks(self, target_ord: int) -> list[Chunk]:
        """Every parent table chunk visible at ``target_ord``."""
        where = f"chunk_type = 'table' AND {_visible(target_ord)}"
        return [_chunk_from_row(r) for r in self._rows(self._chunks, where, _CHUNK_COLUMNS)]

    def intervals_of(self, chunk_id: str) -> list[StoredInterval]:
        """Every interval row of ``chunk_id``, oldest first."""
        rows = self._rows(self._chunks, f"chunk_id = {_quote(chunk_id)}", _CHUNK_COLUMNS)
        found = [
            StoredInterval(
                r["first_seen_ord"],
                r["last_seen_ord"],
                _iso(r["first_seen_date"]),
                _iso(r["last_seen_date"]),
                r["fetched_at"],
            )
            for r in rows
        ]
        return sorted(found, key=lambda i: i.first_seen_ord)

    def child_parents(self, parent_ids: Sequence[str], target_ord: int) -> dict[str, str]:
        """``child chunk_id -> parent chunk_id`` for rows visible at ``target_ord``."""
        found: dict[str, str] = {}
        for batch in _batches(list(parent_ids)):
            where = f"parent_id IN ({_in_list(batch)}) AND {_visible(target_ord)}"
            for row in self._rows(self._chunks, where, ["chunk_id", "parent_id"]):
                found[row["chunk_id"]] = row["parent_id"]
        return found

    def children_of(self, parent_ids: Sequence[str], target_ord: int) -> list[str]:
        """``chunk_id`` of the rows visible at ``target_ord`` whose parent is in ``parent_ids``."""
        return sorted(self.child_parents(parent_ids, target_ord))

    def _hits(self, rows: list[dict[str, Any]], score: str, *, invert: bool) -> list[StoredHit]:
        hits = [
            StoredHit(
                chunk=_chunk_from_row(r),
                first_seen_ord=r["first_seen_ord"],
                last_seen_ord=r["last_seen_ord"],
                score=(1.0 - float(r[score])) if invert else float(r[score]),
                vector=tuple(r["vector"]) if r.get("vector") is not None else None,
            )
            for r in rows
        ]
        return sorted(hits, key=lambda h: (-h.score, h.chunk.chunk_id, h.first_seen_ord))

    def search_vector(self, vector: np.ndarray, target_ord: int, limit: int) -> list[StoredHit]:
        """Exact cosine search among rows visible at ``target_ord``; parents are not searched."""
        if self._chunks.count_rows() == 0:
            return []
        where = f"{_visible(target_ord)} AND chunk_type <> 'table'"
        query = self._chunks.search(vector.astype(np.float32)).distance_type("cosine")
        rows = query.where(where, prefilter=True).limit(limit).to_arrow().to_pylist()
        return self._hits(rows, "_distance", invert=True)

    def search_text(self, query: str, target_ord: int, limit: int) -> list[StoredHit]:
        """BM25 search among rows visible at ``target_ord``; parents are not searched."""
        words = _TOKEN.findall(query)
        if not words or self._chunks.count_rows() == 0:
            return []
        where = f"{_visible(target_ord)} AND chunk_type <> 'table'"
        search = self._chunks.search(" ".join(words), query_type="fts")
        rows = search.where(where, prefilter=True).limit(limit).to_arrow().to_pylist()
        return self._hits(rows, "_score", invert=False)

    def fact_ids(self) -> list[str]:
        """All stored fact ids, sorted."""
        return sorted(
            r["fact_id"]
            for r in self._facts.search()
            .select(["fact_id"])
            .limit(_ALL_ROWS)
            .to_arrow()
            .to_pylist()
        )

    def _versions(self, fact_rows: list[dict[str, Any]]) -> list[FactVersion]:
        """Join fact rows to their chunk intervals (SPEC 5.1, 5.2)."""
        wanted = sorted({r["chunk_id"] for r in fact_rows})
        intervals: dict[tuple[str, int], Interval] = {}
        for batch in _batches(wanted):
            for row in self._rows(self._chunks, f"chunk_id IN ({_in_list(batch)})", _CHUNK_COLUMNS):
                key = (row["chunk_id"], row["first_seen_ord"])
                intervals[key] = Interval(
                    _chunk_from_row(row),
                    row["first_seen_ord"],
                    row["last_seen_ord"],
                    row["first_seen_date"],
                    row["last_seen_date"],
                    row["fetched_at"],
                )
        versions = []
        for row in fact_rows:
            interval = intervals.get((row["chunk_id"], row["first_seen_ord"]))
            if interval is None:
                continue
            fact = _fact_from_row(row)
            versions.append(FactVersion(fact, interval))
        return sorted(
            versions,
            key=lambda v: (v.fact.fact_key, v.first_seen_ord, v.fact.chunk_id, v.fact.value_text),
        )

    def facts_by_key(
        self, fact_keys: Sequence[str], target_ord: int | None = None
    ) -> list[FactVersion]:
        """All versions of the given fact keys (first seen up to ``target_ord`` when given)."""
        rows: list[dict[str, Any]] = []
        for batch in _batches(list(fact_keys)):
            where = f"fact_key IN ({_in_list(batch)})"
            if target_ord is not None:
                where += f" AND first_seen_ord <= {int(target_ord)}"
            rows.extend(self._rows(self._facts, where))
        return self._versions(rows)

    def facts_for_chunks(self, chunk_ids: Sequence[str], target_ord: int) -> list[FactVersion]:
        """Facts of the given chunks that are visible at ``target_ord``."""
        rows: list[dict[str, Any]] = []
        for batch in _batches(list(chunk_ids)):
            where = f"chunk_id IN ({_in_list(batch)}) AND {_visible(target_ord)}"
            rows.extend(self._rows(self._facts, where))
        return self._versions(rows)

    def visible_fact_versions(self, target_ord: int) -> list[FactVersion]:
        """Every fact visible at ``target_ord``."""
        return self._versions(self._rows(self._facts, _visible(target_ord)))

    def all_fact_versions(self) -> list[FactVersion]:
        """Every fact row, joined to its chunk interval (conflict detection input)."""
        return self._versions(self._rows(self._facts, "first_seen_ord >= 0"))
