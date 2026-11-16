"""Developer command: assemble the hand-in folder (SPEC 2). Run it with ``make package``.

Copies ``submission/`` to ``dist/submission/`` without bytecode, egg-info, secrets or working
files, checks that the embedding weights and the extraction cache are present (they are kept
out of git but must ship), and writes ``dist/submission.zip`` next to it.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "solution.py",
    "config.json",
    "requirements.txt",
    "NOTES.md",
    "kk",
    "models",
    "cache/extraction",
)
_SKIP_DIR_SUFFIXES = (".egg-info", ".lance")
_SKIP_DIRS = {"__pycache__", ".kk_cache", ".mypy_cache", ".pytest_cache"}
_SKIP_FILES = {".env", ".DS_Store", ".coverage"}
_SKIP_FILE_SUFFIXES = (".pyc",)


class PackageError(Exception):
    """The hand-in cannot be assembled."""


@dataclass(frozen=True)
class PackageReport:
    """What was copied."""

    files: int
    bytes: int


def _skipped(path: Path) -> bool:
    parts = path.parts
    return (
        any(p in _SKIP_DIRS or p.endswith(_SKIP_DIR_SUFFIXES) for p in parts)
        or path.name in _SKIP_FILES
        or path.name.endswith(_SKIP_FILE_SUFFIXES)
    )


def package(src: Path, out: Path, *, make_zip: bool = True) -> PackageReport:
    """Copy ``src`` to ``out`` (replacing it) and optionally zip it to ``out.zip``."""
    src, out = src.resolve(), out.resolve()
    if out == src or src in out.parents:
        raise PackageError("the output folder must not be inside the source folder")
    missing = [name for name in REQUIRED if not (src / name).exists()]
    if missing:
        raise PackageError("missing from the submission: " + ", ".join(missing))
    if out.exists():
        shutil.rmtree(out)
    files = size = 0
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if _skipped(rel) or not path.is_file():
            continue
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        files += 1
        size += path.stat().st_size
    if make_zip:
        archive = out.with_suffix(".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(out.rglob("*")):
                if path.is_file():
                    zf.write(path, Path(out.name) / path.relative_to(out))
    return PackageReport(files, size)


def main(argv: list[str] | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=ROOT / "submission")
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "submission")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = package(args.src, args.out, make_zip=not args.no_zip)
    except PackageError as exc:
        sys.stderr.write(f"package failed: {exc}\n")
        return 1
    sys.stdout.write(f"packaged {report.files} files ({report.bytes / 1e6:.1f} MB) to {args.out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
