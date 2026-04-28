"""Probe-2 — capture ConversationStep shapes for a TOOL-USING turn.

Probe-1 (scripts/codex_probe.py) captured the basic CodexClient surface
+ a minimal text-only turn. It did NOT capture:
  - shell / apply_patch / web_search step shapes
  - tool RESULT shapes (item_type names + payload structure)
  - turn-completion / usage shape

This probe issues a prompt that's likely to elicit a `shell` tool call
and then prints every ConversationStep that comes back. Run inside a
Zeabur container after `codex login`.

Usage:
    python scripts/codex_probe_tools.py

Feed the output back into:
  - Docs/CODEX_PROBE_OUTPUT.md (append a "Tool-using turn" section)
  - server/runtimes/codex.py `_ITEM_TYPE_TO_HARNESS` table
  - Docs/CODEX_AUDIT.md item #11

This script never prints auth tokens. The CodexClient subprocess can
read $OPENAI_API_KEY / $CODEX_HOME, but the notification stream is
content-only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback


def _hr(label: str) -> None:
    print(f"\n{'=' * 12} {label} {'=' * 12}")


def _step_to_dict(step) -> dict:
    """Best-effort serialization. ConversationStep is a pydantic model,
    so .model_dump() is the right call; fall back to vars() / repr()."""
    if hasattr(step, "model_dump"):
        try:
            return step.model_dump()
        except Exception:
            pass
    if hasattr(step, "__dict__"):
        return dict(step.__dict__)
    return {"_repr": repr(step)}


async def main() -> int:
    _hr("import")
    try:
        from codex_app_server_sdk import CodexClient  # type: ignore[import]
    except ImportError as exc:
        print(f"FAIL import: {exc}")
        return 1

    _hr("connect_stdio + start + initialize")
    client = CodexClient.connect_stdio()
    if hasattr(client, "__await__"):
        client = await client
    r = client.start()
    if hasattr(r, "__await__"):
        await r
    r = client.initialize()
    if hasattr(r, "__await__"):
        await r
    print("client ready")

    _hr("start_thread")
    thread = client.start_thread()
    if hasattr(thread, "__await__"):
        thread = await thread
    print(f"thread_id: {thread.thread_id}")

    # Prompt designed to elicit a shell tool call. The model may also
    # use apply_patch or web_search depending on its mood; that's fine
    # — we want to capture whatever it does.
    prompt = (
        "Run the shell command `echo hello-from-tool-probe` and tell me "
        "what it printed. Use the shell tool to do it."
    )
    _hr(f"chat: {prompt!r}")

    step_records: list[dict] = []
    item_types_seen: set[str] = set()
    step_types_seen: set[str] = set()

    try:
        stream = thread.chat(prompt)
        if hasattr(stream, "__await__"):
            stream = await stream

        async for step in stream:
            d = _step_to_dict(step)
            step_records.append(d)
            it = getattr(step, "item_type", None) or d.get("item_type")
            st = getattr(step, "step_type", None) or d.get("step_type")
            if it:
                item_types_seen.add(it)
            if st:
                step_types_seen.add(st)
            print(f"\n--- step #{len(step_records)} ---")
            print(f"step_type: {st}")
            print(f"item_type: {it}")
            print(f"status:    {d.get('status')}")
            print(f"text:      {d.get('text')!r}")
            print(f"item_id:   {d.get('item_id')}")
            # Print the full payload one level deep so the structure is
            # visible. Truncate the inner string fields to keep output
            # readable.
            data = d.get("data") or {}
            params = (data.get("params") or {}) if isinstance(data, dict) else {}
            item = params.get("item") if isinstance(params, dict) else None
            if isinstance(item, dict):
                print("data.params.item keys: " + ", ".join(sorted(item.keys())))
                print("data.params.item:")
                print(json.dumps(item, indent=2, default=str)[:1500])

            if len(step_records) > 80:
                print("(stopping after 80 steps)")
                break
    except Exception:
        traceback.print_exc()

    _hr("summary")
    print(f"total steps: {len(step_records)}")
    print(f"item_types seen: {sorted(item_types_seen)}")
    print(f"step_types seen: {sorted(step_types_seen)}")

    _hr("close")
    try:
        r = client.close()
        if hasattr(r, "__await__"):
            await r
    except Exception:
        traceback.print_exc()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
