"""SDK smoke test — isolates which ClaudeAgentOptions kwarg trips the CLI.

Run inside the container:
    cd /app && python scripts/diag_sdk.py

The script tries four configurations in order, each layering on one more
thing we pass in production. First failure tells us which layer is the
culprit. Each test captures the full exception including stderr when we
can get at it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

from claude_agent_sdk import ClaudeAgentOptions, query


async def run_test(name: str, **kwargs) -> bool:
    print(f"\n===== {name} =====", flush=True)
    print(f"kwargs: {list(kwargs.keys())}", flush=True)
    try:
        got_reply = False
        async for msg in query(prompt="hi", options=ClaudeAgentOptions(**kwargs)):
            t = type(msg).__name__
            print(f"  msg: {t}", flush=True)
            if t == "AssistantMessage":
                got_reply = True
        print(f"  RESULT: {'ok' if got_reply else 'no reply'}", flush=True)
        return got_reply
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return False


async def main() -> None:
    print(f"python: {sys.version.split()[0]}", flush=True)
    print(f"CLAUDE_CONFIG_DIR: {os.environ.get('CLAUDE_CONFIG_DIR')}", flush=True)

    # 1. Bare minimum. If this fails, the SDK/CLI baseline is broken.
    await run_test("1-minimal")

    # 2. Add cwd. Rules out filesystem issues.
    await run_test("2-cwd", cwd="/workspaces/p1")

    # 3. Add system_prompt. Rules out long-prompt argv limits.
    await run_test(
        "3-system-prompt",
        cwd="/workspaces/p1",
        system_prompt="You are a helpful assistant.",
    )

    # 4. Add max_turns.
    await run_test(
        "4-max-turns",
        cwd="/workspaces/p1",
        system_prompt="You are a helpful assistant.",
        max_turns=1,
    )

    # 5. Add allowed_tools (the harness production list).
    await run_test(
        "5-allowed-tools",
        cwd="/workspaces/p1",
        system_prompt="You are a helpful assistant.",
        max_turns=1,
        allowed_tools=["Read", "Grep", "Glob"],
    )

    # 6. Add an in-process MCP server like we do in the harness.
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool

        @tool("ping", "reply with pong", {})
        async def ping(args):
            return {"content": [{"type": "text", "text": "pong"}]}

        mcp = create_sdk_mcp_server(name="diag", version="0.1.0", tools=[ping])
        await run_test(
            "6-mcp-server",
            cwd="/workspaces/p1",
            system_prompt="You are a helpful assistant.",
            max_turns=1,
            allowed_tools=["Read", "mcp__diag__ping"],
            mcp_servers={"diag": mcp},
        )
    except Exception as e:
        print(f"  mcp test skipped: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
