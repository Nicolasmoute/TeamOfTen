"""Phase 1 of kanban v2 (Docs/kanban-specs-v2.md §9, §16.4, §22.1):
schema for project_events + deviations_log, migration backfill,
subscriber-side mirroring of bus events, and the write_project_event
helper.

These tests don't exercise any v2 BEHAVIOR (Phase 3 onwards) — Phase 1
is purely additive: tables exist, the writer works, the subscriber
calls it. v1 routing semantics are unchanged.
"""

from __future__ import annotations

import json

import pytest

import server.db as dbmod
from server.db import configured_conn, init_db
from server.project_events import (
    _BUS_TYPE_RENAMES,
    _LOGGABLE_BUS_TYPES,
    _extract_pointer,
    _resolve_log_type,
    maybe_write_from_bus,
    write_project_event,
)


# ---------------------------------------------------------------- helpers


async def _table_exists(name: str) -> bool:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return (await cur.fetchone()) is not None
    finally:
        await c.close()


async def _project_events_count(project_id: str = "misc") -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) FROM project_events WHERE project_id = ?",
            (project_id,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        await c.close()


async def _project_events_rows(project_id: str = "misc") -> list[dict]:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, project_id, ts, actor, type, task_id, "
            "payload_json, payload_pointer, read_by_coach_at "
            "FROM project_events WHERE project_id = ? ORDER BY id",
            (project_id,),
        )
        out = []
        for row in await cur.fetchall():
            out.append({
                "id": row[0],
                "project_id": row[1],
                "ts": row[2],
                "actor": row[3],
                "type": row[4],
                "task_id": row[5],
                "payload_json": row[6],
                "payload_pointer": row[7],
                "read_by_coach_at": row[8],
            })
        return out
    finally:
        await c.close()


async def _team_config(key: str) -> str | None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?", (key,),
        )
        row = await cur.fetchone()
        return row[0] if row else None
    finally:
        await c.close()


# ---------------------------------------------------------------- schema


@pytest.mark.asyncio
async def test_init_db_creates_project_events_table(fresh_db: str) -> None:
    await init_db()
    assert await _table_exists("project_events")


@pytest.mark.asyncio
async def test_init_db_creates_deviations_log_table(fresh_db: str) -> None:
    await init_db()
    assert await _table_exists("deviations_log")


@pytest.mark.asyncio
async def test_project_events_schema_columns(fresh_db: str) -> None:
    """The columns match §9.1 exactly — payload_json (not payload),
    payload_pointer, read_by_coach_at."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(project_events)")
        cols = {r[1] for r in await cur.fetchall()}
    finally:
        await c.close()
    assert cols == {
        "id", "project_id", "ts", "actor", "type", "task_id",
        "payload_json", "payload_pointer", "read_by_coach_at",
    }


@pytest.mark.asyncio
async def test_deviations_log_schema_columns(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute("PRAGMA table_info(deviations_log)")
        cols = {r[1] for r in await cur.fetchall()}
    finally:
        await c.close()
    assert cols == {
        "id", "project_id", "ts", "task_id", "executor",
        "noticed_at", "description", "source_event_id",
    }


@pytest.mark.asyncio
async def test_deviations_log_noticed_at_check_constraint(
    fresh_db: str,
) -> None:
    """noticed_at must be 'push'/'audit'/'human' — invalid values rejected."""
    await init_db()
    c = await configured_conn()
    try:
        with pytest.raises(Exception):
            await c.execute(
                "INSERT INTO deviations_log "
                "(project_id, task_id, executor, noticed_at) "
                "VALUES ('misc', 't-1', 'p1', 'invalid')"
            )
            await c.commit()
    finally:
        await c.close()


# ---------------------------------------------------------------- migration


@pytest.mark.asyncio
async def test_migration_marker_set_after_init(fresh_db: str) -> None:
    await init_db()
    marker = await _team_config("tasks_kanban_v2_migrated")
    assert marker == "1"


@pytest.mark.asyncio
async def test_migration_is_idempotent(fresh_db: str) -> None:
    """Running init_db twice shouldn't duplicate the cutover event."""
    await init_db()
    after_first = await _project_events_count("misc")
    await init_db()
    after_second = await _project_events_count("misc")
    assert after_first == after_second


