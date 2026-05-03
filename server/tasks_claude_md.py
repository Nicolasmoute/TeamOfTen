"""Kanban lifecycle paragraph injected into per-project CLAUDE.md.

Both Coach and Players read CLAUDE.md every turn (the per-project
file is loaded into the system prompt). To make the kanban lifecycle
a first-class shared baseline — not just something Coach knows from
its dynamic prompt block — the harness ships a static paragraph that
lands in every project's CLAUDE.md alongside the existing harness
conventions.

Marker-delimited region: `<!-- KANBAN-LIFECYCLE-START -->` /
`<!-- KANBAN-LIFECYCLE-END -->`. Pattern lifted from
`server.compass.pipeline.claude_md` (which uses the same approach
for the Compass section) — the two injectors coexist because they
target different markers.

This module exposes:

  - `render_kanban_block()` — pure function returning the markered
    block text. Static; no project-specific tailoring.
  - `inject_kanban_block(project_id)` — idempotent injection into
    the project's CLAUDE.md. Replaces between the markers if present,
    appends to end-of-file otherwise. Mirrors to kDrive.

Wired in `main.py:lifespan` to run once per known project on harness
boot, alongside the Compass claude_md injection.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import PurePosixPath

from server.paths import project_paths
from server.webdav import webdav

logger = logging.getLogger("harness.tasks_claude_md")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


KANBAN_MD_START_MARKER = "<!-- KANBAN-LIFECYCLE-START -->"
KANBAN_MD_END_MARKER = "<!-- KANBAN-LIFECYCLE-END -->"


# The paragraph is static — same text in every project. When the
# kanban surface evolves (new tools, new role, new policy), update
# this constant; the next harness boot's `inject_kanban_block` call
# rewrites every project's CLAUDE.md to match.
_KANBAN_BLOCK_BODY = """## Task lifecycle (kanban)

Tasks flow through stages: **plan → execute → audit_syntax → audit_semantics → ship → archive**.
Standard tasks traverse the full pipeline. Simple tasks (typos, log tweaks, one-line fixes)
jump plan → execute → archive directly.

Each task produces durable markdown artifacts under
`/data/projects/<project_id>/working/tasks/<task_id>/`:

- `spec.md` — the plan, written before execute (required for standard tasks; optional for simple)
- `audits/audit_<round>_<kind>.md` — Player auditor reports (kind = syntax | semantics; one file per round)

### Strict role boundaries

- **Coach** plans (or delegates planning) and assigns Players to roles. Coach does NOT
  execute, audit, or merge. Coach's task tools: `coord_write_task_spec`,
  `coord_assign_planner`, `coord_assign_task` (executor; accepts a single Player slot or
  a list-as-pool), `coord_assign_auditor` (kind=syntax|semantics), `coord_assign_shipper`,
  `coord_set_task_complexity`, `coord_advance_task_stage`, `coord_set_task_blocked`.
- **Players** execute, audit, and ship. The relevant tools:
    - `coord_my_assignments` — call this any time you're not sure what to do; returns your
      full plate (active executor task / pending audits / pending ship / eligible pools).
    - `coord_claim_task(task_id)` — pull yourself into a posted pool task. First-claim wins.
    - `coord_commit_push(task_id, message)` — pass `task_id` so the kanban auto-advances.
    - `coord_submit_audit_report(task_id, kind, body, verdict)` — auditors submit pass/fail.
    - `coord_mark_shipped(task_id)` — shipper calls after the merge lands.

### Audit verdict routing

Pass → next stage. Fail → reverts to execute; the spec + latest audit report attach to
the task and the executor is auto-woken with both. Compass auto-audit fires informationally
on every commit; the assigned Player auditor is the gate, not Compass.

### Simple-task discipline

When Coach marks a task simple, the executor SELF-AUDITS: run the relevant tests, sanity-check
the change, then `coord_commit_push`. The board archives directly on commit; there is no
separate audit pass. Coach should mark a task simple only when the change is small and
well-bounded enough that self-audit is sufficient."""


def render_kanban_block() -> str:
    """Return the full marker-wrapped block as a single string,
    suitable for direct insertion into CLAUDE.md."""
    return (
        f"{KANBAN_MD_START_MARKER}\n"
        f"{_KANBAN_BLOCK_BODY.rstrip()}\n"
        f"{KANBAN_MD_END_MARKER}"
    )


async def inject_kanban_block(project_id: str) -> bool:
    """Replace the marker-delimited region of the project's CLAUDE.md.

    Idempotent: re-running with the same body produces no change to
    the rest of the file. If markers are missing, appends the block at
    end-of-file with a blank-line separator. Mirrors to kDrive.

    Returns True on success. False on local-write failure (logs and
    moves on — kDrive mirror is best-effort).
    """
    pp = project_paths(project_id)
    target = pp.claude_md
    full_block = render_kanban_block()

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception(
            "tasks_claude_md.inject: mkdir failed: %s", target.parent
        )
        return False

    pattern = re.compile(
        re.escape(KANBAN_MD_START_MARKER) + r".*?"
        + re.escape(KANBAN_MD_END_MARKER),
        re.DOTALL,
    )

    if not target.exists():
        try:
            target.write_text(
                full_block + "\n", encoding="utf-8", newline="\n"
            )
        except OSError:
            logger.exception(
                "tasks_claude_md.inject: initial write failed: %s", target
            )
            return False
        await _mirror_to_kdrive(project_id, full_block + "\n")
        return True

    try:
        existing = target.read_text(encoding="utf-8")
    except OSError:
        logger.exception(
            "tasks_claude_md.inject: read failed: %s", target
        )
        return False

    if pattern.search(existing):
        new_content = pattern.sub(full_block, existing)
    else:
        new_content = existing.rstrip() + "\n\n" + full_block + "\n"

    if new_content == existing:
        return True  # idempotent no-op

    try:
        target.write_text(new_content, encoding="utf-8", newline="\n")
    except OSError:
        logger.exception(
            "tasks_claude_md.inject: write failed: %s", target
        )
        return False

    await _mirror_to_kdrive(project_id, new_content)
    return True


async def inject_into_all_projects() -> int:
    """Walk every known project and inject the block. Called from
    `lifespan` on harness boot. Returns the count of successful
    injections (0 if there are no projects yet)."""
    from server.db import configured_conn
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT id FROM projects WHERE archived = 0")
        rows = await cur.fetchall()
    finally:
        await c.close()
    count = 0
    for r in rows:
        pid = dict(r)["id"]
        try:
            ok = await inject_kanban_block(pid)
            if ok:
                count += 1
        except Exception:
            logger.exception(
                "tasks_claude_md.inject_into_all_projects: failed for %s",
                pid,
            )
    return count


async def _mirror_to_kdrive(project_id: str, content: str) -> None:
    """Best-effort kDrive mirror — same shape as the Compass injector.
    Failure logs but doesn't propagate; the local project_sync loop
    also covers this path."""
    if not webdav.enabled:
        return
    remote = str(PurePosixPath("projects") / project_id / "CLAUDE.md")
    try:
        await webdav.write_text(remote, content)
    except Exception:
        logger.exception(
            "tasks_claude_md.inject: kDrive mirror failed: %s", remote
        )


__all__ = [
    "KANBAN_MD_START_MARKER",
    "KANBAN_MD_END_MARKER",
    "render_kanban_block",
    "inject_kanban_block",
    "inject_into_all_projects",
]
