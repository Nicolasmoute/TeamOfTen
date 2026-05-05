"""v0.3.8 reconciliation sweep tests.

The reconciliation sweep catches the recurring "Player did the work
but the kanban didn't notice" failure mode (p1 / p3 / p8 traces).
Read-only: walks each non-archive task's folder on disk and emits
structured events to Coach when an artifact exists but has no
corresponding kanban record. Does NOT mutate DB rows itself —
Coach uses the existing on_behalf_of override tools.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.events import bus
from server.idle_poller import (
    _reconcile_emitted,
    reconciliation_sweep_once,
)


# ---------------------------------------------------------------- helpers

async def _seed_task(
    *,
    task_id: str,
    spec_path: str | None = None,
    project_id: str = "misc",
    status: str = "execute",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, spec_path) "
            "VALUES (?, ?, 'recon demo', ?, 'p3', 'coach', "
            "'[{\"stage\":\"plan\",\"to\":[]},"
            "{\"stage\":\"execute\",\"to\":[]},"
            "{\"stage\":\"audit_semantics\",\"to\":[]}]', ?)",
            (task_id, project_id, status, spec_path),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_role(
    *,
    task_id: str,
    role: str,
    owner: str,
    report_path: str | None = None,
    completed: bool = False,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, assigned_at, "
            "claimed_at, completed_at, report_path) "
            "VALUES (?, ?, '[]', ?, '2026-05-06T00:00:00Z', "
            "'2026-05-06T00:00:00Z', ?, ?)",
            (
                task_id, role, owner,
                "2026-05-06T01:00:00Z" if completed else None,
                report_path,
            ),
        )
        await c.commit()
    finally:
        await c.close()


def _drain(queue: Any) -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except Exception:
            break
    return out


def _write_spec_to_disk(project_id: str, task_id: str, body: str) -> None:
    """Mirror the Coach-side write that lands `spec.md` on the per-
    project working folder. Does NOT touch the DB — that's the
    failure mode the reconciliation sweep catches."""
    from server.tasks import spec_path
    target = spec_path(project_id, task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def _write_audit_to_disk(
    project_id: str, task_id: str, round_num: int, kind: str, body: str,
) -> None:
    from server.tasks import audit_report_path
    target = audit_report_path(project_id, task_id, round_num, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_dedupe() -> None:
    """The reconciliation dedupe is process-level; clear between
    tests so each test starts with a fresh emit window."""
    _reconcile_emitted.clear()


# ---------------------------------------------------------------- spec unrecorded

async def test_spec_on_disk_but_unrecorded_emits_event(
    fresh_db: str,
) -> None:
    """The exact production trace: a Player wrote `spec.md` to disk
    but couldn't reach `coord_write_task_spec` so the kanban row's
    `spec_path` stayed NULL. The sweep emits `task_spec_unrecorded`
    routed to Coach so they can submit on the Player's behalf."""
    await init_db()
    task_id = "t-2026-05-06-00000001"
    await _seed_task(task_id=task_id, status="plan")
    await _seed_role(task_id=task_id, role="planner", owner="p5")
    _write_spec_to_disk("misc", task_id, "## Goal\nDo the thing.\n")

    queue = bus.subscribe()
    try:
        n = await reconciliation_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    assert n >= 1
    spec_evts = [e for e in events if e.get("type") == "task_spec_unrecorded"]
    assert len(spec_evts) == 1, events
    ev = spec_evts[0]
    assert ev["task_id"] == task_id
    assert ev["spec_path"].endswith("spec.md")
    assert ev["planner"] == "p5"
    assert ev["to"] == "coach"


async def test_spec_unrecorded_dedupes_within_ttl(
    fresh_db: str,
) -> None:
    """Repeated sweeps within the dedupe TTL don't re-emit the same
    finding. Without dedup, the sweeper would spam Coach every 5 min
    until Coach acted."""
    await init_db()
    task_id = "t-2026-05-06-00000002"
    await _seed_task(task_id=task_id, status="plan")
    _write_spec_to_disk("misc", task_id, "## body\n")

    await reconciliation_sweep_once()  # first emit
    queue = bus.subscribe()
    try:
        n = await reconciliation_sweep_once()  # second within TTL
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    assert n == 0
    assert not any(
        e.get("type") == "task_spec_unrecorded" for e in events
    )


