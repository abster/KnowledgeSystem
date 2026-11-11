"""Hand-in shim: puts this folder on ``sys.path`` and exposes ``build()``."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from kk.system import KnowledgeSystem  # noqa: E402


def build() -> KnowledgeSystem:
    """Return a fresh, empty system. Does not touch the network."""
    return KnowledgeSystem()