@pytest.mark.asyncio
async def test_migration_inserts_cutover_event_per_project(
    fresh_db: str,
) -> None:
    """One synthetic kanban_v2_cutover row per project on first migration."""
    await init_db()
    rows = await _project_events_rows("misc")
    cutover_rows = [r for r in rows if r["type"] == "kanban_v2_cutover"]
    assert len(cutover_rows) == 1
    cutover = cutover_rows[0]
    assert cutover["actor"] == "system"
    assert cutover["read_by_coach_at"] is None  # UNREAD per §16.4
    payload = json.loads(cutover["payload_json"])
    assert payload["to"] == "coach"
    assert "v2" in payload["body"].lower()


@pytest.mark.asyncio
async def test_backfill_skips_when_no_legacy_events(fresh_db: str) -> None:
    """Fresh DB → no events to backfill, no project_events rows from
    backfill (only the cutover event)."""
    await init_db()
    rows = await _project_events_rows("misc")
    # Only the cutover row should exist (no legacy events to backfill).
    assert len(rows) == 1
    assert rows[0]["type"] == "kanban_v2_cutover"


@pytest.mark.asyncio
async def test_backfill_copies_mappable_events(fresh_db: str) -> None:
    """Pre-seed legacy events; expect backfill to copy mappable types."""
    # First boot creates schema + sets marker. We need to seed events
    # into the legacy `events` table BEFORE the marker is set, so the
    # backfill picks them up. We achieve this by manually wiping the
    # marker, seeding events, then re-running the migration.
    await init_db()  # Sets schema + marker.

    # Seed events into the legacy table directly.
    c = await configured_conn()
    try:
        await c.executemany(
            "INSERT INTO events (ts, agent_id, project_id, type, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "2026-05-07T12:00:00.000Z",
                    "p1",
                    "misc",
                    "commit_pushed",
                    json.dumps({
                        "task_id": "t-abc",
                        "sha": "abc123",
                        "type": "commit_pushed",
                    }),
                ),
                (
                    "2026-05-07T12:01:00.000Z",
                    "p2",
                    "misc",
                    "task_spec_written",
                    json.dumps({
                        "task_id": "t-def",
                        "spec_path": "projects/misc/working/tasks/t-def/spec.md",
                        "type": "task_spec_written",
                    }),
                ),
                # Unmappable type — should be skipped.
                (
                    "2026-05-07T12:02:00.000Z",
                    "p3",
                    "misc",
                    "text_delta",
                    json.dumps({"text": "ignored", "type": "text_delta"}),
                ),
            ],
        )
        # Wipe the marker + the existing project_events rows so the
        # migration backfills again on the next call.
        await c.execute(
            "DELETE FROM team_config WHERE key = 'tasks_kanban_v2_migrated'"
        )
        await c.execute("DELETE FROM project_events")
        await c.commit()
    finally:
        await c.close()

    # Re-run migration.
    await init_db()

    rows = await _project_events_rows("misc")
    types = {r["type"] for r in rows}
    assert "commit_pushed" in types
    assert "task_spec_written" in types
    assert "text_delta" not in types  # unmappable, skipped
    # Cutover event also present.
    assert "kanban_v2_cutover" in types

    # Backfilled rows are stamped read so Coach's first tick sees only
    # fresh signals (per §16.4).
    backfilled = [r for r in rows if r["type"] in {"commit_pushed", "task_spec_written"}]
    for r in backfilled:
        assert r["read_by_coach_at"] is not None, (
            f"row id={r['id']} type={r['type']} should be stamped read"
        )

    # Cutover stays UNREAD.
    cutover = next(r for r in rows if r["type"] == "kanban_v2_cutover")
    assert cutover["read_by_coach_at"] is None


