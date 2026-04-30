"""Tests for the recurrence v1 surface (table + DSL parser + scheduler).

Phase 1 cover:
  * schema present + indices in place after init_db.
  * env-var migration seeds a tick row for every project, exactly once.
  * cron DSL parser accepts every shape in spec §5.1 and rejects bogus
    input.
  * compute_next_fire_at advances correctly across each schedule type
    (daily / weekly / weekdays / weekends / monthly / once) including
    the timezone conversion.
  * scheduler iteration fires due rows when Coach is idle, skips when
    Coach is busy, and recomputes next_fire_at after each pass.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from typing import Any
from unittest.mock import patch

import pytest

import server.recurrences as recmod
from server.db import configured_conn, init_db


# --- Schema -----------------------------------------------------------


async def test_init_db_creates_recurrence_table(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'coach_recurrence'"
        )
        row = await cur.fetchone()
        assert row is not None
        cur = await c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name = 'idx_recurrence_one_tick'"
        )
        assert await cur.fetchone() is not None
    finally:
        await c.close()


async def test_one_tick_per_project_constraint(fresh_db: str) -> None:
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, enabled) "
            "VALUES ('misc', 'tick', '60', 1)"
        )
        await c.commit()
        with pytest.raises(Exception):
            await c.execute(
                "INSERT INTO coach_recurrence "
                "(project_id, kind, cadence, enabled) "
                "VALUES ('misc', 'tick', '30', 1)"
            )
            await c.commit()
    finally:
        await c.close()


# --- Env migration ----------------------------------------------------


async def test_env_migration_seeds_tick_row(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "300")
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT cadence, prompt, enabled, created_by "
            "FROM coach_recurrence WHERE project_id = 'misc'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    d = dict(row)
    assert d["cadence"] == "5"  # 300s -> 5 min
    assert d["prompt"] is None
    assert d["enabled"] == 1
    assert d["created_by"] == "env_migration"


async def test_env_migration_idempotent(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "60")
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM coach_recurrence "
            "WHERE project_id = 'misc'"
        )
        n_before = dict(await cur.fetchone())["n"]
    finally:
        await c.close()
    # Second init_db pass — env var still set, but migration flag now
    # exists, so no new rows. (Sets the flag only after success.)
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM coach_recurrence "
            "WHERE project_id = 'misc'"
        )
        n_after = dict(await cur.fetchone())["n"]
    finally:
        await c.close()
    assert n_before == 1
    assert n_after == 1


async def test_env_migration_skips_when_zero(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM coach_recurrence"
        )
        n = dict(await cur.fetchone())["n"]
        # Flag is still set so a later non-zero env var won't seed.
        cur = await c.execute(
            "SELECT value FROM team_config "
            "WHERE key = 'recurrence_v1_seeded'"
        )
        flag = await cur.fetchone()
    finally:
        await c.close()
    assert n == 0
    assert flag is not None


# --- DSL parser -------------------------------------------------------


def test_parse_cron_daily() -> None:
    p = recmod.parse_cron("daily 09:00")
    assert p == {"type": "daily", "time": time(9, 0)}


def test_parse_cron_weekdays() -> None:
    p = recmod.parse_cron("weekdays 18:00")
    assert p == {"type": "weekdays", "time": time(18, 0)}


def test_parse_cron_weekends() -> None:
    p = recmod.parse_cron("weekends 10:00")
    assert p == {"type": "weekends", "time": time(10, 0)}


def test_parse_cron_day_list() -> None:
    p = recmod.parse_cron("mon,thu 14:00")
    assert p == {"type": "weekly", "days": [0, 3], "time": time(14, 0)}


def test_parse_cron_weekly_explicit() -> None:
    p = recmod.parse_cron("weekly mon 09:00")
    assert p == {"type": "weekly", "days": [0], "time": time(9, 0)}


def test_parse_cron_monthly() -> None:
    p = recmod.parse_cron("monthly 1 09:00")
    assert p == {"type": "monthly", "day": 1, "time": time(9, 0)}


def test_parse_cron_one_shot() -> None:
    p = recmod.parse_cron("2026-05-01 10:00")
    assert p == {
        "type": "once",
        "date": date(2026, 5, 1),
        "time": time(10, 0),
    }
    assert recmod.is_one_shot(p)


def test_parse_cron_rejects_invalid() -> None:
    for bad in ["", "garbage", "daily", "daily 25:00",
                "weekly xyz 09:00", "monthly 32 09:00",
                "monthly 0 09:00", "2026-13-01 10:00"]:
        with pytest.raises(recmod.CronParseError):
            recmod.parse_cron(bad)


# --- compute_next_fire_at ---------------------------------------------


def _utc(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_compute_next_daily_advances_to_tomorrow() -> None:
    # 10:00 UTC, schedule daily 09:00 UTC -> next is tomorrow 09:00.
    now = _utc(2026, 4, 28, 10, 0)
    nxt = recmod.compute_next_fire_at(
        recmod.parse_cron("daily 09:00"), "UTC", now,
    )
    assert nxt == _utc(2026, 4, 29, 9, 0)


def test_compute_next_daily_today_when_before() -> None:
    now = _utc(2026, 4, 28, 8, 0)
    nxt = recmod.compute_next_fire_at(
        recmod.parse_cron("daily 09:00"), "UTC", now,
    )
    assert nxt == _utc(2026, 4, 28, 9, 0)


def test_compute_next_weekdays_skips_weekend() -> None:
    # Friday 18:01 -> next weekday 18:00 is Monday.
    now = _utc(2026, 5, 1, 18, 1)  # Friday
    nxt = recmod.compute_next_fire_at(
        recmod.parse_cron("weekdays 18:00"), "UTC", now,
    )
    assert nxt == _utc(2026, 5, 4, 18, 0)  # Monday


def test_compute_next_weekly_picks_earliest_match() -> None:
    # Tuesday before 14:00 -> Thursday this week.
    now = _utc(2026, 4, 28, 9, 0)  # Tuesday
    nxt = recmod.compute_next_fire_at(
        recmod.parse_cron("mon,thu 14:00"), "UTC", now,
    )
    assert nxt == _utc(2026, 4, 30, 14, 0)  # Thursday


def test_compute_next_monthly_skips_invalid_day() -> None:
    # monthly 31, starting Feb 1 -> March 31 (Feb has no 31).
    now = _utc(2026, 2, 1, 0, 0)
    nxt = recmod.compute_next_fire_at(
        recmod.parse_cron("monthly 31 09:00"), "UTC", now,
    )
    assert nxt == _utc(2026, 3, 31, 9, 0)


def test_compute_next_one_shot_returns_none_when_past() -> None:
    now = _utc(2026, 5, 2, 0, 0)
    nxt = recmod.compute_next_fire_at(
        recmod.parse_cron("2026-05-01 10:00"), "UTC", now,
    )
    assert nxt is None


def test_compute_next_one_shot_returns_future() -> None:
    now = _utc(2026, 4, 28, 0, 0)
    nxt = recmod.compute_next_fire_at(
        recmod.parse_cron("2026-05-01 10:00"), "UTC", now,
    )
    assert nxt == _utc(2026, 5, 1, 10, 0)


def test_compute_next_respects_tz() -> None:
    # 2026-04-28 — Europe/Paris is UTC+2 (DST).
    # daily 09:00 Paris -> 07:00 UTC.
    now = _utc(2026, 4, 28, 6, 0)
    nxt = recmod.compute_next_fire_at(
        recmod.parse_cron("daily 09:00"), "Europe/Paris", now,
    )
    assert nxt == _utc(2026, 4, 28, 7, 0)


# --- Scheduler iteration ---------------------------------------------


async def _fake_run_agent(*args: Any, **kwargs: Any) -> None:
    return None


async def test_scheduler_fires_due_tick(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    c = await configured_conn()
    try:
        # Past next_fire_at — should fire immediately.
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, enabled, next_fire_at) "
            "VALUES ('misc', 'tick', '60', 1, "
            "'2020-01-01T00:00:00.000Z')"
        )
        await c.commit()
    finally:
        await c.close()

    fired: list[tuple[str, str]] = []

    async def fake_run(agent_id: str, prompt: str, **kw: Any) -> None:
        fired.append((agent_id, prompt))

    async def fake_busy() -> bool:
        return False

    def fake_paused() -> bool:
        return False

    with patch("server.agents.run_agent", fake_run), \
            patch("server.agents._coach_is_working", fake_busy), \
            patch("server.agents.is_paused", fake_paused):
        await recmod._scheduler_iteration()

    assert len(fired) == 1
    assert fired[0][0] == "coach"

    # next_fire_at advanced to ~now+60min.
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT next_fire_at, last_fired_at FROM coach_recurrence"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["last_fired_at"] is not None
    assert row["next_fire_at"] is not None
    nxt = recmod._parse_iso(row["next_fire_at"])
    assert nxt > datetime.now(timezone.utc)


async def test_scheduler_skips_when_busy(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, enabled, next_fire_at) "
            "VALUES ('misc', 'tick', '60', 1, "
            "'2020-01-01T00:00:00.000Z')"
        )
        await c.commit()
    finally:
        await c.close()

    fired: list[Any] = []

    async def fake_run(agent_id: str, prompt: str, **kw: Any) -> None:
        fired.append(agent_id)

    async def fake_busy() -> bool:
        return True

    def fake_paused() -> bool:
        return False

    with patch("server.agents.run_agent", fake_run), \
            patch("server.agents._coach_is_working", fake_busy), \
            patch("server.agents.is_paused", fake_paused):
        await recmod._scheduler_iteration()

    assert fired == []


# --- CRUD helpers (phase 2) ------------------------------------------


async def test_create_repeat(fresh_db: str) -> None:
    await init_db()
    row = await recmod.create_recurrence(
        project_id="misc", kind="repeat", cadence="30",
        prompt="summarize new commits",
    )
    assert row["id"] > 0
    assert row["kind"] == "repeat"
    assert row["cadence"] == "30"
    assert row["prompt"] == "summarize new commits"
    assert row["enabled"] is True
    assert row["next_fire_at"] is not None


async def test_create_cron_validates_dsl(fresh_db: str) -> None:
    await init_db()
    with pytest.raises(ValueError):
        await recmod.create_recurrence(
            project_id="misc", kind="cron", cadence="garbage",
            prompt="x", tz="UTC",
        )


async def test_create_cron_rejects_past_one_shot(fresh_db: str) -> None:
    await init_db()
    with pytest.raises(ValueError, match="past"):
        await recmod.create_recurrence(
            project_id="misc", kind="cron",
            cadence="2020-01-01 10:00", prompt="x", tz="UTC",
        )


async def test_create_cron_validates_tz(fresh_db: str) -> None:
    await init_db()
    with pytest.raises(ValueError, match="timezone"):
        await recmod.create_recurrence(
            project_id="misc", kind="cron",
            cadence="daily 09:00", prompt="x", tz="Mars/Olympus_Mons",
        )


async def test_create_rejects_tick_kind(fresh_db: str) -> None:
    await init_db()
    with pytest.raises(ValueError, match="upsert_tick"):
        await recmod.create_recurrence(
            project_id="misc", kind="tick", cadence="60",
            prompt=None,
        )


async def test_create_enforces_per_project_cap(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(recmod, "MAX_RECURRENCES_PER_PROJECT", 2)
    await init_db()
    await recmod.create_recurrence(
        project_id="misc", kind="repeat", cadence="30", prompt="a",
    )
    await recmod.create_recurrence(
        project_id="misc", kind="repeat", cadence="60", prompt="b",
    )
    with pytest.raises(PermissionError, match="cap"):
        await recmod.create_recurrence(
            project_id="misc", kind="repeat", cadence="120", prompt="c",
        )


async def test_upsert_tick_creates_then_updates(fresh_db: str) -> None:
    await init_db()
    row = await recmod.upsert_tick(project_id="misc", minutes=60)
    assert row is not None and row["cadence"] == "60"
    rid = row["id"]
    row = await recmod.upsert_tick(project_id="misc", minutes=15)
    assert row is not None and row["id"] == rid
    assert row["cadence"] == "15"


async def test_upsert_tick_disable_preserves_row(fresh_db: str) -> None:
    await init_db()
    row = await recmod.upsert_tick(project_id="misc", minutes=60)
    rid = row["id"]
    row = await recmod.upsert_tick(project_id="misc", enabled=False)
    assert row is not None and row["id"] == rid
    assert row["enabled"] is False
    assert row["next_fire_at"] is None


async def test_upsert_tick_disable_when_missing_returns_none(
    fresh_db: str,
) -> None:
    await init_db()
    row = await recmod.upsert_tick(project_id="misc", enabled=False)
    assert row is None


async def test_update_recurrence_changes_cadence_and_recomputes(
    fresh_db: str,
) -> None:
    await init_db()
    row = await recmod.create_recurrence(
        project_id="misc", kind="repeat", cadence="30", prompt="x",
    )
    before_next = row["next_fire_at"]
    updated = await recmod.update_recurrence(row["id"], cadence="120")
    assert updated is not None
    assert updated["cadence"] == "120"
    assert updated["next_fire_at"] != before_next


async def test_update_recurrence_rejects_empty_prompt(
    fresh_db: str,
) -> None:
    await init_db()
    row = await recmod.create_recurrence(
        project_id="misc", kind="repeat", cadence="30", prompt="x",
    )
    with pytest.raises(ValueError):
        await recmod.update_recurrence(row["id"], prompt="   ")


async def test_delete_recurrence(fresh_db: str) -> None:
    await init_db()
    row = await recmod.create_recurrence(
        project_id="misc", kind="repeat", cadence="30", prompt="x",
    )
    ok = await recmod.delete_recurrence(row["id"])
    assert ok is True
    again = await recmod.get_recurrence(row["id"])
    assert again is None


async def test_list_recurrences_orders_kinds(fresh_db: str) -> None:
    await init_db()
    await recmod.create_recurrence(
        project_id="misc", kind="cron",
        cadence="daily 09:00", prompt="c", tz="UTC",
    )
    await recmod.create_recurrence(
        project_id="misc", kind="repeat", cadence="30", prompt="r",
    )
    await recmod.upsert_tick(project_id="misc", minutes=60)
    rows = await recmod.list_recurrences("misc")
    kinds = [r["kind"] for r in rows]
    # tick first, repeats next, crons last (per CASE in SQL)
    assert kinds == ["tick", "repeat", "cron"]


# --- HTTP endpoints --------------------------------------------------


@pytest.fixture
async def client(fresh_db: str):
    """FastAPI test client with token auth disabled. Imports lazily so
    fresh_db's monkeypatch on DB_PATH lands first."""
    from fastapi.testclient import TestClient
    import server.main as mainmod
    # Drop any HARNESS_TOKEN value the env may have set so tests don't
    # need to pass an auth header.
    mainmod.HARNESS_TOKEN = ""
    await init_db()
    with TestClient(mainmod.app) as c:
        yield c


