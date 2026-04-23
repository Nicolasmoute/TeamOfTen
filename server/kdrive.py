"""kDrive WebDAV mirror for durable, human-readable content.

Design:
- Hot state lives in the local SQLite on the /data volume.
- Human-readable content (memory docs, decisions, digests, event log
  rotations) gets mirrored to Infomaniak kDrive over WebDAV, where you
  can read/edit it from your phone or browser outside the harness.
- Fire-and-forget semantics: a WebDAV hiccup must never block an agent
  tool call. Failures are logged and the local DB is still authoritative.

Config (all optional — kDrive stays disabled unless all are set):
  KDRIVE_WEBDAV_URL      e.g. https://connect.drive.infomaniak.com/<drive-id>
  KDRIVE_USER            your Infomaniak email
  KDRIVE_APP_PASSWORD    app-specific password generated in Infomaniak UI
  KDRIVE_ROOT_PATH       defaults to "/harness"
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import sys
from pathlib import PurePosixPath
from typing import Any

logger = logging.getLogger("harness.kdrive")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


try:
    from webdav4.client import Client as _WebDAVClient  # type: ignore
    from webdav4.client import ResourceNotFound as _ResourceNotFound  # type: ignore
except Exception:  # pragma: no cover — lib missing in dev env
    _WebDAVClient = None
    _ResourceNotFound = None  # type: ignore


WEBDAV_URL = os.environ.get("KDRIVE_WEBDAV_URL", "").strip()
WEBDAV_USER = os.environ.get("KDRIVE_USER", "").strip()
WEBDAV_PASS = os.environ.get("KDRIVE_APP_PASSWORD", "").strip()
ROOT_PATH = os.environ.get("KDRIVE_ROOT_PATH", "/harness").strip() or "/harness"


class KDriveClient:
    """Thin WebDAV wrapper. All public methods are async and swallow
    their own errors — the harness continues if kDrive is down."""

    def __init__(self) -> None:
        self._client: Any = None
        self._enabled = False
        self._reason: str = ""

        missing: list[str] = []
        if not WEBDAV_URL:
            missing.append("KDRIVE_WEBDAV_URL")
        if not WEBDAV_USER:
            missing.append("KDRIVE_USER")
        if not WEBDAV_PASS:
            missing.append("KDRIVE_APP_PASSWORD")
        if missing:
            self._reason = f"missing env: {', '.join(missing)}"
            logger.info("kDrive disabled: %s", self._reason)
            return
        if _WebDAVClient is None:
            self._reason = "webdav4 not installed"
            logger.warning("kDrive disabled: %s", self._reason)
            return
        # httpx (webdav4's HTTP layer) applies RFC 3986 URL resolution:
        # if the base URL lacks a trailing '/', the last path segment
        # is treated as a "file" and stripped when joining relatives.
        # e.g. base='https://host/TOT' + 'harness/foo' resolves to
        # 'https://host/harness/foo' — the '/TOT' silently disappears.
        # Normalize once here so operators don't have to remember.
        base_url = WEBDAV_URL if WEBDAV_URL.endswith("/") else WEBDAV_URL + "/"
        try:
            self._client = _WebDAVClient(base_url, auth=(WEBDAV_USER, WEBDAV_PASS))
            self._enabled = True
            logger.info(
                "kDrive enabled (url=%s root=%s user=%s)",
                base_url, ROOT_PATH, WEBDAV_USER,
            )
        except Exception:
            self._reason = "client init failed"
            logger.exception("kDrive init failed")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def reason(self) -> str:
        return self._reason if not self._enabled else "ok"

    @property
    def url(self) -> str:
        return WEBDAV_URL

    @property
    def root(self) -> str:
        return ROOT_PATH

    async def probe(self) -> dict[str, Any]:
        """Write a small visible-named file to the configured root so
        health checks can surface the actual exception (not just a
        bool). Filename is not dot-prefixed so operators can eyeball
        confirmation on kDrive after configuring creds.

        Always returns a detail dict even when disabled — callers
        render the reason in the UI.
        """
        if not self._enabled:
            return {"ok": False, "error": self._reason, "url": WEBDAV_URL, "root": ROOT_PATH}
        rel = "harness-health-probe.txt"
        full_path = self._resolve(rel)
        try:
            await asyncio.to_thread(self._write_sync, full_path, "ok")
            return {"ok": True, "url": WEBDAV_URL, "root": ROOT_PATH, "probe_file": full_path}
        except Exception as e:
            # Capture the full repr — different WebDAV errors carry
            # different metadata (HTTP status, URL, xml body) and the
            # operator needs the details to fix config.
            return {
                "ok": False,
                "url": WEBDAV_URL,
                "root": ROOT_PATH,
                "probe_file": full_path,
                "error": f"{type(e).__name__}: {str(e)[:400]}",
            }

    # ---------- async public API ----------

    def _resolve(self, relative_path: str) -> str:
        """Join ROOT_PATH with the caller's relative path and strip any
        leading slash.

        webdav4 resolves paths containing a leading `/` against the
        host root, bypassing any path component in the base URL — so
        if your URL is https://host/<drive-id>/TOT/ and we pass
        '/harness/foo.txt', the request goes to
        https://host/harness/foo.txt (404 / ResourceConflict) instead
        of https://host/<drive-id>/TOT/harness/foo.txt.
        """
        full = str(PurePosixPath(ROOT_PATH) / relative_path)
        return full.lstrip("/")

    async def write_text(self, relative_path: str, content: str) -> bool:
        """Upload `content` as UTF-8 text to `{ROOT_PATH}/{relative_path}`.

        Returns True on success, False on any failure (error logged).
        Never raises. Safe to call from fire-and-forget create_task().
        """
        if not self._enabled:
            return False
        full_path = self._resolve(relative_path)
        try:
            await asyncio.to_thread(self._write_sync, full_path, content)
            return True
        except Exception:
            logger.exception("kDrive write failed: %s", full_path)
            return False

    async def write_bytes(self, relative_path: str, data: bytes) -> bool:
        """Same as write_text but for binary payloads (e.g. SQLite snapshots)."""
        if not self._enabled:
            return False
        full_path = self._resolve(relative_path)
        try:
            await asyncio.to_thread(self._write_bytes_sync, full_path, data)
            return True
        except Exception:
            logger.exception("kDrive write_bytes failed: %s", full_path)
            return False

    async def read_text(self, relative_path: str) -> str | None:
        """Download a UTF-8 text file from `{ROOT_PATH}/{relative_path}`.
        Returns None if missing or on any failure (not distinguished —
        callers fall back to a local cached copy)."""
        if not self._enabled:
            return None
        full_path = self._resolve(relative_path)
        try:
            return await asyncio.to_thread(self._read_text_sync, full_path)
        except Exception as e:
            if _ResourceNotFound is not None and isinstance(e, _ResourceNotFound):
                return None
            logger.exception("kDrive read_text failed: %s", full_path)
            return None

    async def list_dir(self, relative_path: str) -> list[str]:
        """List filenames (basenames, not full paths) under
        `{ROOT_PATH}/{relative_path}`. Returns [] on any failure or if
        the directory is missing — callers shouldn't have to catch."""
        if not self._enabled:
            return []
        full_path = self._resolve(relative_path)
        try:
            return await asyncio.to_thread(self._list_dir_sync, full_path)
        except Exception:
            logger.exception("kDrive list_dir failed: %s", full_path)
            return []

    async def remove(self, relative_path: str) -> bool:
        """Delete a single file at `{ROOT_PATH}/{relative_path}`.

        Idempotent: a missing file is treated as success."""
        if not self._enabled:
            return False
        full_path = self._resolve(relative_path)
        try:
            await asyncio.to_thread(self._client.remove, full_path)
            return True
        except Exception as e:
            if _ResourceNotFound is not None and isinstance(e, _ResourceNotFound):
                return True
            logger.exception("kDrive remove failed: %s", full_path)
            return False

    # ---------- sync helpers (run in a thread) ----------

    def _write_sync(self, full_path: str, content: str) -> None:
        parent = str(PurePosixPath(full_path).parent).lstrip("/")
        if parent and parent != ".":
            self._ensure_dir_sync(parent)
        self._client.upload_fileobj(
            io.BytesIO(content.encode("utf-8")),
            full_path,
            overwrite=True,
        )

    def _write_bytes_sync(self, full_path: str, data: bytes) -> None:
        parent = str(PurePosixPath(full_path).parent).lstrip("/")
        if parent and parent != ".":
            self._ensure_dir_sync(parent)
        self._client.upload_fileobj(
            io.BytesIO(data),
            full_path,
            overwrite=True,
        )

    def _read_text_sync(self, full_path: str) -> str:
        buf = io.BytesIO()
        self._client.download_fileobj(full_path, buf)
        return buf.getvalue().decode("utf-8", errors="replace")

    def _list_dir_sync(self, full_path: str) -> list[str]:
        # ls(detail=False) returns a list of relative path strings. We
        # want basenames so callers can re-join under their own root.
        try:
            entries = self._client.ls(full_path, detail=False)
        except Exception as e:
            # Missing dir (e.g. first-ever snapshot cycle) — return
            # empty silently. Anything else propagates.
            if _ResourceNotFound is not None and isinstance(e, _ResourceNotFound):
                return []
            raise
        out: list[str] = []
        for entry in entries:
            name = PurePosixPath(str(entry)).name
            if name:
                out.append(name)
        return out

    def _ensure_dir_sync(self, path: str) -> None:
        # Walk each segment, mkdir if missing. webdav4 doesn't have a
        # recursive mkdir. Paths are relative to the base URL — a
        # leading slash would make the server interpret them as
        # host-root-relative and drop the URL's path component.
        path = path.lstrip("/")
        if not path or path == ".":
            return
        parts = PurePosixPath(path).parts
        current = ""
        for seg in parts:
            if seg in ("", "/"):
                continue
            current = f"{current}/{seg}" if current else seg
            try:
                if not self._client.exists(current):
                    self._client.mkdir(current)
            except Exception as e:
                # 405 Method Not Allowed often means it already exists;
                # re-raise anything else.
                msg = str(e)
                if "405" in msg or "exists" in msg.lower():
                    continue
                raise


kdrive = KDriveClient()
