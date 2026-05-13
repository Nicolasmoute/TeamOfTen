"""Workspace provisioning regressions.

The 2026-05-06 refactor collapsed two competing path schemes into one
(`/data/projects/<id>/repo/<slot>`). The first deploy hit
CLIConnectionError on agent spawn because `ensure_workspaces` returned
early on `repo_url=''` without creating plain per-slot dirs — the SDK
chdir then failed with ENOENT before any tool could run. These tests
pin the corrected behavior:

  1. `ensure_workspaces` ALWAYS creates per-slot dirs even without
     a repo_url (research / chat / doc work doesn't need a git tree).
  2. `workspace_dir(slot)` self-heals by mkdir'ing the path if it's
     missing — keeps a transient FS hiccup from cascading into a
     spawn crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.db import configured_conn, init_db, set_active_project


def _drain_provisioned_events(q) -> list[dict]:
    """Pull every queued event without blocking, keep only the
    `project_repo_provisioned` ones. The bus subscribe()/unsubscribe()
    pair returns a plain asyncio.Queue, not a callback registration —
    drain via get_nowait() until empty."""
    import asyncio as _asyncio
    out: list[dict] = []
    while True:
        try:
            ev = q.get_nowait()
        except _asyncio.QueueEmpty:
            break
        if ev.get("type") == "project_repo_provisioned":
            out.append(ev)
    return out


async def _create_project_no_repo(project_id: str) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR IGNORE INTO projects (id, name) VALUES (?, ?)",
            (project_id, project_id),
        )
        await c.commit()
    finally:
        await c.close()


async def _set_project_repo(project_id: str, repo_url: str) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE projects SET repo_url = ? WHERE id = ?",
            (repo_url, project_id),
        )
        await c.commit()
    finally:
        await c.close()


@pytest.mark.usefixtures("fresh_db")
async def test_ensure_workspaces_no_repo_creates_plain_slot_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK chdir crashes ENOENT before any tool can run if the
    cwd doesn't exist. Even without a repo configured, the slot dir
    must exist so research / chat / doc work survives.

    Regression: the first 2026-05-06 deploy hit CLIConnectionError
    on Coach because ensure_workspaces returned early on empty
    repo_url and never mkdir'd the per-slot dirs.
    """
    import server.paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)

    await init_db()
    await _create_project_no_repo("alpha")
    # Repo URL deliberately not set.

    from server.workspaces import ensure_workspaces, SLOT_IDS
    result = await ensure_workspaces("alpha")
    assert result["configured"] is False
    assert result["project_id"] == "alpha"

    repo_root = tmp_path / "projects" / "alpha" / "repo"
    for slot in SLOT_IDS:
        slot_dir = repo_root / slot
        assert slot_dir.exists(), f"missing per-slot dir for {slot}: {slot_dir}"
        assert slot_dir.is_dir()
        # Plain dir, not a worktree — coord_commit_push will reject loudly.
        assert not (slot_dir / ".git").exists()


@pytest.mark.usefixtures("fresh_db")
async def test_ensure_workspaces_idempotent_without_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running ensure_workspaces on a no-repo project must not
    error or duplicate-create."""
    import server.paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)

    await init_db()
    await _create_project_no_repo("beta")

    from server.workspaces import ensure_workspaces
    r1 = await ensure_workspaces("beta")
    r2 = await ensure_workspaces("beta")
    assert r1["configured"] is False
    assert r2["configured"] is False


@pytest.mark.usefixtures("fresh_db")
async def test_workspace_dir_self_heals_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workspace_dir mkdirs the path if it's missing — a transient FS
    error or mid-migration deploy mustn't cascade into ENOENT on
    every agent spawn."""
    import server.paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)

    await init_db()
    await _create_project_no_repo("gamma")
    await set_active_project("gamma")

    # Sanity: confirm the slot dir doesn't exist yet (we never ran
    # ensure_workspaces in this test). The self-heal should create it.
    slot_dir = tmp_path / "projects" / "gamma" / "repo" / "p3"
    assert not slot_dir.exists()

    from server.workspaces import workspace_dir
    result = await workspace_dir("p3")
    assert result == slot_dir
    assert slot_dir.exists()
    assert slot_dir.is_dir()