async def test_http_create_repeat(client) -> None:
    r = client.post("/api/recurrences", json={
        "kind": "repeat", "cadence": "30",
        "prompt": "summarize commits",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "repeat"
    assert body["cadence"] == "30"


async def test_http_create_rejects_bad_cron(client) -> None:
    r = client.post("/api/recurrences", json={
        "kind": "cron", "cadence": "wat 09:00", "prompt": "x", "tz": "UTC",
    })
    assert r.status_code == 400


async def test_http_create_rejects_unknown_kind(client) -> None:
    r = client.post("/api/recurrences", json={
        "kind": "tick", "cadence": "60", "prompt": "x",
    })
    # Pydantic regex catches "tick" → 422.
    assert r.status_code in (400, 422)


async def test_http_list_returns_active(client) -> None:
    client.post("/api/recurrences", json={
        "kind": "repeat", "cadence": "30", "prompt": "x",
    })
    r = client.get("/api/recurrences")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["kind"] == "repeat" for row in rows)


async def test_http_patch_changes_prompt(client) -> None:
    r = client.post("/api/recurrences", json={
        "kind": "repeat", "cadence": "30", "prompt": "before",
    })
    rid = r.json()["id"]
    r = client.patch(f"/api/recurrences/{rid}", json={"prompt": "after"})
    assert r.status_code == 200
    assert r.json()["prompt"] == "after"


async def test_http_delete_removes_row(client) -> None:
    r = client.post("/api/recurrences", json={
        "kind": "repeat", "cadence": "30", "prompt": "x",
    })
    rid = r.json()["id"]
    r = client.delete(f"/api/recurrences/{rid}")
    assert r.status_code == 200
    r = client.delete(f"/api/recurrences/{rid}")
    assert r.status_code == 404


async def test_http_put_coach_tick_creates_tick_row(client) -> None:
    r = client.put("/api/coach/tick", json={"minutes": 45})
    assert r.status_code == 200
    body = r.json()
    assert body["row"]["kind"] == "tick"
    assert body["row"]["cadence"] == "45"
    assert body["row"]["enabled"] is True


async def test_http_put_coach_tick_disable(client) -> None:
    client.put("/api/coach/tick", json={"minutes": 45})
    r = client.put("/api/coach/tick", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["row"]["enabled"] is False


async def test_http_put_coach_tick_requires_field(client) -> None:
    r = client.put("/api/coach/tick", json={})
    assert r.status_code == 400


# --- Smart tick prompt composer (phase 5) ----------------------------


async def test_compose_tick_prompt_base_when_objectives_present(
    fresh_db: str,
) -> None:
    await init_db()
    from server.paths import ensure_project_scaffold, project_paths
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    pp.project_objectives.write_text("Be brilliant.\n", encoding="utf-8")
    out = await recmod.compose_tick_prompt("misc")
    assert out.startswith("Routine tick.")
    # Priority order spelled out in the prompt — inbox, todos, objectives.
    assert "Inbox" in out
    assert "coach-todos" in out
    assert "Project objectives" in out
    # Elicitation note must NOT appear when objectives exist.
    assert "missing or empty" not in out


async def test_compose_tick_prompt_appends_elicitation_first_time(
    fresh_db: str,
) -> None:
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    out = await recmod.compose_tick_prompt("misc")
    assert "Routine tick." in out
    assert "missing or empty" in out
    assert "What are we trying to accomplish" in out
    assert "project-objectives.md" in out


async def test_compose_tick_prompt_skips_elicitation_after_asked(
    fresh_db: str,
) -> None:
    """Spec §15.5: subsequent empty-objectives ticks end quietly —
    the harness must not pester Coach with the same elicitation.
    The prompt composer suppresses the hint after Coach actually sends
    the objectives-asking message."""
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    first = await recmod.compose_tick_prompt("misc")
    assert "missing or empty" in first
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO events (ts, agent_id, project_id, type, payload) "
            "VALUES (?, 'coach', 'misc', 'message_sent', ?)",
            (
                "2026-04-29T00:00:00Z",
                json.dumps(
                    {
                        "to": "human",
                        "body": (
                            "This project has no objectives defined. "
                            "What are we trying to accomplish? Once you reply, "
                            "I'll save them to project-objectives.md."
                        ),
                    }
                ),
            ),
        )
        await c.commit()
    finally:
        await c.close()
    second = await recmod.compose_tick_prompt("misc")
    assert "missing or empty" not in second
    assert second == recmod.TICK_BASE_PROMPT


async def test_compose_tick_prompt_resets_after_objectives_saved(
    fresh_db: str,
) -> None:
    """If objectives go missing again later, the elicitation should
    re-fire. The reset happens whenever a tick observes objectives
    are present."""
    await init_db()
    from server.paths import ensure_project_scaffold, project_paths
    ensure_project_scaffold("misc")
    pp = project_paths("misc")
    # First empty-objectives tick includes the hint but does not stamp
    # anything until Coach actually sends the question.
    await recmod.compose_tick_prompt("misc")
    # Operator saves objectives.
    pp.project_objectives.write_text("Be brilliant.\n", encoding="utf-8")
    out = await recmod.compose_tick_prompt("misc")
    assert "missing or empty" not in out
    # Operator deletes objectives later.
    pp.project_objectives.unlink()
    out = await recmod.compose_tick_prompt("misc")
    assert "missing or empty" in out


def test_tick_base_prompt_constant_matches_spec() -> None:
    # Spec §4 — the tick prompt orients Coach to a priority order:
    # inbox → coach-todos → objectives → end-quietly. The empty-state
    # branch (objectives) explicitly directs Coach to take a concrete
    # action rather than just "advance" something vague. Tests pin
    # the structural pieces, not the verbatim string, so wording can
    # be tuned without breaking unrelated tests.
    p = recmod.TICK_BASE_PROMPT
    assert p.startswith("Routine tick.")
    assert "Inbox" in p
    assert "coord_read_inbox" in p
    assert "coach-todos" in p
    assert "Project objectives" in p
    # Concrete action language for the empty-state branch — not just
    # "advance objectives" which is what the original wording said.
    assert "concrete action" in p
    # Empty-state guard preserved: do not invent work.
    assert "Don't invent work" in p
    assert "without calling tools" in p


async def test_scheduler_uses_compose_tick_prompt(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduler tick fires use the smart composer, not the legacy
    constant. Sets up an objectives-missing project to assert the
    elicitation note flows through."""
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    from server.paths import ensure_project_scaffold
    ensure_project_scaffold("misc")
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, enabled, next_fire_at) "
            "VALUES ('misc', 'tick', '60', 1, "
            "'2020-01-01T00:00:00.000Z')"
        )
        await c.commit()
    finally:
        await c.close()

    fired_prompts: list[str] = []

    async def fake_run(agent_id: str, prompt: str, **kw: Any) -> None:
        fired_prompts.append(prompt)

    async def fake_busy() -> bool:
        return False

    def fake_paused() -> bool:
        return False

    async def fake_caps(agent_id: str) -> tuple[bool, str]:
        return True, ""

    with patch("server.agents.run_agent", fake_run), \
            patch("server.agents._coach_is_working", fake_busy), \
            patch("server.agents.is_paused", fake_paused), \
            patch("server.agents._check_cost_caps", fake_caps):
        await recmod._scheduler_iteration()

    assert len(fired_prompts) == 1
    assert "Routine tick." in fired_prompts[0]
    # No objectives file → elicitation hint included.
    assert "missing or empty" in fired_prompts[0]


async def test_scheduler_skips_when_cost_capped(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§15.9 — when over the daily cost cap, fires are skipped with
    reason=cost_capped instead of letting run_agent emit cost_capped."""
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, enabled, next_fire_at) "
            "VALUES ('misc', 'tick', '60', 1, "
            "'2020-01-01T00:00:00.000Z')"
        )
        await c.commit()
    finally:
        await c.close()

    fired: list[Any] = []
    skipped: list[dict[str, Any]] = []

    async def fake_run(agent_id: str, prompt: str, **kw: Any) -> None:
        fired.append(agent_id)

    async def fake_busy() -> bool:
        return False

    def fake_paused() -> bool:
        return False

    async def fake_caps(agent_id: str) -> tuple[bool, str]:
        return False, "over budget"

    real_publish = recmod.bus.publish

    async def capture_publish(event: dict[str, Any]) -> None:
        if event.get("type") == "recurrence_skipped":
            skipped.append(event)
        await real_publish(event)

    with patch("server.agents.run_agent", fake_run), \
            patch("server.agents._coach_is_working", fake_busy), \
            patch("server.agents.is_paused", fake_paused), \
            patch("server.agents._check_cost_caps", fake_caps), \
            patch.object(recmod.bus, "publish", capture_publish):
        await recmod._scheduler_iteration()

    assert fired == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "cost_capped"
    assert skipped[0]["kind"] == "tick"


