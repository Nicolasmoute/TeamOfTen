"""Measure render_playbook_block() output composition.

Run on a deployed harness with HARNESS_DATA_ROOT pointing at the
container's /data volume (or rsync the lattice.json off-box and
point this at a local mirror).

Reports:
  - active statement count + buckets
  - per-statement char distribution (avg / p50 / p95 / max)
  - rendered block total bytes
  - scaffolding overhead (heading + intro + bucket labels + meta line)
  - estimated bytes per statement (incl. weight prefix)
  - flag the longest N statements over the brevity cap

Usage:
    HARNESS_DATA_ROOT=/path/to/data python scripts/analyze_playbook.py [project_id]

If project_id is omitted, the script walks every project found
under <DATA_ROOT>/projects/ and reports each in turn.
"""

from __future__ import annotations

import os
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from server.playbook.store import Lattice  # noqa: E402
from server.playbook.render import (  # noqa: E402
    _BUCKETS,
    _bucket_for,
    _render_full,
)
from server.playbook import config as pb_config  # noqa: E402

STATEMENT_MAX_CHARS = int(os.environ.get("HARNESS_PLAYBOOK_STATEMENT_MAX_CHARS", "160"))


def _summarize_one(project_id: str, lattice: Lattice) -> None:
    print(f"\n=== Project: {project_id} ===")
    n = len(lattice.statements)
    if n == 0:
        print("  (lattice empty — nothing rendered)")
        return

    print(f"  Active statements: {n}")
    by_bucket: dict[int, list] = {i: [] for i in range(len(_BUCKETS))}
    for s in lattice.statements:
        by_bucket[_bucket_for(s.weight)].append(s)
    for i, (label, _, _) in enumerate(_BUCKETS):
        items = by_bucket[i]
        if items:
            avg_w = statistics.mean(s.weight for s in items)
            print(
                f"    [{i}] {label}: {len(items)} statements "
                f"(avg weight={avg_w:.2f})"
            )

    lengths = [len(s.text) for s in lattice.statements]
    print(f"  Statement char distribution:")
    print(f"    min={min(lengths)}  avg={sum(lengths)/len(lengths):.0f}  "
          f"max={max(lengths)}  total={sum(lengths)}")
    if len(lengths) >= 2:
        srt = sorted(lengths)
        p50 = srt[len(srt) // 2]
        p95 = srt[int(len(srt) * 0.95)]
        print(f"    p50={p50}  p95={p95}")

    over_cap = [(len(s.text), s.id, s.text[:60]) for s in lattice.statements
                if len(s.text) > STATEMENT_MAX_CHARS]
    if over_cap:
        over_cap.sort(reverse=True)
        print(f"  OVER brevity cap ({STATEMENT_MAX_CHARS} chars): {len(over_cap)} rows")
        for chars, sid, preview in over_cap[:10]:
            print(f"    {chars:>4}  {sid}  {preview}…")
    else:
        print(f"  All statements within brevity cap ({STATEMENT_MAX_CHARS} chars).")

    rendered = _render_full(lattice)
    rendered_bytes = len(rendered.encode("utf-8"))

    body_bytes = sum(len(s.text.encode("utf-8")) for s in lattice.statements)
    # Each line: `- [w.ww] <text>\n` = 9 chars overhead per statement
    per_stmt_overhead = 9 * n
    scaffolding = rendered_bytes - body_bytes - per_stmt_overhead

    print(f"  Rendered block: {rendered_bytes} bytes "
          f"(cap RENDER_MAX_BYTES={pb_config.RENDER_MAX_BYTES})")
    print(f"    statement bodies:       {body_bytes:>6} bytes")
    print(f"    per-statement overhead: {per_stmt_overhead:>6} bytes  ({n} × `- [w.ww] `)")
    print(f"    scaffolding:            {scaffolding:>6} bytes  "
          f"(heading + intro + bucket labels + meta)")


def main(argv: list[str]) -> int:
    data_root_str = os.environ.get("HARNESS_DATA_ROOT")
    if not data_root_str:
        print("HARNESS_DATA_ROOT not set — point it at the deployed /data root.",
              file=sys.stderr)
        return 2
    data_root = Path(data_root_str)
    if not data_root.is_dir():
        print(f"DATA_ROOT not found: {data_root}", file=sys.stderr)
        return 2

    if len(argv) > 1:
        targets = [argv[1]]
    else:
        projects_dir = data_root / "projects"
        if not projects_dir.is_dir():
            print(f"No projects dir at {projects_dir}", file=sys.stderr)
            return 2
        targets = sorted(p.name for p in projects_dir.iterdir() if p.is_dir())
        if not targets:
            print("No projects found.")
            return 0

    for pid in targets:
        # The store module reads from the *active* project. We monkey-patch
        # by setting the active project at the path layer instead, but
        # load_lattice() reads via project_paths(). Simplest path: import
        # the loader and call it explicitly per pid.
        from server.paths import project_paths  # noqa: PLC0415

        pp = project_paths(pid)
        lattice_path = pp.playbook / "lattice.json"
        if not lattice_path.is_file():
            print(f"\n=== Project: {pid} ===")
            print(f"  (no lattice.json at {lattice_path})")
            continue
        try:
            # Direct load to bypass the active-project resolver
            import json
            from server.playbook.store import Statement
            raw = json.loads(lattice_path.read_text(encoding="utf-8"))
            stmts = [
                Statement(
                    id=s["id"],
                    text=s["text"],
                    weight=float(s.get("weight", 0.5)),
                    weight_history=[],
                    created_at=s.get("created_at", ""),
                    created_by=s.get("created_by", ""),
                    last_validated_at=s.get("last_validated_at"),
                    applied_count=int(s.get("applied_count", 0)),
                    immutable=bool(s.get("immutable", False)),
                )
                for s in raw.get("statements", [])
                if not s.get("archived", False)
            ]
            # Lattice fields vary by version; load_lattice handles this
            # normally but we're side-loading here. Construct with what's
            # supported.
            try:
                lattice = Lattice(
                    statements=stmts,
                    updated_at=raw.get("updated_at"),
                )
            except TypeError:
                lattice = Lattice(statements=stmts)
        except Exception as e:
            print(f"\n=== Project: {pid} ===")
            print(f"  ERROR loading {lattice_path}: {e}")
            continue
        _summarize_one(pid, lattice)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
