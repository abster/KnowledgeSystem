"""``answer()`` with the slow path wired in (SPEC 12.2, 13, 16; Step 10). Pages are invented."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from contract import Page, Snapshot
from kk.config import Config
from kk.llm.client import LlmError, LlmTransientError
from kk.query.validators import ValidationResult
from kk.system import KnowledgeSystem
from tests.fixtures.embedder import FakeEmbedder
from tests.fixtures.llm_structured import FakeStructured, accepted
from tests.unit.test_answerer import (
    MM_URL,
    Q_APY,
    SAVINGS_URL,
    _about,
    _config,
    _money_market,
    _refused,
    _savings,
    _snap,
)

Q_SAVINGS = "What is the APY for Example Share Savings?"


def _system(
    snaps: Sequence[Snapshot],
    client: FakeStructured | None,
    *,
    floor: float = 0.1,
    accept: float = 1.0,
) -> KnowledgeSystem:
    ks = KnowledgeSystem(config=_config(floor, accept), embedder=FakeEmbedder(), llm_client=client)
    for snap in snaps:
        ks.ingest(snap)
    return ks


def _one_snapshot() -> list[Snapshot]:
    return [_snap(1, "2026-05-12", _money_market("1.25%"), _about())]


def test_a_slow_route_question_gets_the_validated_model_answer(work_dir: Path) -> None:
    client = FakeStructured(accepted("The Example Money Market APY is 1.25%.", "F1"))
    ev = _system(_one_snapshot(), client).answerer.evaluate(Q_APY, None)
    assert ev.route == "slow"
    assert ev.answer.confident is True
    assert ev.answer.text == "The Example Money Market APY is 1.25%."
    assert [s.url for s in ev.answer.sources] == [MM_URL]
    assert ev.answer.sources[0].snapshot_id == "01_2026-05-12"
    assert len(client.calls) == 1
    assert "1.25%" in client.calls[0]["user"]


def test_without_a_client_the_slow_route_is_a_safe_refusal(work_dir: Path) -> None:
    ev = _system(_one_snapshot(), None).answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal"
    assert "slow path unavailable" in ev.reason
    _refused(ev.answer, Q_APY)


def test_a_model_answer_that_fails_the_validators_is_a_refusal(work_dir: Path) -> None:
    client = FakeStructured(accepted("The Example Money Market APY is 9.99%.", "F1"))
    ev = _system(_one_snapshot(), client).answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal"
    _refused(ev.answer, Q_APY)


def test_a_client_error_is_a_refusal_and_is_not_cached(work_dir: Path) -> None:
    client = FakeStructured(LlmError("HTTP 400"))
    ks = _system(_one_snapshot(), client)
    _refused(ks.answer(Q_APY, None), Q_APY)
    _refused(ks.answer(Q_APY, None), Q_APY)
    assert len(client.calls) == 2  # the failure was not cached, so the second call retried
    assert len(ks.answer_cache) == 0


def test_a_validated_slow_answer_is_cached_for_the_same_canonical_key(work_dir: Path) -> None:
    client = FakeStructured(accepted("The Example Money Market APY is 1.25%.", "F1"))
    ks = _system(_one_snapshot(), client)
    first = ks.answer(Q_APY, None)
    second = ks.answer(Q_APY, None)
    assert first == second
    assert len(client.calls) == 1
    assert ks.answerer.evaluate(Q_APY, None).route == "cache"


def test_a_transient_failure_that_recovers_is_still_one_answer(work_dir: Path) -> None:
    client = FakeStructured(
        LlmTransientError("HTTP 429", 1.0), accepted("The Example Money Market APY is 1.25%.", "F1")
    )
    ks = _system(_one_snapshot(), client)
    ks.answerer.slow.sleep = lambda _s: None  # type: ignore[union-attr]
    assert ks.answer(Q_APY, None).confident is True


def test_below_the_floor_the_model_is_never_called(work_dir: Path) -> None:
    client = FakeStructured(accepted("x", "F1"))
    ev = _system(_one_snapshot(), client, floor=0.99).answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal"
    assert client.calls == []


def test_evaluate_can_run_without_the_slow_path_for_calibration(work_dir: Path) -> None:
    client = FakeStructured(accepted("The Example Money Market APY is 1.25%.", "F1"))
    ks = _system(_one_snapshot(), client)
    ev = ks.answerer.evaluate(Q_APY, None, allow_slow=False)
    assert ev.route == "refusal" and client.calls == []


def test_a_fast_answer_that_fails_validation_escalates_to_the_slow_path(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeStructured(accepted("The Example Money Market APY is 1.25%.", "F1"))
    ks = _system(_one_snapshot(), client, accept=0.6)
    monkeypatch.setattr(
        "kk.query.answerer.validate", lambda *_a, **_k: ValidationResult(False, ("V1",))
    )
    ev = ks.answerer.evaluate(Q_APY, None)
    assert ev.route == "slow" and ev.answer.confident is True
    assert ev.candidate is not None  # calibration still sees the fast candidate


def test_a_question_with_no_candidate_fact_goes_to_the_slow_path_above_a_zero_floor(
    work_dir: Path,
) -> None:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []}
    )
    ks = _system(_one_snapshot(), client, floor=0.0)
    ev = ks.answerer.evaluate("What is the rate on a 7/1 ARM?", None)
    assert len(client.calls) == 1
    assert ev.route == "refusal" and "not answerable" in ev.reason
    _refused(ev.answer, "What is the rate on a 7/1 ARM?")


def test_raw_chunks_are_sent_when_there_are_no_facts(work_dir: Path) -> None:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []}
    )
    ks = _system(_one_snapshot(), client, floor=0.0)
    ks.answer("What is the rate on a 7/1 ARM?", None)
    assert "C1" in client.calls[0]["user"]


def test_ambiguous_tiers_go_to_the_model_with_every_sibling_and_the_entry_tier(
    work_dir: Path,
) -> None:
    client = FakeStructured(
        accepted("For the entry balance tier the Example Share Savings APY is 0.12%.", "F1")
    )
    ks = _system([_snap(1, "2026-06-02", _savings(rows=3))], client)
    ev = ks.answerer.evaluate(Q_SAVINGS, None)
    user = client.calls[0]["user"]
    for value in ("3.50%", "0.15%", "0.12%"):
        assert value in user
    assert "ENTRY TIER" in user
    assert ev.route == "slow" and ev.answer.confident is True
    assert [s.url for s in ev.answer.sources] == [SAVINGS_URL]


def test_a_loser_value_never_reaches_the_prompt_or_the_answer(work_dir: Path) -> None:
    from tests.unit.test_answerer import _promo  # noqa: PLC0415

    client = FakeStructured(accepted("The Example Share Savings APY is 3.50%.", "F1"))
    ks = _system([_snap(1, "2026-06-02", _savings(rows=1), _promo())], client)
    ev = ks.answerer.evaluate(Q_SAVINGS, None, allow_slow=True)
    assert all("2.95%" not in c["user"] for c in client.calls)
    assert "2.95%" not in ev.answer.text


# --- failures before the facts exist (SPEC 16) -----------------------------------------------


def test_a_parse_error_still_reaches_the_slow_path_with_the_raw_question(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise ValueError("bug")

    client = FakeStructured(accepted("The Example Money Market APY is 1.25%.", "C1"))
    ks = _system(_one_snapshot(), client)
    monkeypatch.setattr("kk.query.answerer.parse", boom)
    ev = ks.answerer.evaluate(Q_APY, None)
    assert ev.route == "slow" and ev.answer.confident is True
    assert Q_APY in client.calls[0]["user"]
    assert len(ks.answer_cache) == 0  # no parse, no canonical key, nothing cached


def test_a_parse_error_with_no_client_is_a_safe_refusal(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise ValueError("bug")

    ks = _system(_one_snapshot(), None)
    monkeypatch.setattr("kk.query.answerer.parse", boom)
    _refused(ks.answer(Q_APY, None), Q_APY)


def test_a_retrieval_error_is_retried_once(work_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeStructured(accepted("The Example Money Market APY is 1.25%.", "F1"))
    ks = _system(_one_snapshot(), client)
    real = ks.store.search_vector
    calls = {"n": 0}

    def flaky(*args: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ks.store, "search_vector", flaky)
    assert ks.answerer.evaluate(Q_APY, None).route == "slow"
    assert calls["n"] >= 2  # the first search failed and was repeated


def test_a_retrieval_error_that_persists_is_a_refusal_without_a_model_call(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("down")

    client = FakeStructured(accepted("x", "F1"))
    ks = _system(_one_snapshot(), client)
    monkeypatch.setattr(ks.store, "search_vector", boom)
    ev = ks.answerer.evaluate(Q_APY, None)
    assert ev.route == "refusal" and client.calls == []


def test_every_retrieved_parent_goes_to_the_model_up_to_the_character_cap(work_dir: Path) -> None:
    pages = [
        Page(
            f"https://example.test/page-{i}",
            f"Page {i}",
            f"# Page {i}\n\nExample Money Market note number {i}.\n",
            "2026-05-12T00:00:00Z",
        )
        for i in range(14)
    ]
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []}
    )
    ks = _system([_snap(1, "2026-05-12", *pages)], client, floor=0.0)
    ks.answer("Tell me about Example Money Market notes", None)
    assert client.calls[0]["user"].count("[C") >= 12


# --- page context around prose hits (small-to-big) --------------------------------------------


def _branches_page() -> Page:
    markdown = (
        "# Locations\n\n"
        "### Alpha Branch\n\nAlpha Branch Dupaco Branch\n\n"
        "100 First St., Alpha, IA\n\nOpen today until five\n\n"
        "Drive Up Window hours 8 a.m. to 6 p.m.\n\n"
        "### Beta Branch\n\nBeta Branch Dupaco Branch\n\n"
        "200 Second St., Beta, IA\n\nClosed for the season\n"
    )
    return Page("https://example.test/locations", "Locations", markdown, "2026-05-12T00:00:00Z")


def test_a_prose_hit_brings_its_neighbours_under_the_same_heading(work_dir: Path) -> None:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []}
    )
    ks = _system([_snap(1, "2026-05-12", _branches_page())], client, floor=0.0)
    ks.answer("What is the address of the Alpha Branch?", None)
    user = client.calls[0]["user"]
    block = next(b for b in user.split("\n\n[C") if "100 First St." in b)
    assert (
        "Open today until five" in block and "Drive Up Window hours" in block
    )  # the card around the address
    assert "200 Second St." not in block  # a different heading is a different section


def test_context_is_not_repeated_for_hits_in_the_same_window(work_dir: Path) -> None:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []}
    )
    ks = _system([_snap(1, "2026-05-12", _branches_page())], client, floor=0.0)
    ks.answer("What is the address of the Alpha Branch?", None)
    assert client.calls[0]["user"].count("100 First St., Alpha, IA") == 1


# --- deeper retrieval and query rewriting for the slow path ----------------------------------


def _many_pages(count: int) -> list[Page]:
    return [
        Page(
            f"https://example.test/n-{i}",
            f"Note {i}",
            f"# Note {i}\n\nExample Money Market note number {i} with some words.\n",
            "2026-05-12T00:00:00Z",
        )
        for i in range(count)
    ]


def _deep_config(slow_top_k: int) -> Config:
    base = _config(0.0, 1.0)
    retrieval = base.retrieval.model_copy(
        update={"top_k": 5, "per_index_k": 10, "slow_top_k": slow_top_k, "slow_per_index_k": 60}
    )
    return base.model_copy(update={"retrieval": retrieval})


def _ask_blocks(slow_top_k: int, work_dir: Path) -> int:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []}
    )
    ks = KnowledgeSystem(
        config=_deep_config(slow_top_k), embedder=FakeEmbedder(), llm_client=client
    )
    ks.ingest(_snap(1, "2026-05-12", *_many_pages(30)))
    ks.answer("Tell me about Example Money Market notes", None)
    return int(client.calls[0]["user"].count("[C"))


def test_the_slow_path_reads_deeper_than_the_fast_path(work_dir: Path) -> None:
    shallow = _ask_blocks(5, work_dir)
    deep = _ask_blocks(25, work_dir)
    assert shallow <= 5 < deep <= 25


def test_rewritten_queries_are_searched_and_fused_for_the_slow_path(
    work_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []},
        rewrites=["home loan rates", "house financing"],
    )
    ks = _system(_one_snapshot(), client, floor=0.0)
    seen: list[str] = []
    import kk.query.answerer as module  # noqa: PLC0415

    original = module.retrieve_deep

    def spy(store: Any, embedder: Any, question: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        seen.append(question)
        return original(store, embedder, question, *args, **kwargs)

    monkeypatch.setattr(module, "retrieve_deep", spy)
    ks.answer("What are the mortgage rates?", None)
    assert "home loan rates" in seen and "house financing" in seen
    assert client.rewrite_calls == 1


def test_a_failed_rewrite_does_not_stop_the_slow_path(work_dir: Path) -> None:
    client = FakeStructured(
        accepted("The Example Money Market APY is 1.25%.", "F1"), rewrites=LlmError("HTTP 400")
    )
    ks = _system(_one_snapshot(), client)
    assert ks.answerer.evaluate(Q_APY, None).route == "slow"


def test_the_fast_path_never_calls_the_rewriter(work_dir: Path) -> None:
    client = FakeStructured(accepted("x", "F1"), rewrites=["other words"])
    ks = _system(_one_snapshot(), client, accept=0.6)
    assert ks.answerer.evaluate(Q_APY, None).route == "fast"
    assert client.rewrite_calls == 0


def _last_number(user: str) -> list[int]:
    import re  # noqa: PLC0415

    return [int(re.findall(r"^\[(\d+)\]", user, flags=re.M)[-1])]


def test_the_reranker_puts_its_pick_first_in_the_slow_prompt(work_dir: Path) -> None:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []},
        reranks=_last_number,
    )
    ks = KnowledgeSystem(config=_deep_config(25), embedder=FakeEmbedder(), llm_client=client)
    ks.ingest(_snap(1, "2026-05-12", *_many_pages(30)))
    ks.answer("Tell me about Example Money Market notes", None)
    import re  # noqa: PLC0415

    listed = client.rerank_requests[0]
    last_snippet = re.findall(r"^\[\d+\] (.*)$", listed, flags=re.M)[-1]
    number = re.search(r"note number (\d+) with", last_snippet)
    assert number is not None
    first_block = client.calls[0]["user"].split("[C1]", 1)[1].split("[C2]", 1)[0]
    assert f"note number {number.group(1)} with" in first_block


def test_a_failed_rerank_keeps_the_fused_order(work_dir: Path) -> None:
    client = FakeStructured(
        accepted("The Example Money Market APY is 1.25%.", "F1"), reranks=LlmError("HTTP 400")
    )
    ks = _system(_one_snapshot(), client)
    assert ks.answerer.evaluate(Q_APY, None).route == "slow"


def test_the_fast_path_never_calls_the_reranker(work_dir: Path) -> None:
    client = FakeStructured(accepted("x", "F1"), reranks=[1])
    ks = _system(_one_snapshot(), client, accept=0.6)
    assert ks.answerer.evaluate(Q_APY, None).route == "fast"
    assert client.rerank_requests == []


def test_a_news_page_is_labelled_with_its_publication_date(work_dir: Path) -> None:
    news = Page(
        "https://example.test/newsroom/branches",
        "Branch news",
        "[A Writer](https://example.test/author)\nJune 20, 2025\n\n# Branch news\n\n"
        "Example Credit Union has 23 branch office locations.\n",
        "2026-05-12T00:00:00Z",
    )
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []},
        reranks=[1],
    )
    ks = _system([_snap(1, "2026-05-12", news)], client, floor=0.0)
    ks.answer("How many branch office locations does Example Credit Union have?", None)
    assert "published 2025-06-20" in client.calls[0]["user"]
    assert "published 2025-06-20" in client.rerank_requests[0]


# --- copied boilerplate with different numbers (decision 48) ----------------------------------


def _news_pages() -> list[Page]:
    dates = [("1", "June 20, 2025", 23), ("2", "October 8, 2025", 23), ("3", "April 6, 2026", 22)]
    return [
        Page(
            f"https://example.test/newsroom/story-{n}",
            f"Story {n}",
            f"[A Writer](https://example.test/author)\n{stamp}\n\n# Story {n}\n\n"
            f"Story {n} is about a different topic.\n\n## About\n\n"
            f"Example CU has over 600 employees and {count} branch office locations. Learn more.\n",
            "2026-05-12T00:00:00Z",
        )
        for n, stamp, count in dates
    ]


def _branch_run(question: str) -> str:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []},
        reranks=list(range(1, 40)),
    )
    ks = _system([_snap(1, "2026-05-12", *_news_pages())], client, floor=0.0)
    ks.answer(question, None)
    return str(client.calls[0]["user"])


def test_the_newest_copy_of_a_repeated_blurb_comes_first_and_older_copies_stay() -> None:
    user = _branch_run("How many branch office locations does Example CU have?")
    assert "22 branch office locations" in user
    assert "23 branch office locations" in user  # nothing is removed
    assert user.index("22 branch office locations") < user.index("23 branch office locations")


def test_a_change_question_still_sees_every_copy() -> None:
    user = _branch_run("How has the number of branch office locations changed over time?")
    assert "22 branch office locations" in user
    assert "23 branch office locations" in user


# --- stale pages that the question did not name -----------------------------------------------


def _rates_page(url: str, apy: str, date: str) -> Page:
    markdown = (
        "# Savings\n\n## Savings Rates\n\n"
        "| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY) |\n"
        "| --- | --- | --- | --- |\n"
        f"| Example Share Savings | $25,000+ | {apy} | {apy} |\n\n"
        f"Rates are effective as of {date}.\n"
    )
    return Page(url, "Savings", markdown, "2026-06-02T00:00:00Z")


def test_a_superseded_table_is_left_out_even_when_the_question_names_no_entity(
    work_dir: Path,
) -> None:
    client = FakeStructured(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []}
    )
    pages = [
        _rates_page("https://example.test/old-rates", "1.11%", "04/22/2026"),
        _rates_page("https://example.test/rates", "9.09%", "09/25/2026"),
    ]
    ks = _system([_snap(1, "2026-09-28", *pages)], client, floor=0.0)
    ks.answer("What are the current savings rates?", None)
    user = client.calls[0]["user"]
    assert "9.09%" in user
    assert "1.11%" not in user  # the older generation never reaches the model


def test_a_group_loser_excludes_its_chunk_only_when_it_lost_on_evidence_of_being_replaced() -> None:
    from kk.conflicts import Resolution  # noqa: PLC0415
    from kk.query.answerer import Answerer  # noqa: PLC0415
    from kk.query.retrieve import CandidateGroup  # noqa: PLC0415
    from tests.fixtures.facts import make_version  # noqa: PLC0415

    winner = make_version(value="6,187", attribute="count", url="https://example.test/b", last=5)
    loser = make_version(value="7,260", attribute="count", url="https://example.test/a", last=5)

    def group(strategy: str) -> CandidateGroup:
        return CandidateGroup(
            winner.fact.fact_key,
            (winner, loser),
            Resolution("resolved", winner, strategy),
            0.5,
            1,
            1,
            0.1,
        )

    recency = Answerer._loser_chunk_ids([group("snapshot_recency")])
    dated = Answerer._loser_chunk_ids([group("effective_date")])
    assert recency == frozenset()
    assert dated == {loser.fact.chunk_id}
