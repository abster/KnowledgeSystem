"""Ingest wiring of prose facts and row-label verification (Step 8; PRO-7, TBL-12, SPEC 6.12)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from contract import Page, Snapshot
from kk.config import load_config
from kk.llm.client import LlmTransientError
from kk.system import KnowledgeSystem
from tests.fixtures.embedder import FakeEmbedder
from tests.fixtures.llm import FakeLlm, failing
from tests.unit.test_ingest_pipeline import SAVINGS_URL, _savings_page

RATE_TEXT = "The 30-year fixed mortgage rate is 7.13% for loans of $100,000 or less."
QUOTE = "30-year fixed mortgage rate is 7.13%"
PROSE_URL = "https://example.test/home-loans"


def _prose_page() -> Page:
    markdown = f"# Home Loans\n\nWelcome to home loans.\n\n{RATE_TEXT}\n"
    return Page(PROSE_URL, "Home Loans", markdown, "2026-06-02T00:00:00Z")


def _snap(n: int, day: str, *pages: Page) -> Snapshot:
    return Snapshot(snapshot_id=f"{n:02d}_{day}", taken_on=day, pages=list(pages))


def _respond(request: dict[str, Any], *, reject: str = "") -> dict[str, Any]:
    if request["tool_name"] == "record_facts":
        if QUOTE not in request["user"]:
            return {"facts": []}
        return {
            "facts": [
                {
                    "entity": "Mortgage",
                    "attribute": "rate",
                    "qualifiers": {"term": "30-year", "rate_type": "fixed"},
                    "value": "7.13%",
                    "unit": "percent",
                    "quote": QUOTE,
                }
            ]
        }
    return {"correct": not (reject and reject in request["user"])}


def _calls(client: FakeLlm, tool: str) -> int:
    return sum(1 for c in client.calls if c["tool_name"] == tool)


def test_prose_facts_are_stored_with_their_origin_and_counted(work_dir: Path) -> None:
    client = FakeLlm(_respond)
    ks = KnowledgeSystem(embedder=FakeEmbedder(), llm_client=client)
    ks.ingest(_snap(1, "2026-06-02", _prose_page(), _savings_page()))
    prose = [v for v in ks.store.visible_fact_versions(1) if v.fact.origin == "prose_llm"]
    assert [(v.fact.entity, v.fact.value_text, v.url) for v in prose] == [
        ("mortgage", "7.13%", PROSE_URL)
    ]
    summary = ks.last_summary
    assert summary is not None
    assert summary.facts_by_origin["prose_llm"] == 1
    assert summary.prose.calls >= 1 and summary.prose.facts == 1
    assert "mortgage" in ks.prose_labels


def test_a_second_snapshot_makes_no_new_calls(work_dir: Path) -> None:
    client = FakeLlm(_respond)
    ks = KnowledgeSystem(embedder=FakeEmbedder(), llm_client=client)
    ks.ingest(_snap(1, "2026-06-02", _prose_page(), _savings_page()))
    made = len(client.calls)
    ks.ingest(_snap(2, "2026-06-09", _prose_page(), _savings_page()))
    assert len(client.calls) == made
    assert ks.last_summary is not None
    assert ks.last_summary.prose.calls == 0 and ks.last_summary.prose.cache_hits >= 1


def test_without_a_key_ingest_completes_with_table_facts_and_one_warning(work_dir: Path) -> None:
    ks = KnowledgeSystem(embedder=FakeEmbedder())
    ks.ingest(_snap(1, "2026-06-02", _prose_page(), _savings_page()))
    ks.ingest(_snap(2, "2026-06-09", _prose_page(), _savings_page()))
    origins = {v.fact.origin for v in ks.store.visible_fact_versions(2)}
    assert origins == {"table_rule"}
    log = (work_dir / "kk.log").read_text(encoding="utf-8")
    assert log.count("no API key") == 1
    assert ks.last_summary is not None and ks.last_summary.prose.skipped_no_key >= 1


def test_failing_calls_never_stop_ingest(work_dir: Path) -> None:
    client = FakeLlm(failing(LlmTransientError("rate limit")))
    ks = KnowledgeSystem(embedder=FakeEmbedder(), llm_client=client)
    ks.ingest(_snap(1, "2026-06-02", _prose_page(), _savings_page()))
    assert ks.last_summary is not None
    assert ks.last_summary.prose.failures >= 1
    assert {v.fact.origin for v in ks.store.visible_fact_versions(1)} == {"table_rule"}


def test_a_no_from_the_verifier_quarantines_that_row_labels_facts(work_dir: Path) -> None:
    client = FakeLlm(lambda r: _respond(r, reject="Example Share Savings"))
    ks = KnowledgeSystem(embedder=FakeEmbedder(), llm_client=client)
    ks.ingest(_snap(1, "2026-06-02", _prose_page(), _savings_page()))
    origins = {v.fact.origin for v in ks.store.visible_fact_versions(1)}
    assert origins == {"prose_llm"}
    summary = ks.last_summary
    assert summary is not None
    assert summary.verify_rejected >= 1 and summary.verify_facts_removed > 0
    assert summary.facts_by_origin.get("table_rule", 0) == 0


def test_a_yes_keeps_table_facts_and_each_label_is_asked_once(work_dir: Path) -> None:
    client = FakeLlm(_respond)
    ks = KnowledgeSystem(embedder=FakeEmbedder(), llm_client=client)
    ks.ingest(_snap(1, "2026-06-02", _savings_page(rows=2)))
    assert {v.fact.origin for v in ks.store.visible_fact_versions(1)} == {"table_rule"}
    asked = _calls(client, "record_verdict")
    assert 1 <= asked <= 2  # one per distinct (label, qualifier cells) parse, not per fact
    ks.ingest(_snap(2, "2026-06-09", _savings_page(rows=2)))
    assert _calls(client, "record_verdict") == asked


def test_the_verify_flag_off_means_no_verification_calls(work_dir: Path) -> None:
    config = load_config()
    off = config.model_copy(
        update={"flags": config.flags.model_copy(update={"ingest_llm_verify": False})}
    )
    client = FakeLlm(lambda r: _respond(r, reject="Example Share Savings"))
    ks = KnowledgeSystem(config=off, embedder=FakeEmbedder(), llm_client=client)
    ks.ingest(_snap(1, "2026-06-02", _savings_page()))
    assert _calls(client, "record_verdict") == 0
    assert {v.fact.origin for v in ks.store.visible_fact_versions(1)} == {"table_rule"}


def test_the_savings_url_constant_is_the_fixture_page() -> None:
    assert _savings_page().url == SAVINGS_URL
