"""Standalone stdio MCP server that proxies coord_* calls to FastAPI.

Codex expects MCP servers as subprocess configs (`command`, `args`,
`env`), while the real coord tool handlers live in the main harness
process where the DB, event bus, and wake scheduler are already active.
This module is the small bridge between those worlds:

    python -m server.coord_mcp \
        --caller-id p3 \
        --proxy-url http://127.0.0.1:8000

with HARNESS_COORD_PROXY_TOKEN in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any

import httpx
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger("server.coord_mcp")


class CoordProxyClient:
    """Forward tool calls to `${proxy_url}/api/_coord/{tool}`."""

    def __init__(self, proxy_url: str, token: str, caller_id: str) -> None:
        self.base = proxy_url.rstrip("/")
        self.token = token
        self.caller_id = caller_id
        self._client = httpx.AsyncClient(timeout=120.0)

    async def list_tools(self) -> list[str]:
        resp = await self._client.get(f"{self.base}/api/_coord/_tools")
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("tools", []))

    async def call_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self.base}/api/_coord/{tool_name}",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"caller_id": self.caller_id, "args": args},
        )
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


async def _serve(client: CoordProxyClient) -> int:
    """Serve the coord proxy over the official MCP stdio transport."""
    try:
        tool_names = await client.list_tools()
    except Exception as exc:
        logger.exception("coord_mcp: failed to fetch tool catalog: %s", exc)
        return 2

    tool_descriptors = [
        types.Tool(
            name=name,
            description=f"Coord proxy tool {name}",
            inputSchema={"type": "object", "additionalProperties": True},
        )
        for name in tool_names
    ]

    server = Server("coord-proxy", version="0.1.0")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return tool_descriptors

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, args: dict[str, Any]) -> types.CallToolResult:
        try:
            resp = await client.call_tool(name, args or {})
        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"proxy call failed: {type(exc).__name__}: {exc}",
                    )
                ],
                isError=True,
            )
        if not resp.get("ok"):
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=str(resp.get("error") or "unknown coord proxy error"),
                    )
                ],
                isError=True,
            )

        text = json.dumps(resp.get("result"), ensure_ascii=False)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            isError=False,
        )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Coord MCP stdio proxy")
    parser.add_argument("--caller-id", required=True)
    parser.add_argument("--proxy-url", required=True)
    args = parser.parse_args()

    token = os.environ.get("HARNESS_COORD_PROXY_TOKEN", "").strip()
    if not token:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        )
        logger.error(
            "coord_mcp: HARNESS_COORD_PROXY_TOKEN env var is required "
            "(spawn-time token is passed via env, never argv)."
        )
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    client = CoordProxyClient(args.proxy_url, token, args.caller_id)
    try:
        return asyncio.run(_serve(client))
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            asyncio.run(client.aclose())
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
