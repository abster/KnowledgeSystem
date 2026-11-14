"""Calibration of ``tau_floor`` and ``tau_accept`` (SPEC 12.4). Run with ``make calibrate``.

Pure functions (split, threshold choice, report, question generation, config writing) are
unit-tested on synthetic items. ``main`` ingests the real snapshots, scores a labelled set and
writes ``submission/calibration_report.json`` (numbers only) and the thresholds in
``submission/config.json``. The questions themselves quote site content, so they are written
only under ``results/`` (git-ignored), never to a committed file.

The thresholds are chosen on the training part only; the held-out part is reported, and the
guard of SPEC 12.4 step 6 compares the two. Nothing here is tuned against the public questions:
they are 40 of the items, split by their ``group`` like every other item.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from kk.config import Config, ConfigError, load_config
from kk.query.confidence import Signals, confidence, similarity_signal
from kk.query.render import RATE_ATTRIBUTES, attribute_label, qualifier_phrase
from kk.types import Fact

ROOT = Path(__file__).resolve().parents[1]
HELDOUT_SHARE = 0.30  # SPEC 12.4 step 2
TARGET_PRECISION = 0.97  # SPEC 12.4 step 4
MAX_FLOOR_MISS = 0.02  # SPEC 12.4 step 4
MAX_GAP = 0.03  # SPEC 12.4 step 6
FLOOR_MARGIN = 0.01  # the floor stays below the acceptance threshold
CURVE_STEP = 0.05
_ROUND = 4
_FRACTION_BITS = 64

Label = Literal["correct", "wrong", "should_refuse"]
Part = Literal["train", "heldout"]


@dataclass(frozen=True)
class Scored:
    """One calibration item after the fast path ran on it.

    ``confidence`` is the score before the hard caps; ``capped`` says a cap applied (so the
    item never takes the fast path); ``label`` is the verdict on the answer the fast path
    would give; ``answerable`` is false for an item whose right answer is a refusal.
    """

    item_id: str
    group: str
    confidence: float
    label: Label
    capped: bool
    answerable: bool


@dataclass(frozen=True)
class Question:
    """A calibration question. ``gold`` is the known value (``None`` for a negative)."""

    item_id: str
    group: str
    kind: str
    text: str
    gold: str | None
    should_refuse: bool
    as_of: str | None = None
    must_include: tuple[str, ...] = ()
    must_not_include: tuple[str, ...] = ()


# --- split -----------------------------------------------------------------------------------


def split_of(group: str) -> Part:
    """The part a group belongs to: a stable hash, so no run and no process can differ (DET-3)."""
    digest = hashlib.sha256(group.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**_FRACTION_BITS
    return "heldout" if fraction < HELDOUT_SHARE else "train"


def split_items(items: Sequence[Scored]) -> tuple[list[Scored], list[Scored]]:
    """``(train, heldout)``; every item of a group lands in the same part."""
    train = [i for i in items if split_of(i.group) == "train"]
    held = [i for i in items if split_of(i.group) == "heldout"]
    return train, held


# --- thresholds ------------------------------------------------------------------------------


def precision_at(items: Sequence[Scored], tau: float) -> float | None:
    """Share of the uncapped items at or above ``tau`` whose fast answer is correct.

    A ``should_refuse`` item that the fast path would answer counts as wrong. ``None`` when the
    fast path would answer nothing at ``tau``.
    """
    answered = [i for i in items if not i.capped and i.confidence >= tau]
    if not answered:
        return None
    return sum(i.label == "correct" for i in answered) / len(answered)


def choose_tau_accept(items: Sequence[Scored], target: float) -> float | None:
    """SPEC 12.4 step 4: the lowest value whose fast-path precision reaches ``target``."""
    for tau in sorted({i.confidence for i in items if not i.capped}):
        precision = precision_at(items, tau)
        if precision is not None and precision >= target:
            return tau
    return None


def choose_tau_floor(items: Sequence[Scored], tau_accept: float, max_miss: float) -> float:
    """The highest floor below which at most ``max_miss`` of the answerable items fall."""
    scores = sorted(i.confidence for i in items if i.answerable)
    if not scores:
        return 0.0
    allowed = int(max_miss * len(scores) + 1e-9)
    floor = scores[min(allowed, len(scores) - 1)]
    return round(min(floor, tau_accept - FLOOR_MARGIN), _ROUND)


# --- report ----------------------------------------------------------------------------------


def _part_figures(items: Sequence[Scored], tau_accept: float) -> dict[str, Any]:
    accepted = [i for i in items if not i.capped and i.confidence >= tau_accept]
    correct = sum(i.label == "correct" for i in accepted)
    answerable = sum(i.answerable for i in items)
    refused_right = sum(not i.answerable and i not in accepted for i in items)
    return {
        "n": len(items),
        "answered": len(accepted),
        "precision": round(correct / len(accepted), _ROUND) if accepted else None,
        "recall": round(correct / answerable, _ROUND) if answerable else None,
        "accuracy": round((correct + refused_right) / len(items), _ROUND) if items else None,
    }


def build_report(
    items: Sequence[Scored], tau_floor: float, tau_accept: float, target: float
) -> dict[str, Any]:
    """Train and held-out figures, the calibration curve and the guard. No question text."""
    train, held = split_items(items)
    steps = int(round(1 / CURVE_STEP))
    curve = []
    for k in range(steps + 1):
        tau = round(k * CURVE_STEP, _ROUND)
        row: dict[str, Any] = {"tau": tau}
        for name, part in (("train", train), ("heldout", held)):
            precision = precision_at(part, tau)
            row[f"{name}_precision"] = None if precision is None else round(precision, _ROUND)
            row[f"{name}_answered"] = sum(1 for i in part if not i.capped and i.confidence >= tau)
        curve.append(row)
    train_figures = _part_figures(train, tau_accept)
    held_figures = _part_figures(held, tau_accept)
    gap = None
    if train_figures["precision"] is not None and held_figures["precision"] is not None:
        gap = round(train_figures["precision"] - held_figures["precision"], _ROUND)
    return {
        "target_precision": target,
        "tau_floor": tau_floor,
        "tau_accept": tau_accept,
        "train": train_figures,
        "heldout": held_figures,
        "curve": curve,
        "guard": {"ok": gap is None or gap <= MAX_GAP, "gap": gap, "max_gap": MAX_GAP},
    }


# --- generated questions (SPEC 12.4 step 1) --------------------------------------------------

_VALUE_ATTRIBUTES = (*RATE_ATTRIBUTES, "fee", "count", "address", "hours")
_NEGATIVE_TERM = "a 999-month term"
_NEGATIVE_YEAR = "1999 model year"
_HEDGES = (
    "What is the {a} for {s}, roughly?",
    "About what is the {a} for {s}?",
    "What is the {a} around {s}?",
)


def _id(fact_key: str, kind: str, n: int) -> str:
    return f"gen-{hashlib.sha256(fact_key.encode()).hexdigest()[:10]}-{kind}{n}"


def _subject(entity: str, phrase: str) -> str:
    return f"{phrase} on the {entity}" if phrase else f"the {entity}"


def _paraphrases(fact: Fact, phrase: str) -> list[str]:
    entity = fact.entity_label.strip()
    label = attribute_label(fact.attribute) or fact.attribute
    subject = _subject(entity, phrase)
    attribute = fact.attribute
    if attribute in RATE_ATTRIBUTES:
        return [
            f"What is the {label} for {subject}?",
            f"What's the {label} for {subject}?",
            f"Tell me the {label} for {subject}.",
        ]
    if attribute == "fee":
        return [
            f"What is the {label} for {subject}?",
            f"How much is the {entity} {label}?",
            f"Tell me the {label} for {subject}.",
        ]
    if attribute == "count":
        return [f"How many {entity} are there?", f"What is the number of {entity}?"]
    if attribute == "address":
        return [f"Where is the {entity} located?", f"What is the address for the {entity}?"]
    return [f"What are the hours for the {entity}?", f"Tell me the hours for the {entity}."]


def generate_questions(facts: Sequence[Fact]) -> list[Question]:
    """Questions generated from facts: paraphrases, negatives and adversarial forms.

    One group per fact key, so every question about one fact lands in the same part of the
    split. The gold of a question is the fact's value text. Deterministic.
    """
    questions: list[Question] = []
    seen: set[str] = set()
    for fact in sorted(facts, key=lambda f: f.fact_key):
        if fact.fact_key in seen or fact.attribute not in _VALUE_ATTRIBUTES or fact.none_value:
            continue
        phrase = qualifier_phrase(fact.qualifiers)
        label = attribute_label(fact.attribute)
        entity = fact.entity_label.strip()
        if phrase is None or label is None or not entity or not fact.value_text.strip():
            continue
        seen.add(fact.fact_key)
        group, gold = fact.fact_key, fact.value_text
        for n, text in enumerate(_paraphrases(fact, phrase)):
            questions.append(
                Question(_id(group, "para", n), group, "paraphrase", text, gold, False,
                         must_include=(gold,))
            )  # fmt: skip
        if fact.attribute in (*RATE_ATTRIBUTES, "fee"):
            subject = _subject(entity, phrase)
            for n, template in enumerate(_HEDGES):
                text = template.format(a=label, s=subject)
                questions.append(
                    Question(_id(group, "adv", n), group, "adversarial", text, gold, False,
                             must_include=(gold,))
                )  # fmt: skip
            for n, qualifier in enumerate((_NEGATIVE_TERM, _NEGATIVE_YEAR)):
                text = f"What is the {label} for {qualifier} on the {entity}?"
                questions.append(
                    Question(_id(group, "neg", n), group, "negative", text, None, True)
                )  # fmt: skip
    return sorted(questions, key=lambda q: (q.group, q.item_id))


# --- writing the thresholds ------------------------------------------------------------------


def _replace_number(text: str, key: str, value: float) -> str:
    updated, count = re.subn(rf'("{key}":\s*)[-0-9.eE]+', rf"\g<1>{value!r}", text, count=1)
    if count != 1:
        msg = f"{key} not found in the configuration file"
        raise ValueError(msg)
    return updated


def write_thresholds(  # noqa: PLR0913
    path: Path,
    *,
    tau_floor: float,
    tau_accept: float,
    bm25_bound: float,
    dataset: str,
    report_name: str,
) -> None:
    """Write the calibrated values and their provenance into ``config.json``.

    Only these three numbers and the ``confidence`` provenance change; the rest of the file
    (layout included) is kept. The result must load, or nothing is written.
    """
    if not tau_floor < tau_accept:
        msg = "tau_floor must be below tau_accept"
        raise ValueError(msg)
    text = path.read_text(encoding="utf-8")
    text = _replace_number(text, "tau_floor", tau_floor)
    text = _replace_number(text, "tau_accept", tau_accept)
    text = _replace_number(text, "bm25_bound", bm25_bound)
    set_by = f"calibration (make calibrate, SPEC 12.4); report: {report_name}"
    text, count = re.subn(
        r'("confidence": \{\s*"set_by": )"[^"]*"(,\s*"dataset": )"[^"]*"',
        lambda m: f"{m.group(1)}{json.dumps(set_by)}{m.group(2)}{json.dumps(dataset)}",
        text,
        count=1,
    )
    if count != 1:
        msg = "confidence provenance not found in the configuration file"
        raise ValueError(msg)
    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / "config.json"
        probe.write_text(text, encoding="utf-8")
        try:
            load_config(probe)
        except ConfigError as exc:
            msg = f"the calibrated configuration is invalid: {exc}"
            raise ValueError(msg) from exc
    path.write_text(text, encoding="utf-8")


# --- the real run ----------------------------------------------------------------------------


def _scorer() -> Callable[..., dict[str, Any]]:
    module = importlib.import_module("scoring")  # harness/scoring.py, the eval's own rules
    score: Callable[..., dict[str, Any]] = module.score_item
    return score


def label_item(question: Question, answer: Any, score_item: Callable[..., dict[str, Any]]) -> Label:  # noqa: ANN401
    """Verdict on the answer the fast path would give, by the eval's own scoring rule."""
    if question.should_refuse:
        return "should_refuse"
    item = {
        "id": question.item_id,
        "question": question.text,
        "must_include": list(question.must_include),
        "must_not_include": list(question.must_not_include),
        "category": "current_value",
        "expected_sources": [],
    }
    return "correct" if score_item(item, answer)["correctness"] >= 1.0 else "wrong"


