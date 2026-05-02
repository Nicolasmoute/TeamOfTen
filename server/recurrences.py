"""Coach recurrence scheduler.

Implements the unified recurrence runtime described in
`Docs/recurrence-specs.md` §11. Three flavors share one row format and
one scheduler loop:

  * **tick**   — singleton per project, harness-composed prompt via
                 :func:`compose_tick_prompt` (recurrence-specs.md §4).
  * **repeat** — many per project, fixed-minute cadence + caller prompt.
  * **cron**   — many per project, DSL string + timezone + caller prompt.

Phase 1 surface (this file): table + scheduler only. Slash commands and
HTTP APIs that *create* recurrence rows are out of scope; the migration
in :func:`server.db._seed_recurrence_from_env` is the only writer in
phase 1.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from server.db import configured_conn, resolve_active_project
from server.events import bus

logger = logging.getLogger("harness.recurrences")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s | %(message)s"
    ))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


SCHEDULER_TICK_SECONDS = max(
    1, int(os.environ.get("HARNESS_RECURRENCE_TICK_SECONDS", "30"))
)

# Soft cap on rows-per-project (`recurrence-specs.md` §15.8). Beyond
# this, POST returns 409 — prevents accidental fork-bombs.
MAX_RECURRENCES_PER_PROJECT = max(
    1, int(os.environ.get("HARNESS_MAX_RECURRENCES_PER_PROJECT", "50"))
)


# --- Cron DSL ---------------------------------------------------------

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_INDEX = {d: i for i, d in enumerate(_DAYS)}
# Spec §5.1: TIME := HH:MM (24h). Strict — single-digit hour like
# `9:00` is rejected so the surface stays consistent with the
# documented grammar.
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class CronParseError(ValueError):
    """Raised when a cron DSL string cannot be parsed."""


def _parse_time(token: str) -> time:
    m = _TIME_RE.match(token)
    if not m:
        raise CronParseError(f"invalid time: {token!r}")
    return time(int(m.group(1)), int(m.group(2)))


def _parse_day_list(token: str, *, allow_single: bool = True) -> list[int]:
    """Parse a comma-separated day list. Spec §5.1: bare-list shape
    is `DAY ("," DAY)+` — that is, **two or more** days. Single days
    must be expressed as `weekly DAY TIME`. ``allow_single=False``
    enforces the spec for the bare DAY_LIST entry point."""
    parts = [p.strip() for p in token.split(",")]
    out: list[int] = []
    for p in parts:
        if p not in _DAY_INDEX:
            raise CronParseError(f"invalid day: {p!r}")
        if _DAY_INDEX[p] not in out:
            out.append(_DAY_INDEX[p])
    if not out:
        raise CronParseError("empty day list")
    if not allow_single and len(out) < 2:
        raise CronParseError(
            f"single-day shorthand: use 'weekly {parts[0]} HH:MM' instead"
        )
    return out


def parse_cron(dsl: str) -> dict[str, Any]:
    """Parse a friendly cron DSL string per `recurrence-specs.md` §5.1.

    Returns a dict with one of the following shapes:

      ``{"type": "once", "date": <date>, "time": <time>}``
      ``{"type": "daily", "time": <time>}``
      ``{"type": "weekdays", "time": <time>}``
      ``{"type": "weekends", "time": <time>}``
      ``{"type": "weekly", "days": [0..6], "time": <time>}``
      ``{"type": "monthly", "day": 1..31, "time": <time>}``

    The "weekly DAY TIME" form collapses into ``{"type": "weekly",
    "days": [...]}`` so it shares ``compute_next_fire_at``'s day-list
    branch with bare ``DAY_LIST TIME``.
    """
    s = dsl.strip()
    if not s:
        raise CronParseError("empty schedule")
    parts = s.split()

    if len(parts) == 2:
        first, t_token = parts
        m = _DATE_RE.match(first)
        if m:
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError as exc:
                raise CronParseError(f"invalid date: {first!r}") from exc
            return {"type": "once", "date": d, "time": _parse_time(t_token)}
        if first == "daily":
            return {"type": "daily", "time": _parse_time(t_token)}
        if first == "weekdays":
            return {"type": "weekdays", "time": _parse_time(t_token)}
        if first == "weekends":
            return {"type": "weekends", "time": _parse_time(t_token)}
        # bare DAY_LIST TIME (e.g. "mon,thu 14:00")
        return {
            "type": "weekly",
            "days": _parse_day_list(first, allow_single=False),
            "time": _parse_time(t_token),
        }

    if len(parts) == 3 and parts[0] == "weekly":
        return {
            "type": "weekly",
            "days": _parse_day_list(parts[1]),
            "time": _parse_time(parts[2]),
        }

    if len(parts) == 3 and parts[0] == "monthly":
        try:
            dom = int(parts[1])
        except ValueError as exc:
            raise CronParseError(f"invalid day-of-month: {parts[1]!r}") from exc
        if not 1 <= dom <= 31:
            raise CronParseError(f"day-of-month out of range: {dom}")
        return {
            "type": "monthly",
            "day": dom,
            "time": _parse_time(parts[2]),
        }

    raise CronParseError(f"unrecognized schedule: {dsl!r}")


def is_one_shot(parsed: dict[str, Any]) -> bool:
    return parsed.get("type") == "once"


def _next_for_days(
    after_local: datetime, days: list[int], t: time
) -> datetime:
    """Earliest local datetime strictly after `after_local` whose
    weekday is in `days` and whose time is `t`."""
    candidate = after_local.replace(
        hour=t.hour, minute=t.minute, second=0, microsecond=0
    )
    if candidate <= after_local:
        candidate = candidate + timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in days:
            return candidate
        candidate = candidate + timedelta(days=1)
    # Should be unreachable — at most 7 hops to find a matching weekday.
    raise RuntimeError("no matching day within a week")


def _next_for_dom(
    after_local: datetime, dom: int, t: time
) -> datetime:
    """Earliest local datetime strictly after `after_local` whose
    day-of-month is `dom` and whose time is `t`. Months that don't have
    that day (e.g. Feb 31) are skipped."""
    cur = after_local
    for _ in range(48):
        try:
            candidate = cur.replace(
                day=dom, hour=t.hour, minute=t.minute,
                second=0, microsecond=0,
            )
            if candidate > after_local:
                return candidate
        except ValueError:
            pass
        # Step into next month (set day=1 first to dodge end-of-month
        # arithmetic).
        first_of_next = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        cur = first_of_next
    raise RuntimeError("no matching day-of-month within 4 years")


def compute_next_fire_at(
    parsed: dict[str, Any],
    tz: str,
    after_utc: datetime,
) -> datetime | None:
    """Return the next UTC datetime at which a cron schedule should
    fire after ``after_utc``. Returns ``None`` for one-shot schedules
    whose date+time is at/before ``after_utc`` (caller disables the row).
    """
    # "UTC" is the lingua franca and lookups for it failing on a host
    # without the tzdata package shouldn't break us.
    if tz == "UTC":
        zone: Any = timezone.utc
    else:
        try:
            zone = ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            raise CronParseError(f"unknown timezone: {tz!r}") from exc
    after_local = after_utc.astimezone(zone)
    kind = parsed["type"]

    if kind == "once":
        d: date = parsed["date"]
        t: time = parsed["time"]
        local = datetime.combine(d, t, tzinfo=zone)
        if local <= after_local:
            return None
        return local.astimezone(timezone.utc)

    if kind == "daily":
        t = parsed["time"]
        candidate = after_local.replace(
            hour=t.hour, minute=t.minute, second=0, microsecond=0
        )
        if candidate <= after_local:
            candidate = candidate + timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    if kind == "weekdays":
        t = parsed["time"]
        local = _next_for_days(after_local, [0, 1, 2, 3, 4], t)
        return local.astimezone(timezone.utc)

    if kind == "weekends":
        t = parsed["time"]
        local = _next_for_days(after_local, [5, 6], t)
        return local.astimezone(timezone.utc)

    if kind == "weekly":
        local = _next_for_days(after_local, parsed["days"], parsed["time"])
        return local.astimezone(timezone.utc)

    if kind == "monthly":
        local = _next_for_dom(after_local, parsed["day"], parsed["time"])
        return local.astimezone(timezone.utc)

    raise CronParseError(f"unhandled schedule type: {kind!r}")


# --- Scheduler --------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # SQLite strftime emits "YYYY-MM-DDTHH:MM:SS.fffZ"; fromisoformat
        # in 3.11+ accepts the Z suffix.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def _compute_next_for_row(
    row: dict[str, Any], after_utc: datetime
) -> datetime | None:
    """Resolve the next-fire UTC for any row kind. Returns None to
    signal the row must be disabled (one-shot complete or unparsable)."""
    kind = row["kind"]
    cadence = row["cadence"]
    if kind in ("tick", "repeat"):
        try:
            minutes = max(1, int(cadence))
        except (TypeError, ValueError):
            return None
        return after_utc + timedelta(minutes=minutes)
    if kind == "cron":
        tz = row.get("tz") or "UTC"
        try:
            parsed = parse_cron(cadence)
        except CronParseError:
            logger.exception(
                "recurrence: failed to parse cron row id=%s cadence=%r",
                row.get("id"), cadence,
            )
            return None
        return compute_next_fire_at(parsed, tz, after_utc)
    return None


async def _emit(event: dict[str, Any]) -> None:
    event.setdefault("ts", _now_utc().isoformat())
    event.setdefault("agent_id", "coach")
    await bus.publish(event)


# Spec §4 priority orientation. Short by design — the system prompt
# already contains project objectives + open coach todos, so the user
# prompt just tells Coach the priority order. Step (3) is intentionally
# directive: when inbox/todos are empty BUT objectives exist, Coach
# must still pick a concrete action grounded in the objectives — the
# whole point of a recurring tick is forward motion. The end-quietly
# branch is gated on objectives being absent or empty (nothing to
# anchor invented work to), not on inbox/todos being empty.
TICK_BASE_PROMPT = (
    "Routine tick. Work the project — do something useful every "
    "fire.\n"
    "\n"
    "Priority order:\n"
    "\n"
    "(1) Inbox first — call coord_read_inbox. Respond to anything "
    "pending from the human or your teammates.\n"
    "(2) Open coach-todos — if inbox is clear, pick the todo most "
    "aligned with current priorities and act on it (assign to a "
    "Player, break it into smaller steps, or do the work yourself).\n"
    "(3) Drive the objectives — if inbox AND todos are both empty, "
    "you must still pick one concrete action that materially "
    "advances a project objective. Examples: assign a Player to "
    "scout an open question, send a status update or coordination "
    "message, capture a new coach-todo for the operator to refine, "
    "audit recent Player work for blockers, propose a useful next "
    "step and execute it. Don't end the turn idle when objectives "
    "exist — invent forward motion grounded in them.\n"
    "\n"
    "Only end the turn without acting when project objectives are "
    "absent or empty (no \"## Project objectives\" section in your "
    "system prompt). In that case, end quietly — there's nothing "
    "to anchor invented work to."
)


async def _coach_recently_asked_objectives(
    project_id: str,
) -> bool:
    """True if Coach has emitted any text mentioning 'project-
    objectives.md' or 'objectives' in a sent message within the last
    24h for this project. Used by ``compose_tick_prompt`` to suppress
    re-elicitation per spec §15.5 ("subsequent ticks → end quietly").

    This replaces the older team_config-flag mechanism, which marked
    the elicitation as 'asked' even when the inbox / todos were busy
    and Coach never actually saw the hint as actionable.
    """
    c = await configured_conn()
    try:
        cur = await c.execute(
            """
            SELECT 1 FROM events
            WHERE project_id = ?
              AND agent_id = 'coach'
              AND type = 'message_sent'
              AND id IN (
                SELECT id FROM events
                WHERE project_id = ?
                  AND agent_id = 'coach'
                  AND type = 'message_sent'
                ORDER BY id DESC
                LIMIT 50
              )
              AND (
                payload LIKE '%project-objectives.md%'
                OR payload LIKE '%define%objectives%'
                OR payload LIKE '%trying to accomplish%'
              )
            LIMIT 1
            """,
            (project_id, project_id),
        )
        return await cur.fetchone() is not None
    except Exception:
        # DB hiccup → fail open (include the hint); Coach can
        # self-regulate by reading inbox.
        return False
    finally:
        await c.close()


async def compose_tick_prompt(project_id: str) -> str:
    """Per-tick user prompt for Coach (`recurrence-specs.md` §4).

    The base orientation is constant. When ``project-objectives.md``
    is missing or empty AND Coach hasn't already asked recently
    (last 50 outgoing messages, scanned for objectives-related
    wording), append the elicitation hint per spec §15.5. Once Coach
    sends an objectives-asking message, the events log is the source
    of truth — no team_config flag needed.
    """
    from server.coach_objectives import (
        OBJECTIVES_ELICITATION_PROMPT, has_objectives,
    )
    prompt = TICK_BASE_PROMPT
    if has_objectives(project_id):
        return prompt
    if await _coach_recently_asked_objectives(project_id):
        return prompt
    prompt += (
        "\n\nNote: project-objectives.md is missing or empty. If "
        "your inbox is empty AND there are no open coach-todos, "
        f"ask the operator: \"{OBJECTIVES_ELICITATION_PROMPT}\" "
        "Once they reply, save the answer via the Write tool to "
        f"/data/projects/{project_id}/project-objectives.md."
    )
    return prompt


async def _fire_row(row: dict[str, Any]) -> None:
    """Spawn the Coach turn for a due row. Caller has already verified
    Coach is idle."""
    # Imported lazily to dodge an import cycle during module load:
    # server.agents imports from server.events, and server.events is
    # imported above.
    from server.agents import run_agent

    kind = row["kind"]
    if kind == "tick":
        # Phase 5 replaces the static COACH_TICK_PROMPT with a per-fire
        # composer (recurrence-specs.md §4). The composer is short by
        # design — Coach's system prompt already has the inbox-able
        # state via injected objectives + open todos, so this user
        # prompt only orients them to the priority order.
        prompt = await compose_tick_prompt(row["project_id"])
    else:
        prompt = (row.get("prompt") or "").strip()
        if not prompt:
            logger.warning(
                "recurrence: row id=%s kind=%s has empty prompt; skipping",
                row.get("id"), kind,
            )
            return
    await _emit({
        "type": "recurrence_fired",
        "id": row["id"],
        "kind": kind,
        "prompt_excerpt": prompt[:80],
        "project_id": row["project_id"],
    })
    await run_agent("coach", prompt)


async def _skip_row(
    db: Any, row: dict[str, Any], reason: str, now: datetime,
) -> None:
    """Persist the deferred fire and emit ``recurrence_skipped``.

    Advances ``next_fire_at`` past now so the row doesn't immediately
    re-fire on the next 30s scheduler pass. One-shot crons whose schedule
    is now in the past are disabled instead.
    """
    rid = row["id"]
    kind = row["kind"]
    project_id = row["project_id"]
    next_fire = await _compute_next_for_row(row, now)

    one_shot_terminal = next_fire is None and kind == "cron"
    if one_shot_terminal:
        # Past one-shot: persist the disable (no future fire) but
        # still emit the skip first per spec §11 — the operator
        # should see WHY the row was skipped (coach_busy /
        # cost_capped) before it disappears.
        await db.execute(
            "UPDATE coach_recurrence SET enabled = 0, "
            "next_fire_at = NULL WHERE id = ?",
            (rid,),
        )
    else:
        await db.execute(
            "UPDATE coach_recurrence SET next_fire_at = ? WHERE id = ?",
            (_format_iso(next_fire) if next_fire else None, rid),
        )
    await db.commit()

    await _emit({
        "type": "recurrence_skipped",
        "id": rid,
        "kind": kind,
        "reason": reason,
        "project_id": project_id,
    })
    if one_shot_terminal:
        await _emit({
            "type": "recurrence_disabled",
            "id": rid,
            "kind": kind,
            "reason": "one_shot_complete",
            "project_id": project_id,
        })


async def _handle_due_row(
    db: Any, row: dict[str, Any], coach_busy: bool, now: datetime,
) -> bool:
    """Process one due row. Updates next_fire_at / last_fired_at and
    emits the appropriate events. Returns True if Coach was actually
    spawned (so the pass can force-skip subsequent rows per spec
    §15.1). Returns False on skip / disable paths."""
    # Imported lazily to dodge an import cycle during module load.
    from server.agents import _check_cost_caps

    rid = row["id"]
    kind = row["kind"]
    project_id = row["project_id"]

    if coach_busy:
        await _skip_row(db, row, "coach_busy", now)
        return False

    # §15.9: a recurrence fire is subject to the same per-agent and
    # team-daily cost caps as any other Coach turn. Skip with
    # reason=cost_capped instead of letting run_agent emit the noisier
    # cost_capped event so the recurrence pane sees a single skip.
    allowed, _reason = await _check_cost_caps("coach")
    if not allowed:
        await _skip_row(db, row, "cost_capped", now)
        return False

    # Coach is free + under cap — fire the row. last_fired_at stamps
    # before the spawn so a long-running turn doesn't push next_fire_at
    # into the turn end (a 30-min cron should still fire every 30 min
    # even if the turn occasionally takes a few minutes).
    fired_at = now
    next_fire = await _compute_next_for_row(row, fired_at)

    one_shot_done = (
        kind == "cron"
        and is_one_shot(parse_cron(row["cadence"]))
    )
    if one_shot_done:
        await db.execute(
            "UPDATE coach_recurrence SET enabled = 0, "
            "last_fired_at = ?, next_fire_at = NULL WHERE id = ?",
            (_format_iso(fired_at), rid),
        )
    else:
        await db.execute(
            "UPDATE coach_recurrence SET last_fired_at = ?, "
            "next_fire_at = ? WHERE id = ?",
            (
                _format_iso(fired_at),
                _format_iso(next_fire) if next_fire else None,
                rid,
            ),
        )
    await db.commit()

    await _fire_row(row)

    if one_shot_done:
        await _emit({
            "type": "recurrence_disabled",
            "id": rid,
            "kind": kind,
            "reason": "one_shot_complete",
            "project_id": project_id,
        })
    return True


async def _scheduler_iteration() -> None:
    """One pass of the scheduler. Reads due rows for the active
    project; fires them sequentially; persists new next_fire_at."""
    # Imported lazily — see _fire_row.
    from server.agents import _coach_is_working, is_paused

    if is_paused():
        return

    project_id = await resolve_active_project()
    now = _now_utc()
    now_iso = _format_iso(now)

    db = await configured_conn()
    try:
        cur = await db.execute(
            "SELECT id, project_id, kind, cadence, tz, prompt, enabled, "
            "next_fire_at, last_fired_at FROM coach_recurrence "
            "WHERE project_id = ? AND enabled = 1 "
            "AND (next_fire_at IS NULL OR next_fire_at <= ?) "
            "ORDER BY id",
            (project_id, now_iso),
        )
        rows = [dict(r) for r in await cur.fetchall()]

        if not rows:
            return

        # Spec §15.1: "Multiple due rows in one scheduler tick: fire
        # them sequentially; after the first fires, the rest see
        # 'coach_busy' and skip." Because we await the full Coach turn
        # inside _fire_row, ``_coach_is_working`` is False again once
        # the await returns — so a second await would happily fire
        # another row. Track a local flag and force-skip any further
        # rows in this pass.
        fired_in_pass = False
        for row in rows:
            busy = fired_in_pass or await _coach_is_working()
            did_fire = await _handle_due_row(db, row, busy, _now_utc())
            if did_fire:
                fired_in_pass = True
    finally:
        await db.close()


# --- CRUD helpers -----------------------------------------------------


def _row_to_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


async def list_recurrences(project_id: str) -> list[dict[str, Any]]:
    """Return all recurrence rows for a project, ordered by kind then id.

    Disabled rows are included so the UI can show the user "you turned
    this off" instead of silently dropping it.
    """
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, project_id, kind, cadence, tz, prompt, enabled, "
            "next_fire_at, last_fired_at, created_at, created_by "
            "FROM coach_recurrence WHERE project_id = ? "
            "ORDER BY CASE kind WHEN 'tick' THEN 0 WHEN 'repeat' THEN 1 "
            "ELSE 2 END, id",
            (project_id,),
        )
        rows = [_row_to_dict(r) for r in await cur.fetchall()]
    finally:
        await c.close()
    for r in rows:
        r["enabled"] = bool(r["enabled"])
    return rows


async def get_recurrence(rec_id: int) -> dict[str, Any] | None:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, project_id, kind, cadence, tz, prompt, enabled, "
            "next_fire_at, last_fired_at, created_at, created_by "
            "FROM coach_recurrence WHERE id = ?",
            (rec_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if row is None:
        return None
    d = _row_to_dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


async def _count_enabled(project_id: str) -> int:
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT COUNT(*) AS n FROM coach_recurrence "
            "WHERE project_id = ? AND enabled = 1",
            (project_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    return int(_row_to_dict(row).get("n", 0))


def _validate_minutes(cadence: str | int) -> int:
    try:
        mins = int(cadence)
    except (TypeError, ValueError) as exc:
        raise ValueError("cadence must be an integer number of minutes") from exc
    if mins < 1:
        raise ValueError("cadence must be at least 1 minute")
    if mins > 525_600:  # 365 days
        raise ValueError("cadence absurdly large; pick something < 525600")
    return mins


def _normalize_tz(tz: str | None) -> str:
    """Validate `tz` against ZoneInfo (UTC short-circuited). Defaults
    to UTC when caller omits it. Raises ValueError on bad input."""
    if not tz:
        return "UTC"
    if tz == "UTC":
        return "UTC"
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {tz!r}") from exc
    return tz


async def create_recurrence(
    *,
    project_id: str,
    kind: str,
    cadence: str | int,
    prompt: str | None = None,
    tz: str | None = None,
    created_by: str = "human",
    actor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a recurrence row. Returns the row dict.

    Validates per spec §10:
      * kind ∈ {'repeat', 'cron'} — tick rows go through
        :func:`upsert_tick`. Allowing 'tick' here would race the
        partial-unique index in odd ways.
      * cadence parses for the kind.
      * prompt non-empty for repeat / cron.
      * tz parseable when kind == 'cron'.
      * Soft cap §15.8: HARNESS_MAX_RECURRENCES_PER_PROJECT.
    """
    if kind not in ("repeat", "cron"):
        raise ValueError(
            "kind must be 'repeat' or 'cron'; use upsert_tick for 'tick'"
        )

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required for repeat / cron rows")

    if kind == "repeat":
        minutes = _validate_minutes(cadence)
        cadence_str = str(minutes)
        tz_resolved: str | None = None
        next_fire = _now_utc() + timedelta(minutes=minutes)
    else:  # cron
        if not isinstance(cadence, str):
            raise ValueError("cron cadence must be a DSL string")
        try:
            parsed = parse_cron(cadence)
        except CronParseError as exc:
            raise ValueError(str(exc)) from exc
        tz_resolved = _normalize_tz(tz)
        next_fire = compute_next_fire_at(parsed, tz_resolved, _now_utc())
        if next_fire is None:
            raise ValueError("schedule is in the past")
        cadence_str = cadence.strip()

    if await _count_enabled(project_id) >= MAX_RECURRENCES_PER_PROJECT:
        raise PermissionError(
            f"per-project recurrence cap reached "
            f"({MAX_RECURRENCES_PER_PROJECT}); trim some first"
        )

    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO coach_recurrence "
            "(project_id, kind, cadence, tz, prompt, enabled, "
            "next_fire_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (
                project_id, kind, cadence_str, tz_resolved, prompt,
                _format_iso(next_fire), created_by,
            ),
        )
        rec_id = cur.lastrowid
        await c.commit()
    finally:
        await c.close()

    row = await get_recurrence(int(rec_id))
    assert row is not None
    event: dict[str, Any] = {
        "type": "recurrence_added",
        "id": row["id"],
        "kind": row["kind"],
        "cadence": row["cadence"],
        "tz": row["tz"],
        "prompt": row["prompt"],
        "project_id": project_id,
    }
    if actor:
        event["actor"] = actor
    await _emit(event)
    return row


