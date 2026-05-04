"""Canonical per-project CLAUDE.md template + Coach-driven reconciliation.

Single source of truth for what the harness wants every downstream
project's CLAUDE.md to reflect. The canonical body lives in
`server/templates/app_dev_claude_md.md` (one file per playbook flavour
when more flavours land). When the harness ships functionality
downstream projects need to be aware of (a new MCP tool category, a
new lifecycle stage, a renamed concept, a new convention), update the
template — the next project activation propagates the change.

Two consumers:

  1. Project creation — `paths.write_project_claude_md_stub` reads
     `canonical_project_claude_md_template(...)` and seeds the new
     project's `CLAUDE.md` with it. First-write only.
  2. Project activation (and harness boot for the active project) —
     `update_claude_md_via_coach(project_id)` runs a hidden one-shot
     LLM turn that wears Coach's identity. The turn reads the latest
     canonical template + the project's current `CLAUDE.md` and
     rewrites the file to reflect the latest harness rules while
     preserving every line of project-specific content. Skipped when
     the canonical template hash hasn't changed since the last
     successful update (`team_config['claude_md_template_hash_<id>']`,
     mirrors the Compass `compass_truth_hash_<id>` precedent).

The Coach pane shows only `.sys` rows (`claude_md_update_started` /
`_completed` / `_skipped` / `_failed`) — not the full prompt/response.
On validation failure we also emit a `human_attention` event so the
EnvPane attention strip + Telegram bridge raise the issue.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Final

from server.paths import _read_template, project_paths
from server.webdav import webdav

logger = logging.getLogger("harness.project_claude_md")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Map of project-type -> template filename under `server/templates/`.
# Add an entry here when a new playbook flavour ships.
_TEMPLATE_FILES: Final[dict[str, str]] = {
    "app_dev": "app_dev_claude_md.md",
}
_DEFAULT_PROJECT_TYPE: Final[str] = "app_dev"


# Minimal skeleton used only when the canonical template file is
# missing on disk (renamed, deleted, or never shipped). Keeps the
# harness boot + project creation path robust against a missing file.
_FALLBACK_BODY: Final[str] = """# Project: {name}

## Repo
{repo}

## Project objectives

The project's goals, success criteria, and scope live in
`/data/projects/{slug}/project-objectives.md`.

## Stakeholders
<filled in by Coach>

## Team
<filled in by Coach>

## Glossary
<filled in by Coach>

