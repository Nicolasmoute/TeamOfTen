"""Tests for the retention trim jobs in server/sync.py.

Covers:
  - trim_events_once: respects cutoff, honors disabled=0,
    no-op on empty / all-recent.
  - trim_attachments_once: deletes old files by mtime,
    skips fresh, respects disabled=0.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import server.sync as syncmod
from server.db import configured_conn, init_db


# ---------- events retention ----------


async def _insert_event(ts_iso: str, agent_id: str = "p1", type_: str = "text") -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO events (ts, agent_id, type, payload) VALUES (?, ?, ?, '{}')",
            (ts_iso, agent_id, type_),
        )
        await c.commit()
    finally:
        await c.close()


async def _count_events() -> int:
    c = await configured_conn()
    try:
        cur = await c.execute("SELECT COUNT(*) AS n FROM events")
        return int(dict(await cur.fetchone())["n"])
    finally:
        await c.close()


async def test_trim_disabled_is_noop(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    await _insert_event(old)
    monkeypatch.setattr(syncmod, "EVENTS_RETENTION_DAYS", 0)
    assert await syncmod.trim_events_once() == 0
    assert await _count_events() == 1


async def test_trim_deletes_only_rows_older_than_cutoff(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await _insert_event(old, agent_id="coach")
    await _insert_event(old, agent_id="p1")
    await _insert_event(fresh, agent_id="p1")
    monkeypatch.setattr(syncmod, "EVENTS_RETENTION_DAYS", 30)
    deleted = await syncmod.trim_events_once()
    assert deleted == 2
    assert await _count_events() == 1


async def test_trim_empty_table(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await init_db()
    monkeypatch.setattr(syncmod, "EVENTS_RETENTION_DAYS", 30)
    assert await syncmod.trim_events_once() == 0


# ---------- attachments retention ----------


@pytest.fixture
def tmp_att(monkeypatch: pytest.MonkeyPatch) -> Path:
    d = Path(tempfile.mkdtemp(prefix="harness-att-"))
    monkeypatch.setenv("HARNESS_ATTACHMENTS_DIR", str(d))
    return d


async def test_attachment_trim_disabled(
    tmp_att: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(syncmod, "ATTACHMENTS_RETENTION_DAYS", 0)
    (tmp_att / "old.png").write_bytes(b"x")
    # Force the mtime way into the past.
    old_ts = time.time() - 400 * 86400
    os.utime(tmp_att / "old.png", (old_ts, old_ts))
    assert await syncmod.trim_attachments_once() == 0
    assert (tmp_att / "old.png").exists()


async def test_attachment_trim_deletes_old_keeps_fresh(
    tmp_att: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(syncmod, "ATTACHMENTS_RETENTION_DAYS", 30)
    old_ts = time.time() - 45 * 86400
    fresh_ts = time.time() - 1 * 86400
    (tmp_att / "old1.png").write_bytes(b"x")
    (tmp_att / "old2.jpg").write_bytes(b"y")
    (tmp_att / "fresh.png").write_bytes(b"z")
    for name in ("old1.png", "old2.jpg"):
        os.utime(tmp_att / name, (old_ts, old_ts))
    os.utime(tmp_att / "fresh.png", (fresh_ts, fresh_ts))
    assert await syncmod.trim_attachments_once() == 2
    assert not (tmp_att / "old1.png").exists()
    assert not (tmp_att / "old2.jpg").exists()
    assert (tmp_att / "fresh.png").exists()


async def test_attachment_trim_missing_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Trim must not raise when the directory doesn't exist yet
    # (fresh deploy with no attachments ever uploaded).
    monkeypatch.setenv("HARNESS_ATTACHMENTS_DIR", "/tmp/harness-att-does-not-exist-xyz")
    monkeypatch.setattr(syncmod, "ATTACHMENTS_RETENTION_DAYS", 30)
    assert await syncmod.trim_attachments_once() == 0
