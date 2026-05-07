"""Soft-stall watchdog (Docs/kanban-specs-v2.md §10.7).

The §10.5 stall ladder operates on `tasks.last_stage_change_at` —
catches tasks where nothing has progressed for at least 30 min. The
§10.6 reconciliation sweep catches the "artifact on disk but kanban
didn't notice" pattern. Neither catches the *soft* stalls that show
up earlier and look fine to a SQL query:

  - Agent says "I've finished, here's the summary" but never calls
    the kanban completion tool.
  - Agent looped for ten messages without producing useful tool_use.
  - A turn errored mid-tool and the agent acknowledged but didn't
    retry, escalate, or message Coach.

Coach can't catch these without spending an Opus turn reading every
agent's timeline. The watchdog spends a bundled Haiku 4.5 call
instead. Fires from `idle_poller.sweep_once` after the reconciliation
pass; runs at the same 5-min cadence.

Three tiers:

  Tier 1 (free, SQL): filter the 11 agents to a handful of candidates
    matching a stall-shape signature. Most ticks return zero
    candidates and the watchdog short-circuits before any LLM call.
  Tier 2 (cheap Haiku): bundle each candidate's last N events + task
    state into ONE prompt; ask Haiku to classify per-candidate.
  Tier 3 (route): for non-`progressing` / non-`idle_ok` verdicts,
    emit `watchdog_finding` bus events. Coach's per-tick rollup
    reads the events and renders `## Soft stalls (watchdog-detected)`.

Cost discipline: turns ledger insert under `agent_id="watchdog"`,
`cost_basis="watchdog:tick"` so spend rolls into the team daily cap.
The cap itself is checked pre-fire as a fail-closed gate.

Failure isolation: tier 2 LLM error → log + drop. Tier 1 SQL error
→ log + skip. Per-candidate parse exceptions don't block siblings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from server.db import configured_conn
from server.events import bus

logger = logging.getLogger("harness.kanban_watchdog")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    )
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---------------------------------------------------------------- env

PLAYER_SLOTS: tuple[str, ...] = tuple(f"p{i}" for i in range(1, 11))


# Verdict enum. The Haiku prompt is told to use these exact strings;
# anything else falls back to `idle_ok` (drop) at parse time.
VERDICT_PROGRESSING = "progressing"
VERDICT_FINISHED_NOT_REPORTED = "finished_not_reported"
VERDICT_BLOCKED = "blocked"
VERDICT_ERRORING = "erroring"
VERDICT_LOOPING = "looping"
VERDICT_IDLE_OK = "idle_ok"

VALID_VERDICTS: frozenset[str] = frozenset({
    VERDICT_PROGRESSING,
    VERDICT_FINISHED_NOT_REPORTED,
    VERDICT_BLOCKED,
    VERDICT_ERRORING,
    VERDICT_LOOPING,
    VERDICT_IDLE_OK,
})

# Verdicts worth surfacing to Coach. The other two (progressing /
# idle_ok) are dropped before any event emission.
ACTIONABLE_VERDICTS: frozenset[str] = frozenset({
    VERDICT_FINISHED_NOT_REPORTED,
    VERDICT_BLOCKED,
    VERDICT_ERRORING,
    VERDICT_LOOPING,
})

# Verdicts that warrant an out-of-band Coach wake when the env flag
# is set. The default flag value is False — the rollup carries them
# on the next scheduled tick anyway.
HIGH_SEVERITY_VERDICTS: frozenset[str] = frozenset({
    VERDICT_ERRORING,
    VERDICT_BLOCKED,
})


def _flag_enabled() -> bool:
    raw = os.environ.get("HARNESS_WATCHDOG_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _no_tool_use_seconds() -> int:
    raw = os.environ.get("HARNESS_WATCHDOG_NO_TOOL_USE_SECONDS", "600").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 600


def _idle_with_task_seconds() -> int:
    raw = os.environ.get("HARNESS_WATCHDOG_IDLE_WITH_TASK_SECONDS", "600").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 600


def _recent_events_per_candidate() -> int:
    raw = os.environ.get("HARNESS_WATCHDOG_RECENT_EVENTS", "10").strip()
    try:
        return max(1, min(50, int(raw)))
    except ValueError:
        return 10


def _dedup_ttl_seconds() -> int:
    raw = os.environ.get("HARNESS_WATCHDOG_DEDUP_TTL_SECONDS", "3600").strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return 3600


def _max_candidates() -> int:
    raw = os.environ.get("HARNESS_WATCHDOG_MAX_CANDIDATES", "5").strip()
    try:
        return max(1, min(10, int(raw)))
    except ValueError:
        return 5


def _wake_coach_on_high() -> bool:
    raw = os.environ.get(
        "HARNESS_WATCHDOG_WAKE_COACH_ON_HIGH", "false"
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- state


# In-memory dedup: hash of (agent_id, verdict, last_event_ids) → last
# emit timestamp. Reset on process restart (mirrors §10.6 reconcile
# pattern). Module-level — single asyncio task drives the sweep, no
# lock needed.
_dedup_emitted: dict[str, float] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_mono() -> float:
    import time as _t
    return _t.monotonic()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- candidate types


@dataclass
class Candidate:
    """One agent flagged by tier 1 SQL as worth inspecting."""
    slot: str
    signal: str  # 'working_no_tool_use' | 'idle_with_task'
    status: str
    current_task_id: str | None
    last_event_ts: str | None
    last_tool_use_ts: str | None
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    task: dict[str, Any] | None = None


# ---------------------------------------------------------------- tier 1


async def _tier1_candidates() -> list[Candidate]:
    """SQL filter — return Players whose recent activity matches a
    stall-shape signature. Coach is excluded (Coach has its own
    recurrence machinery and IS the watchdog's target). Locked
    Players are skipped.
    """
    no_tool_thresh = _no_tool_use_seconds()
    idle_task_thresh = _idle_with_task_seconds()

    c = await configured_conn()
    try:
        cur = await c.execute(
            """
            SELECT a.id AS slot, a.status, a.current_task_id,
                   a.locked,
                   (SELECT MAX(ts) FROM events e
                     WHERE e.agent_id = a.id) AS last_event_ts,
                   (SELECT MAX(ts) FROM events e
                     WHERE e.agent_id = a.id AND e.type = 'tool_use'
                   ) AS last_tool_use_ts
              FROM agents a
             WHERE a.id LIKE 'p%'
            """
        )
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()

    now = datetime.now(timezone.utc)
    candidates: list[Candidate] = []
    for r in rows:
        if r.get("locked"):
            continue
        slot = r.get("slot") or ""
        if slot not in PLAYER_SLOTS:
            continue
        status = (r.get("status") or "").lower()
        last_tool_dt = _parse_iso(r.get("last_tool_use_ts"))
        last_event_dt = _parse_iso(r.get("last_event_ts"))

        # Signal A: working but no tool_use in window. A "working"
        # agent that hasn't called a tool recently is either looping
        # via plain text, stuck in a long thought, or hung — all
        # warrant a closer look. Crucially, the absence of `tool_use`
        # is the signal — recent `text` events do NOT reset the timer
        # (a 20-min monologue without a single tool call IS the
        # stall pattern we want to catch). AUDIT-2026-05-06 fix:
        # earlier code used `ref = last_tool_dt or last_event_dt`,
        # which let a chatty-but-tool-silent agent slip through.
        if status == "working":
            # Skip freshly-spawned agents (no events recorded yet).
            # `last_event_dt is None` means the agent hasn't even
            # emitted `agent_started` yet — too early.
            if last_event_dt is None:
                continue
            # Treat "no tool_use ever" as infinite age so the
            # threshold check fires. A working agent with events but
            # no tool calls IS suspicious.
            if last_tool_dt is None:
                tool_age = float("inf")
            else:
                tool_age = (now - last_tool_dt).total_seconds()
            if tool_age >= no_tool_thresh:
                candidates.append(Candidate(
                    slot=slot,
                    signal="working_no_tool_use",
                    status=status,
                    current_task_id=r.get("current_task_id"),
                    last_event_ts=r.get("last_event_ts"),
                    last_tool_use_ts=r.get("last_tool_use_ts"),
                ))
                continue

        # Signal B: idle but holds a task and no recent activity. The
        # canonical "agent declared done in chat but forgot to advance
        # the kanban" shape.
        if status == "idle" and r.get("current_task_id"):
            ref = last_event_dt
            if ref is None:
                continue
            if (now - ref).total_seconds() >= idle_task_thresh:
                candidates.append(Candidate(
                    slot=slot,
                    signal="idle_with_task",
                    status=status,
                    current_task_id=r.get("current_task_id"),
                    last_event_ts=r.get("last_event_ts"),
                    last_tool_use_ts=r.get("last_tool_use_ts"),
                ))

    # Defensive cap. Sort oldest-event-first so the most-stale slots
    # win the inspection slots when a flood is detected.
    if len(candidates) > _max_candidates():
        candidates.sort(key=lambda c: c.last_event_ts or "")
        candidates = candidates[:_max_candidates()]
    return candidates


async def _hydrate_candidate(cand: Candidate) -> None:
    """Fill `recent_events` and `task` for a candidate so tier 2 has
    enough context to classify. Read N most recent events for the
    slot + task row when applicable. Mutates the dataclass in place.
    """
    n = _recent_events_per_candidate()
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, ts, type, payload FROM events "
            "WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
            (cand.slot, n),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if cand.current_task_id:
            cur = await c.execute(
                "SELECT id, title, status, owner, last_stage_change_at "
                "FROM tasks WHERE id = ?",
                (cand.current_task_id,),
            )
            t = await cur.fetchone()
            if t:
                cand.task = dict(t)
    finally:
        await c.close()
    rows.reverse()  # chronological for prompt readability
    cand.recent_events = rows


# ---------------------------------------------------------------- tier 2 (Haiku)


_SYSTEM_PROMPT = """You are a stall-detection assistant for a multi-agent harness.
Each Player agent below was flagged by a SQL filter as possibly stuck.
Read its last few events + current task state and classify each one.

Verdicts (use exactly these strings):
- progressing — actually fine, ignore. SQL flagged a false positive.
- finished_not_reported — the agent declared completion in chat ("done", "wrote spec.md", "ready for review", "I've finished") but did NOT call the matching kanban completion tool (coord_write_task_spec / coord_commit_push / coord_complete_execution / coord_submit_audit_report / coord_mark_shipped) so the task is stuck.
- blocked — the agent reported it cannot proceed (ambiguous spec, missing info, dependency gap, audit feedback unclear) and is waiting for Coach to clarify or unblock.
- erroring — the agent encountered an error or exception in a recent tool_result and did NOT recover (no retry, no escalation, no message to Coach).
- looping — the agent has repeated similar actions without progress (re-reading the same files, restating what it will do, calling the same tool with the same args).
- idle_ok — the agent is genuinely between tasks and there is nothing to flag.

Return STRICT JSON of this shape (no prose, no fences):
{"verdicts":[{"agent_id":"p3","verdict":"finished_not_reported","reason":"Agent posted 'wrote spec.md' but never called coord_write_task_spec; task t-42 still in plan."}]}

Reasons should be one short sentence (under 200 chars), naming the
specific event or message that drove the verdict so Coach can act on
it. NEVER guess — if you can't tell, return idle_ok.
"""


def _compose_user_prompt(candidates: list[Candidate]) -> str:
    """Render the bundled per-candidate context block. Keep each
    candidate's section tight — Haiku only needs enough to decide,
    not the full event payloads."""
    parts: list[str] = []
    for c in candidates:
        head = f"## agent {c.slot} (signal: {c.signal}, status: {c.status})"
        parts.append(head)
        if c.task:
            parts.append(
                f"current task: {c.task.get('id')} \"{(c.task.get('title') or '')[:80]}\" "
                f"stage={c.task.get('status')} "
                f"last_stage_change={c.task.get('last_stage_change_at')}"
            )
        else:
            parts.append("current task: (none)")
        parts.append("")
        parts.append("recent events (oldest first):")
        for ev in c.recent_events:
            ts = (ev.get("ts") or "")[:19]
            etype = ev.get("type") or "?"
            payload_str = ev.get("payload") or "{}"
            try:
                payload = json.loads(payload_str)
            except Exception:
                payload = {}
            summary = _summarize_event_payload(etype, payload)
            parts.append(f"  - [{ts}] {etype} {summary}")
        parts.append("")
    return "\n".join(parts)


def _summarize_event_payload(etype: str, payload: dict[str, Any]) -> str:
    """One-line summary of an event's payload, tuned for the LLM
    prompt. Keep small — Haiku reads dozens of these per call.
    Truncate freeform text strings hard."""
    def _short(s: Any, n: int = 200) -> str:
        if not isinstance(s, str):
            s = str(s)
        s = s.replace("\n", " ").strip()
        if len(s) > n:
            return s[:n] + "…"
        return s

    if etype == "text":
        return _short(payload.get("text") or "")
    if etype == "tool_use":
        name = payload.get("name") or "?"
        inp = payload.get("input") or {}
        return f"{name}({_short(json.dumps(inp), 160)})"
    if etype == "tool_result":
        is_err = payload.get("is_error") and " ERR" or ""
        body = payload.get("content") or payload.get("output") or ""
        return f"{is_err}{_short(body)}"
    if etype == "message_sent":
        to = payload.get("to") or "?"
        body = payload.get("body") or ""
        return f"to={to} {_short(body, 160)}"
    if etype == "agent_started":
        reason = payload.get("reason") or ""
        return _short(reason, 100)
    if etype == "agent_stopped":
        sr = payload.get("stop_reason") or ""
        return f"stop_reason={sr}"
    if etype == "result":
        return _short(payload.get("result") or "", 160)
    if etype == "error":
        return f"ERR {_short(payload.get('reason') or payload.get('error') or '', 200)}"
    # Fallback — show subject / title-ish keys we know about.
    for k in ("subject", "title", "task_id", "stage", "kind", "verdict"):
        if k in payload:
            return f"{k}={_short(payload[k], 80)}"
    return ""


@dataclass
class WatchdogLLMResult:
    text: str
    is_error: bool
    cost_usd: float | None
    duration_ms: int | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


async def _call_haiku(
    system: str,
    user: str,
    *,
    candidates_count: int,
    project_id: str | None = None,
) -> WatchdogLLMResult:
    """Run one Haiku one-shot call. Mirrors compass.llm.call's shape
    but writes the turns ledger row under `agent_id="watchdog"` so
    cost attribution is unambiguous in the EnvPane meter.

    `project_id` is the active project pinned at sweep start; it's
    threaded into the `watchdog_llm_call` bus event so dashboard
    counters partition correctly across project switches. None falls
    back to `bus.publish`'s auto-stamp.

    Lazy-imports `claude_agent_sdk` so a hermetic test environment
    without the SDK can still load this module (tests stub
    `_call_haiku` directly).
    """
    from claude_agent_sdk import (  # noqa: PLC0415
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
    from server.models_catalog import resolve_model_alias  # noqa: PLC0415

    model = resolve_model_alias("latest_haiku")
    started = _now_iso()
    started_mono_ns_ = _monotonic_ns()

    options_kwargs: dict[str, Any] = dict(
        system_prompt=system,
        max_turns=1,
        mcp_servers={},
        allowed_tools=[],
        model=model,
    )
    try:
        from server.agent_env import build_agent_env_overrides  # noqa: PLC0415
        options_kwargs["env"] = build_agent_env_overrides()
    except Exception:
        pass
    options = ClaudeAgentOptions(**options_kwargs)

    text_parts: list[str] = []
    cost_usd: float | None = None
    duration_ms: int | None = None
    is_error = False
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_creation = 0
    saw_result = False
    session_id: str | None = None
    stop_reason: str | None = None

    async def _stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "user", "message": {"role": "user", "content": user}}

    try:
        async for msg in query(prompt=_stream(), options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text or "")
            elif isinstance(msg, ResultMessage):
                saw_result = True
                cost_usd = getattr(msg, "total_cost_usd", None)
                duration_ms = getattr(msg, "duration_ms", None)
                session_id = getattr(msg, "session_id", None)
                stop_reason = getattr(msg, "stop_reason", None)
                is_error = bool(getattr(msg, "is_error", False))
                u = getattr(msg, "usage", None)
                if u is not None:
                    def _u(name: str) -> int:
                        v = u.get(name) if isinstance(u, dict) else getattr(u, name, None)
                        try:
                            return int(v) if v is not None else 0
                        except (TypeError, ValueError):
                            return 0
                    input_tokens = _u("input_tokens")
                    output_tokens = _u("output_tokens")
                    cache_read = _u("cache_read_input_tokens")
                    cache_creation = _u("cache_creation_input_tokens")
    except Exception as e:
        if not saw_result:
            logger.exception("watchdog: Haiku call failed before ResultMessage")
            return WatchdogLLMResult(
                text="", is_error=True, cost_usd=None,
                duration_ms=None, input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_creation_tokens=0,
            )
        # Post-result teardown noise — same suppression rule as
        # agents.py and compass.llm.
        logger.warning(
            "watchdog: ignoring post-result %s: %s",
            type(e).__name__, str(e)[:200],
        )

    text = "".join(text_parts).strip()

    # Turns ledger insert. Lazy import to dodge any potential
    # circular import on cold load.
    try:
        from server.agents import _insert_turn_row  # noqa: PLC0415
        await _insert_turn_row(
            agent_id="watchdog",
            started_at=started,
            ended_at=_now_iso(),
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            session_id=session_id,
            num_turns=1,
            stop_reason=stop_reason,
            is_error=is_error,
            model=model,
            plan_mode=False,
            effort=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            runtime="claude",
            cost_basis="watchdog:tick",
        )
    except Exception:
        logger.exception("watchdog: turn ledger insert failed (continuing)")

    # Live UI counter event — mirrors compass_llm_call.
    try:
        ev: dict[str, Any] = {
            "ts": _now_iso(),
            "agent_id": "watchdog",
            "type": "watchdog_llm_call",
            "model": model,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "candidates": candidates_count,
            "is_error": is_error,
        }
        if project_id:
            ev["project_id"] = project_id
        await bus.publish(ev)
    except Exception:
        pass

    elapsed = duration_ms
    if elapsed is None:
        elapsed = int((_monotonic_ns() - started_mono_ns_) / 1_000_000)

    return WatchdogLLMResult(
        text=text,
        is_error=is_error,
        cost_usd=cost_usd,
        duration_ms=elapsed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )


def _monotonic_ns() -> int:
    import time as _t
    return _t.monotonic_ns()


# ---------------------------------------------------------------- parsing


def _parse_verdicts_response(
    text: str, candidates: list[Candidate],
) -> list[dict[str, Any]]:
    """Parse Haiku's JSON output into a per-candidate verdict list.

    Recovery rules:
      - Strip code fences.
      - Tolerate brace-balanced extraction when the model wraps the
        JSON in commentary.
      - Unknown verdict strings → coerce to `idle_ok` (drop downstream).
      - Missing candidates in the response → default to `idle_ok`.
      - Reason truncated at 300 chars (UI safety).

    Returns a list of {agent_id, verdict, reason, candidate} entries
    in candidate-input order. Always covers every candidate.
    """
    raw_obj = _parse_json_safe(text)
    by_slot: dict[str, dict[str, Any]] = {}
    if isinstance(raw_obj, dict):
        verdicts = raw_obj.get("verdicts")
        if isinstance(verdicts, list):
            for v in verdicts:
                if not isinstance(v, dict):
                    continue
                slot = (v.get("agent_id") or "").strip()
                if not slot:
                    continue
                verdict = (v.get("verdict") or "").strip().lower()
                if verdict not in VALID_VERDICTS:
                    verdict = VERDICT_IDLE_OK
                reason = (v.get("reason") or "").strip()[:300]
                by_slot[slot] = {"verdict": verdict, "reason": reason}

    out: list[dict[str, Any]] = []
    for c in candidates:
        v = by_slot.get(c.slot, {"verdict": VERDICT_IDLE_OK, "reason": ""})
        out.append({
            "agent_id": c.slot,
            "verdict": v["verdict"],
            "reason": v["reason"],
            "candidate": c,
        })
    return out


def _parse_json_safe(text: str) -> Any:
    """Mirror of compass.llm.parse_json_safe — kept local so we don't
    drag the compass module in. Same three-step strategy: raw load,
    code-fence strip, brace-balance extract."""
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Code-fence strip.
    import re as _re
    m = _re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, _re.MULTILINE)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    # Brace-balance.
    sliced = _extract_balanced(text)
    if sliced is not None:
        try:
            return json.loads(sliced)
        except json.JSONDecodeError:
            return None
    return None


def _extract_balanced(text: str) -> str | None:
    open_chars = {"{": "}", "[": "]"}
    start = -1
    open_ch = ""
    for i, ch in enumerate(text):
        if ch in open_chars:
            start = i
            open_ch = ch
            break
    if start < 0:
        return None
    close_ch = open_chars[open_ch]
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# ---------------------------------------------------------------- tier 3 (route)


def _dedup_key(verdict_row: dict[str, Any]) -> str:
    """Hash over (agent_id, verdict, last 10 event ids). The same
    observation can't fire twice within the TTL window. Verdict is
    part of the key so a flip from `looping` to `blocked` IS treated
    as a new finding (the situation actually changed)."""
    cand: Candidate = verdict_row["candidate"]
    ids = [str(ev.get("id") or "") for ev in cand.recent_events[-10:]]
    payload = "|".join([
        cand.slot,
        verdict_row["verdict"],
        ",".join(ids),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _dedup_should_emit(key: str) -> bool:
    """True iff this finding hash hasn't been emitted within TTL.
    Stamps the timestamp on success."""
    ttl = _dedup_ttl_seconds()
    now = _now_mono()
    last = _dedup_emitted.get(key)
    if last is not None and (now - last) < ttl:
        return False
    _dedup_emitted[key] = now
    return True


async def _emit_findings(
    verdicts: list[dict[str, Any]], *, project_id: str | None = None,
) -> int:
    """For each actionable verdict, dedup + emit `watchdog_finding`.
    Returns the number of fresh findings emitted this sweep.

    `project_id` is captured at sweep start and stamped explicitly on
    each emitted event so a project switch mid-sweep doesn't land
    findings on the wrong project's Coach rollup. `bus.publish`
    auto-stamps from `resolve_active_project()` only when the caller
    didn't set the field, so providing it here pins the value. None
    falls back to auto-stamp (no behavior change for legacy callers).
    """
    emitted = 0
    wake_high = _wake_coach_on_high()
    for v in verdicts:
        verdict = v["verdict"]
        if verdict not in ACTIONABLE_VERDICTS:
            continue
        cand: Candidate = v["candidate"]
        key = _dedup_key(v)
        if not _dedup_should_emit(key):
            continue

        ev_payload: dict[str, Any] = {
            "ts": _now_iso(),
            "agent_id": "system",
            "type": "watchdog_finding",
            "subject_agent": cand.slot,
            "verdict": verdict,
            "reason": v["reason"],
            "task_id": cand.current_task_id,
            "signal": cand.signal,
            "hash": key,
            "to": "coach",
        }
        if project_id:
            ev_payload["project_id"] = project_id
        try:
            await bus.publish(ev_payload)
        except Exception:
            logger.exception(
                "watchdog: emit watchdog_finding failed (slot=%s)", cand.slot
            )
            continue
        emitted += 1

        if verdict == VERDICT_ERRORING:
            try:
                attn: dict[str, Any] = {
                    "ts": _now_iso(),
                    "agent_id": "system",
                    "type": "human_attention",
                    "subject": (
                        f"Watchdog: {cand.slot} appears to be erroring "
                        f"without recovery"
                    ),
                    "body": v["reason"] or "(no reason captured)",
                    "urgency": "medium",
                    "to": "human",
                }
                if project_id:
                    attn["project_id"] = project_id
                await bus.publish(attn)
            except Exception:
                pass

        if wake_high and verdict in HIGH_SEVERITY_VERDICTS:
            try:
                from server.agents import maybe_wake_agent  # noqa: PLC0415
                body = (
                    f"Watchdog flagged {cand.slot} as {verdict}: "
                    f"{v['reason'] or '(no detail)'}. Decide whether to "
                    f"clarify (coord_send_message), advance the task on "
                    f"their behalf (coord_advance_task_stage / "
                    f"coord_write_task_spec / coord_submit_audit_report "
                    f"with on_behalf_of=...), reassign "
                    f"(coord_assign_*), or escalate "
                    f"(coord_request_human)."
                )
                await maybe_wake_agent(
                    "coach", body,
                    bypass_debounce=False,
                    wake_source="watchdog_high",
                )
            except Exception:
                logger.exception(
                    "watchdog: maybe_wake_agent('coach') failed"
                )
    return emitted


# ---------------------------------------------------------------- public api


# Indirection so tests can monkeypatch the LLM call cheaply.
async def _call_llm(
    system: str, user: str, *, candidates_count: int,
    project_id: str | None = None,
) -> WatchdogLLMResult:
    return await _call_haiku(
        system, user,
        candidates_count=candidates_count,
        project_id=project_id,
    )


async def _within_cost_cap() -> bool:
    """Fail-closed cost-cap probe — mirrors compass.audit_watcher's
    pattern. Pessimistic: if reading spend fails, skip the sweep."""
    try:
        from server.agents import (  # noqa: PLC0415
            TEAM_DAILY_CAP_USD, _today_spend,
        )
    except Exception:
        return True
    if TEAM_DAILY_CAP_USD <= 0:
        return True
    try:
        spent = await _today_spend()
    except Exception:
        logger.exception("watchdog: spend lookup failed; skipping sweep")
        return False
    return spent < TEAM_DAILY_CAP_USD


async def sweep_once() -> int:
    """Run one full watchdog cycle. Returns the number of fresh
    findings emitted (deduped). Exposed for tests so they can drive
    the loop deterministically without waiting on the idle poller.

    AUDIT-2026-05-06: capture `project_id` at sweep start and pass it
    through to `_emit_findings` so a mid-sweep project switch can't
    land findings on the wrong project's Coach rollup. The LLM call
    event also carries this pinned id.
    """
    if not _flag_enabled():
        return 0

    # Pin project_id at sweep start. Any switch during the sweep
    # leaves us emitting against the project the candidates actually
    # belong to.
    pinned_project: str | None = None
    try:
        from server.db import resolve_active_project  # noqa: PLC0415
        pinned_project = await resolve_active_project()
    except Exception:
        pinned_project = None

    candidates: list[Candidate] = []
    try:
        candidates = await _tier1_candidates()
    except Exception:
        logger.exception("watchdog: tier 1 SQL failed")
        return 0
    if not candidates:
        return 0

    if not await _within_cost_cap():
        return 0

    # Hydrate each candidate with recent events + task. Per-candidate
    # exception isolation so one bad slot doesn't blank the rest.
    hydrated: list[Candidate] = []
    for c in candidates:
        try:
            await _hydrate_candidate(c)
            hydrated.append(c)
        except Exception:
            logger.exception(
                "watchdog: hydrate failed (slot=%s)", c.slot
            )
    if not hydrated:
        return 0

    user_prompt = _compose_user_prompt(hydrated)

    try:
        result = await _call_llm(
            _SYSTEM_PROMPT, user_prompt,
            candidates_count=len(hydrated),
            project_id=pinned_project,
        )
    except Exception:
        logger.exception("watchdog: LLM call crashed")
        return 0
    if result.is_error or not result.text:
        logger.info(
            "watchdog: LLM returned no usable text (is_error=%s, len=%d)",
            result.is_error, len(result.text),
        )
        return 0

    verdicts = _parse_verdicts_response(result.text, hydrated)
    return await _emit_findings(verdicts, project_id=pinned_project)


__all__ = [
    "sweep_once",
    "Candidate",
    "PLAYER_SLOTS",
    "VALID_VERDICTS",
    "ACTIONABLE_VERDICTS",
    "HIGH_SEVERITY_VERDICTS",
    "VERDICT_PROGRESSING",
    "VERDICT_FINISHED_NOT_REPORTED",
    "VERDICT_BLOCKED",
    "VERDICT_ERRORING",
    "VERDICT_LOOPING",
    "VERDICT_IDLE_OK",
]
