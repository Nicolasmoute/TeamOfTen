"""Audit tests for the v2 Coach prompt blocks (Docs/kanban-specs-v2.md
§9.3, §11.1, §11.2, §11.3, §14.1).

Each builder returns markdown when there's something to render, "" when
the team is healthy / no relevant data exists. Coverage:
  - _build_player_health_block (counters, empty-state)
  - _build_audit_aggregator_rows (with summary read, empty-state)
  - _build_recent_patterns_block (repeat audit fails, deviations spike,
    empty-state)
  - _build_recent_events_block (cap, overflow footer, surfaced_event_ids
    population, empty-state)
  - _stamp_events_read_by_coach (idempotent, only stamps unread rows)
  - _build_coach_coordination_block surfaces the §14.1 lifecycle policy
"""

from __future__ import annotations

import json
from typing import Any

from server.agents import (
    ACTIVE_TASK_HEALTH_CAP,
    _build_active_task_health_rows,
    _build_audit_aggregator_rows,
    _build_coach_coordination_block,
    _build_player_health_block,
    _build_recent_events_block,
    _build_recent_patterns_block,
    _stamp_events_read_by_coach,
)
from server.db import configured_conn, init_db


async def _seed_task(
    *,
    task_id: str,
    title: str = "demo",
    status: str = "execute",
    owner: str | None = "p2",
    project: str = "misc",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) "
            "VALUES (?, ?, ?, ?, ?, 'coach', '[]')",
            (task_id, project, title, status, owner),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_role_row(
    *,
    task_id: str,
    role: str = "auditor_syntax",
    owner: str | None = "p4",
    verdict: str | None = None,
    completed_at: str | None = None,
    report_path: str | None = None,
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO task_role_assignments "
            "(task_id, role, eligible_owners, owner, "
            " assigned_at, claimed_at, completed_at, verdict, report_path) "
            "VALUES (?, ?, '[]', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?, ?)",
            (task_id, role, owner, completed_at, verdict, report_path),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_deviation(
    *,
    task_id: str,
    executor: str = "p2",
    noticed_at: str = "audit",
    project: str = "misc",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO deviations_log "
            "(project_id, task_id, executor, noticed_at, description) "
            "VALUES (?, ?, ?, ?, 'demo')",
            (project, task_id, executor, noticed_at),
        )
        await c.commit()
    finally:
        await c.close()


async def _seed_project_event(
    *,
    actor: str = "p2",
    type: str = "commit_pushed",
    task_id: str | None = None,
    project: str = "misc",
    payload_pointer: str | None = None,
    read_by_coach_at: str | None = None,
) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO project_events "
            "(project_id, actor, type, task_id, payload_json, "
            " payload_pointer, read_by_coach_at) "
            "VALUES (?, ?, ?, ?, '{}', ?, ?)",
            (project, actor, type, task_id, payload_pointer, read_by_coach_at),
        )
        await c.commit()
        return int(cur.lastrowid)
    finally:
        await c.close()


# ---------------------------------------------------------------------
# _build_player_health_block (§11.1)
# ---------------------------------------------------------------------

async def test_player_health_empty_returns_empty_string(fresh_db: str) -> None:
    await init_db()
    out = await _build_player_health_block("misc")
    assert out == ""


async def test_player_health_renders_deviations(fresh_db: str) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-07-aaaa1111", owner="p2")
    # Two FAIL rounds for p2.
    await _seed_role_row(
        task_id="t-2026-05-07-aaaa1111", role="auditor_syntax",
        owner="p4", verdict="fail",
        completed_at="2026-05-07T01:00:00Z",
    )
    await _seed_role_row(
        task_id="t-2026-05-07-aaaa1111", role="auditor_syntax",
        owner="p4", verdict="fail",
        completed_at="2026-05-07T02:00:00Z",
    )
    out = await _build_player_health_block("misc")
    assert "## Player health" in out
    assert "p2" in out
    # Two failing audit rounds for the same task = 2.
    assert "| 2 " in out or "| 2  " in out


async def test_player_health_renders_off_spec_completions(fresh_db: str) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-07-bbbb2222", owner="p3")
    await _seed_deviation(
        task_id="t-2026-05-07-bbbb2222",
        executor="p3",
        noticed_at="audit",
    )
    out = await _build_player_health_block("misc")
    assert "## Player health" in out
    assert "p3" in out


