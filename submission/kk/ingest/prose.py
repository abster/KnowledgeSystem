"""Prose and layout-table facts from the extraction model (SPEC 6.7.3, PRO-1 to PRO-8).

Precision first: a fact that fails any check is dropped, never repaired. A failed call gives no
facts and is not cached, so the next run retries it (PRO-5). With no API key a cached result is
still used; otherwise the chunk yields no facts and one warning is logged per run (PRO-7).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from kk.ingest.aliases import canonical_id
from kk.ingest.extract_cache import ExtractionCache, cache_key
from kk.ingest.prompts import extract as prompts
from kk.ingest.tables import encode_qualifiers, make_fact_key, normalise_qualifier
from kk.llm.client import LlmClient, LlmError, LlmOutputError, PauseGate, call_with_retries
from kk.log import get_logger
from kk.query.normalise import parse_quantity
from kk.types import Chunk, Fact

_log = get_logger("prose")

_CANDIDATE_TYPES = frozenset({"text_paragraph", "list_item"})  # layout tables chunk as these
_SIGNS = re.compile(r"[\d$%]")
_CUE = re.compile(
    r"\b(?:hours|open|closed|closing|located|address|phone|fee|eligible|member)", re.IGNORECASE
)
_DIGITS = re.compile(r"\d+")
_MAX_TOKENS = 2048
_TIMEOUT_S = 60.0
_UNIT_KIND = {
    "rate": ("percent",),
    "dividend_rate": ("percent",),
    "apy": ("percent",),
    "apr": ("percent",),
    "min_to_open": ("usd",),
    "min_to_earn": ("usd",),
    "fee": ("usd", "percent"),
    "count": ("count",),
}


@dataclass(frozen=True)
class ProseStats:
    """Counts for the ingest summary (SPEC 6.12).

    ``candidates`` and ``skipped_no_key`` count chunks; ``cache_hits``, ``calls`` (attempted,
    including those that failed) and ``failures`` count distinct texts (one call serves every
    chunk with the same text).
    """

    candidates: int = 0
    cache_hits: int = 0
    calls: int = 0
    failures: int = 0
    skipped_no_key: int = 0
    dropped_facts: int = 0
    facts: int = 0


@dataclass(frozen=True)
class ProseResult:
    """Accepted facts and the counts."""

    facts: tuple[Fact, ...]
    stats: ProseStats


def is_candidate(chunk: Chunk) -> bool:
    """PRO-1: a prose or list chunk with a digit, currency or percent sign, or a cue word."""
    if chunk.chunk_type not in _CANDIDATE_TYPES:
        return False
    return _SIGNS.search(chunk.text) is not None or _CUE.search(chunk.text) is not None


def header_path(chunk: Chunk) -> str:
    """The chunk's heading path as one line (part of the cache key, PRO-5)."""
    return " > ".join(chunk.parent_headers)


def user_message(chunk: Chunk) -> str:
    """What the extraction model is shown for ``chunk``."""
    return f"Header path: {header_path(chunk)}\n\nPassage:\n{chunk.text}"


def _text(item: dict[str, Any], name: str) -> str | None:
    value = item.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _qualifiers(raw: object) -> str | None:
    if not isinstance(raw, dict):
        return None
    normalised: dict[str, str | int] = {}
    for key, phrase in raw.items():
        if key not in prompts.QUALIFIER_KEYS or not isinstance(phrase, str):
            return None
        value = normalise_qualifier(key, phrase)
        if value is None:
            return None
        normalised[key] = value
    return encode_qualifiers(normalised)


def _accept(item: object, chunk: Chunk) -> Fact | None:  # noqa: PLR0911
    """PRO-3 and PRO-4 for one returned fact, or ``None`` when any rule fails."""
    if not isinstance(item, dict):
        return None
    attribute = item.get("attribute")
    entity, value, quote = _text(item, "entity"), _text(item, "value"), _text(item, "quote")
    if attribute not in prompts.ATTRIBUTES or entity is None or value is None or quote is None:
        return None
    if quote not in chunk.text or any(d not in quote for d in _DIGITS.findall(value)):
        return None
    qualifiers = _qualifiers(item.get("qualifiers"))
    entity_id = canonical_id(entity)
    if qualifiers is None or not entity_id:
        return None
    number, unit = None, None
    if attribute not in prompts.TEXT_ATTRIBUTES:
        quantity = parse_quantity(value)
        if quantity is None or quantity.kind not in _UNIT_KIND[attribute]:
            return None
        number, unit = quantity.value, quantity.kind
    return Fact(
        entity=entity_id,
        entity_label=entity,
        attribute=attribute,
        qualifiers=qualifiers,
        fact_key=make_fact_key(entity_id, attribute, qualifiers),
        value_text=value,
        value_num=number,
        unit=unit,
        none_value=False,
        quote=quote,
        chunk_id=chunk.chunk_id,
        effective_date=chunk.effective_date,
        expiry_date=chunk.expiry_date,
        authority_rank=chunk.authority_rank,
        entity_inferred=False,
        origin="prose_llm",
    )


