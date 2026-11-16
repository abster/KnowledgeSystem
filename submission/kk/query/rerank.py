"""Second-stage ranking for the slow path (Step 10).

Lexical and vector search rank by wording, so a page that answers a question in other words can
sit far down the list. One short call to the extraction model reads a one-line snippet of each
fused candidate and names the ones most likely to hold the answer. The step is optional: no
client, a timeout or any error picks nothing and the fused order stands. Results are remembered
for the run.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from kk.llm.client import LlmClient
from kk.log import get_logger

RERANK_TOOL = "pick_passages"
DEFAULT_TIMEOUT_S = 6.0
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["ids"],
}
SYSTEM = (
    "You help find the passages of a credit union's website that answer a member's question. "
    "You get the question and numbered passages (page, heading, start of the text). Return the "
    "numbers of the passages most likely to contain the answer or to be needed to answer it "
    "completely, best first. Include every passage that states a rule, a number, a list item or a "
    "date the answer may need, even when its wording differs from the question. Skip passages "
    "that merely repeat generic membership or marketing text. When passages state different "
    "values for the same thing and have different publication dates, include the one with the "
    "newest publication date. Do not answer the question."
)
_log = get_logger("rerank")


class Reranker:
    """Orders candidates by relevance with one cheap call; never raises."""

    def __init__(
        self, client: LlmClient | None, model: str, *, timeout_s: float = DEFAULT_TIMEOUT_S
    ) -> None:
        """``client`` is ``None`` without an API key."""
        self._client = client
        self._model = model
        self._timeout_s = timeout_s
        self._memo: dict[str, list[int]] = {}

    def rank(self, question: str, candidates: Sequence[tuple[int, str]], *, keep: int) -> list[int]:
        """The numbers of up to ``keep`` candidates, best first; ``[]`` when nothing was picked."""
        if self._client is None or not candidates:
            return []
        lines = [f"[{number}] {snippet}" for number, snippet in candidates]
        user = f"Question: {question}\n\nPassages:\n" + "\n".join(lines)
        key = hashlib.sha256(f"{keep}\n{user}".encode()).hexdigest()
        if key not in self._memo:
            self._memo[key] = self._ask(self._client, user, {n for n, _ in candidates}, keep)
        return list(self._memo[key])

    def _ask(self, client: LlmClient, user: str, valid: set[int], keep: int) -> list[int]:
        try:
            reply = client.call_json(
                model=self._model,
                system=SYSTEM,
                user=user,
                tool_name=RERANK_TOOL,
                schema=SCHEMA,
                max_tokens=300,
                timeout_s=self._timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - optional step: never break the slow path
            _log.warning("rerank failed", extra={"error": type(exc).__name__})
            return []
        raw = reply.get("ids")
        if not isinstance(raw, list):
            return []
        picked: list[int] = []
        for item in raw:
            if isinstance(item, int) and not isinstance(item, bool) and item in valid:
                if item not in picked:
                    picked.append(item)
                if len(picked) == keep:
                    break
        return picked
