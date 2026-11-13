from __future__ import annotations

import re
from pathlib import Path

import pytest

from contract import Answer, KnowledgeSystem, Page, Snapshot
from kk.system import REFUSAL_TEXT
from solution import build
from tests.fixtures.embedder import FakeEmbedder

DIGITS = re.compile(r"\d")


@pytest.fixture(autouse=True)
def _fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stub tests do not need the real embedding model."""
    monkeypatch.setattr("kk.system.load_embedder", FakeEmbedder)


def _snapshot(snapshot_id: str = "01_2026-01-05_baseline") -> Snapshot:
    page = Page(
        url="https://example.test/a", title="A", markdown="# A\n\nrate 9.99%", fetched_at="x"
    )
    return Snapshot(snapshot_id=snapshot_id, taken_on="2026-01-05", pages=[page])


def test_stub_satisfies_protocol(work_dir: Path) -> None:
    assert isinstance(build(), KnowledgeSystem)


def test_answer_before_ingest_is_safe_refusal(work_dir: Path) -> None:
    ans = build().answer("What is the rate?", as_of=None)
    assert isinstance(ans, Answer)
    assert ans.confident is False
    assert ans.sources == []
    assert ans.text == REFUSAL_TEXT


def test_conflicts_before_and_after_ingest_is_empty(work_dir: Path) -> None:
    ks = build()
    assert ks.conflicts() == []
    ks.ingest(_snapshot())
    assert ks.conflicts() == []


@pytest.mark.parametrize("as_of", [None, "2026-05-31", "not-a-date", "", "2026-05-31T10:00:00"])
def test_answer_never_raises(work_dir: Path, as_of: str | None) -> None:
    ks = build()
    ks.ingest(_snapshot())
    ans = ks.answer("Anything at all?", as_of=as_of)
    assert ans.confident is False


@pytest.mark.parametrize(
    "question", ["", " ", "x" * 100_000, "Rate for 36 months at 4.5%?", "\x00"]
)
def test_refusal_has_no_new_digits(work_dir: Path, question: str) -> None:
    ans = build().answer(question)
    assert not (set(DIGITS.findall(ans.text)) - set(DIGITS.findall(question)))


def test_ingest_does_not_raise_on_empty_snapshot(work_dir: Path) -> None:
    build().ingest(Snapshot(snapshot_id="s", taken_on="2026-01-01", pages=[]))


def test_ingest_records_snapshots_in_order_without_page_text(work_dir: Path) -> None:
    ks = build()
    ks.ingest(_snapshot("01_a"))
    ks.ingest(_snapshot("02_b"))
    assert [s.snapshot_id for s in ks.store.snapshots] == ["01_a", "02_b"]  # type: ignore[attr-defined]


def test_build_writes_only_under_work_dir_and_not_stdout(
    work_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ks = build()
    ks.ingest(_snapshot())
    ks.answer("q")
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert any(work_dir.iterdir())
