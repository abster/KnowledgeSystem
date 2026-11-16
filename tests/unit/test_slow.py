"""The slow path: payload, acceptance rules, deadline and retry (SPEC 13, 14, 16; Step 10).

Facts and pages are invented. Evidence ids in the payload are ``F<n>`` for resolved facts
(in group order) and ``C<n>`` for parent chunks (in score order).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

import pytest

from kk.conflicts import Resolution
from kk.llm.client import LlmError, LlmOutputError, LlmTransientError
from kk.query.retrieve import CandidateGroup
from kk.query.slow import (
    MAX_CHUNK_CHARS,
    MAX_HISTORY_LINES,
    SLOW_SCHEMA,
    SYSTEM_PROMPT,
    ParentChunk,
    SlowInputs,
    SlowPath,
    build_request,
    history_view,
    loser_numbers,
    loser_scopes,
    published_date,
    stale_tables,
    stale_tables_detail,
    superseded,
    superseded_detail,
)
from kk.types import Chunk, FactVersion
from tests.fixtures.facts import SNAPS, make_version
from tests.fixtures.llm_structured import FakeClock, FakeStructured, accepted

QUESTION = "What is the mortgage rate?"
URL = "https://example.test/rates/mortgage"


def _group(version: FactVersion, score: float = 0.5, *, unresolved: bool = False) -> CandidateGroup:
    resolution = (
        Resolution("unresolved", None, "unresolved")
        if unresolved
        else Resolution("single", version, None)
    )
    return CandidateGroup(
        fact_key=version.fact.fact_key,
        versions=(version,),
        resolution=resolution,
        score=score,
        qualifier=1.0,
        entity=1.0,
        rrf=0.1,
    )


def _tier(value: str, tier: str, **kw: Any) -> FactVersion:  # noqa: ANN401
    return make_version(
        value=value,
        entity="mortgage",
        label="Mortgage",
        attribute="apr",
        url=URL,
        qualifiers={"amount_tier": tier},
        **kw,
    )


def _inputs(groups: list[CandidateGroup], **kw: Any) -> SlowInputs:  # noqa: ANN401
    return SlowInputs(
        question=QUESTION,
        taken_on="2026-09-28",
        snapshot_id="05_2026-09-28",
        groups=tuple(groups),
        **kw,
    )


# --- the payload (SLOW-6, 13.1) --------------------------------------------------------------


def test_the_payload_holds_the_resolved_value_and_names_the_snapshot_date() -> None:
    request = build_request(_inputs([_group(_tier("6.10%", "<=100000"))]))
    assert "6.10%" in request.user
    assert "2026-09-28" in request.user
    assert QUESTION in request.user
    assert request.evidence["F1"].url == URL


def test_an_unresolved_conflict_gives_no_value_and_a_not_confident_answer() -> None:
    version = _tier("6.10%", "<=100000")
    request = build_request(_inputs([_group(version, unresolved=True)]))
    assert "6.10%" not in request.user
    assert "unresolved" in request.user.lower()
    assert request.confident is False


def test_a_resolved_conflict_note_reaches_the_model_without_the_loser_value() -> None:
    request = build_request(
        _inputs(
            [_group(_tier("6.10%", "<=100000"))],
            conflict_notes=("strategy=effective_date: 6.10% used; older value superseded",),
        )
    )
    assert "strategy=effective_date" in request.user


def test_the_system_prompt_carries_the_slow_rules() -> None:  # SLOW-2
    system = build_request(_inputs([])).system.lower()
    assert "only from the supplied evidence" in system
    assert "stale" in system and "expired" in system and "superseded" in system
    assert "assumed tier" in system
    assert "answerable" in system


def test_parent_chunks_are_capped_at_12000_characters_best_first() -> None:  # 13.1
    chunks = [
        ParentChunk(
            f"c{i}", URL, "Rates", f"chunk {i} " + "x" * 5000, score=1.0 - i / 10, authority_rank=6
        )
        for i in range(5)
    ]
    request = build_request(_inputs([], chunks=tuple(chunks)))
    total = sum(len(e.text) for e in request.evidence.values() if e.id.startswith("C"))
    assert total <= MAX_CHUNK_CHARS == 12000
    assert request.evidence["C1"].text.startswith("chunk 0")  # the best-scored chunk first
    assert "chunk 4" not in request.user  # the worst one did not fit


def test_all_sibling_tiers_are_listed_and_the_entry_tier_is_marked() -> None:  # 13.4
    groups = [
        _group(_tier("6.30%", "<=300000")),
        _group(_tier("6.10%", "<=100000")),
        _group(_tier("6.50%", ">300000")),
    ]
    request = build_request(_inputs(groups, default_tier="entry"))
    for value in ("6.30%", "6.10%", "6.50%"):
        assert value in request.user
    entry = [line for line in request.user.splitlines() if "ENTRY TIER" in line]
    assert len(entry) == 1
    assert "6.10%" in entry[0]


def test_numbers_of_every_sibling_are_allowed_but_not_a_number_from_elsewhere() -> None:  # TIER-5
    request = build_request(
        _inputs([_group(_tier("6.10%", "<=100000")), _group(_tier("6.30%", "<=300000"))])
    )
    assert ("%", 6.1) in request.allowed and ("%", 6.3) in request.allowed
    assert ("%", 9.99) not in request.allowed


def test_loser_numbers_reach_the_validators_not_the_model() -> None:
    request = build_request(
        _inputs([_group(_tier("6.10%", "<=100000"))], losers=frozenset({("%", 5.5)}))
    )
    assert ("%", 5.5) in request.losers
    assert "5.5" not in request.user


def test_with_tier_siblings_only_rates_must_come_from_a_fact() -> None:  # TIER-5
    chunk = ParentChunk("c1", URL, "Hours", "Open 9 to 5 on weekdays", score=0.9, authority_rank=6)
    assert build_request(_inputs([], chunks=(chunk,))).chunk_units == {"", "$", "%"}
    one = build_request(_inputs([_group(_tier("6.10%", "<=100000"))], chunks=(chunk,)))
    assert one.chunk_units == {"", "$", "%"}  # a single fact is not a choice between tiers
    siblings = build_request(
        _inputs(
            [_group(_tier("6.10%", "<=100000")), _group(_tier("6.30%", "<=300000"))],
            chunks=(chunk,),
        )
    )
    assert siblings.chunk_units == {"", "$"}


def test_a_dollar_threshold_from_the_page_is_allowed_beside_tier_siblings_but_a_rate_is_not() -> (
    None
):
    chunk = ParentChunk(
        "c1",
        URL,
        "Savings",
        "Earn 0.35% APY on balances of $25,000 or more; also 9.99% promo",
        score=0.9,
        authority_rank=6,
    )
    groups = [_group(_tier("6.10%", "<=100000")), _group(_tier("6.30%", "<=300000"))]
    request = build_request(_inputs(groups, chunks=(chunk,)))
    path, _ = _path(accepted("For balances of $25,000 or more the rate is 6.10%.", "F1", "C1"))
    assert path.answer(request, QUESTION).answer is not None
    path, _ = _path(accepted("The APY is 0.35% on balances of $25,000.", "F1", "C1"))
    outcome = path.answer(request, QUESTION)
    assert outcome.answer is None and "V1" in outcome.failed


def test_numbers_in_the_cited_facts_own_row_and_dates_are_grounded() -> None:  # V1
    version = _tier("4.10%", "<=100000", effective="2026-06-15")
    version = replace(version, fact=replace(version.fact, quote="Cert | 4.10% | $1,000 minimum"))
    request = build_request(_inputs([_group(version)]))
    path, _ = _path(accepted("4.10% APY, $1,000 minimum, effective 2026-06-15.", "F1"))
    assert path.answer(request, QUESTION).answer is not None


def test_numbers_of_a_fact_that_was_not_cited_are_not_grounded() -> None:
    first = _tier("4.10%", "<=100000")
    first = replace(first, fact=replace(first.fact, quote="Cert | 4.10% | $1,000 minimum"))
    request = build_request(_inputs([_group(first), _group(_tier("4.20%", ">100000"))]))
    path, _ = _path(accepted("The APY is 4.20%, $1,000 minimum.", "F2"))  # cites the other fact
    outcome = path.answer(request, QUESTION)
    assert outcome.answer is None and "V1" in outcome.failed


def test_sources_prefer_the_most_authoritative_evidence_and_are_at_most_three() -> None:
    chunks = tuple(
        ParentChunk(f"c{i}", f"{URL}/{i}", "T", f"text {i}", score=1.0 - i / 10, authority_rank=i)
        for i in range(5)
    )
    request = build_request(_inputs([], chunks=chunks))
    path = SlowPath(FakeStructured(accepted("text", "C1", "C2", "C3", "C4", "C5")), "m", 15)
    outcome = path.answer(request, QUESTION)
    assert outcome.answer is not None
    assert len(outcome.answer.sources) == 3
    assert [s.url for s in outcome.answer.sources][0].endswith("/4")  # rank 4 is the highest


# --- the fact-history view (13.3) ------------------------------------------------------------


def _history() -> list[FactVersion]:
    return [
        make_version(value="3.50%", first=1, last=2),
        make_version(value="3.90%", first=3, last=4),
        make_version(value="4.40%", first=5, last=5),
    ]


def test_the_history_lists_each_distinct_value_with_its_dates_oldest_first() -> None:  # HIST-1
    lines = history_view(_history(), SNAPS, target_ordinal=4)
    assert lines[0].startswith("3.50%") and "2026-05-12" in lines[0] and "2026-05-19" in lines[0]
    assert lines[1].startswith("3.90%") and "2026-06-09" in lines[1]


def test_a_value_after_the_target_snapshot_never_appears() -> None:  # HIST-2
    text = "\n".join(history_view(_history(), SNAPS, target_ordinal=4))
    assert "4.40%" not in text
    assert "2026-09-28" not in text


def test_a_page_added_after_the_first_snapshot_is_listed() -> None:  # HIST-3
    versions = [
        make_version(value="3.50%", first=1, last=4),
        make_version(value="3.50%", first=3, last=4, url="https://example.test/new"),
    ]
    text = "\n".join(history_view(versions, SNAPS, target_ordinal=4))
    assert "https://example.test/new" in text
    assert "added" in text


def test_a_page_that_disappeared_is_listed_as_removed() -> None:  # HIST-3
    versions = [
        make_version(value="3.50%", first=1, last=2),
        make_version(value="3.50%", first=1, last=4, url="https://example.test/b"),
    ]
    text = "\n".join(history_view(versions, SNAPS, target_ordinal=4))
    assert "removed" in text


def test_the_history_is_capped_at_40_lines_keeping_the_most_recent() -> None:  # HIST-4
    many = [
        make_version(
            value=f"{i}.00%", first=1 + (i % 5), last=1 + (i % 5), url=f"https://example.test/p{i}"
        )
        for i in range(1, 80)
    ]
    lines = history_view(many, SNAPS, target_ordinal=5)
    assert len(lines) <= MAX_HISTORY_LINES == 40


def test_the_history_reaches_the_model_when_given() -> None:
    lines = history_view(_history(), SNAPS, target_ordinal=4)
    request = build_request(
        _inputs([_group(_tier("6.10%", "<=100000"))], history_lines=tuple(lines))
    )
    assert "3.90%" in request.user and "3.50%" in request.user
    assert ("%", 3.5) in request.allowed  # every history value may be printed
    assert request.relax_stale is True


# --- acceptance (SLOW-3) ---------------------------------------------------------------------


def _request(*, losers: frozenset[tuple[str, float]] = frozenset()) -> Any:  # noqa: ANN401
    return build_request(_inputs([_group(_tier("6.10%", "<=100000"))], losers=losers))


def _path(
    *outcomes: dict[str, Any] | Exception,
    clock: FakeClock | None = None,
    call_seconds: float = 0.0,
    deadline: int = 15,
) -> tuple[SlowPath, FakeStructured]:
    clock = clock or FakeClock()
    client = FakeStructured(*outcomes, clock=clock, call_seconds=call_seconds)
    return SlowPath(client, "slow-model", deadline, clock=clock, sleep=clock.sleep), client


def test_a_valid_response_becomes_a_confident_cited_answer() -> None:
    path, client = _path(accepted("For loans up to $100,000 the mortgage APR is 6.10%.", "F1"))
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is not None
    assert outcome.answer.confident is True
    assert outcome.answer.sources[0].url == URL
    assert outcome.answer.sources[0].quote
    call = client.calls[0]
    assert call["model"] == "slow-model" and call["schema"] == SLOW_SCHEMA
    assert "temperature" not in call


def test_answerable_false_is_a_refusal_reason_not_an_answer() -> None:
    path, _ = _path(
        {"answerable": False, "answer": "", "evidence_ids": [], "loser_values_mentioned": []}
    )
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is None and "not answerable" in outcome.reason


def test_an_unknown_evidence_id_is_rejected() -> None:
    path, _ = _path(accepted("The mortgage APR is 6.10%.", "F1", "F99"))
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is None and "evidence" in outcome.reason


def test_an_answer_without_evidence_is_rejected() -> None:
    path, _ = _path(accepted("The mortgage APR is 6.10%."))
    assert path.answer(_request(), QUESTION).answer is None


def test_a_number_that_is_not_grounded_fails_the_validators() -> None:  # V1
    path, _ = _path(accepted("The mortgage APR is 7.77%.", "F1"))
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is None and "V1" in outcome.failed


def test_a_loser_value_in_the_text_fails_the_validators() -> None:  # V2
    request = _request(losers=frozenset({("%", 5.5)}))
    request.allowed |= {("%", 5.5)}  # even if some page grounded it, it is a known loser
    path, _ = _path(accepted("The APR is 6.10%, up from 5.5%.", "F1"))
    outcome = path.answer(request, QUESTION)
    assert outcome.answer is None and "V2" in outcome.failed


def test_a_listed_loser_mention_is_rejected() -> None:
    response = accepted("The mortgage APR is 6.10%.", "F1") | {"loser_values_mentioned": ["5.5%"]}
    path, _ = _path(response)
    assert path.answer(_request(), QUESTION).answer is None


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"answerable": "yes"},
        {"answerable": True, "answer": 3, "evidence_ids": [], "loser_values_mentioned": []},
    ],
)
def test_malformed_output_is_a_refusal_reason(bad: dict[str, Any]) -> None:
    path, _ = _path(bad)
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is None and "malformed" in outcome.reason


def test_an_output_error_from_the_client_is_a_refusal_reason() -> None:
    path, _ = _path(LlmOutputError("x"))
    assert path.answer(_request(), QUESTION).answer is None


# --- deadline, retry, no key (SLOW-4, SLOW-5) ------------------------------------------------


def test_a_rate_limit_then_success_retries_once_within_the_deadline() -> None:
    path, client = _path(LlmTransientError("HTTP 429", 2.0), accepted("APR 6.10%.", "F1"))
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is not None
    assert len(client.calls) == 2


def test_the_per_call_timeout_is_what_is_left_of_the_deadline() -> None:
    path, client = _path(
        LlmTransientError("HTTP 429", 2.0), accepted("APR 6.10%.", "F1"), call_seconds=1.0
    )
    path.answer(_request(), QUESTION)
    first, second = (c["timeout_s"] for c in client.calls)
    assert first == pytest.approx(15.0)
    assert second == pytest.approx(15.0 - 1.0 - 2.0)


def test_only_one_retry_is_made() -> None:
    path, client = _path(
        LlmTransientError("a"), LlmTransientError("b"), accepted("APR 6.10%.", "F1")
    )
    assert path.answer(_request(), QUESTION).answer is None
    assert len(client.calls) == 2


def test_no_retry_when_half_the_deadline_is_gone() -> None:
    path, client = _path(
        LlmTransientError("timeout"), accepted("APR 6.10%.", "F1"), call_seconds=8.0
    )
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is None and len(client.calls) == 1


def test_a_response_that_arrives_after_the_deadline_is_not_used() -> None:
    path, _ = _path(accepted("APR 6.10%.", "F1"), call_seconds=16.0)
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is None and "deadline" in outcome.reason


def test_a_non_transient_error_is_not_retried() -> None:
    path, client = _path(LlmError("HTTP 400"))
    assert path.answer(_request(), QUESTION).answer is None
    assert len(client.calls) == 1


def test_no_client_means_no_answer_and_a_single_warning(caplog: pytest.LogCaptureFixture) -> None:
    path = SlowPath(None, "m", 15)
    assert path.available is False
    with caplog.at_level(logging.WARNING):
        first = path.answer(_request(), QUESTION)
        path.answer(_request(), QUESTION)
    assert first.answer is None and "no API key" in first.reason
    assert sum("API key" in r.getMessage() for r in caplog.records) == 1


def test_the_configured_effort_is_sent_with_the_call() -> None:
    clock = FakeClock()
    client = FakeStructured(accepted("APR 6.10%.", "F1"), clock=clock)
    path = SlowPath(client, "m", 15, effort="low", clock=clock, sleep=clock.sleep)
    path.answer(_request(), QUESTION)
    assert client.calls[0]["effort"] == "low"


def test_a_chunk_is_labelled_with_its_header_path_and_page_url() -> None:  # SPEC 6.3 (CH-1)
    chunk = ParentChunk(
        "c1",
        URL,
        "Branch news",
        "Lobby Monday-Thursday: 8:30 a.m.-5 p.m.",
        score=0.9,
        authority_rank=6,
        headers=("Branch news", "Hours", "Cedar Falls"),
    )
    user = build_request(_inputs([], chunks=(chunk,))).user
    assert f"[C1] Branch news > Hours > Cedar Falls ({URL})" in user


def test_a_chunk_without_headers_is_labelled_with_the_page_title() -> None:
    chunk = ParentChunk("c1", URL, "Branch news", "text here", score=0.9, authority_rank=6)
    assert f"[C1] Branch news ({URL})" in build_request(_inputs([], chunks=(chunk,))).user


def _text_group() -> CandidateGroup:
    first = make_version(
        value="Drive-up Monday-Friday: 8 a.m.-6 p.m.",
        entity="branch",
        label="Branch",
        attribute="hours",
        url="https://example.test/a",
    )
    second = make_version(
        value="Drive-up Monday-Friday: 7 a.m.-7 p.m.",
        entity="branch",
        label="Branch",
        attribute="hours",
        url="https://example.test/b",
    )
    return CandidateGroup(
        fact_key=first.fact.fact_key,
        versions=(first, second),
        resolution=Resolution("unresolved", None, "unresolved"),
        score=0.5,
        qualifier=1.0,
        entity=1.0,
        rrf=0.1,
    )


def test_differing_text_statements_are_shown_not_blocked_as_a_conflict() -> None:  # decision 27
    request = build_request(_inputs([_text_group()], losers=frozenset({("", 7.0), ("", 8.0)})))
    assert "8 a.m.-6 p.m." in request.user and "7 a.m.-7 p.m." in request.user
    assert "UNRESOLVED CONFLICT" not in request.user
    assert request.confident is True  # conflicts are numeric only
    assert ("", 8.0) in request.allowed and ("", 7.0) not in request.losers


def test_an_unresolved_numeric_conflict_still_blocks_its_values() -> None:
    first = _tier("6.10%", "<=100000")
    second = make_version(
        value="6.40%",
        entity="mortgage",
        label="Mortgage",
        attribute="apr",
        url="https://example.test/other",
        qualifiers={"amount_tier": "<=100000"},
    )
    group = CandidateGroup(
        fact_key=first.fact.fact_key,
        versions=(first, second),
        resolution=Resolution("unresolved", None, "unresolved"),
        score=0.5,
        qualifier=1.0,
        entity=1.0,
        rrf=0.1,
    )
    request = build_request(_inputs([group]))
    assert "6.10%" not in request.user and "6.40%" not in request.user
    assert "UNRESOLVED CONFLICT" in request.user and request.confident is False


def test_only_numeric_facts_make_loser_numbers() -> None:  # V2 is about stale values
    winner = make_version(value="3.90%", attribute="apy")
    old = make_version(value="3.50%", attribute="apy", first=1, last=1)
    text = make_version(value="Routing number 273974581", attribute="routing", none=True)
    losers = loser_numbers([winner, old, text], ["3.90%"])
    assert ("%", 3.5) in losers
    assert ("", 273974581.0) not in losers
    assert ("%", 3.9) not in losers


def test_the_models_stated_reason_for_declining_is_kept_in_the_outcome() -> None:
    declined = {
        "answerable": False,
        "answer": "",
        "evidence_ids": [],
        "loser_values_mentioned": [],
        "reason": "only the Platteville branch hours are given",
    }
    path, _ = _path(declined)
    outcome = path.answer(_request(), QUESTION)
    assert outcome.answer is None
    assert "only the Platteville branch hours are given" in outcome.reason


def test_the_reason_field_is_part_of_the_schema_but_not_required() -> None:
    assert "reason" in SLOW_SCHEMA["properties"]
    assert "reason" not in SLOW_SCHEMA["required"]


def test_the_prompt_does_not_treat_an_old_effective_date_as_stale() -> None:
    system = build_request(_inputs([])).system.lower()
    assert "effective date only says when" in system
    assert "earlier than the snapshot date" in system


def test_the_prompt_allows_a_scoped_answer_and_keeps_refusal_for_no_evidence() -> None:
    system = build_request(_inputs([])).system.lower()
    assert "particular branches, products or programs" in system
    assert "nothing in the evidence addresses the question" in system


def test_a_chunk_label_carries_its_effective_and_expiry_dates() -> None:  # META-2
    chunk = ParentChunk(
        "c1",
        URL,
        "Rates",
        "30-year fixed 7.13%",
        score=0.9,
        authority_rank=6,
        effective_date="2026-09-25",
        expiry_date="2026-12-31",
    )
    user = build_request(_inputs([], chunks=(chunk,))).user
    assert f"[C1] Rates ({URL}) [effective 2026-09-25, expires 2026-12-31]" in user


def test_a_chunk_without_dates_has_a_plain_label() -> None:
    chunk = ParentChunk("c1", URL, "Rates", "text", score=0.9, authority_rank=6)
    assert f"[C1] Rates ({URL})\n" in build_request(_inputs([], chunks=(chunk,))).user


def test_the_prompt_prefers_the_latest_effective_date_and_forbids_older_values() -> None:
    system = build_request(_inputs([])).system.lower()
    assert "latest effective date" in system
    assert "do not mention older values" in system


# --- which retrieved chunks are superseded -----------------------------------------------------


def _by_key(*versions: FactVersion) -> dict[str, list[FactVersion]]:
    grouped: dict[str, list[FactVersion]] = {}
    for version in versions:
        grouped.setdefault(version.fact.fact_key, []).append(version)
    return grouped


def test_a_table_value_that_lost_on_effective_date_is_superseded() -> None:
    old = make_version(
        value="1.11%", url="https://example.test/old", effective="2026-04-22", last=5
    )
    new = make_version(
        value="9.09%", url="https://example.test/new", effective="2026-09-25", last=5
    )
    chunks, numbers = superseded(_by_key(old, new), ordinal=5, snapshots=SNAPS)
    assert chunks == {old.fact.chunk_id}
    assert ("%", 1.11) in numbers and ("%", 9.09) not in numbers


def test_a_value_that_lost_only_on_snapshot_recency_is_not_called_stale() -> None:
    # two unrelated articles that share a key: recency says nothing about which is current
    first = make_version(value="7,260", url="https://example.test/2023", attribute="count", last=5)
    second = make_version(value="6,187", url="https://example.test/2024", attribute="count", last=5)
    chunks, numbers = superseded(_by_key(first, second), ordinal=5, snapshots=SNAPS)
    assert chunks == frozenset() and numbers == frozenset()


def test_a_value_that_lost_on_source_authority_is_superseded() -> None:
    weak = make_version(value="2.00%", url="https://example.test/blog", rank=1, last=5)
    strong = make_version(value="3.00%", url="https://example.test/rates", rank=6, last=5)
    chunks, _ = superseded(_by_key(weak, strong), ordinal=5, snapshots=SNAPS)
    assert chunks == {weak.fact.chunk_id}


def test_an_unresolved_key_and_a_single_version_supersede_nothing() -> None:
    only = make_version(value="3.00%", last=5)
    assert superseded(_by_key(only), ordinal=5, snapshots=SNAPS) == (frozenset(), frozenset())


def test_a_number_that_is_current_for_another_key_is_never_a_loser() -> None:
    old = make_version(
        value="3.00%", url="https://example.test/old", effective="2026-04-22", last=5
    )
    new = make_version(
        value="4.00%", url="https://example.test/new", effective="2026-09-25", last=5
    )
    other = make_version(value="3.00%", attribute="apr", url="https://example.test/o", last=5)
    _, numbers = superseded(_by_key(old, new, other), ordinal=5, snapshots=SNAPS)
    assert ("%", 3.0) not in numbers  # 3.00% is the current APR


# --- the same table in two generations (no facts needed) --------------------------------------


def _table(text: str, effective: str | None, n: int = 0) -> Chunk:
    return Chunk(
        chunk_id=f"t{n}",
        url=f"https://example.test/t{n}",
        file_name="t",
        title="Rates",
        parent_headers=("Rates",),
        chunk_type="table",
        parent_id=None,
        text=text,
        effective_date=effective,
        expiry_date=None,
        source_class="rate_sheet",
        authority_rank=6,
        fetched_at="x",
        position=n,
    )


OLD_TABLE = (
    "Term | Rate | APR\n"
    "Relief Program for qualified Borrowers | 5.125% | 5.196%\n"
    "Purchase: 30-Year Fixed $300,000 or less | 6.000% | 6.061%\n"
    "Purchase: 30-Year Fixed $200,000 or less | 5.875% | 5.954%\n"
    "Purchase: 20-Year Fixed | 5.875% | 6.005%\n"
    "Purchase: 15-Year Fixed | 5.500% | 5.623%\n"
    "Rates are effective as of 04/22/2026."
)
NEW_TABLE = (
    "Program | Rate | APR\n"
    "Purchase: 30-Year Fixed $300,000 or less | 7.25% | 7.32%\n"
    "Purchase: 30-Year Fixed $200,000 or less | 7.25% | 7.34%\n"
    "Purchase: 20-Year Fixed | 7.13% | 7.26%\n"
    "Purchase: 15-Year Fixed | 6.75% | 6.88%\n"
    "Rates are effective as of 09/25/2026."
)


def test_an_older_generation_of_the_same_table_is_stale() -> None:
    old, new = _table(OLD_TABLE, "2026-04-22", 1), _table(NEW_TABLE, "2026-09-25", 2)
    chunks, numbers = stale_tables([old, new])
    assert chunks == {"t1"}
    assert ("%", 6.0) in numbers and ("%", 5.875) in numbers
    assert ("%", 7.25) not in numbers  # the current values are never losers


def test_tables_with_different_rows_or_the_same_date_are_not_generations() -> None:
    other = (
        "Account | APY\nShare Savings balance band one | 0.10%\nHoliday Club band | 0.20%\n"
        "Rates effective as of 09/25/2026."
    )
    new = _table(NEW_TABLE, "2026-09-25", 2)
    assert (
        stale_tables([_table(OLD_TABLE, "2026-04-22", 1), _table(other, "2026-09-25", 3)])[0]
        == frozenset()
    )
    assert stale_tables([_table(OLD_TABLE, "2026-09-25", 1), new])[0] == frozenset()
    assert stale_tables([_table(OLD_TABLE, None, 1), new])[0] == frozenset()


def test_a_table_needs_three_shared_row_labels_to_count_as_a_generation() -> None:
    small_old = (
        "A | B\nRow one label | 1.00%\nRow two label | 2.00%\nRates effective as of 04/22/2026."
    )
    small_new = (
        "A | B\nRow one label | 3.00%\nRow two label | 4.00%\nRates effective as of 09/25/2026."
    )
    assert (
        stale_tables([_table(small_old, "2026-04-22", 1), _table(small_new, "2026-09-25", 2)])[0]
        == frozenset()
    )


def test_generations_must_share_their_columns_and_section_heading() -> None:
    old = _table(OLD_TABLE, "2026-04-22", 1)
    other_columns = NEW_TABLE.replace("Program | Rate | APR", "Program | Dividend | Yield")
    assert stale_tables([old, _table(other_columns, "2026-09-25", 2)])[0] == frozenset()
    elsewhere = replace(_table(NEW_TABLE, "2026-09-25", 3), parent_headers=("Rates", "Business"))
    assert stale_tables([old, elsewhere])[0] == frozenset()
    assert stale_tables([old, _table(NEW_TABLE, "2026-09-25", 2)])[0] == {"t1"}


# --- publication date of a news page (SRET-5) -----------------------------------------------


def test_a_date_alone_on_a_line_near_the_top_is_the_publication_date() -> None:
    texts = ["2 min read", "[Writer](https://x.test/a)", "June 20, 2025", "# Title"]
    assert published_date(texts) == "2025-06-20"
    assert published_date(["Writer (https://x.test/a) June 20, 2025"]) == "2025-06-20"


def test_a_date_inside_a_sentence_or_far_down_the_page_is_not_a_publication_date() -> None:
    assert published_date(["The branch closes on March 31, 2026."]) is None
    assert published_date(["intro"] * 10 + ["June 20, 2025"]) is None
    assert published_date([]) is None
    assert published_date(["February 30, 2025"]) is None


def test_the_label_shows_the_publication_date_and_the_prompt_prefers_the_newest() -> None:
    chunk = ParentChunk(
        "c1",
        URL,
        "News",
        "Example has 22 locations",
        score=0.9,
        authority_rank=2,
        published="2026-05-05",
    )
    request = build_request(_inputs([], chunks=(chunk,)))
    assert "published 2026-05-05" in request.user
    assert "publication date" in SYSTEM_PROMPT


# --- losers scoped to the key they belong to (V2) -------------------------------------------


def _ids(version: FactVersion) -> frozenset[str]:
    return frozenset({version.fact.chunk_id})


def test_a_superseded_number_is_scoped_to_the_chunks_that_replaced_it() -> None:
    old = make_version(
        value="1.11%", url="https://example.test/old", effective="2026-04-22", last=5
    )
    new = make_version(
        value="9.09%", url="https://example.test/new", effective="2026-09-25", last=5
    )
    chunks, numbers, scopes = superseded_detail(_by_key(old, new), ordinal=5, snapshots=SNAPS)
    assert (chunks, numbers) == superseded(_by_key(old, new), ordinal=5, snapshots=SNAPS)
    assert scopes == {("%", 1.11): _ids(new)}


def test_a_stale_table_number_is_scoped_to_the_table_that_replaced_it() -> None:
    old, new = _table(OLD_TABLE, "2026-04-22", 1), _table(NEW_TABLE, "2026-09-25", 2)
    chunks, numbers, scopes = stale_tables_detail([old, new])
    assert (chunks, numbers) == stale_tables([old, new])
    assert scopes[("%", 6.0)] == {"t2"} and set(scopes) == set(numbers)


def test_numeric_losers_of_a_group_are_scoped_to_its_winner_or_to_every_version() -> None:
    old = make_version(
        value="1.11%", url="https://example.test/old", effective="2026-04-22", last=5
    )
    new = make_version(
        value="9.09%", url="https://example.test/new", effective="2026-09-25", last=5
    )
    resolved = _group(new)
    assert loser_scopes([resolved], [old, new]) == {("%", 1.11): _ids(new)}
    unresolved = _group(new, unresolved=True)
    both = _ids(old) | _ids(new)  # nothing won, so every value is a loser of every version
    assert loser_scopes([unresolved], [old, new]) == {("%", 1.11): both, ("%", 9.09): both}


def _scoped_request(scope: frozenset[str]) -> Any:  # noqa: ANN401
    winner = _tier("6.10%", "<=100000")
    chunk = ParentChunk(
        "c1", URL, "Cards", "Our card pays 5.5% back on fuel.", 0.9, 6, member_ids=("c1",)
    )
    return build_request(
        _inputs(
            [_group(winner)],
            chunks=(chunk,),
            losers=frozenset({("%", 5.5)}),
            loser_scopes={("%", 5.5): scope},
        )
    )


def test_a_loser_printed_from_unrelated_cited_evidence_is_accepted() -> None:
    request = _scoped_request(frozenset({"somewhere-else"}))
    path, _ = _path(
        accepted("The mortgage APR is 6.10%. Our card pays 5.5% back on fuel.", "F1", "C1")
    )
    assert path.answer(request, QUESTION).answer is not None


def test_the_same_loser_is_rejected_when_the_answer_cites_what_replaced_it() -> None:
    winner_id = _tier("6.10%", "<=100000").fact.chunk_id
    request = _scoped_request(frozenset({winner_id}))
    path, _ = _path(
        accepted("The mortgage APR is 6.10%. Our card pays 5.5% back on fuel.", "F1", "C1")
    )
    outcome = path.answer(request, QUESTION)
    assert outcome.answer is None and "V2" in outcome.failed


def test_the_prompt_asks_for_a_scoped_answer_instead_of_a_refusal_for_a_general_question() -> None:
    assert "do not refuse" in SYSTEM_PROMPT
    assert "name the branch, product or program" in SYSTEM_PROMPT
    assert "can vary" in SYSTEM_PROMPT


def test_a_date_in_a_chunk_label_may_be_printed_by_an_answer_that_cites_the_chunk() -> None:
    chunk = ParentChunk(
        "c1", URL, "News", "Lobby open 9 to 5.", score=0.9, authority_rank=2, published="2024-04-09"
    )
    request = build_request(_inputs([], chunks=(chunk,)))
    path, _ = _path(accepted("Lobby hours (listed April 2024): 9 to 5.", "C1"))
    assert path.answer(request, QUESTION).answer is not None
    other, _ = _path(accepted("Lobby hours (listed April 2031): 9 to 5.", "C1"))
    assert other.answer(request, QUESTION).answer is None


def test_a_digit_may_repeat_a_number_the_cited_page_spells_out() -> None:
    chunk = ParentChunk("c1", URL, "Tips", "It takes eight years to break even.", 0.9, 2)
    request = build_request(_inputs([], chunks=(chunk,)))
    path, _ = _path(accepted("It takes 8 years to break even.", "C1"))
    assert path.answer(request, QUESTION).answer is not None
    other, _ = _path(accepted("It takes 7 years to break even.", "C1"))
    assert other.answer(request, QUESTION).answer is None


def test_list_numbering_in_an_answer_needs_no_grounding() -> None:
    chunk = ParentChunk("c1", URL, "Wires", "Give the beneficiary name and ABA number.", 0.9, 2)
    request = build_request(_inputs([], chunks=(chunk,)))
    path, _ = _path(accepted("(1) The beneficiary name. (2) The ABA number.", "C1"))
    assert path.answer(request, QUESTION).answer is not None
