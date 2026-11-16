"""``KnowledgeSystem``: ingest wired end to end, the fast path and the slow path.

Step 7 builds the real index (chunks, table facts, conflicts), Step 8 adds prose facts and
row-label verification from the extraction model (cached on disk), Step 9 answers from the
facts with templates and Step 10 adds the slow path (a validated model answer, or the safe
refusal when there is no key or the answer is not accepted).
"""

from __future__ import annotations

import contextlib

from contract import Answer, Conflict, Snapshot
from kk.config import Config, load_config
from kk.conflicts import to_contract
from kk.ingest.embed import EmbedderLike, load_embedder
from kk.ingest.extract_cache import ExtractionCache, default_cache_dir
from kk.ingest.pipeline import IngestState, IngestSummary, ingest_snapshot
from kk.ingest.prose import ProseExtractor
from kk.ingest.rowcheck import RowLabelVerifier
from kk.llm.client import LlmClient, PauseGate, StructuredLlmClient, make_client
from kk.log import get_logger, setup_logging, work_dir_from_env
from kk.query.answerer import Answerer
from kk.query.cache import AnswerCache
from kk.query.rerank import Reranker
from kk.query.rewrite import QueryRewriter
from kk.query.slow import SlowPath
from kk.store import Store

# SPEC 12.3 with no linked entity. Contains no digits, so it can never add a number.
REFUSAL_TEXT = "I couldn't find that information on Dupaco's website. Please contact Dupaco."


class KnowledgeSystem:
    """Ingests snapshots into LanceDB and answers from the stored facts."""

    def __init__(
        self,
        config: Config | None = None,
        embedder: EmbedderLike | None = None,
        llm_client: LlmClient | None = None,
    ) -> None:
        """Load configuration, the embedding model and an empty store (IF-1, no network).

        ``llm_client`` replaces the real client in tests; by default a client is built only if an
        API key exists. Without one, ingest still uses cached extraction results (PRO-7).
        """
        self.config = config or load_config()
        self.work_dir = work_dir_from_env()
        setup_logging(self.work_dir)
        self._log = get_logger("system")
        self.embedder: EmbedderLike = embedder if embedder is not None else load_embedder()
        self.store = Store(
            self.work_dir / "lance", max_batch_chars=self.config.embed.max_batch_chars
        )
        self._state = IngestState()
        client = llm_client if llm_client is not None else make_client()
        cache = ExtractionCache(default_cache_dir())
        llm = self.config.llm
        gate = PauseGate()  # one rate-limit pause shared by every worker (PRO-6)
        self._prose = ProseExtractor(
            client=client,
            cache=cache,
            model=llm.extract_model,
            concurrency=llm.extract_concurrency,
            gate=gate,
        )
        self._verifier = (
            RowLabelVerifier(
                client=client,
                cache=cache,
                model=llm.extract_model,
                concurrency=llm.extract_concurrency,
                gate=gate,
            )
            if self.config.flags.ingest_llm_verify
            else None
        )
        self.last_summary: IngestSummary | None = None
        self.answer_cache = AnswerCache(self.config.cache.max_entries)
        structured = client if isinstance(client, StructuredLlmClient) else None
        self.answerer = Answerer(
            self.store,
            self.embedder,
            self.config,
            self._state,
            self.answer_cache,
            SlowPath(structured, llm.slow_model, llm.slow_deadline_s, effort=llm.slow_effort),
            QueryRewriter(client, llm.extract_model) if structured is not None else None,
            Reranker(client, llm.extract_model) if structured is not None else None,
        )

    @property
    def degraded(self) -> frozenset[tuple[str, str]]:
        """``(entity, attribute)`` pairs marked degraded by TBL-11 (used by the answer path)."""
        return self._state.degraded

    @property
    def prose_labels(self) -> frozenset[str]:
        """Entity ids named by prose facts (for entity linking in Step 9)."""
        return frozenset(self._state.prose_labels)

    def ingest(self, snapshot: Snapshot) -> None:
        """Ingest one snapshot completely before returning (IF-2).

        A malformed page is skipped and counted. Only an embedding or store failure raises,
        because the index would be incomplete (SPEC 6.11).
        """
        try:
            self.last_summary = ingest_snapshot(
                self.store,
                self.embedder,
                snapshot,
                self._state,
                prose=self._prose,
                verifier=self._verifier,
            )
        finally:
            self.answerer.refresh()  # new linker and an empty cache (CACHE-2), even after a failure

    def answer(self, question: str, as_of: str | None = None) -> Answer:
        """The fast, cached or slow answer, or the safe refusal (SPEC 12.3). Never raises (IF-3)."""
        try:
            return self.answerer.answer(question, as_of)
        except Exception:  # noqa: BLE001 - the harness must always get an Answer
            with contextlib.suppress(Exception):
                self._log.exception("answer failed")
            return Answer(text=REFUSAL_TEXT, sources=[], confident=False)

    def conflicts(self) -> list[Conflict]:
        """The records built by the last ingest, with no new analysis (IF-4)."""
        try:
            return [to_contract(r) for r in self.store.conflict_records()]
        except Exception:  # noqa: BLE001 - the harness must always get a list
            self._log.exception("reading conflicts failed")
            return []
