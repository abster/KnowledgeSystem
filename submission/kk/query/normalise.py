"""The one shared normaliser (SPEC 7).

The same functions read table cells, prose-fact values and question text. Every parser returns a
canonical value or ``None`` ("not recognised"); it never guesses. Dates are parsed here for
tables, prose and footers only. The question's target date comes from ``as_of``, never from here.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

# --- 7.6 dashes and whitespace --------------------------------------------------------------

_DASHES = "‐‑‒–—―−"
_DASH_TABLE = {ord(ch): "-" for ch in _DASHES}
_SPACE_RE = re.compile(r"\s+")


def clean_text(text: str) -> str:
    """NFKC-normalise, collapse all whitespace to single spaces and trim (case is kept)."""
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def fold_dashes(text: str) -> str:
    """Map hyphen, en dash, em dash and similar characters to ``-``."""
    return text.translate(_DASH_TABLE)


def _prepare(text: str) -> str:
    return fold_dashes(clean_text(text))


# --- 7.1 numbers, percent, money -------------------------------------------------------------

_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_PERCENT_RE = re.compile(rf"^({_NUM})\s*(?:%|percent)(\d*)$", re.IGNORECASE)
_MONEY_RE = re.compile(rf"^\$\s*({_NUM})\s*(k|m|million|thousand)?$", re.IGNORECASE)
_DOLLARS_RE = re.compile(rf"^({_NUM})\s*dollars$", re.IGNORECASE)
_BARE_K_RE = re.compile(rf"^({_NUM})\s*k$", re.IGNORECASE)
_COUNT_RE = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d+)$")
_MULTIPLIER = {
    "k": Decimal(1000),
    "thousand": Decimal(1000),
    "m": Decimal(1_000_000),
    "million": Decimal(1_000_000),
}
_DECIMAL_PLACES_MONEY = 2


@dataclass(frozen=True)
class Quantity:
    """A recognised number: a percent, an amount in US dollars, or a plain count."""

    kind: Literal["percent", "usd", "count"]
    value: Decimal


def _decimal(digits: str) -> Decimal | None:
    try:
        return Decimal(digits.replace(",", ""))
    except InvalidOperation:  # pragma: no cover - the regexes only pass valid numbers
        return None


def _parse_money(cleaned: str) -> Decimal | None:
    if (m := _MONEY_RE.match(cleaned)) is not None:
        value = _decimal(m.group(1))
        multiplier = _MULTIPLIER.get((m.group(2) or "").lower(), Decimal(1))
        return value * multiplier if value is not None else None
    if (m := _DOLLARS_RE.match(cleaned)) is not None:
        return _decimal(m.group(1))
    if (m := _BARE_K_RE.match(cleaned)) is not None:
        value = _decimal(m.group(1))
        return value * _MULTIPLIER["k"] if value is not None else None
    return None


def parse_quantity(text: str) -> Quantity | None:
    """Parse ``3.5%``, ``$1.2M``, ``100,000 dollars``, ``1,234`` and friends, else ``None``.

    A trailing footnote digit directly after ``%`` is stripped (``0.10%1`` is 0.10 percent).
    A bare decimal, or a number with letters attached (``3.5x``), is not recognised.
    """
    cleaned = _prepare(text)
    if (m := _PERCENT_RE.match(cleaned)) is not None:
        percent = _decimal(m.group(1))
        return Quantity("percent", percent) if percent is not None else None
    money = _parse_money(cleaned)
    if money is not None:
        return Quantity("usd", money)
    count = _decimal(cleaned) if _COUNT_RE.match(cleaned) is not None else None
    return Quantity("count", count) if count is not None else None


def format_quantity(quantity: Quantity) -> str:
    """Canonical text of a quantity; ``parse_quantity`` of the result gives it back."""
    value = quantity.value
    if quantity.kind == "percent":
        return f"{value:f}%"
    if quantity.kind == "count":
        return f"{int(value):,}"
    if value == value.to_integral_value():
        return f"${int(value):,}"
    whole, _, fraction = f"{value:,f}".partition(".")
    return f"${whole}.{fraction.ljust(_DECIMAL_PLACES_MONEY, '0')}"


# --- 7.2 terms -------------------------------------------------------------------------------

_NUMBER_WORDS = {
    **{
        word: n
        for n, word in enumerate(
            [
                "one",
                "two",
                "three",
                "four",
                "five",
                "six",
                "seven",
                "eight",
                "nine",
                "ten",
                "eleven",
                "twelve",
                "thirteen",
                "fourteen",
                "fifteen",
                "sixteen",
                "seventeen",
                "eighteen",
                "nineteen",
                "twenty",
            ],
            start=1,
        )
    },
    "twenty-four": 24,
    "thirty": 30,
    "thirty-six": 36,
    "forty-eight": 48,
    "sixty": 60,
}
_WORD_ALT = "|".join(sorted((re.escape(w) for w in _NUMBER_WORDS), key=len, reverse=True))
_TERM_BODY = rf"(?<![\w.])(?P<n>\d+|{_WORD_ALT})[- ]?(?P<unit>months?|mo|years?|yrs?|yr)(?![\w])"
_TERM_RE = re.compile(_TERM_BODY, re.IGNORECASE)
_MONTHS_PER_YEAR = 12


def _term_months(number: str, unit: str) -> int | None:
    n = int(number) if number.isdigit() else _NUMBER_WORDS.get(number.lower())
    if n is None or n <= 0:
        return None
    return n * (_MONTHS_PER_YEAR if unit.lower().startswith("y") else 1)


@dataclass(frozen=True)
class TermMatch:
    """A term found in text, in whole months, with its character span."""

    months: int
    start: int
    end: int


def parse_term(text: str) -> int | None:
    """Return a whole-string term in months (``30-Year`` is 360), else ``None``."""
    cleaned = _prepare(text)
    match = _TERM_RE.fullmatch(cleaned)
    if match is None:
        return None
    return _term_months(match.group("n"), match.group("unit"))


def find_terms(text: str) -> list[TermMatch]:
    """Find every recognised term in ``text``. Spans refer to the NFKC-folded text."""
    cleaned = _prepare(text)
    found: list[TermMatch] = []
    for match in _TERM_RE.finditer(cleaned):
        months = _term_months(match.group("n"), match.group("unit"))
        if months is not None:
            found.append(TermMatch(months, match.start(), match.end()))
    return found


# --- 7.3 amount constraints ------------------------------------------------------------------

_AMT = rf"(?:\$\s*(?:{_NUM})\s*(?:k|m|million|thousand)?|(?:{_NUM})\s*dollars)"
_UPPER_INCLUSIVE = r"(?:up to|at most|no more than)"
_UPPER_STRICT = r"(?:under|below|less than)"
_LOWER_INCLUSIVE = r"(?:at least)"
_LOWER_STRICT = r"(?:over|above|more than|greater than)"
_RE_FLAGS = re.IGNORECASE


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, _RE_FLAGS)


_AC_OR_LESS = _compile(rf"^(?P<a>{_AMT})\s+or less$")
_AC_OR_MORE = _compile(rf"^(?P<a>{_AMT})\s+or more$")
_AC_PLUS = _compile(rf"^(?P<a>{_AMT})\s*\+$")
_AC_UPPER_INC = _compile(rf"^{_UPPER_INCLUSIVE}\s+(?P<a>{_AMT})$")
_AC_UPPER_STRICT = _compile(rf"^{_UPPER_STRICT}\s+(?P<a>{_AMT})$")
_AC_LOWER_INC = _compile(rf"^{_LOWER_INCLUSIVE}\s+(?P<a>{_AMT})$")
_AC_LOWER_STRICT = _compile(rf"^{_LOWER_STRICT}\s+(?P<a>{_AMT})$")
_AC_BETWEEN = _compile(rf"^between\s+(?P<a>{_AMT})\s+and\s+(?P<b>{_AMT})$")
_AC_RANGE = _compile(rf"^(?P<a>{_AMT})\s*(?:-|to)\s*(?P<b>{_AMT})$")
_AC_POINT = _compile(rf"^(?P<a>{_AMT})$")


@dataclass(frozen=True)
class AmountConstraint:
    """A pair of bounds with inclusive flags. ``None`` means unbounded on that side."""

    lower: Decimal | None
    lower_inclusive: bool
    upper: Decimal | None
    upper_inclusive: bool

    @property
    def kind(self) -> Literal["point", "cap", "floor", "band"]:
        """``point`` (one value), ``cap`` (upper only), ``floor`` (lower only) or ``band``."""
        if self.lower is None:
            return "cap"
        if self.upper is None:
            return "floor"
        return "point" if self.lower == self.upper else "band"


def _amount(text: str) -> Decimal | None:
    quantity = parse_quantity(text)
    return quantity.value if quantity is not None and quantity.kind == "usd" else None


def _one_sided(cleaned: str) -> AmountConstraint | None:
    simple: tuple[tuple[re.Pattern[str], bool, bool], ...] = (
        # (pattern, is_upper, inclusive)
        (_AC_OR_LESS, True, True),
        (_AC_UPPER_INC, True, True),
        (_AC_UPPER_STRICT, True, False),
        (_AC_OR_MORE, False, True),
        (_AC_PLUS, False, True),
        (_AC_LOWER_INC, False, True),
        (_AC_LOWER_STRICT, False, False),
    )
    for pattern, is_upper, inclusive in simple:
        if (m := pattern.match(cleaned)) is not None:
            amount = _amount(m.group("a"))
            if amount is None:
                return None
            if is_upper:
                return AmountConstraint(None, False, amount, inclusive)
            return AmountConstraint(amount, inclusive, None, False)
    return None


def _two_sided(cleaned: str) -> AmountConstraint | None:
    for pattern in (_AC_BETWEEN, _AC_RANGE):
        if (m := pattern.match(cleaned)) is not None:
            low, high = _amount(m.group("a")), _amount(m.group("b"))
            if low is None or high is None or low > high:
                return None
            return AmountConstraint(low, True, high, True)
    if (m := _AC_POINT.match(cleaned)) is not None:
        amount = _amount(m.group("a"))
        return AmountConstraint(amount, True, amount, True) if amount is not None else None
    return None


def parse_amount_constraint(text: str) -> AmountConstraint | None:
    """Parse an amount phrase (question side or table side), else ``None``.

    Any other modifier word (``around``, ``about``, ``or so``) is not recognised.
    """
    cleaned = _prepare(text)
    return _one_sided(cleaned) or _two_sided(cleaned)


# --- 7.4 model years -------------------------------------------------------------------------

_YEAR_MIN, _YEAR_MAX = 1900, 2100
_CENTURY = 100
_MY_RANGE = re.compile(r"^(?P<a>\d{4})\s*(?:-|to)\s*(?P<b>\d{4}|\d{2})(?:\s+model(?:\s+year)?s?)?$")
_MY_POINT = re.compile(r"^(?:(?P<full>\d{4})|'(?P<short>\d{2}))\s+model(?:\s+year)?$")
_MY_BARE = re.compile(r"^(?P<full>\d{4})$")


@dataclass(frozen=True)
class ModelYears:
    """An inclusive range of model years (a single year has ``first == last``)."""

    first: int
    last: int


def _valid_year(year: int) -> bool:
    return _YEAR_MIN <= year <= _YEAR_MAX


def parse_model_years(text: str, *, bare_ok: bool = False) -> ModelYears | None:
    """Parse ``2024-27``, ``2025 model year`` or ``'25 model``, else ``None``.

    A bare four-digit year is accepted only when the caller knows the context is a model-year
    column (``bare_ok=True``); elsewhere a stray year must fail the coverage check.
    """
    cleaned = _prepare(text).lower()
    if (m := _MY_RANGE.match(cleaned)) is not None:
        first = int(m.group("a"))
        end = m.group("b")
        last = (
            int(end) if len(end) == len(m.group("a")) else first // _CENTURY * _CENTURY + int(end)
        )
        if _valid_year(first) and _valid_year(last) and first <= last:
            return ModelYears(first, last)
        return None
    if (m := _MY_POINT.match(cleaned)) is not None:
        year = int(m.group("full")) if m.group("full") else 2000 + int(m.group("short"))
        return ModelYears(year, year) if _valid_year(year) else None
    if bare_ok and (m := _MY_BARE.match(cleaned)) is not None:
        year = int(m.group("full"))
        return ModelYears(year, year) if _valid_year(year) else None
    return None


# --- 7.5 dates (never the question) ----------------------------------------------------------

_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_MONTH_NAME = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DATE_PATTERNS = (
    ("iso", r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})"),
    ("us", r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}|\d{2})"),
    (
        "mdy",
        rf"(?P<mon>{_MONTH_NAME})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})",
    ),
    (
        "dmy",
        rf"(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{_MONTH_NAME})\.?,?\s+(?P<y>\d{{4}})",
    ),
)
_DATE_ALTS = [(name, re.compile(body, re.IGNORECASE)) for name, body in _DATE_PATTERNS]
_TWO_DIGIT = 2


@dataclass(frozen=True)
class DateMatch:
    """A date found in text: ISO form plus its character span in the folded text."""

    iso: str
    start: int
    end: int


def _build_date(match: re.Match[str]) -> str | None:
    groups = match.groupdict()
    year_text = groups["y"]
    year = int(year_text)
    if len(year_text) == _TWO_DIGIT:
        year += 2000
    month = int(groups["m"]) if groups.get("m") else _MONTHS[groups["mon"][:3].lower()]
    try:
        return date(year, month, int(groups["d"])).isoformat()
    except ValueError:
        return None


def parse_date(text: str) -> str | None:
    """Parse a whole-string date to ISO (``2026-09-25``), else ``None``."""
    cleaned = clean_text(text)
    for _, pattern in _DATE_ALTS:
        if (m := pattern.fullmatch(cleaned)) is not None:
            return _build_date(m)
    return None


def find_dates(text: str) -> list[DateMatch]:
    """Find every recognised date in ``text`` (left to right, no overlaps).

    Spans refer to the whitespace-collapsed text, which is ``text`` itself when it contains no
    runs of whitespace.
    """
    cleaned = clean_text(text)
    found: list[DateMatch] = []
    taken_until = 0
    candidates: list[tuple[int, int, str]] = []
    for _, pattern in _DATE_ALTS:
        for m in pattern.finditer(cleaned):
            if m.start() > 0 and (
                cleaned[m.start() - 1].isalnum() or cleaned[m.start() - 1] == "/"
            ):
                continue
            if m.end() < len(cleaned) and (cleaned[m.end()].isalnum() or cleaned[m.end()] == "/"):
                continue
            iso = _build_date(m)
            if iso is not None:
                candidates.append((m.start(), -m.end(), iso))
    for start, neg_end, iso in sorted(candidates):
        if start >= taken_until:
            found.append(DateMatch(iso, start, -neg_end))
            taken_until = -neg_end
    return found
