"""Load the Anthropic API key without ever exposing it for logging.

The key is read from ``ANTHROPIC_API_KEY`` or from a ``.env`` file. It is wrapped in
:class:`ApiKey`, whose ``repr``, ``str`` and ``format`` are redacted; only
:meth:`ApiKey.reveal` returns the value, and only the SDK call should use it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NoReturn

ENV_VAR = "ANTHROPIC_API_KEY"
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
_REDACTED = "ApiKey(<redacted>)"


class KeyUnavailableError(Exception):
    """No usable API key is configured."""

    def __init__(self) -> None:
        """Create the error with a fixed, secret-free message."""
        super().__init__("no Anthropic API key is configured")


class ApiKey:
    """Opaque holder for the API key."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Wrap ``value``."""
        self._value = value

    def reveal(self) -> str:
        """Return the raw key. Use only to build the SDK client."""
        return self._value

    def __repr__(self) -> str:
        """Return a redacted representation."""
        return _REDACTED

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        """Return a redacted representation whatever the format spec."""
        return format(_REDACTED, format_spec)

    def __reduce__(self) -> NoReturn:
        """Refuse pickling so the key cannot be serialised by accident."""
        msg = "ApiKey cannot be pickled"
        raise TypeError(msg)


def _from_env_file(path: Path) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == ENV_VAR:
            return value.strip().strip("'\"")
    return None


def load_key(env_file: Path | None = None) -> ApiKey | None:
    """Return the key if one is configured, else ``None``.

    The environment variable wins over the ``.env`` file. A blank value counts as absent.
    """
    value = os.environ.get(ENV_VAR) or _from_env_file(env_file or DEFAULT_ENV_FILE)
    if value is None or not value.strip():
        return None
    return ApiKey(value.strip())


def has_key(env_file: Path | None = None) -> bool:
    """Return whether a key is configured, without exposing it."""
    return load_key(env_file) is not None


def require_key(env_file: Path | None = None) -> ApiKey:
    """Return the key or raise :class:`KeyUnavailableError`."""
    key = load_key(env_file)
    if key is None:
        raise KeyUnavailableError
    return key
