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

    _hr("instantiate CodexClient")
    try:
        client = CodexClient()
    except Exception:
        print("FAIL: CodexClient() constructor")
        traceback.print_exc()
        return 1
    print("client:", client)
    print("instance attrs:", [a for a in dir(client) if not a.startswith("_")])

    _hr("start_thread / thread_start (try both)")
    thread = None
    for method_name in ("start_thread", "thread_start", "create_thread", "new_thread"):
        m = getattr(client, method_name, None)
        if m is None:
            continue
        try:
            sig = inspect.signature(m)
        except (ValueError, TypeError):
            sig = "(...)"
        print(f"trying client.{method_name}{sig}")
        try:
            result = m()
            if inspect.isawaitable(result):
                result = await result
            thread = result
            print(f"  OK -> {type(thread).__name__}")
            break
        except Exception:
            print(f"  FAIL on client.{method_name}:")
            traceback.print_exc()
    if thread is None:
        print("FAIL: could not start a thread via any expected method name")
        return 1

    _hr("thread methods")
    for name in sorted(n for n in dir(thread) if not n.startswith("_")):
        obj = getattr(thread, name)
        if not callable(obj):
            continue
        try:
            sig = inspect.signature(obj)
        except (ValueError, TypeError):
            sig = "(...)"
        print(f"  {name}{sig}")
    print("thread instance attrs (non-callable):")
    for name in sorted(n for n in dir(thread) if not n.startswith("_")):
        obj = getattr(thread, name)
        if not callable(obj):
            print(f"  {name} = {obj!r}")

    _hr("run a tiny turn — 'reply hi'")
    run_method = None
    for cand in ("run", "send", "stream", "start_turn"):
        if hasattr(thread, cand):
            run_method = cand
            break
    if run_method is None:
        print("FAIL: thread has no recognizable run method")
        return 1
    print(f"using thread.{run_method}(...)")
    try:
        stream = getattr(thread, run_method)("reply with the single word: hi")
        if inspect.isawaitable(stream):
            stream = await stream
        # Try async-iterating; if that fails, try buffering attrs.
        notifications = []
        try:
            async for note in stream:
                notifications.append(note)
                print(f"NOTE  {type(note).__name__}  {repr(note)[:400]}")
                if len(notifications) > 60:
                    print("(stopping after 60 notifications)")
                    break
        except TypeError:
            print("stream is not async-iterable; printing repr:")
            print(repr(stream)[:600])
            return 1
    except Exception:
        print("FAIL: running turn:")
        traceback.print_exc()
        return 1

    _hr("compact / resume probing")
    for cand in ("compact", "resume", "close", "delete"):
        if hasattr(thread, cand):
            obj = getattr(thread, cand)
            try:
                sig = inspect.signature(obj)
            except (ValueError, TypeError):
                sig = "(...)"
            print(f"thread.{cand}{sig} EXISTS")
        else:
            print(f"thread.{cand}  -- not present")

    _hr("done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
