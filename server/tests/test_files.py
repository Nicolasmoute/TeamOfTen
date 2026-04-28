"""Tests for server/files.py — the browsable-file backend.

Only `global` and `project` roots are exposed; everything else is
reached by drilling into `project`. The per-test data root is
sandboxed by the `fresh_db` fixture in conftest.
"""

from __future__ import annotations

import asyncio

import pytest

import server.files as filesmod
from server.db import (
    MISC_PROJECT_ID,
    configured_conn,
    init_db,
    set_active_project,
)
from server.paths import global_paths, project_paths


# ---------- path safety ----------


def test_resolve_rejects_unknown_root(fresh_db) -> None:
    with pytest.raises(ValueError):
        filesmod._resolve("nope", "x.md")


def test_resolve_rejects_traversal(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    for bad in ("../../etc/passwd", "../escape", "a/../../escape"):
        with pytest.raises(ValueError):
            filesmod._resolve("global", bad)


def test_resolve_strips_leading_slash(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    target = filesmod._resolve("global", "/foo.md")
    # Resolves under the global root (not as an absolute filesystem path).
    assert target.parent == global_paths().root.resolve()


def test_resolve_empty_returns_root(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    assert filesmod._resolve("global", "") == global_paths().root.resolve()


# ---------- list_roots ----------


def test_list_roots_phase5_two_root_payload(fresh_db) -> None:
    """Phase 5 (PROJECTS_SPEC.md §7) — list_roots() surfaces exactly
    two scoped roots: `global` and `project` (the active project's tree)."""
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

    project_row = by_id["project"]
    assert project_row["scope"] == "project"
    assert project_row["project_id"] == MISC_PROJECT_ID
    assert project_row["writable"] is True
    # Project root must be a sub-path of global so longest-prefix
    # matching in the UI file-link resolver picks `project` for paths
    # under /data/projects/<active>/.
    assert project_row["path"].startswith(global_row["path"])
    assert project_row["path"] != global_row["path"]


def test_list_roots_omits_legacy_keys(fresh_db) -> None:
    """Only `global` and `project` are exposed — the per-project
    subtrees (context/knowledge/decisions/workspaces/outputs/uploads/
    plans/handoffs) are reached by drilling into `project`, not as
    top-level roots."""
    asyncio.get_event_loop().run_until_complete(init_db())

    rows = filesmod.list_roots()
    ids = {r["id"] for r in rows}
    for legacy in (
        "context", "knowledge", "decisions", "workspaces",
        "outputs", "uploads", "plans", "handoffs",
    ):
        assert legacy not in ids, (
            f"root {legacy!r} should not surface in list_roots()"
        )

    internal = filesmod._roots()
    for legacy in ("context", "knowledge", "decisions"):
        assert legacy not in internal


def test_list_roots_project_path_tracks_active_project(fresh_db) -> None:
    """Switching the active project must change the `project` root's
    path on the next list_roots() call so the UI renders the correct
    tree without a server restart."""

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

    asyncio.get_event_loop().run_until_complete(set_active_project("beta"))

    beta_rows = filesmod.list_roots()
    beta_project = next(r for r in beta_rows if r["id"] == "project")
    assert beta_project["project_id"] == "beta"
    assert beta_project["path"].endswith("beta")
    assert beta_project["path"] != alpha_project["path"]


# ---------- tree ----------


def test_tree_empty_root_returns_no_children(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    pp = project_paths(MISC_PROJECT_ID)
    # Wipe the misc project tree so tree() has nothing to enumerate.
    import shutil
    shutil.rmtree(pp.root, ignore_errors=True)
    pp.root.mkdir(parents=True, exist_ok=True)
    t = filesmod.tree("project")
    assert t["type"] == "dir"
    assert t["children"] == []


def test_tree_missing_root_returns_empty(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    pp = project_paths(MISC_PROJECT_ID)
    import shutil
    shutil.rmtree(pp.root, ignore_errors=True)
    # tree() should not raise even though the dir is gone.
    t = filesmod.tree("project")
    assert t["children"] == []


def test_tree_sorts_dirs_before_files_case_insensitive(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    pp = project_paths(MISC_PROJECT_ID)
    # Use working/workspace as a clean sandbox where we control everything.
    sandbox = pp.working_workspace
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / "zeta.md").write_text("z")
    (sandbox / "alpha").mkdir()
    (sandbox / "alpha" / "inner.md").write_text("i")
    (sandbox / "beta.md").write_text("b")
    t = filesmod.tree("project")
    # Walk into working/workspace.
    working = next(c for c in t["children"] if c["name"] == "working")
    workspace = next(c for c in working["children"] if c["name"] == "workspace")
    names = [c["name"] for c in workspace["children"]]
    assert names[0] == "alpha"
    assert names[1:] == ["beta.md", "zeta.md"]


def test_tree_skips_noise_dirs(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    pp = project_paths(MISC_PROJECT_ID)
    sandbox = pp.working_workspace
    sandbox.mkdir(parents=True, exist_ok=True)
    (sandbox / ".git").mkdir()
    (sandbox / ".git" / "config").write_text("x")
    (sandbox / "keep.md").write_text("k")
    t = filesmod.tree("project")
    working = next(c for c in t["children"] if c["name"] == "working")
    workspace = next(c for c in working["children"] if c["name"] == "workspace")
    names = [c["name"] for c in workspace["children"]]
    assert ".git" not in names
    assert "keep.md" in names


# ---------- read ----------


def test_read_missing_raises_filenotfound(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    with pytest.raises(FileNotFoundError):
        filesmod.read_text("project", "does/not/exist.md")


def test_read_oversize_raises_value_error(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    pp = project_paths(MISC_PROJECT_ID)
    huge_path = pp.working_workspace / "huge.md"
    pp.working_workspace.mkdir(parents=True, exist_ok=True)
    huge_path.write_bytes(b"x" * (filesmod.READ_MAX_BYTES + 1))
    with pytest.raises(ValueError):
        filesmod.read_text("project", "working/workspace/huge.md")


def test_read_roundtrip(fresh_db) -> None:
    asyncio.get_event_loop().run_until_complete(init_db())
    pp = project_paths(MISC_PROJECT_ID)
    pp.working_workspace.mkdir(parents=True, exist_ok=True)
    # write_bytes (not write_text) so Windows doesn't translate
    # \n -> \r\n. read_text reads raw bytes back — the test asserts
    # the harness preserves what was on disk.
    (pp.working_workspace / "note.md").write_bytes(b"hello\n")
    r = filesmod.read_text("project", "working/workspace/note.md")
    assert r["content"] == "hello\n"
    assert r["root"] == "project"
    assert r["path"] == "working/workspace/note.md"


# ---------- write routing ----------


async def test_write_to_project_writes_to_disk(fresh_db) -> None:
    await init_db()
    r = await filesmod.write_text("project", "working/workspace/hi.md", "body")
    assert r["routed_through"] == "disk"
    pp = project_paths(MISC_PROJECT_ID)
    assert (pp.working_workspace / "hi.md").read_text() == "body"


async def test_write_to_global_writes_to_disk(fresh_db) -> None:
    """`global` is writable (the file-browser can edit the global
    CLAUDE.md). All writes go through plain disk."""
    await init_db()
    r = await filesmod.write_text("global", "CLAUDE.md", "global body\n")
    assert r["routed_through"] == "disk"
    assert global_paths().claude_md.read_text(encoding="utf-8") == "global body\n"


async def test_write_rejects_non_text_extension(fresh_db) -> None:
    await init_db()
    with pytest.raises(ValueError):
        await filesmod.write_text("project", "working/workspace/foo.bin", "body")


async def test_write_rejects_unknown_root(fresh_db) -> None:
    await init_db()
    with pytest.raises(PermissionError):
        await filesmod.write_text("nope", "x.md", "body")
