"""Tests for `server.telegram_escalation`.

The watcher subscribes to the event bus, schedules a per-item asyncio
timer for each `pending_question(route='human')` /
`pending_plan(route='human')` / `file_write_proposal_created`, and
calls `server.telegram.send_outbound` when the delay expires unless a
matching resolution event cancels the timer first.

Coverage here:
  - Pure key extraction (pending + resolution).
  - Schedule + cancel via the resolution event (no telegram fire).
  - Fire-on-timeout when no resolution arrives.
  - Web-inactive path uses the short grace delay.
  - Watcher disabled (delay=0) is a hard no-op even on watched events.
  - route='coach' pending events are ignored — Coach handles those.
  - Message formatters include extra context (agent label, body
    preview) and don't blow up on missing fields.
  - Idempotent start / stop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.db import configured_conn, init_db
from server.events import bus, EventBus
from server import telegram_escalation as esc


# ----------------------------------------------------- helpers


def _stub_send(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace `send_outbound` with a recorder. Returns the list it
    appends to so tests can assert on actual calls."""
    sent: list[str] = []

    async def fake(text: str) -> bool:
        sent.append(text)
        return True

    monkeypatch.setattr(esc, "send_outbound", fake)
    return sent


def _stub_send_disabled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`send_outbound` returns False (bridge disabled / unconfigured).
    The watcher should still attempt — we want to see it tried and
    silently no-op'd."""
    sent: list[str] = []

    async def fake(text: str) -> bool:
        sent.append(text)
        return False

    monkeypatch.setattr(esc, "send_outbound", fake)
    return sent


async def _drain(seconds: float = 0.05) -> None:
    """Yield control so scheduled tasks get a chance to run."""
    await asyncio.sleep(seconds)


async def _seed_task(
    task_id: str,
    *,
    title: str = "Fix audit target",
    status: str = "execute",
    owner: str = "p3",
    priority: str = "high",
) -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, priority) VALUES (?, 'misc', ?, ?, ?, 'coach', ?)",
            (task_id, title, status, owner, priority),
        )
        await c.commit()
    finally:
        await c.close()


# ----------------------------------------------------- key extraction


def test_key_for_pending_question_human() -> None:
    ev = {
        "type": "pending_question",
        "route": "human",
        "correlation_id": "abc",
    }
    assert esc._key_for_pending(ev) == ("question", "abc")


def test_key_for_pending_question_coach_is_ignored() -> None:
    """When the question is routed at Coach (not the human), the
    watcher should ignore it — Coach is responsible, not the human."""
    ev = {
        "type": "pending_question",
        "route": "coach",
        "correlation_id": "abc",
    }
    assert esc._key_for_pending(ev) is None


def test_key_for_pending_plan_human() -> None:
    ev = {
        "type": "pending_plan",
        "route": "human",
        "correlation_id": "xyz",
    }
    assert esc._key_for_pending(ev) == ("plan", "xyz")


def test_key_for_pending_plan_coach_is_ignored() -> None:
    ev = {
        "type": "pending_plan",
        "route": "coach",
        "correlation_id": "xyz",
    }
    assert esc._key_for_pending(ev) is None


def test_key_for_file_write_proposal() -> None:
    ev = {"type": "file_write_proposal_created", "proposal_id": 42}
    assert esc._key_for_pending(ev) == ("proposal", "42")


def test_key_for_unrelated_event() -> None:
    assert esc._key_for_pending({"type": "agent_started"}) is None
    assert esc._key_for_pending({"type": "text", "content": "hi"}) is None
    assert esc._key_for_pending({}) is None


def test_resolution_keys_match_pending_keys() -> None:
    """Every resolution flavour we recognise should map to a key
    shape that matches what `_key_for_pending` produces — otherwise
    we'd schedule but never cancel."""
    assert esc._key_for_resolution(
        {"type": "question_answered", "correlation_id": "a"}
    ) == ("question", "a")
    assert esc._key_for_resolution(
        {"type": "question_cancelled", "correlation_id": "a"}
    ) == ("question", "a")
    assert esc._key_for_resolution(
        {"type": "plan_decided", "correlation_id": "b"}
    ) == ("plan", "b")
    assert esc._key_for_resolution(
        {"type": "plan_cancelled", "correlation_id": "b"}
    ) == ("plan", "b")
    for t in (
        "file_write_proposal_approved",
        "file_write_proposal_denied",
        "file_write_proposal_cancelled",
        "file_write_proposal_superseded",
    ):
        assert esc._key_for_resolution({"type": t, "proposal_id": 7}) == ("proposal", "7")


