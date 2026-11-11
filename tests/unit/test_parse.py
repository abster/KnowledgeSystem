"""Tests for markdown parsing (SPEC ING-4 to ING-6)."""

from __future__ import annotations

import pytest

from kk.ingest.parse import (
    CodeBlock,
    Heading,
    ListBlock,
    Paragraph,
    ParseError,
    TableBlock,
    ThematicBreak,
    parse_markdown,
)
from tests.fixtures.pages import RATE_PAGE


def test_all_block_kinds_in_order() -> None:
    md = (
        "# Title\n\nA paragraph.\n\n- one\n- two\n\n1. first\n2. second\n\n"
        "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n---\n\n```\ncode\n```\n"
    )
    blocks = parse_markdown(md)
    assert blocks[0] == Heading(1, "Title")
    assert blocks[1] == Paragraph("A paragraph.")
    assert blocks[2] == ListBlock(("one", "two"), ordered=False)
    assert blocks[3] == ListBlock(("first", "second"), ordered=True)
    assert isinstance(blocks[4], TableBlock)
    assert blocks[5] == ThematicBreak()
    assert blocks[6] == CodeBlock("code")
    assert len(blocks) == 7


def test_table_header_alignment_rows_and_raw_cells() -> None:
    md = (
        "| A | B | C |\n| :--- | ---: | :---: |\n| 1 | x\\|y |  |\n"
        "| [l](http://u.test)<br>- q | 4 | 5 |\n"
    )
    (table,) = parse_markdown(md)
    assert isinstance(table, TableBlock)
    assert table.header == ("A", "B", "C")
    assert table.align == ("left", "right", "center")
    assert table.rows[0] == ("1", "x|y", "")
    assert table.rows[1] == ("l - q", "4", "5")
    assert table.raw_rows[1][0] == "[l](http://u.test)<br>- q"


def test_short_rows_are_padded_and_long_rows_truncated_to_header_width() -> None:
    md = "| A | B |\n| --- | --- |\n| 1 |\n| 1 | 2 | 3 |\n"
    (table,) = parse_markdown(md)
    assert isinstance(table, TableBlock)
    assert table.rows == (("1", ""), ("1", "2"))


def test_footnote_digits_in_headers_are_kept_for_step_four() -> None:
    blocks = parse_markdown(RATE_PAGE)
    table = next(b for b in blocks if isinstance(b, TableBlock))
    assert table.header[2] == "Annual Percentage Yield (APY)1"


def test_html_removed_and_image_alt_and_link_text_kept() -> None:
    md = "Hello <b>bold</b> world<br>next ![Alt text](http://x.test/y.png) [link](http://u.test)\n"
    (para,) = parse_markdown(md)
    assert para == Paragraph("Hello bold world next Alt text link")


def test_html_block_text_is_kept_without_tags() -> None:
    blocks = parse_markdown("<div><p>Inside text</p></div>\n\nAfter\n")
    assert blocks == [Paragraph("Inside text"), Paragraph("After")]


def test_only_top_level_list_items_with_nested_text_folded_in() -> None:
    (lst,) = parse_markdown("- one\n  - nested\n- two\n")
    assert isinstance(lst, ListBlock)
    assert lst.items == ("one nested", "two")


def test_escaped_characters_are_unescaped() -> None:
    (para,) = parse_markdown("\\*Payment Example\n")
    assert para == Paragraph("*Payment Example")


@pytest.mark.parametrize("markdown", ["", "   \n\n  "])
def test_empty_page_is_skipped(markdown: str) -> None:
    with pytest.raises(ParseError):
        parse_markdown(markdown)


def test_oversized_page_is_skipped() -> None:
    with pytest.raises(ParseError):
        parse_markdown("a" * (5 * 1024 * 1024 + 1))
