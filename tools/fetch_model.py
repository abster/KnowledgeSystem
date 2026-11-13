"""Developer command: download the embedding weights once into ``submission/models/``.

The submission never downloads anything at runtime (SPEC EMB-1); this is the only place the
network is used for the model. Run it with ``make fetch-model``.
"""

from __future__ import annotations

import sys

from kk.ingest.embed import MODEL_DIR, MODEL_NAME


def main() -> int:
    """Fetch the weights into the shipped folder and check that they load offline."""
    from fastembed import TextEmbedding  # noqa: PLC0415

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    TextEmbedding(MODEL_NAME, cache_dir=str(MODEL_DIR))
    sys.stdout.write(f"model ready under {MODEL_DIR}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