# ----------------------------------------------------- env knobs


def test_delay_seconds_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", raising=False)
    assert esc._delay_seconds() == 300


def test_delay_seconds_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "0")
    assert esc._delay_seconds() == 0


def test_delay_seconds_negative_clamps_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "-30")
    assert esc._delay_seconds() == 0


def test_delay_seconds_invalid_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "not-a-number")
    # Falls back to default rather than disabling — silent typos
    # should never silently drop alerts.
    assert esc._delay_seconds() == 300


# ----------------------------------------------------- formatters


async def test_format_question_msg_minimal(fresh_db: str) -> None:
    """Formatter survives a sparse pending_question payload (no
    questions array, no body, no deadline)."""
    await init_db()
    text = await esc._format_question_msg({
        "type": "pending_question",
        "agent_id": "coach",
        "ts": "2026-05-02T12:34:56Z",
    })
    assert "Question from coach" in text
    assert "12:34" in text
    assert "Open the web UI to answer." in text


async def test_format_question_msg_with_questions(fresh_db: str) -> None:
    """Formatter folds in the structured questions array — that's the
    actual context the user needs to decide whether to drop everything
    and answer."""
    await init_db()
    text = await esc._format_question_msg({
        "type": "pending_question",
        "agent_id": "coach",
        "questions": [
            {
                "question": "Should we ship the migration tonight?",
                "options": [{"label": "yes"}, {"label": "no"}],
            },
        ],
    })
    assert "ship the migration tonight" in text
    assert "yes / no" in text


async def test_format_plan_msg_truncates_long_plan(fresh_db: str) -> None:
    """Plans can be very long; the formatter must trim."""
    await init_db()
    long_plan = "step\n" * 5000  # ~25k chars
    text = await esc._format_plan_msg({
        "agent_id": "p3",
        "plan": long_plan,
    })
    # Truncation marker present; full plan absent.
    assert "…" in text
    assert len(text) < 5000


async def test_format_proposal_msg_includes_context(fresh_db: str) -> None:
    """File-write proposal: scope + path + summary all surface."""
    await init_db()
    text = await esc._format_proposal_msg({
        "agent_id": "coach",
        "scope": "truth",
        "path": "specs/auth.md",
        "summary": "Add note about session token rotation.",
        "size": 1234,
    })
    assert "truth/specs/auth.md" in text
    assert "scope=truth" in text
    assert "1234" in text
    assert "session token rotation" in text


async def test_format_proposal_msg_claude_md_label(fresh_db: str) -> None:
    """`project_claude_md` scope is rendered as 'CLAUDE.md', not the
    raw path."""
    await init_db()
    text = await esc._format_proposal_msg({
        "agent_id": "coach",
        "scope": "project_claude_md",
        "path": "CLAUDE.md",
        "summary": "Document the new escalation watcher.",
        "size": 100,
    })
    assert "CLAUDE.md" in text
    assert "scope=project_claude_md" in text


async def test_format_message_dispatches_by_kind(fresh_db: str) -> None:
    await init_db()
    q = await esc._format_message("question", {"agent_id": "coach"})
    p = await esc._format_message("plan", {"agent_id": "p1", "plan": "do thing"})
    f = await esc._format_message("proposal", {
        "agent_id": "coach", "scope": "truth", "path": "x.md", "summary": "s", "size": 1,
    })
    assert "Question from" in q
    assert "Plan approval from" in p
    assert "File-write proposal from" in f
    # Unknown kind → empty (caller skips the send).
    assert await esc._format_message("???", {}) == ""


# ----------------------------------------------------- schedule + cancel


@pytest.fixture(autouse=True)
def _stub_subscriber_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default subscriber_count to 0 (web inactive) so tests use the
    short grace path. Tests that need the long-delay branch override
    this fixture explicitly."""
    monkeypatch.setattr(
        type(bus), "subscriber_count", property(lambda self: 0),
    )


@pytest.fixture(autouse=True)
def _short_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force short delays so tests don't sit around. The defaults
    are 300s / 5s — way too long. We pick numbers that still let us
    distinguish the two branches in tests that care about which
    branch fired."""
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "1")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "0")


