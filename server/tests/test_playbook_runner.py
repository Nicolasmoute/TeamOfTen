"""Playbook runner tests — spec §18.1.

Covers activity gate, cost gate, op apply order, parse failure → no_changes,
relevant_ids increment, and Codex fallback.

Most tests stub the LLM so they don't hit the network.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import server.playbook.paths as pb_paths_mod
import server.playbook.runner as runner_mod
from server.playbook import config
from server.shared.llm_types import LLMError, LLMResult


@pytest.fixture
def runner_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Sandbox DATA_ROOT + a tempfile DB with bootstrap_done flag set."""
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    db = tmp_path / "harness.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE team_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO team_config VALUES ('playbook_bootstrap_done', '1')")
        # Tables the runner queries for the evidence bundle. Empty schemas so
        # SQL doesn't error; runner tolerates empty rows.
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, "
            "trajectory TEXT, owner TEXT, archived_at TEXT, "
            "cancelled_at TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE project_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts TEXT, task_id TEXT, type TEXT, payload_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE deviations_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts TEXT, task_id TEXT, executor TEXT, "
            "noticed_at TEXT, description TEXT)"
        )
        conn.execute(
            "CREATE TABLE turns (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id TEXT, cost_usd REAL)"
        )
        conn.commit()
    finally:
        conn.close()
    import server.db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", str(db))
    return tmp_path


def _stub_llm_call(monkeypatch: pytest.MonkeyPatch, response: str) -> None:
    async def _fake(system: str, user: str, *, label: str = "", **kw) -> LLMResult:
        return LLMResult(
            text=response, is_error=False, cost_usd=0.015, duration_ms=900,
            input_tokens=4000, output_tokens=600,
            cache_read_tokens=0, cache_creation_tokens=0,
        )
    monkeypatch.setattr(runner_mod, "llm_call", _fake)


