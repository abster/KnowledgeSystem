"""A synthetic alias index for question tests (Step 6). Invented labels, no real site text."""

from __future__ import annotations

from kk.ingest.aliases import build_aliases
from kk.query.question import EntityLinker

LABELS = (
    "Auto Loan",
    "Mortgage",
    "Savings Account",
    "Share Certificate",
    "Certificate of Deposit",  # "cd" is then an ambiguous alias (AL-2)
    "Location",
    "Hy-Vee Location",
    "Business 1st Rate Checking",
)

LINKER = EntityLinker(build_aliases(LABELS))
