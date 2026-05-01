"""File-write proposal resolution + truth-index helpers — kept
FastAPI-free.

The HTTP wrappers in `server/main.py` translate exceptions raised here
into HTTPException codes and own dependency-injection (auth, audit
actor). This module owns the actual proposal lifecycle: read the row,
dispatch on scope to write the right file (on approve), mark the row,
emit the event. Two scopes today: 'truth' and 'project_claude_md'
(see `coord_propose_file_write` in `server/tools.py` for the proposer
side and the `file_write_proposals` schema in `server/db.py`).

Sitting outside `main.py` keeps the test suite from having to import
FastAPI just to exercise approve/deny flows — same pattern the rest
of the test suite already follows (see comments in
`server/tests/test_bootstrap.py`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server.db import configured_conn
from server.events import bus
from server.paths import project_paths


class FileWriteProposalNotFound(Exception):
    """Resolver couldn't find the proposal row."""


class FileWriteProposalConflict(Exception):
    """Proposal is already resolved (status != 'pending')."""

    def __init__(self, status: str) -> None:
        super().__init__(f"proposal is {status}, not pending")
        self.status = status


class FileWriteProposalBadRequest(Exception):
    """File-write rejection (path validation, oversize, etc)."""


def file_write_proposal_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "project_id": row[1],
        "proposer_id": row[2],
        "scope": row[3],
        "path": row[4],
        "proposed_content": row[5],
        "summary": row[6],
        "status": row[7],
        "created_at": row[8],
        "resolved_at": row[9],
        "resolved_by": row[10],
        "resolved_note": row[11],
    }


SELECT_PROPOSAL_SQL = (
    "SELECT id, project_id, proposer_id, scope, path, proposed_content, "
    "summary, status, created_at, resolved_at, resolved_by, "
    "resolved_note FROM file_write_proposals WHERE id = ?"
)


def resolve_target_path(proposal: dict[str, Any]) -> Any:
    """Compute the on-disk write target for an approved proposal.

    Anchors and re-verifies the resolved path so a malformed row from
    a future migration (or an oddly crafted scope) can't escape its
    permitted lane.

    Raises FileWriteProposalBadRequest on any path-escape violation.
    """
    pp = project_paths(proposal["project_id"])
    scope = proposal["scope"]
    if scope == "truth":
        # Direct disk write — we deliberately bypass `files.write_text`
        # because it caps at .md/.txt + 100 KB, which is too narrow for
        # truth/ (the user wanted spec/brand/contract files which often
        # live as .json / .yaml / .toml / .csv, sometimes >100 KB). The
        # path-traversal protection comes from anchoring
        # `target.resolve()` under the project's truth/ directory and
        # re-checking it after resolution.
        truth_root = pp.truth.resolve()
        rel_path = proposal["path"].lstrip("/")
        target = (truth_root / rel_path).resolve()
        try:
            target.relative_to(truth_root)
        except ValueError:
            raise FileWriteProposalBadRequest(
                "resolved target escapes truth/ — refusing write"
            )
        return target
    if scope == "project_claude_md":
        # Path is locked to 'CLAUDE.md' at the proposer; double-check
        # here so a row whose path was tampered with post-insert can't
        # write to a sibling file (e.g. '../truth/specs.md').
        if proposal["path"] != "CLAUDE.md":
            raise FileWriteProposalBadRequest(
                f"project_claude_md proposal must have path "
                f"'CLAUDE.md' (got {proposal['path']!r}); refusing "
                "write"
            )
        return pp.claude_md
    raise FileWriteProposalBadRequest(
        f"unknown proposal scope {scope!r}; refusing write"
    )


async def resolve_file_write_proposal(
    proposal_id: int,
    *,
    new_status: str,
    note: str | None,
    actor: dict[str, Any],
) -> dict[str, Any]:
    """Approve, deny, or cancel a pending file-write proposal.

    On `approved`, writes the proposed content to the file BEFORE
    marking the row — so a crash mid-operation leaves the proposal
    pending instead of marking it approved without a real disk
    update. Scope dispatch happens inside `resolve_target_path`.

    Raises:
      FileWriteProposalNotFound  — no row with that id.
      FileWriteProposalConflict  — row exists but isn't pending.
      FileWriteProposalBadRequest — file-write rejected (bad path,
                                    oversize, unknown scope, etc).
    """
    if new_status not in ("approved", "denied", "cancelled"):
        raise ValueError(f"bad status: {new_status}")

    c = await configured_conn()
    try:
        cur = await c.execute(SELECT_PROPOSAL_SQL, (proposal_id,))
        row = await cur.fetchone()
    finally:
        await c.close()
    if not row:
        raise FileWriteProposalNotFound(
            f"proposal {proposal_id} not found"
        )
    proposal = file_write_proposal_row_to_dict(row)
    if proposal["status"] != "pending":
        raise FileWriteProposalConflict(proposal["status"])

    if new_status == "approved":
        target = resolve_target_path(proposal)
        content = proposal["proposed_content"]
        if len(content) > 200_000:
            raise FileWriteProposalBadRequest(
                f"content too long ({len(content)} chars, max 200000)"
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            raise FileWriteProposalBadRequest(f"write failed: {e}")
        write_size = len(content.encode("utf-8"))
    else:
        write_size = 0

    now_iso = datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE file_write_proposals SET status = ?, "
            "resolved_at = ?, resolved_by = ?, resolved_note = ? "
            "WHERE id = ? AND status = 'pending'",
            (new_status, now_iso, "human", note, proposal_id),
        )
        await c.commit()
    finally:
        await c.close()

    event_type = (
        "file_write_proposal_approved" if new_status == "approved"
        else "file_write_proposal_denied" if new_status == "denied"
        else "file_write_proposal_cancelled"
    )
    await bus.publish(
        {
            "ts": now_iso,
            "agent_id": "human",
            "type": event_type,
            "proposal_id": proposal_id,
            "scope": proposal["scope"],
            "path": proposal["path"],
            "summary": proposal["summary"],
            "proposer_id": proposal["proposer_id"],
            "size": write_size,
            "note": note,
            "actor": actor,
        }
    )
    return {
        "ok": True,
        "id": proposal_id,
        "scope": proposal["scope"],
        "status": new_status,
        "size": write_size,
    }