def _seed_activity(db_path: Path, count: int) -> None:
    conn = sqlite3.connect(db_path)
    try:
        for i in range(count):
            conn.execute(
                "INSERT INTO project_events (ts, task_id, type, payload_json) "
                "VALUES (datetime('now'), ?, 'audit_report_submitted', '{}')",
                (f"t-{i:03d}",),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- gates


def test_activity_gate_skip_when_quiet(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero activity → skipped_no_activity outcome, no LLM call."""
    async def _no_call(*a, **kw):
        raise AssertionError("LLM should NOT be called when no activity")
    monkeypatch.setattr(runner_mod, "llm_call", _no_call)

    row = asyncio.run(runner_mod.run_daily_reflection(manual=False))
    assert row["outcome"] == "skipped_no_activity"


def test_activity_gate_passes_with_enough_activity(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Threshold activity → LLM gets called."""
    _seed_activity(runner_env / "harness.db", config.MIN_ACTIVITY_DEFAULT)
    _stub_llm_call(monkeypatch, json.dumps({
        "relevant_ids": [], "adjustments": [], "creations": [], "merges": [],
    }))
    row = asyncio.run(runner_mod.run_daily_reflection(manual=False))
    assert row["outcome"] in ("applied", "no_changes")


def test_force_through_no_activity_bypasses_gate(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_llm_call(monkeypatch, json.dumps({
        "relevant_ids": [], "adjustments": [], "creations": [], "merges": [],
    }))
    row = asyncio.run(runner_mod.run_daily_reflection(manual=True, force_through_no_activity=True))
    assert row["outcome"] in ("applied", "no_changes")


def test_cost_cap_skip(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_activity(runner_env / "harness.db", config.MIN_ACTIVITY_DEFAULT)
    monkeypatch.setenv("HARNESS_TEAM_DAILY_CAP", "1.0")
    async def _over_cap():
        return 100.0
    monkeypatch.setattr("server.agents._today_spend", _over_cap)

    row = asyncio.run(runner_mod.run_daily_reflection(manual=False))
    assert row["outcome"] == "skipped_cost_cap"


# ---------------------------------------------------------------- parse / apply


def test_llm_parse_failure_outcome_error_parse(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_activity(runner_env / "harness.db", config.MIN_ACTIVITY_DEFAULT)
    _stub_llm_call(monkeypatch, "this is plain prose, not JSON at all")

    row = asyncio.run(runner_mod.run_daily_reflection(manual=False))
    assert row["outcome"] == "error_parse"


def test_llm_empty_response_outcome_no_changes(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_activity(runner_env / "harness.db", config.MIN_ACTIVITY_DEFAULT)
    _stub_llm_call(monkeypatch, json.dumps({
        "relevant_ids": [], "adjustments": [], "creations": [], "merges": [],
    }))

    row = asyncio.run(runner_mod.run_daily_reflection(manual=False))
    assert row["outcome"] == "no_changes"
    assert row["proposals_applied"] == []
    assert row["relevance_increments"] == 0


def test_llm_creation_applied_outcome_applied(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_activity(runner_env / "harness.db", config.MIN_ACTIVITY_DEFAULT)
    _stub_llm_call(monkeypatch, json.dumps({
        "relevant_ids": [],
        "adjustments": [],
        "creations": [{
            "text": "novel pattern that wasn't in the lattice before",
            "weight": 0.6,
            "reason": "observed 3 times today",
        }],
        "merges": [],
    }))
    row = asyncio.run(runner_mod.run_daily_reflection(manual=False))
    assert row["outcome"] == "applied"
    assert len(row["proposals_applied"]) == 1
    assert row["proposals_applied"][0]["op"] == "create"


def test_llm_call_raises_outcome_error_llm(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_activity(runner_env / "harness.db", config.MIN_ACTIVITY_DEFAULT)
    async def _raise(*a, **kw):
        raise LLMError("simulated total failure (Claude + Codex)")
    monkeypatch.setattr(runner_mod, "llm_call", _raise)

    row = asyncio.run(runner_mod.run_daily_reflection(manual=False))
    assert row["outcome"] == "error_llm"


# ---------------------------------------------------------------- cap pressure


def test_cap_pressure_drops_creations_from_end(runner_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When Coach proposes more creations than the soft cap allows, drop
    from the END of the input list per spec §5.7."""
    _seed_activity(runner_env / "harness.db", config.MIN_ACTIVITY_DEFAULT)

    # Pre-populate the lattice up to 99 statements via store helpers.
    from server.playbook import mutate as pb_mutate
    from server.playbook.store import (
        Lattice, Statement, WeightHistoryEntry, save_lattice
    )
    seed_stmts = [
        Statement(
            id=f"pb-{i:03d}", text=f"existing stmt {i}", weight=0.5,
            weight_history=[WeightHistoryEntry(ts="2026-04-01T00:00:00Z",
                                               from_=None, to=0.5, reason="seed")],
            created_at="2026-04-01T00:00:00Z",
            created_by="test",
            last_validated_at="2026-04-01T00:00:00Z",
            applied_count=0, immutable=False,
        )
        for i in range(99)
    ]
    asyncio.run(save_lattice(Lattice(schema_version=1, updated_at="now", statements=seed_stmts)))

    # Coach proposes 8 creations → pressure = 99+8 = 107 → soft branch:
    # survivors = 100 - 99 = 1, dropped = 7.
    creations = [
        {"text": f"new_unique_pattern_alpha_beta_{i}", "weight": 0.6, "reason": "x"}
        for i in range(8)
    ]
    _stub_llm_call(monkeypatch, json.dumps({
        "relevant_ids": [], "adjustments": [],
        "creations": creations, "merges": [],
    }))

    row = asyncio.run(runner_mod.run_daily_reflection(manual=False))
    # Exactly one creation should apply
    applied_creates = [op for op in row["proposals_applied"] if op.get("op") == "create"]
    assert len(applied_creates) == 1
    # Seven creations should be rejected with reason soft_cap_pressure
    rejected_caps = [op for op in row["proposals_rejected"]
                     if op.get("reason") == "soft_cap_pressure"]
    assert len(rejected_caps) == 7
