"""Tests for server/project_sync.py — Phase 2 per-project sync.

Covers the parts that don't require a live WebDAV mirror:
- `sync_state` CRUD via _sync_state_paths_for / _sync_state_upsert /
  _sync_state_delete
- _file_sha256 over a tempfile
- retry semantics: backoff, exhaustion, kdrive_sync_failed event
- _walk_files honors exclude_subdirs

Tests for the actual push/pull paths run with a stub webdav module
patched into server.project_sync.webdav so we can assert behavior
without webdav4 in the venv.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

import pytest

import server.project_sync as ps
from server.db import configured_conn, init_db


# ---------- _file_sha256 ----------


async def test_sha256_matches_hashlib(tmp_path: Path, fresh_db: str) -> None:
    p = tmp_path / "a.bin"
    payload = b"hello sync world\n" * 100
    p.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert ps._sha256_file(p) == expected


# ---------- _walk_files ----------


async def test_walk_files_skips_excluded_top_level(
    tmp_path: Path, fresh_db: str
) -> None:
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "a.md").write_text("a")
    (tmp_path / "decisions").mkdir()
    (tmp_path / "decisions" / "b.md").write_text("b")
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "deep").mkdir()
    (tmp_path / "repo" / "deep" / "c.md").write_text("c")
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "d.png").write_bytes(b"x")

    rels = sorted(rel for rel, _, _ in ps._walk_files(
        tmp_path, exclude_subdirs=("repo", "attachments")
    ))
    assert rels == ["decisions/b.md", "memory/a.md"]


async def test_walk_files_missing_root_yields_nothing(fresh_db: str) -> None:
    rels = list(ps._walk_files(Path("/nonexistent-harness-path-xyz")))
    assert rels == []


# ---------- sync_state CRUD ----------


async def test_sync_state_crud_roundtrip(fresh_db: str) -> None:
    await init_db()
    db = await configured_conn()
    try:
        # misc project is auto-seeded by init_db; reuse it as the FK.
        await ps._sync_state_upsert(
            db, "misc", "project", "memory/a.md",
            mtime=1234.5, size_bytes=42, sha256="aa" * 32,
        )
        await ps._sync_state_upsert(
            db, "misc", "project", "memory/b.md",
            mtime=4321.0, size_bytes=7, sha256="bb" * 32,
        )
        await db.commit()
        rows = await ps._sync_state_paths_for(db, "misc", "project")
        assert set(rows.keys()) == {"memory/a.md", "memory/b.md"}
        assert rows["memory/a.md"].size_bytes == 42
        assert rows["memory/b.md"].sha256 == "bb" * 32

        # Upsert: same path bumps fields.
        await ps._sync_state_upsert(
            db, "misc", "project", "memory/a.md",
            mtime=9999.0, size_bytes=100, sha256="cc" * 32,
        )
        await db.commit()
        rows = await ps._sync_state_paths_for(db, "misc", "project")
        assert rows["memory/a.md"].size_bytes == 100
        assert rows["memory/a.md"].sha256 == "cc" * 32

        # Delete drops the row.
        await ps._sync_state_delete(db, "misc", "project", "memory/a.md")
        await db.commit()
        rows = await ps._sync_state_paths_for(db, "misc", "project")
        assert "memory/a.md" not in rows
        assert "memory/b.md" in rows
    finally:
        await db.close()


async def test_sync_state_scope_per_tree(fresh_db: str) -> None:
    """Same path, different tree, must not collide."""
    await init_db()
    db = await configured_conn()
    try:
        await ps._sync_state_upsert(
            db, "misc", "project", "x.md",
            mtime=1.0, size_bytes=1, sha256="aa" * 32,
        )
        await ps._sync_state_upsert(
            db, "misc", "wiki", "x.md",
            mtime=2.0, size_bytes=2, sha256="bb" * 32,
        )
        await db.commit()
        proj_rows = await ps._sync_state_paths_for(db, "misc", "project")
        wiki_rows = await ps._sync_state_paths_for(db, "misc", "wiki")
        assert proj_rows["x.md"].size_bytes == 1
        assert wiki_rows["x.md"].size_bytes == 2
    finally:
        await db.close()


# ---------- _with_kdrive_retry ----------


async def test_retry_succeeds_on_first_attempt(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    monkeypatch.setattr(ps, "KDRIVE_RETRY_INITIAL_S", 0.0)
    calls: list[int] = []

    async def op() -> bool:
        calls.append(1)
        return True

    ok = await ps._with_kdrive_retry(
        op, op_label="push", project_id="misc", tree="project", path="t",
    )
    assert ok is True
    assert len(calls) == 1


async def test_retry_eventually_succeeds(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    # Set delay to 0 so the test runs fast; we still exercise the loop.
    monkeypatch.setattr(ps, "KDRIVE_RETRY_INITIAL_S", 0.0)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_CAP_S", 0.0)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_MAX", 5)
    counter = {"n": 0}

    async def op() -> bool:
        counter["n"] += 1
        if counter["n"] < 3:
            return False
        return True

    ok = await ps._with_kdrive_retry(
        op, op_label="push", project_id="misc", tree="project", path="t",
    )
    assert ok is True
    assert counter["n"] == 3


async def test_retry_exhaustion_emits_event(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    monkeypatch.setattr(ps, "KDRIVE_RETRY_INITIAL_S", 0.0)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_CAP_S", 0.0)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_MAX", 3)

    captured: list[dict[str, Any]] = []

    class _StubBus:
        async def publish(self, ev: dict[str, Any]) -> None:
            captured.append(ev)

    monkeypatch.setattr(ps, "bus", _StubBus())

    async def op() -> bool:
        return False

    ok = await ps._with_kdrive_retry(
        op,
        op_label="push",
        project_id="alpha",
        tree="project",
        path="memory/x.md",
    )
    assert ok is False
    assert len(captured) == 1
    ev = captured[0]
    assert ev["type"] == "kdrive_sync_failed"
    assert ev["op"] == "push"
    assert ev["project_id"] == "alpha"
    assert ev["tree"] == "project"
    assert ev["path"] == "memory/x.md"


async def test_retry_propagates_cancellation(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled coroutine must NOT be swallowed by the retry shell —
    it has to surface so loop teardown can complete."""
    await init_db()
    monkeypatch.setattr(ps, "KDRIVE_RETRY_INITIAL_S", 0.05)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_MAX", 5)

    started = asyncio.Event()

    async def op() -> bool:
        started.set()
        await asyncio.sleep(60)
        return False

    async def runner() -> bool:
        return await ps._with_kdrive_retry(
            op,
            op_label="push",
            project_id="misc",
            tree="project",
            path="x",
        )

    task = asyncio.create_task(runner())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------- _project_remote_for ----------


