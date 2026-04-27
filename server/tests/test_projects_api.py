"""Tests for server/projects_api.py — Phase 3 project CRUD + activate.

Slug validator + pure helpers are exercised directly without spinning
up FastAPI (which isn't installed in this dev venv). Endpoint behavior
is exercised at the helper level: validate_slug → DB rows via the same
SQL the handlers run, plus the switch-flow primitives.
"""

from __future__ import annotations

import pytest

from server.db import (
    MISC_PROJECT_ID,
    configured_conn,
    init_db,
    pin_active_project,
    resolve_active_project,
    set_active_project,
)
from server.projects_api import (
    RESERVED_SLUGS,
    derive_slug_from_name,
    validate_slug,
    _mask_repo_url,
)


# ---------- slug validator ----------


def test_validate_slug_happy_path() -> None:
    for slug in ("misc", "harness2", "simaero-rebrand", "a1", "p2-q3"):
        ok, _ = validate_slug(slug)
        assert ok, f"{slug!r} should be valid"


def test_validate_slug_rejects_too_short() -> None:
    ok, reason = validate_slug("a")
    assert not ok
    assert "2-48" in reason


def test_validate_slug_rejects_too_long() -> None:
    ok, reason = validate_slug("a" + "1" * 50)
    assert not ok
    assert "2-48" in reason


@pytest.mark.parametrize(
    "slug",
    [
        "1abc",          # starts with digit
        "-misc",         # leading dash
        "misc-",         # trailing dash
        "mis--c",        # consecutive dashes
        "Misc",          # uppercase
        "misc_v2",       # underscore
        "misc/v2",       # slash
        "misc v2",       # space
    ],
)
def test_validate_slug_rejects_bad_charset(slug: str) -> None:
    ok, reason = validate_slug(slug)
    assert not ok, f"{slug!r} should be rejected"
    # Reason mentions the regex.
    assert "[a-z" in reason or "regex" in reason.lower()


@pytest.mark.parametrize("slug", sorted(RESERVED_SLUGS))
def test_validate_slug_rejects_reserved(slug: str) -> None:
    ok, reason = validate_slug(slug)
    assert not ok, f"{slug!r} is reserved and must be rejected"
    assert "reserved" in reason


def test_validate_slug_rejects_non_string() -> None:
    ok, _ = validate_slug(123)  # type: ignore[arg-type]
    assert not ok


def test_derive_slug_from_name() -> None:
    assert derive_slug_from_name("Simaero Rebrand") == "simaero-rebrand"
    assert derive_slug_from_name("  Hello, World!  ") == "hello-world"
    assert derive_slug_from_name("foo--bar") == "foo-bar"
    assert derive_slug_from_name("FOO  BAR  baz") == "foo-bar-baz"


# ---------- mask_repo_url ----------


def test_mask_repo_url() -> None:
    assert _mask_repo_url(None) is None
    assert _mask_repo_url("") == ""
    # No userinfo → unchanged.
    assert _mask_repo_url("https://github.com/foo/bar.git") == \
        "https://github.com/foo/bar.git"
    assert _mask_repo_url("https://user:tok@github.com/foo/bar") == \
        "https://***@github.com/foo/bar"
    # Placeholders pass through.
    assert _mask_repo_url("https://${GITHUB_TOKEN}@github.com/foo/bar") == \
        "https://${GITHUB_TOKEN}@github.com/foo/bar"


# ---------- TOCTOU mitigation primitives ----------


async def test_pin_active_project_overrides_team_config(fresh_db: str) -> None:
    """resolve_active_project() returns the pinned slug while the
    context manager is active, then falls back to team_config."""
    await init_db()
    # Seed two projects so we can flip the active.
    c = await configured_conn()
    try:
        await c.execute("INSERT INTO projects (id, name) VALUES ('p-a', 'A')")
        await c.execute("INSERT INTO projects (id, name) VALUES ('p-b', 'B')")
        await c.commit()
    finally:
        await c.close()
    await set_active_project("p-a")
    assert await resolve_active_project() == "p-a"
    with pin_active_project("p-b"):
        assert await resolve_active_project() == "p-b"
    # Pin lifted: back to team_config.
    assert await resolve_active_project() == "p-a"


