"""Golden tests for chunking (SPEC CH-1 to CH-6)."""

from __future__ import annotations

import hashlib

from contract import Page
from kk.ingest.chunk import ChunkedPage, chunk_page, estimate_tokens, is_layout_table, window_parent
from kk.ingest.parse import TableBlock, parse_markdown
from kk.types import Chunk
from tests.fixtures.pages import (
    DUPLICATE_PAGE,
    LAYOUT_PAGE,
    LIST_PAGE,
    PRESS_PAGE,
    PROMO_PAGE,
    RATE_PAGE,
    RATE_TITLE,
    RATE_URL,
)

MAX_TOKENS = 1200


def _page(markdown: str, *, url: str = RATE_URL, title: str = RATE_TITLE) -> Page:
    return Page(url=url, title=title, markdown=markdown, fetched_at="2026-06-02T00:00:00Z")


def _chunk(markdown: str, **kwargs: str) -> ChunkedPage:
    return chunk_page(_page(markdown, **kwargs))


def test_rate_table_page_golden() -> None:
    result = _chunk(RATE_PAGE)
    kinds = [(c.chunk_type, c.text) for c in result.chunks]
    prefix = f"{RATE_TITLE} - Premier Checking"
    header_line = "Balance | Dividend Rate | Annual Percentage Yield (APY)1 | Min. to Open"
    rows = [
        "$100,000+ | 0.10% | 0.10% | None",
        "$25,000 - $99,999.99 | 0.10% | 0.10% | None",
        "$25,000 - $99,999.99 | 2.47% | 2.50% | $5,000",
    ]
    footers = [
        "Rates are effective as of 06/02/2026 and are subject to change without notice.",
        "1APY= Annual Percentage Yield. Rates are variable and may change after opening.",
    ]
    assert kinds == [
        (
            "text_paragraph",
            "Intro text about the checking accounts that Example CU offers to businesses.",
        ),
        ("table", "\n".join([header_line, *rows, *footers])),
        (
            "table_row",
            f"{prefix} | Balance: $100,000+ | Dividend Rate: 0.10%"
            " | Annual Percentage Yield (APY)1: 0.10% | Min. to Open: None",
        ),
        (
            "table_row",
            f"{prefix} | Balance: $25,000 - $99,999.99 | Dividend Rate: 0.10%"
            " | Annual Percentage Yield (APY)1: 0.10% | Min. to Open: None",
        ),
        (
            "table_row",
            f"{prefix} | Balance: $25,000 - $99,999.99 | Dividend Rate: 2.47%"
            " | Annual Percentage Yield (APY)1: 2.50% | Min. to Open: $5,000",
        ),
        ("text_paragraph", footers[0]),
        ("text_paragraph", footers[1]),
        ("text_paragraph", "Our branches are open on weekdays for member service."),
    ]
    assert (result.tables, result.layout_tables) == (1, 0)


def test_rate_table_metadata_and_links() -> None:
    result = _chunk(RATE_PAGE)
    intro, parent, row0, row1, row2, foot1, _foot2, tail = result.chunks
    assert intro.parent_headers == (RATE_TITLE, "Business Checking")
    assert parent.parent_headers == (RATE_TITLE, "Business Checking", "Premier Checking")
    assert (parent.row_count, parent.footer_count, parent.parent_id) == (3, 2, None)
    for index, row in enumerate((row0, row1, row2)):
        assert row.parent_id == parent.chunk_id
        assert row.row_index == index
        assert row.parent_headers == parent.parent_headers
    assert {c.effective_date for c in (parent, row0, row1, row2, foot1)} == {"2026-06-02"}
    assert intro.effective_date is None and tail.effective_date is None
    assert all(c.expiry_date is None for c in result.chunks)
    assert {c.file_name for c in result.chunks} == {"checking-rates"}
    assert {c.title for c in result.chunks} == {RATE_TITLE}
    assert {(c.source_class, c.authority_rank) for c in result.chunks} == {("rate_sheet", 6)}
    assert {c.fetched_at for c in result.chunks} == {"2026-06-02T00:00:00Z"}
    assert [c.position for c in result.chunks] == list(range(len(result.chunks)))


def test_chunk_id_is_the_documented_hash() -> None:
    chunk = _chunk(RATE_PAGE).chunks[0]
    header_path = " > ".join(chunk.parent_headers)
    expected = hashlib.sha256(f"{chunk.url}\n{header_path}\n{chunk.text}".encode()).hexdigest()
    assert chunk.chunk_id == expected


def test_chunking_is_deterministic() -> None:
    assert _chunk(RATE_PAGE) == _chunk(RATE_PAGE)


def test_layout_table_is_chunked_as_prose() -> None:
    result = _chunk(LAYOUT_PAGE)
    assert [(c.chunk_type, c.text) for c in result.chunks] == [
        ("list_item", "No initial deposit requirement"),
        ("list_item", "Free debit card with chip"),
        ("list_item", "Online banking included"),
        ("list_item", "Text alerts on account activity"),
    ]
    assert (result.tables, result.layout_tables, result.dropped_short) == (1, 1, 1)


def _table(markdown: str) -> TableBlock:
    (table,) = parse_markdown(markdown)
    assert isinstance(table, TableBlock)
    return table


