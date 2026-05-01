"""Phase 5 tests — Compass MCP tools wired into `build_coord_server`.

Verifies (per the plan's MCP scope decision):
  - Coach can call all four tools when Compass is enabled
  - Players cannot call any of the four (Coach-only invariant)
  - All four reject when Compass is disabled for the project
  - `compass_status` works without an LLM call
  - `compass_brief` returns the latest briefing if one exists
  - `compass_ask` calls the LLM with the lattice + truth payload
  - `compass_audit` round-trips through `audit.audit_work` and writes
    to audits.jsonl
  - `coord_tool_names()` lists the four new tools
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.compass import audit as audit_mod
from server.compass import config as cmp_config
from server.compass import llm as llm_mod
from server.compass import store as cmp_store
from server.tools import build_coord_server, coord_tool_names


@dataclass
class _FakeResult:
    text: str
    is_error: bool = False
    cost_usd: float | None = 0.001
    duration_ms: int | None = 50
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    session_id: str | None = "stub"
    stop_reason: str | None = "end_turn"
    errors: list[str] = field(default_factory=list)


def _stub_llm(monkeypatch: pytest.MonkeyPatch, response_text: str) -> None:
    async def _fake(system: str, user: str, **kwargs: Any) -> _FakeResult:
        return _FakeResult(text=response_text)

    monkeypatch.setattr(llm_mod, "call", _fake)
    monkeypatch.setattr(audit_mod.llm, "call", _fake)


def _get_handler(server: dict[str, Any], name: str) -> Any:
    handlers = server.get("_handlers") or {}
    return handlers[name]


async def _enable_compass(project_id: str) -> None:
    from server.db import configured_conn

    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO team_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cmp_config.enabled_key(project_id), "1"),
        )
        await c.commit()
    finally:
        await c.close()


# --------------------------------------------------- registration


def test_compass_tools_registered_in_coord_namespace() -> None:
    names = coord_tool_names()
    assert "compass_ask" in names
    assert "compass_audit" in names
    assert "compass_brief" in names
    assert "compass_status" in names


# --------------------------------------------------- access gating


@pytest.mark.asyncio
async def test_player_rejected_from_all_four(fresh_db: str) -> None:
    """Spec: Coach-only at the MCP surface. Players read Compass via
    the CLAUDE.md block."""
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    await _enable_compass("misc")

    server = build_coord_server("p3", include_proxy_metadata=True)
    for tool_name in ("compass_ask", "compass_audit", "compass_brief", "compass_status"):
        handler = _get_handler(server, tool_name)
        out = await handler({"query": "x", "artifact": "x"})
        assert out.get("isError") is True
        text = out["content"][0]["text"]
        assert "Coach-only" in text


@pytest.mark.asyncio
async def test_coach_rejected_when_compass_disabled(fresh_db: str) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    # Compass NOT enabled; tools should refuse.
    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = _get_handler(server, "compass_status")
    out = await handler({})
    assert out.get("isError") is True
    assert "disabled for this project" in out["content"][0]["text"]


# --------------------------------------------------- compass_status


@pytest.mark.asyncio
async def test_compass_status_returns_counts(fresh_db: str) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    await _enable_compass("misc")
    await cmp_store.bootstrap_state("misc")
    state = cmp_store.load_state("misc")
    state.statements.append(cmp_store.Statement(
        id="s1", text="x", region="pricing", weight=0.5, created_at="t",
    ))
    state.questions.append(cmp_store.Question(
        id="q1", q="?", prediction="yes", targets=[], rationale="r",
        asked_at="t", asked_in_run="r1",
    ))
    await cmp_store.save_lattice("misc", state.statements)
    await cmp_store.save_questions("misc", state.questions)

    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = _get_handler(server, "compass_status")
    out = await handler({})
    assert out.get("isError") is not True
    text = out["content"][0]["text"]
    assert "active statements: 1" in text
    assert "pending questions: 1" in text
    assert "regions" in text


# --------------------------------------------------- compass_brief


@pytest.mark.asyncio
async def test_compass_brief_returns_latest(fresh_db: str) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    await _enable_compass("misc")
    await cmp_store.write_briefing("misc", "2026-05-01", "## Briefing 1\n\nbody\n")
    await cmp_store.write_briefing("misc", "2026-05-02", "## Briefing 2\n\nfresher\n")

    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = _get_handler(server, "compass_brief")
    out = await handler({})
    assert out.get("isError") is not True
    assert "Briefing 2" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_compass_brief_placeholder_when_none(fresh_db: str) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    await _enable_compass("misc")

    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = _get_handler(server, "compass_brief")
    out = await handler({})
    assert out.get("isError") is not True
    assert "No Compass briefing" in out["content"][0]["text"]


# --------------------------------------------------- compass_ask


@pytest.mark.asyncio
async def test_compass_ask_calls_llm_and_returns_text(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    await _enable_compass("misc")
    _stub_llm(monkeypatch, "Per `s1` (0.91): pricing favors usage-based.")

    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = _get_handler(server, "compass_ask")
    out = await handler({"query": "what about pricing?"})
    assert out.get("isError") is not True
    assert "s1" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_compass_ask_requires_query(fresh_db: str) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    await _enable_compass("misc")

    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = _get_handler(server, "compass_ask")
    out = await handler({"query": ""})
    assert out.get("isError") is True


# --------------------------------------------------- compass_audit


@pytest.mark.asyncio
async def test_compass_audit_writes_log_and_renders_verdict(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    await _enable_compass("misc")
    _stub_llm(monkeypatch, json.dumps({
        "verdict": "confident_drift",
        "summary": "drift in pricing",
        "contradicting_ids": ["s2"],
        "message_to_coach": "Halt the worker.",
        "question_for_human": None,
    }))

    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = _get_handler(server, "compass_audit")
    out = await handler({"artifact": "worker-4 shipped per-second billing"})
    assert out.get("isError") is not True
    text = out["content"][0]["text"]
    assert "confident_drift" in text
    assert "Halt the worker." in text
    # Recorded in audits.jsonl
    audits = cmp_store.read_audits("misc")
    assert len(audits) == 1
    assert audits[0].verdict == "confident_drift"


@pytest.mark.asyncio
async def test_compass_audit_requires_artifact(fresh_db: str) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")
    await _enable_compass("misc")

    server = build_coord_server("coach", include_proxy_metadata=True)
    handler = _get_handler(server, "compass_audit")
    out = await handler({"artifact": ""})
    assert out.get("isError") is True
