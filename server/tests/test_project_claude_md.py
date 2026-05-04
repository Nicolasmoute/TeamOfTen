"""Tests for the canonical project CLAUDE.md template + the
Coach-driven reconciliation flow.

The module under test is `server.project_claude_md`:
- `canonical_project_claude_md_template(...)` reads the canonical
  template body from `server/templates/app_dev_claude_md.md` and
  substitutes {name}/{slug}/{repo} placeholders.
- `update_claude_md_via_coach(project_id, source)` runs a hidden
  Coach-identity LLM one-shot that reconciles the project's
  CLAUDE.md with the latest canonical template. Hash-gated, lock-
  serialised, validation-failure-escalating.

The LLM call is monkeypatched to a stub so the test suite stays
hermetic — no claude CLI subprocess, no network, no /data scribbles.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.db import init_db
from server.events import bus
from server.paths import project_paths
from server.project_claude_md import (
    _hash,
    _template_hash_key,
    _validate_output,
    canonical_project_claude_md_template,
    update_claude_md_via_coach,
)


# ---------------------------------------------------------------- helpers


class _SubBus:
    """Context manager that subscribes to the bus on enter and drains
    everything on exit. The captured list is mutated in-place so the
    test body can assert on it after the `async with` block ends."""

    def __init__(self, timeout: float = 0.5) -> None:
        self.events: list[dict[str, Any]] = []
        self._timeout = timeout
        self._queue: asyncio.Queue[dict[str, Any]] | None = None

    async def __aenter__(self) -> "_SubBus":
        self._queue = bus.subscribe()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        assert self._queue is not None
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(
                        self._queue.get(), timeout=self._timeout,
                    )
                    self.events.append(ev)
                except asyncio.TimeoutError:
                    break
        finally:
            bus.unsubscribe(self._queue)


# Convenience: a stub LLM result class with the same shape the real
# `compass.llm.CompassLLMResult` exposes (only the fields our module
# reads). Built locally so the tests don't import the SDK.
class _StubResult:
    def __init__(
        self,
        text: str = "",
        is_error: bool = False,
        errors: list[str] | None = None,
    ) -> None:
        self.text = text
        self.is_error = is_error
        self.errors = errors or []
        self.cost_usd = 0.0
        self.duration_ms = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.session_id = None
        self.stop_reason = None


def _patch_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str = "",
    is_error: bool = False,
    errors: list[str] | None = None,
    raise_exc: BaseException | None = None,
) -> list[dict[str, Any]]:
    """Patch `compass.llm.call` to return a canned result. Returns a
    list that captures every call's kwargs so tests can assert on the
    prompts."""
    calls: list[dict[str, Any]] = []

    async def _fake_call(
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        model: str | None = None,
        project_id: str | None = None,
        label: str = "compass",
    ) -> Any:
        calls.append({
            "system": system, "user": user, "model": model,
            "project_id": project_id, "label": label,
        })
        if raise_exc is not None:
            raise raise_exc
        return _StubResult(text=text, is_error=is_error, errors=errors)

    import server.compass.llm as llm_mod
    monkeypatch.setattr(llm_mod, "call", _fake_call)
    return calls


# A minimum-viable canonical-template-output that passes validation
# (>200 bytes, starts with #).
_VALID_OUTPUT = (
    "# Project: misc\n\n"
    + ("Filler paragraph that exists to push the body well past "
       "the 200-byte minimum so validation passes. ") * 3
)


# ---------------------------------------------------------------- canonical template


def test_canonical_template_substitutes_placeholders() -> None:
    """The canonical template is read fresh from the templates dir and
    {name}/{slug}/{repo} placeholders are replaced. Assert on a few
    distinctive substrings from the file."""
    body = canonical_project_claude_md_template(
        name="Acme", slug="acme", repo_url="https://example.com/acme",
    )
    assert "# Project: Acme" in body
    # The canonical template references {slug} in the truth/ section.
    assert "/data/projects/acme/" in body
    assert "https://example.com/acme" in body


def test_canonical_template_default_repo_placeholder() -> None:
    """Repo URL defaults to a clear placeholder when not supplied."""
    body = canonical_project_claude_md_template(name="Misc", slug="misc")
    assert "<no repo configured>" in body


def test_canonical_template_includes_kanban_lifecycle_section() -> None:
    """After folding the kanban paragraph into the canonical template,
    every freshly-rendered body should carry the kanban surface so
    Coach + Players see the lifecycle every turn."""
    body = canonical_project_claude_md_template(name="Misc", slug="misc")
    assert "Task lifecycle (kanban)" in body
    assert "plan -> execute -> audit_syntax" in body
    # Strict role boundaries section.
    assert "Coach** plans" in body
    assert "Players** execute, review, and ship" in body
    # Self-audit fallback when no audit stage in trajectory.
    assert "Self-audit when the trajectory has no audit stage" in body


def test_canonical_template_missing_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the on-disk template file disappears (renamed, deleted),
    the helper falls back to a minimal skeleton instead of crashing."""
    import server.project_claude_md as mod
    monkeypatch.setattr(mod, "_read_template", lambda _name: "")
    body = mod.canonical_project_claude_md_template(
        name="Acme", slug="acme",
    )
    assert "# Project: Acme" in body
    # Fallback skeleton mentions project-objectives.md.
    assert "project-objectives.md" in body


