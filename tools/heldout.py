"""Developer command: held-out questions from the raw pages, and the end-to-end score on them.

SPEC 20 AC-14 wants accuracy on questions that were not used to tune anything, and the
generated calibration questions only cover table facts. This tool samples passages (paragraphs
and list items) from the latest snapshot, has the extraction model write one member-style
question per passage and name verbatim key phrases that the answer must contain, and keeps only
phrases that really occur in the passage. The gold therefore comes from the page text, not
from the system's own extraction. Items are split into ``dev`` and ``held`` by a hash of the
group. Everything is written under ``results/heldout/`` (git-ignored: it contains site text).

    make heldout-generate      # needs the API key; one cheap call per passage
    make heldout PART=dev      # runs the whole system on the items, prints accuracy
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import importlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kk.llm.client import LlmClient
from kk.types import Chunk

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "results" / "heldout"
MIN_PASSAGE_CHARS = 40
NEIGHBOURS = 2
HELD_SHARE = 0.5
TOOL = "record_question"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question": {"type": "string"},
        "must_include": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["question", "must_include"],
}
SYSTEM = (
    "You write evaluation questions for a credit union website assistant. Given a passage from "
    "the website, write ONE question a member might ask whose answer is stated in the passage. "
    "Do not mention the page, the passage or its title, and use your own wording rather than "
    "copying phrases from the passage. Then give 1 to 3 short key phrases (a number, a time, a "
    "name, an address) copied EXACTLY from the passage that a correct answer must contain."
)


@dataclass(frozen=True)
class HeldoutItem:
    """One question with the phrases a correct answer must contain."""

    item_id: str
    group: str
    question: str
    must_include: tuple[str, ...]
    kind: str
    source_url: str


def part_of(group: str) -> str:
    """``dev`` or ``held``, by a stable hash of the group."""
    digest = hashlib.sha256(f"split:{group}".encode()).digest()  # not the sampling hash
    return "held" if digest[0] / 255 < HELD_SHARE else "dev"


def select_passages(chunks: list[Chunk], count: int) -> list[Chunk]:
    """A deterministic sample of paragraphs and list items, half of each when possible."""
    pools: dict[str, list[Chunk]] = defaultdict(list)
    for chunk in chunks:
        if (
            chunk.chunk_type in {"text_paragraph", "list_item"}
            and len(chunk.text) >= MIN_PASSAGE_CHARS
        ):
            pools[chunk.chunk_type].append(chunk)
    order = {
        kind: sorted(pool, key=lambda c: hashlib.sha256(c.chunk_id.encode()).hexdigest())
        for kind, pool in pools.items()
    }
    chosen: list[Chunk] = []
    kinds = sorted(order)
    index = 0
    while len(chosen) < count and any(order[k] for k in kinds):
        kind = kinds[index % len(kinds)]
        if order[kind]:
            chosen.append(order[kind].pop(0))
        index += 1
    return chosen


def passage_of(chunk: Chunk, page_chunks: list[Chunk]) -> str:
    """The chunk with up to two neighbours on each side of the same page."""
    page = sorted((c for c in page_chunks if c.url == chunk.url), key=lambda c: c.position)
    at = next((i for i, c in enumerate(page) if c.chunk_id == chunk.chunk_id), None)
    if at is None:
        return chunk.text
    window = page[max(0, at - NEIGHBOURS) : at + NEIGHBOURS + 1]
    return "\n".join(c.text for c in window)


def generate_item(chunk: Chunk, passage: str, client: LlmClient, model: str) -> HeldoutItem | None:
    """One item for ``chunk``, or ``None`` if the model gave no usable question or phrase."""
    reply = client.call_json(
        model=model,
        system=SYSTEM,
        user=f"Passage:\n{passage}",
        tool_name=TOOL,
        schema=SCHEMA,
        max_tokens=300,
        timeout_s=30.0,
    )
    question = str(reply.get("question", "")).strip()
    phrases = tuple(
        p.strip()
        for p in reply.get("must_include", [])
        if isinstance(p, str) and p.strip() in passage
    )
    if not question or not phrases:
        return None
    return HeldoutItem(
        item_id=f"ho-{chunk.chunk_id}",
        group=chunk.chunk_id,
        question=question,
        must_include=phrases,
        kind=chunk.chunk_type,
        source_url=chunk.url,
    )


def is_correct(item: HeldoutItem, text: str, *, confident: bool) -> bool:
    """All key phrases present (the scorer's value-aware match); a refusal is never correct."""
    scoring = importlib.import_module("scoring")  # harness/scoring.py, the eval's own rules
    return confident and all(bool(scoring.contains(text, p)) for p in item.must_include)


_NUMBER_WORDS = {
    w: str(n)
    for n, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
_NUMBER_WORDS.update({"thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70"})
_FILLER = frozenset(
    "a an the of to and or is are be for in on at your you it its with that this".split()
)
_CLOSE_COVERAGE = 0.8
_STEM = 5


def _close_tokens(text: str) -> list[str]:
    """Lower-case words and numbers; number words become digits; fillers are dropped."""
    out: list[str] = []
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?|[a-z]+", text.lower().replace("\u2019", "'")):
        tok = _NUMBER_WORDS.get(raw, raw.replace(",", ""))
        if tok not in _FILLER:
            out.append(tok)
    return out


def _phrase_close(phrase: str, answer: str) -> bool:
    want, have = _close_tokens(phrase), _close_tokens(answer)
    if not want:
        return False
    numbers = [t for t in want if t[0].isdigit()]
    if any(n not in have for n in numbers):
        return False  # a different figure is never "close"
    stems = {t[:_STEM] for t in have}
    words = [t for t in want if not t[0].isdigit()]
    hit = sum(t[:_STEM] in stems for t in words)
    return not words or hit / len(words) >= _CLOSE_COVERAGE


def is_close(item: HeldoutItem, text: str, *, confident: bool) -> bool:
    """Lenient reading of correctness: the strict match, or the same words and the same numbers.

    Number words equal digits, case and punctuation are ignored, and at least 80% of a phrase's
    content words must appear. Every number in the phrase must appear. A refusal is never close.
    """
    scoring = importlib.import_module("scoring")
    return confident and all(
        bool(scoring.contains(text, p)) or _phrase_close(p, text) for p in item.must_include
    )


def _refused_class(detail: str) -> str:
    for prefix in ("not answerable", "call failed", "malformed output"):
        if detail.startswith(prefix):
            return prefix
    if detail.startswith("deadline exceeded"):
        return "deadline"
    if detail.startswith("validators failed"):
        return "validators " + detail.rsplit(":", 1)[-1].strip()
    return detail[:30]


def reason_class(route: str, reason: str) -> str:
    """A short class for a route and reason, so a run reports why items failed."""
    if route in {"fast", "cache"}:
        return route
    if route == "slow":
        return "answered"
    if "slow path unavailable" in reason:
        return "unavailable"
    if "slow path refused:" in reason:
        return _refused_class(reason.rsplit("slow path refused:", 1)[-1].strip())
    return "below floor" if "floor" in reason else reason[:30]


def _load_items(path: Path) -> list[HeldoutItem]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [HeldoutItem(**{**r, "must_include": tuple(r["must_include"])}) for r in rows]


def _generate(args: argparse.Namespace) -> int:
    from kk.config import load_config  # noqa: PLC0415
    from kk.llm.client import make_client  # noqa: PLC0415
    from tools.measure_ingest import load_snapshots  # noqa: PLC0415

    client = make_client()
    if client is None:
        sys.stderr.write("no API key\n")
        return 1
    snapshots = load_snapshots(ROOT / "snapshots")
    latest = snapshots[-1]
    from kk.ingest.chunk import chunk_page  # noqa: PLC0415
    from kk.ingest.parse import ParseError  # noqa: PLC0415

    chunks: list[Chunk] = []
    for page in latest.pages:
        try:
            chunks.extend(chunk_page(page).chunks)
        except ParseError:
            continue
    picked = select_passages(chunks, args.count)
    model = load_config().llm.extract_model

    def make(chunk: Chunk) -> HeldoutItem | None:
        return generate_item(chunk, passage_of(chunk, chunks), client, model)

    with cf.ThreadPoolExecutor(4) as pool:
        items = [i for i in pool.map(make, picked) if i is not None]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "items.jsonl").write_text(
        "\n".join(json.dumps(asdict(i)) for i in items) + "\n", encoding="utf-8"
    )
    sys.stdout.write(f"wrote {len(items)} items to {args.out / 'items.jsonl'}\n")
    return 0


def _run(args: argparse.Namespace) -> int:
    from kk.system import KnowledgeSystem  # noqa: PLC0415
    from tools.measure_ingest import load_snapshots  # noqa: PLC0415

    items = [
        i for i in _load_items(args.out / "items.jsonl") if args.part in ("all", part_of(i.group))
    ]
    ks = KnowledgeSystem()
    for snapshot in load_snapshots(ROOT / "snapshots"):
        ks.ingest(snapshot)

    def one(item: HeldoutItem) -> tuple[HeldoutItem, bool, bool, str]:
        evaluation = ks.answerer.evaluate(item.question, None, use_cache=False)
        answer = evaluation.answer
        ok = is_correct(item, answer.text, confident=answer.confident)
        close = is_close(item, answer.text, confident=answer.confident)
        return item, ok, close, reason_class(evaluation.route, evaluation.reason)

    with cf.ThreadPoolExecutor(4) as pool:
        results = list(pool.map(one, items))
    by_kind: dict[str, list[bool]] = defaultdict(list)
    routes: dict[str, int] = defaultdict(int)
    for item, ok, _close, route in results:
        by_kind[item.kind].append(ok)
        routes[route] += 1
    total = sum(ok for _, ok, _, _ in results)
    near = sum(close for _, _, close, _ in results)
    n = max(1, len(results))
    sys.stdout.write(
        f"part={args.part} items={len(results)} accuracy={total / n:.3f} (strict)"
        f" close={near / n:.3f}\n"
    )
    for kind, flags in sorted(by_kind.items()):
        sys.stdout.write(f"  {kind}: {sum(flags) / len(flags):.3f} (n={len(flags)})\n")
    sys.stdout.write(f"  classes: {dict(sorted(routes.items(), key=lambda kv: -kv[1]))}\n")
    (args.out / f"run_{args.part}.json").write_text(
        json.dumps(
            [{"id": i.item_id, "ok": ok, "close": c, "route": r} for i, ok, c, r in results],
            indent=1,
        ),
        encoding="utf-8",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "run"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--part", choices=("dev", "held", "all"), default="dev")
    args = parser.parse_args(argv)
    return _generate(args) if args.command == "generate" else _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
