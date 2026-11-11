"""Synthetic pages that copy the structure (not the text) of the real site."""

from __future__ import annotations

RATE_URL = "https://www.example-cu.test/personal/checking-rates"
RATE_TITLE = "Example CU Checking Rates"

# A rate table with a blank row label, a footnoted header, footer notes and a trailing paragraph.
RATE_PAGE = """\
# Business Checking

Intro text about the checking accounts that Example CU offers to businesses.

## Premier Checking

| Balance | Dividend Rate | Annual Percentage Yield (APY)1 | Min. to Open |
| --- | --- | --- | --- |
| $100,000+ | 0.10% | 0.10% | None |
| $25,000 - $99,999.99 | 0.10% | 0.10% | None |
|  | 2.47% | 2.50% | $5,000 |

Rates are effective as of 06/02/2026 and are subject to change without notice.

1APY= Annual Percentage Yield. Rates are variable and may change after opening.

Our branches are open on weekdays for member service.
"""

# A layout table: blank header cells, bullet lists inside cells separated by <br>.
LAYOUT_PAGE = """\
# Account Features

|     |     |
| --- | --- |
| - No initial deposit requirement<br>  - Free debit card with chip<br>  - Mobile deposit | - Online banking included<br>  - Text alerts on account activity |
"""

LIST_PAGE = """\
# Member Benefits

## Short list

- Free checking
- Online banking
- Mobile deposit

## Long list

- Free checking for every member
- Online banking and bill pay
- Mobile deposit from your phone
- Shared branching nationwide
"""

PRESS_PAGE = """\
# Example CU Opens New Branch

September 25, 2026

The credit union will open a new branch on Main Street next month, offering full service.

## Details

The branch will offer teller service and a drive-up window for all members.
"""

PROMO_PAGE = """\
# Spring Bonus

Earn a $100 bonus when you open a new account. Offer ends 2025-09-30 at close of business.
"""

DUPLICATE_PAGE = """\
# Same Twice

This exact paragraph appears twice on the page for testing.

This exact paragraph appears twice on the page for testing.

x

---

```
code block text
```
"""
