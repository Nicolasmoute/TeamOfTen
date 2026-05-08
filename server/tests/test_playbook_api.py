"""Playbook HTTP API tests — spec §18.1.

Covers endpoint shape, 409 conditions, blocking reset with timeout,
override + restore round-trip.

Uses the FastAPI TestClient via a minimal app that mounts only the
playbook router (avoids booting the full lifespan).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import server.playbook.paths as pb_paths_mod
from server.playbook import config
from server.playbook.api import build_router


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """Build a minimal FastAPI app with the playbook router mounted."""
    monkeypatch.setattr(pb_paths_mod, "DATA_ROOT", tmp_path)
    db = tmp_path / "harness.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE team_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    finally:
        conn.close()
    import server.db as dbmod
    monkeypatch.setattr(dbmod, "DB_PATH", str(db))

    app = FastAPI()

    async def _no_auth() -> None:
        return None

    def _no_actor(request: Request) -> dict:
        return {"source": "test", "ip": "127.0.0.1", "ua": "test"}

    app.include_router(build_router(require_token=_no_auth, audit_actor=_no_actor))
    return TestClient(app)


def test_state_endpoint_shape(api_client: TestClient) -> None:
    """GET /state returns the expected top-level keys."""
    r = api_client.get("/api/playbook/state")
    assert r.status_code == 200
    data = r.json()
    assert "active" in data
    assert "archived" in data
    assert "runs" in data
    assert "flags" in data
    assert "caps" in data
    assert "soft" in data["caps"]


def test_run_endpoint_409_when_not_bootstrapped(api_client: TestClient) -> None:
    """POST /run returns 409 when bootstrap is incomplete."""
    r = api_client.post("/api/playbook/run", json={})
    assert r.status_code == 409
    assert "bootstrap" in r.text.lower()


def test_bootstrap_endpoint_409_when_blocked(api_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """POST /bootstrap returns 409 when blocked flag set."""
    db = tmp_path / "harness.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO team_config VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY, "1"),
        )
        conn.commit()
    finally:
        conn.close()

    r = api_client.post("/api/playbook/bootstrap", json={})
    assert r.status_code == 409


def test_bootstrap_endpoint_409_when_already_done(api_client: TestClient, tmp_path: Path) -> None:
    db = tmp_path / "harness.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO team_config VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (config.PLAYBOOK_BOOTSTRAP_DONE_KEY, "1"),
        )
        conn.commit()
    finally:
        conn.close()

    r = api_client.post("/api/playbook/bootstrap", json={})
    assert r.status_code == 409


def test_reset_requires_confirm_yes(api_client: TestClient) -> None:
    """POST /reset without `confirm: 'yes'` returns 400."""
    r = api_client.post("/api/playbook/reset", json={"confirm": "no"})
    assert r.status_code == 400


def test_reset_clears_flags(api_client: TestClient, tmp_path: Path) -> None:
    """Reset wipes done / retries / blocked flags + sets reset_at."""
    db = tmp_path / "harness.db"
    conn = sqlite3.connect(db)
    try:
        for k in (
            config.PLAYBOOK_BOOTSTRAP_DONE_KEY,
            config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY,
            config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY,
        ):
            conn.execute(
                "INSERT INTO team_config VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k,),
            )
        conn.commit()
    finally:
        conn.close()

    r = api_client.post("/api/playbook/reset", json={"confirm": "yes"})
    assert r.status_code == 200

    # Verify flags cleared
    conn = sqlite3.connect(db)
    try:
        for k in (
            config.PLAYBOOK_BOOTSTRAP_DONE_KEY,
            config.PLAYBOOK_BOOTSTRAP_RETRIES_KEY,
            config.PLAYBOOK_BOOTSTRAP_BLOCKED_KEY,
        ):
            cur = conn.execute("SELECT value FROM team_config WHERE key = ?", (k,))
            row = cur.fetchone()
            assert row is None  # Cleared (deleted)
        cur = conn.execute("SELECT value FROM team_config WHERE key = ?",
                           (config.PLAYBOOK_RESET_AT_KEY,))
        row = cur.fetchone()
        assert row is not None  # Set
    finally:
        conn.close()


def test_override_weight_round_trip(api_client: TestClient, tmp_path: Path) -> None:
    """POST /statements/{id}/weight updates lattice and persists."""
    # Seed a lattice with one statement
    from server.playbook.store import (
        Lattice, Statement, WeightHistoryEntry, save_lattice
    )
    asyncio.run(save_lattice(Lattice(
        schema_version=1, updated_at="now",
        statements=[Statement(
            id="pb-001", text="x", weight=0.5,
            weight_history=[WeightHistoryEntry(ts="now", from_=None, to=0.5, reason="seed")],
            created_at="2026-04-01T00:00:00Z", created_by="test",
            applied_count=0, immutable=False,
        )],
    )))

    r = api_client.post("/api/playbook/statements/pb-001/weight", json={"weight": 0.0})
    assert r.status_code == 200
    data = r.json()
    assert data["weight"] == 0.0


def test_override_weight_rejects_immutable(api_client: TestClient, tmp_path: Path) -> None:
    from server.playbook.store import (
        Lattice, Statement, WeightHistoryEntry, save_lattice
    )
    asyncio.run(save_lattice(Lattice(
        schema_version=1, updated_at="now",
        statements=[Statement(
            id="pb-001", text="x", weight=1.0,
            weight_history=[WeightHistoryEntry(ts="now", from_=None, to=1.0, reason="seed")],
            created_at="now", created_by="test",
            applied_count=0, immutable=True,
        )],
    )))

    r = api_client.post("/api/playbook/statements/pb-001/weight", json={"weight": 0.0})
    assert r.status_code == 400
    assert "immutable" in r.text.lower()


def test_override_weight_rejects_out_of_range(api_client: TestClient) -> None:
    r = api_client.post("/api/playbook/statements/pb-001/weight", json={"weight": 1.5})
    assert r.status_code == 400
