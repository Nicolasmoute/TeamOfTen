"""Playbook bootstrap tests — spec §18.1.

Covers happy path, missing template, malformed LLM response, retry counter,
3rd-fail blocked flag, cost-skip no-counter-increment, soft cap at bootstrap,
and source field semantics (boot vs reset).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import server.playbook.bootstrap as bootstrap_mod
import server.playbook.paths as pb_paths_mod
from server.playbook import config
from server.playbook.store import load_lattice
from server.shared.llm_types import LLMError, LLMResult


# ---------------------------------------------------------------- env helpers


@pytest.fixture
def pb_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Sandbox DATA_ROOT, an in-memory team_config, and a fresh corpus
    template path. Yields the data root."""
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    # Create an in-memory team_config table on a tempfile DB.
    db_path = tmp_path / "harness.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE team_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()
    import server.db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", str(db_path))
    return tmp_path


def _set_template(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _stub_llm_call(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    """Stub `llm_call` (the public `call` import in bootstrap.py)."""
    async def _fake_call(system: str, user: str, *, label: str = "", **kw) -> LLMResult:
        return LLMResult(
            text=response_text, is_error=False,
            cost_usd=0.04, duration_ms=1234,
            input_tokens=6000, output_tokens=2000,
            cache_read_tokens=0, cache_creation_tokens=0,
        )
    monkeypatch.setattr(bootstrap_mod, "llm_call", _fake_call)


def _stub_llm_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(system: str, user: str, *, label: str = "", **kw) -> LLMResult:
        raise LLMError("simulated LLM failure")
    monkeypatch.setattr(bootstrap_mod, "llm_call", _fake)


def _read_team_config(db_path: Path, key: str) -> str:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT value FROM team_config WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


# ---------------------------------------------------------------- tests


def test_bootstrap_happy_path_persists_lattice(pb_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid LLM response → lattice persisted, flag set, retries cleared."""
    _set_template(pb_env / "templates" / "app_dev_playbook.md", "prose corpus")
    monkeypatch.setattr(bootstrap_mod, "_PROSE_TEMPLATE_PATH",
                        pb_env / "templates" / "app_dev_playbook.md")
    seeds_json = json.dumps([
        {"text": "audit every code change", "suggested_weight": 0.85},
        {"text": "use plan mode for big tasks", "suggested_weight": 0.75},
    ])
    _stub_llm_call(monkeypatch, seeds_json)

    row = asyncio.run(bootstrap_mod.run_bootstrap())

    assert row["outcome"] == "applied"
    assert row["seeds_inserted"] == 2
    lat = load_lattice()
    assert len(lat.statements) == 2
    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_DONE_KEY) == "1"
    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY) == ""


def test_bootstrap_missing_template_empty_lattice_path(pb_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing template → empty lattice, no LLM call, flag still set."""
    monkeypatch.setattr(bootstrap_mod, "_PROSE_TEMPLATE_PATH",
                        pb_env / "missing_template.md")

    # If LLM gets called, raise so the test fails loudly.
    async def _no_call(*a, **kw):
        raise AssertionError("LLM should NOT be called when template is missing")
    monkeypatch.setattr(bootstrap_mod, "llm_call", _no_call)

    row = asyncio.run(bootstrap_mod.run_bootstrap())

    assert row["outcome"] == "no_changes"
    assert row["seeds_inserted"] == 0
    lat = load_lattice()
    assert lat.statements == []
    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_DONE_KEY) == "1"


def test_bootstrap_malformed_json_increments_retries(pb_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM returns non-JSON → outcome=error_parse, retry counter +1."""
    _set_template(pb_env / "templates" / "app_dev_playbook.md", "prose")
    monkeypatch.setattr(bootstrap_mod, "_PROSE_TEMPLATE_PATH",
                        pb_env / "templates" / "app_dev_playbook.md")
    _stub_llm_call(monkeypatch, "this is plain prose, not JSON")

    row = asyncio.run(bootstrap_mod.run_bootstrap())

    assert row["outcome"] == "error_parse"
    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY) == "1"
    # Not blocked yet (only 1 fail)
    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY) == ""


def test_bootstrap_llm_raise_outcome_error_llm(pb_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_template(pb_env / "templates" / "app_dev_playbook.md", "prose")
    monkeypatch.setattr(bootstrap_mod, "_PROSE_TEMPLATE_PATH",
                        pb_env / "templates" / "app_dev_playbook.md")
    _stub_llm_raise(monkeypatch)

    row = asyncio.run(bootstrap_mod.run_bootstrap())

    assert row["outcome"] == "error_llm"


def test_bootstrap_third_failure_sets_blocked_flag(pb_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_template(pb_env / "templates" / "app_dev_playbook.md", "prose")
    monkeypatch.setattr(bootstrap_mod, "_PROSE_TEMPLATE_PATH",
                        pb_env / "templates" / "app_dev_playbook.md")
    _stub_llm_raise(monkeypatch)

    # 3 sequential failures
    for _ in range(3):
        asyncio.run(bootstrap_mod.run_bootstrap())

    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY) == "1"
    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY) == "3"


def test_bootstrap_cost_cap_skip_no_retry_increment(pb_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cost-skip must NOT increment retries (G3)."""
    _set_template(pb_env / "templates" / "app_dev_playbook.md", "prose")
    monkeypatch.setattr(bootstrap_mod, "_PROSE_TEMPLATE_PATH",
                        pb_env / "templates" / "app_dev_playbook.md")
    monkeypatch.setenv("HARNESS_TEAM_DAILY_CAP", "1.0")

    # Stub _today_spend to be over cap
    async def _over_cap():
        return 100.0
    monkeypatch.setattr("server.agents._today_spend", _over_cap)

    row = asyncio.run(bootstrap_mod.run_bootstrap())

    assert row["outcome"] == "skipped_cost_cap"
    # Retry counter stays at 0
    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY) == ""


def test_bootstrap_soft_cap_drops_excess_seeds(pb_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM returns >60 seeds → drop from end down to soft cap (G4)."""
    _set_template(pb_env / "templates" / "app_dev_playbook.md", "prose")
    monkeypatch.setattr(bootstrap_mod, "_PROSE_TEMPLATE_PATH",
                        pb_env / "templates" / "app_dev_playbook.md")
    too_many = [
        {"text": f"unique seed text number {i} which is sufficiently distinct",
         "suggested_weight": 0.75}
        for i in range(120)
    ]
    _stub_llm_call(monkeypatch, json.dumps(too_many))
    events: list[dict[str, Any]] = []

    async def _capture_event(payload: dict[str, Any]) -> None:
        events.append(payload)

    monkeypatch.setattr(bootstrap_mod, "_publish", _capture_event)

    row = asyncio.run(bootstrap_mod.run_bootstrap())

    # Hard cap branch: 120 > 80 → drop to soft cap (60)
    assert row["seeds_inserted"] == config.SOFT_STATEMENT_CAP
    assert row["outcome"] == "applied"
    overflow_events = [
        e for e in events
        if e.get("type") == "playbook_soft_cap_exceeded"
    ]
    assert overflow_events == [{
        "type": "playbook_soft_cap_exceeded",
        "count": len(too_many),
        "dropped": len(too_many) - config.SOFT_STATEMENT_CAP,
    }]


def test_bootstrap_source_boot_vs_reset(pb_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """First-deploy bootstrap → source='boot'. Post-reset → source='reset'."""
    _set_template(pb_env / "templates" / "app_dev_playbook.md", "prose")
    monkeypatch.setattr(bootstrap_mod, "_PROSE_TEMPLATE_PATH",
                        pb_env / "templates" / "app_dev_playbook.md")
    _stub_llm_call(monkeypatch, json.dumps([{"text": "x", "suggested_weight": 0.75}]))

    # First boot
    row = asyncio.run(bootstrap_mod.run_bootstrap())
    assert row["source"] == "boot"

    # Simulate reset by setting playbook_reset_at + clearing done flag
    bootstrap_mod._write_team_config_sync(config.PLAYBOOK_BOOTSTRAP_DONE_KEY, None)
    bootstrap_mod._write_team_config_sync(config.PLAYBOOK_RESET_AT_KEY, "2026-05-08T00:00:00Z")

    row2 = asyncio.run(bootstrap_mod.run_bootstrap())
    assert row2["source"] == "reset"

    # After successful re-bootstrap, reset_at should be cleared
    assert _read_team_config(pb_env / "harness.db", config.PLAYBOOK_RESET_AT_KEY) == ""