def public_questions(path: Path) -> list[Question]:
    """The public eval items as calibration questions (read only, never edited)."""
    questions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        questions.append(
            Question(
                item_id=row["id"],
                group=row.get("group") or row["id"],
                kind="public",
                text=row["question"],
                gold=None,
                should_refuse=row.get("category") == "should_refuse",
                as_of=row.get("as_of"),
                must_include=tuple(row.get("must_include", [])),
                must_not_include=tuple(row.get("must_not_include", [])),
            )
        )
    return questions


def _rescore(
    signals: Signals | None, cosine: float, bm25: float, bound: float, cfg: Config
) -> float:
    if signals is None:
        return 0.0
    moved = replace(signals, similarity=similarity_signal(cosine, bm25, bound))
    return confidence(moved, (), cfg.confidence)


def _bound_from(bm25_scores: Sequence[float]) -> float:
    """The largest top-hit BM25 on the training part, rounded up: it maps them all into [0, 1]."""
    top = max(bm25_scores, default=1.0)
    return float(max(1.0, math.ceil(top * 2) / 2))


def _resolved_table_facts(ks: Any, max_facts: int) -> list[Fact]:  # noqa: ANN401
    """The winning table fact of every fact key at the latest snapshot (a stable subset)."""
    from kk.conflicts import resolve_key_at  # noqa: PLC0415 - heavy imports only for the run

    latest = max(s.ordinal for s in ks.store.snapshots)
    by_key: dict[str, list[Any]] = {}
    for version in ks.store.visible_fact_versions(latest):
        by_key.setdefault(version.fact.fact_key, []).append(version)
    facts = []
    for key in sorted(by_key):
        res = resolve_key_at(by_key[key], ordinal=latest, snapshots=ks.store.snapshots)
        if res.winner is not None and res.winner.fact.origin == "table_rule":
            facts.append(res.winner.fact)
    if max_facts:
        facts = facts[:: max(1, len(facts) // max_facts)]
    return facts


def _score_all(
    questions: Sequence[Question], evaluations: Sequence[Any], cfg: Config
) -> tuple[list[Scored], float]:
    """Scored items, with ``bm25_bound`` taken from the training part (then used for every item)."""
    train_bm25 = [
        e.top_bm25 or 0.0
        for q, e in zip(questions, evaluations, strict=True)
        if split_of(q.group) == "train"
    ]
    bound = _bound_from(train_bm25)
    score_item = _scorer()
    items = []
    for q, e in zip(questions, evaluations, strict=True):
        c = _rescore(e.signals, e.top_cosine or 0.0, e.top_bm25 or 0.0, bound, cfg)
        if e.candidate is None:
            label: Label = "should_refuse" if q.should_refuse else "wrong"
        else:
            label = label_item(q, e.candidate, score_item)
        items.append(
            Scored(q.item_id, q.group, c, label, capped=bool(e.caps) or e.candidate is None,
                   answerable=not q.should_refuse)
        )  # fmt: skip
    return items, bound


def main(argv: Sequence[str] | None = None) -> int:
    """Ingest the snapshots, score the labelled set, choose and write the thresholds."""
    parser = argparse.ArgumentParser(description="Calibrate tau_floor and tau_accept.")
    parser.add_argument("--snapshots", type=Path, default=ROOT / "snapshots")
    parser.add_argument("--eval", type=Path, default=ROOT / "eval" / "public_eval.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "calibration")
    parser.add_argument(
        "--report", type=Path, default=ROOT / "submission" / "calibration_report.json"
    )
    parser.add_argument("--max-facts", type=int, default=0, help="0 = every fact")
    parser.add_argument("--apply", action="store_true", help="write config.json and the report")
    args = parser.parse_args(argv)

    from kk.system import KnowledgeSystem  # noqa: PLC0415
    from tools.measure_ingest import load_snapshots  # noqa: PLC0415

    ks = KnowledgeSystem()
    for snapshot in load_snapshots(args.snapshots):
        ks.ingest(snapshot)
    facts = _resolved_table_facts(ks, args.max_facts)
    questions = [*public_questions(args.eval), *generate_questions(facts)]
    print(f"scoring {len(questions)} questions", file=sys.stderr)  # noqa: T201
    evaluations = [ks.answerer.evaluate(q.text, q.as_of, use_cache=False) for q in questions]
    items, bound = _score_all(questions, evaluations, ks.config)

    train, _ = split_items(items)
    tau_accept = choose_tau_accept(train, TARGET_PRECISION)
    if tau_accept is None:
        print("no threshold reaches the precision target on the training part", file=sys.stderr)  # noqa: T201
        return 1
    tau_accept = round(tau_accept, _ROUND)
    tau_floor = choose_tau_floor(train, tau_accept, MAX_FLOOR_MISS)
    report = build_report(items, tau_floor, tau_accept, TARGET_PRECISION)
    report["bm25_bound"] = bound
    report["items"] = len(items)
    args.out.mkdir(parents=True, exist_ok=True)
    rows = [
        {"id": q.item_id, "kind": q.kind, "group": q.group, "text": q.text, "label": i.label,
         "c": round(i.confidence, _ROUND), "capped": i.capped}
        for q, i in zip(questions, items, strict=True)
    ]  # fmt: skip
    (args.out / "items.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "curve"}, indent=2))  # noqa: T201
    if args.apply:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        write_thresholds(
            ROOT / "submission" / "config.json",
            tau_floor=tau_floor,
            tau_accept=tau_accept,
            bm25_bound=bound,
            dataset=(
                f"{len(items)} items: 40 public questions plus questions generated from the "
                "latest snapshot's table facts (paraphrases, negatives, adversarial); "
                f"{HELDOUT_SHARE:.0%} held out by group hash"
            ),
            report_name=args.report.name,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
