"""Live check against the real API (run manually: ``pytest -m live``). Costs a few calls."""

from __future__ import annotations

import pytest

from kk.config import load_config
from kk.ingest.extract_cache import ExtractionCache
from kk.ingest.prose import ProseExtractor
from kk.llm.client import make_client
from tests.unit.test_prose import make_chunk

pytestmark = pytest.mark.live

TEXTS = [
    "The 30-year fixed mortgage rate is 7.13% for loans of $100,000 or less.",
    "Our lobby is open Monday to Friday from 9:00 a.m. to 5:00 p.m.",
    "There is a $25 fee to replace a lost debit card.",
    "Welcome to our credit union.",
]


def test_a_handful_of_synthetic_chunks_produce_valid_facts(
    tmp_path: pytest.TempPathFactory,
) -> None:
    client = make_client()
    if client is None:
        pytest.skip("no API key")
    config = load_config()
    extractor = ProseExtractor(
        client=client,
        cache=ExtractionCache(tmp_path / "live"),
        model=config.llm.extract_model,
        concurrency=config.llm.extract_concurrency,
    )
    result = extractor.extract([make_chunk(t, chunk_id=f"c{i}") for i, t in enumerate(TEXTS)])
    assert result.stats.failures == 0
    assert result.facts, "the model returned no valid fact for any chunk"
    assert all(f.quote and f.origin == "prose_llm" for f in result.facts)
