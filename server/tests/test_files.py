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


def test_list_roots_phase5_two_root_payload(fresh_db) -> None:
    """Phase 5 (PROJECTS_SPEC.md §7) — list_roots() surfaces exactly
    two scoped roots: `global` (parent of projects/) and `project`
    (the active project's tree). Legacy roots (context/knowledge/
    decisions/...) are intentionally hidden from the UI payload but
    remain registered for the read/write/edit code paths."""
    import asyncio

    from server.db import MISC_PROJECT_ID, init_db

    asyncio.get_event_loop().run_until_complete(init_db())

    rows = filesmod.list_roots()
    assert len(rows) == 2, f"expected 2 roots, got {len(rows)}: {rows}"

    by_id = {r["id"]: r for r in rows}
    assert "global" in by_id
    assert "project" in by_id

    global_row = by_id["global"]
    assert global_row["scope"] == "global"
    assert global_row["project_id"] is None
    assert global_row["writable"] is True
    assert "path" in global_row
    assert "label" in global_row

    project_row = by_id["project"]
    assert project_row["scope"] == "project"
    assert project_row["project_id"] == MISC_PROJECT_ID
    assert project_row["writable"] is True
    # Project root must be a sub-path of global so longest-prefix
    # matching in the UI file-link resolver picks `project` for paths
    # under /data/projects/<active>/.
    assert project_row["path"].startswith(global_row["path"])
    assert project_row["path"] != global_row["path"]
    # `key` alias kept for older clients still keying off `key`.
    assert project_row["key"] == project_row["id"] == "project"


def test_list_roots_omits_legacy_keys(fresh_db) -> None:
    """Legacy roots (context/knowledge/decisions/workspaces/outputs/
    uploads/plans/handoffs) are still in `_roots()` so the read/write
    code paths keep working, but `list_roots()` filters them out so
    the UI only renders the two Phase-5 scoped roots."""
    import asyncio

    from server.db import init_db

    asyncio.get_event_loop().run_until_complete(init_db())

    rows = filesmod.list_roots()
    ids = {r["id"] for r in rows}
    for legacy in (
        "context", "knowledge", "decisions", "workspaces",
        "outputs", "uploads", "plans", "handoffs",
    ):
        assert legacy not in ids, (
            f"legacy root {legacy!r} should not surface in list_roots()"
        )

    # ...but `_roots()` (the internal map) still has them so write
    # routing through ctxmod / `/api/files/write/context` keeps working.
    internal = filesmod._roots()
    assert "context" in internal
    assert internal["context"].scope == "legacy"


def test_list_roots_project_path_tracks_active_project(fresh_db) -> None:
    """Switching the active project must change the `project` root's
    path on the next list_roots() call so the UI renders the correct
    tree without a server restart."""
    import asyncio

    from server.db import (
        configured_conn,
        init_db,
        set_active_project,
    )

    async def setup_two_projects():
        await init_db()
        c = await configured_conn()
        try:
            await c.execute(
                "INSERT INTO projects (id, name) VALUES (?, ?)",
                ("alpha", "Alpha"),
            )
            await c.execute(
                "INSERT INTO projects (id, name) VALUES (?, ?)",
                ("beta", "Beta"),
            )
            await c.commit()
        finally:
            await c.close()
        await set_active_project("alpha")

    asyncio.get_event_loop().run_until_complete(setup_two_projects())

    alpha_rows = filesmod.list_roots()
    alpha_project = next(r for r in alpha_rows if r["id"] == "project")
    assert alpha_project["project_id"] == "alpha"
    assert alpha_project["path"].endswith("alpha")
    assert alpha_project["label"] == "Alpha"

    asyncio.get_event_loop().run_until_complete(set_active_project("beta"))

    beta_rows = filesmod.list_roots()
    beta_project = next(r for r in beta_rows if r["id"] == "project")
    assert beta_project["project_id"] == "beta"
    assert beta_project["path"].endswith("beta")
    assert beta_project["label"] == "Beta"
    assert beta_project["path"] != alpha_project["path"]


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
