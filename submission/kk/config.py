"""Load and validate ``config.json`` (SPEC section 3).

The file is the only place tunable numbers live. Unknown keys and wrong types are errors.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
_WEIGHT_TOLERANCE = 1e-9


class ConfigError(Exception):
    """The configuration file is missing, malformed or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Provenance(_Section):
    """Who or what set a group of values, and on which dataset."""

    set_by: str
    dataset: str


class Retrieval(_Section):
    """Hybrid retrieval sizes."""

    top_k: int = Field(gt=0)
    per_index_k: int = Field(gt=0)
    rrf_k: int = Field(gt=0)


class Weights(_Section):
    """Confidence signal weights."""

    similarity: float = Field(ge=0, le=1)
    margin: float = Field(ge=0, le=1)
    qualifier: float = Field(ge=0, le=1)
    entity: float = Field(ge=0, le=1)


class ResolutionState(_Section):
    """Confidence signal per conflict resolution state."""

    unique: float = Field(ge=0, le=1)
    strategy_1_2: float = Field(ge=0, le=1)
    strategy_3: float = Field(ge=0, le=1)
    unresolved: float = Field(ge=0, le=1)


class Confidence(_Section):
    """Confidence score parameters."""

    weights: Weights
    tau_floor: float = Field(ge=0, le=1)
    tau_accept: float = Field(ge=0, le=1)
    resolution_state: ResolutionState

    @model_validator(mode="after")
    def _check(self) -> Confidence:
        total = sum(self.weights.model_dump().values())
        if abs(total - 1.0) > _WEIGHT_TOLERANCE:
            msg = f"confidence.weights must sum to 1, got {total}"
            raise ValueError(msg)
        if self.tau_floor >= self.tau_accept:
            msg = "confidence.tau_floor must be below confidence.tau_accept"
            raise ValueError(msg)
        return self


class Tier(_Section):
    """Tier selector defaults."""

    default_by_attribute: dict[str, str]


class Llm(_Section):
    """Model names and limits."""

    slow_model: str = Field(min_length=1)
    extract_model: str = Field(min_length=1)
    slow_deadline_s: int = Field(gt=0)
    extract_concurrency: int = Field(gt=0)


class Flags(_Section):
    """Optional behaviours."""

    polish: bool
    llm_crosscheck: bool
    ingest_llm_verify: bool


class Chunk(_Section):
    """Chunking limits."""

    max_tokens: int = Field(gt=0)


class Embed(_Section):
    """Embedding batch limit: a batch holds at most this many characters (see EMB-2)."""

    max_batch_chars: int = Field(gt=0)


class CacheCfg(_Section):
    """Answer-cache size."""

    max_entries: int = Field(gt=0)


class Config(_Section):
    """The whole configuration."""

    provenance: dict[str, Provenance]
    retrieval: Retrieval
    confidence: Confidence
    tier: Tier
    llm: Llm
    flags: Flags
    chunk: Chunk
    embed: Embed
    cache: CacheCfg


def load_config(path: Path | None = None) -> Config:
    """Read and validate the configuration file.

    Args:
        path: File to read; defaults to the shipped ``submission/config.json``.

    Returns:
        The validated, read-only configuration.

    Raises:
        ConfigError: If the file is missing, not JSON, has unknown keys or bad values.
    """
    target = path or DEFAULT_CONFIG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return Config.model_validate(raw)
    except (OSError, ValueError, ValidationError) as exc:
        # ValidationError subclasses ValueError; the message never contains secrets.
        msg = f"invalid configuration {target.name}: {exc}"
        raise ConfigError(msg) from exc
