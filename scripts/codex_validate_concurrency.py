"""Probe-3 — validate the SDK's "one active turn consumer per client"
constraint (audit item #29 / spec §L.2).

Two checks:

  A. Sequential 5-turn loop on a single CodexClient + ThreadHandle.
     Asserts:
       - every emitted ConversationStep carries a turn_id
       - within each turn, all steps share one turn_id
       - across turns, turn_ids differ (no cross-talk into a previous turn)
       - the stream terminates cleanly per turn

  B. Concurrent turns on the SAME client (deliberate violation of the
     SDK's documented constraint). Observes whether the SDK:
       - serializes the second `chat()` (waits for first to finish)
       - rejects the second `chat()` with CodexTurnInactiveError
       - INTERLEAVES notifications across both turns (the dangerous case)

  The harness's `_SPAWN_LOCK` already prevents (B) in practice, but
  the goal of this probe is to confirm the SDK's behaviour matches our
  defensive assumption. If the SDK interleaves, we need to add a
  turn_id check to `handle_step`.

Usage (in a Zeabur shell after `codex login`):

    cd /tmp && rm -rf tot && \
      git clone --branch <your-branch> --depth 1 \
      https://github.com/Nicolasmoute/TeamOfTen.git tot && \
      python tot/scripts/codex_validate_concurrency.py

Cost note: 5+ small Codex turns. On a ChatGPT plan it counts toward
plan limits; on an API key it costs a few cents at most.
"""

from __future__ import annotations

import asyncio
import sys
import traceback


def _hr(label: str) -> None:
    print(f"\n{'=' * 12} {label} {'=' * 12}")


async def _open_client():
    from codex_app_server_sdk import CodexClient  # type: ignore[import]

    client = CodexClient.connect_stdio()
    if hasattr(client, "__await__"):
        client = await client
    r = client.start()
    if hasattr(r, "__await__"):
        await r
    r = client.initialize()
    if hasattr(r, "__await__"):
        await r
    return client


async def _drain_turn(thread, prompt: str, *, label: str) -> dict:
    """Drain one turn; return diagnostics."""
    print(f"\n--- {label}: chat({prompt!r}) ---")
    turn_ids: set[str] = set()
    item_types: list[str] = []
    step_count = 0
    final_text: str | None = None
    err: str | None = None

    try:
        stream = thread.chat(prompt)
        if hasattr(stream, "__await__"):
            stream = await stream
        async for step in stream:
            step_count += 1
            tid = getattr(step, "turn_id", None)
            if tid:
                turn_ids.add(tid)
            it = getattr(step, "item_type", None) or "?"
            item_types.append(it)
            txt = getattr(step, "text", None)
            if txt:
                final_text = txt
            if step_count > 80:
                print("(stopping after 80 steps)")
                break
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()

    return {
        "label": label,
        "step_count": step_count,
        "turn_ids": sorted(turn_ids),
        "item_types": item_types,
        "final_text": final_text,
        "err": err,
    }


async def _check_a_sequential(thread) -> bool:
    """5 turns, one after another. Each should have its own turn_id;
    no turn_id should leak across boundaries."""
    _hr("CHECK A — sequential 5-turn loop")
    diagnostics = []
    for i in range(1, 6):
        d = await _drain_turn(
            thread,
            f"reply with the single word: {i}",
            label=f"turn-{i}",
        )
        diagnostics.append(d)
        print(
            f"  -> {d['step_count']} steps, "
            f"turn_ids={d['turn_ids']}, "
            f"final_text={d['final_text']!r}, err={d['err']}"
        )

    ok = True
    seen_turn_ids: set[str] = set()
    for d in diagnostics:
        if d["err"]:
            ok = False
            print(f"FAIL: {d['label']} errored: {d['err']}")
        if len(d["turn_ids"]) != 1:
            ok = False
            print(
                f"FAIL: {d['label']} saw {len(d['turn_ids'])} turn_ids "
                f"({d['turn_ids']}) — expected exactly 1 per turn"
            )
        for tid in d["turn_ids"]:
            if tid in seen_turn_ids:
                ok = False
                print(
                    f"FAIL: turn_id {tid} appeared in multiple turns — "
                    f"this would be cross-talk"
                )
            seen_turn_ids.add(tid)

    print(f"\nCHECK A {'PASSED' if ok else 'FAILED'} "
          f"({len(seen_turn_ids)} distinct turn_ids across "
          f"{len(diagnostics)} turns)")
    return ok