def validate_facts(raw: Any, chunk: Chunk) -> tuple[list[Fact], int]:  # noqa: ANN401
    """PRO-3 and PRO-4: the accepted facts of one model response and the number dropped.

    Raises:
        LlmOutputError: ``raw`` is not ``{"facts": [...]}`` (the response broke the schema).
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("facts"), list):
        raise LlmOutputError("response does not match the schema")
    accepted: dict[tuple[str, str], Fact] = {}
    dropped = 0
    for item in raw["facts"]:
        fact = _accept(item, chunk)
        if fact is None:
            dropped += 1
        else:
            accepted.setdefault((fact.fact_key, fact.value_text), fact)
    return list(accepted.values()), dropped


@dataclass
class _Fetched:
    responses: dict[str, dict[str, Any]]
    hits: int = 0
    calls: int = 0
    failures: int = 0


def _loads(text: str | None) -> dict[str, Any] | None:
    """A cached response, or ``None`` for a miss or an entry that does not parse."""
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class ProseExtractor:
    """Extracts prose facts, serving repeats from the cache (PRO-5, PRO-6, PRO-7)."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        client: LlmClient | None,
        cache: ExtractionCache,
        model: str,
        concurrency: int,
        sleep: Callable[[float], None] = time.sleep,
        gate: PauseGate | None = None,
    ) -> None:
        """Create an extractor; ``client=None`` means no API key (cache hits still work)."""
        self._client = client
        self._cache = cache
        self._model = model
        self._concurrency = max(1, concurrency)
        self._sleep = sleep
        self._gate = gate
        self._warned = False

    def _key(self, chunk: Chunk) -> str:
        return cache_key(prompts.EXTRACT_VERSION, self._model, chunk.text, header_path(chunk))

    def _call(self, client: LlmClient, chunk: Chunk) -> dict[str, Any]:
        response = call_with_retries(
            client,
            sleep=self._sleep,
            gate=self._gate,
            model=self._model,
            system=prompts.EXTRACT_SYSTEM,
            user=user_message(chunk),
            tool_name=prompts.EXTRACT_TOOL,
            schema=prompts.EXTRACT_SCHEMA,
            max_tokens=_MAX_TOKENS,
            timeout_s=_TIMEOUT_S,
        )
        validate_facts(response, chunk)  # a response that breaks the schema is a failure
        return response

    def _fetch(self, first: dict[str, Chunk]) -> _Fetched:
        """Responses by cache key: from the cache, else from concurrent calls (PRO-5, PRO-6)."""
        fetched = _Fetched({})
        misses: dict[str, Chunk] = {}
        for key, chunk in first.items():
            cached = _loads(self._cache.get(key))
            if cached is not None:
                fetched.responses[key] = cached
                fetched.hits += 1
            else:
                misses[key] = chunk
        client = self._client
        if client is None or not misses:
            return fetched
        fetched.calls = len(misses)
        with ThreadPoolExecutor(max_workers=min(self._concurrency, len(misses))) as pool:
            futures: dict[Future[dict[str, Any]], str] = {
                pool.submit(self._call, client, chunk): key for key, chunk in misses.items()
            }
            for future in as_completed(futures):
                try:
                    response = future.result()
                except LlmError as exc:
                    fetched.failures += 1
                    _log.warning("extraction call failed", extra={"error": str(exc)})
                except Exception:  # noqa: BLE001 - one bad chunk never stops the ingest
                    fetched.failures += 1
                    _log.exception("extraction call raised")
                else:
                    key = futures[future]
                    fetched.responses[key] = response
                    self._cache.put(key, json.dumps(response, sort_keys=True))
        return fetched

    def extract(self, chunks: Sequence[Chunk]) -> ProseResult:
        """Facts of every candidate chunk. Never raises for a model or cache problem."""
        candidates = [c for c in chunks if is_candidate(c)]
        first: dict[str, Chunk] = {}
        for chunk in candidates:
            first.setdefault(self._key(chunk), chunk)
        fetched = self._fetch(first)
        facts: list[Fact] = []
        dropped = skipped = 0
        for chunk in candidates:
            response = fetched.responses.get(self._key(chunk))
            if response is None:
                skipped += self._client is None
                continue
            try:
                accepted, lost = validate_facts(response, chunk)
            except LlmOutputError:  # a damaged cache entry: no facts for this chunk
                continue
            facts.extend(accepted)
            dropped += lost
        if skipped and not self._warned:
            self._warned = True
            _log.warning("no API key and no cached extraction: prose facts skipped for some chunks")
        stats = ProseStats(
            candidates=len(candidates),
            cache_hits=fetched.hits,
            calls=fetched.calls,
            failures=fetched.failures,
            skipped_no_key=skipped,
            dropped_facts=dropped,
            facts=len(facts),
        )
        return ProseResult(tuple(facts), stats)
