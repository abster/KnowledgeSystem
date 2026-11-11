"""Internal dataclasses shared by the ingest and query packages (SPEC 2.1, 5.1)."""

from __future__ import annotations

from dataclasses import dataclass
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