async def test_remote_path_mapping(fresh_db: str) -> None:
    assert ps._project_remote_for("misc", "project", "memory/a.md") == \
        "projects/misc/memory/a.md"
    assert ps._project_remote_for("misc", "wiki", "concept.md") == \
        "wiki/misc/concept.md"
    assert ps._project_remote_for("misc", "global", "CLAUDE.md") == "CLAUDE.md"
    assert ps._project_remote_for("misc", "global", "wiki/INDEX.md") == \
        "wiki/INDEX.md"
    with pytest.raises(ValueError):
        ps._project_remote_for("misc", "bogus", "x")


# ---------- push_project_tree (with stub webdav) ----------


class _StubWebDAV:
    """In-memory WebDAV stand-in. Records every write/remove as a dict
    keyed by remote path so tests can assert. Implements both
    `write_bytes` and `write_bytes_atomic` because the push path uses
    the atomic variant after Phase 2 audit fix #6."""

    def __init__(self, *, fail_paths: set[str] | None = None) -> None:
        self.enabled = True
        self.writes: dict[str, bytes] = {}
        self.removes: list[str] = []
        self._fail_paths = fail_paths or set()

    async def write_bytes(self, rel: str, data: bytes) -> bool:
        if rel in self._fail_paths:
            return False
        self.writes[rel] = data
        return True

    async def write_bytes_atomic(self, rel: str, data: bytes) -> bool:
        return await self.write_bytes(rel, data)

    async def write_text(self, rel: str, text: str) -> bool:
        return await self.write_bytes(rel, text.encode("utf-8"))

    async def remove(self, rel: str) -> bool:
        self.removes.append(rel)
        return True

    async def list_dir(self, rel: str) -> list[str]:
        return []

    async def walk_files(self, rel: str) -> list[str]:
        return []

    async def read_bytes(self, rel: str) -> bytes | None:
        return self.writes.get(rel)


