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
except Exception:  # pragma: no cover — lib missing in dev env
    _WebDAVClient = None


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

        if not (WEBDAV_URL and WEBDAV_USER and WEBDAV_PASS):
            self._reason = (
                "KDRIVE_WEBDAV_URL / KDRIVE_USER / KDRIVE_APP_PASSWORD not all set"
            )
            logger.info("kDrive disabled: %s", self._reason)
            return
        if _WebDAVClient is None:
            self._reason = "webdav4 not installed"
            logger.warning("kDrive disabled: %s", self._reason)
            return
        try:
            self._client = _WebDAVClient(WEBDAV_URL, auth=(WEBDAV_USER, WEBDAV_PASS))
            self._enabled = True
            logger.info(
                "kDrive enabled (url=%s root=%s user=%s)",
                WEBDAV_URL, ROOT_PATH, WEBDAV_USER,
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

    # ---------- async public API ----------

    async def write_text(self, relative_path: str, content: str) -> bool:
        """Upload `content` as UTF-8 text to `{ROOT_PATH}/{relative_path}`.

        Returns True on success, False on any failure (error logged).
        Never raises. Safe to call from fire-and-forget create_task().
        """
        if not self._enabled:
            return False
        full_path = str(PurePosixPath(ROOT_PATH) / relative_path)
        try:
            await asyncio.to_thread(self._write_sync, full_path, content)
            return True
        except Exception:
            logger.exception("kDrive write failed: %s", full_path)
            return False

    # ---------- sync helpers (run in a thread) ----------

    def _write_sync(self, full_path: str, content: str) -> None:
        self._ensure_dir_sync(str(PurePosixPath(full_path).parent))
        self._client.upload_fileobj(
            io.BytesIO(content.encode("utf-8")),
            full_path,
            overwrite=True,
        )

    def _ensure_dir_sync(self, path: str) -> None:
        # Walk each segment, mkdir if missing. webdav4 doesn't have a
        # recursive mkdir.
        parts = PurePosixPath(path).parts
        current = PurePosixPath("/")
        for seg in parts:
            if seg in ("", "/"):
                continue
            current = current / seg
            cur_str = str(current)
            try:
                if not self._client.exists(cur_str):
                    self._client.mkdir(cur_str)
            except Exception as e:
                # 405 Method Not Allowed often means it already exists;
                # re-raise anything else.
                msg = str(e)
                if "405" in msg or "exists" in msg.lower():
                    continue
                raise


kdrive = KDriveClient()
