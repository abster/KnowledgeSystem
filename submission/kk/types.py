"""Internal dataclasses shared by the ingest and query packages (SPEC 2.1, 5.1)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

ChunkType = Literal["text_paragraph", "table_row", "table", "list_item"]


@dataclass(frozen=True)
class Chunk:
    """One chunk of a page, before storage (the SPEC 5.1 row minus vector and interval)."""

    chunk_id: str
    url: str
    file_name: str
    title: str
    parent_headers: tuple[str, ...]
    chunk_type: ChunkType
    parent_id: str | None
    text: str
    effective_date: str | None
    expiry_date: str | None
    source_class: str
    authority_rank: int
    fetched_at: str
    position: int
    row_index: int | None = None
    row_count: int | None = None
    footer_count: int | None = None


@dataclass(frozen=True)
class Fact:
    """One atomic claim before storage (the SPEC 5.2 row minus the interval ordinals).

    ``fact_id``, ``first_seen_ord`` and ``last_seen_ord`` are set when facts are stored
    (Step 7). ``qualifiers`` is JSON with sorted keys.
    """

    entity: str
    entity_label: str
    attribute: str
    qualifiers: str
    fact_key: str
    value_text: str
    value_num: Decimal | None
    unit: str | None
    none_value: bool
    quote: str
    chunk_id: str
    effective_date: str | None
    expiry_date: str | None
    authority_rank: int
    entity_inferred: bool
    origin: Literal["table_rule", "prose_llm"]