async def test_player_health_skips_zero_rows(fresh_db: str) -> None:
    """A Player whose three counters are all zero should not appear in
    the table — keeps the prompt quiet on a healthy team."""
    await init_db()
    await _seed_task(task_id="t-2026-05-07-cccc3333", owner="p2")
    await _seed_role_row(
        task_id="t-2026-05-07-cccc3333", role="auditor_syntax",
        owner="p4", verdict="fail",
        completed_at="2026-05-07T01:00:00Z",
    )
    out = await _build_player_health_block("misc")
    assert "p2" in out
    # Only p2 has a non-zero counter — no other slot listed.
    for slot in ("p1", "p3", "p5", "p7", "p9"):
        assert f"| {slot} " not in out


# ---------------------------------------------------------------------
# _build_audit_aggregator_rows (§11.2)
# ---------------------------------------------------------------------

async def test_audit_aggregator_empty_when_no_audit_history(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-07-dddd4444", status="execute")
    out = await _build_audit_aggregator_rows("misc")
    assert out == ""


async def test_audit_aggregator_renders_per_round(fresh_db: str) -> None:
    await init_db()
    await _seed_task(
        task_id="t-2026-05-07-dddd4444",
        status="audit_semantics",
        owner="p2",
    )
    await _seed_role_row(
        task_id="t-2026-05-07-dddd4444", role="auditor_syntax",
        owner="p4", verdict="fail",
        completed_at="2026-05-07T01:00:00Z",
    )
    await _seed_role_row(
        task_id="t-2026-05-07-dddd4444", role="auditor_syntax",
        owner="p4", verdict="pass",
        completed_at="2026-05-07T02:00:00Z",
    )
    await _seed_role_row(
        task_id="t-2026-05-07-dddd4444", role="auditor_semantics",
        owner="p7",  # active, no verdict yet
    )
    out = await _build_audit_aggregator_rows("misc")
    assert "## Audit history" in out
    assert "t-2026-05-07-dddd4444" in out
    assert "syntax round 1" in out and "FAIL" in out
    assert "syntax round 2" in out and "PASS" in out
    assert "semantic round 1" in out and "pending" in out


# ---------------------------------------------------------------------
# _build_recent_patterns_block (§11.3)
# ---------------------------------------------------------------------

async def test_recent_patterns_empty_returns_empty(fresh_db: str) -> None:
    await init_db()
    out = await _build_recent_patterns_block("misc")
    assert out == ""


async def test_recent_patterns_flags_repeat_audit_fails(fresh_db: str) -> None:
    await init_db()
    await _seed_task(task_id="t-2026-05-07-eeee5555", owner="p2")
    # Two same-kind FAILs in the window.
    for i in range(2):
        await _seed_role_row(
            task_id="t-2026-05-07-eeee5555",
            role="auditor_syntax",
            owner="p4",
            verdict="fail",
            # Use SQLite default (now()) by setting completed_at to
            # the format SQLite produces for `datetime('now')`.
            completed_at=None,
        )
    # Manually stamp `completed_at` to "now" for both rows so the
    # window filter picks them up.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE task_role_assignments SET completed_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE task_id = ? AND role = 'auditor_syntax'",
            ("t-2026-05-07-eeee5555",),
        )
        await c.commit()
    finally:
        await c.close()
    out = await _build_recent_patterns_block("misc")
    assert "## Recent patterns" in out
    assert "t-2026-05-07-eeee5555" in out
    assert "syntax fail" in out


# ---------------------------------------------------------------------
# _build_recent_events_block (§9.3) + surfaced_event_ids
# ---------------------------------------------------------------------

async def test_recent_events_empty_returns_empty(
    fresh_db: str, monkeypatch
) -> None:
    """Even on a fresh DB the migration inserts a kanban_v2_cutover row,
    so we override that to get a true empty state."""
    await init_db()
    # Mark every project_events row as read so the unread-tail returns nothing.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE project_events SET read_by_coach_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        )
        await c.commit()
    finally:
        await c.close()
    surfaced: list[int] = []
    out = await _build_recent_events_block("misc", surfaced)
    assert out == ""
    assert surfaced == []