@pytest.fixture(autouse=True)
async def _isolate_pending() -> Any:
    """Each test gets a fresh `_pending` map. Otherwise tasks from
    earlier tests can leak across the suite (the watcher is a
    module-level singleton)."""
    esc._pending.clear()
    yield
    # Cancel any leftover timers so we don't pollute the next test.
    for key, task in list(esc._pending.items()):
        if not task.done():
            task.cancel()
    esc._pending.clear()


async def test_schedule_cancels_on_resolution(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending → resolution before timer fires → no telegram send."""
    await init_db()
    sent = _stub_send(monkeypatch)

    # Use a long delay so we know the timer hasn't fired naturally.
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "60")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "60")

    await esc._handle_event({
        "type": "pending_question",
        "route": "human",
        "agent_id": "coach",
        "correlation_id": "c1",
    })
    assert ("question", "c1") in esc._pending

    await esc._handle_event({
        "type": "question_answered",
        "correlation_id": "c1",
    })
    # Cancellation pops the key.
    assert ("question", "c1") not in esc._pending

    # Even after a generous yield, no message goes out.
    await _drain(0.1)
    assert sent == []


async def test_fire_when_no_resolution(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending arrives, nobody resolves, timer expires → telegram send
    fires once with the formatted body."""
    await init_db()
    sent = _stub_send(monkeypatch)

    # Use the grace path (subscriber_count=0 from the fixture, plus
    # GRACE=0 means "fire on the next event-loop tick").
    await esc._handle_event({
        "type": "pending_plan",
        "route": "human",
        "agent_id": "p2",
        "correlation_id": "plan-1",
        "plan": "Refactor the auth middleware.",
    })

    # Give the task time to wake from sleep(0) and run.
    await _drain(0.1)
    assert len(sent) == 1
    assert "Plan approval from p2" in sent[0]
    assert "Refactor the auth middleware" in sent[0]


async def test_fire_uses_long_delay_when_web_active(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the bus has subscribers, the long delay path is taken so
    the user has a chance to respond on the web before the phone pings."""
    await init_db()
    sent = _stub_send(monkeypatch)

    # Pretend the web is connected.
    monkeypatch.setattr(
        type(bus), "subscriber_count", property(lambda self: 3),
    )
    # Set the long delay to something we can wait out, the grace to
    # zero. If the watcher takes the wrong path, the test would either
    # fire instantly (grace) or never (default 300s).
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "1")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "0")

    await esc._handle_event({
        "type": "pending_question",
        "route": "human",
        "agent_id": "coach",
        "correlation_id": "c-active",
        "questions": [{"question": "ok?"}],
    })

    # Half the delay window — should NOT have fired yet on the long
    # path.
    await _drain(0.3)
    assert sent == []

    # Past the delay window — should fire now.
    await _drain(1.0)
    assert len(sent) == 1


async def test_disabled_does_not_schedule(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delay=0 → watcher is a no-op; no timer registered."""
    await init_db()
    sent = _stub_send(monkeypatch)

    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "0")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "0")

    await esc._handle_event({
        "type": "pending_question",
        "route": "human",
        "agent_id": "coach",
        "correlation_id": "x",
    })
    assert ("question", "x") not in esc._pending

    await _drain(0.1)
    assert sent == []


