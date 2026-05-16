"""Regression tests for the top-level WebDAV uploads/ inbound lane."""

from __future__ import annotations

from pathlib import Path

import pytest

import server.sync as sync


class _StubWebDAV:
    enabled = True

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.ensured: list[str] = []
        self.listed: list[str] = []
        self.reads: list[str] = []

    async def ensure_dir(self, rel: str) -> bool:
        self.ensured.append(rel)
        assert rel == "uploads"
        return True

    async def list_dir(self, rel: str) -> list[str]:
        self.listed.append(rel)
        assert rel == "uploads"
        return list(self.files.keys())

    async def read_bytes(self, rel: str) -> bytes | None:
        self.reads.append(rel)
        assert rel.startswith("uploads/")
        return self.files.get(Path(rel).name)


@pytest.mark.asyncio
async def test_pull_uploads_once_uses_top_level_webdav_uploads(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local = tmp_path / "local-uploads"
    local.mkdir()
    (local / "stale.pdf").write_bytes(b"old")

    stub = _StubWebDAV(
        {
            "brief.pdf": b"brief bytes",
            "screen.png": b"png bytes",
        }
    )
    monkeypatch.setattr(sync, "webdav", stub)
    monkeypatch.setattr(sync, "UPLOADS_LOCAL_DIR", local)

    out = await sync.pull_uploads_once()

    assert out == {"added": 2, "removed": 1, "kept": 0}
    assert stub.ensured == ["uploads"]
    assert stub.listed == ["uploads"]
    assert sorted(stub.reads) == ["uploads/brief.pdf", "uploads/screen.png"]
    assert (local / "brief.pdf").read_bytes() == b"brief bytes"
    assert (local / "screen.png").read_bytes() == b"png bytes"
    assert not (local / "stale.pdf").exists()
