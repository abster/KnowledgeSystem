"""Structured JSON logging to a file under the work directory (SPEC section 18).

Nothing is written to stdout or stderr. The API key is scrubbed from every record.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from kk.llm.key import load_key

_ROOT = "kk"
_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {"level": record.levelname, "logger": record.name}
        data["msg"] = record.getMessage()
        data.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        text = json.dumps(data, default=str, sort_keys=True)
        key = load_key()
        if key is not None:
            text = text.replace(key.reveal(), "[REDACTED]")
        return text


def setup_logging(work_dir: Path) -> None:
    """Send ``kk`` log records to ``<work_dir>/kk.log`` as JSON lines.

    Args:
        work_dir: Directory for working files (``KK_WORK_DIR``).
    """
    logger = logging.getLogger(_ROOT)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    work_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(work_dir / "kk.log", encoding="utf-8")
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child of the ``kk`` logger."""
    return logging.getLogger(f"{_ROOT}.{name}")


def work_dir_from_env() -> Path:
    """Return ``KK_WORK_DIR`` if set, else a fresh temporary directory."""
    configured = os.environ.get("KK_WORK_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.mkdtemp(prefix="kk-"))
