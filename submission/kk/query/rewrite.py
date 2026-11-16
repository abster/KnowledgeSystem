"""Alternative search queries for the slow path (Step 10).

Hybrid retrieval misses a page when the member's words and the page's words differ ("mortgage"
against "home loan"). One short call to the extraction model proposes up to three differently
worded queries; they are searched next to the original and the rankings are fused. The step is
optional: no client, a timeout or any error gives no queries and the slow path goes on with the
original question alone. Rewrites are remembered for the run, so one question costs one call.
"""

from __future__ import annotations

from typing import Any

from kk.llm.client import LlmClient, LlmError
from kk.log import get_logger

REWRITE_TOOL = "record_queries"
MAX_QUERIES = 3
MAX_QUERY_CHARS = 200
DEFAULT_TIMEOUT_S = 4.0
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}
SYSTEM = (
    "You help search the website of a credit union. Given a member's question, write up to three "
    "short search queries that use different words a web page might use for the same thing "
    "(synonyms, official product names, the words of a page title). Write at least one of them "
    "as a plain statement the answering page might contain, not as a question (for example "
    "'you must live or work in our charter area' for a question about who can join). Do not "
    "answer the question, do not repeat it, and do not invent numbers."
)
_log = get_logger("rewrite")


class QueryRewriter:
    """Proposes alternative queries; never raises."""

    def __init__(
        self, client: LlmClient | None, model: str, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        """``client`` is ``None`` without an API key."""
        self._client = client
        self._model = model
        self._timeout_s = timeout_s
        self._memo: dict[str, list[str]] = {}

    def rewrite(self, question: str) -> list[str]:
        """Up to ``MAX_QUERIES`` alternative queries, without duplicates of the question."""
        if self._client is None:
            return []
        if question in self._memo:
            return list(self._memo[question])
        queries = self._ask(self._client, question)
        self._memo[question] = queries
        return list(queries)

    def _ask(self, client: LlmClient, question: str) -> list[str]:
        try:
            reply = client.call_json(
                model=self._model,
                system=SYSTEM,
                user=f"Question: {question}",
                tool_name=REWRITE_TOOL,
                schema=SCHEMA,
                max_tokens=200,
                timeout_s=self._timeout_s,
            )
        except LlmError as exc:
            _log.warning("rewrite failed", extra={"error": type(exc).__name__})
            return []
        except Exception as exc:  # noqa: BLE001 - optional step: never break the slow path
            _log.warning("rewrite failed", extra={"error": type(exc).__name__})
            return []
        raw = reply.get("queries")
        if not isinstance(raw, list):
            return []
        seen = {question.strip().casefold()}
        found: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                continue
            text = item.strip()[:MAX_QUERY_CHARS]
            if text and text.casefold() not in seen:
                seen.add(text.casefold())
                found.append(text)
            if len(found) == MAX_QUERIES:
                break
        return found
