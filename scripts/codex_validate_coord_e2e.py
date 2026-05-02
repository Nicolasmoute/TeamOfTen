"""Probe-4 — end-to-end validation of coord_* MCP under a live Codex
turn (audit item #30 / spec §L.3).

Drives the running harness from outside via its normal HTTP + WebSocket
API. The harness handles token minting + proxy subprocess spawn +
Codex thread config; this script just verifies the loop closes.

Flow:
  1. PUT /api/agents/<slot>/runtime → codex (assumes flag enabled).
  2. POST /api/agents/<slot>/start with a prompt asking the agent to
     call `coord_list_tasks` and report the count.
  3. Subscribe to /ws (or poll /api/events) for the duration of the
     turn.
  4. PASS if a `tool_use` event with tool starting `mcp__coord__`
     arrives AND a paired `tool_result` (or any subsequent `text`
     event referencing the count) lands without an error.
  5. FAIL if the turn errors before the coord call, or the call's
     tool_result carries an error string.

Usage (on the same host as the harness, e.g. inside the Zeabur shell):

    HARNESS_BASE=http://127.0.0.1:${PORT:-8080} \
    HARNESS_TOKEN=<bearer-or-empty> \
    HARNESS_VALIDATE_SLOT=p10 \
    python scripts/codex_validate_coord_e2e.py

Defaults: HARNESS_BASE=http://127.0.0.1:8080, slot=p10. HARNESS_TOKEN is
optional (the harness only requires it when configured).

Cost note: spawns one real Codex turn. ChatGPT plan: counts toward
limits. API key: a few cents.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _hr(label: str) -> None:
    print(f"\n{'=' * 12} {label} {'=' * 12}")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


HARNESS_BASE = _env("HARNESS_BASE", "http://127.0.0.1:8080").rstrip("/")
HARNESS_TOKEN = _env("HARNESS_TOKEN", "")
SLOT = _env("HARNESS_VALIDATE_SLOT", "p10")
TURN_TIMEOUT_S = float(_env("HARNESS_VALIDATE_TIMEOUT", "120"))
COORD_PROMPT = (
    "Call the coord_list_tasks tool with no filter. Once it returns, "
    "tell me how many tasks were in the list. Do not invent a number "
    "— use the real tool result."
)


def _hdr() -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if HARNESS_TOKEN:
        h["Authorization"] = f"Bearer {HARNESS_TOKEN}"
    return h


def _request(method: str, path: str, *, body: dict | None = None,
             timeout: float = 10.0) -> tuple[int, Any]:
    url = HARNESS_BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method, headers=_hdr())
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
            return e.code, json.loads(raw) if raw else None
        except Exception:
            return e.code, None
    except URLError as e:
        return 0, str(e)


def _ws_url(after_id: int | None = None) -> str:
    """Return ws:// or wss:// URL with optional `after_id` resume cursor.
    Adds the bearer token as ?token= per the harness's WS auth path."""
    base = HARNESS_BASE.replace("http://", "ws://").replace("https://", "wss://")
    qs = []
    if HARNESS_TOKEN:
        qs.append("token=" + HARNESS_TOKEN)
    if after_id is not None:
        qs.append("after_id=" + str(after_id))
    return base + "/ws" + ("?" + "&".join(qs) if qs else "")


async def _restore_runtime(original: str | None) -> None:
    """Best-effort restore of the slot's runtime_override to whatever
    it was before we set it to codex. Empty string clears any override
    (falls through to role default → claude). Silent on failure — this
    is cleanup, not part of the verdict."""
    if original is None:
        return
    payload = {"runtime": original}
    status, body = _request(
        "PUT", f"/api/agents/{SLOT}/runtime", body=payload, timeout=5.0,
    )
    if status == 200:
        print(f"  restored runtime_override → {original!r}")
    else:
        print(
            f"  WARN: could not restore runtime_override (status={status}). "
            "Set it back manually via the pane settings popover."
        )


async def _watch_via_polling(*, since_id: int, deadline: float) -> dict:
    """Poll /api/events for the slot until success or deadline.

    Event shape (per server/main.py /api/events):
      {"id": <int>, "ts": ..., "agent_id": ..., "type": ...,
       "payload": {...type-specific keys...}}
    The cursor query parameter is `since_id`, not `after_id`. Fields
    like `tool`, `is_error`, `error`, `content` live on `payload`,
    not at the top level.
    """
    saw_coord_tool_use = False
    saw_coord_tool_result = False
    saw_tool_error: str | None = None
    saw_turn_error: str | None = None
    done = False
    last_id = since_id

    while time.time() < deadline:
        path = (
            f"/api/events?agent={SLOT}&since_id={last_id}&limit=200"
        )
        status, body = _request("GET", path)
        if status != 200 or not isinstance(body, dict):
            await asyncio.sleep(1.0)
            continue
        events = body.get("events") or []
        for ev in events:
            eid = ev.get("id")
            if isinstance(eid, int) and eid > last_id:
                last_id = eid
            t = ev.get("type")
            payload = ev.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            if t == "tool_use" and str(payload.get("tool", "")).startswith("mcp__coord__"):
                saw_coord_tool_use = True
                print(f"  + tool_use {payload.get('tool')} id={payload.get('id')}")
            elif t == "tool_result":
                if saw_coord_tool_use:
                    content = payload.get("content")
                    is_err = bool(payload.get("is_error"))
                    if is_err:
                        saw_tool_error = (
                            content if isinstance(content, str)
                            else json.dumps(content)[:300]
                        )
                    else:
                        saw_coord_tool_result = True
                    print(f"  + tool_result id={payload.get('id')} is_error={is_err}")
            elif t == "error":
                saw_turn_error = str(payload.get("error") or "")[:300]
                print(f"  + error: {saw_turn_error}")
            elif t == "result":
                done = True
                print("  + result: ok")
        if done:
            break
        await asyncio.sleep(1.5)

    return {
        "saw_coord_tool_use": saw_coord_tool_use,
        "saw_coord_tool_result": saw_coord_tool_result,
        "saw_tool_error": saw_tool_error,
        "saw_turn_error": saw_turn_error,
        "done": done,
        "last_id": last_id,
    }


