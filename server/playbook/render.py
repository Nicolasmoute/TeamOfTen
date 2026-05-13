"""Render the active lattice into a system-prompt markdown block.

Called from [server/context.py:build_system_prompt_suffix](../context.py)
on every Coach turn (Coach-only — Players don't get the playbook in
their prompt; their coordination cues come from Coach's per-stage
wake notes). Sync function (no I/O beyond the lattice file read) so
the async caller can invoke it without ceremony — same pattern
`_read_text_safe()` uses for CLAUDE.md.

Output shape per spec §6.2: full self-contained markdown including the
`## Orchestration playbook` heading + intro + four weight-bucketed
groups + closing meta line. Returns the empty string when:
  - lattice has zero active statements
  - `lattice.json` is missing on disk
  - `team_config['playbook_disabled']` is set

Caller treats empty string as "skip the section entirely" — no header
without body.

Size budget enforcement (spec §6.2): if rendered output exceeds
`RENDER_MAX_BYTES` (default 8 KB), drop the "Uncertain" bucket from
the rendered block. Coach can still see those statements in the
dashboard; the rendered playbook is for actionable patterns.

Bucket sort: within each weight bucket, statements are ordered by
`weight × log(1 + applied_count)` descending — frequently-observed
high-confidence rules surface above rarely-observed ones (spec §13.1).
"""

from __future__ import annotations

import logging
import math
import sys
from typing import Iterable

from server.playbook import config
from server.playbook.store import Lattice, Statement, load_lattice

logger = logging.getLogger("harness.playbook.render")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# Weight bucket boundaries (spec §1.2 / §6.2). Inclusive of lower bound.
_BUCKETS = [
    ("Validated (weight ≥ 0.85)", 0.85, 1.01),
    ("Working hypotheses (0.65 ≤ weight < 0.85)", 0.65, 0.85),
    ("Uncertain (0.35 ≤ weight < 0.65)", 0.35, 0.65),
    ("Anti-patterns (weight < 0.35)", 0.0, 0.35),
]


# Cached `playbook_disabled` flag. The query is read by every Coach
# turn during system-prompt assembly via the async caller in
# build_system_prompt_suffix; the underlying sync sqlite3.connect()
# blocks the event loop briefly each time. The flag itself is a rarely-
# toggled operator setting (no runtime mutation API), so a short TTL
# is the right shape — long enough to eliminate the per-turn cost,
# short enough that a manual DB edit takes effect within seconds.
_DISABLED_CACHE: dict[str, float | bool] = {"value": False, "expires_at": 0.0}
_DISABLED_TTL_S = 10.0


def _is_disabled() -> bool:
    """Read `team_config['playbook_disabled']` via direct sync sqlite3.

    Sync I/O so this module stays callable from `build_system_prompt_suffix`
    without forcing async. Mirrors the pattern of `_read_text_safe()` for
    CLAUDE.md (sync file read inside an async caller). Errors → return
    False (fail-open: render the playbook rather than silently hide it).

    Result cached for `_DISABLED_TTL_S` seconds to skip the SQLite open
    on every Coach turn.
    """
    import sqlite3
    import time

    now = time.monotonic()
    if now < float(_DISABLED_CACHE["expires_at"]):
        return bool(_DISABLED_CACHE["value"])

    try:
        from server.db import DB_PATH  # noqa: PLC0415
    except ImportError:
        return False
    try:
        conn = sqlite3.connect(DB_PATH, timeout=1.0)
        try:
            cur = conn.execute(
                "SELECT value FROM team_config WHERE key = ?",
                (config.PLAYBOOK_DISABLED_KEY,),
            )
            row = cur.fetchone()
            value = bool(row and row[0] == "1")
        finally:
            conn.close()
    except Exception:
        value = False

    _DISABLED_CACHE["value"] = value
    _DISABLED_CACHE["expires_at"] = now + _DISABLED_TTL_S
    return value


def _sort_key(stmt: Statement) -> float:
    """`weight × log(1 + applied_count)` — descending sort within bucket
    means higher score first, so we negate for ascending sort.

    Edge case: a freshly-created statement with applied_count=0 yields
    score=0. To preserve weight ordering at the floor, fall back to
    weight as a tiebreak.
    """
    return -(stmt.weight * math.log1p(stmt.applied_count) + 1e-6 * stmt.weight)


