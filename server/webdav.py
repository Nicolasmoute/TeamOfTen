"""WebDAV mirror for durable, human-readable content.

Design:
- Hot state lives in the local SQLite on the /data volume.
- Human-readable content (memory docs, decisions, digests, event log
  rotations) gets mirrored to a WebDAV cloud drive, where you can
  read/edit it from your phone or browser outside the harness.
- Fire-and-forget semantics: a WebDAV hiccup must never block an agent
  tool call. Failures are logged and the local DB is still authoritative.

Works with any WebDAV-compatible service — Infomaniak kDrive,
Nextcloud, ownCloud, Fastmail Files, etc. Point it at the folder you
want the harness to own; it works with any provider that speaks plain
WebDAV. If your provider needs more sophisticated auth (OAuth, S3-
compatible, etc.), fork and adapt — the interface is ~10 methods.

Config (all three required — WebDAV mirror stays disabled unless all
are set):
  HARNESS_WEBDAV_URL        full path to the folder the app owns,
                            e.g. https://<host>/remote.php/dav/files/<user>/TOT
                            (kDrive: https://<drive-id>.connect.kdrive.infomaniak.com/TOT)
  HARNESS_WEBDAV_USER       your WebDAV username / email
  HARNESS_WEBDAV_PASSWORD   app-specific password

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

logger = logging.getLogger("harness.webdav")
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


WEBDAV_URL = os.environ.get("HARNESS_WEBDAV_URL", "").strip()
WEBDAV_USER = os.environ.get("HARNESS_WEBDAV_USER", "").strip()
WEBDAV_PASS = os.environ.get("HARNESS_WEBDAV_PASSWORD", "").strip()


class WebDAVClient:
    """Thin WebDAV wrapper. All public methods are async and swallow
    their own errors — the harness continues if the mirror is down."""

    def __init__(self) -> None:
        self._client: Any = None
        self._enabled = False
        self._reason: str = ""

        missing: list[str] = []
        if not WEBDAV_URL:
            missing.append("HARNESS_WEBDAV_URL")
        if not WEBDAV_USER:
            missing.append("HARNESS_WEBDAV_USER")
        if not WEBDAV_PASS:
            missing.append("HARNESS_WEBDAV_PASSWORD")
        if missing:
            self._reason = f"missing env: {', '.join(missing)}"
            logger.info("WebDAV mirror disabled: %s", self._reason)
            return
        if _WebDAVClient is None:
            self._reason = "webdav4 not installed"
            logger.warning("WebDAV mirror disabled: %s", self._reason)
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
                "WebDAV mirror enabled (url=%s user=%s)", base_url, WEBDAV_USER,
            )
        except Exception:
            self._reason = "client init failed"
            logger.exception("WebDAV init failed")

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
        # the URL doesn't exist on the server; a 401/403 means auth; a
        # connection error means URL hostname / scheme is wrong.
        try:
            entries = await asyncio.to_thread(self._list_dir_sync, "")
        except Exception as e:
            return {
                "ok": False,
                "url": WEBDAV_URL,
                "step": "list",
                "error": f"{type(e).__name__}: {str(e)[:400]}",
                "hint": "Auth, URL, or target folder is wrong. Verify HARNESS_WEBDAV_URL points at an existing folder on the server and the credentials are for this account.",
            }
        # Step 2: write. If listing worked but PUT fails we're looking
        # at a share-level permission issue, a filename the server rejects,
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
        """Upload `content` as UTF-8 text to `{HARNESS_WEBDAV_URL}/{relative_path}`.

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
            logger.exception("WebDAV write failed: %s", full_path)
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
            logger.exception("WebDAV write_bytes failed: %s", full_path)
            return False

    async def write_bytes_atomic(self, relative_path: str, data: bytes) -> bool:
        """Atomic remote write per PROJECTS_SPEC.md §5: upload to a
        sibling temp file, then MOVE it onto the final path so a
        partial write is never visible to readers / cloud clients.

        Falls back to a non-atomic PUT if the underlying webdav4 client
        doesn't expose a `move` (older versions / alternate libs).
        Never raises — returns False on any failure.
        """
        if not self._enabled:
            return False
        full_path = self._resolve(relative_path)
        try:
            await asyncio.to_thread(self._write_bytes_atomic_sync, full_path, data)
            return True
        except Exception:
            logger.exception("WebDAV write_bytes_atomic failed: %s", full_path)
            # Best-effort fallback to a non-atomic write so a single bad
            # MOVE doesn't strand the file. The caller's retry wrapper
            # still gates the call so a flaky kDrive eventually surfaces
            # via kdrive_sync_failed.
            try:
                await asyncio.to_thread(self._write_bytes_sync, full_path, data)
                return True
            except Exception:
                logger.exception(
                    "WebDAV atomic-write fallback PUT also failed: %s", full_path
                )
                return False

    async def read_text(self, relative_path: str) -> str | None:
        """Download a UTF-8 text file from `{HARNESS_WEBDAV_URL}/{relative_path}`.
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
            logger.exception("WebDAV read_text failed: %s", full_path)
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
            logger.exception("WebDAV read_bytes failed: %s", full_path)
            return None

    async def list_dir(self, relative_path: str) -> list[str]:
        """List filenames (basenames, not full paths) under
        `{HARNESS_WEBDAV_URL}/{relative_path}`. Returns [] on any failure
        or if the directory is missing — callers shouldn't have to catch."""
        if not self._enabled:
            return []
        full_path = self._resolve(relative_path)
        try:
            return await asyncio.to_thread(self._list_dir_sync, full_path)
        except Exception:
            logger.exception("WebDAV list_dir failed: %s", full_path)
            return []

    async def walk_files(self, relative_path: str) -> list[str]:
        """Recursive PROPFIND of `{HARNESS_WEBDAV_URL}/{relative_path}`
        returning every file path beneath it as a posix-style relative
        path (relative to `relative_path` itself).

        Uses webdav4's `detail=True` so we know which entries are files
        vs directories — no extension heuristic. Returns `[]` on missing
        directory or any error. Used by the Phase 3 pull-on-open flow to
        enumerate remote files reliably regardless of whether sub-folders
        contain dots or files lack extensions.
        """
        if not self._enabled:
            return []
        full_path = self._resolve(relative_path)
        try:
            return await asyncio.to_thread(self._walk_files_sync, full_path)
        except Exception:
            logger.exception("WebDAV walk_files failed: %s", full_path)
            return []

    async def remove(self, relative_path: str) -> bool:
        """Delete a single file at `{HARNESS_WEBDAV_URL}/{relative_path}`.

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
            logger.exception("WebDAV remove failed: %s", full_path)
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

    def _write_bytes_atomic_sync(self, full_path: str, data: bytes) -> None:
        """Spec §5: PUT to `<path>.tmp.<uuid>`, then MOVE onto `<path>`.
        webdav4 exposes `move(from, to)` — if missing, raise so the
        async wrapper falls back to a non-atomic write."""
        import uuid as _uuid

        move = getattr(self._client, "move", None)
        if not callable(move):
            raise RuntimeError("webdav client has no move(); fallback to PUT")
        parent = str(PurePosixPath(full_path).parent).lstrip("/")
        if parent and parent != ".":
            self._ensure_dir_sync(parent)
        leaf = PurePosixPath(full_path).name
        tmp_full = (
            f"{parent}/{leaf}.tmp.{_uuid.uuid4().hex[:8]}"
            if parent and parent != "."
            else f"{leaf}.tmp.{_uuid.uuid4().hex[:8]}"
        )
        self._client.upload_fileobj(
            io.BytesIO(data),
            tmp_full,
            overwrite=True,
        )
        try:
            move(tmp_full, full_path, overwrite=True)
        except Exception:
            # Best-effort cleanup of the orphan temp.
            try:
                self._client.remove(tmp_full)
            except Exception:
                pass
            raise

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

    def _walk_files_sync(self, full_path: str) -> list[str]:
        """Recursive walk via repeated `ls(detail=True)` calls.
        webdav4 returns dicts with `name` + `type` (file|directory).
        Returns posix-style paths relative to `full_path`.
        """
        out: list[str] = []
        # Stack holds (full_remote_dir, prefix_relative_to_root).
        stack: list[tuple[str, str]] = [(full_path.rstrip("/"), "")]
        while stack:
            sub_full, sub_rel = stack.pop()
            try:
                entries = self._client.ls(sub_full, detail=True)
            except Exception as e:
                if _ResourceNotFound is not None and isinstance(e, _ResourceNotFound):
                    continue
                # Non-fatal: log and keep walking siblings.
                logger.warning("walk_files: ls failed at %s: %s", sub_full, e)
                continue
            for entry in entries:
                # webdav4 yields dicts with at least `name` (full path
                # on the server, e.g. `/TOT/projects/misc/foo.md`) and
                # `type` ('file' | 'directory'). Some servers return
                # the path with the host stripped already; either way
                # the basename is the leaf.
                if not isinstance(entry, dict):
                    continue
                etype = entry.get("type") or entry.get("kind") or ""
                ename = entry.get("name") or entry.get("href") or ""
                leaf = PurePosixPath(str(ename)).name
                if not leaf:
                    continue
                # Skip the directory's self-entry (PROPFIND returns the
                # collection itself when depth=1).
                child_full = (
                    f"{sub_full}/{leaf}" if sub_full else leaf
                )
                if child_full.rstrip("/") == sub_full.rstrip("/"):
                    continue
                child_rel = (
                    f"{sub_rel}/{leaf}" if sub_rel else leaf
                )
                if etype == "directory" or etype == "collection":
                    stack.append((child_full, child_rel))
                else:
                    out.append(child_rel)
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


webdav = WebDAVClient()
