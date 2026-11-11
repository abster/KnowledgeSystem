"""``KnowledgeSystem``: the Step 2 stub that records snapshots and refuses every question."""

from __future__ import annotations

import contextlib

from contract import Answer, Conflict, Snapshot
from kk.config import Config, load_config
from kk.log import get_logger, setup_logging, work_dir_from_env

# SPEC 12.3 with no linked entity. Contains no digits, so it can never add a number.
REFUSAL_TEXT = "I couldn't find that information on Dupaco's website. Please contact Dupaco."


class KnowledgeSystem:
    """Refuse-all stub (real logic arrives in later steps)."""

    def __init__(self, config: Config | None = None) -> None:
        """Load configuration and start logging under the work directory."""
        self.config = config or load_config()
        self.work_dir = work_dir_from_env()
        setup_logging(self.work_dir)
        self._log = get_logger("system")
        self.snapshot_ids: list[str] = []

    def ingest(self, snapshot: Snapshot) -> None:
        """Record the snapshot id and page count (never page text)."""
        try:
            self.snapshot_ids.append(snapshot.snapshot_id)
            self._log.info(
                "ingest", extra={"ord": len(self.snapshot_ids), "pages": len(snapshot.pages)}
            )
        except Exception:  # noqa: BLE001 - IF-2: ingest never raises
            self._log.exception("ingest failed")

    def answer(self, question: str, as_of: str | None = None) -> Answer:
        """Return the safe refusal (SPEC 12.3). Never raises."""
        with contextlib.suppress(Exception):  # logging must never break an answer
            self._log.info("answer", extra={"route": "refusal", "has_as_of": as_of is not None})
        return Answer(text=REFUSAL_TEXT, sources=[], confident=False)

    def conflicts(self) -> list[Conflict]:
        """Return no conflicts (detection arrives in Step 5)."""
        return []