@pytest.mark.asyncio
async def test_backfill_renames_legacy_types(fresh_db: str) -> None:
    """`message_sent` → `coord_send_message`, `compass_audit_logged` →
    `compass_audit`, etc."""
    await init_db()

    c = await configured_conn()
    try:
        await c.executemany(
            "INSERT INTO events (ts, agent_id, project_id, type, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "2026-05-07T12:00:00.000Z",
                    "p1",
                    "misc",
                    "message_sent",
                    json.dumps({
                        "from_id": "p1",
                        "to_id": "coach",
                        "body": "hello coach",
                        "type": "message_sent",
                    }),
                ),
                (
                    "2026-05-07T12:01:00.000Z",
                    "compass",
                    "misc",
                    "compass_audit_logged",
                    json.dumps({
                        "verdict": "aligned",
                        "summary": "all good",
                        "type": "compass_audit_logged",
                    }),
                ),
            ],
        )
        await c.execute(
            "DELETE FROM team_config WHERE key = 'tasks_kanban_v2_migrated'"
        )
        await c.execute("DELETE FROM project_events")
        await c.commit()
    finally:
        await c.close()

    await init_db()

    rows = await _project_events_rows("misc")
    types = {r["type"] for r in rows}
    assert "coord_send_message" in types
    assert "compass_audit" in types
    # The legacy type names should NOT appear in project_events.
    assert "message_sent" not in types
    assert "compass_audit_logged" not in types


@pytest.mark.asyncio
async def test_backfill_skips_old_events(fresh_db: str) -> None:
    """Events older than 30 days are not backfilled."""
    await init_db()

    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO events (ts, agent_id, project_id, type, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                "2025-01-01T00:00:00.000Z",  # ~16 months ago
                "p1",
                "misc",
                "commit_pushed",
                json.dumps({"sha": "old", "type": "commit_pushed"}),
            ),
        )
        await c.execute(
            "DELETE FROM team_config WHERE key = 'tasks_kanban_v2_migrated'"
        )
        await c.execute("DELETE FROM project_events")
        await c.commit()
    finally:
        await c.close()

    await init_db()

    rows = await _project_events_rows("misc")
    # Only cutover, no backfill (old event was outside the 30-day window).
    assert len(rows) == 1
    assert rows[0]["type"] == "kanban_v2_cutover"


# ---------------------------------------------------------------- writer


@pytest.mark.asyncio
async def test_write_project_event_inserts_row(fresh_db: str) -> None:
    await init_db()
    row_id = await write_project_event(
        project_id="misc",
        actor="p3",
        type="commit_pushed",
        task_id="t-xyz",
        payload={"sha": "deadbeef", "type": "commit_pushed"},
        payload_pointer="deadbeef",
    )
    assert row_id is not None
    rows = await _project_events_rows("misc")
    inserted = [r for r in rows if r["id"] == row_id]
    assert len(inserted) == 1
    r = inserted[0]
    assert r["actor"] == "p3"
    assert r["type"] == "commit_pushed"
    assert r["task_id"] == "t-xyz"
    assert r["payload_pointer"] == "deadbeef"
    assert r["read_by_coach_at"] is None


@pytest.mark.asyncio
async def test_maybe_write_from_bus_writes_mappable_event(
    fresh_db: str,
) -> None:
    await init_db()
    row_id = await maybe_write_from_bus({
        "type": "commit_pushed",
        "agent_id": "p2",
        "project_id": "misc",
        "ts": "2026-05-07T15:00:00.000Z",
        "task_id": "t-1",
        "sha": "cafef00d",
        "message": "implement foo",
    })
    assert row_id is not None
    rows = await _project_events_rows("misc")
    r = next(r for r in rows if r["id"] == row_id)
    assert r["type"] == "commit_pushed"
    assert r["payload_pointer"] == "cafef00d"
    assert r["actor"] == "p2"
    assert r["task_id"] == "t-1"


@pytest.mark.asyncio
async def test_maybe_write_from_bus_skips_unmappable_event(
    fresh_db: str,
) -> None:
    await init_db()
    before = await _project_events_count("misc")
    row_id = await maybe_write_from_bus({
        "type": "text_delta",
        "agent_id": "p1",
        "project_id": "misc",
        "text": "streaming token",
    })
    assert row_id is None  # unmappable type → no row written
    after = await _project_events_count("misc")
    assert after == before


@pytest.mark.asyncio
async def test_maybe_write_from_bus_renames_compass_audit(
    fresh_db: str,
) -> None:
    """Bus event `compass_audit_logged` → log row `compass_audit`."""
    await init_db()
    row_id = await maybe_write_from_bus({
        "type": "compass_audit_logged",
        "agent_id": "compass",
        "project_id": "misc",
        "verdict": "aligned",
        "summary": "lattice signed off",
    })
    assert row_id is not None
    rows = await _project_events_rows("misc")
    r = next(r for r in rows if r["id"] == row_id)
    assert r["type"] == "compass_audit"


