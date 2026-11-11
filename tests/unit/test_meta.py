"""Tests for source class, authority and effective or expiry dates (META-1 to META-5)."""

from __future__ import annotations

import pytest

from kk.ingest.meta import classify_source, extract_dates, is_expired, page_header_dates
from kk.ingest.parse import parse_markdown

BASE = "https://www.example-cu.test"


@pytest.mark.parametrize(
    ("path", "title", "name", "rank"),
    [
        ("/personal/rates/share-savings", "", "rate_sheet", 6),
        ("/x", "Current Rates", "rate_sheet", 6),
        ("/disclosures/truth-in-savings", "", "rate_sheet", 6),
        ("/personal/checking-accounts", "", "product_page", 5),
        ("/membership/join", "", "product_page", 5),
        ("/tools/payment-calculator", "", "calculator", 4),
        ("/faq/how-to-join", "", "faq_learn", 3),
        ("/support/contact", "", "faq_learn", 3),
        ("/blog/saving-tips", "", "blog", 2),
        ("/news/new-branch", "", "newsroom_promo", 1),
        ("/promo/spring", "", "newsroom_promo", 1),
        ("/about-us", "About Us", "other", 3),
        ("/PERSONAL/CHECKING", "", "product_page", 5),
        ("/blog/rates-update", "", "rate_sheet", 6),
    ],
)
def test_source_class(path: str, title: str, name: str, rank: int) -> None:
    result = classify_source(BASE + path, title)
    assert (result.name, result.rank) == (name, rank)


def test_source_class_ignores_the_host() -> None:
    assert classify_source("https://rates.example-cu.test/about-us", "").name == "other"


@pytest.mark.parametrize(
    ("text", "effective", "expiry"),
    [
        ("Rates are effective as of 06/02/2026 and are subject to change.", "2026-06-02", None),
        ("Effective date: 09/25/2026", "2026-09-25", None),
        ("Effective 2026-09-25", "2026-09-25", None),
        ("Effective on September 25, 2026.", "2026-09-25", None),
        ("Rates as of 9/25/26", "2026-09-25", None),
        ("as of Sept. 25, 2026", "2026-09-25", None),
        ("Offer ends 2025-09-30 at close.", None, "2025-09-30"),
        ("Offer expires 12/31/2026", None, "2026-12-31"),
        ("Offer ended September 30, 2025", None, "2025-09-30"),
        ("Valid through 12/31/2026", None, "2026-12-31"),
        ("valid until Dec 31, 2026", None, "2026-12-31"),
        ("This promotion expires Dec. 31, 2026.", None, "2026-12-31"),
        ("Good through 12/31/2026", None, "2026-12-31"),
        ("Effective 06/01/2026, offer valid until 09/30/2026", "2026-06-01", "2026-09-30"),
        ("seven effective strategies to save", None, None),
        ("as of today", None, None),
        ("no dates here at all", None, None),
        ("The date 06/02/2026 has no cue word", None, None),
    ],
)
def test_cue_patterns(text: str, effective: str | None, expiry: str | None) -> None:
    info = extract_dates(text)
    assert (info.effective, info.expiry) == (effective, expiry)


def test_equal_distance_ties_go_to_the_later_date() -> None:
    info = extract_dates("as of 01/01/2026 and as of 02/01/2026")
    assert info.effective == "2026-02-01"


def test_closest_date_after_the_cue_wins() -> None:
    info = extract_dates("effective as of the end of day on 01/01/2026; as of 02/01/2026")
    assert info.effective == "2026-02-01"


def _header(markdown: str):  # type: ignore[no-untyped-def]
    return page_header_dates(parse_markdown(markdown))


def test_dated_line_under_the_title_is_the_effective_date() -> None:
    info = _header("# Title\n\nSeptember 25, 2026\n\nBody paragraph of the release.\n")
    assert (info.effective, info.expiry) == ("2026-09-25", None)


def test_dated_line_with_a_cue_uses_the_cue() -> None:
    info = _header("# Title\n\nOffer ends Dec 31, 2026\n\nBody paragraph of the release.\n")
    assert (info.effective, info.expiry) == (None, "2026-12-31")


def test_dated_line_beyond_three_blocks_or_too_long_is_ignored() -> None:
    far = "# T\n\nOne paragraph.\n\nTwo paragraph.\n\nThree paragraph.\n\nSeptember 25, 2026\n"
    assert _header(far).effective is None
    long_line = "# T\n\n" + "Word " * 30 + "September 25, 2026\n"
    assert _header(long_line).effective is None


def test_no_title_heading_looks_at_the_first_blocks() -> None:
    assert (
        _header("September 25, 2026\n\nBody paragraph of the release.\n").effective == "2026-09-25"
    )


@pytest.mark.parametrize(
    ("expiry", "taken_on", "expected"),
    [
        ("2026-01-01", "2026-02-01", True),
        ("2026-02-01", "2026-02-01", False),
        ("2026-03-01", "2026-02-01", False),
        (None, "2026-02-01", False),
        ("2026-01-01", "not-a-date", False),
        ("garbage", "2026-02-01", False),
    ],
)
def test_expired_at_snapshot(expiry: str | None, taken_on: str, expected: bool) -> None:
    assert is_expired(expiry, taken_on) is expected