async def _check_b_concurrent(thread) -> str:
    """Issue two thread.chat() calls without awaiting the first to
    completion. Observe behavior."""
    _hr("CHECK B — deliberately concurrent chat() calls")

    async def one(prompt, label):
        try:
            return await _drain_turn(thread, prompt, label=label)
        except Exception as exc:
            return {"label": label, "err": f"{type(exc).__name__}: {exc}",
                    "turn_ids": [], "step_count": 0, "item_types": [],
                    "final_text": None}

    a, b = await asyncio.gather(
        one("count to 3 slowly", "concurrent-A"),
        one("name three colors", "concurrent-B"),
        return_exceptions=False,
    )

    print(f"\nCONCURRENT-A: turn_ids={a['turn_ids']}, "
          f"steps={a['step_count']}, err={a['err']}")
    print(f"CONCURRENT-B: turn_ids={b['turn_ids']}, "
          f"steps={b['step_count']}, err={b['err']}")

    a_tids = set(a["turn_ids"])
    b_tids = set(b["turn_ids"])
    overlap = a_tids & b_tids

    if a["err"] and "TurnInactive" in (a["err"] or ""):
        verdict = "REJECTS — second chat() raised CodexTurnInactiveError. _SPAWN_LOCK is correct defense; no harness change needed."
    elif b["err"] and "TurnInactive" in (b["err"] or ""):
        verdict = "REJECTS — second chat() raised CodexTurnInactiveError. _SPAWN_LOCK is correct defense; no harness change needed."
    elif a["err"] or b["err"]:
        verdict = f"ERROR — at least one turn errored. a.err={a['err']!r} b.err={b['err']!r}. Inspect manually."
    elif overlap:
        verdict = (
            f"INTERLEAVED — turn_ids overlap: {overlap}. The SDK mixes "
            "notifications across concurrent turns. handle_step MUST add "
            "a turn_id check; otherwise tool-results/usage may attach to "
            "the wrong turn under any rare _SPAWN_LOCK breach."
        )
    elif not a_tids or not b_tids:
        verdict = "INCONCLUSIVE — at least one turn produced no turn_ids. Inspect manually."
    elif a["step_count"] == 0 or b["step_count"] == 0:
        verdict = "SERIALIZES (apparent) — one turn produced no steps; the SDK probably blocked it. _SPAWN_LOCK alignment good."
    else:
        verdict = (
            f"SERIALIZES — both turns produced disjoint turn_ids "
            f"({a_tids} vs {b_tids}). The SDK ran them sequentially "
            "internally. _SPAWN_LOCK matches SDK behavior; defense-in-depth."
        )
    print(f"\nVERDICT: {verdict}")
    return verdict


async def main() -> int:
    _hr("open client + thread")
    try:
        client = await _open_client()
    except ImportError:
        print("FAIL: codex_app_server_sdk not installed")
        traceback.print_exc()
        return 1
    except Exception:
        print("FAIL: opening client")
        traceback.print_exc()
        return 1

    try:
        thread = client.start_thread()
        if hasattr(thread, "__await__"):
            thread = await thread
        print(f"thread_id: {thread.thread_id}")

        ok_a = await _check_a_sequential(thread)
        verdict_b = await _check_b_concurrent(thread)
    finally:
        try:
            r = client.close()
            if hasattr(r, "__await__"):
                await r
        except Exception:
            traceback.print_exc()

    _hr("summary")
    print(f"  CHECK A (sequential):  {'PASSED' if ok_a else 'FAILED'}")
    print(f"  CHECK B (concurrent):  {verdict_b}")

    print(
        "\nFeed CHECK B's verdict into Docs/CODEX_AUDIT.md item #29:\n"
        "  - REJECTS / SERIALIZES → mark 'completed and audited'\n"
        "  - INTERLEAVED → add turn_id check to handle_step before closing\n"
        "  - INCONCLUSIVE / ERROR → re-run, inspect output\n"
    )
    return 0 if ok_a else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
