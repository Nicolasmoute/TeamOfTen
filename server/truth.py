"""Truth-proposal resolution logic — kept FastAPI-free.

The HTTP wrappers in `server/main.py` translate exceptions raised here
into HTTPException codes and own dependency-injection (auth, audit
actor). This module owns the actual proposal lifecycle: read the row,
write the file (on approve), mark the row, emit the event.

Sitting outside `main.py` keeps the test suite from having to import
FastAPI just to exercise approve/deny flows — same pattern the rest
of the test suite already follows (see comments in
`server/tests/test_bootstrap.py`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from server.db import configured_conn
from server.events import bus
from server.paths import project_paths


class TruthProposalNotFound(Exception):
    """Resolver couldn't find the proposal row."""


class TruthProposalConflict(Exception):
    """Proposal is already resolved (status != 'pending')."""

    def __init__(self, status: str) -> None:
        super().__init__(f"proposal is {status}, not pending")
        self.status = status


class TruthProposalBadRequest(Exception):
    """File-write rejection (path validation, oversize, etc)."""


def truth_proposal_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "project_id": row[1],
        "proposer_id": row[2],
        "path": row[3],
        "proposed_content": row[4],
        "summary": row[5],
        "status": row[6],
        "created_at": row[7],
        "resolved_at": row[8],
        "resolved_by": row[9],
        "resolved_note": row[10],
    }


SELECT_PROPOSAL_SQL = (
    "SELECT id, project_id, proposer_id, path, proposed_content, "
    "summary, status, created_at, resolved_at, resolved_by, "
    "resolved_note FROM truth_proposals WHERE id = ?"
)