def _bucket_for(weight: float) -> int:
    """Return the bucket index for a given weight."""
    for i, (_, lo, hi) in enumerate(_BUCKETS):
        if lo <= weight < hi:
            return i
    # Edge case: weight == 1.0 → goes in bucket 0 (≥ 0.85). Already
    # handled by hi=1.01 above. But weight 0.85 exactly → bucket 0
    # because of >= comparison. ✓
    return len(_BUCKETS) - 1


def _format_bucket(label: str, statements: Iterable[Statement]) -> str:
    items = list(statements)
    if not items:
        return ""
    items.sort(key=_sort_key)
    lines = [f"**{label}:**"]
    for stmt in items:
        # Spec §6.2: include the pb-id alongside the weight so Coach
        # can target an existing row via `coord_propose_playbook_changes`
        # (adjust / merge / archive ops require the id). Without it
        # Coach can only `create`, producing near-duplicate rows that
        # the dedup proposer eventually merges — wasteful churn.
        # 2026-05-12 report (pb-065/pb-066 collision).
        lines.append(f"- [{stmt.id} / {stmt.weight:.2f}] {stmt.text}")
    return "\n".join(lines)


def _render_full(lattice: Lattice, *, drop_uncertain: bool = False) -> str:
    """Render with all four buckets, or omit the Uncertain bucket
    when over the size budget."""
    parts: list[str] = []
    parts.append("## Orchestration playbook")
    parts.append("")
    parts.append(
        "Your coordination memory. Each entry has a confidence weight in "
        "[0, 1] — high = validated discipline, low = validated "
        "anti-pattern, ~0.5 = uncertain. Apply high-confidence patterns "
        "by default; deviate with explicit reason. Update this lattice "
        "mid-turn via `coord_propose_playbook_changes`; a nightly "
        "reflection run also evolves it from observed evidence. Coach-"
        "only context — Players don't see this block, so coordination "
        "discipline reaches them through the wake notes you compose at "
        "each `coord_approve_stage`."
    )
    parts.append("")

    by_bucket: dict[int, list[Statement]] = {i: [] for i in range(len(_BUCKETS))}
    for stmt in lattice.statements:
        idx = _bucket_for(stmt.weight)
        if drop_uncertain and idx == 2:  # Uncertain bucket
            continue
        by_bucket[idx].append(stmt)

    for i, (label, _, _) in enumerate(_BUCKETS):
        if drop_uncertain and i == 2:
            continue
        block = _format_bucket(label, by_bucket[i])
        if block:
            parts.append(block)
            parts.append("")

    # Closing meta line.
    n_active = len(lattice.statements)
    last_updated = lattice.updated_at or "unknown"
    parts.append(
        f"— End playbook ({n_active} statement"
        f"{'s' if n_active != 1 else ''} active, last reflected: {last_updated})"
    )

    return "\n".join(parts).strip() + "\n"


def render_playbook_block() -> str:
    """Return the rendered `## Orchestration playbook` markdown block,
    or empty string when there's nothing to render.

    Empty-string conditions (caller skips the section entirely):
      - `playbook_disabled` flag set
      - lattice file missing on disk
      - lattice has zero active statements

    Size budget: if rendered output > RENDER_MAX_BYTES, retry with the
    "Uncertain" bucket dropped. If still over budget, accept overage —
    the system-prompt cap (200 KB CLAUDE.md, plenty of headroom) won't
    be hit by Playbook alone.
    """
    if _is_disabled():
        return ""

    try:
        lattice = load_lattice()
    except FileNotFoundError:
        return ""
    except Exception:
        logger.exception("playbook.render: load_lattice raised — emitting empty block")
        return ""

    if not lattice.statements:
        return ""

    rendered = _render_full(lattice)
    if len(rendered.encode("utf-8")) <= config.RENDER_MAX_BYTES:
        return rendered

    # Over budget — try without the Uncertain bucket.
    rendered = _render_full(lattice, drop_uncertain=True)
    if len(rendered.encode("utf-8")) <= config.RENDER_MAX_BYTES:
        logger.info("playbook.render: dropped Uncertain bucket to fit size budget")
        return rendered

    # Still over budget — accept overage. Logging once per process is
    # enough; the dashboard's capacity bar already shows the count.
    logger.warning(
        "playbook.render: %d active statements exceed RENDER_MAX_BYTES "
        "(%d) even with Uncertain dropped; emitting full block",
        len(lattice.statements), config.RENDER_MAX_BYTES,
    )
    return rendered


__all__ = ["render_playbook_block"]
