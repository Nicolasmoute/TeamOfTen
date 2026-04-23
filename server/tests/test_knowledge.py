"""Tests for server/knowledge.py — the durable artifact bucket.

Mirror of test_context.py: isolate KNOWLEDGE_DIR to a tempdir per test,
leave kDrive disabled so only the local-cache path is exercised.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import server.knowledge as knowmod


@pytest.fixture
def tmp_know(monkeypatch: pytest.MonkeyPatch) -> Path:
    d = Path(tempfile.mkdtemp(prefix="harness-know-"))
    monkeypatch.setattr(knowmod, "KNOWLEDGE_DIR", d)
    return d


# ---------- validate ----------


def test_validate_rejects_empty() -> None:
    assert knowmod.validate("") is not None


def test_validate_rejects_traversal() -> None:
    for bad in (
        "../etc/passwd.md",
        "foo/../../escape.md",
        "./relative.md",
        "foo/./bar.md",
    ):
        assert knowmod.validate(bad) is not None, bad


def test_validate_rejects_bad_extension() -> None:
    for bad in ("foo.py", "foo", "foo.json", "reports/weekly"):
        assert knowmod.validate(bad) is not None, bad


def test_validate_rejects_too_deep() -> None:
    assert knowmod.validate("a/b/c/d/e.md") is not None  # 5 segments > 4


def test_validate_accepts_reasonable_paths() -> None:
    for good in (
        "notes.md",
        "reports/2026-04-23.md",
        "research/arch/v2.md",
        "a/b/c/d.md",
    ):
        assert knowmod.validate(good) is None, good


def test_validate_rejects_bad_segment_start() -> None:
    assert knowmod.validate("-leading-dash/foo.md") is not None
    assert knowmod.validate("foo/-bad/baz.md") is not None


# ---------- write / read / delete ----------


async def test_write_then_read_roundtrip(tmp_know: Path) -> None:
    await knowmod.write("reports/weekly.md", "# Weekly\n\nhi\n")
    assert (tmp_know / "reports" / "weekly.md").read_text() == "# Weekly\n\nhi\n"
    assert await knowmod.read("reports/weekly.md") == "# Weekly\n\nhi\n"


async def test_write_rejects_empty_body(tmp_know: Path) -> None:
    with pytest.raises(ValueError):
        await knowmod.write("x.md", "")
    with pytest.raises(ValueError):
        await knowmod.write("x.md", "   \n  ")


async def test_write_rejects_oversize_body(tmp_know: Path) -> None:
    huge = "x" * (knowmod.MAX_BODY_CHARS + 1)
    with pytest.raises(ValueError):
        await knowmod.write("x.md", huge)


async def test_write_rejects_bad_path(tmp_know: Path) -> None:
    with pytest.raises(ValueError):
        await knowmod.write("../escape.md", "body")


async def test_write_creates_intermediate_dirs(tmp_know: Path) -> None:
    await knowmod.write("deep/nested/path/file.md", "body")
    assert (tmp_know / "deep" / "nested" / "path" / "file.md").exists()


async def test_read_missing_returns_none(tmp_know: Path) -> None:
    assert await knowmod.read("does/not/exist.md") is None


# ---------- list_paths ----------


async def test_list_paths_empty_when_dir_missing(tmp_know: Path) -> None:
    # Fresh tempdir exists but is empty — list_paths should return [].
    assert knowmod.list_paths() == []


async def test_list_paths_sorted_posix(tmp_know: Path) -> None:
    await knowmod.write("b.md", "b")
    await knowmod.write("a/nested.md", "n")
    await knowmod.write("a.md", "a")
    paths = knowmod.list_paths()
    assert paths == ["a.md", "a/nested.md", "b.md"]


async def test_list_paths_ignores_non_md(tmp_know: Path) -> None:
    await knowmod.write("kept.md", "md")
    await knowmod.write("also.txt", "txt")
    # Write a non-allowed file directly to disk (simulating an agent
    # dropping an artifact the normal tool wouldn't accept).
    (tmp_know / "binary.dat").write_bytes(b"junk")
    paths = knowmod.list_paths()
    assert "kept.md" in paths
    assert "also.txt" in paths
    assert "binary.dat" not in paths
