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


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _response_payload(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text.strip()


def _http_error_text(status_code: int, payload: Any) -> str:
    detail: Any = None
    if isinstance(payload, dict):
        detail = (
            payload.get("error")
            or payload.get("detail")
            or payload.get("message")
        )
    elif payload:
        detail = payload
    text = _stringify(detail if detail is not None else payload)
    return f"HTTP {status_code}: {text}" if text else f"HTTP {status_code}"


def _proxy_error_text(resp: dict[str, Any]) -> str:
    for key in ("error", "detail", "message"):
        value = resp.get(key)
        if value:
            return _stringify(value)
    return _stringify(resp) or "unknown coord proxy error"


def _tool_result_content(result: Any) -> list[types.TextContent]:
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        items: list[types.TextContent] = []
        for item in result["content"]:
            if (
                isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ):
                items.append(types.TextContent(type="text", text=item["text"]))
            else:
                items.append(types.TextContent(type="text", text=_stringify(item)))
        return items or [types.TextContent(type="text", text="")]
    return [types.TextContent(type="text", text=_stringify(result))]


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
        payload = _response_payload(resp)
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": _http_error_text(resp.status_code, payload),
            }
        if not isinstance(payload, dict):
            return {
                "ok": False,
                "error": f"unexpected proxy response: {_stringify(payload)}",
            }
        return payload

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
                        text=_proxy_error_text(resp),
                    )
                ],
                isError=True,
            )

        result = resp.get("result")
        if isinstance(result, dict) and result.get("isError"):
            return types.CallToolResult(
                content=_tool_result_content(result),
                isError=True,
            )

        return types.CallToolResult(
            content=_tool_result_content(result),
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
