"""Tests for server.tasks — spec.md + audit-report writers."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.tasks import (
    audit_report_filename,
    audit_report_path,
    audit_report_relative_path,
    is_valid_task_id,
    kdrive_audit_path,
    kdrive_spec_path,
    read_task_spec,
    spec_path,
    spec_relative_path,
    task_dir,
    write_audit_report,
    write_task_spec,
)


# -------- shape validators --------

def test_is_valid_task_id_accepts_canonical_shape() -> None:
    assert is_valid_task_id("t-2026-05-03-abc12345")
    assert is_valid_task_id("t-2026-12-31-deadbeef")


def test_is_valid_task_id_rejects_traversal_attempts() -> None:
    # Path traversal attempts must be rejected so a malicious task_id
    # can't be used to escape the project's working/tasks/ folder.
    assert not is_valid_task_id("t-../etc/passwd")
    assert not is_valid_task_id("../foo")
    assert not is_valid_task_id("t-2026-05-03-abc12345/../bar")
    assert not is_valid_task_id("")
    assert not is_valid_task_id("t-2026-5-3-abc12345")  # zero-pad missing
    assert not is_valid_task_id("T-2026-05-03-ABC12345")  # uppercase rejected
    assert not is_valid_task_id("t-2026-05-03-abc1234")  # 7 hex chars
    assert not is_valid_task_id("t-2026-05-03-abc123456")  # 9 hex chars


def test_task_dir_rejects_invalid_id() -> None:
    with pytest.raises(ValueError):
        task_dir("misc", "../escape")


def test_audit_report_filename_validates_kind_and_round() -> None:
    assert audit_report_filename(1, "syntax") == "audit_1_syntax.md"
    assert audit_report_filename(3, "semantics") == "audit_3_semantics.md"
    with pytest.raises(ValueError):
        audit_report_filename(1, "garbage")
    with pytest.raises(ValueError):
        audit_report_filename(0, "syntax")


# -------- relative path conventions (used as DB column values) --------

def test_spec_relative_path_uses_forward_slashes() -> None:
    rel = spec_relative_path("misc", "t-2026-05-03-abc12345")
    assert "/" in rel
    assert "\\" not in rel
    assert rel == "projects/misc/working/tasks/t-2026-05-03-abc12345/spec.md"


def test_audit_relative_path_round_kind_in_filename() -> None:
    rel = audit_report_relative_path("misc", "t-2026-05-03-abc12345", 2, "syntax")
    assert rel == (
        "projects/misc/working/tasks/t-2026-05-03-abc12345/audits/"
        "audit_2_syntax.md"
    )


def test_kdrive_paths_omit_working_segment() -> None:
    """kDrive mirror lives under projects/<id>/tasks/... — flatter than
    the local layout so mobile browsing isn't bogged down by the
    `working/` prefix."""
    assert kdrive_spec_path("misc", "t-2026-05-03-abc12345") == (
        "projects/misc/tasks/t-2026-05-03-abc12345/spec.md"
    )
    assert kdrive_audit_path("misc", "t-2026-05-03-abc12345", 1, "semantics") == (
        "projects/misc/tasks/t-2026-05-03-abc12345/audits/audit_1_semantics.md"
    )


# -------- write_task_spec --------

async def test_write_task_spec_creates_file_with_frontmatter(fresh_db: str) -> None:
    target, rel, written_at = await write_task_spec(
        project_id="misc",
        task_id="t-2026-05-03-abc12345",
        title="Add dark mode toggle",
        body="## Goal\nLet the user switch themes.\n",
        author="coach",
        created_by="human",
        created_at="2026-05-03T10:00:00+00:00",
        priority="normal",
        complexity="standard",
    )
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    # Frontmatter block at the top.
    assert text.startswith("---\n")
    assert "task_id: t-2026-05-03-abc12345" in text
    assert "title: Add dark mode toggle" in text
    assert "spec_author: coach" in text
    assert f"spec_written_at: {written_at}" in text
    assert "priority: normal" in text
    assert "complexity: standard" in text
    # Body preserved verbatim after the frontmatter terminator.
    assert "## Goal\nLet the user switch themes." in text
    assert text.endswith("\n")
    # Relative path matches DB-storage convention.
    assert rel == "projects/misc/working/tasks/t-2026-05-03-abc12345/spec.md"


async def test_write_task_spec_overwrites_existing(fresh_db: str) -> None:
    """A re-spec is a full overwrite — the rolling history lives in
    the event stream + git, not in the spec.md."""
    common = dict(
        project_id="misc",
        task_id="t-2026-05-03-abc12345",
        title="Same title",
        author="coach",
        created_by="coach",
        created_at="2026-05-03T10:00:00+00:00",
        priority="normal",
        complexity="standard",
    )
    target, _, _ = await write_task_spec(body="first version", **common)
    first_text = target.read_text(encoding="utf-8")
    target2, _, _ = await write_task_spec(body="second version", **common)
    assert target == target2
    text = target.read_text(encoding="utf-8")
    assert "first version" not in text
    assert "second version" in text
    # Ensure the file is fully replaced — not appended to.
    assert text.count("---") == 2  # exactly one frontmatter block


async def test_write_task_spec_atomic_no_tmp_file_left(fresh_db: str) -> None:
    target, _, _ = await write_task_spec(
        project_id="misc",
        task_id="t-2026-05-03-abc12345",
        title="atomicity",
        body="x",
        author="coach",
        created_by="coach",
        created_at="2026-05-03T10:00:00+00:00",
        priority="normal",
        complexity="standard",
    )
    # The atomic-write helper uses a sibling tempfile + os.replace.
    # No `.tmp` siblings should remain after a successful write.
    siblings = list(target.parent.iterdir())
    assert all(not p.name.endswith(".tmp") for p in siblings), (
        f"unexpected tmp files: {siblings}"
    )


async def test_write_task_spec_rejects_invalid_task_id(fresh_db: str) -> None:
    with pytest.raises(ValueError):
        await write_task_spec(
            project_id="misc",
            task_id="../etc/passwd",
            title="evil",
            body="x",
            author="coach",
            created_by="coach",
            created_at="2026-05-03T10:00:00+00:00",
            priority="normal",
            complexity="standard",
        )


# -------- write_audit_report --------

async def test_write_audit_report_round_filename(fresh_db: str) -> None:
    target, rel, ts = await write_audit_report(
        project_id="misc",
        task_id="t-2026-05-03-abc12345",
        kind="syntax",
        round_num=1,
        body="all green\n",
        auditor="p4",
        verdict="pass",
    )
    assert target.name == "audit_1_syntax.md"
    text = target.read_text(encoding="utf-8")
    assert "audit_kind: syntax" in text
    assert "audit_round: 1" in text
    assert "auditor: p4" in text
    assert "verdict: pass" in text
    assert f"submitted_at: {ts}" in text
    assert "all green" in text
    assert rel.endswith("/audits/audit_1_syntax.md")


async def test_write_audit_report_multiple_rounds_coexist(fresh_db: str) -> None:
    """The audit history on disk is round-numbered; older rounds stay
    even after a fresh round lands. Card surfaces only the latest;
    disk has the full trail."""
    common = dict(
        project_id="misc",
        task_id="t-2026-05-03-abc12345",
        kind="semantics",
        body="x",
        auditor="p2",
    )
    t1, _, _ = await write_audit_report(round_num=1, verdict="fail", **common)
    t2, _, _ = await write_audit_report(round_num=2, verdict="pass", **common)
    assert t1.exists() and t2.exists()
    assert t1 != t2
    parent = t1.parent
    files = sorted(p.name for p in parent.iterdir())
    assert "audit_1_semantics.md" in files
    assert "audit_2_semantics.md" in files


async def test_write_audit_report_validates_kind_and_verdict(fresh_db: str) -> None:
    common = dict(
        project_id="misc",
        task_id="t-2026-05-03-abc12345",
        body="x",
        auditor="p4",
        round_num=1,
    )
    with pytest.raises(ValueError):
        await write_audit_report(kind="garbage", verdict="pass", **common)
    with pytest.raises(ValueError):
        await write_audit_report(kind="syntax", verdict="maybe", **common)


# -------- read_task_spec --------

async def test_read_task_spec_returns_none_when_missing(fresh_db: str) -> None:
    """Read failures are not propagated — callers (Coach prompt block,
    pane composer, etc.) treat missing spec as 'no spec yet'."""
    out = read_task_spec("misc", "t-2026-05-03-deadbeef")
    assert out is None


async def test_read_task_spec_round_trip(fresh_db: str) -> None:
    await write_task_spec(
        project_id="misc",
        task_id="t-2026-05-03-abc12345",
        title="round trip",
        body="hello world",
        author="coach",
        created_by="human",
        created_at="2026-05-03T10:00:00+00:00",
        priority="urgent",
        complexity="simple",
    )
    text = read_task_spec("misc", "t-2026-05-03-abc12345")
    assert text is not None
    assert "hello world" in text
    assert "priority: urgent" in text
    assert "complexity: simple" in text


async def test_path_helpers_within_project(fresh_db: str) -> None:
    """Resolved local paths should be confined to the project's
    working/tasks/<task_id>/ folder. A defensive check against
    accidental escape — separate from the regex validator."""
    pid = "misc"
    tid = "t-2026-05-03-abc12345"
    sp = spec_path(pid, tid)
    ap = audit_report_path(pid, tid, 1, "syntax")
    assert sp.parent.name == tid
    assert sp.parent.parent.name == "tasks"
    assert ap.parent.name == "audits"
    assert ap.parent.parent.name == tid
    # All paths share a common ancestor — the task's folder.
    assert task_dir(pid, tid) in sp.parents
    assert task_dir(pid, tid) in ap.parents


async def test_relative_paths_match_local_layout(fresh_db: str) -> None:
    """The relative path stored in the DB must navigate the local
    filesystem from /data/ to the actual file. The Files pane keys on
    this consistency for the single-click `data-harness-path` open."""
    # Write a spec and confirm the relative path corresponds to the
    # local file's actual /data-anchored path.
    target, rel, _ = await write_task_spec(
        project_id="misc",
        task_id="t-2026-05-03-abc12345",
        title="rel match",
        body="x",
        author="coach",
        created_by="human",
        created_at="2026-05-03T10:00:00+00:00",
        priority="normal",
        complexity="standard",
    )
    # rel is "projects/misc/working/tasks/<id>/spec.md". The local
    # absolute path's tail must end with the same segments.
    assert str(target).replace("\\", "/").endswith(rel)
