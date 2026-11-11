"""Markdown to ordered blocks (SPEC 6.2, ING-4 to ING-6).

Parsing uses ``markdown-it-py`` with its table rule (GFM tables). Raw HTML tags and image
links are removed from text but the alt text is kept. Footnote markers in table headers are
left alone: stripping them is a table-extraction rule (SPEC 6.7.2, TBL-2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.token import Token

from kk.query.normalise import clean_text

MAX_PAGE_BYTES = 5 * 1024 * 1024  # ING-6
_HEADING_TAG_LEN = 2  # "h1" .. "h6"
_TEXT_ALIGN_PREFIX = "text-align:"


class ParseError(Exception):
    """The page is empty, too large or cannot be parsed (ING-6): skip it and count it."""


@dataclass(frozen=True)
class Heading:
    """A heading of the given level (1 to 6)."""

    level: int
    text: str


@dataclass(frozen=True)
class Paragraph:
    """A paragraph of plain text."""

    text: str


@dataclass(frozen=True)
class ListBlock:
    """A list; each item is the text of one top-level item (nested items folded in)."""

    items: tuple[str, ...]
    ordered: bool


@dataclass(frozen=True)
class TableBlock:
    """A GFM table.

    ``header`` and ``rows`` hold cleaned cell text. ``raw_header`` and ``raw_rows`` hold the
    cell source before HTML removal, so layout-table detection (TBL-1) can see list markers,
    links and line breaks. Every row has the header's width: short rows are padded and long
    rows truncated by the parser.
    """

    header: tuple[str, ...]
    align: tuple[str | None, ...]
    rows: tuple[tuple[str, ...], ...]
    raw_header: tuple[str, ...]
    raw_rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class CodeBlock:
    """A fenced or indented code block."""

    text: str


@dataclass(frozen=True)
class ThematicBreak:
    """A horizontal rule."""


Block = Heading | Paragraph | ListBlock | TableBlock | CodeBlock | ThematicBreak

_MD = MarkdownIt("commonmark").enable("table")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_BLOCKED = re.compile(r"<(script|style)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]*>")
_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _inline_text(token: Token) -> str:
    """Plain text of an inline token: tags dropped, image alt and link text kept."""
    parts: list[str] = []
    for child in token.children or []:
        if child.type in {"text", "code_inline"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append(" ")
        elif child.type == "image":
            parts.append(child.content)
        elif child.type == "html_inline" and _BR_TAG.fullmatch(child.content.strip()):
            parts.append(" ")
    return clean_text("".join(parts))


def inline_markdown_text(raw: str) -> str:
    """Plain text of one inline markdown fragment, such as a raw table cell."""
    tokens = _MD.parseInline(raw)
    return _inline_text(tokens[0]) if tokens else ""


def _html_text(html: str) -> str:
    stripped = _HTML_BLOCKED.sub(" ", _HTML_COMMENT.sub(" ", html))
    return clean_text(_HTML_TAG.sub(" ", stripped))


def _matching_close(tokens: list[Token], start: int) -> int:
    """Index of the close token that matches the open token at ``start``."""
    open_token = tokens[start]
    close_type = open_token.type.replace("_open", "_close")
    for index in range(start + 1, len(tokens)):
        if tokens[index].type == close_type and tokens[index].level == open_token.level:
            return index
    return len(tokens) - 1


def _list_block(tokens: list[Token], start: int, end: int) -> ListBlock:
    ordered = tokens[start].type == "ordered_list_open"
    item_level = tokens[start].level + 1
    items: list[str] = []
    current: list[str] | None = None
    for token in tokens[start + 1 : end]:
        if token.type == "list_item_open" and token.level == item_level:
            current = []
        elif token.type == "list_item_close" and token.level == item_level:
            if current is not None:
                items.append(clean_text(" ".join(current)))
            current = None
        elif token.type == "inline" and current is not None:
            current.append(_inline_text(token))
    return ListBlock(tuple(item for item in items if item), ordered)


def _alignment(token: Token) -> str | None:
    style = str(token.attrs.get("style", ""))
    return style[len(_TEXT_ALIGN_PREFIX) :] if style.startswith(_TEXT_ALIGN_PREFIX) else None


def _table_block(tokens: list[Token], start: int, end: int) -> TableBlock:
    header: list[str] = []
    raw_header: list[str] = []
    align: list[str | None] = []
    rows: list[list[str]] = []
    raw_rows: list[list[str]] = []
    in_header = False
    for index in range(start + 1, end):
        token = tokens[index]
        if token.type == "thead_open":
            in_header = True
        elif token.type == "thead_close":
            in_header = False
        elif token.type == "tr_open" and not in_header:
            rows.append([])
            raw_rows.append([])
        elif token.type == "th_open":
            align.append(_alignment(token))
        elif token.type == "inline" and tokens[index - 1].type in {"th_open", "td_open"}:
            if in_header:
                header.append(_inline_text(token))
                raw_header.append(token.content)
            else:
                rows[-1].append(_inline_text(token))
                raw_rows[-1].append(token.content)
    return TableBlock(
        tuple(header),
        tuple(align),
        tuple(tuple(row) for row in rows),
        tuple(raw_header),
        tuple(tuple(row) for row in raw_rows),
    )


def _blocks(tokens: list[Token]) -> list[Block]:  # noqa: PLR0912 - one branch per block kind
    blocks: list[Block] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        step = 1
        if token.type == "heading_open":
            text = _inline_text(tokens[index + 1])
            if text:
                blocks.append(Heading(int(token.tag[1:_HEADING_TAG_LEN]), text))
            step = 3
        elif token.type == "paragraph_open":
            text = _inline_text(tokens[index + 1])
            if text:
                blocks.append(Paragraph(text))
            step = 3
        elif token.type in {"bullet_list_open", "ordered_list_open"}:
            end = _matching_close(tokens, index)
            block = _list_block(tokens, index, end)
            if block.items:
                blocks.append(block)
            step = end - index + 1
        elif token.type == "table_open":
            end = _matching_close(tokens, index)
            blocks.append(_table_block(tokens, index, end))
            step = end - index + 1
        elif token.type in {"fence", "code_block"}:
            blocks.append(CodeBlock(token.content.rstrip("\n")))
        elif token.type == "hr":
            blocks.append(ThematicBreak())
        elif token.type == "html_block":
            text = _html_text(token.content)
            if text:
                blocks.append(Paragraph(text))
        index += step
    return blocks


def parse_markdown(markdown: str) -> list[Block]:
    """Parse a page's markdown into ordered blocks.

    Raises:
        ParseError: the page is empty, larger than 5 MB, or the parser failed (ING-6).
    """
    if not markdown.strip():
        raise ParseError("empty page")
    if len(markdown.encode("utf-8", errors="replace")) > MAX_PAGE_BYTES:
        raise ParseError("page larger than 5 MB")
    try:
        return _blocks(_MD.parse(markdown))
    except Exception as exc:  # noqa: BLE001 - any parser failure means "skip the page"
        raise ParseError(f"markdown parse failed: {type(exc).__name__}") from exc
