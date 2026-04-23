"""kDrive WebDAV mirror for durable, human-readable content.

Design:
- Hot state lives in the local SQLite on the /data volume.
- Human-readable content (memory docs, decisions, digests, event log
  rotations) gets mirrored to Infomaniak kDrive over WebDAV, where you
  can read/edit it from your phone or browser outside the harness.
- Fire-and-forget semantics: a WebDAV hiccup must never block an agent
  tool call. Failures are logged and the local DB is still authoritative.

Config (all required — kDrive stays disabled unless all are set):
  KDRIVE_WEBDAV_URL      full path to the folder the app owns on kDrive,
                         e.g. https://<drive-id>.connect.kdrive.infomaniak.com/TOT
  KDRIVE_USER            your Infomaniak email
  KDRIVE_APP_PASSWORD    app-specific password generated in Infomaniak UI

All files live directly under the URL — no extra prefix. If you want
a sub-folder, include it in the URL itself.
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
                "kDrive enabled (url=%s user=%s)", base_url, WEBDAV_USER,
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

    async def probe(self) -> dict[str, Any]:
        """Two-step health check:
          1. PROPFIND the base URL (lists the target folder) — proves
             auth + URL are correct and the folder exists.
          2. PUT a small visible file — proves write permission.

        Reports which step failed so the operator doesn't have to guess
        between "wrong credentials", "wrong URL", "folder missing", and
        "read-only share". Always returns a detail dict even when
        disabled — callers render the reason in the UI.
        """
        if not self._enabled:
            return {"ok": False, "error": self._reason, "url": WEBDAV_URL}
        # Step 1: list the configured folder. "" means "the base URL
        # itself". A ResourceNotFound here means the folder path in
        # the URL doesn't exist on kDrive; a 401/403 means auth; a
        # connection error means URL hostname / scheme is wrong.
        try:
            entries = await asyncio.to_thread(self._list_dir_sync, "")
        except Exception as e:
            return {
                "ok": False,
                "url": WEBDAV_URL,
                "step": "list",
                "error": f"{type(e).__name__}: {str(e)[:400]}",
                "hint": "Auth, URL, or target folder is wrong. Verify KDRIVE_WEBDAV_URL points at an existing kDrive folder and the app-password is for this account.",
            }
        # Step 2: write. If listing worked but PUT fails we're looking
        # at a share-level permission issue, a filename kDrive rejects,
        # or an upload-protocol quirk (some WebDAV servers 409 fresh
        # PUTs sent with chunked transfer-encoding).
        rel = "harness-health-probe.txt"
        full_path = self._resolve(rel)
        try:
            await asyncio.to_thread(self._write_sync, full_path, "ok")
            return {
                "ok": True,
                "url": WEBDAV_URL,
                "probe_file": full_path,
                "existing_entries": len(entries),
            }
        except Exception as e:
            # webdav4 HTTPError carries the underlying httpx.Response
            # on .response — pull the status + body text so the
            # operator sees what the server actually said, not just
            # the generic "received 409".
            err_txt = f"{type(e).__name__}: {str(e)[:400]}"
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    body = resp.text[:400] if resp.text else "(empty body)"
                except Exception:
                    body = "(body unreadable)"
                err_txt = (
                    f"{type(e).__name__}: HTTP {resp.status_code} {resp.reason_phrase}\n"
                    f"body: {body}"
                )
            return {
                "ok": False,
                "url": WEBDAV_URL,
                "step": "write",
                "probe_file": full_path,
                "existing_entries": len(entries),
                "error": err_txt,
                "hint": "Folder exists and listing works, but PUT was rejected. Check that the Infomaniak app-password has WRITE permission (not read-only). If the body below mentions 'Precondition' or 'locked', the URL may point at a read-only share.",
            }

    # ---------- async public API ----------

    def _resolve(self, relative_path: str) -> str:
        """Strip any leading slash from the caller's path so webdav4
        resolves it relative to the base URL.

        webdav4 (and httpx underneath) treats paths containing a
        leading `/` as host-root-relative, bypassing any path
        component in the base URL. Keep everything relative so the
        URL is the single source of truth for where files live.
        """
        return relative_path.lstrip("/")

    async def write_text(self, relative_path: str, content: str) -> bool:
        """Upload `content` as UTF-8 text to `{KDRIVE_WEBDAV_URL}/{relative_path}`.

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
        """Download a UTF-8 text file from `{KDRIVE_WEBDAV_URL}/{relative_path}`.
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

    async def read_bytes(self, relative_path: str) -> bytes | None:
        """Same as read_text but returns raw bytes — use for binary
        downloads (pdf, docx, images). Returns None on missing / error."""
        if not self._enabled:
            return None
        full_path = self._resolve(relative_path)
        try:
            return await asyncio.to_thread(self._read_bytes_sync, full_path)
        except Exception as e:
            if _ResourceNotFound is not None and isinstance(e, _ResourceNotFound):
                return None
            logger.exception("kDrive read_bytes failed: %s", full_path)
            return None

    async def list_dir(self, relative_path: str) -> list[str]:
        """List filenames (basenames, not full paths) under
        `{KDRIVE_WEBDAV_URL}/{relative_path}`. Returns [] on any failure
        or if the directory is missing — callers shouldn't have to catch."""
        if not self._enabled:
            return []
        full_path = self._resolve(relative_path)
        try:
            return await asyncio.to_thread(self._list_dir_sync, full_path)
        except Exception:
            logger.exception("kDrive list_dir failed: %s", full_path)
            return []

    async def remove(self, relative_path: str) -> bool:
        """Delete a single file at `{KDRIVE_WEBDAV_URL}/{relative_path}`.

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

    def _read_bytes_sync(self, full_path: str) -> bytes:
        buf = io.BytesIO()
        self._client.download_fileobj(full_path, buf)
        return buf.getvalue()

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