async def test_silently_noops_when_telegram_disabled(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge unconfigured → `send_outbound` returns False; the
    watcher fires but doesn't crash."""
    await init_db()
    sent = _stub_send_disabled(monkeypatch)

    await esc._handle_event({
        "type": "file_write_proposal_created",
        "agent_id": "coach",
        "proposal_id": 7,
        "scope": "truth",
        "path": "x.md",
        "summary": "tweak",
        "size": 50,
    })

    await _drain(0.1)
    # We did call send_outbound; it returned False; no exception.
    assert len(sent) == 1


async def test_route_coach_pending_is_ignored(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `pending_question` routed at Coach (not the human) doesn't
    register a timer at all."""
    await init_db()
    sent = _stub_send(monkeypatch)

    await esc._handle_event({
        "type": "pending_question",
        "route": "coach",
        "agent_id": "p4",
        "correlation_id": "coach-ask",
    })
    assert ("question", "coach-ask") not in esc._pending

    await _drain(0.1)
    assert sent == []


async def test_unrelated_resolution_is_idempotent(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray resolution event with no matching timer is harmless."""
    await init_db()
    _stub_send(monkeypatch)

    # No prior pending event — cancellation should just do nothing.
    await esc._handle_event({
        "type": "question_answered",
        "correlation_id": "ghost",
    })
    assert esc._pending == {}


async def test_duplicate_pending_replaces_timer(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two `pending_question` events with the same correlation_id
    should leave one live timer, not two competing ones."""
    await init_db()
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "60")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "60")
    _stub_send(monkeypatch)

    await esc._handle_event({
        "type": "pending_question",
        "route": "human",
        "agent_id": "coach",
        "correlation_id": "dup",
    })
    first = esc._pending[("question", "dup")]

    await esc._handle_event({
        "type": "pending_question",
        "route": "human",
        "agent_id": "coach",
        "correlation_id": "dup",
    })
    second = esc._pending[("question", "dup")]

    assert first is not second
    # First task got cancelled when the second arrived.
    await _drain(0.05)
    assert first.cancelled() or first.done()


# ----------------------------------------------------- lifecycle


async def test_start_idempotent(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling start twice doesn't spawn two consumers."""
    await init_db()
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "60")

    try:
        await esc.start_escalation_watcher()
        first = esc._current_task
        await esc.start_escalation_watcher()
        second = esc._current_task
        assert first is second
        assert esc.is_running()
    finally:
        await esc.stop_escalation_watcher()
        assert not esc.is_running()


async def test_start_disabled_is_noop(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delay=0 → start does not subscribe."""
    await init_db()
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "0")

    try:
        await esc.start_escalation_watcher()
        assert not esc.is_running()
    finally:
        await esc.stop_escalation_watcher()


async def test_stop_cancels_in_flight_timers(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running timer at shutdown time should be cancelled, not
    fire after the watcher is gone."""
    await init_db()
    sent = _stub_send(monkeypatch)
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "60")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "60")

    await esc.start_escalation_watcher()
    try:
        await esc._handle_event({
            "type": "pending_question",
            "route": "human",
            "agent_id": "coach",
            "correlation_id": "shutdown",
        })
        assert esc.pending_count() == 1
    finally:
        await esc.stop_escalation_watcher()

    # Even if we wait now, no message fires — the timer is gone.
    await _drain(0.1)
    assert sent == []
    assert esc.pending_count() == 0


async def test_full_path_via_bus(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: start the watcher, publish a pending event through
    the real bus, observe the send. Catches subscribe-then-publish
    races that bypass `_handle_event` would miss."""
    await init_db()
    sent = _stub_send(monkeypatch)
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "1")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "0")

    await esc.start_escalation_watcher()
    try:
        await bus.publish({
            "type": "pending_plan",
            "route": "human",
            "agent_id": "p7",
            "correlation_id": "bus-plan",
            "plan": "Drop the legacy auth middleware.",
        })
        # `bus.publish` schedules tasks; wait long enough for the
        # consumer to pick up the event and the grace-zero timer to
        # fire on the next loop tick.
        await _drain(0.2)
        assert len(sent) == 1
        assert "Plan approval from p7" in sent[0]
    finally:
        await esc.stop_escalation_watcher()


# ----------------------------------------------------- kanban escalations


def test_key_for_audit_fail() -> None:
    """audit_report_submitted with verdict='fail' is escalated; pass is not."""
    ev_fail = {
        "type": "audit_report_submitted",
        "task_id": "t-2026-05-03-aaaaaaaa",
        "kind": "syntax",
        "verdict": "fail",
    }
    assert esc._key_for_pending(ev_fail) == ("audit_fail", "t-2026-05-03-aaaaaaaa")
    ev_pass = {**ev_fail, "verdict": "pass"}
    assert esc._key_for_pending(ev_pass) is None


def test_key_for_audit_assignment_needed() -> None:
    ev = {
        "type": "audit_assignment_needed",
        "task_id": "t-2026-05-03-aaaaaaaa",
        "role": "auditor_syntax",
    }
    assert esc._key_for_pending(ev) == (
        "audit_assignment_needed",
        "t-2026-05-03-aaaaaaaa:auditor_syntax",
    )


def test_key_for_audit_self_review() -> None:
    ev = {
        "type": "audit_self_review_warning",
        "task_id": "t-2026-05-03-aaaaaaaa",
        "kind": "semantics",
    }
    assert esc._key_for_pending(ev) == (
        "audit_self_review",
        "t-2026-05-03-aaaaaaaa:semantics",
    )


def test_resolution_task_role_assigned_cancels_audit_assignment_needed() -> None:
    """When Coach finally fills the role, the assignment-needed
    timer should be cancelled."""
    ev = {
        "type": "task_role_assigned",
        "task_id": "t-2026-05-03-aaaaaaaa",
        "role": "auditor_syntax",
    }
    assert esc._key_for_resolution(ev) == (
        "audit_assignment_needed",
        "t-2026-05-03-aaaaaaaa:auditor_syntax",
    )


async def test_audit_fail_fires_with_message_body(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a fail verdict on the bus → watcher schedules timer
    → no resolution → Telegram outbound fires with a useful body."""
    await init_db()
    sent = _stub_send(monkeypatch)
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "1")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "0")

    await esc.start_escalation_watcher()
    try:
        await bus.publish({
            "type": "audit_report_submitted",
            "task_id": "t-2026-05-03-bbbbbbbb",
            "kind": "syntax",
            "verdict": "fail",
            "auditor_id": "p4",
            "round": 2,
            "report_path": "audits/audit_2_syntax.md",
            "ts": "2026-05-03T14:22:11+00:00",
        })
        await _drain(0.2)
        assert len(sent) == 1
        body = sent[0]
        assert "Audit fail" in body
        assert "t-2026-05-03-bbbbbbbb" in body
        assert "syntax" in body
        assert "p4" in body
        assert "round 2" in body
        assert "audit_2_syntax.md" in body
    finally:
        await esc.stop_escalation_watcher()


