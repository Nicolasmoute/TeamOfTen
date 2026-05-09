"""Per-turn prompt-size logger — append-only JSONL for offline analysis.

Lives at `<HARNESS_DATA_ROOT>/prompt_log/<YYYY-MM-DD>.jsonl`. One row
per agent turn spawn, recorded right after the system prompt is
assembled in `agents.run_agent`. Section breakdown mirrors the
concatenation parts so a `pandas.read_json(..., lines=True)` lands
ready to pivot.

Disable entirely via `HARNESS_PROMPT_LOG=false` (default on).

Schema per row:
    {
      "ts": ISO-8601 UTC,
      "agent_id": "coach" | "p1".."p10",
      "runtime": "claude" | "codex",
      "model": str,
      "total_chars": int,
      "sections": { "<name>": int, ... }   # chars per section
    }

Failures are swallowed — prompt logging must never break a turn.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.paths import DATA_ROOT

logger = logging.getLogger("harness.prompt_log")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


_LOG_DIR = DATA_ROOT / "prompt_log"


def _enabled() -> bool:
    raw = os.environ.get("HARNESS_PROMPT_LOG", "").strip().lower()
    if not raw:
        return True
    return raw in ("1", "true", "yes", "on")


def record(
    *,
    agent_id: str,
    runtime: str,
    model: str | None,
    sections: dict[str, int],
) -> None:
    """Append one prompt-size record. Section values are char counts.

    `sections` SHOULD include the per-contributor sizes plus a TOTAL
    matching the assembled prompt. Caller decides which sections to
    name; this module imposes no schema beyond "dict of str -> int."
    """
    if not _enabled():
        return
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        path = _LOG_DIR / f"{now.strftime('%Y-%m-%d')}.jsonl"
        total = int(sections.get("total", sum(sections.values())))
        row: dict[str, Any] = {
            "ts": now.isoformat(),
            "agent_id": agent_id,
            "runtime": runtime,
            "model": model,
            "total_chars": total,
            "sections": {k: int(v) for k, v in sections.items() if k != "total"},
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("prompt_log: record failed (non-fatal)")


__all__ = ["record"]
