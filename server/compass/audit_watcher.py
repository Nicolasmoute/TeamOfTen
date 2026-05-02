"""Compass audit watcher — auto-fires `compass_audit` on artifact events.

The §5 spec puts Coach in charge of calling `compass_audit(artifact)`
"whenever a worker produces a meaningful unit of work". In practice
Coach forgets, and the only fall-back was the dashboard's manual
paste UI — which is the wrong path (humans don't produce the
artifacts, agents do). This subscriber closes the loop: it watches
the event bus for the four artifact events the harness already
publishes — `commit_pushed`, `decision_written`, `knowledge_written`,
and `output_saved` — and calls `audit.audit_work` for each one,
without any human or Coach action.

`output_saved` events get richer treatment than the other three
(Tier B per compass-specs §5.5.2): the watcher reads the saved file's
body via `output_extractor` for text-native and office formats and
folds the extracted text into the audit artifact. Images and unknown
formats fall back to a path-only header. This matters because
binary deliverables are what the human consumes — auditing only
their path is a false economy on the highest-stakes lane.

Design constraints:
  - **Project-scoped, not actor-scoped.** Each event carries a
    `project_id` (auto-stamped by `EventBus.publish`); the watcher
    audits that project regardless of which project is currently
    active in the UI. So an inactive project still gets audited when
    its commits land, matching the scheduler's "iterate all projects"
    rule. We don't audit when Compass is disabled for the event's
    project (`compass_enabled_<id>` flag).
  - **Cost-gated.** Auto-audits respect the team daily cap
    (`HARNESS_TEAM_DAILY_CAP`) and the per-project enable flag.
    `audit_work` itself doesn't gate on cost (the only prior caller
    was Coach via MCP, which already saw the cap in its own prompts).
    We add the gate here so a busy commit day doesn't blow the budget.
  - **Debounced.** A burst of commits on the same slot in the same
    project shouldn't fan out to N parallel audits — that's
    expensive and the verdicts would all read against the same
    lattice anyway. Debounce window is per-(project, agent, type).
  - **Fire-and-forget.** The watcher pushes audit_work onto a
    background task so a slow LLM call doesn't backpressure the bus.
    Audit failures are logged and dropped — never block the
    originating tool call (Compass §10.6: audits never block work).
  - **Opt-out.** Set `HARNESS_COMPASS_AUTO_AUDIT=false` to disable
    the watcher entirely (e.g. for cost-constrained deploys that
    only want manual audits via the MCP tool).

The dashboard's manual paste UI is removed in the same change —
audits are now driven by events; the audit log is read-only on the
human's side, matching §5.3 ("the human reads the log when curious").
The `POST /api/compass/audit` HTTP endpoint is kept as a debug
backstop (curl-able for testing) but is not surfaced in the UI.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any

from server.events import bus

from server.compass import audit as cmp_audit
from server.compass import config

logger = logging.getLogger("harness.compass.audit_watcher")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Event types that represent "a meaningful unit of work" worth auditing.
# Keep this list tight — every entry costs an LLM call per event burst.
WATCHED_EVENT_TYPES: frozenset[str] = frozenset({
    "commit_pushed",
    "decision_written",
    "knowledge_written",
    # `output_saved` audits binary deliverables (Tier B per
    # compass-specs §5.5). The artifact composer reads the file body
    # via `output_extractor` for office/text formats and falls back to
    # path-only for images. Outputs are infrequent but high-stakes —
    # they're what the human consumes, so a real review pays for itself.
    "output_saved",
})


# ---------------------------------------------------------------- state


# Module-level lifecycle handles, mirroring the telegram bridge pattern.
_current_task: asyncio.Task[None] | None = None
_stopping = False
# Per-(project, agent, type) timestamp of the last audit fired. Read +
# written from the bus-consumer task only, so no lock needed.
_last_fire: dict[tuple[str, str, str], float] = {}


def is_running() -> bool:
    """True iff the watcher background task is alive."""
    return _current_task is not None and not _current_task.done()


# ---------------------------------------------------------------- lifecycle


async def start_audit_watcher() -> None:
    """Start the background subscriber. Idempotent. No-op when the
    feature flag is off.

    Subscribes to the bus **synchronously** before scheduling the
    consumer task. If we deferred the subscribe to inside the task,
    any event published in the window between `create_task` returning
    and the task actually running would be lost — easy to hit in
    tests, possible under load.
    """
    global _current_task, _stopping
    if not config.AUTO_AUDIT_ENABLED:
        logger.info("compass.audit_watcher: disabled via env (HARNESS_COMPASS_AUTO_AUDIT)")
        return
    if is_running():
        return
    _stopping = False
    _last_fire.clear()
    queue = bus.subscribe()
    loop = asyncio.get_running_loop()
    _current_task = loop.create_task(
        _run(queue), name="harness.compass.audit_watcher",
    )
    logger.info(
        "compass.audit_watcher: started (debounce=%ss, types=%s)",
        config.AUTO_AUDIT_DEBOUNCE_SECONDS,
        sorted(WATCHED_EVENT_TYPES),
    )


async def stop_audit_watcher(timeout: float = 2.0) -> None:
    """Stop the background subscriber and drain. Idempotent."""
    global _current_task, _stopping
    _stopping = True
    task = _current_task
    if task is None:
        return
    if not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _current_task = None


# ---------------------------------------------------------------- core


async def _run(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Drain the pre-subscribed queue, dispatch matching events."""
    try:
        while not _stopping:
            try:
                ev = await queue.get()
            except asyncio.CancelledError:
                return
            try:
                await _handle_event(ev)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "compass.audit_watcher: handler crashed on event %r",
                    ev.get("type"),
                )
    finally:
        bus.unsubscribe(queue)