async def main() -> int:
    _hr("preflight")
    print(f"  HARNESS_BASE = {HARNESS_BASE}")
    print(f"  slot          = {SLOT}")
    print(f"  token set     = {bool(HARNESS_TOKEN)}")

    # 1. Health probe — use /api/health/detail (auth-protected) for the
    # full subsystem report. The public /api/health is liveness-only
    # since the security hardening PR.
    status, body = _request("GET", "/api/health/detail")
    if status != 200:
        print(f"FAIL: /api/health/detail returned {status}: {body!r}")
        return 1
    health = body if isinstance(body, dict) else {}
    codex_auth = (health.get("checks") or {}).get("codex_auth") or {}
    if not codex_auth.get("credentials_present"):
        print(
            "FAIL: codex_auth.credentials_present is false. Run "
            "`codex login` (device-auth) inside the container or set "
            "an OPENAI_API_KEY in the secrets store before re-running."
        )
        return 1
    print(f"  codex auth method: {codex_auth.get('method')}")

    # 2. Snapshot original runtime so we can restore on exit.
    status, body = _request("GET", "/api/agents")
    original_runtime: str | None = None
    if status == 200 and isinstance(body, dict):
        for a in body.get("agents") or []:
            if a.get("id") == SLOT:
                original_runtime = a.get("runtime_override") or ""
                break
    print(f"  original runtime_override for {SLOT} = {original_runtime!r}")

    # 3. Set the slot to codex runtime.
    _hr("set runtime → codex")
    status, body = _request(
        "PUT", f"/api/agents/{SLOT}/runtime",
        body={"runtime": "codex"},
    )
    if status != 200:
        print(f"FAIL: PUT runtime returned {status}: {body!r}")
        if status == 400:
            print(
                "  (HARNESS_CODEX_ENABLED probably not set — flip the "
                "env var on the deployed harness and redeploy.)"
            )
        elif status == 409:
            print(
                "  (Slot is mid-turn. Wait for it to finish or cancel "
                f"via the UI, then re-run.)"
            )
        return 1
    print(f"  runtime_override = {body.get('runtime_override')}")

    # 4. Snapshot the latest event id BEFORE starting the turn so the
    #    poller can scope to events the turn emits. /api/events returns
    #    events ordered oldest→newest; the last one is the largest id.
    status, body = _request("GET", f"/api/events?agent={SLOT}&limit=1")
    since_id = 0
    if status == 200 and isinstance(body, dict):
        events = body.get("events") or []
        if events:
            since_id = events[-1].get("id") or 0
    print(f"  events since_id = {since_id}")

    # 5. Fire the turn.
    _hr(f"start turn — {COORD_PROMPT[:60]}…")
    status, body = _request(
        "POST", f"/api/agents/{SLOT}/start",
        body={"prompt": COORD_PROMPT},
        timeout=15.0,
    )
    if status == 409:
        print(
            f"FAIL: slot {SLOT} is mid-turn (409). Either wait for the "
            "current turn to finish, or pick a different slot via "
            f"HARNESS_VALIDATE_SLOT=<slotid>."
        )
        await _restore_runtime(original_runtime)
        return 1
    if status not in (200, 202):
        print(f"FAIL: POST start returned {status}: {body!r}")
        await _restore_runtime(original_runtime)
        return 1
    print(f"  start ok — observing for up to {TURN_TIMEOUT_S}s")

    # 5. Watch events until result/error/timeout.
    _hr("observe")
    deadline = time.time() + TURN_TIMEOUT_S
    obs = await _watch_via_polling(since_id=since_id, deadline=deadline)

    # 6. Verdict. Always restore the original runtime before returning
    #    so re-running this script doesn't leave the slot pinned to codex.
    _hr("verdict")
    try:
        if obs["saw_turn_error"]:
            print(f"FAIL — turn errored before coord call: {obs['saw_turn_error']}")
            return 1
        if not obs["saw_coord_tool_use"]:
            print(
                "FAIL — no `mcp__coord__*` tool_use observed within timeout. "
                "Possible causes: agent ignored the instruction, the proxy "
                "subprocess didn't start, or HARNESS_COORD_PROXY_TOKEN env "
                "wasn't injected. Check the agent pane timeline and the "
                "harness logs around the turn."
            )
            return 1
        if obs["saw_tool_error"]:
            print(f"FAIL — coord tool returned error: {obs['saw_tool_error']}")
            return 1
        if not obs["saw_coord_tool_result"]:
            print(
                "PARTIAL — coord tool_use observed but no successful "
                "tool_result before deadline. Re-run with a longer timeout "
                "or check whether the harness's /api/_coord endpoint is "
                "reachable from the proxy subprocess."
            )
            return 2
        print("PASS — Codex invoked coord_*, the proxy reached the harness, "
              "and the result came back without error.")
        print(
            "\nFlip Docs/CODEX_AUDIT.md item #30 to `completed and audited` "
            "and record the run timestamp."
        )
        return 0
    finally:
        await _restore_runtime(original_runtime)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