async def test_spec_recorded_path_is_silent(
    fresh_db: str,
) -> None:
    """When `tasks.spec_path` is set (the spec was submitted via
    `coord_write_task_spec`), the file is NOT a finding even if it
    sits on disk."""
    await init_db()
    task_id = "t-2026-05-06-00000003"
    rel = f"projects/misc/working/tasks/{task_id}/spec.md"
    await _seed_task(task_id=task_id, status="execute", spec_path=rel)
    _write_spec_to_disk("misc", task_id, "## body\n")

    queue = bus.subscribe()
    try:
        await reconciliation_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    assert not any(
        e.get("type") == "task_spec_unrecorded" for e in events
    )


# ---------------------------------------------------------------- audit unrecorded

async def test_audit_on_disk_but_unrecorded_emits_event(
    fresh_db: str,
) -> None:
    """Mirror of the spec case — Theo's (p3) production trace where
    a semantic auditor wrote `audit_1_semantics.md` but couldn't
    reach `coord_submit_audit_report`. The sweep emits
    `task_audit_unrecorded` so Coach can submit on their behalf."""
    await init_db()
    task_id = "t-2026-05-06-00000004"
    await _seed_task(task_id=task_id, status="audit_semantics")
    await _seed_role(
        task_id=task_id, role="auditor_semantics", owner="p3",
    )
    _write_audit_to_disk(
        "misc", task_id, 1, "semantics", "## Findings\nLooks good.\n",
    )

    queue = bus.subscribe()
    try:
        await reconciliation_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)

    audit_evts = [e for e in events if e.get("type") == "task_audit_unrecorded"]
    assert len(audit_evts) == 1, events
    ev = audit_evts[0]
    assert ev["task_id"] == task_id
    assert ev["kind"] == "semantics"
    assert ev["round"] == 1
    assert ev["report_path"].endswith("audit_1_semantics.md")
    assert ev["auditor"] == "p3"
    assert ev["to"] == "coach"


async def test_audit_recorded_via_role_row_path_is_silent(
    fresh_db: str,
) -> None:
    """When the auditor role row already records the report_path
    (i.e. the audit went through `coord_submit_audit_report`), the
    file is NOT a finding."""
    await init_db()
    task_id = "t-2026-05-06-00000005"
    rel = (
        f"projects/misc/working/tasks/{task_id}/audits/audit_1_semantics.md"
    )
    await _seed_task(task_id=task_id, status="ship")
    await _seed_role(
        task_id=task_id, role="auditor_semantics",
        owner="p3", report_path=rel, completed=True,
    )
    _write_audit_to_disk("misc", task_id, 1, "semantics", "## body\n")

    queue = bus.subscribe()
    try:
        await reconciliation_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    assert not any(
        e.get("type") == "task_audit_unrecorded" for e in events
    )


async def test_malformed_audit_filename_ignored(
    fresh_db: str, tmp_path: Path,
) -> None:
    """Files in the audits/ dir that don't match `audit_<N>_<kind>.md`
    are not findings (typos, drafts, READMEs). Without this filter
    the sweep would spam Coach for every weird file."""
    await init_db()
    task_id = "t-2026-05-06-00000006"
    await _seed_task(task_id=task_id, status="audit_semantics")
    # Drop two files: one canonical (would emit), one weird (must not).
    from server.tasks import audits_dir
    d = audits_dir("misc", task_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "draft_notes.md").write_text("scratch\n", encoding="utf-8")
    (d / "audit_X_syntax.md").write_text("scratch\n", encoding="utf-8")

    queue = bus.subscribe()
    try:
        await reconciliation_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    audit_evts = [e for e in events if e.get("type") == "task_audit_unrecorded"]
    assert audit_evts == []