async def test_recurrence_fired_payload_uses_id_key(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §13: the recurrence_fired payload uses `id`, not
    `recurrence_id`. Locks the contract so UI consumers can rely on it."""
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, enabled, next_fire_at) "
            "VALUES ('misc', 'tick', '60', 1, "
            "'2020-01-01T00:00:00.000Z')"
        )
        await c.commit()
    finally:
        await c.close()

    fired_events: list[dict[str, Any]] = []

    async def fake_run(agent_id: str, prompt: str, **kw: Any) -> None:
        return None

    async def fake_busy() -> bool:
        return False

    def fake_paused() -> bool:
        return False

    async def fake_caps(agent_id: str) -> tuple[bool, str]:
        return True, ""

    real_publish = recmod.bus.publish

    async def capture_publish(event: dict[str, Any]) -> None:
        if event.get("type") == "recurrence_fired":
            fired_events.append(event)
        await real_publish(event)

    with patch("server.agents.run_agent", fake_run), \
            patch("server.agents._coach_is_working", fake_busy), \
            patch("server.agents.is_paused", fake_paused), \
            patch("server.agents._check_cost_caps", fake_caps), \
            patch.object(recmod.bus, "publish", capture_publish):
        await recmod._scheduler_iteration()

    assert len(fired_events) == 1
    payload = fired_events[0]
    assert "id" in payload, "spec §13 requires `id` key on recurrence_fired"
    assert "recurrence_id" not in payload
    assert payload["kind"] == "tick"


async def test_scheduler_one_shot_disables_after_fire(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, tz, prompt, enabled, "
            "next_fire_at) "
            "VALUES ('misc', 'cron', '2020-01-01 10:00', 'UTC', "
            "'fire-once', 1, '2020-01-01T10:00:00.000Z')"
        )
        await c.commit()
    finally:
        await c.close()

    async def fake_run(agent_id: str, prompt: str, **kw: Any) -> None:
        return None

    async def fake_busy() -> bool:
        return False

    def fake_paused() -> bool:
        return False

    with patch("server.agents.run_agent", fake_run), \
            patch("server.agents._coach_is_working", fake_busy), \
            patch("server.agents.is_paused", fake_paused):
        await recmod._scheduler_iteration()

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT enabled, next_fire_at FROM coach_recurrence"
        )
        row = dict(await cur.fetchone())
    finally:
        await c.close()
    assert row["enabled"] == 0
    assert row["next_fire_at"] is None


# --- Audit gap regressions -------------------------------------------


async def test_scheduler_only_fires_one_row_per_pass(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §15.1: when multiple rows are due in the same scheduler
    pass, only the first one fires; the rest skip with reason=
    coach_busy. The previous implementation awaited the full Coach
    turn before checking the next row, so all due rows fired
    back-to-back — fixed by tracking a fired_in_pass flag."""
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    c = await configured_conn()
    try:
        # Two due repeats — both have past next_fire_at.
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, prompt, enabled, next_fire_at) "
            "VALUES ('misc', 'repeat', '30', 'A', 1, "
            "'2020-01-01T00:00:00.000Z')"
        )
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, prompt, enabled, next_fire_at) "
            "VALUES ('misc', 'repeat', '30', 'B', 1, "
            "'2020-01-01T00:00:00.000Z')"
        )
        await c.commit()
    finally:
        await c.close()

    fired: list[str] = []
    skipped: list[dict[str, Any]] = []

    async def fake_run(agent_id: str, prompt: str, **kw: Any) -> None:
        fired.append(prompt)

    async def fake_busy() -> bool:
        return False

    def fake_paused() -> bool:
        return False

    async def fake_caps(agent_id: str) -> tuple[bool, str]:
        return True, ""

    real_publish = recmod.bus.publish

    async def capture(event: dict[str, Any]) -> None:
        if event.get("type") == "recurrence_skipped":
            skipped.append(event)
        await real_publish(event)

    with patch("server.agents.run_agent", fake_run), \
            patch("server.agents._coach_is_working", fake_busy), \
            patch("server.agents.is_paused", fake_paused), \
            patch("server.agents._check_cost_caps", fake_caps), \
            patch.object(recmod.bus, "publish", capture):
        await recmod._scheduler_iteration()

    # Exactly one fire.
    assert len(fired) == 1
    # And exactly one skip with reason=coach_busy.
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "coach_busy"