async def test_audit_fail_formatter_includes_task_context(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task(
        "t-2026-05-03-context1",
        title="Repair checkout flow",
        status="execute",
        owner="p6",
        priority="urgent",
    )
    body = await esc._format_audit_fail_msg({
        "type": "audit_report_submitted",
        "task_id": "t-2026-05-03-context1",
        "kind": "semantics",
        "verdict": "fail",
        "auditor_id": "p8",
        "round": 1,
    })
    assert "Repair checkout flow" in body
    assert "stage=execute" in body
    assert "owner=p6" in body
    assert "priority=urgent" in body


async def test_audit_assignment_needed_cancelled_by_role_assigned(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Coach assigns the auditor before the timer expires, no
    Telegram message goes out."""
    await init_db()
    sent = _stub_send(monkeypatch)
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "60")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "60")

    await esc.start_escalation_watcher()
    try:
        await bus.publish({
            "type": "audit_assignment_needed",
            "task_id": "t-2026-05-03-cccccccc",
            "role": "auditor_syntax",
            "ts": "2026-05-03T14:00:00+00:00",
        })
        await _drain(0.05)
        assert esc.pending_count() == 1
        # Coach assigns the auditor — cancels the timer.
        await bus.publish({
            "type": "task_role_assigned",
            "task_id": "t-2026-05-03-cccccccc",
            "role": "auditor_syntax",
        })
        await _drain(0.1)
        assert esc.pending_count() == 0
        assert sent == []
    finally:
        await esc.stop_escalation_watcher()


async def test_self_review_warning_fires_message(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audit_self_review_warning is informational; it fires after the
    grace period because there's no natural resolution event."""
    await init_db()
    sent = _stub_send(monkeypatch)
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_SECONDS", "1")
    monkeypatch.setenv("HARNESS_TELEGRAM_ESCALATION_GRACE", "0")

    await esc.start_escalation_watcher()
    try:
        await bus.publish({
            "type": "audit_self_review_warning",
            "task_id": "t-2026-05-03-dddddddd",
            "kind": "semantics",
            "auditor_id": "p3",
            "executor_id": "p3",
            "ts": "2026-05-03T14:00:00+00:00",
        })
        await _drain(0.2)
        assert len(sent) == 1
        body = sent[0]
        assert "Self-review" in body
        assert "p3" in body
        assert "semantics" in body
    finally:
        await esc.stop_escalation_watcher()
