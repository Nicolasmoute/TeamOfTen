"""Standalone stdio MCP server that proxies coord_* tool calls to the
main FastAPI process over loopback HTTP.

Why subprocess?  The Codex runtime expects MCP servers as
`{command, args, env}` configs — not in-process callables. We spawn
this module per turn so Codex gets its MCP fix while the actual tool
work still runs in the main process where the event bus and wake
scheduler live. ClaudeRuntime stays in-process (no subprocess hop)
because there's no reason to add latency to the default path.

See `Docs/CODEX_RUNTIME_SPEC.md` §C for design rationale.

Usage (Codex runtime spawns this with the token in env):

    python -m server.coord_mcp \\
        --caller-id p3 \\
        --proxy-url http://127.0.0.1:8000

with `HARNESS_COORD_PROXY_TOKEN=<token>` in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx

logger = logging.getLogger("server.coord_mcp")


# ---------------- HTTP client ----------------


class CoordProxyClient:
    """Forwards tool calls to `${proxy_url}/api/_coord/{tool}`.

    Single instance per subprocess; reuses an httpx connection so
    each tool call is one round-trip on a kept-alive socket.
    """

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
        url = f"{self.base}/api/_coord/{tool_name}"
        resp = await self._client.post(
            url,
            headers={"Authorization": f"Bearer {self.token}"},
            json={"caller_id": self.caller_id, "args": args},
        )
        if resp.status_code >= 500:
            resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------- MCP stdio loop ----------------
#
# Hand-rolled JSON-RPC 2.0 over stdio — Codex's MCP loader speaks the
# same protocol regardless of the implementation. The mcp Python
# package would also work; this stays minimal so the subprocess has
# no dependency beyond httpx (already a runtime dep).


async def _read_message(stream: asyncio.StreamReader) -> dict[str, Any] | None:
    line = await stream.readline()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def _write_message(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _success(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def _serve(client: CoordProxyClient) -> int:
    # Cache the tool catalog once at startup. The catalog is static
    # for the duration of the subprocess; if main-process tools
    # change, the next spawn picks up the new list.
    try:
        tool_names = await client.list_tools()
    except Exception as exc:
        logger.exception("coord_mcp: failed to fetch tool catalog: %s", exc)
        return 2

    tool_descriptors = [
        {
            "name": name,
            # Description and schema are intentionally minimal — the
            # actual schemas live in server.tools and the LLM-side
            # catalog is hydrated by the host runtime, not by us. We
            # only need names here so the host registers the tools.
            "description": f"Coord proxy tool {name}",
            "inputSchema": {"type": "object", "additionalProperties": True},
        }
        for name in tool_names
    ]

    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(loop=loop)
    proto = asyncio.StreamReaderProtocol(reader, loop=loop)
    await loop.connect_read_pipe(lambda: proto, sys.stdin)

    while True:
        msg = await _read_message(reader)
        if msg is None:
            break  # EOF — host closed stdin, we exit cleanly
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            _write_message(_success(msg_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "coord-proxy", "version": "0.1.0"},
            }))
        elif method == "tools/list":
            _write_message(_success(msg_id, {"tools": tool_descriptors}))
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                resp = await client.call_tool(name, args)
            except Exception as exc:
                _write_message(_error(msg_id, -32000, f"proxy call failed: {exc}"))
                continue
            if not resp.get("ok"):
                _write_message(_error(msg_id, -32000, resp.get("error") or "unknown error"))
                continue
            # MCP's tools/call shape: { content: [{type:"text", text:...}] }.
            # We marshal the proxy's `result` dict into a single text
            # block so the host runtime sees a uniform envelope.
            text = json.dumps(resp.get("result"), ensure_ascii=False)
            _write_message(_success(msg_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            }))
        elif method == "ping":
            _write_message(_success(msg_id, {}))
        elif method is not None and msg_id is None:
            # Notification (no response) — silently ignore unknowns.
            continue
        else:
            _write_message(_error(msg_id, -32601, f"unknown method: {method!r}"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Coord MCP stdio proxy")
    parser.add_argument("--caller-id", required=True)
    parser.add_argument("--proxy-url", required=True)
    args = parser.parse_args()

    token = os.environ.get("HARNESS_COORD_PROXY_TOKEN", "").strip()
    if not token:
        sys.stderr.write(
            "coord_mcp: HARNESS_COORD_PROXY_TOKEN env var is required "
            "(spawn-time token is passed via env, never argv).\n"
        )
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
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
