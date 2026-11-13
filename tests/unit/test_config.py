from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kk.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config


def _shipped() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(DEFAULT_CONFIG_PATH.read_text())
    return data


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


def test_shipped_config_is_valid_and_matches_spec_3() -> None:
    cfg = load_config()
    assert isinstance(cfg, Config)
    assert cfg.retrieval.top_k == 20
    assert cfg.retrieval.per_index_k == 40
    assert cfg.retrieval.rrf_k == 60
    assert cfg.confidence.tau_floor == 0.30
    assert cfg.confidence.tau_accept == 0.80
    assert cfg.llm.slow_model == "claude-sonnet-5-5"
    assert cfg.llm.extract_model == "claude-haiku-5-5"
    assert cfg.llm.slow_deadline_s == 15
    assert cfg.llm.extract_concurrency == 8
    assert cfg.flags.polish is False
    assert cfg.flags.llm_crosscheck is False
    assert cfg.flags.ingest_llm_verify is True
    assert cfg.chunk.max_tokens == 1200
    assert cfg.embed.max_batch_chars == 8000
    assert cfg.cache.max_entries == 10000
    assert cfg.tier.default_by_attribute == {"*": "entry"}


def test_unknown_top_level_key_is_error(tmp_path: Path) -> None:
    data = _shipped()
    data["retreival"] = {"top_k": 1}
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_unknown_nested_key_is_error(tmp_path: Path) -> None:
    data = _shipped()
    data["retrieval"]["top_kk"] = 5
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_bad_type_is_error(tmp_path: Path) -> None:
    data = _shipped()
    data["retrieval"]["top_k"] = "20"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_floor_must_be_below_accept(tmp_path: Path) -> None:
    data = _shipped()
    data["confidence"]["tau_floor"] = 0.9
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_weights_must_sum_to_one(tmp_path: Path) -> None:
    data = _shipped()
    data["confidence"]["weights"]["entity"] = 0.9
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, data))


def test_missing_and_malformed_files_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_config_is_read_only() -> None:
    cfg = load_config()
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic frozen error
        cfg.retrieval.top_k = 1  # type: ignore[misc]


def test_embed_batch_budget_must_be_positive(tmp_path: Path) -> None:
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text())
    raw["embed"]["max_batch_chars"] = 0
    bad = tmp_path / "config.json"
    bad.write_text(json.dumps(raw))
    with pytest.raises(ConfigError):
        load_config(bad)
