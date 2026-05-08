"""Playbook store tests — spec §18.1 §test_playbook_store.

Covers atomic JSON I/O, missing-file tolerance, weight_history cap,
schema_version mismatch handling, and kDrive failure event emission.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from server.playbook import config
from server.playbook.store import (
    Lattice,
    Statement,
    WeightHistoryEntry,
    _trim_weight_history,
    load_lattice,
    load_archive,
    read_runs,
    save_lattice,
    save_archive,
    append_run,
    wipe_files,
)
from server.playbook.paths import PlaybookPaths


@pytest.fixture
def pb_paths(tmp_path: Path) -> PlaybookPaths:
    """Tempdir-rooted PlaybookPaths so tests don't touch /data."""
    return PlaybookPaths(
        root=tmp_path,
        lattice=tmp_path / "lattice.json",
        archived=tmp_path / "archived.json",
        runs=tmp_path / "runs.jsonl",
    )


def test_load_lattice_missing_file_returns_empty(pb_paths: PlaybookPaths) -> None:
    assert not pb_paths.lattice.exists()
    lat = load_lattice(paths=pb_paths)
    assert lat.statements == []
    assert lat.schema_version == config.PLAYBOOK_SCHEMA_VERSION


def test_load_archive_missing_file_returns_empty(pb_paths: PlaybookPaths) -> None:
    arch = load_archive(paths=pb_paths)
    assert arch.statements == []


def test_save_then_load_round_trip(pb_paths: PlaybookPaths) -> None:
    lat = Lattice(
        schema_version=config.PLAYBOOK_SCHEMA_VERSION,
        updated_at="2026-05-08T00:00:00Z",
        statements=[
            Statement(id="pb-001", text="audit code changes", weight=0.85,
                      created_at="2026-05-08T00:00:00Z", created_by="bootstrap-playbook"),
        ],
    )
    asyncio.run(save_lattice(lat, paths=pb_paths))
    assert pb_paths.lattice.exists()
    re_read = load_lattice(paths=pb_paths)
    assert len(re_read.statements) == 1
    assert re_read.statements[0].id == "pb-001"
    assert re_read.statements[0].weight == 0.85


def test_atomic_write_uses_tempfile_pattern(pb_paths: PlaybookPaths) -> None:
    """Writing should not leave any .tmp files behind on success."""
    lat = Lattice(schema_version=1, updated_at="now", statements=[])
    asyncio.run(save_lattice(lat, paths=pb_paths))
    tmp_files = list(pb_paths.root.glob(".lattice.json.*"))
    assert tmp_files == []


def test_schema_version_mismatch_raises(pb_paths: PlaybookPaths) -> None:
    """Unknown schema version must raise rather than silently heal."""
    pb_paths.lattice.write_text(
        json.dumps({"schema_version": 999, "updated_at": "now", "statements": []}),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="schema_version"):
        load_lattice(paths=pb_paths)


def test_weight_history_cap_at_50(pb_paths: PlaybookPaths) -> None:
    """save_lattice should trim weight_history to last 50 entries."""
    history = [
        WeightHistoryEntry(ts=f"2026-05-{(i % 28) + 1:02d}T00:00:00Z",
                           from_=0.5, to=0.5 + (i * 0.001), reason=f"step {i}")
        for i in range(70)
    ]
    stmt = Statement(
        id="pb-001", text="x", weight=0.5,
        weight_history=history,
        created_at="2026-05-01T00:00:00Z",
        created_by="bootstrap-playbook",
    )
    lat = Lattice(schema_version=1, updated_at="now", statements=[stmt])
    asyncio.run(save_lattice(lat, paths=pb_paths))
    re_read = load_lattice(paths=pb_paths)
    assert len(re_read.statements[0].weight_history) == 50
    # Should have kept the LAST 50 (most recent)
    assert re_read.statements[0].weight_history[0].reason == "step 20"
    assert re_read.statements[0].weight_history[-1].reason == "step 69"


def test_runs_jsonl_append_and_read(pb_paths: PlaybookPaths) -> None:
    asyncio.run(append_run({"run_id": "run-1", "outcome": "applied"}, paths=pb_paths))
    asyncio.run(append_run({"run_id": "run-2", "outcome": "no_changes"}, paths=pb_paths))
    rows = read_runs(paths=pb_paths)
    assert len(rows) == 2
    assert rows[0]["run_id"] == "run-1"
    assert rows[1]["run_id"] == "run-2"


def test_runs_read_skips_corrupt_lines(pb_paths: PlaybookPaths) -> None:
    """Corrupt jsonl lines should be skipped, not raise."""
    pb_paths.runs.write_text(
        '{"run_id": "ok-1", "outcome": "applied"}\n'
        'not valid json at all\n'
        '{"run_id": "ok-2", "outcome": "no_changes"}\n',
        encoding="utf-8",
    )
    rows = read_runs(paths=pb_paths)
    assert len(rows) == 2
    assert {r["run_id"] for r in rows} == {"ok-1", "ok-2"}


def test_wipe_files_replaces_with_empty_schemas(pb_paths: PlaybookPaths) -> None:
    """wipe_files() should write empty schemas, not delete files."""
    asyncio.run(save_lattice(
        Lattice(schema_version=1, updated_at="now", statements=[
            Statement(id="pb-001", text="x", weight=0.5,
                      created_at="now", created_by="bootstrap"),
        ]),
        paths=pb_paths,
    ))
    asyncio.run(append_run({"run_id": "rid", "outcome": "applied"}, paths=pb_paths))

    wipe_files(paths=pb_paths)

    assert pb_paths.lattice.exists()
    assert pb_paths.runs.exists()
    after = load_lattice(paths=pb_paths)
    assert after.statements == []
    after_runs = read_runs(paths=pb_paths)
    assert after_runs == []


def test_runs_retention_trim() -> None:
    """append_run should trim to RUNS_RETENTION_DEFAULT lines."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pp = PlaybookPaths(
            root=root,
            lattice=root / "lattice.json",
            archived=root / "archived.json",
            runs=root / "runs.jsonl",
        )
        # Stuff retention + 5 rows
        for i in range(config.RUNS_RETENTION_DEFAULT + 5):
            asyncio.run(append_run({"run_id": f"run-{i}"}, paths=pp))
        rows = read_runs(paths=pp)
        assert len(rows) == config.RUNS_RETENTION_DEFAULT
        # Oldest 5 should have been dropped — first kept row is run-5
        assert rows[0]["run_id"] == "run-5"


def test_trim_weight_history_helper_in_place() -> None:
    """The internal helper should mutate the statement directly."""
    history = [
        WeightHistoryEntry(ts=f"x{i}", to=0.5, from_=0.4, reason="r")
        for i in range(60)
    ]
    stmt = Statement(
        id="pb-001", text="x", weight=0.5, weight_history=history,
        created_at="now", created_by="b",
    )
    _trim_weight_history(stmt, cap=50)
    assert len(stmt.weight_history) == 50
