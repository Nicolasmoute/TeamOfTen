"""Playbook MCP tool tests — spec §18.1.

Covers Coach-only enforcement, ops cap, lock-contention return string,
and the apply order via the tool path.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import server.playbook.paths as pb_paths_mod
from server.playbook import config


@pytest.fixture
def tool_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    db = tmp_path / "harness.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE team_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()
    import server.db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", str(db))
    return tmp_path


def _build_server(caller_id: str):
    """Build a coord MCP server for the given caller."""
    from server.tools import build_coord_server
    return build_coord_server(caller_id, include_proxy_metadata=True)


def _find_handler(server, name: str):
    handlers = server.get("_handlers", {})
    return handlers.get(name)


def test_player_call_rejected(tool_env: Path) -> None:
    """coord_propose_playbook_changes is Coach-only."""
    server = _build_server("p1")
    handler = _find_handler(server, "coord_propose_playbook_changes")
    assert handler is not None, "tool should be registered"
    result = asyncio.run(handler({
        "operations": [{"op": "adjust", "id": "pb-001", "delta": 0.10, "reason": "x"}],
    }))
    # Result is wrapped in {"content": [{"type": "text", "text": "..."}]}
    text = result["content"][0]["text"]
    assert "Coach-only" in text


def test_ops_cap_enforced(tool_env: Path) -> None:
    """More than 5 ops → rejected at the wrapper level."""
    server = _build_server("coach")
    handler = _find_handler(server, "coord_propose_playbook_changes")
    too_many = [{"op": "adjust", "id": f"pb-{i:03d}", "delta": 0.05, "reason": "x"}
                for i in range(6)]
    result = asyncio.run(handler({"operations": too_many}))
    text = result["content"][0]["text"]
    assert "too many operations" in text.lower()


def test_empty_operations_rejected(tool_env: Path) -> None:
    server = _build_server("coach")
    handler = _find_handler(server, "coord_propose_playbook_changes")
    result = asyncio.run(handler({"operations": []}))
    text = result["content"][0]["text"]
    assert "at least one operation" in text.lower()


def test_lock_contention_returns_busy_string(tool_env: Path) -> None:
    """When `_run_lock` is held, the tool returns a string starting with
    `"playbook engine busy"` (spec §G8 / §N9)."""
    server = _build_server("coach")
    handler = _find_handler(server, "coord_propose_playbook_changes")

    from server.playbook import runner as pb_runner

    async def _scenario():
        await pb_runner._run_lock.acquire()
        try:
            return await handler({
                "operations": [
                    {"op": "adjust", "id": "pb-001", "delta": 0.05, "reason": "x"},
                ],
            })
        finally:
            pb_runner._run_lock.release()

    result = asyncio.run(_scenario())
    text = result["content"][0]["text"]
    assert text.startswith("playbook engine busy")
    assert "retry" in text.lower()


def test_apply_order_through_tool(tool_env: Path) -> None:
    """Verify the tool routes through mutate.apply_coach_proposals
    and respects the spec §5.6 order."""
    server = _build_server("coach")
    handler = _find_handler(server, "coord_propose_playbook_changes")

    # Pre-seed the lattice with two statements
    from server.playbook.store import (
        Lattice, Statement, WeightHistoryEntry, save_lattice, load_lattice
    )
    asyncio.run(save_lattice(Lattice(
        schema_version=1, updated_at="now",
        statements=[
            Statement(id="pb-001", text="x", weight=0.5,
                      weight_history=[WeightHistoryEntry(ts="now", from_=None, to=0.5, reason="s")],
                      created_at="now", created_by="test", applied_count=0, immutable=False),
            Statement(id="pb-002", text="y", weight=0.6,
                      weight_history=[WeightHistoryEntry(ts="now", from_=None, to=0.6, reason="s")],
                      created_at="now", created_by="test", applied_count=0, immutable=False),
        ],
    )))

    result = asyncio.run(handler({
        "operations": [
            # Adjust on pb-001 first in input — but should apply AFTER merge
            {"op": "adjust", "id": "pb-001", "delta": 0.10, "reason": "x"},
            # Merge pb-002 into pb-001
            {"op": "merge", "keep_id": "pb-001", "drop_id": "pb-002", "reason": "dupe"},
        ],
    }))
    text = result["content"][0]["text"]
    assert "Applied 2" in text or "applied 2" in text.lower()

    # Verify post-state: pb-002 is archived; pb-001 weight = max(0.5, 0.6) + 0.10 = 0.7
    lat = load_lattice()
    assert len(lat.statements) == 1
    assert lat.statements[0].id == "pb-001"
    assert lat.statements[0].weight == pytest.approx(0.7)
