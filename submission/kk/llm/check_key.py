"""``make check-key``: one tiny call to the extraction model; prints only PASS or FAIL."""

from __future__ import annotations

from pathlib import Path

import anthropic

from kk.config import load_config
from kk.llm.key import load_key

_MAX_TOKENS = 5


def _tiny_call(key: str, model: str) -> None:
    client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=30.0)
    client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": "Reply with OK."}],
    )


def main(env_file: Path | None = None) -> int:
    """Run the check and return the process exit code.

    Only the outcome and the exception class name are printed; never the key or the message.
    """
    key = load_key(env_file)
    if key is None:
        print("check-key: FAIL (no key)")  # noqa: T201
        return 1
    try:
        _tiny_call(key.reveal(), load_config().llm.extract_model)
    except Exception as exc:  # noqa: BLE001 - any failure is reported as FAIL
        print(f"check-key: FAIL ({type(exc).__name__})")  # noqa: T201
        return 1
    print("check-key: PASS")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
