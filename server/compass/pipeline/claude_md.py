"""CLAUDE.md compass block — render and inject into the project file.

The block lives between the markers
`<!-- compass:start -->` and `<!-- compass:end -->` (spec §3.10).
Everything between is rewritten on each run; everything outside is
preserved so Coach's hand-edits to the rest of CLAUDE.md aren't
clobbered. If markers are absent, the block is appended at end of
file.

A copy of the rendered block is also persisted to
`compass/claude_md_block.md` for traceability — the human dashboard
reads this so it can show the exact text Compass would write.

The injection is async because it also mirrors to kDrive — each
project's CLAUDE.md lives at `/data/projects/<id>/CLAUDE.md` locally
and `projects/<id>/CLAUDE.md` on kDrive (per `server.paths`).
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import PurePosixPath

from server.compass import config, llm, prompts
from server.compass.store import LatticeState, write_claude_md_block
from server.paths import project_paths
from server.webdav import webdav

logger = logging.getLogger("harness.compass.claude_md")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


async def generate(state: LatticeState) -> str:
    """Produce the markdown body that goes between the markers
    (NOT including the markers themselves)."""
    if not state.active_statements() and not state.archived_statements():
        return _placeholder_block()
    res = await llm.call(
        prompts.CLAUDE_MD_BLOCK_SYSTEM,
        prompts.claude_md_block_user(state),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:claude_md",
    )
    body = (res.text or "").strip()
    if not body:
        return _placeholder_block()
    return body


def _placeholder_block() -> str:
    return (
        "## Compass\n\n"
        "Compass is the project's strategy engine. It maintains a lattice of "
        "weighted statements, asks the human focused questions, and exposes its "
        "current best guess to Coach via `compass_ask`. Workers read this section "
        "for direction; only the human answers questions and edits truth.\n\n"
        "### Where we stand · next steps\n\n"
        "_Lattice is empty so far. Open the Compass dashboard and run a "
        "bootstrap — Compass will start asking._\n"
    )


async def inject(project_id: str, block_body: str) -> bool:
    """Replace the marker-delimited region of the project's CLAUDE.md.

    Idempotent: re-running with the same body produces no change to
    the rest of the file. If markers are missing, appends the block at
    end-of-file. Persists a copy under `compass/claude_md_block.md`
    via `store.write_claude_md_block`.

    Returns True on success. False on local-write failure (logs and
    moves on — kDrive mirror is best-effort).
    """
    pp = project_paths(project_id)
    target = pp.claude_md
    start = config.CLAUDE_MD_START_MARKER
    end = config.CLAUDE_MD_END_MARKER
    full_block = f"{start}\n{block_body.rstrip()}\n{end}"

    # Persist the block-only copy first; even if the project CLAUDE.md
    # write fails (rare — disk full, perms), the dashboard view stays
    # correct.
    await write_claude_md_block(project_id, full_block + "\n")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("compass.claude_md.inject: mkdir failed: %s", target.parent)
        return False

    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)

    if not target.exists():
        # No CLAUDE.md yet — write the block as the entire file.
        try:
            target.write_text(full_block + "\n", encoding="utf-8", newline="\n")
        except OSError:
            logger.exception("compass.claude_md.inject: initial write failed: %s", target)
            return False
        await _mirror_to_kdrive(project_id, full_block + "\n")
        return True

    try:
        existing = target.read_text(encoding="utf-8")
    except OSError:
        logger.exception("compass.claude_md.inject: read failed: %s", target)
        return False

    if pattern.search(existing):
        new_content = pattern.sub(full_block, existing)
    else:
        # Append — leave a blank line of separation before the block.
        new_content = existing.rstrip() + "\n\n" + full_block + "\n"

    if new_content == existing:
        return True  # idempotent no-op

    try:
        target.write_text(new_content, encoding="utf-8", newline="\n")
    except OSError:
        logger.exception("compass.claude_md.inject: write failed: %s", target)
        return False

    await _mirror_to_kdrive(project_id, new_content)
    return True


async def _mirror_to_kdrive(project_id: str, content: str) -> None:
    """Mirror the project CLAUDE.md to kDrive at `projects/<id>/CLAUDE.md`.

    Best-effort: failure logs but doesn't propagate. The local
    project_sync loop also covers this path; explicit mirror here
    keeps the post-injection view consistent without waiting for the
    next sync tick.
    """
    if not webdav.enabled:
        return
    remote = str(PurePosixPath("projects") / project_id / "CLAUDE.md")
    try:
        await webdav.write_text(remote, content)
    except Exception:
        logger.exception("compass.claude_md.inject: kDrive mirror failed: %s", remote)
