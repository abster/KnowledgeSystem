"""``KnowledgeSystem``: ingest wired end to end; ``answer`` is still the refuse-all stub.

Step 7 builds the real index (chunks, table facts, conflicts). The answer path arrives in
Step 9, so every question is refused until then.
"""

from __future__ import annotations

import contextlib

from contract import Answer, Conflict, Snapshot
from kk.config import Config, load_config
from kk.conflicts import to_contract
from kk.ingest.embed import EmbedderLike, load_embedder
from kk.ingest.pipeline import IngestState, IngestSummary, ingest_snapshot
from kk.log import get_logger, setup_logging, work_dir_from_env
from kk.store import Store

# SPEC 12.3 with no linked entity. Contains no digits, so it can never add a number.
REFUSAL_TEXT = "I couldn't find that information on Dupaco's website. Please contact Dupaco."


class KnowledgeSystem:
    """Ingests snapshots into LanceDB; refuses every question until Step 9."""

    def __init__(self, config: Config | None = None, embedder: EmbedderLike | None = None) -> None:
        """Load configuration, the embedding model and an empty store (IF-1, no network)."""
        self.config = config or load_config()
        self.work_dir = work_dir_from_env()
        setup_logging(self.work_dir)
        self._log = get_logger("system")
        self.embedder: EmbedderLike = embedder if embedder is not None else load_embedder()
        self.store = Store(
            self.work_dir / "lance", max_batch_chars=self.config.embed.max_batch_chars
        )
        self._state = IngestState()
        self.last_summary: IngestSummary | None = None

    @property
    def degraded(self) -> frozenset[tuple[str, str]]:
        """``(entity, attribute)`` pairs marked degraded by TBL-11 (used by the answer path)."""
        return self._state.degraded

    def ingest(self, snapshot: Snapshot) -> None:
        """Ingest one snapshot completely before returning (IF-2).

        A malformed page is skipped and counted. Only an embedding or store failure raises,
        because the index would be incomplete (SPEC 6.11).
        """
        self.last_summary = ingest_snapshot(self.store, self.embedder, snapshot, self._state)

    def answer(self, question: str, as_of: str | None = None) -> Answer:
        """Return the safe refusal (SPEC 12.3). Never raises."""
        with contextlib.suppress(Exception):  # logging must never break an answer
            self._log.info("answer", extra={"route": "refusal", "has_as_of": as_of is not None})
        return Answer(text=REFUSAL_TEXT, sources=[], confident=False)

    def conflicts(self) -> list[Conflict]:
        """The records built by the last ingest, with no new analysis (IF-4)."""
        try:
            return [to_contract(r) for r in self.store.conflict_records()]
        except Exception:  # noqa: BLE001 - the harness must always get a list
            self._log.exception("reading conflicts failed")
            return []
