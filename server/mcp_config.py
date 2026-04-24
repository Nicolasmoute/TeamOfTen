"""External MCP server configuration.

In addition to the in-process `coord` server (see server.tools), users
can plug external MCP servers into every agent via a JSON config
file — GitHub, Linear, Notion, Slack, or anything else that speaks
MCP over stdio or HTTP.

Usage:
    export HARNESS_MCP_CONFIG=/data/mcp-servers.json

Example file:
    {
      "servers": {
        "github": {
          "type": "stdio",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-github"],
          "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}
        },
        "notion": {
          "type": "http",
          "url": "https://mcp.notion.com/sse",
          "headers": {"Authorization": "Bearer ${NOTION_TOKEN}"}
        }
      },
      "allowed_tools": {
        "github": ["create_issue", "list_issues", "get_pr", "search_repositories"],
        "notion": ["search_pages", "get_page"]
      }
    }

The "allowed_tools" mapping is required — we translate it to the
fully-qualified `mcp__<server>__<tool>` names the SDK expects. We do
NOT auto-discover tools, so a user can't inadvertently expose a write
tool they didn't intend. If a server is listed in "servers" but not
in "allowed_tools", it's loaded but the agents can't call anything
on it (loaded for future expansion / listing tools).

Env-var interpolation: `${NAME}` in any string value is replaced with
os.environ[NAME] at load time so secrets don't live in the config
file itself.

Failures to read / parse the config are logged and treated as "no
external servers" — agents keep working with just the coord server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

from server import secrets as secrets_store

# Common token patterns we should NEVER let land in the DB as raw
# strings — users should use ${VAR} placeholders pulling from env. The
# UI save endpoint runs a paste against these and refuses / warns when
# one matches. Patterns are intentionally loose (prefix + reasonable
# length) so we catch more than we miss.
_SECRET_PATTERNS = [
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "GitHub personal access token"),
    (re.compile(r"\bgho_[A-Za-z0-9]{20,}"), "GitHub OAuth token"),
    (re.compile(r"\bghu_[A-Za-z0-9]{20,}"), "GitHub user token"),
    (re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"), "API key (Anthropic/OpenAI/…)"),
    (re.compile(r"\bxoxb-[A-Za-z0-9\-]{20,}"), "Slack bot token"),
    (re.compile(r"\bxoxp-[A-Za-z0-9\-]{20,}"), "Slack user token"),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}"), "Google API key"),
    # Generic bearer catchall: "Bearer <long opaque thing>". Skipped
    # when the value still contains a ${VAR} placeholder.
    (re.compile(r"Bearer\s+(?!\$\{)[A-Za-z0-9\-_\.=]{20,}"), "bearer token"),
]


def detect_secrets(text: str) -> list[str]:
    """Scan `text` for likely secrets and return a list of human-readable
    hits. Empty list = clean. Used by the save endpoint to warn before
    persisting a paste with inlined credentials."""
    out: list[str] = []
    seen: set[str] = set()
    for pat, label in _SECRET_PATTERNS:
        if pat.search(text) and label not in seen:
            out.append(label)
            seen.add(label)
    return out

logger = logging.getLogger("harness.mcp_config")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value: Any) -> Any:
    """Recursively replace `${VAR}` placeholders. Resolution order:
    (1) encrypted secrets store (UI-managed, highest priority — the
    user's active management layer), (2) os.environ fallback,
    (3) empty string + warning if neither has it. A name defined in
    BOTH gets a collision warning so the user notices shadowing."""
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            v_secret = secrets_store.lookup_sync(name)
            v_env = os.environ.get(name)
            if v_secret is not None and v_env is not None and v_env != v_secret:
                logger.warning(
                    "mcp_config: ${%s} defined in BOTH secrets store and env; "
                    "using secrets store (UI-managed wins on collision)",
                    name,
                )
            if v_secret is not None:
                return v_secret
            if v_env is not None:
                return v_env
            logger.warning(
                "mcp_config: ${%s} referenced but not set anywhere "
                "(expanded to '')",
                name,
            )
            return ""
        return _ENV_VAR_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _config_path() -> Path | None:
    raw = os.environ.get("HARNESS_MCP_CONFIG", "").strip()
    if not raw:
        return None
    return Path(raw)


def _load_from_file() -> tuple[dict[str, Any], list[str]]:
    """Load servers+allow-list from HARNESS_MCP_CONFIG, if configured.
    Returns ({}, []) on missing / parse-error / wrong shape."""
    path = _config_path()
    if path is None:
        return {}, []
    if not path.is_file():
        logger.info(
            "mcp_config: HARNESS_MCP_CONFIG=%s does not exist; skipping", path
        )
        return {}, []

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        logger.exception("mcp_config: failed to read/parse %s", path)
        return {}, []

    data = _interpolate(data)

    servers_in = data.get("servers") or {}
    if not isinstance(servers_in, dict):
        logger.warning("mcp_config: 'servers' must be a dict; ignoring")
        return {}, []

    allowed_in = data.get("allowed_tools") or {}
    if not isinstance(allowed_in, dict):
        logger.warning("mcp_config: 'allowed_tools' must be a dict; ignoring")
        allowed_in = {}

    servers_out: dict[str, Any] = {}
    tool_names: list[str] = []

    for name, cfg in servers_in.items():
        if not isinstance(name, str) or not name.isidentifier():
            logger.warning(
                "mcp_config: server name %r is not a valid identifier; skipping",
                name,
            )
            continue
        if not isinstance(cfg, dict):
            logger.warning("mcp_config: server %r config is not a dict; skipping", name)
            continue
        servers_out[name] = cfg
        allowed_for_this = allowed_in.get(name) or []
        if not isinstance(allowed_for_this, list):
            logger.warning(
                "mcp_config: allowed_tools[%s] must be a list; ignoring",
                name,
            )
            continue
        for t in allowed_for_this:
            if not isinstance(t, str) or not t:
                continue
            tool_names.append(f"mcp__{name}__{t}")
    return servers_out, tool_names


def _load_from_db() -> tuple[dict[str, Any], list[str]]:
    """Load enabled MCP servers + their allowed tools from the
    `mcp_servers` table. Returns ({}, []) on DB error / missing table
    (tests using a bare in-memory DB hit that path)."""
    try:
        import sqlite3
        from server.db import DB_PATH
    except Exception:
        return {}, []
    try:
        conn = sqlite3.connect(DB_PATH, timeout=2.0)
        try:
            cur = conn.execute(
                "SELECT name, config_json, allowed_tools_json "
                "FROM mcp_servers WHERE enabled = 1"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        # Table may not exist (pre-migration DB) or DB locked —
        # both fine; file-based config still works.
        return {}, []
    servers_out: dict[str, Any] = {}
    tool_names: list[str] = []
    for name, config_json, allowed_json in rows:
        if not isinstance(name, str) or not name.isidentifier():
            logger.warning("mcp_config: DB server %r has invalid name, skipping", name)
            continue
        try:
            cfg = json.loads(config_json or "{}")
        except Exception:
            logger.warning("mcp_config: DB server %r config_json is not valid JSON", name)
            continue
        if not isinstance(cfg, dict):
            continue
        cfg = _interpolate(cfg)
        try:
            allowed = json.loads(allowed_json or "[]")
        except Exception:
            allowed = []
        servers_out[name] = cfg
        for t in allowed or []:
            if isinstance(t, str) and t:
                tool_names.append(f"mcp__{name}__{t}")
    return servers_out, tool_names


def load_external_servers() -> tuple[dict[str, Any], list[str]]:
    """Load MCP servers from both HARNESS_MCP_CONFIG (file) and the
    mcp_servers DB table. DB entries are loaded SECOND so a UI-managed
    server overrides a file-based one with the same name.

    Returns (servers_dict, allowed_tool_names).
    - servers_dict: {name: sdk-compatible config dict} — ready to pass
      as ClaudeAgentOptions.mcp_servers alongside our in-process coord.
    - allowed_tool_names: list of fully-qualified tool names
      ('mcp__<server>__<tool>') to extend ALLOWED_*_TOOLS.

    Never raises — an MCP misconfig should not block the harness.
    """
    file_servers, file_tools = _load_from_file()
    db_servers, db_tools = _load_from_db()

    # DB wins on name collision. Drop file-based tools for any server
    # that the DB redefines so the two allow-lists don't leak.
    servers_out: dict[str, Any] = dict(file_servers)
    tool_names: list[str] = [
        t for t in file_tools
        # Filter: split prefix, check server name isn't overridden by DB.
        if not any(t.startswith(f"mcp__{name}__") for name in db_servers)
    ]
    servers_out.update(db_servers)
    tool_names.extend(db_tools)

    if servers_out:
        logger.info(
            "mcp_config: loaded %d external server(s): %s (allowed tools: %d)",
            len(servers_out), list(servers_out.keys()), len(tool_names),
        )
    return servers_out, tool_names
