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

logger = logging.getLogger("harness.mcp_config")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value: Any) -> Any:
    """Recursively replace `${VAR}` placeholders with os.environ values.
    Unknown vars expand to empty string (logged once)."""
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            v = os.environ.get(name)
            if v is None:
                logger.warning(
                    "mcp_config: ${%s} referenced but not in env (expanded to '')",
                    name,
                )
                return ""
            return v
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


def load_external_servers() -> tuple[dict[str, Any], list[str]]:
    """Load the MCP config file.

    Returns (servers_dict, allowed_tool_names).
    - servers_dict: {name: sdk-compatible config dict} — ready to pass
      as ClaudeAgentOptions.mcp_servers alongside our in-process coord.
    - allowed_tool_names: list of fully-qualified tool names
      ('mcp__<server>__<tool>') to extend ALLOWED_*_TOOLS.

    Returns ({}, []) when the config is missing, unreadable, or empty.
    Never raises — an MCP misconfig should not block the harness.
    """
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

    if servers_out:
        logger.info(
            "mcp_config: loaded %d external server(s): %s (allowed tools: %d)",
            len(servers_out), list(servers_out.keys()), len(tool_names),
        )
    return servers_out, tool_names