async def _handle_event(ev: dict[str, Any]) -> None:
    """Filter an event and spawn an audit task if appropriate."""
    etype = ev.get("type") or ""
    if etype not in WATCHED_EVENT_TYPES:
        return
    project_id = (ev.get("project_id") or "").strip()
    if not project_id:
        return
    agent_id = (ev.get("agent_id") or "").strip() or "system"

    if not await _is_compass_enabled(project_id):
        return

    if not _debounce_ok(project_id, agent_id, etype):
        return

    # Cost gate — read live so a deploy bumping the cap mid-day takes
    # effect without restart. We're pessimistic: if reading the spend
    # fails for any reason, we DON'T fire (better to skip an audit
    # than to blow past the cap when the bookkeeping is broken).
    if not await _within_cost_cap():
        return

    artifact = _compose_artifact(ev)
    if not artifact:
        return

    asyncio.create_task(
        _safe_audit(project_id, artifact, etype),
        name=f"compass.audit_watcher.fire:{etype}:{project_id}:{agent_id}",
    )


async def _safe_audit(project_id: str, artifact: str, etype: str) -> None:
    """Run audit_work in a background task and never raise.

    `audit_work` itself catches LLM errors and degrades to an
    'aligned' verdict; this wrapper is a final safety net so a
    background task failure doesn't go unnoticed in logs.
    """
    try:
        await cmp_audit.audit_work(project_id, artifact)
    except Exception:
        logger.exception(
            "compass.audit_watcher: audit_work failed (project=%s, src=%s)",
            project_id, etype,
        )


# --------------------------------------------------------- predicates


def _debounce_ok(project_id: str, agent_id: str, etype: str) -> bool:
    """Return True if enough time has passed since the last fire for
    this (project, agent, event_type) tuple. Updates the timestamp on
    success.
    """
    window = config.AUTO_AUDIT_DEBOUNCE_SECONDS
    if window <= 0:
        return True
    key = (project_id, agent_id, etype)
    now = time.monotonic()
    last = _last_fire.get(key, 0.0)
    if now - last < window:
        return False
    _last_fire[key] = now
    return True


async def _is_compass_enabled(project_id: str) -> bool:
    """Read `compass_enabled_<id>` from team_config. Same shape as the
    scheduler check — mirrored here to avoid a dependency on the
    scheduler module's internal helper.
    """
    from server.db import configured_conn  # noqa: PLC0415

    try:
        c = await configured_conn()
        try:
            cur = await c.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (config.enabled_key(project_id),),
            )
            row = await cur.fetchone()
        finally:
            await c.close()
    except Exception:
        logger.exception("compass.audit_watcher: enable-check query failed")
        return False
    if not row:
        return False
    val = (dict(row).get("value") or "").strip().lower()
    return val in ("1", "true", "yes")


async def _within_cost_cap() -> bool:
    """True iff the team's daily spend is below the configured cap.
    A 0/negative cap disables the check (matching agents.py's
    convention)."""
    from server.agents import TEAM_DAILY_CAP_USD, _today_spend  # noqa: PLC0415

    if TEAM_DAILY_CAP_USD <= 0:
        return True
    try:
        spent = await _today_spend()
    except Exception:
        logger.exception("compass.audit_watcher: spend lookup failed; skipping audit")
        return False
    return spent < TEAM_DAILY_CAP_USD


