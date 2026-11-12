"""Blocks to chunks (SPEC 6.3, CH-1 to CH-6).

Paragraphs and list items become chunks with their header path. A table becomes one parent
``table`` chunk (header row, rows, footer notes) plus one self-contained ``table_row`` child
per data row. Layout tables are chunked as prose. Output is deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from contract import Page
from kk.ingest.dedupe import make_chunk_id
from kk.ingest.meta import DateInfo, classify_source, extract_dates, page_header_dates
from kk.ingest.parse import (
    Block,
    Heading,
    ListBlock,
    Paragraph,
    TableBlock,
    inline_markdown_text,
    parse_markdown,
)
from kk.query.normalise import clean_text, find_dates
from kk.types import Chunk, ChunkType

MIN_CHUNK_CHARS = 20  # CH-5
SINGLE_LIST_MAX_ITEMS = 3  # CH-1
SINGLE_LIST_MAX_TOKENS = 200  # CH-1
WINDOW_NEIGHBOURS = 2  # CH-3: rows kept on each side of the matched row
_LAYOUT_FLAG_SHARE = 0.5  # TBL-1: "most" data cells
_MIN_HEADER_CELLS = 2  # TBL-1

_FOOTER_WORDS = re.compile(r"\b(?:effective|rates|apy|apr)\b|\bas\s+of\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"\w+|[^\w\s]")
_LIST_MARKER = re.compile(r"(?:^|<br\s*/?>\s*)[-*•+]\s+\S", re.IGNORECASE)
_LINK = re.compile(r"\]\(")
_BREAK = re.compile(r"<br", re.IGNORECASE)
_BREAK_SPLIT = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LINE_MARKER = re.compile(r"^\s*[-*•+]\s+")


@dataclass(frozen=True)
class ChunkedPage:
    """Chunks of one page, plus what was dropped and how many tables were seen."""

    chunks: tuple[Chunk, ...]
    dropped_short: int
    dropped_duplicate: int
    tables: int
    layout_tables: int


def estimate_tokens(text: str) -> int:
    """Approximate token count: words plus punctuation marks. Used for the size caps only."""
    return len(_TOKEN_RE.findall(text))


def is_layout_table(table: TableBlock) -> bool:
    """TBL-1: blank header cell, fewer than two header cells, or mostly list-like cells."""
    non_blank = [h for h in table.header if h.strip()]
    if len(non_blank) != len(table.header) or len(non_blank) < _MIN_HEADER_CELLS:
        return True
    cells = [raw for row in table.raw_rows for raw in row if raw.strip()]
    if not cells:
        return False
    flagged = sum(
        1
        for raw in cells
        if _LIST_MARKER.search(raw.strip()) or _LINK.search(raw) or _BREAK.search(raw)
    )
    return flagged > len(cells) * _LAYOUT_FLAG_SHARE


def _file_name(url: str) -> str:
    parsed = urlparse(url)
    segment = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return segment or parsed.netloc


def _footer_notes(blocks: list[Block], table_index: int) -> list[str]:
    """CH-4: following paragraphs holding a date or a cue word, up to the next non-paragraph."""
    notes: list[str] = []
    for block in blocks[table_index + 1 :]:
        if not isinstance(block, Paragraph):
            break
        if _FOOTER_WORDS.search(block.text) or find_dates(block.text):
            notes.append(block.text)
    return notes


def _footer_dates(notes: list[str]) -> DateInfo:
    effective: str | None = None
    expiry: str | None = None
    for note in notes:
        info = extract_dates(note)
        effective = effective or info.effective
        expiry = expiry or info.expiry
    return DateInfo(effective, expiry)


def carry_down(table: TableBlock) -> list[tuple[str, ...]]:
    """TBL-7 applied to rendering: a blank first cell takes the label of the row above."""
    rows: list[tuple[str, ...]] = []
    label = ""
    for row in table.rows:
        if not any(cell.strip() for cell in row):
            continue
        if row[0].strip():
            label = row[0]
        elif label:
            row = (label, *row[1:])  # noqa: PLW2901
        rows.append(row)
    return rows


class _Builder:
    """Collects the chunks of one page."""

    def __init__(self, url: str, title: str, fetched_at: str, page_dates: DateInfo) -> None:
        source = classify_source(url, title)
        self.url = url
        self.title = title
        self.fetched_at = fetched_at
        self.page_dates = page_dates
        self.file_name = _file_name(url)
        self.source_class = source.name
        self.rank = source.rank
        self.chunks: list[Chunk] = []
        self.seen: set[str] = set()
        self.dropped_short = 0
        self.dropped_duplicate = 0

    def add(  # noqa: PLR0913 - a chunk has this many independent attributes
        self,
        chunk_type: ChunkType,
        text: str,
        headers: tuple[str, ...],
        *,
        parent_id: str | None = None,
        dates: DateInfo | None = None,
        row_index: int | None = None,
        row_count: int | None = None,
        footer_count: int | None = None,
    ) -> Chunk | None:
        """Add one chunk unless CH-5 or in-page deduplication drops it."""
        if len(text) < MIN_CHUNK_CHARS or not any(ch.isalnum() for ch in text):
            self.dropped_short += 1
            return None
        chunk_id = make_chunk_id(self.url, headers, text)
        if chunk_id in self.seen:
            self.dropped_duplicate += 1
            return None
        self.seen.add(chunk_id)
        own = extract_dates(text)
        first = dates or DateInfo(None, None)
        chunk = Chunk(
            chunk_id=chunk_id,
            url=self.url,
            file_name=self.file_name,
            title=self.title,
            parent_headers=headers,
            chunk_type=chunk_type,
            parent_id=parent_id,
            text=text,
            effective_date=first.effective or own.effective or self.page_dates.effective,
            expiry_date=first.expiry or own.expiry or self.page_dates.expiry,
            source_class=self.source_class,
            authority_rank=self.rank,
            fetched_at=self.fetched_at,
            position=len(self.chunks),
            row_index=row_index,
            row_count=row_count,
            footer_count=footer_count,
        )
        self.chunks.append(chunk)
        return chunk


def _headers(title: str, stack: list[tuple[int, str]]) -> tuple[str, ...]:
    path = [title]
    for _, text in stack:
        if text != path[-1]:
            path.append(text)
    return tuple(path)


def _add_list(builder: _Builder, block: ListBlock, headers: tuple[str, ...]) -> None:
    joined = "\n".join(block.items)
    if (
        len(block.items) <= SINGLE_LIST_MAX_ITEMS
        and estimate_tokens(joined) <= SINGLE_LIST_MAX_TOKENS
    ):
        builder.add("list_item", joined, headers)
        return
    for item in block.items:
        builder.add("list_item", item, headers)


def _add_layout_table(builder: _Builder, table: TableBlock, headers: tuple[str, ...]) -> None:
    for raw_row in (table.raw_header, *table.raw_rows):
        for raw_cell in raw_row:
            for segment in _BREAK_SPLIT.split(raw_cell):
                is_item = _LINE_MARKER.match(segment) is not None
                text = inline_markdown_text(_LINE_MARKER.sub("", segment, count=1))
                if text:
                    builder.add("list_item" if is_item else "text_paragraph", text, headers)


def table_row_text(
    title: str, nearest_heading: str, header: tuple[str, ...], row: tuple[str, ...]
) -> str:
    """CH-2: the self-contained text of one table row (page title, heading, ``header: cell``)."""
    prefix = (
        title if not nearest_heading or nearest_heading == title else f"{title} - {nearest_heading}"
    )
    parts = [
        f"{header[col]}: {cell}" if col < len(header) and header[col] else cell
        for col, cell in enumerate(row)
        if cell.strip()
    ]
    return " | ".join([prefix, *parts])


def _add_table(
    builder: _Builder,
    table: TableBlock,
    notes: list[str],
    headers: tuple[str, ...],
    nearest: str,
) -> None:
    rows = carry_down(table)
    if not rows:
        return
    footer_dates = _footer_dates(notes)
    lines = [" | ".join(table.header), *(" | ".join(row) for row in rows), *notes]
    parent_text = "\n".join(lines)
    parent = builder.add(
        "table",
        parent_text,
        headers,
        dates=footer_dates,
        row_count=len(rows),
        footer_count=len(notes),
    )
    parent_id = (
        parent.chunk_id if parent is not None else make_chunk_id(builder.url, headers, parent_text)
    )
    for row_index, row in enumerate(rows):
        builder.add(
            "table_row",
            table_row_text(builder.title, nearest, table.header, row),
            headers,
            parent_id=parent_id,
            dates=footer_dates,
            row_index=row_index,
        )


def chunk_blocks(blocks: list[Block], *, url: str, page_title: str, fetched_at: str) -> ChunkedPage:
    """Turn parsed blocks into chunks (deterministic; CH-6)."""
    first_h1 = next((b.text for b in blocks if isinstance(b, Heading) and b.level == 1), "")
    title = clean_text(page_title) or first_h1 or _file_name(url)
    builder = _Builder(url, title, fetched_at, page_header_dates(blocks))
    stack: list[tuple[int, str]] = []
    tables = layout_tables = 0
    for index, block in enumerate(blocks):
        if isinstance(block, Heading):
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text))
            continue
        headers = _headers(title, stack)
        if isinstance(block, Paragraph):
            builder.add("text_paragraph", block.text, headers)
        elif isinstance(block, ListBlock):
            _add_list(builder, block, headers)
        elif isinstance(block, TableBlock):
            tables += 1
            if is_layout_table(block):
                layout_tables += 1
                _add_layout_table(builder, block, headers)
            else:
                nearest = stack[-1][1] if stack else ""
                _add_table(builder, block, _footer_notes(blocks, index), headers, nearest)
    return ChunkedPage(
        tuple(builder.chunks),
        builder.dropped_short,
        builder.dropped_duplicate,
        tables,
        layout_tables,
    )


def chunk_page(page: Page) -> ChunkedPage:
    """Parse and chunk one page.

    Raises:
        ParseError: the page is empty, too large or cannot be parsed (ING-6).
    """
    blocks = parse_markdown(page.markdown)
    return chunk_blocks(blocks, url=page.url, page_title=page.title, fetched_at=page.fetched_at)


def window_parent(parent: Chunk, row_index: int, max_tokens: int) -> str:
    """CH-3: the parent text to return for a matched row.

    The whole table when it fits in ``max_tokens``; otherwise the header row, the footer notes
    and the matched row with two neighbours on each side.
    """
    if parent.row_count is None or estimate_tokens(parent.text) <= max_tokens:
        return parent.text
    lines = parent.text.split("\n")
    header = lines[0]
    rows = lines[1 : 1 + parent.row_count]
    footers = lines[1 + parent.row_count :]
    low = max(0, row_index - WINDOW_NEIGHBOURS)
    high = min(len(rows), row_index + WINDOW_NEIGHBOURS + 1)
    return "\n".join([header, *rows[low:high], *footers])