## Conventions
<project-specific rules>
"""


# Per-project lock so two concurrent activations (or
# activation + boot) don't fight over the same CLAUDE.md.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(project_id: str) -> asyncio.Lock:
    lock = _locks.get(project_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[project_id] = lock
    return lock


# ---------------------------------------------------------------- canonical template


def canonical_project_claude_md_template(
    name: str,
    slug: str,
    repo_url: str | None = None,
    project_type: str = _DEFAULT_PROJECT_TYPE,
) -> str:
    """Read the canonical CLAUDE.md template for the project's
    playbook from `server/templates/`, substitute `{name}`, `{slug}`,
    `{repo}` placeholders, and return the full body. Single source
    of truth for both new-project seeding AND the Coach-driven
    reconciliation flow.
    """
    fname = _TEMPLATE_FILES.get(project_type, _TEMPLATE_FILES[_DEFAULT_PROJECT_TYPE])
    body = _read_template(fname)
    if not body:
        logger.warning(
            "project_claude_md: template %s missing; using fallback skeleton",
            fname,
        )
        body = _FALLBACK_BODY
    repo = (repo_url or "").strip() or "<no repo configured>"
    # str.replace (not str.format / Template) so the canonical template
    # body can carry literal `{` / `}` characters — the kanban
    # lifecycle section includes JSON snippets like
    # `{"stage":"execute"}` which would crash str.format.
    return (
        body.replace("{name}", name)
            .replace("{slug}", slug)
            .replace("{repo}", repo)
    )


# ---------------------------------------------------------------- hash gate


def _template_hash_key(project_id: str) -> str:
    return f"claude_md_template_hash_{project_id}"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _get_stored_hash(project_id: str) -> str | None:
    from server.db import configured_conn  # lazy
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?",
            (_template_hash_key(project_id),),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return None
    try:
        return row[0]
    except Exception:
        return None


async def _set_stored_hash(project_id: str, h: str) -> None:
    from server.db import configured_conn  # lazy
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT OR REPLACE INTO team_config (key, value) VALUES (?, ?)",
            (_template_hash_key(project_id), h),
        )
        await c.commit()
    finally:
        await c.close()


# ---------------------------------------------------------------- validation

_MIN_BYTES = 200
_MAX_BYTES = 500_000


def _validate_output(text: str) -> tuple[bool, str]:
    """Returns (ok, reason). `ok=False` means write nothing + emit
    `human_attention`."""
    if not text or not text.strip():
        return False, "empty output"
    body = text.strip()
    n = len(body.encode("utf-8"))
    if n < _MIN_BYTES:
        return False, f"output too short ({n} bytes < {_MIN_BYTES})"
    if n > _MAX_BYTES:
        return False, f"output too long ({n} bytes > {_MAX_BYTES})"
    for line in body.splitlines():
        if line.strip():
            if not line.lstrip().startswith("#"):
                return False, "first non-blank line is not a markdown heading"
            break
    return True, ""


# ---------------------------------------------------------------- kDrive mirror


async def _mirror_to_kdrive(project_id: str, content: str) -> None:
    """Best-effort kDrive mirror — failure logs but doesn't propagate;
    the project-sync loop also covers this path."""
    if not webdav.enabled:
        return
    remote = str(PurePosixPath("projects") / project_id / "CLAUDE.md")
    try:
        await webdav.write_text(remote, content)
    except Exception:
        logger.exception("project_claude_md: kDrive mirror failed: %s", remote)


# ---------------------------------------------------------------- prompts


_SYSTEM_PROMPT = """You are Coach, the orchestrator of an 11-agent team
(1 Coach + 10 Players) running inside the TeamOfTen harness. The
harness has asked you to reconcile a project's CLAUDE.md with the
latest canonical harness-supplied template.

The CANONICAL TEMPLATE block below is the harness's current rules,
conventions, and lifecycle paragraphs — it reflects the latest harness
functionality every downstream project should be aware of. The
CURRENT PROJECT FILE block is the project's existing CLAUDE.md, which
may carry hand-written content (project goals, custom rules,
stakeholders, glossary, team composition, decisions, project-specific
notes).

Produce a single new CLAUDE.md body that:

  1. Reflects every harness-rule paragraph from the canonical
     template — keep the surface up to date with the current harness
     functionality (new MCP tools, new lifecycle stages, renamed
     concepts, new conventions).
  2. Preserves EVERY paragraph, line, and list item of project-
     specific content from the current project file that is not
     directly contradicted by the canonical template. Do not delete
     glossary entries, stakeholder lists, decisions, custom
     conventions, or hand-written notes.
  3. Where the canonical template has a section the current project
     file already fills in (Stakeholders, Glossary, Team), KEEP the
     project's filled-in content; only update the surrounding harness-
     supplied prose.
  4. When in doubt, keep the project's content.
  5. Format placeholders ({name}, {repo}, {slug}) have already been
     substituted in the canonical template — do not re-emit them.

