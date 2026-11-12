"""Tests for the alias builder (SPEC 6.9, AL-1, AL-2)."""

from __future__ import annotations

from kk.ingest.aliases import build_aliases, canonical_id


def test_canonical_id_lowercases_and_strips_punctuation() -> None:
    assert canonical_id("Business 1st Rate Checking") == "business 1st rate checking"
    assert canonical_id("Roth: 17-Month  Fixed!") == "roth 17 month fixed"
    assert canonical_id("Member's Choice®") == "members choice"


def test_label_and_stop_word_free_form_are_aliases() -> None:
    index = build_aliases(["The Share Savings of Members"])
    assert index.resolve("share savings members") == "the share savings of members"
    assert index.resolve("The Share Savings of Members") == "the share savings of members"


def test_singular_and_plural_pair() -> None:
    index = build_aliases(["Share Certificates"])
    assert index.resolve("share certificate") == "share certificates"


def test_known_abbreviations() -> None:
    index = build_aliases(["17 Month Share Certificate"])
    assert index.resolve("17 month cd") == "17 month share certificate"


def test_unknown_text_does_not_resolve() -> None:
    assert build_aliases(["Share Savings"]).resolve("auto loans") is None


def test_ambiguous_alias_is_recorded_and_not_a_unique_match() -> None:
    index = build_aliases(["Gold Plan for Members", "Gold Plan of Members"])
    assert "gold plan members" in index.ambiguous
    assert index.aliases["gold plan members"] == frozenset(
        {"gold plan for members", "gold plan of members"}
    )
    assert index.resolve("gold plan members") is None


def test_exact_entity_id_beats_an_ambiguous_alias() -> None:
    index = build_aliases(["Share Certificate", "Share Certificates"])
    assert "share certificate" in index.ambiguous
    assert index.resolve("Share Certificate") == "share certificate"
    assert index.resolve("Share Certificates") == "share certificates"


def test_empty_and_blank_labels_are_ignored() -> None:
    index = build_aliases(["", "   ", "Share Savings"])
    assert index.resolve("") is None
    assert index.resolve("share savings") == "share savings"