async def test_recent_events_returns_unread_and_populates_ids(
    fresh_db: str,
) -> None:
    await init_db()
    # Mark migration cutover row as read so we see only fresh rows.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE project_events SET read_by_coach_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        )
        await c.commit()
    finally:
        await c.close()
    e1 = await _seed_project_event(
        actor="p2", type="commit_pushed",
        task_id="t-2026-05-07-aaaa1111",
        payload_pointer="abc1234",
    )
    e2 = await _seed_project_event(
        actor="compass", type="compass_audit",
        task_id="t-2026-05-07-aaaa1111",
        payload_pointer="aligned",
    )
    surfaced: list[int] = []
    out = await _build_recent_events_block("misc", surfaced)
    assert "## Recent events" in out
    assert "p2 commit_pushed" in out
    assert "compass compass_audit" in out
    assert sorted(surfaced) == sorted([e1, e2])


async def test_recent_events_overflow_footer(
    fresh_db: str, monkeypatch
) -> None:
    await init_db()
    monkeypatch.setenv("HARNESS_PROJECT_EVENTS_PER_TICK", "3")
    # Mark migration cutover row read first.
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE project_events SET read_by_coach_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        )
        await c.commit()
    finally:
        await c.close()
    for i in range(5):
        await _seed_project_event(
            actor="system",
            type="task_stage_changed",
            task_id=f"t-2026-05-07-{i:08x}",
        )
    surfaced: list[int] = []
    out = await _build_recent_events_block("misc", surfaced)
    assert "## Recent events" in out
    # Cap of 3 → 2 older unread.
    assert "+ 2 older unread events" in out
    assert len(surfaced) == 3


# ---------------------------------------------------------------------
# _stamp_events_read_by_coach
# ---------------------------------------------------------------------

async def test_stamp_events_idempotent(fresh_db: str) -> None:
    await init_db()
    eid = await _seed_project_event(
        actor="p2", type="commit_pushed",
    )
    await _stamp_events_read_by_coach([eid])
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT read_by_coach_at FROM project_events WHERE id = ?",
            (eid,),
        )
        first = dict(await cur.fetchone())["read_by_coach_at"]
    finally:
        await c.close()
    assert first is not None
    # Re-call should NOT update the column (only stamps unread rows).
    await _stamp_events_read_by_coach([eid])
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT read_by_coach_at FROM project_events WHERE id = ?",
            (eid,),
        )
        second = dict(await cur.fetchone())["read_by_coach_at"]
    finally:
        await c.close()
    assert second == first  # unchanged on second stamp


async def test_stamp_events_empty_list_noop(fresh_db: str) -> None:
    await init_db()
    # Should not throw.
    await _stamp_events_read_by_coach([])


# ---------------------------------------------------------------------
# _build_coach_coordination_block — trimmed structure (2026-05-11)
# ---------------------------------------------------------------------

async def test_coordination_block_drops_lifecycle_policy(
    fresh_db: str,
) -> None:
    """2026-05-11: `## Lifecycle policy` was dropped from the per-turn
    coordination block. Its content (kanban v2 rules, deviation tagging,
    plan-mode policy, archival, Compass-verdict guidance) now lives
    exclusively in the project CLAUDE.md (auto-loaded via SDK
    setting_sources for Claude turns; manually injected for Codex).
    The block also drops `## Trajectory examples` for the same reason."""
    await init_db()
    surfaced: list[int] = []
    body = await _build_coach_coordination_block(surfaced_event_ids=surfaced)
    assert "## Lifecycle policy" not in body
    assert "## Trajectory examples" not in body
    # Recent events still surfaces — most load-bearing section.
    assert "## Recent events" in body
    assert len(surfaced) >= 1


async def test_coordination_block_section_ordering(fresh_db: str) -> None:
    """Ordering of the remaining always-on sections post-2026-05-11
    trim — Coordinating header → Current state → Recent events. Player
    health / Active task health / Stalled tasks / Soft stalls /
    Recent patterns are all conditional and order between themselves
    when they fire, but those tests live separately."""
    await init_db()
    surfaced: list[int] = []
    body = await _build_coach_coordination_block(surfaced_event_ids=surfaced)
    headers_in_order = [
        "## Coordinating:",
        "## Current state",
        "## Recent events",  # cutover event always present
    ]
    last_pos = -1
    for h in headers_in_order:
        pos = body.find(h)
        assert pos != -1, f"missing header: {h}"
        assert pos > last_pos, (
            f"header {h!r} appears at {pos}, before previous (at {last_pos})"
        )
        last_pos = pos
    # Dropped sections must NOT appear anywhere.
    assert "## Team composition" not in body
    assert "## Trajectory examples" not in body
    assert "## Lifecycle policy" not in body


