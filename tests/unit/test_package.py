"""``make package``: the hand-in folder is complete and clean (Step 10, SPEC 2)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from tools.package import PackageError, package


def _tree(root: Path, *, models: bool = True, cache: bool = True) -> Path:
    src = root / "submission"
    (src / "kk" / "query").mkdir(parents=True)
    (src / "kk" / "__init__.py").write_text("")
    (src / "kk" / "query" / "slow.py").write_text("x = 1\n")
    (src / "kk" / "__pycache__").mkdir()
    (src / "kk" / "__pycache__" / "a.pyc").write_bytes(b"\0")
    for name in ("solution.py", "config.json", "requirements.txt", "NOTES.md"):
        (src / name).write_text("x")
    if models:
        (src / "models" / "bge").mkdir(parents=True)
        (src / "models" / "bge" / "model.onnx").write_bytes(b"w")
    if cache:
        (src / "cache" / "extraction").mkdir(parents=True)
        (src / "cache" / "extraction" / "ab.json").write_text("{}")
    (src / "kk_submission.egg-info").mkdir()
    (src / "kk_submission.egg-info" / "PKG-INFO").write_text("x")
    (src / ".env").write_text("SECRET")
    return src


def test_the_package_holds_the_code_weights_and_cache(tmp_path: Path) -> None:
    out = tmp_path / "out"
    report = package(_tree(tmp_path), out, make_zip=False)
    assert (out / "solution.py").is_file()
    assert (out / "kk" / "query" / "slow.py").is_file()
    assert (out / "models" / "bge" / "model.onnx").is_file()
    assert (out / "cache" / "extraction" / "ab.json").is_file()
    assert report.files > 0


def test_caches_bytecode_egg_info_and_secrets_are_left_out(tmp_path: Path) -> None:
    out = tmp_path / "out"
    package(_tree(tmp_path), out, make_zip=False)
    names = {p.name for p in out.rglob("*")}
    assert "__pycache__" not in names and "a.pyc" not in names
    assert "kk_submission.egg-info" not in names
    assert ".env" not in names


@pytest.mark.parametrize("missing", ["models", "cache"])
def test_missing_weights_or_cache_fail_loudly(tmp_path: Path, missing: str) -> None:
    src = _tree(tmp_path, models=missing != "models", cache=missing != "cache")
    with pytest.raises(PackageError) as info:
        package(src, tmp_path / "out", make_zip=False)
    assert missing in str(info.value)


def test_a_zip_is_written_next_to_the_folder_when_asked(tmp_path: Path) -> None:
    out = tmp_path / "out"
    package(_tree(tmp_path), out, make_zip=True)
    archive = tmp_path / "out.zip"
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
    assert "out/solution.py" in names
    assert not any(n.endswith(".env") for n in names)


def test_a_second_run_replaces_the_first(tmp_path: Path) -> None:
    out = tmp_path / "out"
    src = _tree(tmp_path)
    package(src, out, make_zip=False)
    (out / "stale.txt").write_text("old")
    package(src, out, make_zip=False)
    assert not (out / "stale.txt").exists()


def test_the_output_may_not_be_inside_the_source(tmp_path: Path) -> None:
    src = _tree(tmp_path)
    with pytest.raises(PackageError):
        package(src, src / "out", make_zip=False)