async def test_one_shot_busy_emits_skipped_then_disabled(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §11: skipped fires must be recorded. A one-shot cron
    that's both busy AND past should emit recurrence_skipped before
    being disabled — the operator needs to see WHY it was skipped
    before the row goes silent."""
    monkeypatch.setenv("HARNESS_COACH_TICK_INTERVAL", "0")
    await init_db()
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, tz, prompt, enabled, "
            "next_fire_at) "
            "VALUES ('misc', 'cron', '2020-01-01 10:00', 'UTC', "
            "'once', 1, '2020-01-01T10:00:00.000Z')"
        )
        await c.commit()
    finally:
        await c.close()

    events: list[dict[str, Any]] = []

    async def fake_run(*a: Any, **kw: Any) -> None:
        return None

    async def fake_busy() -> bool:
        return True  # Force the busy path.

    def fake_paused() -> bool:
        return False

    real_publish = recmod.bus.publish

    async def capture(event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t.startswith("recurrence_"):
            events.append(event)
        await real_publish(event)

    with patch("server.agents.run_agent", fake_run), \
            patch("server.agents._coach_is_working", fake_busy), \
            patch("server.agents.is_paused", fake_paused), \
            patch.object(recmod.bus, "publish", capture):
        await recmod._scheduler_iteration()

    types = [e["type"] for e in events]
    # Both events present, in order: skip first, disable second.
    assert "recurrence_skipped" in types
    assert "recurrence_disabled" in types
    assert types.index("recurrence_skipped") < types.index(
        "recurrence_disabled"
    )


async def test_cron_grammar_rejects_single_digit_hour(fresh_db: str) -> None:
    """Spec §5.1: TIME = HH:MM. Single-digit hour is now rejected."""
    with pytest.raises(recmod.CronParseError):
        recmod.parse_cron("daily 9:00")


async def test_cron_grammar_rejects_bare_single_day(fresh_db: str) -> None:
    """Spec §5.1: bare DAY_LIST requires ≥2 days. Single days must
    use `weekly DAY TIME` shorthand."""
    with pytest.raises(recmod.CronParseError):
        recmod.parse_cron("mon 09:00")


async def test_tick_added_event_includes_tz_and_prompt(
    fresh_db: str,
) -> None:
    """Spec §13: recurrence_added payload = id, kind, cadence, tz,
    prompt. tick rows have null tz/prompt but the keys still exist
    so consumers can index uniformly."""
    await init_db()
    captured: list[dict[str, Any]] = []
    real_publish = recmod.bus.publish

    async def capture(event: dict[str, Any]) -> None:
        if event.get("type") == "recurrence_added":
            captured.append(event)
        await real_publish(event)

    with patch.object(recmod.bus, "publish", capture):
        await recmod.upsert_tick(project_id="misc", minutes=60)

    assert len(captured) == 1
    ev = captured[0]
    assert "tz" in ev
    assert "prompt" in ev
    assert ev["tz"] is None
    assert ev["prompt"] is None


async def test_tick_changed_event_uses_before_after(
    fresh_db: str,
) -> None:
    """Spec §13: recurrence_changed payload has `before` and `after`
    snapshots."""
    await init_db()
    await recmod.upsert_tick(project_id="misc", minutes=60)
    captured: list[dict[str, Any]] = []
    real_publish = recmod.bus.publish

    async def capture(event: dict[str, Any]) -> None:
        if event.get("type") == "recurrence_changed":
            captured.append(event)
        await real_publish(event)

    with patch.object(recmod.bus, "publish", capture):
        await recmod.upsert_tick(project_id="misc", minutes=15)

    assert len(captured) == 1
    ev = captured[0]
    assert ev["before"]["cadence"] == "60"
    assert ev["after"]["cadence"] == "15"


async def test_schema_version_stamped(fresh_db: str) -> None:
    """Spec §10: migration recurrence_v1 stamps team_config.schema_version."""
    await init_db()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = 'schema_version'"
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    assert dict(row)["value"] == "recurrence_v1"
