"""Ingest-time verification of table row-label parses (SPEC 6.7.2 TBL-12).

Each distinct parse is sent once to the extraction model, which is asked only whether the
reading of the label is correct. A "no" rejects the parse, and the pipeline quarantines every
fact from it. A failed call, a missing key or a "yes" keeps the facts, and a failure is not
cached, so the next run asks again.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from kk.ingest.extract_cache import ExtractionCache, cache_key
from kk.ingest.prompts import extract as prompts
from kk.ingest.tables import RowParse
from kk.llm.client import LlmClient, LlmError, LlmOutputError, PauseGate, call_with_retries
from kk.log import get_logger

_log = get_logger("rowcheck")
_MAX_TOKENS = 64
_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class VerifyResult:
    """The parses the model rejected, and counts (distinct parses)."""

    rejected: frozenset[RowParse]
    checked: int = 0
    cache_hits: int = 0
    calls: int = 0
    failures: int = 0
    skipped_no_key: int = 0


def user_message(parse: RowParse) -> str:
    """What the model is shown for one parse."""
    cells = " | ".join(parse.qualifier_cells) or "(none)"
    return (
        f"Label text: {parse.label}\n"
        f"Other label cells: {cells}\n"
        f"Reading: product name = {parse.entity_label}; qualifiers = {parse.qualifiers}"
    )


def _verdict(text: str | None) -> bool | None:
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, bool) else None


class RowLabelVerifier:
    """Asks the extraction model once per distinct parse whether the reading is correct."""

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
        """Create a verifier; ``client=None`` means no API key (cache hits still work)."""
        self._client = client
        self._cache = cache
        self._model = model
        self._concurrency = max(1, concurrency)
        self._sleep = sleep
        self._gate = gate

    def _key(self, parse: RowParse) -> str:
        return cache_key(prompts.VERIFY_VERSION, self._model, user_message(parse), "")

    def _ask(self, client: LlmClient, parse: RowParse) -> bool:
        response: dict[str, Any] = call_with_retries(
            client,
            sleep=self._sleep,
            gate=self._gate,
            model=self._model,
            system=prompts.VERIFY_SYSTEM,
            user=user_message(parse),
            tool_name=prompts.VERIFY_TOOL,
            schema=prompts.VERIFY_SCHEMA,
            max_tokens=_MAX_TOKENS,
            timeout_s=_TIMEOUT_S,
        )
        correct = response.get("correct")
        if not isinstance(correct, bool):
            raise LlmOutputError("verdict is not a boolean")
        return correct

    def verify(self, parses: Iterable[RowParse]) -> VerifyResult:
        """A "no" rejects the parse; a failure, a missing key or a "yes" keeps it."""
        distinct = list(dict.fromkeys(parses))
        verdicts: dict[RowParse, bool] = {}
        misses: list[RowParse] = []
        for parse in distinct:
            cached = _verdict(self._cache.get(self._key(parse)))
            if cached is None:
                misses.append(parse)
            else:
                verdicts[parse] = cached
        hits = len(verdicts)
        client = self._client
        calls = failures = 0
        if client is not None and misses:
            calls = len(misses)
            with ThreadPoolExecutor(max_workers=min(self._concurrency, len(misses))) as pool:
                futures = {pool.submit(self._ask, client, p): p for p in misses}
                for future in as_completed(futures):
                    parse = futures[future]
                    try:
                        verdicts[parse] = future.result()
                    except LlmError as exc:
                        failures += 1
                        _log.warning("verification call failed", extra={"error": str(exc)})
                    except Exception:  # noqa: BLE001 - a bad call keeps the facts
                        failures += 1
                        _log.exception("verification call raised")
                    else:
                        self._cache.put(self._key(parse), json.dumps(verdicts[parse]))
        return VerifyResult(
            rejected=frozenset(p for p, ok in verdicts.items() if not ok),
            checked=len(distinct),
            cache_hits=hits,
            calls=calls,
            failures=failures,
            skipped_no_key=len(misses) if client is None else 0,
        )
