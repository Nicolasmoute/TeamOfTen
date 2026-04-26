"""Tests for server/knowledge.py — the durable artifact bucket.

projects_v2 update: knowledge is project-scoped (active project's
`working/knowledge/`) and `list_paths` is async. Tests use the
`fresh_db` fixture so `resolve_active_project()` returns 'misc' and
the per-project tree exists under the sandboxed DATA_ROOT.
"""

from __future__ import annotations

import pytest

import server.knowledge as knowmod
from server.db import init_db
from server.paths import project_paths


# ---------- validate (sync, no DB needed) ----------


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


# ---------- write / read / list_paths (DB-backed) ----------


async def test_write_routes_to_active_project_knowledge(fresh_db) -> None:
    """projects_v2: writes land at /data/projects/<active>/working/knowledge/."""
    await init_db()
    await knowmod.write("reports/weekly.md", "# Weekly\n\nhi\n")
    pp = project_paths("misc")
    target = pp.knowledge / "reports" / "weekly.md"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "# Weekly\n\nhi\n"
    # And the active project's knowledge dir really IS under working/.
    assert pp.knowledge == pp.working / "knowledge"


async def test_read_returns_what_was_written(fresh_db) -> None:
    await init_db()
    await knowmod.write("notes.md", "body")
    assert await knowmod.read("notes.md") == "body"


async def test_write_rejects_empty_body(fresh_db) -> None:
    await init_db()
    with pytest.raises(ValueError):
        await knowmod.write("x.md", "")
    with pytest.raises(ValueError):
        await knowmod.write("x.md", "   \n  ")


async def test_write_rejects_oversize_body(fresh_db) -> None:
    await init_db()
    huge = "x" * (knowmod.MAX_BODY_CHARS + 1)
    with pytest.raises(ValueError):
        await knowmod.write("x.md", huge)


async def test_write_rejects_bad_path(fresh_db) -> None:
    await init_db()
    with pytest.raises(ValueError):
        await knowmod.write("../escape.md", "body")


async def test_write_creates_intermediate_dirs(fresh_db) -> None:
    await init_db()
    await knowmod.write("deep/nested/path/file.md", "body")
    pp = project_paths("misc")
    assert (pp.knowledge / "deep" / "nested" / "path" / "file.md").exists()


async def test_read_missing_returns_none(fresh_db) -> None:
    await init_db()
    assert await knowmod.read("does/not/exist.md") is None


async def test_list_paths_empty_when_dir_missing(fresh_db) -> None:
    await init_db()
    assert await knowmod.list_paths() == []


async def test_list_paths_sorted_posix(fresh_db) -> None:
    await init_db()
    await knowmod.write("b.md", "b")
    await knowmod.write("a/nested.md", "n")
    await knowmod.write("a.md", "a")
    paths = await knowmod.list_paths()
    assert paths == ["a.md", "a/nested.md", "b.md"]


async def test_list_paths_ignores_non_md(fresh_db) -> None:
    await init_db()
    await knowmod.write("kept.md", "md")
    await knowmod.write("also.txt", "txt")
    # Write a non-allowed file directly to disk (simulating an agent
    # dropping an artifact the normal tool wouldn't accept).
    pp = project_paths("misc")
    (pp.knowledge / "binary.dat").write_bytes(b"junk")
    paths = await knowmod.list_paths()
    assert "kept.md" in paths
    assert "also.txt" in paths
    assert "binary.dat" not in paths
