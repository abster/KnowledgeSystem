"""Source class, authority rank, effective and expiry dates (SPEC 6.5, 6.6).

``effective_date`` and ``expiry_date`` are never defaulted to a snapshot date: if no rule
matches they stay ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse

from kk.ingest.parse import Block, Heading, Paragraph
from kk.query.normalise import DateMatch, clean_text, find_dates

# META-5: first matching rule wins. (class, rank, substrings over URL path and page title)
SOURCE_RULES: tuple[tuple[str, int, tuple[str, ...]], ...] = (
    (
        "rate_sheet",
        6,
        ("rates", "rate-sheet", "disclosure", "fee-schedule", "fees", "truth-in-savings"),
    ),
    (
        "product_page",
        5,
        ("checking", "savings", "loans", "certificates", "cards", "mortgage", "membership"),
    ),
    ("calculator", 4, ("calculator", "calc")),
    ("faq_learn", 3, ("faq", "learn", "resources", "help", "support")),
    ("blog", 2, ("blog", "article")),
    ("newsroom_promo", 1, ("news", "press", "promo", "offer", "special", "banner")),
)
OTHER_CLASS = "other"
OTHER_RANK = 3

_CUE_WINDOW_CHARS = 40  # how far after a cue word its date may start
HEADER_LINE_MAX_CHARS = 80  # a "dated header line" is short
HEADER_LINES = 3  # META-2c: within three lines under the page title

_EFFECTIVE_CUES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\beffective(?:\s+date)?",
        r"\brates?\s+(?:are\s+)?(?:effective\s+)?as\s+of",
        r"\bas\s+of",
    )
)
_EXPIRY_CUES = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bvalid\s+(?:through|until)",
        r"\boffer\s+(?:ends|expires|ended)",
        r"\bexpires?",
        r"\bthrough",
        r"\bended",
    )
)


@dataclass(frozen=True)
class SourceClass:
    """Source class name and authority rank."""

    name: str
    rank: int


@dataclass(frozen=True)
class DateInfo:
    """Effective and expiry dates as ISO strings, each ``None`` when not found."""

    effective: str | None
    expiry: str | None


def classify_source(url: str, title: str) -> SourceClass:
    """Assign source class and rank by the first matching rule over URL path and title."""
    haystack = f"{urlparse(url).path} {title}".lower()
    for name, rank, needles in SOURCE_RULES:
        if any(needle in haystack for needle in needles):
            return SourceClass(name, rank)
    return SourceClass(OTHER_CLASS, OTHER_RANK)


def _best_after_cues(
    text: str, cues: tuple[re.Pattern[str], ...], dates: list[DateMatch]
) -> str | None:
    """The date closest after any cue word; ties go to the later date (META-3)."""
    best: tuple[int, str] | None = None
    for cue in cues:
        for match in cue.finditer(text):
            for found in dates:
                gap = found.start - match.end()
                if gap < 0:
                    continue
                if gap > _CUE_WINDOW_CHARS or any(
                    ch.isdigit() for ch in text[match.end() : found.start]
                ):
                    break
                if best is None or gap < best[0] or (gap == best[0] and found.iso > best[1]):
                    best = (gap, found.iso)
                break
    return best[1] if best is not None else None


def extract_dates(text: str) -> DateInfo:
    """Apply the META-2 cue patterns to ``text``."""
    cleaned = clean_text(text)
    dates = find_dates(cleaned)
    if not dates:
        return DateInfo(None, None)
    return DateInfo(
        _best_after_cues(cleaned, _EFFECTIVE_CUES, dates),
        _best_after_cues(cleaned, _EXPIRY_CUES, dates),
    )


def page_header_dates(blocks: list[Block]) -> DateInfo:
    """META-2c: dates from short lines within three blocks under the page title.

    A line with a cue word uses it. A short line holding a date and no cue word gives that
    date as the effective date (press releases, newsroom pages).
    """
    start = next(
        (i + 1 for i, b in enumerate(blocks) if isinstance(b, Heading) and b.level == 1), 0
    )
    effective: str | None = None
    expiry: str | None = None
    for block in blocks[start : start + HEADER_LINES]:
        if not isinstance(block, Paragraph) or len(block.text) > HEADER_LINE_MAX_CHARS:
            continue
        info = extract_dates(block.text)
        found = find_dates(block.text)
        if info.effective is None and info.expiry is None and found:
            info = DateInfo(found[0].iso, None)
        effective = effective or info.effective
        expiry = expiry or info.expiry
    return DateInfo(effective, expiry)


def is_expired(expiry_date: str | None, taken_on: str) -> bool:
    """META-4: true if ``expiry_date`` is earlier than the snapshot's ``taken_on``."""
    if expiry_date is None:
        return False
    try:
        return date.fromisoformat(expiry_date) < date.fromisoformat(taken_on)
    except ValueError:
        return False