# ---------------------------------------------------------------------
# _build_active_task_health_rows — top-N cap (2026-05-14 H1 fix)
# ---------------------------------------------------------------------

async def _seed_task_with_fails(
    *,
    task_id: str,
    title: str = "demo",
    syntax_fails: int = 0,
    semantics_fails: int = 0,
    last_stage_change_at: str = "2026-05-14T10:00:00Z",
    project: str = "misc",
) -> None:
    """Seed a task + role-assignment fail rows to exercise the health rollup."""
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory, last_stage_change_at) "
            "VALUES (?, ?, ?, 'execute', 'p2', 'coach', '[]', ?)",
            (task_id, project, title, last_stage_change_at),
        )
        await c.commit()
    finally:
        await c.close()

    for _ in range(syntax_fails):
        await _seed_role_row(
            task_id=task_id,
            role="auditor_syntax",
            owner="p4",
            verdict="fail",
            completed_at="2026-05-14T10:00:00Z",
        )
    for _ in range(semantics_fails):
        await _seed_role_row(
            task_id=task_id,
            role="auditor_semantics",
            owner="p5",
            verdict="fail",
            completed_at="2026-05-14T10:00:00Z",
        )


async def test_active_task_health_empty_no_signal(fresh_db: str) -> None:
    """No tasks → empty list."""
    await init_db()
    rows = await _build_active_task_health_rows("misc")
    assert rows == []


async def test_active_task_health_caps_at_three(fresh_db: str) -> None:
    """With 5 qualifying tasks, the coordination block shows ≤ ACTIVE_TASK_HEALTH_CAP entries."""
    await init_db()
    for i in range(5):
        await _seed_task_with_fails(
            task_id=f"t-cap-{i:04d}",
            title=f"task {i}",
            syntax_fails=2 + i,  # fail counts 2..6, all qualify
        )
    surfaced: list[int] = []
    body = await _build_coach_coordination_block(surfaced_event_ids=surfaced)
    # Isolate the ## Active task health section, then count bullet lines.
    health_section = ""
    if "## Active task health" in body:
        start = body.index("## Active task health")
        # Find next ## header after the section start, or end of string.
        next_header = body.find("\n## ", start + 1)
        health_section = body[start:] if next_header == -1 else body[start:next_header]
    bullet_lines = [ln for ln in health_section.splitlines() if ln.startswith("- ")]
    assert len(bullet_lines) <= ACTIVE_TASK_HEALTH_CAP


async def test_active_task_health_overflow_footer_in_coordination_block(
    fresh_db: str,
) -> None:
    """When >3 tasks qualify, the coordination block includes a (+N more) line."""
    await init_db()
    for i in range(5):
        await _seed_task_with_fails(
            task_id=f"t-overflow-{i:04d}",
            title=f"overflow task {i}",
            syntax_fails=2,
        )
    surfaced: list[int] = []
    body = await _build_coach_coordination_block(surfaced_event_ids=surfaced)
    # Should show the cap footer for 2 overflow tasks.
    assert "(+2 more)" in body


async def test_active_task_health_ranked_by_fail_count(fresh_db: str) -> None:
    """Rows are returned in descending fail-count order."""
    await init_db()
    # Three tasks with fail_counts 2, 4, 3 → expected order: 4, 3, 2.
    await _seed_task_with_fails(task_id="t-rank-a", title="low", syntax_fails=2)
    await _seed_task_with_fails(task_id="t-rank-b", title="high", syntax_fails=4)
    await _seed_task_with_fails(task_id="t-rank-c", title="mid", syntax_fails=3)
    rows = await _build_active_task_health_rows("misc")
    counts = [r["kind_fail_count"] for r in rows]
    assert counts == sorted(counts, reverse=True)


async def test_active_task_health_tiebreak_by_recency(fresh_db: str) -> None:
    """When fail counts are equal, the more recently changed task appears first."""
    await init_db()
    await _seed_task_with_fails(
        task_id="t-older",
        title="older",
        syntax_fails=3,
        last_stage_change_at="2026-05-10T08:00:00Z",
    )
    await _seed_task_with_fails(
        task_id="t-newer",
        title="newer",
        syntax_fails=3,
        last_stage_change_at="2026-05-14T12:00:00Z",
    )
    rows = await _build_active_task_health_rows("misc")
    assert rows[0]["task_id"] == "t-newer"
