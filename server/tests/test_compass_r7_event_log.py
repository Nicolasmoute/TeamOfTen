"""v2 §19 / R7 — every Compass verdict lands in `project_events`.

The lattice signing off (`aligned`) is just as informative for Coach
as a drift verdict — it tells Coach why the audit passed, which feeds
the §11 patterns layer. This test pins R7: all three Compass verdict
types produce a `project_events` row of type `compass_audit`.

The Phase 1 mapping in `server/project_events.py` already handles the
rename. Phase 7's job is to confirm the end-to-end flow.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.project_events import maybe_write_from_bus


async def _project_events(project_id: str) -> list[dict]:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, project_id, actor, type, task_id, "
            "       payload_json, payload_pointer "
            "FROM project_events WHERE project_id = ? "
            "ORDER BY id ASC",
            (project_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()


def _compass_audit_event(verdict: str) -> dict[str, Any]:
    return {
        "type": "compass_audit_logged",
        "agent_id": "compass",
        "project_id": "misc",
        "audit_id": f"a-{verdict}",
        "verdict": verdict,
        "summary": f"verdict={verdict}",
        "contradicting_ids": [],
        "report_path": f"compass/audits/audit-{verdict}.md",
    }


# ---------------------------------------------------------------- per-verdict

@pytest.mark.parametrize(
    "verdict", ["aligned", "confident_drift", "uncertain_drift"]
)
async def test_compass_verdict_writes_project_event(
    fresh_db: str, verdict: str,
) -> None:
    """All three verdict shapes must produce a `compass_audit` row in
    `project_events` — Coach reads them via `## Recent events`."""
    await init_db()
    row_id = await maybe_write_from_bus(_compass_audit_event(verdict))
    assert row_id is not None

    rows = await _project_events("misc")
    audit_rows = [r for r in rows if r["type"] == "compass_audit"]
    assert len(audit_rows) == 1
    r = audit_rows[0]
    assert r["actor"] == "compass"
    assert r["project_id"] == "misc"
    # payload_json round-trip preserves the verdict so Coach can filter
    # without re-querying compass.
    import json
    body = json.loads(r["payload_json"])
    assert body["verdict"] == verdict


async def test_all_three_verdicts_coexist(fresh_db: str) -> None:
    """Real Compass deployment will produce a stream of mixed verdicts.
    Make sure they don't collide on the unique-key path or otherwise
    drop silently."""
    await init_db()
    for verdict in ("aligned", "confident_drift", "uncertain_drift"):
        await maybe_write_from_bus(_compass_audit_event(verdict))

    rows = await _project_events("misc")
    audit_rows = [r for r in rows if r["type"] == "compass_audit"]
    assert len(audit_rows) == 3
    import json
    seen = sorted(json.loads(r["payload_json"])["verdict"] for r in audit_rows)
    assert seen == ["aligned", "confident_drift", "uncertain_drift"]


async def test_compass_audit_unread_by_default(fresh_db: str) -> None:
    """v2 §9.5 — newly-written project_events rows have NULL
    `read_by_coach_at`; the post-turn handler in `agents.py` stamps
    them after Coach reads them via `## Recent events`."""
    await init_db()
    row_id = await maybe_write_from_bus(_compass_audit_event("aligned"))
    assert row_id is not None

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT read_by_coach_at FROM project_events WHERE id = ?",
            (row_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    assert dict(row)["read_by_coach_at"] is None
