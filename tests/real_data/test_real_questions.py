"""Real-data checks for Step 6. Skipped when ``snapshots/`` or ``eval/`` is absent.

Parses the public questions against an alias index built from the latest real snapshot and
writes **counts only** to ``results/`` (git-ignored). The test asserts invariants, never how
many questions pass: the lists in ``question.py`` are not tuned against the public questions.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from kk.ingest.aliases import build_aliases
from kk.ingest.tables import entity_labels
from kk.ingest.tables_report import Snapshot, load_snapshots
from kk.query.asof import target_ordinal
from kk.query.grammar import PATTERN_NAMES
from kk.query.question import EntityLinker, parse
from kk.types import SnapshotRef

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.real_data


@pytest.fixture(scope="module")
def corpus() -> list[Snapshot]:
    snaps = load_snapshots(ROOT / "snapshots")
    if not snaps:
        pytest.skip("snapshots/ is not present")
    return snaps


@pytest.fixture(scope="module")
def questions() -> list[dict[str, object]]:
    path = ROOT / "eval" / "public_eval.jsonl"
    if not path.exists():
        pytest.skip("eval/public_eval.jsonl is not present")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _refs(corpus: list[Snapshot]) -> tuple[SnapshotRef, ...]:
    return tuple(
        SnapshotRef(ordinal=i, snapshot_id=s.snapshot_id, taken_on=s.taken_on)
        for i, s in enumerate(corpus, 1)
    )


def test_every_public_as_of_resolves_or_is_a_clean_refusal(
    corpus: list[Snapshot], questions: list[dict[str, object]]
) -> None:
    refs = _refs(corpus)
    for item in questions:
        result = target_ordinal(item.get("as_of"), refs)  # type: ignore[arg-type]
        assert result.reason in {"ok", "before_first_snapshot"}, item["id"]
        if result.ordinal is not None:
            assert 1 <= result.ordinal <= len(refs)


def test_the_spec_example_date_gives_snapshot_two(corpus: list[Snapshot]) -> None:
    refs = _refs(corpus)
    assert [r.taken_on for r in refs[:3]] == ["2026-05-12", "2026-05-19", "2026-06-09"]
    assert target_ordinal("2026-05-31", refs).ordinal == 2


def test_parsing_the_public_questions_is_safe_and_writes_counts(
    corpus: list[Snapshot], questions: list[dict[str, object]]
) -> None:
    linker = EntityLinker(
        build_aliases(label for page in corpus[-1].pages for label in entity_labels(page))
    )
    refs = _refs(corpus)
    shapes: Counter[str] = Counter()
    patterns: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    covered = fast = 0
    for item in questions:
        ordinal = target_ordinal(item.get("as_of"), refs).ordinal  # type: ignore[arg-type]
        text = str(item["question"])
        parsed = parse(text, linker, target_ord=ordinal)
        assert parsed == parse(text, linker, target_ord=ordinal)
        shapes[parsed.shape] += 1
        covered += parsed.covered
        if parsed.fast_eligible:
            fast += 1
            patterns[str(parsed.pattern)] += 1
            assert parsed.pattern in PATTERN_NAMES
            assert parsed.covered
            assert not parsed.uncertain
            if ordinal is not None:
                assert parsed.canonical_key is not None
        else:
            assert parsed.canonical_key is None
        for reason in parsed.reasons:
            reasons[reason] += 1
    out = ROOT / "results" / "step6_questions.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "questions": len(questions),
                "covered": covered,
                "fast_eligible": fast,
                "shapes": dict(sorted(shapes.items())),
                "patterns": dict(sorted(patterns.items())),
                "reasons": dict(reasons.most_common()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
