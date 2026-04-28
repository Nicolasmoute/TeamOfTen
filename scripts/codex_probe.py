"""Codex Python SDK probe — prints actual signatures + a live turn's
notification stream. Run inside a Zeabur container after `codex login`.

Usage:
    python scripts/codex_probe.py

Output is verbose by design: feed it back into Docs/CODEX_RUNTIME_SPEC.md
§E.2 (method names) and §E.3 (notification table). Then unblock items
8-11, 14-15, 25-26, 31-32 in CODEX_AUDIT.md.

This script never prints auth tokens — only public method names and
notification class names / payloads. If your prompt accidentally elicits
a token, the SDK doesn't expose it through the notification stream
anyway.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import traceback


def _hr(label: str) -> None:
    print(f"\n{'=' * 12} {label} {'=' * 12}")


async def main() -> int:
    _hr("import")
    try:
        import codex_app_server_sdk as sdk  # type: ignore[import]
    except ImportError as exc:
        print(f"FAIL: import codex_app_server_sdk -> {exc}")
        return 1
    print("module:", sdk.__name__)
    print("file:", getattr(sdk, "__file__", "<unknown>"))
    print("version:", getattr(sdk, "__version__", "<no __version__>"))
    print("public attrs:")
    for name in sorted(n for n in dir(sdk) if not n.startswith("_")):
        obj = getattr(sdk, name)
        kind = type(obj).__name__
        print(f"  {name}  ({kind})")

    _hr("CodexClient methods")
    CodexClient = getattr(sdk, "CodexClient", None)
    if CodexClient is None:
        print("FAIL: sdk has no CodexClient attribute")
        return 1
    for name in sorted(n for n in dir(CodexClient) if not n.startswith("_")):
        obj = getattr(CodexClient, name)
        if not callable(obj):
            continue
        try:
            sig = inspect.signature(obj)
        except (ValueError, TypeError):
            sig = "(...)"
        print(f"  {name}{sig}")

    _hr("ThreadHandle methods")
    ThreadHandle = getattr(sdk, "ThreadHandle", None)
    if ThreadHandle is not None:
        for name in sorted(n for n in dir(ThreadHandle) if not n.startswith("_")):
            obj = getattr(ThreadHandle, name)
            if not callable(obj):
                continue
            try:
                sig = inspect.signature(obj)
            except (ValueError, TypeError):
                sig = "(...)"
            print(f"  {name}{sig}")

    _hr("ThreadConfig fields")
    ThreadConfig = getattr(sdk, "ThreadConfig", None)
    if ThreadConfig is not None:
        try:
            from pydantic import BaseModel
            if issubclass(ThreadConfig, BaseModel):
                for fname, finfo in ThreadConfig.model_fields.items():
                    print(f"  {fname}: {finfo.annotation} default={finfo.default!r}")
        except Exception:
            print("  (not a pydantic model)")
            for n in sorted(d for d in dir(ThreadConfig) if not d.startswith("_")):
                print(f"  {n}")

    _hr("TurnOverrides fields")
    TurnOverrides = getattr(sdk, "TurnOverrides", None)
    if TurnOverrides is not None:
        try:
            from pydantic import BaseModel
            if issubclass(TurnOverrides, BaseModel):
                for fname, finfo in TurnOverrides.model_fields.items():
                    print(f"  {fname}: {finfo.annotation} default={finfo.default!r}")
        except Exception:
            for n in sorted(d for d in dir(TurnOverrides) if not d.startswith("_")):
                print(f"  {n}")

    _hr("connect_stdio")
    # CodexClient() needs a transport; use the connect_stdio
    # classmethod which spawns `codex app-server` as a subprocess.
    try:
        connect = CodexClient.connect_stdio
        sig = inspect.signature(connect)
        print(f"signature: {sig}")
    except Exception:
        traceback.print_exc()
    try:
        client = CodexClient.connect_stdio()
        if inspect.isawaitable(client):
            client = await client
        print(f"client constructed: {type(client).__name__}")
    except Exception:
        print("FAIL: connect_stdio:")
        traceback.print_exc()
        return 1

    # Some SDKs require explicit start; try if it exists.
    if hasattr(client, "start"):
        try:
            r = client.start()
            if inspect.isawaitable(r):
                r = await r
            print(f"client.start() -> {type(r).__name__}")
        except Exception:
            print("client.start() raised:")
            traceback.print_exc()

    if hasattr(client, "initialize"):
        try:
            r = client.initialize()
            if inspect.isawaitable(r):
                r = await r
            print(f"client.initialize() -> {type(r).__name__}: {repr(r)[:400]}")
        except Exception:
            print("client.initialize() raised:")
            traceback.print_exc()

    _hr("start_thread")
    try:
        r = client.start_thread()
        if inspect.isawaitable(r):
            r = await r
        thread = r
        print(f"  OK -> {type(thread).__name__}: {repr(thread)[:300]}")
    except Exception:
        print("FAIL start_thread:")
        traceback.print_exc()
        return 1

    print("thread instance attrs (non-callable):")
    for name in sorted(n for n in dir(thread) if not n.startswith("_")):
        obj = getattr(thread, name)
        if not callable(obj):
            print(f"  {name} = {obj!r}")

    _hr("run a tiny turn via thread.chat — 'reply hi'")
    try:
        stream = thread.chat("reply with the single word: hi")
        if inspect.isawaitable(stream):
            stream = await stream
        notifications = []
        try:
            async for note in stream:
                notifications.append(note)
                print(f"NOTE  {type(note).__name__}  {repr(note)[:600]}")
                if len(notifications) > 80:
                    print("(stopping after 80 notifications)")
                    break
        except TypeError:
            print("stream not async-iterable; repr:")
            print(repr(stream)[:600])
    except Exception:
        print("FAIL chat:")
        traceback.print_exc()

    _hr("close")
    if hasattr(client, "close"):
        try:
            r = client.close()
            if inspect.isawaitable(r):
                r = await r
            print("closed")
        except Exception:
            traceback.print_exc()

    _hr("done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