async def resolve_truth_proposal(
    proposal_id: int,
    *,
    new_status: str,
    note: str | None,
    actor: dict[str, Any],
) -> dict[str, Any]:
    """Approve, deny, or cancel a pending truth proposal.

    On `approved`, writes the proposed content to the file BEFORE
    marking the row — so a crash mid-operation leaves the proposal
    pending instead of marking it approved without a real disk
    update. The write goes through the same path-resolution sandbox
    the Files-pane uses, plus an explicit `truth/` prefix.

    Raises:
      TruthProposalNotFound  — no row with that id.
      TruthProposalConflict  — row exists but isn't pending.
      TruthProposalBadRequest — file-write rejected (bad path, etc).
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
        raise TruthProposalNotFound(f"proposal {proposal_id} not found")
    proposal = truth_proposal_row_to_dict(row)
    if proposal["status"] != "pending":
        raise TruthProposalConflict(proposal["status"])

    if new_status == "approved":
        # Direct disk write — we deliberately bypass `files.write_text`
        # because it caps at .md/.txt + 100 KB, which is too narrow for
        # truth/ (the user wanted spec/brand/contract files which often
        # live as .json / .yaml / .toml / .csv, sometimes >100 KB). The
        # path-traversal protection comes from anchoring `target.resolve()`
        # under the project's truth/ directory and re-checking it after
        # resolution — a malformed row from a future migration can't
        # escape the lane.
        truth_root = project_paths(proposal["project_id"]).truth.resolve()
        rel_path = proposal["path"].lstrip("/")
        target = (truth_root / rel_path).resolve()
        try:
            target.relative_to(truth_root)
        except ValueError:
            raise TruthProposalBadRequest(
                "resolved target escapes truth/ — refusing write"
            )
        content = proposal["proposed_content"]
        if len(content) > 200_000:
            raise TruthProposalBadRequest(
                f"content too long ({len(content)} chars, max 200000)"
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as e:
            raise TruthProposalBadRequest(f"write failed: {e}")
        write_size = len(content.encode("utf-8"))
    else:
        write_size = 0

    now_iso = datetime.now(timezone.utc).isoformat()
    c = await configured_conn()
    try:
        await c.execute(
            "UPDATE truth_proposals SET status = ?, resolved_at = ?, "
            "resolved_by = ?, resolved_note = ? "
            "WHERE id = ? AND status = 'pending'",
            (new_status, now_iso, "human", note, proposal_id),
        )
        await c.commit()
    finally:
        await c.close()

    event_type = (
        "truth_proposal_approved" if new_status == "approved"
        else "truth_proposal_denied" if new_status == "denied"
        else "truth_proposal_cancelled"
    )
    await bus.publish(
        {
            "ts": now_iso,
            "agent_id": "human",
            "type": event_type,
            "proposal_id": proposal_id,
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
        "status": new_status,
        "size": write_size,
    }


# ---------- Manifest (truth-index.md) parsing ----------------------

# Bullet shape (matches the seeded template):
#   - `filename` — description
# Em-dash separator is preferred but we accept ASCII " - " too so users
# editing on a stripped keyboard layout aren't punished. The first
# segment must be a backtick-wrapped filename so we don't pick up
# unrelated bullets.
_MANIFEST_LINE_RE = re.compile(
    r"^\s*[-*]\s+`(?P<file>[^`]+)`\s*[—–-]\s*(?P<desc>.+?)\s*$",
    re.MULTILINE,
)
TRUTH_INDEX_FILENAME = "truth-index.md"


def parse_truth_manifest(project_id: str) -> list[dict[str, Any]]:
    """Read truth/truth-index.md and return the expected-files list.

    Each entry: `{filename, description, exists, size}`. `exists` is a
    direct stat call; `size` is bytes (None if missing). The manifest
    file itself (`truth-index.md`) is filtered out of the results so it
    doesn't list itself as expected.

    Returns [] when the manifest file is missing OR when it has no
    bullet lines that match the documented shape — both are normal
    states (an empty manifest is a valid state for a brand-new project
    where the user hasn't customized it yet, but our seeded template
    always has at least the `specs.md` bullet).
    """
    pp = project_paths(project_id)
    manifest_path = pp.truth / TRUTH_INDEX_FILENAME
    try:
        body = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for m in _MANIFEST_LINE_RE.finditer(body):
        filename = m.group("file").strip()
        if not filename or filename == TRUTH_INDEX_FILENAME:
            continue
        if filename in seen:
            continue
        seen.add(filename)
        target = pp.truth / filename
        exists = target.is_file()
        try:
            size = target.stat().st_size if exists else None
        except OSError:
            size = None
        out.append({
            "filename": filename,
            "description": m.group("desc").strip(),
            "exists": exists,
            "size": size,
            "abs_path": str(target),
        })
    return out


def create_empty_truth_file(project_id: str, rel_path: str) -> dict[str, Any]:
    """Write a zero-byte file under <project>/truth/<rel_path>.

    Refuses if the target already exists (caller maps to HTTP 409).
    Validates the resolved path stays inside truth/ (caller maps to
    400). Bypasses the Files-pane `.md`/`.txt` extension restriction
    on purpose — humans approving the manifest wrote any extension
    they wanted in `truth-index.md`, so creating it as an empty file
    is no looser than what they already accepted.
    """
    rel = (rel_path or "").lstrip("/")
    if not rel:
        raise TruthProposalBadRequest("path is required")
    # Defensive symmetry with the propose tool: accept "truth/specs.md"
    # and "specs.md" both. A bullet authored with a `truth/` prefix
    # in `truth-index.md` would otherwise land at truth/truth/<file>.
    if rel.startswith("truth/"):
        rel = rel[len("truth/"):]
    if ".." in rel.split("/"):
        raise TruthProposalBadRequest(
            "path must be relative under truth/, no '..' segments"
        )
    truth_root = project_paths(project_id).truth.resolve()
    target = (truth_root / rel).resolve()
    try:
        target.relative_to(truth_root)
    except ValueError:
        raise TruthProposalBadRequest(
            "resolved target escapes truth/ — refusing write"
        )
    if target.exists():
        raise TruthProposalConflict("exists")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    except OSError as e:
        raise TruthProposalBadRequest(f"write failed: {e}")
    return {"ok": True, "path": rel, "size": 0}