# ---------------------------------------------------------------- archive bypass

async def test_archived_tasks_skipped(
    fresh_db: str,
) -> None:
    """Archived tasks are skipped — the sweep only walks active
    tasks. Otherwise stale on-disk artifacts under archived tasks
    would generate findings forever."""
    await init_db()
    task_id = "t-2026-05-06-00000007"
    await _seed_task(task_id=task_id, status="archive")
    _write_spec_to_disk("misc", task_id, "## body\n")

    queue = bus.subscribe()
    try:
        n = await reconciliation_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    assert n == 0
    assert not any(
        e.get("type") == "task_spec_unrecorded" for e in events
    )


# ---------------------------------------------------------------- feature flag

async def test_coach_rollup_drops_stale_spec_finding_after_fix(
    fresh_db: str,
) -> None:
    """AUDIT FIX (medium): once Coach has submitted via
    `coord_write_task_spec(on_behalf_of=...)`, `tasks.spec_path` is
    set. The events table still has the old `task_spec_unrecorded`
    finding, but Coach's rollup must drop it — otherwise Coach sees
    a stale finding and re-attempts the override.
    """
    from server.agents import _build_unrecorded_artifacts_rows
    await init_db()
    task_id = "t-2026-05-06-00000020"
    rel = f"projects/misc/working/tasks/{task_id}/spec.md"
    # Stage 1: artifact unrecorded — emit the event.
    await _seed_task(task_id=task_id, status="plan")
    _write_spec_to_disk("misc", task_id, "## body\n")
    await reconciliation_sweep_once()
    # Stage 2: Coach submits via override. spec_path is set.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE tasks SET spec_path = ? WHERE id = ?",
            (rel, task_id),
        )
        await c.commit()
    finally:
        await c.close()
    # The event is still in the table from stage 1.
    rows = await _build_unrecorded_artifacts_rows("misc")
    # The rollup must NOT include this finding.
    assert all(r["task_id"] != task_id for r in rows), rows


async def test_coach_rollup_drops_stale_audit_finding_after_fix(
    fresh_db: str,
) -> None:
    """Same shape for audits. After the override submits via
    `coord_submit_audit_report(on_behalf_of=...)`, the role row
    records `report_path`. The rollup cross-checks via the role
    row and drops the stale finding."""
    from server.agents import _build_unrecorded_artifacts_rows
    await init_db()
    task_id = "t-2026-05-06-00000021"
    rel = (
        f"projects/misc/working/tasks/{task_id}/audits/"
        f"audit_1_semantics.md"
    )
    # Stage 1: artifact unrecorded — emit the event.
    await _seed_task(task_id=task_id, status="audit_semantics")
    await _seed_role(
        task_id=task_id, role="auditor_semantics", owner="p3",
    )
    _write_audit_to_disk("misc", task_id, 1, "semantics", "## body\n")
    await reconciliation_sweep_once()
    # Stage 2: Coach submits via override; the role row records
    # report_path.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE task_role_assignments "
            "SET report_path = ?, completed_at = '2026-05-06T01:00:00Z' "
            "WHERE task_id = ? AND role = 'auditor_semantics'",
            (rel, task_id),
        )
        await c.commit()
    finally:
        await c.close()
    rows = await _build_unrecorded_artifacts_rows("misc")
    assert all(r["task_id"] != task_id for r in rows), rows


async def test_feature_flag_disables_sweep(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_KANBAN_RECONCILE_ENABLED", "false")
    await init_db()
    task_id = "t-2026-05-06-00000008"
    await _seed_task(task_id=task_id, status="plan")
    _write_spec_to_disk("misc", task_id, "## body\n")

    queue = bus.subscribe()
    try:
        n = await reconciliation_sweep_once()
        await asyncio.sleep(0.05)
        events = _drain(queue)
    finally:
        bus.unsubscribe(queue)
    assert n == 0
    assert events == []
