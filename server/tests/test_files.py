"""Tests for server/files.py — the browsable-file backend.

Focus is on the path-safety guards (_resolve must reject traversal
and absolute-escape), the tree walk, and the write-routing that
sends context edits back through ctxmod. kDrive stays disabled.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

import server.context as ctxmod
import server.files as filesmod


@pytest.fixture
def tmp_roots(monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    ctx = Path(tempfile.mkdtemp(prefix="harness-f-ctx-"))
    knw = Path(tempfile.mkdtemp(prefix="harness-f-knw-"))
    dec = Path(tempfile.mkdtemp(prefix="harness-f-dec-"))
    monkeypatch.setenv("HARNESS_CONTEXT_DIR", str(ctx))
    monkeypatch.setenv("HARNESS_KNOWLEDGE_DIR", str(knw))
    monkeypatch.setenv("HARNESS_DECISIONS_DIR", str(dec))
    # ctxmod caches CONTEXT_DIR at import time, so point it at the
    # same tempdir for the ctxmod-routed write tests.
    monkeypatch.setattr(ctxmod, "CONTEXT_DIR", ctx)
    ctxmod._invalidate_list_cache()
    return {"context": ctx, "knowledge": knw, "decisions": dec}


# ---------- path safety ----------


def test_resolve_rejects_unknown_root(tmp_roots) -> None:
    with pytest.raises(ValueError):
        filesmod._resolve("nope", "x.md")


def test_resolve_rejects_traversal(tmp_roots) -> None:
    for bad in ("../../etc/passwd", "../escape", "a/../../escape"):
        with pytest.raises(ValueError):
            filesmod._resolve("knowledge", bad)


def test_resolve_strips_leading_slash(tmp_roots) -> None:
    # Leading / on the relative path should be stripped (not treated as
    # absolute) — otherwise the join would escape.
    target = filesmod._resolve("knowledge", "/foo.md")
    assert target.parent == tmp_roots["knowledge"].resolve()


def test_resolve_empty_returns_root(tmp_roots) -> None:
    assert filesmod._resolve("knowledge", "") == tmp_roots["knowledge"].resolve()


def test_list_roots_includes_absolute_path(tmp_roots) -> None:
    """The UI's file-link resolver does longest-prefix matching against
    the on-disk root path, so list_roots() must expose it."""
    rows = filesmod.list_roots()
    by_key = {r["key"]: r for r in rows}
    for key in ("context", "knowledge", "decisions"):
        assert key in by_key, f"missing root: {key}"
        assert "path" in by_key[key], f"root {key} missing 'path' field"
        assert by_key[key]["path"] == str(tmp_roots[key])


# ---------- tree ----------


def test_tree_empty_root_returns_no_children(tmp_roots) -> None:
    t = filesmod.tree("knowledge")
    assert t["type"] == "dir"
    assert t["children"] == []


def test_tree_missing_root_returns_empty(tmp_roots, monkeypatch) -> None:
    # Point knowledge at a path that doesn't exist — tree() must not raise.
    monkeypatch.setenv("HARNESS_KNOWLEDGE_DIR", "/tmp/does/not/exist/harness-test")
    t = filesmod.tree("knowledge")
    assert t["children"] == []


def test_tree_sorts_dirs_before_files_case_insensitive(tmp_roots) -> None:
    root = tmp_roots["knowledge"]
    (root / "zeta.md").write_text("z")
    (root / "alpha").mkdir()
    (root / "alpha" / "inner.md").write_text("i")
    (root / "beta.md").write_text("b")
    t = filesmod.tree("knowledge")
    names = [c["name"] for c in t["children"]]
    # Dir 'alpha' first (dirs-before-files), then files beta, zeta.
    assert names[0] == "alpha"
    assert names[1:] == ["beta.md", "zeta.md"]


def test_tree_skips_noise_dirs(tmp_roots) -> None:
    root = tmp_roots["knowledge"]
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x")
    (root / "keep.md").write_text("k")
    t = filesmod.tree("knowledge")
    names = [c["name"] for c in t["children"]]
    assert names == ["keep.md"]


# ---------- read ----------


def test_read_missing_raises_filenotfound(tmp_roots) -> None:
    with pytest.raises(FileNotFoundError):
        filesmod.read_text("knowledge", "nope.md")


def test_read_oversize_raises_value_error(tmp_roots) -> None:
    root = tmp_roots["knowledge"]
    huge_path = root / "huge.md"
    huge_path.write_bytes(b"x" * (filesmod.READ_MAX_BYTES + 1))
    with pytest.raises(ValueError):
        filesmod.read_text("knowledge", "huge.md")


def test_read_roundtrip(tmp_roots) -> None:
    root = tmp_roots["knowledge"]
    (root / "note.md").write_text("hello\n", encoding="utf-8")
    r = filesmod.read_text("knowledge", "note.md")
    assert r["content"] == "hello\n"
    assert r["root"] == "knowledge"
    assert r["path"] == "note.md"


# ---------- write routing ----------


async def test_write_decisions_refused(tmp_roots) -> None:
    # Decisions root is read-only through the files API.
    with pytest.raises(PermissionError):
        await filesmod.write_text("decisions", "2026-04-23-foo.md", "body")


async def test_write_knowledge_writes_to_disk(tmp_roots) -> None:
    r = await filesmod.write_text("knowledge", "hi.md", "body")
    assert r["routed_through"] == "disk"
    assert (tmp_roots["knowledge"] / "hi.md").read_text() == "body"


async def test_write_knowledge_rejects_non_text_extension(tmp_roots) -> None:
    with pytest.raises(ValueError):
        await filesmod.write_text("knowledge", "foo.bin", "body")


async def test_write_context_routes_through_ctxmod(tmp_roots) -> None:
    # CLAUDE.md at the root of context/ is handled by ctxmod.write, so
    # the result is marked routed_through='ctxmod' (kDrive mirror + list-
    # cache invalidation happen there, not on the disk path).
    r = await filesmod.write_text("context", "CLAUDE.md", "top")
    assert r["routed_through"] == "ctxmod"
    assert (tmp_roots["context"] / "CLAUDE.md").read_text() == "top"


async def test_write_context_skill_goes_to_skills_kind(tmp_roots) -> None:
    await filesmod.write_text("context", "skills/debug.md", "skill body")
    assert (tmp_roots["context"] / "skills" / "debug.md").read_text() == "skill body"


async def test_write_context_rejects_unsupported_path(tmp_roots) -> None:
    # Only root/CLAUDE.md, skills/*.md, rules/*.md are valid context
    # paths. Anything else at the top level should be refused.
    with pytest.raises(ValueError):
        await filesmod.write_text("context", "arbitrary.md", "body")
    with pytest.raises(ValueError):
        await filesmod.write_text("context", "skills/bad/nested.md", "body")
