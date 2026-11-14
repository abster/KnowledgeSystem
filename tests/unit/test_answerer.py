"""End-to-end tests of the fast-path ``answer()`` on synthetic snapshots (Step 9).

Covers SPEC 8.1 (as_of), 9.2 to 9.4, 10.5, 11 (cache), 12 (routing, templates, refusal), 14
(validators) and 16 (error boundaries). Pages are invented; no real site text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from contract import Answer, Page, Snapshot
from kk.config import Config, load_config
from kk.ingest.aliases import canonical_id
from kk.query.answerer import Evaluation, build_linker
from kk.query.validators import ValidationResult
from kk.system import KnowledgeSystem
from tests.fixtures.embedder import FakeEmbedder

MM_URL = "https://example.test/personal/money-market"
SAVINGS_URL = "https://example.test/personal/savings"
PROMO_URL = "https://example.test/promotions/summer"
OTHER_URL = "https://example.test/about"
DIGIT = re.compile(r"\d")
Q_APY = "What is the APY for Example Money Market?"


def _config(floor: float = 0.1, accept: float = 0.6) -> Config:
    """Permissive thresholds, so these tests exercise routing and not calibration."""
    base = load_config()
    confidence = base.confidence.model_copy(update={"tau_floor": floor, "tau_accept": accept})
    return base.model_copy(update={"confidence": confidence})


def _money_market(apy: str = "1.25%", rate: str = "1.20%") -> Page:
    markdown = (
        "# Money Market\n\n## Money Market Rates\n\n"
        "| Account Type | Dividend Rate | Annual Percentage Yield (APY) |\n"
        "| --- | --- | --- |\n"
        f"| Example Money Market | {rate} | {apy} |\n"
    )
    return Page(MM_URL, "Money Market", markdown, "2026-05-12T00:00:00Z")


def _savings(apy: str = "3.50%", rows: int = 1) -> Page:
    tiers = [
        ("Example Share Savings", "$25,000+", apy),
        ("Example Share Savings", "$10,000 - $24,999.99", "0.15%"),
        ("Example Share Savings", "$5,000 - $9,999.99", "0.12%"),
    ][:rows]
    body = "\n".join(f"| {a} | {b} | {c} | {c} |" for a, b, c in tiers)
    markdown = (
        "# Savings\n\n## Savings Rates\n\n"
        "| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |\n"
        "| --- | --- | --- | --- |\n"
        f"{body}\n\nRates are effective as of 06/02/2026.\n"
    )
    return Page(SAVINGS_URL, "Savings", markdown, "2026-06-02T00:00:00Z")


def _promo() -> Page:
    markdown = (
        "# Summer Promo\n\n## Savings Rates\n\n"
        "| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |\n"
        "| --- | --- | --- | --- |\n"
        "| Example Share Savings | $25,000+ | 2.90% | 2.95% |\n\n"
        "Summer special. Offer ended 2025-09-30.\n"
    )
    return Page(PROMO_URL, "Summer Promo", markdown, "2026-06-02T00:00:00Z")


def _about() -> Page:
    return Page(OTHER_URL, "About", "# About\n\nWe are a credit union.\n", "2026-05-12T00:00:00Z")


def _snap(n: int, taken_on: str, *pages: Page) -> Snapshot:
    return Snapshot(snapshot_id=f"{n:02d}_{taken_on}", taken_on=taken_on, pages=list(pages))


def _system(snaps: Sequence[Snapshot], cfg: Config | None = None) -> KnowledgeSystem:
    ks = KnowledgeSystem(config=cfg or _config(), embedder=FakeEmbedder())
    for snap in snaps:
        ks.ingest(snap)
    return ks


@pytest.fixture
def three(work_dir: Path) -> KnowledgeSystem:
    """APY 1.25% in snapshots 1 and 2, 1.40% in snapshot 3."""
    return _system(
        [
            _snap(1, "2026-05-12", _money_market("1.25%")),
            _snap(2, "2026-05-19", _money_market("1.25%")),
            _snap(3, "2026-06-09", _money_market("1.40%")),
        ]
    )


def _refused(answer: Answer, question: str) -> None:
    assert answer.confident is False
    assert answer.sources == []
    assert set(DIGIT.findall(answer.text)) <= set(DIGIT.findall(question))


# --- the fast answer -------------------------------------------------------------------------


def test_a_unique_table_fact_is_answered_from_a_template(three: KnowledgeSystem) -> None:
    ans = three.answer(Q_APY, None)
    assert ans.confident is True
    assert ans.text == "The Example Money Market APY is 1.40%."
    assert ans.conflict_note is None


def test_the_answer_cites_the_winning_page_with_a_verbatim_quote(three: KnowledgeSystem) -> None:
    ans = three.answer(Q_APY, None)
    assert len(ans.sources) == 1
    source = ans.sources[0]
    assert source.url == MM_URL
    assert source.snapshot_id == "03_2026-06-09"
    assert source.quote is not None
    assert 0 < len(source.quote) <= 200
    assert "1.40%" in source.quote
    assert source.quote in _money_market("1.40%").markdown


def test_the_answer_never_contains_the_other_attribute(three: KnowledgeSystem) -> None:
    ans = three.answer(Q_APY, None)
    assert "1.20%" not in ans.text  # the dividend rate is a different attribute


# --- as_of (SPEC 8.1, ASOF-5) ----------------------------------------------------------------


def test_as_of_picks_the_snapshot_and_never_reads_a_later_one(three: KnowledgeSystem) -> None:
    ans = three.answer(Q_APY, "2026-05-31")
    assert "1.25%" in ans.text
    assert "1.40%" not in ans.text
    assert [s.snapshot_id for s in ans.sources] == ["02_2026-05-19"]


def test_as_of_is_inclusive_and_accepts_a_datetime(three: KnowledgeSystem) -> None:
    assert "1.40%" in three.answer(Q_APY, "2026-06-09").text
    assert "1.25%" in three.answer(Q_APY, "2026-06-08T23:59:59").text


@pytest.mark.parametrize("as_of", ["2026-05-11", "not-a-date", "2026-13-45", ""])
def test_no_snapshot_or_an_invalid_as_of_is_a_safe_refusal(
    three: KnowledgeSystem, as_of: str
) -> None:
    _refused(three.answer(Q_APY, as_of), Q_APY)


def test_a_date_in_the_question_is_never_read_as_the_target_date(three: KnowledgeSystem) -> None:
    question = "What is the APY for Example Money Market as of 2026-05-12?"
    ans = three.answer(question, None)
    assert "1.25%" not in ans.text  # the date words are residual, so no fast path (CHG-4)
    _refused(ans, question)


# --- conflicts (SPEC 10.5, CA-1) -------------------------------------------------------------


def test_a_resolved_conflict_answers_with_the_winner_and_names_the_loser_in_the_note(
    work_dir: Path,
) -> None:
    ks = _system([_snap(1, "2026-06-02", _savings(), _promo())])
    ans = ks.answer("What is the APY for Example Share Savings?", None)
    assert ans.confident is True
    assert "3.50%" in ans.text
    assert "2.95%" not in ans.text
    assert ans.conflict_note is not None
    assert ans.conflict_note.startswith("strategy=effective_date")
    assert "2.95%" in ans.conflict_note
    assert {s.url for s in ans.sources} == {SAVINGS_URL}


# --- refusals --------------------------------------------------------------------------------


def test_a_question_with_several_tiers_and_no_amount_is_refused_not_guessed(
    work_dir: Path,
) -> None:
    ks = _system([_snap(1, "2026-06-02", _savings(rows=3))])
    question = "What is the APY for Example Share Savings?"
    ev = ks.answerer.evaluate(question, None)
    assert "several_tiers" in ev.caps
    assert ev.route == "refusal"  # the slow path does not exist before Step 10 (D10)
    _refused(ks.answer(question, None), question)


@pytest.mark.parametrize(
    "question",
    [
        "What is the APY for the 7/1 ARM?",
        "How many ATMs are there?",
        "",
        " ",
        "x" * 5000,
        "\x00",
        "What's the APY for Example Money Market, roughly?",
    ],
)
def test_an_unknown_or_unsupported_question_is_a_safe_refusal(
    three: KnowledgeSystem, question: str
) -> None:
    _refused(three.answer(question, None), question)


def test_answer_before_any_ingest_is_a_safe_refusal(work_dir: Path) -> None:
    _refused(_system([]).answer(Q_APY, None), Q_APY)


def test_below_the_relevance_floor_is_a_refusal_even_for_a_covered_question(
    work_dir: Path,
) -> None:
    ks = _system([_snap(1, "2026-05-12", _money_market())], cfg=_config(floor=0.99, accept=1.0))
    ev = ks.answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal"
    assert "floor" in ev.reason
    _refused(ev.answer, Q_APY)


def test_between_the_thresholds_is_refused_until_the_slow_path_exists(work_dir: Path) -> None:
    ks = _system([_snap(1, "2026-05-12", _money_market())], cfg=_config(floor=0.1, accept=1.0))
    ev = ks.answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal"
    assert "slow" in ev.reason
    _refused(ev.answer, Q_APY)


# --- history (SPEC 9.4, 12.5) ----------------------------------------------------------------


def test_a_history_question_that_changed_names_both_values_and_only_snapshot_dates(
    three: KnowledgeSystem,
) -> None:
    ans = three.answer("Has the Example Money Market APY changed?", None)
    assert ans.confident is True
    assert ans.text.startswith("Yes - ")
    assert "1.25% as of 2026-05-19" in ans.text
    assert "1.40% by 2026-06-09" in ans.text
    assert {s.snapshot_id for s in ans.sources} == {"01_2026-05-12", "03_2026-06-09"}


def test_an_unchanged_history_says_no_with_the_first_and_last_snapshot_dates(
    work_dir: Path,
) -> None:
    ks = _system([_snap(1, "2026-05-12", _money_market()), _snap(2, "2026-05-19", _money_market())])
    ans = ks.answer("Has the Example Money Market APY changed?", None)
    assert ans.text.startswith("No - ")
    assert "1.25%" in ans.text
    assert "from 2026-05-12 to 2026-05-19" in ans.text


def test_history_respects_as_of(three: KnowledgeSystem) -> None:
    ans = three.answer("Has the Example Money Market APY changed?", "2026-05-31")
    assert ans.text.startswith("No - ")
    assert "1.40%" not in ans.text


def test_a_history_with_a_gap_is_refused_not_worded(work_dir: Path) -> None:  # HSEG-3
    ks = _system(
        [
            _snap(1, "2026-05-12", _money_market("1.25%")),
            _snap(2, "2026-05-19", _about()),
            _snap(3, "2026-06-09", _money_market("1.40%")),
        ]
    )
    question = "Has the Example Money Market APY changed?"
    ev = ks.answerer.evaluate(question, None)
    assert "history_untrusted" in ev.caps
    _refused(ev.answer, question)


def test_a_history_question_without_a_measure_is_refused(three: KnowledgeSystem) -> None:
    question = "Has the Example Money Market changed?"
    ev = three.answerer.evaluate(question, None)
    assert "history_untrusted" in ev.caps
    _refused(ev.answer, question)


# --- cache (SPEC 11) -------------------------------------------------------------------------


def test_paraphrases_with_one_canonical_key_share_a_cache_entry(three: KnowledgeSystem) -> None:
    first = three.answer("What is the APY for Example Money Market?", None)
    embedded = len(three.embedder.query_texts)  # type: ignore[attr-defined]
    second = three.answer("What's the APY for the Example Money Market?", None)
    assert second == first
    assert len(three.answer_cache) == 1
    assert len(three.embedder.query_texts) == embedded  # type: ignore[attr-defined]


def test_a_different_target_ordinal_is_a_different_entry(three: KnowledgeSystem) -> None:
    latest = three.answer(Q_APY, None)
    earlier = three.answer(Q_APY, "2026-05-31")
    assert latest.text != earlier.text
    assert len(three.answer_cache) == 2


def test_a_question_that_is_not_fully_covered_is_never_cached(three: KnowledgeSystem) -> None:
    three.answer("What's the APY for Example Money Market, roughly?", None)
    three.answer("What is the APY for the 7/1 ARM?", None)
    assert len(three.answer_cache) == 0


def test_a_question_routed_to_the_slow_path_is_not_cached(work_dir: Path) -> None:
    ks = _system([_snap(1, "2026-06-02", _savings(rows=3))])
    ks.answer("What is the APY for Example Share Savings?", None)
    assert len(ks.answer_cache) == 0


def test_a_deterministic_refusal_is_cached(work_dir: Path) -> None:  # CACHE-3
    ks = _system([_snap(1, "2026-05-12", _money_market())], cfg=_config(floor=0.99, accept=1.0))
    first = ks.answer(Q_APY, None)
    assert len(ks.answer_cache) == 1
    assert ks.answer(Q_APY, None) == first


def test_ingest_clears_the_cache_so_a_new_snapshot_is_seen(work_dir: Path) -> None:  # CACHE-2
    ks = _system([_snap(1, "2026-05-12", _money_market("1.25%"))])
    assert "1.25%" in ks.answer(Q_APY, None).text
    assert len(ks.answer_cache) == 1
    ks.ingest(_snap(2, "2026-05-19", _money_market("1.40%")))
    assert len(ks.answer_cache) == 0
    assert "1.40%" in ks.answer(Q_APY, None).text


def test_the_same_question_gives_the_same_text_with_and_without_the_cache(
    three: KnowledgeSystem,
) -> None:  # DET-5
    first = three.answer(Q_APY, None)
    cached = three.answer(Q_APY, None)
    three.answer_cache.clear()
    fresh = three.answer(Q_APY, None)
    assert first.text == cached.text == fresh.text
    assert first == fresh


def test_a_cache_error_is_ignored(three: KnowledgeSystem, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("cache broken")

    monkeypatch.setattr(three.answer_cache, "get", boom)
    monkeypatch.setattr(three.answer_cache, "put", boom)
    assert "1.40%" in three.answer(Q_APY, None).text


# --- error boundaries (SPEC 16) --------------------------------------------------------------


@pytest.mark.parametrize("stage", ["parse", "retrieve", "candidate_facts", "validate"])
def test_a_raising_stage_still_returns_a_safe_refusal_that_is_not_cached(
    three: KnowledgeSystem, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(f"{stage} broke")

    monkeypatch.setattr(f"kk.query.answerer.{stage}", boom)
    ev = three.answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal"
    assert stage in ev.reason or "error" in ev.reason
    _refused(three.answer(Q_APY, None), Q_APY)
    assert len(three.answer_cache) == 0  # failures are never cached (SPEC 16)


def test_a_missing_table_still_returns_an_answer(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ks = _system([_snap(1, "2026-05-12", _money_market())])

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("table missing")

    monkeypatch.setattr(ks.store, "search_vector", boom)
    _refused(ks.answer(Q_APY, None), Q_APY)


def test_a_fast_answer_that_fails_a_validator_is_not_returned(
    three: KnowledgeSystem, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kk.query.answerer.validate", lambda *_a, **_k: ValidationResult(False, ("V1",))
    )
    ev = three.answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal"  # escalated to the slow path, which does not exist yet
    assert "V1" in ev.reason
    assert ev.validated is False
    assert ev.candidate is not None  # calibration still sees what the fast path would have said
    _refused(ev.answer, Q_APY)


# --- the evaluation record used by calibration -----------------------------------------------


def test_evaluate_exposes_confidence_caps_and_the_candidate(three: KnowledgeSystem) -> None:
    ev = three.answerer.evaluate(Q_APY, None)
    assert isinstance(ev, Evaluation)
    assert ev.route == "fast"
    assert 0.0 <= (ev.confidence or 0.0) <= 1.0
    assert ev.caps == ()
    assert ev.validated is True
    assert ev.candidate is not None
    assert ev.candidate.text == ev.answer.text
    assert ev.top_bm25 is not None


def test_a_candidate_is_reported_even_when_the_route_is_a_refusal(work_dir: Path) -> None:
    ks = _system([_snap(1, "2026-05-12", _money_market())], cfg=_config(floor=0.1, accept=1.0))
    ev = ks.answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal"
    assert ev.candidate is not None
    assert "1.25%" in ev.candidate.text
    assert "1.25%" not in ev.answer.text


def test_a_cached_answer_reports_the_cache_route(three: KnowledgeSystem) -> None:
    three.answer(Q_APY, None)
    assert three.answerer.evaluate(Q_APY, None).route == "cache"


# --- entity linking uses prose labels --------------------------------------------------------


def test_the_linker_knows_table_entities_and_prose_entities() -> None:
    linker = build_linker(
        labels=["Example Money Market"], prose_entities=[canonical_id("Hy-Vee Branch")]
    )
    assert linker.index.resolve("example money market") == "example money market"
    assert linker.index.resolve("hy vee branch") == "hy vee branch"
    assert linker.index.resolve("hy vee branches") == "hy vee branch"