def test_layout_table_detection() -> None:
    assert is_layout_table(_table("|  | B |\n| --- | --- |\n| 1 | 2 |\n"))
    assert is_layout_table(_table("| A |  |\n| --- | --- |\n| 1 | 2 |\n"))
    assert is_layout_table(_table("| A |\n| --- |\n| 1 |\n"))
    assert is_layout_table(_table("| A | B |\n| --- | --- |\n| - x<br>- y | - z |\n"))
    assert is_layout_table(_table("| A | B |\n| --- | --- |\n| [a](u) | [b](u) |\n"))
    assert is_layout_table(_table("| A | B |\n| --- | --- |\n| a<br>b | c<br>d |\n"))


def test_plain_and_partly_linked_tables_are_not_layout_tables() -> None:
    plain = "| A | B |\n| --- | --- |\n| $25,000 - $99,999 | 2% |\n| $1 - $2 | 3% |\n"
    assert not is_layout_table(_table(plain))
    one_linked_column = (
        "| Program | Rate | APR | Term |\n| --- | --- | --- | --- |\n"
        "| [Auto](u) | 5% | 6% | 60 |\n| [Boat](u) | 5% | 6% | 60 |\n"
    )
    assert not is_layout_table(_table(one_linked_column))


def test_short_list_is_one_chunk_long_list_is_split() -> None:
    result = _chunk(LIST_PAGE)
    short = [c for c in result.chunks if c.parent_headers[-1] == "Short list"]
    long = [c for c in result.chunks if c.parent_headers[-1] == "Long list"]
    assert [c.text for c in short] == ["Free checking\nOnline banking\nMobile deposit"]
    assert [c.text for c in long] == [
        "Free checking for every member",
        "Online banking and bill pay",
        "Mobile deposit from your phone",
        "Shared branching nationwide",
    ]
    assert {c.chunk_type for c in short + long} == {"list_item"}


def test_three_item_list_over_200_tokens_is_split() -> None:
    items = "\n".join(f"- item {n} " + "word " * 100 for n in range(3))
    result = _chunk("# L\n\n" + items + "\n")
    assert len(result.chunks) == 3


def test_short_and_empty_chunks_dropped_and_duplicates_collapsed() -> None:
    result = _chunk(DUPLICATE_PAGE)
    assert [c.text for c in result.chunks] == [
        "This exact paragraph appears twice on the page for testing."
    ]
    assert (result.dropped_short, result.dropped_duplicate) == (1, 1)


def test_headings_are_not_chunks_and_nest_into_the_header_path() -> None:
    md = (
        "# A\n\n## B\n\nParagraph one is long enough to keep.\n\n### C\n\n"
        "Paragraph two is long enough to keep.\n\n## D\n\nParagraph three is long enough to keep.\n"
    )
    paths = [c.parent_headers for c in _chunk(md).chunks]
    assert paths == [(RATE_TITLE, "A", "B"), (RATE_TITLE, "A", "B", "C"), (RATE_TITLE, "A", "D")]


def test_empty_page_title_falls_back_to_the_first_h1() -> None:
    result = _chunk(RATE_PAGE, title="")
    intro, _parent, row0, *_ = result.chunks
    assert intro.parent_headers == ("Business Checking",)
    assert intro.title == "Business Checking"
    assert row0.text.startswith("Business Checking - Premier Checking | Balance: $100,000+")


def test_table_directly_under_the_title_heading_has_no_repeated_title() -> None:
    result = _chunk("# Rates\n\n| A | B |\n| --- | --- |\n| one | two |\n", title="")
    row = next(c for c in result.chunks if c.chunk_type == "table_row")
    assert row.text == "Rates | A: one | B: two"


def test_press_release_gets_the_header_date_and_drops_the_short_date_line() -> None:
    result = _chunk(
        PRESS_PAGE,
        url="https://www.example-cu.test/news/new-branch",
        title="Example CU Opens Branch",
    )
    assert [c.effective_date for c in result.chunks] == ["2026-09-25", "2026-09-25"]
    assert all("September 25, 2026" != c.text for c in result.chunks)
    assert result.dropped_short == 1
    assert {c.source_class for c in result.chunks} == {"newsroom_promo"}


def test_expiry_from_the_paragraph_itself() -> None:
    (chunk,) = _chunk(PROMO_PAGE, url="https://www.example-cu.test/promo/spring").chunks
    assert (chunk.effective_date, chunk.expiry_date) == (None, "2025-09-30")


def _long_table(rows: int) -> Chunk:
    lines = ["| Name | Value |", "| --- | --- |"]
    lines += [f"| row{n} label | value{n} |" for n in range(rows)]
    md = "# T\n\n" + "\n".join(lines) + "\n\nRates are effective as of 06/02/2026.\n"
    return next(c for c in _chunk(md).chunks if c.chunk_type == "table")


def test_estimate_tokens_grows_with_text() -> None:
    assert estimate_tokens("") == 0
    assert 0 < estimate_tokens("a b") < estimate_tokens("a b c d | e")


def test_parent_returned_whole_when_it_fits() -> None:
    parent = _long_table(10)
    assert window_parent(parent, 5, max_tokens=10_000) == parent.text


def test_parent_window_is_header_footer_and_matched_row_plus_two_each_side() -> None:
    parent = _long_table(10)
    lines = parent.text.split("\n")
    header, rows, footers = lines[0], lines[1:11], lines[11:]
    assert window_parent(parent, 5, max_tokens=10) == "\n".join([header, *rows[3:8], *footers])
    assert window_parent(parent, 0, max_tokens=10) == "\n".join([header, *rows[0:3], *footers])
    assert window_parent(parent, 9, max_tokens=10) == "\n".join([header, *rows[7:10], *footers])