@pytest.mark.usefixtures("fresh_db")
async def test_workspace_dir_returns_existing_path_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the dir already exists (the common case after
    ensure_workspaces ran), workspace_dir is a pure resolution —
    no mtime touch, no recreate."""
    import server.paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)

    await init_db()
    await _create_project_no_repo("delta")
    await set_active_project("delta")

    from server.workspaces import ensure_workspaces, workspace_dir
    await ensure_workspaces("delta")

    slot_dir = tmp_path / "projects" / "delta" / "repo" / "p5"
    sentinel = slot_dir / "marker.txt"
    sentinel.write_text("preserved", encoding="utf-8")

    result = await workspace_dir("p5")
    assert result == slot_dir
    # Existing contents preserved (mkdir(exist_ok=True) is a no-op).
    assert sentinel.read_text(encoding="utf-8") == "preserved"


@pytest.mark.usefixtures("fresh_db")
async def test_provision_after_change_no_repo_emits_ok_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fire-and-forget hook fired by create_project / patch_project
    must succeed (configured: False) when no repo_url is set, and
    publish a `project_repo_provisioned` event with the source tag so
    subscribers can tell apart auto-fire from manual provision."""
    import server.paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)

    await init_db()
    await _create_project_no_repo("epsilon")

    from server.events import bus
    q = bus.subscribe()
    try:
        from server.projects_api import _provision_after_change
        await _provision_after_change(
            project_id="epsilon", source="project_created", actor={"source": "test"},
        )
        captured = _drain_provisioned_events(q)
    finally:
        bus.unsubscribe(q)

    assert len(captured) == 1
    ev = captured[0]
    assert ev["project_id"] == "epsilon"
    assert ev["ok"] is True
    assert ev["error"] is None
    assert ev["slot_failures"] is None
    assert ev["source"] == "project_created"


