"""Tests for the Compass audit-report markdown writer.

The writer produces a standalone `.md` file alongside the structured
`audits.jsonl` entry every time `audit_work` (or any caller) wants
to persist an audit. Tests cover:

  - the markdown body shape (verdict label, contradicting statements
    inlined verbatim, message to coach, archived statement fallback)
  - the file lands at the expected path under the project's compass
    working dir
  - atomic write semantics (no leftover .tmp siblings)
  - `compass_audit_report_path` shape matches the data-harness-path
    expectation

We exercise `write_audit_report_md` directly — bypassing the LLM call
in `audit_work` keeps the tests fast and deterministic.
"""

from __future__ import annotations

from server.compass.audit import (
    _VERDICT_LABELS,
    _format_audit_report,
    write_audit_report_md,
)
from server.compass.paths import compass_paths
from server.compass.store import AuditRecord, LatticeState, Statement
from server.db import init_db


def _record(
    *,
    id: str = "audit_1700000000000",
    verdict: str = "confident_drift",
    summary: str = "Header sticky behavior was removed.",
    contradicting_ids: list[str] | None = None,
    message_to_coach: str = "Restore sticky-on-mobile behavior.",
    artifact: str = "commit 8a3f2c: header refactor",
    question_id: str | None = None,
) -> AuditRecord:
    return AuditRecord(
        id=id,
        ts="2026-05-03T14:22:11+00:00",
        artifact=artifact,
        verdict=verdict,
        summary=summary,
        contradicting_ids=contradicting_ids or [],
        message_to_coach=message_to_coach,
        question_id=question_id,
    )


def _state(statements: list[Statement] | None = None) -> LatticeState:
    """Minimal LatticeState for formatter tests."""
    return LatticeState(
        project_id="misc",
        statements=statements or [],
    )


def _statement(
    *, id: str, text: str, weight: float = 0.85, region: str = "ux"
) -> Statement:
    return Statement(
        id=id,
        text=text,
        weight=weight,
        region=region,
        created_at="2026-04-01T00:00:00Z",
        created_by="human",
    )


# -------- formatter shape --------

def test_format_audit_report_includes_verdict_label() -> None:
    out = _format_audit_report(record=_record(), state=_state())
    assert "# Audit audit_1700000000000" in out
    assert _VERDICT_LABELS["confident_drift"] in out
    assert "Header sticky behavior was removed." in out


def test_format_audit_report_inlines_contradicting_statements() -> None:
    state = _state([
        _statement(
            id="s-0042",
            text="All header layout work must preserve the sticky behavior on mobile.",
            weight=0.91,
        ),
        _statement(id="s-0107", text="Other rule.", weight=0.85),
    ])
    out = _format_audit_report(
        record=_record(contradicting_ids=["s-0042", "s-0107"]),
        state=state,
    )
    assert "s-0042" in out
    assert "0.91" in out
    assert (
        "All header layout work must preserve the sticky behavior on mobile."
        in out
    )
    assert "s-0107" in out


def test_format_audit_report_handles_missing_statement() -> None:
    """A contradicting_id that no longer exists on the lattice still
    appears in the report — with a `(no longer in lattice)` note —
    rather than being dropped silently."""
    out = _format_audit_report(
        record=_record(contradicting_ids=["s-9999"]),
        state=_state(),  # empty lattice
    )
    assert "s-9999" in out
    assert "no longer in lattice" in out


def test_format_audit_report_artifact_block() -> None:
    """The audited artifact is fenced so its own markdown headings
    don't bleed into the report's structure."""
    out = _format_audit_report(
        record=_record(artifact="## My fake heading\nbody"),
        state=_state(),
    )
    assert "```\n## My fake heading\nbody\n```" in out


def test_format_audit_report_aligned_no_message() -> None:
    """An aligned audit doesn't necessarily have a message_to_coach.
    The report should render cleanly without that section instead of
    showing an empty header."""
    out = _format_audit_report(
        record=_record(verdict="aligned", message_to_coach=""),
        state=_state(),
    )
    assert "## Message to Coach" not in out
    # Verdict label is the human-readable one.
    assert _VERDICT_LABELS["aligned"] in out


def test_format_audit_report_question_id_surfaced() -> None:
    out = _format_audit_report(
        record=_record(verdict="uncertain_drift", question_id="q-42"),
        state=_state(),
    )
    assert "Question queued" in out
    assert "q-42" in out


# -------- write_audit_report_md --------

async def test_write_audit_report_md_writes_file(fresh_db: str) -> None:
    await init_db()
    state = _state([_statement(id="s-1", text="Some rule.", weight=0.8)])
    rec = _record(id="audit_1700000123456", contradicting_ids=["s-1"])
    paths = await write_audit_report_md("misc", rec, state)
    assert paths is not None
    local_rel, remote_rel = paths

    # File exists at the expected absolute path.
    cp = compass_paths("misc")
    target = cp.audit_report_for(rec.id)
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "# Audit audit_1700000123456" in text
    assert "Some rule." in text

    # Relative path is `data-harness-path`-compatible (forward slashes,
    # working/compass/ segment present).
    assert local_rel == (
        "projects/misc/working/compass/audit_reports/audit_1700000123456.md"
    )
    # Remote path follows the kDrive convention (no `working/` segment).
    assert remote_rel == (
        "projects/misc/compass/audit_reports/audit_1700000123456.md"
    )


async def test_write_audit_report_md_atomic_no_tmp_left(fresh_db: str) -> None:
    await init_db()
    rec = _record(id="audit_1700000999999")
    paths = await write_audit_report_md("misc", rec, _state())
    assert paths is not None
    cp = compass_paths("misc")
    siblings = list(cp.audit_reports_dir.iterdir())
    assert all(not p.name.endswith(".tmp") for p in siblings), (
        f"unexpected tmp files: {siblings}"
    )


async def test_write_audit_report_md_overwrites_same_id(fresh_db: str) -> None:
    """If an audit_id collides (impossible in practice given ms
    timestamps but defensible), the second write replaces the first
    rather than appending — atomic semantics."""
    await init_db()
    rec1 = _record(id="audit_dupe", verdict="aligned", summary="first")
    rec2 = _record(id="audit_dupe", verdict="confident_drift", summary="second")
    await write_audit_report_md("misc", rec1, _state())
    await write_audit_report_md("misc", rec2, _state())
    cp = compass_paths("misc")
    text = cp.audit_report_for("audit_dupe").read_text(encoding="utf-8")
    assert "second" in text
    assert "first" not in text


async def test_audit_reports_dir_in_compass_paths(fresh_db: str) -> None:
    """CompassPaths.audit_reports_dir is a real Path under the compass
    root (sibling of audits.jsonl, briefings/, proposals/)."""
    await init_db()
    cp = compass_paths("misc")
    assert cp.audit_reports_dir.parent == cp.root
    assert cp.audit_reports_dir.name == "audit_reports"
    # Filename for a given audit_id matches the .md convention.
    p = cp.audit_report_for("audit_xyz")
    assert p.name == "audit_xyz.md"
    assert p.parent == cp.audit_reports_dir
