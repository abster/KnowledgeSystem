"""Synthetic table fixtures that copy the header shapes of the real site (Step 4)."""

from __future__ import annotations

# Deposit rows with a balance tier, a footnoted APY, a dash and a carried-down label.
CHECKING_TABLE = """\
| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY)1 | Min. to Open | Min. to Earn |
| --- | --- | --- | --- | --- | --- |
| Business 1st Rate Checking | $25,000+ | 2.60% | 2.63%1 | — | $25,000.00 |
|  | $5,000 – $24,999.99 | 2.47% | 2.50%1 | — | $5,000.00 |
| Business 1st Rate Checking | $0.01 – $4,999.99 | 0.00% | 0.00%1 | — | $0.01 |
"""

# Loan tiers under a heading, with no entity column (the row label is qualifiers only).
MORTGAGE_TABLE = """\
| Program | Rate | Points | APR | *Payment Example |
| --- | --- | --- | --- | --- |
| Purchase: 30-Year Fixed Conventional $100,000 or less | 7.13% | 0 | 7.27% | $673.80 |
| Purchase: 30-Year Fixed Conventional $300,000 or less | 7.25% | 0 | 7.32% | $2,046.53 |
| Refinance: 15-Year Fixed Jumbo | 6.50% | 0 | 6.60% | $1,500.00 |
"""

CERTIFICATE_TABLE = """\
| Certificate | Dividend Rate | Annual Percentage Yield (APY) | Min. to Earn |
| --- | --- | --- | --- |
| 17 Month | 4.06% | 4.10%1 | $1,000.00 |
| 6 Month | 3.37% | 3.40%1 | $1,000.00 |
"""

IRA_TABLE = """\
| IRA Type | Dividend Rate | Annual Percentage Yield (APY) | Min. to Earn |
| --- | --- | --- | --- |
| Roth: 17 Month Fixed Jumbo Special | 4.15% | 4.20%1 | $100,000.00 |
| Traditional: 17 Month Fixed Jumbo Special | 4.15% | 4.20%1 | $100,000.00 |
"""

AUTO_TABLE = """\
| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2024-27 Model Year | 60 Months | 5.39%1 |
| 2024-27 Model Year | 72 Months | 5.49% |
| 2021-23 Model Year | 60 Months | 5.64%1 |
"""

SAVINGS_TABLE = """\
| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |
| --- | --- | --- | --- |
| Example Share Savings | $25,000+ | 0.20% | 0.20% |
| Example Share Savings | $10,000 – $24,999.99 | 0.15% | 0.15% |
| Example Share Savings | $0.01 – $9,999.99 | 0.10% | 0.10% |
"""

# A balance-only landing table: the entity can only come from the heading.
LANDING_TABLE = """\
| Balance | Dividend Rate | Annual Percentage Yield (APY)1 | Min. to Open | Min. to Earn |
| --- | --- | --- | --- | --- |
| $5,000 - $24,999.99 | 2.90% | 3.00% | None | $5,000 |
"""

AMORTIZATION_TABLE = """\
| Category | Interest | Principal | Remaining |
| --- | --- | --- | --- |
| Month 1 | $120.00 | $80.00 | $9,920.00 |
"""

CREDITOR_FORM_TABLE = """\
| Creditor Name/Address | Original Date | Original Amount | Present Balance | INTEREST RATE | Monthly Payment |
| --- | --- | --- | --- | --- | --- |
| Example Bank | 01/01/2020 | $10,000 | $5,000 | 5.00% | $200 |
"""

BLANK_HEADER_TABLE = """\
|  |  |
| --- | --- |
| Free checking | Online banking |
"""

AS_LOW_AS_ABBREVIATED_TABLE = """\
| Product | APR (As Low As) |
| --- | --- |
| HELOC - Standard | 6.25%1 |
"""

# A page with a footer date, so facts can be linked to real row chunks.
PAGE_WITH_FOOTER = """\
# Example Checking Rates

## Example Checking Rates

| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |
| --- | --- | --- | --- |
| Example Checking | $5,000 - $24,999.99 | 2.47% | 2.50% |
| Example Checking | $0.01 - $4,999.99 | 0.00% | 0.00% |

Rates are effective as of 06/02/2026 and are subject to change without notice.
"""