@pytest.mark.usefixtures("fresh_db")
async def test_create_project_endpoint_auto_fires_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the real `POST /api/projects` HTTP endpoint
    with `TestClient`. Verifies the auto-fired provisioning task
    actually runs (the helper is `asyncio.create_task`-d, so the
    direct-await tests below don't exercise that path) and ends up
    publishing the `project_repo_provisioned` event.

    Regression for the audit finding that the helper was tested in
    isolation but the actual fire-and-forget path was not.
    """
    import server.paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)

    await init_db()

    from fastapi.testclient import TestClient
    from server.events import bus
    from server.main import app
    from server.projects_api import _provision_tasks

    q = bus.subscribe()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post(
                "/api/projects",
                json={
                    "slug": "endtoend",
                    "name": "End-to-End",
                    # No repo_url — keeps the test off the network. The
                    # auto-fire still runs (creates plain per-slot
                    # dirs) and emits the bus event.
                    "description": "auto-fire smoke",
                },
            )
            assert r.status_code == 201, r.text

            # Wait for any in-flight provisioning task to settle.
            # `_provision_tasks` is the registry the lifespan uses to
            # cancel on shutdown; it's also a handy way for tests to
            # observe completion deterministically.
            import asyncio as _asyncio
            for _ in range(50):
                pending = [t for t in _provision_tasks if not t.done()]
                if not pending:
                    break
                await _asyncio.sleep(0.05)

        captured = _drain_provisioned_events(q)
    finally:
        bus.unsubscribe(q)

    auto_fired = [e for e in captured if e.get("project_id") == "endtoend"]
    assert len(auto_fired) == 1, f"expected 1 event, got: {captured}"
    ev = auto_fired[0]
    assert ev["ok"] is True
    assert ev["source"] == "project_created"
    # Plain per-slot dirs created (the always-on path).
    repo_root = tmp_path / "projects" / "endtoend" / "repo"
    assert (repo_root / "coach").exists()
    assert (repo_root / "p1").exists()


@pytest.mark.usefixtures("fresh_db")
async def test_provision_after_change_swallows_exceptions_and_publishes_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ensure_workspaces raises (network error, FS issue,
    whatever), the helper must NOT propagate the exception (it's
    fire-and-forget from create_project) — instead emit a failure
    event so the EnvPane / logs surface it."""
    import server.paths as paths_mod
    monkeypatch.setattr(paths_mod, "DATA_ROOT", tmp_path)

    await init_db()
    await _create_project_no_repo("zeta")

    import server.workspaces as ws_mod

    async def _boom(_pid: str) -> dict:
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(ws_mod, "ensure_workspaces", _boom)

    from server.events import bus
    q = bus.subscribe()
    try:
        from server.projects_api import _provision_after_change
        # Must not raise.
        await _provision_after_change(
            project_id="zeta", source="project_updated", actor={"source": "test"},
        )
        captured = _drain_provisioned_events(q)
    finally:
        bus.unsubscribe(q)

    assert len(captured) == 1
    ev = captured[0]
    assert ev["ok"] is False
    assert "simulated network failure" in ev["error"]
    assert ev["source"] == "project_updated"


# ------------------------------------------------------------------
# _ensure_base_clone: URL-refresh behaviour
# ------------------------------------------------------------------


class _FakeGitDir:
    """Minimal stand-in for an already-cloned bare directory.

    Creates `<root>/.git/` so the existence check in
    `_ensure_base_clone` passes, and pre-seeds
    `_clone_locks` / `_read_remote_url` so no real subprocess runs.
    """

    def __init__(self, root: Path, current_url: str) -> None:
        self.root = root
        self.current_url = current_url
        root.mkdir(parents=True, exist_ok=True)
        (root / ".git").mkdir(exist_ok=True)


async def test_url_mismatch_runs_set_url_and_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the bare clone exists but the configured URL has changed,
    _ensure_base_clone must call `git remote set-url` + `git fetch`
    instead of raising."""
    import server.workspaces as ws_mod

    bare = tmp_path / ".project"
    _FakeGitDir(bare, "https://old-url/repo.git")

    calls: list[list[str]] = []

    async def _fake_run(cmd, cwd=None, timeout=120):
        calls.append(cmd)
        if "remote" in cmd and "get-url" in cmd:
            return 0, "https://old-url/repo.git", ""
        # set-url and fetch both succeed
        return 0, "", ""

    monkeypatch.setattr(ws_mod, "_run", _fake_run)

    await ws_mod._ensure_base_clone(bare, "https://new-url/repo.git")

    cmds = [" ".join(c) for c in calls]
    assert any("remote" in c and "set-url" in c for c in cmds), (
        f"expected a set-url call; got: {cmds}"
    )
    assert any("fetch" in c for c in cmds), (
        f"expected a fetch call after set-url; got: {cmds}"
    )


async def test_url_mismatch_set_url_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit from `git remote set-url` must propagate as
    RuntimeError so the caller emits an honest ok=False event."""
    import server.workspaces as ws_mod

    bare = tmp_path / ".project"
    _FakeGitDir(bare, "https://old-url/repo.git")

    async def _fake_run(cmd, cwd=None, timeout=120):
        if "remote" in cmd and "get-url" in cmd:
            return 0, "https://old-url/repo.git", ""
        if "set-url" in cmd:
            return 1, "", "authentication failed"
        return 0, "", ""

    monkeypatch.setattr(ws_mod, "_run", _fake_run)

    with pytest.raises(RuntimeError, match="git remote set-url failed"):
        await ws_mod._ensure_base_clone(bare, "https://new-url/repo.git")


async def test_url_mismatch_fetch_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing `git fetch` after a successful set-url must NOT raise
    (non-fatal — the URL is now correct; push will surface the error)."""
    import server.workspaces as ws_mod

    bare = tmp_path / ".project"
    _FakeGitDir(bare, "https://old-url/repo.git")

    async def _fake_run(cmd, cwd=None, timeout=120):
        if "remote" in cmd and "get-url" in cmd:
            return 0, "https://old-url/repo.git", ""
        if "set-url" in cmd:
            return 0, "", ""
        if "fetch" in cmd:
            return 1, "", "network timeout"
        return 0, "", ""

    monkeypatch.setattr(ws_mod, "_run", _fake_run)

    # Must complete without raising.
    await ws_mod._ensure_base_clone(bare, "https://new-url/repo.git")


async def test_url_unchanged_skips_set_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the existing remote matches the configured URL, no set-url
    or fetch should be issued — the clone is already correct."""
    import server.workspaces as ws_mod

    bare = tmp_path / ".project"
    _FakeGitDir(bare, "https://same-url/repo.git")

    calls: list[list[str]] = []

    async def _fake_run(cmd, cwd=None, timeout=120):
        calls.append(cmd)
        if "remote" in cmd and "get-url" in cmd:
            return 0, "https://same-url/repo.git", ""
        return 0, "", ""

    monkeypatch.setattr(ws_mod, "_run", _fake_run)

    await ws_mod._ensure_base_clone(bare, "https://same-url/repo.git")

    cmds = [" ".join(c) for c in calls]
    assert not any("set-url" in c for c in cmds), (
        f"set-url should not run when URL unchanged; got: {cmds}"
    )