# ---------------------------------------------------------------- validation


def test_validate_output_accepts_canonical_body() -> None:
    ok, why = _validate_output(_VALID_OUTPUT)
    assert ok, f"expected ok, got: {why}"


def test_validate_output_rejects_empty() -> None:
    ok, why = _validate_output("")
    assert not ok and "empty" in why


def test_validate_output_rejects_too_short() -> None:
    ok, why = _validate_output("# Tiny")
    assert not ok and "too short" in why


def test_validate_output_rejects_no_heading() -> None:
    body = "Plain paragraph that does not start with a markdown heading. " * 10
    ok, why = _validate_output(body)
    assert not ok and "heading" in why


# ---------------------------------------------------------------- update flow


async def test_update_succeeds_writes_file_and_records_hash(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end happy path: template differs from stored hash, LLM
    returns a valid body, file is written, hash is recorded, events
    fire (started + completed)."""
    await init_db()
    _patch_llm(monkeypatch, text=_VALID_OUTPUT)

    # Wipe any pre-seeded CLAUDE.md so we exercise the 'current is
    # missing' path (init_db's project_claude_md_stub may have written
    # one).
    pp = project_paths("misc")
    if pp.claude_md.exists():
        pp.claude_md.unlink()

    async with _SubBus() as sub:
        status = await update_claude_md_via_coach("misc", source="test")
    assert status == "completed"

    # File written.
    assert pp.claude_md.exists()
    assert pp.claude_md.read_text(encoding="utf-8").startswith("# Project: misc")

    # Hash recorded in team_config. The seeded `misc` project has
    # name="Misc" (capital M); the stored hash matches a render
    # against that exact name + slug.
    from server.db import configured_conn
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?",
            (_template_hash_key("misc"),),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is not None
    expected_hash = _hash(canonical_project_claude_md_template(
        name="Misc", slug="misc",
    ))
    assert dict(row)["value"] == expected_hash

    types = [e.get("type") for e in sub.events]
    assert "claude_md_update_started" in types
    assert "claude_md_update_completed" in types


async def test_update_skips_when_hash_unchanged(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second run with the same canonical template hash short-
    circuits — no LLM call, no file write, single skip event."""
    await init_db()
    calls = _patch_llm(monkeypatch, text=_VALID_OUTPUT)

    # First run records the hash.
    status1 = await update_claude_md_via_coach("misc", source="test")
    assert status1 == "completed"
    assert len(calls) == 1

    # Capture mtime + content for tamper-detection.
    pp = project_paths("misc")
    pre_text = pp.claude_md.read_text(encoding="utf-8")

    # Second run — hash matches, should skip without calling LLM.
    async with _SubBus() as sub:
        status2 = await update_claude_md_via_coach("misc", source="test")
    assert status2 == "skipped:unchanged"
    assert len(calls) == 1, "LLM should NOT be re-invoked"

    post_text = pp.claude_md.read_text(encoding="utf-8")
    assert post_text == pre_text, "file should not change on hash hit"

    skip_evs = [
        e for e in sub.events
        if e.get("type") == "claude_md_update_skipped"
    ]
    assert any(e.get("reason") == "unchanged" for e in skip_evs)


async def test_update_skips_missing_or_archived_project(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A project_id that doesn't exist in the projects table (or is
    archived) is a silent skip — no events, no errors."""
    await init_db()
    calls = _patch_llm(monkeypatch, text=_VALID_OUTPUT)

    status = await update_claude_md_via_coach(
        "definitely-not-a-real-project", source="test",
    )
    assert status == "skipped:missing_or_archived"
    assert calls == []


async def test_update_validation_failure_emits_human_attention(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coach returns a body too short to be a real CLAUDE.md →
    `claude_md_update_failed` AND `human_attention` both fire,
    file is left untouched, hash is NOT recorded."""
    await init_db()
    _patch_llm(monkeypatch, text="# Hi\n")  # under 200 bytes

    pp = project_paths("misc")
    pre_existing = "# Misc — hand-written\n\nKept verbatim.\n"
    pp.claude_md.parent.mkdir(parents=True, exist_ok=True)
    pp.claude_md.write_text(pre_existing, encoding="utf-8", newline="\n")

    async with _SubBus() as sub:
        status = await update_claude_md_via_coach("misc", source="test")
    assert status.startswith("failed:")

    # File unchanged.
    assert pp.claude_md.read_text(encoding="utf-8") == pre_existing

    # Hash NOT recorded.
    from server.db import configured_conn
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?",
            (_template_hash_key("misc"),),
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    assert row is None, "hash must NOT be recorded on validation fail"

    types = [e.get("type") for e in sub.events]
    assert "claude_md_update_failed" in types
    assert "human_attention" in types


async def test_update_llm_is_error_emits_human_attention(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`is_error=True` on the LLM result is an explicit Coach-side
    failure — `claude_md_update_failed` + `human_attention`, no
    write, no hash."""
    await init_db()
    _patch_llm(
        monkeypatch, text=_VALID_OUTPUT, is_error=True,
        errors=["upstream model refused"],
    )

    pp = project_paths("misc")
    if pp.claude_md.exists():
        pp.claude_md.unlink()

    async with _SubBus() as sub:
        status = await update_claude_md_via_coach("misc", source="test")
    assert status == "failed:llm_error"
    assert not pp.claude_md.exists()

    types = [e.get("type") for e in sub.events]
    assert "claude_md_update_failed" in types
    assert "human_attention" in types


async def test_update_llm_exception_emits_human_attention(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raised CompassLLMError surfaces via `failed:llm` +
    `human_attention`, no file write, no hash."""
    from server.compass.llm import CompassLLMError
    await init_db()
    _patch_llm(monkeypatch, raise_exc=CompassLLMError("subprocess died"))

    async with _SubBus() as sub:
        status = await update_claude_md_via_coach("misc", source="test")
    assert status == "failed:llm"

    types = [e.get("type") for e in sub.events]
    assert "claude_md_update_failed" in types
    assert "human_attention" in types


async def test_update_uses_coach_role_default_model(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconciliation turn runs on Coach's role-default model
    (typically `latest_opus`). Asserted by capturing the model arg
    passed to compass.llm.call."""
    await init_db()
    calls = _patch_llm(monkeypatch, text=_VALID_OUTPUT)

    status = await update_claude_md_via_coach("misc", source="test")
    assert status == "completed"
    assert len(calls) == 1
    # `latest_opus` (alias) is what role_default_model returns for
    # coach + claude. compass.llm runs the alias through
    # resolve_model_alias internally; we just check the alias landed.
    assert calls[0]["model"] == "latest_opus"
    assert calls[0]["label"] == "claude_md_update"


async def test_update_per_project_lock_serialises(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent calls for the same project run sequentially
    under the per-project asyncio.Lock. The first call records the
    hash; the second observes the recorded hash and skips."""
    await init_db()
    calls = _patch_llm(monkeypatch, text=_VALID_OUTPUT)

    a, b = await asyncio.gather(
        update_claude_md_via_coach("misc", source="test"),
        update_claude_md_via_coach("misc", source="test"),
    )
    # One completed, one skipped — order non-deterministic but the
    # set must be exactly these two outcomes.
    assert {a, b} == {"completed", "skipped:unchanged"}
    assert len(calls) == 1


async def test_update_failure_does_not_release_hash(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validation failure leaves no hash — so the next attempt
    retries (eventual self-heal once Coach is on a better day)."""
    await init_db()
    _patch_llm(monkeypatch, text="# bad\n")

    status1 = await update_claude_md_via_coach("misc", source="test")
    assert status1.startswith("failed:")

    # Now re-patch with a valid body and retry — should succeed.
    _patch_llm(monkeypatch, text=_VALID_OUTPUT)
    status2 = await update_claude_md_via_coach("misc", source="test")
    assert status2 == "completed"


# ---------------------------------------------------------------- write_project_claude_md_stub


def test_write_project_claude_md_stub_uses_canonical_template(
    fresh_db: str,
) -> None:
    """`paths.write_project_claude_md_stub` reads the canonical
    template helper and seeds the new file with the canonical body
    (including the kanban lifecycle section)."""
    from server.paths import write_project_claude_md_stub

    # No pre-existing file.
    pp = project_paths("misc")
    if pp.claude_md.exists():
        pp.claude_md.unlink()

    wrote = write_project_claude_md_stub(
        "misc", name="Misc", repo_url="https://example.com/misc",
    )
    assert wrote is True
    text = pp.claude_md.read_text(encoding="utf-8")
    assert "# Project: Misc" in text
    assert "https://example.com/misc" in text
    # Canonical template fold — kanban paragraph must land too.
    assert "Task lifecycle (kanban)" in text


def test_write_project_claude_md_stub_first_write_only(
    fresh_db: str,
) -> None:
    """Re-running the stub-writer is a no-op when the file already
    exists — preserves Coach edits across re-runs of init_db."""
    from server.paths import write_project_claude_md_stub

    pp = project_paths("misc")
    pp.claude_md.parent.mkdir(parents=True, exist_ok=True)
    pp.claude_md.write_text("# already here\n", encoding="utf-8")

    wrote = write_project_claude_md_stub("misc", name="Misc")
    assert wrote is False
    assert pp.claude_md.read_text(encoding="utf-8") == "# already here\n"