def test_resolve_log_type_renames() -> None:
    """The static rename map covers the four v1→v2 renames per §9.2."""
    assert _resolve_log_type("message_sent") == "coord_send_message"
    assert _resolve_log_type("knowledge_written") == "coord_write_knowledge"
    assert _resolve_log_type("decision_written") == "coord_write_decision"
    assert _resolve_log_type("compass_audit_logged") == "compass_audit"


def test_resolve_log_type_passthrough() -> None:
    """Direct types pass through unchanged."""
    assert _resolve_log_type("commit_pushed") == "commit_pushed"
    assert _resolve_log_type("task_stage_changed") == "task_stage_changed"


def test_loggable_types_includes_all_renames() -> None:
    """Every key of `_BUS_TYPE_RENAMES` must be in `_LOGGABLE_BUS_TYPES`,
    otherwise the rename never fires."""
    for v1_name in _BUS_TYPE_RENAMES:
        assert v1_name in _LOGGABLE_BUS_TYPES, (
            f"Bus type {v1_name!r} is renamed but not in _LOGGABLE_BUS_TYPES"
        )


# ---------------------------------------------------------------- payload_pointer


def test_extract_pointer_commit_pushed() -> None:
    p = _extract_pointer("commit_pushed", {"sha": "abc123"})
    assert p == "abc123"


def test_extract_pointer_task_spec_written() -> None:
    p = _extract_pointer(
        "task_spec_written", {"spec_path": "projects/misc/spec.md"}
    )
    assert p == "projects/misc/spec.md"


def test_extract_pointer_audit_report_submitted() -> None:
    p = _extract_pointer(
        "audit_report_submitted",
        {"report_path": "projects/misc/working/tasks/t-1/audits/audit_1_syntax.md"},
    )
    assert p == "projects/misc/working/tasks/t-1/audits/audit_1_syntax.md"


def test_extract_pointer_coord_send_message_truncates() -> None:
    """Bodies > 500 chars should be truncated to 500."""
    long_body = "x" * 1000
    p = _extract_pointer("coord_send_message", {"body": long_body})
    assert p is not None
    assert len(p) == 500


def test_extract_pointer_unmapped_returns_none() -> None:
    """Types without a structured pointer return None (the row's
    payload_json still has the data, just no fast-path)."""
    assert _extract_pointer("task_stage_changed", {}) is None
    assert _extract_pointer("human_attention", {}) is None


# ---------------------------------------------------------------- subscriber wiring


@pytest.mark.asyncio
async def test_kanban_subscriber_mirrors_to_project_events(
    fresh_db: str,
) -> None:
    """End-to-end: bus.publish() → kanban subscriber consumes → row
    in project_events."""
    await init_db()

    from server.events import bus
    from server.kanban import start_kanban_subscriber, stop_kanban_subscriber
    import asyncio

    await start_kanban_subscriber()
    try:
        await bus.publish({
            "type": "commit_pushed",
            "agent_id": "p1",
            "project_id": "misc",
            "ts": "2026-05-07T16:00:00.000Z",
            "task_id": "t-mirror",
            "sha": "feedface",
            "message": "test commit",
        })
        # Subscriber is async — give it a moment to drain the queue.
        for _ in range(20):
            await asyncio.sleep(0.05)
            rows = await _project_events_rows("misc")
            if any(
                r["type"] == "commit_pushed" and r["task_id"] == "t-mirror"
                for r in rows
            ):
                break
        else:
            pytest.fail("subscriber did not mirror the bus event")
    finally:
        await stop_kanban_subscriber()


@pytest.mark.asyncio
async def test_kanban_subscriber_skips_unmappable_event(
    fresh_db: str,
) -> None:
    """Bus event of unmappable type → subscriber doesn't write."""
    await init_db()

    from server.events import bus
    from server.kanban import start_kanban_subscriber, stop_kanban_subscriber
    import asyncio

    await start_kanban_subscriber()
    try:
        before = await _project_events_count("misc")
        await bus.publish({
            "type": "text_delta",
            "agent_id": "p1",
            "project_id": "misc",
            "text": "streaming",
        })
        # Wait a bit to ensure the subscriber would have processed it.
        await asyncio.sleep(0.2)
        after = await _project_events_count("misc")
        assert after == before
    finally:
        await stop_kanban_subscriber()