# --------------------------------------------------------- artifact prep


# Cap on the artifact text we shove into the audit prompt for short-
# header artifacts (commit / decision / knowledge / output-metadata
# only). Long commit bodies and decision documents are common; the
# audit verdict only needs the gist. Keeps prompts cheap and
# predictable.
_ARTIFACT_TRUNCATE = 4000

# Cap for body-included output artifacts. Higher than `_ARTIFACT_TRUNCATE`
# because we deliberately fold extracted document text in — but still
# bounded so a 200-page PDF can't blow up the prompt. The extractor
# does its own per-format truncation at MAX_BODY_CHARS; this is the
# final outer cap that includes the metadata header + body.
_OUTPUT_ARTIFACT_TRUNCATE = 18_000


def _compose_artifact(ev: dict[str, Any]) -> str:
    """Render a one-shot artifact string for the audit prompt.

    Each event type has its own shape; the helper normalizes them into
    a short, self-describing blob so the audit prompt has enough to
    reason about without needing the full DB context.
    """
    etype = ev.get("type") or ""
    actor = (ev.get("agent_id") or "system").strip()
    if etype == "commit_pushed":
        sha = (ev.get("sha") or "?").strip()
        message = (ev.get("message") or "").strip()
        pushed = "pushed" if ev.get("pushed") else "local-only"
        return _truncate(
            f"[commit] {actor} {pushed} {sha}\n\n{message}"
        )
    if etype == "decision_written":
        title = (ev.get("title") or "").strip()
        size = ev.get("size") or 0
        # Decision body isn't on the event payload (only title + size)
        # — that's by design; reading the file would couple this module
        # to the decisions storage layer. Title is usually informative
        # enough for an audit verdict; if Coach wants deeper reasoning
        # they call `compass_audit` directly with the body.
        return _truncate(
            f"[decision] {actor} wrote: {title} ({size} chars)"
        )
    if etype == "knowledge_written":
        path = (ev.get("path") or "").strip()
        size = ev.get("size") or 0
        return _truncate(
            f"[knowledge] {actor} saved knowledge[{path}] ({size} chars)"
        )
    if etype == "output_saved":
        return _compose_output_artifact(ev, actor)
    return ""


def _compose_output_artifact(ev: dict[str, Any], actor: str) -> str:
    """Render an audit artifact for `output_saved`. For text-native and
    office formats we extract the body and inline it; for images and
    unknown formats we fall back to a path + size header.

    The on-disk file is at `outputs.OUTPUTS_DIR / path` — outputs are
    a global lane (not per-project), so resolution doesn't depend on
    the event's `project_id` field. Lazy import of `outputs` to avoid
    pulling that module into the import graph for the other event
    types.
    """
    from server.outputs import OUTPUTS_DIR  # noqa: PLC0415
    from server.compass import output_extractor  # noqa: PLC0415

    path = (ev.get("path") or "").strip()
    size = ev.get("bytes") or 0
    if not path:
        return ""
    full = OUTPUTS_DIR / path
    head = f"[output] {actor} saved outputs[{path}] ({size} bytes)"
    body = None
    try:
        if full.exists() and full.is_file():
            body = output_extractor.extract_body(full)
    except Exception:
        logger.exception(
            "compass.audit_watcher: output extractor crashed on %s", path,
        )
        body = None
    if body:
        # Use the extension as the format hint in the separator. If the
        # path somehow has no extension (shouldn't happen — `coord_save_output`
        # rejects extension-less leaves at write time — but defensive),
        # render a generic separator instead of `( extracted)`.
        ext = full.suffix.lower() or "(no ext)"
        artifact = (
            f"{head}\n\n--- document body ({ext} extracted) ---\n{body}"
        )
        return _truncate_long(artifact)
    return _truncate(head)


def _truncate(s: str) -> str:
    s = (s or "").strip()
    if len(s) <= _ARTIFACT_TRUNCATE:
        return s
    return s[:_ARTIFACT_TRUNCATE] + f"\n\n[truncated — was {len(s)} chars]"


def _truncate_long(s: str) -> str:
    """Higher cap for body-included output artifacts."""
    s = (s or "").strip()
    if len(s) <= _OUTPUT_ARTIFACT_TRUNCATE:
        return s
    return (
        s[:_OUTPUT_ARTIFACT_TRUNCATE]
        + f"\n\n[truncated — artifact was {len(s)} chars]"
    )


__all__ = [
    "WATCHED_EVENT_TYPES",
    "is_running",
    "start_audit_watcher",
    "stop_audit_watcher",
]