Output ONLY the full new CLAUDE.md body. No code fences. No preamble.
No commentary. The first character of your output must be the first
character of the new file (typically `#`)."""


def _user_prompt(template: str, current: str) -> str:
    parts = [
        "=== CANONICAL TEMPLATE (latest harness-supplied) ===",
        template,
        "",
        "=== CURRENT PROJECT FILE ===",
        current if current.strip()
        else "<empty - write the file from scratch using the template>",
        "",
        "=== END ===",
        "",
        "Output the full new CLAUDE.md body only. Start with `#`.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- orchestration


async def _emit(event_type: str, **payload) -> None:
    from server.events import bus  # lazy
    try:
        await bus.publish({
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "coach",
            "type": event_type,
            **payload,
        })
    except Exception:
        logger.exception(
            "project_claude_md: bus.publish failed for %s", event_type,
        )


async def _project_meta(
    project_id: str,
) -> tuple[str, str, str | None] | None:
    """Return (name, slug, repo_url) or None if missing/archived."""
    from server.db import configured_conn  # lazy
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT id, name, repo_url, archived FROM projects WHERE id = ?",
            (project_id,),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        return None
    d = dict(row)
    if d.get("archived"):
        return None
    return d["name"], d["id"], d.get("repo_url")


async def _resolve_coach_model() -> str | None:
    """Coach's role-default model alias (typically `latest_opus` for
    Claude). `compass.llm.call` resolves the alias to a concrete id
    before handing it to the SDK."""
    try:
        from server.models_catalog import role_default_model  # lazy
        m = role_default_model("coach", "claude")
        return m or None
    except Exception:
        return None


def _line_count(text: str) -> int:
    if not text:
        return 0
    n = text.count("\n")
    if not text.endswith("\n"):
        n += 1
    return n


async def update_claude_md_via_coach(
    project_id: str, source: str = "activation",
) -> str:
    """Reconcile `<project>/CLAUDE.md` with the latest canonical
    template via a hidden one-shot Coach-identity LLM turn.

    Idempotent: skips when the canonical template hash hasn't changed
    since the last successful update. Cost-cap aware. Lock-serialised
    per project. Validation failures emit `human_attention` so the
    human notices.

    Always returns a status string for the caller / tests:
      - `"completed"`
      - `"skipped:<reason>"` (`unchanged`, `cost_capped`,
        `missing_or_archived`)
      - `"failed:<reason>"`

    Never raises.
    """
    lock = _lock_for(project_id)
    async with lock:
        try:
            return await _run_update_locked(project_id, source)
        except Exception as e:
            logger.exception(
                "update_claude_md_via_coach: unexpected failure for %s",
                project_id,
            )
            reason = f"{type(e).__name__}: {str(e)[:200]}"
            await _emit(
                "claude_md_update_failed",
                project_id=project_id, source=source, reason=reason,
            )
            await _emit(
                "human_attention",
                subject=f"CLAUDE.md update failed for project {project_id}",
                body=(
                    f"Unexpected failure during template reconciliation:\n\n"
                    f"{reason}"
                ),
                urgency="normal",
            )
            return f"failed:{reason}"


async def _run_update_locked(project_id: str, source: str) -> str:
    meta = await _project_meta(project_id)
    if meta is None:
        logger.info(
            "project_claude_md: skipping %s (missing/archived)", project_id,
        )
        return "skipped:missing_or_archived"
    name, slug, repo_url = meta

    # Cost-cap check — defensive; never blocks on import errors.
    try:
        from server.agents import (  # lazy
            TEAM_DAILY_CAP_USD,
            _today_spend,
        )
        if TEAM_DAILY_CAP_USD > 0:
            spent = await _today_spend()
            if spent >= TEAM_DAILY_CAP_USD:
                await _emit(
                    "claude_md_update_skipped",
                    project_id=project_id, source=source,
                    reason="cost_capped",
                )
                return "skipped:cost_capped"
    except Exception:
        logger.exception(
            "project_claude_md: cost-cap check failed (continuing)"
        )

    # Build canonical template + hash gate.
    template = canonical_project_claude_md_template(
        name=name, slug=slug, repo_url=repo_url,
    )
    new_hash = _hash(template)
    stored = await _get_stored_hash(project_id)
    if stored == new_hash:
        await _emit(
            "claude_md_update_skipped",
            project_id=project_id, source=source, reason="unchanged",
        )
        return "skipped:unchanged"

    # Read current file.
    pp = project_paths(project_id)
    target = pp.claude_md
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.exception(
            "project_claude_md: mkdir failed for %s", target.parent,
        )
        await _emit(
            "claude_md_update_failed",
            project_id=project_id, source=source,
            reason=f"mkdir_failed: {e}",
        )
        await _emit(
            "human_attention",
            subject=f"CLAUDE.md update failed for project {name}",
            body=f"Could not create project directory: {e}",
            urgency="normal",
        )
        return "failed:mkdir"

    current = ""
    if target.exists():
        try:
            current = target.read_text(encoding="utf-8")
        except OSError:
            logger.exception(
                "project_claude_md: read failed for %s", target,
            )
            current = ""
    bytes_before = len(current.encode("utf-8"))
    lines_before = _line_count(current)

    await _emit(
        "claude_md_update_started",
        project_id=project_id, source=source,
    )

    # One-shot LLM call — Compass-style helper.
    try:
        from server.compass.llm import (  # lazy
            CompassLLMError,
            call as llm_call,
        )
    except Exception:
        logger.exception(
            "project_claude_md: compass.llm import failed"
        )
        await _emit(
            "claude_md_update_failed",
            project_id=project_id, source=source,
            reason="llm_unavailable",
        )
        await _emit(
            "human_attention",
            subject=f"CLAUDE.md update failed for project {name}",
            body="LLM helper module unavailable — see server logs.",
            urgency="normal",
        )
        return "failed:llm_unavailable"

    coach_model = await _resolve_coach_model()
    user_prompt = _user_prompt(template, current)
    try:
        result = await llm_call(
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            model=coach_model,
            project_id=project_id,
            label="claude_md_update",
        )
    except CompassLLMError as e:
        reason = f"llm_call_failed: {str(e)[:200]}"
        logger.exception("project_claude_md: LLM call failed")
        await _emit(
            "claude_md_update_failed",
            project_id=project_id, source=source, reason=reason,
        )
        await _emit(
            "human_attention",
            subject=f"CLAUDE.md update failed for project {name}",
            body=f"LLM call failed during template reconciliation: {reason}",
            urgency="normal",
        )
        return "failed:llm"
    except Exception as e:
        reason = f"llm_call_unexpected: {type(e).__name__}: {str(e)[:200]}"
        logger.exception("project_claude_md: unexpected LLM exception")
        await _emit(
            "claude_md_update_failed",
            project_id=project_id, source=source, reason=reason,
        )
        await _emit(
            "human_attention",
            subject=f"CLAUDE.md update failed for project {name}",
            body=f"Unexpected exception during reconciliation: {reason}",
            urgency="normal",
        )
        return "failed:llm_exception"

    new_text = (result.text or "").strip()
    if result.is_error:
        snippet = new_text[:400] if new_text else "(empty output)"
        first_err = result.errors[0] if result.errors else snippet
        reason = f"llm_is_error: {first_err}"
        await _emit(
            "claude_md_update_failed",
            project_id=project_id, source=source, reason=reason,
        )
        await _emit(
            "human_attention",
            subject=f"CLAUDE.md update failed for project {name}",
            body=(
                "Coach's reconciliation turn returned an error. "
                f"First error: {first_err}\n\n"
                f"Output snippet:\n{snippet}\n\n"
                "Hash NOT updated; next activation will retry."
            ),
            urgency="normal",
        )
        return "failed:llm_error"

    ok, why = _validate_output(new_text)
    if not ok:
        snippet = new_text[:400] if new_text else "(empty)"
        await _emit(
            "claude_md_update_failed",
            project_id=project_id, source=source, reason=why,
        )
        await _emit(
            "human_attention",
            subject=f"CLAUDE.md update failed for project {name}",
            body=(
                f"Validation failed: {why}\n\n"
                f"Output snippet (first 400 chars):\n{snippet}\n\n"
                "Hash NOT updated; next activation will retry."
            ),
            urgency="normal",
        )
        return "failed:validation"

    # Atomic write: tempfile + os.replace.
    try:
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            new_text + ("\n" if not new_text.endswith("\n") else ""),
            encoding="utf-8", newline="\n",
        )
        os.replace(tmp, target)
    except OSError as e:
        logger.exception(
            "project_claude_md: atomic write failed for %s", target,
        )
        await _emit(
            "claude_md_update_failed",
            project_id=project_id, source=source,
            reason=f"write_failed: {e}",
        )
        await _emit(
            "human_attention",
            subject=f"CLAUDE.md update failed for project {name}",
            body=f"Atomic write failed: {e}",
            urgency="normal",
        )
        return "failed:write"

    final_text = target.read_text(encoding="utf-8")
    await _mirror_to_kdrive(project_id, final_text)
    await _set_stored_hash(project_id, new_hash)

    bytes_after = len(final_text.encode("utf-8"))
    lines_after = _line_count(final_text)
    await _emit(
        "claude_md_update_completed",
        project_id=project_id, source=source,
        lines_before=lines_before, lines_after=lines_after,
        bytes_before=bytes_before, bytes_after=bytes_after,
    )
    return "completed"


__all__ = [
    "canonical_project_claude_md_template",
    "update_claude_md_via_coach",
]