async def test_push_project_tree_pushes_changed_files(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end push for one project: changed files upload, unchanged
    files short-circuit, deleted files remove + drop sync_state."""
    await init_db()
    # Use the auto-seeded misc project; relocate paths.DATA_ROOT so
    # project_paths('misc') points under tmp_path. The fresh_db fixture
    # already does this — assert and continue.
    import server.paths as pathsmod
    assert pathsmod.DATA_ROOT == Path(pathsmod.DATA_ROOT)  # patched by fixture

    pp = pathsmod.project_paths("misc")
    # Phase 7 audit: init_db now writes a CLAUDE.md stub for misc.
    # This test exercises push mechanics on a controlled set of files,
    # so remove the stub up front.
    if pp.claude_md.exists():
        pp.claude_md.unlink()
    pp.memory.mkdir(parents=True, exist_ok=True)
    (pp.memory / "a.md").write_text("hello v1")
    (pp.memory / "b.md").write_text("second")

    stub = _StubWebDAV()
    monkeypatch.setattr(ps, "webdav", stub)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_INITIAL_S", 0.0)

    # First push: both files upload.
    out = await ps.push_project_tree("misc")
    assert out["project"]["pushed"] == 2
    assert out["project"]["unchanged"] == 0
    assert "projects/misc/memory/a.md" in stub.writes
    assert "projects/misc/memory/b.md" in stub.writes

    # Second push: nothing changed.
    out = await ps.push_project_tree("misc")
    assert out["project"]["pushed"] == 0
    assert out["project"]["unchanged"] == 2

    # Modify a.md; only that file should re-push.
    (pp.memory / "a.md").write_text("hello v2 — bigger")
    stub.writes.clear()
    out = await ps.push_project_tree("misc")
    assert out["project"]["pushed"] == 1
    assert out["project"]["unchanged"] == 1
    assert list(stub.writes) == ["projects/misc/memory/a.md"]

    # Delete b.md locally; next push should remove + drop sync_state.
    (pp.memory / "b.md").unlink()
    stub.writes.clear()
    stub.removes.clear()
    out = await ps.push_project_tree("misc")
    assert out["project"]["deleted"] == 1
    assert out["project"]["pushed"] == 0
    assert "projects/misc/memory/b.md" in stub.removes


async def test_push_project_tree_skips_repo_and_attachments(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`repo/` is git's territory; `attachments/` is local-only by §4."""
    await init_db()
    import server.paths as pathsmod
    pp = pathsmod.project_paths("misc")
    # Phase 7 audit: init_db writes a CLAUDE.md stub for misc; remove
    # so this test sees only the controlled fixture files.
    if pp.claude_md.exists():
        pp.claude_md.unlink()
    (pp.memory).mkdir(parents=True, exist_ok=True)
    (pp.memory / "ok.md").write_text("ok")
    (pp.repo).mkdir(parents=True, exist_ok=True)
    (pp.repo / "should-skip.txt").write_text("nope")
    (pp.attachments).mkdir(parents=True, exist_ok=True)
    (pp.attachments / "img.png").write_bytes(b"local-only")

    stub = _StubWebDAV()
    monkeypatch.setattr(ps, "webdav", stub)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_INITIAL_S", 0.0)
    out = await ps.push_project_tree("misc")
    # Only memory/ok.md should land remote.
    assert list(stub.writes) == ["projects/misc/memory/ok.md"]
    assert out["project"]["pushed"] == 1


async def test_push_project_tree_when_webdav_disabled(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()

    class _Off:
        enabled = False

    monkeypatch.setattr(ps, "webdav", _Off())
    out = await ps.push_project_tree("misc")
    assert out == {
        "project": {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0},
        "wiki": {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0},
    }


# ---------- force_push_project ----------


async def test_force_push_project_timeout(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()

    async def slow_push(project_id: str) -> dict[str, dict[str, int]]:
        await asyncio.sleep(60)
        return {"project": {}, "wiki": {}}

    monkeypatch.setattr(ps, "push_project_tree", slow_push)
    out = await ps.force_push_project("misc", timeout_s=0.05)
    assert out["timed_out"] is True
    assert out["counts"] is None


async def test_force_push_project_completes(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    captured: dict[str, str] = {}

    async def fast_push(project_id: str) -> dict[str, dict[str, int]]:
        captured["called_with"] = project_id
        return {
            "project": {"pushed": 1, "unchanged": 0, "failed": 0, "deleted": 0},
            "wiki": {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0},
        }

    monkeypatch.setattr(ps, "push_project_tree", fast_push)
    out = await ps.force_push_project("misc", timeout_s=5)
    assert out["timed_out"] is False
    assert captured["called_with"] == "misc"
    assert out["counts"]["project"]["pushed"] == 1


# ---------- push_global_tree e2e (Audit fix #1: CHECK constraint) ----------


async def test_push_global_tree_inserts_sync_state_with_global_tree(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for Audit fix #1: sync_state.tree CHECK previously
    rejected 'global', causing push_global_tree to crash silently on
    every cycle. Verify a real INSERT lands."""
    await init_db()
    import server.paths as pathsmod
    gp = pathsmod.global_paths()
    gp.root.mkdir(parents=True, exist_ok=True)
    gp.skills.mkdir(parents=True, exist_ok=True)
    gp.wiki.mkdir(parents=True, exist_ok=True)
    gp.claude_md.write_text("# Global rules")
    (gp.skills / "demo").mkdir(parents=True, exist_ok=True)
    (gp.skills / "demo" / "SKILL.md").write_text("skill body")
    (gp.wiki / "INDEX.md").write_text("# Wiki Index")
    (gp.wiki / "shared.md").write_text("cross-project concept")

    stub = _StubWebDAV()
    monkeypatch.setattr(ps, "webdav", stub)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_INITIAL_S", 0.0)

    counts = await ps.push_global_tree()
    assert counts["pushed"] >= 4  # CLAUDE.md + skill + INDEX + shared
    assert "CLAUDE.md" in stub.writes
    assert "skills/demo/SKILL.md" in stub.writes
    assert "wiki/INDEX.md" in stub.writes
    assert "wiki/shared.md" in stub.writes

    # Verify sync_state has tree='global' rows now.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT path FROM sync_state WHERE tree = 'global' ORDER BY path"
        )
        paths = [dict(r)["path"] for r in await cur.fetchall()]
    finally:
        await c.close()
    assert "CLAUDE.md" in paths
    assert "wiki/INDEX.md" in paths

    # Second cycle: nothing changed, all unchanged.
    stub.writes.clear()
    counts2 = await ps.push_global_tree()
    assert counts2["pushed"] == 0
    assert counts2["unchanged"] >= 4
    assert stub.writes == {}


# ---------- tag_live_conversations (Audit fix #7) ----------


async def test_tag_live_conversations_marks_recent_files(
    fresh_db: str, tmp_path: Path
) -> None:
    """Recent (within LIVE_FRESHNESS_S) conversation files get
    `live: true` frontmatter. Stale files left alone. Files that
    already have frontmatter not overwritten."""
    import os as _os
    import time as _time
    import server.paths as pathsmod
    pp = pathsmod.project_paths("misc")
    pp.working_conversations.mkdir(parents=True, exist_ok=True)
    fresh = pp.working_conversations / "fresh.md"
    stale = pp.working_conversations / "stale.md"
    already = pp.working_conversations / "already.md"
    fresh.write_text("conversation body fresh")
    stale.write_text("conversation body stale")
    already.write_text("---\nresumed: 2026-04-25\n---\n\nbody")
    # Backdate stale by 60 minutes.
    stale_ts = _time.time() - 3600
    _os.utime(stale, (stale_ts, stale_ts))

    tagged = ps.tag_live_conversations("misc")
    assert tagged == 1
    assert fresh.read_text().startswith("---\nlive: true\n---\n")
    assert "live: true" not in stale.read_text()
    assert already.read_text().startswith("---\nresumed:")


async def test_tag_live_conversations_no_dir(fresh_db: str) -> None:
    """No conversations dir → returns 0, no exception."""
    tagged = ps.tag_live_conversations("misc")
    assert tagged == 0


async def test_force_push_project_invokes_live_tagging(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per spec §5 step 2 the live tag pass must run BEFORE the push."""
    await init_db()
    seq: list[str] = []

    def stub_tag(project_id: str) -> int:
        seq.append("tag")
        return 0

    async def stub_push(project_id: str) -> dict[str, dict[str, int]]:
        seq.append("push")
        return {
            "project": {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0},
            "wiki": {"pushed": 0, "unchanged": 0, "failed": 0, "deleted": 0},
        }

    monkeypatch.setattr(ps, "tag_live_conversations", stub_tag)
    monkeypatch.setattr(ps, "push_project_tree", stub_push)
    await ps.force_push_project("misc", timeout_s=5)
    assert seq == ["tag", "push"]


# ---------- atomic write (Audit fix #6) ----------


async def test_push_uses_atomic_write(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The push path should call `webdav.write_bytes_atomic`, not the
    legacy `write_bytes`. Captures both methods on the stub and asserts
    only the atomic one fires."""
    await init_db()
    import server.paths as pathsmod
    pp = pathsmod.project_paths("misc")
    # Phase 7 audit: init_db writes a CLAUDE.md stub for misc; remove
    # so this test sees only the controlled fixture files.
    if pp.claude_md.exists():
        pp.claude_md.unlink()
    pp.memory.mkdir(parents=True, exist_ok=True)
    (pp.memory / "a.md").write_text("hello")

    class _Stub:
        enabled = True
        atomic_writes: dict[str, bytes] = {}
        plain_writes: dict[str, bytes] = {}
        removes: list[str] = []

        async def write_bytes_atomic(self, rel: str, data: bytes) -> bool:
            self.atomic_writes[rel] = data
            return True

        async def write_bytes(self, rel: str, data: bytes) -> bool:
            self.plain_writes[rel] = data
            return True

        async def write_text(self, rel: str, text: str) -> bool:
            return True

        async def remove(self, rel: str) -> bool:
            self.removes.append(rel)
            return True

        async def list_dir(self, rel: str) -> list[str]:
            return []

        async def walk_files(self, rel: str) -> list[str]:
            return []

    stub = _Stub()
    monkeypatch.setattr(ps, "webdav", stub)
    monkeypatch.setattr(ps, "KDRIVE_RETRY_INITIAL_S", 0.0)
    out = await ps.push_project_tree("misc")
    assert out["project"]["pushed"] == 1
    assert "projects/misc/memory/a.md" in stub.atomic_writes
    assert stub.plain_writes == {}