async def upsert_tick(
    *,
    project_id: str,
    minutes: int | None = None,
    enabled: bool | None = None,
    created_by: str = "human",
    actor: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Update or create the tick row for `project_id`.

    Pass ``minutes`` to set / update the cadence. Pass ``enabled=False``
    to disable while preserving the row. Pass both to do both. Returns
    the resulting row, or None when the caller passed ``enabled=False``
    on a project that had no tick row.
    """
    if minutes is None and enabled is None:
        raise ValueError("must pass minutes or enabled")
    if minutes is not None:
        minutes = _validate_minutes(minutes)

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, enabled, cadence FROM coach_recurrence "
            "WHERE project_id = ? AND kind = 'tick'",
            (project_id,),
        )
        existing = await cur.fetchone()
        if existing:
            existing_d = _row_to_dict(existing)
            before_snapshot = {
                "cadence": existing_d["cadence"],
                "enabled": bool(existing_d["enabled"]),
                "tz": None,
                "prompt": None,
            }
            new_enabled = (
                existing_d["enabled"] if enabled is None
                else (1 if enabled else 0)
            )
            new_cadence = (
                existing_d["cadence"] if minutes is None else str(minutes)
            )
            new_next_fire = (
                _format_iso(_now_utc() + timedelta(minutes=int(new_cadence)))
                if new_enabled and minutes is not None
                else None
            )
            if new_next_fire is None and minutes is None and enabled is True:
                # Re-enabling without changing cadence: schedule next
                # fire one cadence-unit out from now.
                new_next_fire = _format_iso(
                    _now_utc() + timedelta(minutes=int(new_cadence))
                )
            params = [new_enabled, new_cadence]
            sql = (
                "UPDATE coach_recurrence "
                "SET enabled = ?, cadence = ?"
            )
            if new_next_fire is not None:
                sql += ", next_fire_at = ?"
                params.append(new_next_fire)
            elif new_enabled == 0:
                sql += ", next_fire_at = NULL"
            sql += " WHERE id = ?"
            params.append(existing_d["id"])
            await c.execute(sql, tuple(params))
            await c.commit()
            rec_id = existing_d["id"]
            event_type = "recurrence_changed"
        else:
            if enabled is False:
                return None
            if minutes is None:
                raise ValueError(
                    "creating a tick row requires minutes"
                )
            next_fire = _format_iso(
                _now_utc() + timedelta(minutes=minutes)
            )
            cur = await c.execute(
                "INSERT INTO coach_recurrence "
                "(project_id, kind, cadence, prompt, enabled, "
                "next_fire_at, created_by) "
                "VALUES (?, 'tick', ?, NULL, 1, ?, ?)",
                (project_id, str(minutes), next_fire, created_by),
            )
            rec_id = cur.lastrowid
            await c.commit()
            event_type = "recurrence_added"
            before_snapshot = None
    finally:
        await c.close()

    row = await get_recurrence(int(rec_id))
    if row is None:
        return None

    # Spec §13:
    #   recurrence_added  → id, kind, cadence, tz, prompt
    #   recurrence_changed → id, before, after
    # tick rows have no tz / prompt — emit them as null so consumers
    # that key off the field exist regardless of kind.
    if event_type == "recurrence_added":
        event: dict[str, Any] = {
            "type": "recurrence_added",
            "id": row["id"],
            "kind": row["kind"],
            "cadence": row["cadence"],
            "tz": row["tz"],
            "prompt": row["prompt"],
            "enabled": row["enabled"],
            "project_id": project_id,
        }
    else:
        event = {
            "type": "recurrence_changed",
            "id": row["id"],
            "kind": row["kind"],
            "before": before_snapshot,
            "after": {
                "cadence": row["cadence"],
                "enabled": row["enabled"],
                "tz": row["tz"],
                "prompt": row["prompt"],
            },
            "project_id": project_id,
        }
    if actor:
        event["actor"] = actor
    await _emit(event)
    return row


async def update_recurrence(
    rec_id: int,
    *,
    cadence: str | int | None = None,
    prompt: str | None = None,
    tz: str | None = None,
    enabled: bool | None = None,
    actor: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Patch an existing recurrence row. Only the passed fields are
    changed. Returns the updated row, or None if the row is missing.
    """
    row = await get_recurrence(rec_id)
    if row is None:
        return None

    new_cadence = row["cadence"]
    new_tz = row["tz"]
    new_prompt = row["prompt"]
    new_enabled = 1 if (enabled if enabled is not None else row["enabled"]) else 0
    next_fire_changed = False
    new_next_fire: str | None = row["next_fire_at"]

    if cadence is not None or tz is not None:
        if row["kind"] == "tick" or row["kind"] == "repeat":
            mins_input = cadence if cadence is not None else new_cadence
            minutes = _validate_minutes(mins_input)
            new_cadence = str(minutes)
            new_tz = None
            base = _now_utc()
            new_next_fire = _format_iso(base + timedelta(minutes=minutes))
            next_fire_changed = True
        elif row["kind"] == "cron":
            cadence_str = cadence if cadence is not None else new_cadence
            try:
                parsed = parse_cron(cadence_str)
            except CronParseError as exc:
                raise ValueError(str(exc)) from exc
            new_tz = _normalize_tz(tz if tz is not None else new_tz)
            nxt = compute_next_fire_at(parsed, new_tz, _now_utc())
            if nxt is None:
                raise ValueError("schedule is in the past")
            new_cadence = cadence_str.strip()
            new_next_fire = _format_iso(nxt)
            next_fire_changed = True
    if prompt is not None:
        if row["kind"] == "tick":
            raise ValueError("tick rows have no prompt")
        new_prompt = prompt.strip()
        if not new_prompt:
            raise ValueError("prompt cannot be empty")

    c = await configured_conn()
    try:
        sql = (
            "UPDATE coach_recurrence SET cadence = ?, tz = ?, "
            "prompt = ?, enabled = ?"
        )
        params: list[Any] = [new_cadence, new_tz, new_prompt, new_enabled]
        if next_fire_changed:
            sql += ", next_fire_at = ?"
            params.append(new_next_fire)
        elif new_enabled == 0:
            sql += ", next_fire_at = NULL"
        sql += " WHERE id = ?"
        params.append(rec_id)
        await c.execute(sql, tuple(params))
        await c.commit()
    finally:
        await c.close()

    after = await get_recurrence(rec_id)
    assert after is not None
    event: dict[str, Any] = {
        "type": "recurrence_changed",
        "id": rec_id,
        "kind": after["kind"],
        "before": {
            "cadence": row["cadence"], "tz": row["tz"],
            "prompt": row["prompt"], "enabled": row["enabled"],
        },
        "after": {
            "cadence": after["cadence"], "tz": after["tz"],
            "prompt": after["prompt"], "enabled": after["enabled"],
        },
        "project_id": after["project_id"],
    }
    if actor:
        event["actor"] = actor
    await _emit(event)
    return after


async def delete_recurrence(
    rec_id: int, actor: dict[str, Any] | None = None,
) -> bool:
    """Remove a recurrence row. Returns True if a row was deleted."""
    row = await get_recurrence(rec_id)
    if row is None:
        return False
    c = await configured_conn()
    try:
        await c.execute(
            "DELETE FROM coach_recurrence WHERE id = ?", (rec_id,)
        )
        await c.commit()
    finally:
        await c.close()
    event: dict[str, Any] = {
        "type": "recurrence_deleted",
        "id": rec_id,
        "kind": row["kind"],
        "project_id": row["project_id"],
    }
    if actor:
        event["actor"] = actor
    await _emit(event)
    return True


async def recurrence_scheduler_loop() -> None:
    """Background task: every SCHEDULER_TICK_SECONDS, check the active
    project for due rows and fire them. The unified replacement for
    the legacy per-flavor loops; recurrence-specs.md phase 8 removed
    the old `coach_tick_loop` / `coach_repeat_loop` and their module
    globals."""
    logger.info(
        "recurrence scheduler running (resolution %ds)",
        SCHEDULER_TICK_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)
        except asyncio.CancelledError:
            raise
        try:
            await _scheduler_iteration()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("recurrence scheduler: iteration failed")
