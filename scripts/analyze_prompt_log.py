#!/usr/bin/env python3
"""Analyze prompt-size logs.

Reads `<HARNESS_DATA_ROOT>/prompt_log/*.jsonl` (default `/data/`) and
prints rollups so you can see which contributor is eating the budget.

Usage:
    python scripts/analyze_prompt_log.py
    python scripts/analyze_prompt_log.py --root ./data
    python scripts/analyze_prompt_log.py --since 2026-05-09
    python scripts/analyze_prompt_log.py --agent coach
    python scripts/analyze_prompt_log.py --tail 5     # last 5 turns

Prints three rollups:
  1. Per-agent: turn count, mean total chars, p95 total chars.
  2. Per-section: mean chars across all rows + share of total.
  3. Top heaviest turns.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from collections import defaultdict
from datetime import date as _date
from pathlib import Path


def _load_rows(root: Path, since: str | None) -> list[dict]:
    log_dir = root / "prompt_log"
    if not log_dir.is_dir():
        return []
    rows: list[dict] = []
    for path in sorted(log_dir.glob("*.jsonl")):
        if since:
            stem_date = path.stem  # YYYY-MM-DD
            if stem_date < since:
                continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def _filter(rows: list[dict], agent: str | None) -> list[dict]:
    if not agent:
        return rows
    return [r for r in rows if r.get("agent_id") == agent]


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[idx]


def _per_agent(rows: list[dict]) -> None:
    by_agent: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        by_agent[r.get("agent_id", "?")].append(int(r.get("total_chars") or 0))
    print("\n=== Per-agent ===")
    print(f"{'agent':<8}{'turns':>8}{'mean':>12}{'p50':>12}{'p95':>12}{'max':>12}")
    for agent in sorted(by_agent):
        vals = by_agent[agent]
        mean = int(statistics.mean(vals)) if vals else 0
        print(
            f"{agent:<8}{len(vals):>8}{mean:>12,}"
            f"{_percentile(vals, 50):>12,}{_percentile(vals, 95):>12,}{max(vals):>12,}"
        )


def _per_section(rows: list[dict]) -> None:
    sums: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    grand_total = 0
    for r in rows:
        for k, v in (r.get("sections") or {}).items():
            sums[k] += int(v or 0)
            counts[k] += 1
        grand_total += int(r.get("total_chars") or 0)
    if not rows:
        print("\n=== Per-section ===\n(no rows)")
        return
    print(f"\n=== Per-section (avg over {len(rows)} turns) ===")
    print(f"{'section':<20}{'mean chars':>14}{'share':>10}")
    by_size = sorted(sums.items(), key=lambda kv: -kv[1])
    for name, total in by_size:
        n = counts[name] or 1
        mean = total // n
        share = 100 * total / grand_total if grand_total else 0
        print(f"{name:<20}{mean:>14,}{share:>9.1f}%")


def _top_turns(rows: list[dict], n: int) -> None:
    if not rows:
        return
    print(f"\n=== Top {n} heaviest turns ===")
    print(f"{'ts':<28}{'agent':<8}{'runtime':<8}{'total':>10}")
    top = sorted(rows, key=lambda r: -int(r.get("total_chars") or 0))[:n]
    for r in top:
        print(
            f"{r.get('ts',''):<28}{r.get('agent_id','?'):<8}"
            f"{r.get('runtime','?'):<8}{int(r.get('total_chars') or 0):>10,}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=os.environ.get("HARNESS_DATA_ROOT", "/data"))
    ap.add_argument("--since", help="ISO date, inclusive (e.g. 2026-05-09)")
    ap.add_argument("--agent", help="Filter to one agent_id (e.g. coach, p3)")
    ap.add_argument("--tail", type=int, default=5, help="Heaviest-turns count")
    args = ap.parse_args()

    rows = _filter(_load_rows(Path(args.root), args.since), args.agent)
    if not rows:
        print(f"No rows under {args.root}/prompt_log/ (since={args.since}, agent={args.agent})")
        return

    today = _date.today().isoformat()
    print(f"Analyzed {len(rows)} rows (root={args.root}, since={args.since or 'all'}, today={today})")
    _per_agent(rows)
    _per_section(rows)
    _top_turns(rows, args.tail)


if __name__ == "__main__":
    main()