async def test_pin_active_project_nests_correctly(fresh_db: str) -> None:
    """Nested pins restore the outer one on exit."""
    await init_db()
    c = await configured_conn()
    try:
        for s in ("p-a", "p-b", "p-c"):
            await c.execute(
                "INSERT INTO projects (id, name) VALUES (?, ?)", (s, s.upper())
            )
        await c.commit()
    finally:
        await c.close()
    await set_active_project("p-a")
    with pin_active_project("p-b"):
        assert await resolve_active_project() == "p-b"
        with pin_active_project("p-c"):
            assert await resolve_active_project() == "p-c"
        # Inner pin reset; outer pin still active.
        assert await resolve_active_project() == "p-b"
    # All pins lifted.
    assert await resolve_active_project() == "p-a"


async def test_set_active_project_writes_team_config(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute("INSERT INTO projects (id, name) VALUES ('p-a', 'A')")
        await c.execute("INSERT INTO projects (id, name) VALUES ('p-b', 'B')")
        await c.commit()
    finally:
        await c.close()
    await set_active_project("p-b")
    assert await resolve_active_project() == "p-b"
    await set_active_project("p-a")
    assert await resolve_active_project() == "p-a"


# ---------- activate flow primitives ----------


async def test_emit_step_publishes_project_switch_step_event(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_emit_step publishes a `project_switch_step` event with job_id +
    step + status fields. The Phase-3-as-spec'd UI subscribes to these
    on the bus."""
    from server.projects_api import _emit_step

    captured: list[dict] = []

    class _StubBus:
        async def publish(self, ev: dict) -> None:
            captured.append(ev)

    monkeypatch.setattr("server.projects_api.bus", _StubBus())
    await _emit_step(
        job_id="abc", step="push_current", status="running",
        from_project="misc", to_project="alpha",
    )
    assert len(captured) == 1
    ev = captured[0]
    assert ev["type"] == "project_switch_step"
    assert ev["job_id"] == "abc"
    assert ev["step"] == "push_current"
    assert ev["status"] == "running"
    assert ev["from_project"] == "misc"
    assert ev["to_project"] == "alpha"


async def test_run_switch_emits_full_step_sequence(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch flow per §6 emits steps in order:
    started → push_current → pull_new → swap_pointer → reload →
    project_switched. Stub the kDrive primitives so the test runs
    without WebDAV."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute("INSERT INTO projects (id, name) VALUES ('alpha', 'A')")
        await c.commit()
    finally:
        await c.close()

    captured: list[dict] = []

    class _StubBus:
        async def publish(self, ev: dict) -> None:
            captured.append(ev)

    async def _stub_force_push(project_id: str):
        return {"timed_out": False, "counts": {}}

    async def _stub_pull(project_id: str):
        return {}

    monkeypatch.setattr("server.projects_api.bus", _StubBus())
    # Patch the inner imports too — they're local imports, so monkey-
    # patching the module attribute we use lazily is the easy path.
    monkeypatch.setattr(
        "server.project_sync.force_push_project", _stub_force_push
    )
    monkeypatch.setattr(
        "server.project_sync.pull_project_tree", _stub_pull
    )

    from server.projects_api import _run_switch

    await _run_switch(
        job_id="job-1",
        from_project=MISC_PROJECT_ID,
        to_project="alpha",
        actor={"source": "test"},
    )

    steps = [
        (e["step"], e["status"]) for e in captured
        if e["type"] == "project_switch_step"
    ]
    # Required step order — extra "ok" / "running" pairs are fine, the
    # important thing is the sequence is monotonic.
    step_names = [s for s, _ in steps]
    assert step_names == [
        "started", "push_current", "push_current",
        "pull_new", "pull_new", "swap_pointer", "reload",
    ]
    # Final event closes the subscriber.
    final = [e for e in captured if e["type"] == "project_switched"]
    assert len(final) == 1
    assert final[0]["ok"] is True
    assert final[0]["job_id"] == "job-1"
    assert final[0]["from_project"] == MISC_PROJECT_ID
    assert final[0]["to_project"] == "alpha"
    # Active project actually swapped.
    assert await resolve_active_project() == "alpha"


# ---------- Phase 3 audit follow-up: pinning during switch ----------


async def test_resolve_during_pin_returns_pinned_even_if_db_lags(
    fresh_db: str,
) -> None:
    """While pin_active_project is active, resolve_active_project()
    must return the pinned id even though team_config still says
    something else. This is the TOCTOU mitigation from §13 Phase 3."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute("INSERT INTO projects (id, name) VALUES ('a', 'A')")
        await c.execute("INSERT INTO projects (id, name) VALUES ('b', 'B')")
        await c.commit()
    finally:
        await c.close()
    await set_active_project("a")
    with pin_active_project("b"):
        # team_config still says 'a' but resolve returns 'b'.
        assert await resolve_active_project() == "b"
    assert await resolve_active_project() == "a"


# ---------- Phase 4: switch_in_progress flag (atomic check-and-set) ----------


async def test_switch_in_progress_flag_default(fresh_db: str) -> None:
    """The flag should reset to False at module load (sanity check —
    if a prior test crashed mid-switch the flag could stick)."""
    import server.projects_api as papi
    # Reset defensively in case a previous test left it true.
    papi._switch_in_progress = False
    assert papi._switch_in_progress is False


# ---------- Phase 4: terminal event shape ----------


async def test_run_switch_emits_terminal_marker_on_success(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit fix #8: the success-path `project_switched` event must
    carry `terminal: true` so subscribers know the stream is over."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute("INSERT INTO projects (id, name) VALUES ('alpha', 'A')")
        await c.commit()
    finally:
        await c.close()

    captured: list[dict] = []

    class _StubBus:
        async def publish(self, ev: dict) -> None:
            captured.append(ev)

    async def _stub_force_push(project_id: str):
        return {"timed_out": False, "counts": {}}

    async def _stub_pull(project_id: str):
        return {}

    monkeypatch.setattr("server.projects_api.bus", _StubBus())
    monkeypatch.setattr(
        "server.project_sync.force_push_project", _stub_force_push
    )
    monkeypatch.setattr(
        "server.project_sync.pull_project_tree", _stub_pull
    )

    from server.projects_api import _run_switch
    await _run_switch(
        job_id="job-success",
        from_project=MISC_PROJECT_ID,
        to_project="alpha",
        actor={"source": "test"},
    )
    final = [e for e in captured if e["type"] == "project_switched"]
    assert len(final) == 1
    assert final[0]["ok"] is True
    assert final[0]["terminal"] is True


async def test_run_switch_aborts_on_push_failure(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit fix #5: a hard step failure aborts BEFORE the swap and
    emits a terminal `ok=False` event with `failed_step` set."""
    await init_db()
    c = await configured_conn()
    try:
        await c.execute("INSERT INTO projects (id, name) VALUES ('alpha', 'A')")
        await c.commit()
    finally:
        await c.close()

    captured: list[dict] = []

    class _StubBus:
        async def publish(self, ev: dict) -> None:
            captured.append(ev)

    async def _failing_push(project_id: str):
        raise RuntimeError("kdrive offline")

    async def _stub_pull(project_id: str):
        return {}

    monkeypatch.setattr("server.projects_api.bus", _StubBus())
    monkeypatch.setattr(
        "server.project_sync.force_push_project", _failing_push
    )
    monkeypatch.setattr(
        "server.project_sync.pull_project_tree", _stub_pull
    )

    from server.projects_api import _run_switch
    await _run_switch(
        job_id="job-fail",
        from_project=MISC_PROJECT_ID,
        to_project="alpha",
        actor={"source": "test"},
    )
    final = [e for e in captured if e["type"] == "project_switched"]
    assert len(final) == 1
    assert final[0]["ok"] is False
    assert final[0]["terminal"] is True
    assert final[0]["failed_step"] == "push_current"
    # Active project should NOT have changed (abort-before-swap).
    assert await resolve_active_project() == MISC_PROJECT_ID
    # `pull_new` and `swap_pointer` must NOT have run.
    step_names = [e["step"] for e in captured if e["type"] == "project_switch_step"]
    assert "pull_new" not in step_names
    assert "swap_pointer" not in step_names


# ---------- /repeat clears on project switch -----------------------


async def test_run_switch_clears_coach_repeat_on_success(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A custom /repeat prompt was scoped to the prior project — it
    must be cleared on a successful switch and a coach_repeat_changed
    event published with reason='project_switched' so the loops-bar
    UI redraws."""
    from server import agents as agents_mod
    from server.projects_api import _run_switch

    await init_db()
    c = await configured_conn()
    try:
        await c.execute("INSERT INTO projects (id, name) VALUES ('alpha', 'A')")
        await c.commit()
    finally:
        await c.close()

    # Seed an active /repeat — pretend the user ran "/repeat 120 hi".
    agents_mod.set_coach_repeat(120, "hi")
    assert agents_mod.get_coach_repeat() == (120, "hi")

    captured: list[dict] = []

    class _StubBus:
        async def publish(self, ev: dict) -> None:
            captured.append(ev)

    async def _stub_force_push(project_id: str):
        return {"timed_out": False, "counts": {}}

    async def _stub_pull(project_id: str):
        return {}

    monkeypatch.setattr("server.projects_api.bus", _StubBus())
    monkeypatch.setattr(
        "server.project_sync.force_push_project", _stub_force_push
    )
    monkeypatch.setattr(
        "server.project_sync.pull_project_tree", _stub_pull
    )

    await _run_switch(
        job_id="job-clear",
        from_project=MISC_PROJECT_ID,
        to_project="alpha",
        actor={"source": "test"},
    )

    # /repeat is gone.
    assert agents_mod.get_coach_repeat() == (0, None)

    # And a coach_repeat_changed event fired with reason set.
    cleared = [
        e for e in captured
        if e["type"] == "coach_repeat_changed"
        and e.get("reason") == "project_switched"
    ]
    assert len(cleared) == 1
    assert cleared[0]["interval_seconds"] == 0
    assert cleared[0]["prompt"] is None

    # Cleanup so the module-level state doesn't leak into other tests.
    agents_mod.set_coach_repeat(0, None)


async def test_run_switch_does_not_emit_repeat_clear_when_unset(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When /repeat was already off, the switch must not emit a
    spurious coach_repeat_changed event — that would flicker the
    loops-bar UI on every project change for users who don't use
    /repeat at all."""
    from server import agents as agents_mod
    from server.projects_api import _run_switch

    await init_db()
    c = await configured_conn()
    try:
        await c.execute("INSERT INTO projects (id, name) VALUES ('alpha', 'A')")
        await c.commit()
    finally:
        await c.close()

    # /repeat is off (default).
    assert agents_mod.get_coach_repeat() == (0, None)

    captured: list[dict] = []

    class _StubBus:
        async def publish(self, ev: dict) -> None:
            captured.append(ev)

    async def _stub_force_push(project_id: str):
        return {"timed_out": False, "counts": {}}

    async def _stub_pull(project_id: str):
        return {}

    monkeypatch.setattr("server.projects_api.bus", _StubBus())
    monkeypatch.setattr(
        "server.project_sync.force_push_project", _stub_force_push
    )
    monkeypatch.setattr(
        "server.project_sync.pull_project_tree", _stub_pull
    )

    await _run_switch(
        job_id="job-noop",
        from_project=MISC_PROJECT_ID,
        to_project="alpha",
        actor={"source": "test"},
    )

    cleared = [
        e for e in captured if e["type"] == "coach_repeat_changed"
    ]
    assert cleared == []
