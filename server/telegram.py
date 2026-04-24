"""Telegram bot bridge for talking to Coach from a phone.

Config is loaded from the encrypted `secrets` store (UI-managed) with
fallback to env vars for backwards compat:

    secret name `telegram_bot_token`         (env: TELEGRAM_BOT_TOKEN)
    secret name `telegram_allowed_chat_ids`  (env: TELEGRAM_ALLOWED_CHAT_IDS)

The chat IDs are a comma-separated whitelist of integers. The bridge
refuses to start if a token is set without a whitelist (anyone who
finds the bot could otherwise pilot Coach).

Lifecycle: `start_telegram_bridge()` is awaited from the FastAPI
lifespan. The /api/team/telegram endpoints call `reload_telegram_bridge()`
after writing config so the user doesn't need to redeploy. The module
owns the task handle internally — lifespan stops the bridge on shutdown
via `stop_telegram_bridge()`.

Flow:

  inbound : telegram → getUpdates → _post_human_message(to='coach') →
            existing maybe_wake_agent path spawns Coach's turn

  outbound: bus.subscribe() → buffer agent_id='coach' text events →
            on agent_stopped, flush accumulated text to every allowed
            chat_id (one message per chunk of ≤ 4000 chars)

Long-polling is deliberate over webhooks: works behind Zeabur TLS with
no public-URL plumbing, and the harness already does plenty of
background asyncio work.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from server.db import configured_conn
from server.events import bus

logger = logging.getLogger("harness.telegram")

_API_BASE = "https://api.telegram.org"
_POLL_TIMEOUT = 25  # seconds; Telegram's long-poll
_MAX_MSG_CHARS = 4000  # Telegram caps at 4096; leave headroom for prefix
_INBOUND_BODY_MAX = 5000  # mirrors HumanMessageRequest cap

# Stop hammering Telegram (and the log) after this many consecutive
# auth-failures (401/403). Escalates via `human_attention` and exits the
# poll loop; user must save fresh config to retry.
_AUTH_FAIL_LIMIT = 5

SECRET_TOKEN_NAME = "telegram_bot_token"
SECRET_CHAT_IDS_NAME = "telegram_allowed_chat_ids"
DISABLED_FLAG_KEY = "telegram_disabled"  # team_config; "1" == bridge off

# Bot token shape per BotFather: <bot_id>:<35-char auth>. Liberal regex
# so future format tweaks don't lock users out.
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_\-]{30,}$")


def is_valid_token(token: str) -> bool:
    """True iff `token` looks like a Telegram bot token."""
    return bool(_TOKEN_RE.match((token or "").strip()))

# Module-owned task handle. start/stop/reload mutate it under the lock so
# concurrent /api/team/telegram requests don't race the lifespan.
_current_task: asyncio.Task[None] | None = None
_lock = asyncio.Lock()


def _parse_chat_ids(raw: str) -> set[int]:
    """Parse a CSV of integer chat_ids, ignoring blank / non-numeric entries."""
    out: set[int] = set()
    for part in (raw or "").split(","):
        p = part.strip()
        if not p:
            continue
        try:
            out.add(int(p))
        except ValueError:
            logger.warning("ignoring non-integer chat_id in whitelist: %r", p)
    return out


async def _read_token() -> tuple[str, str]:
    """Return (token, source) where source ∈ {'db', 'env', 'unset'}."""
    from server import secrets as secrets_store
    val = await secrets_store.get_secret(SECRET_TOKEN_NAME)
    if val:
        return val.strip(), "db"
    env = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if env:
        return env, "env"
    return "", "unset"


async def _read_chat_ids() -> tuple[set[int], str]:
    """Return (chat_ids, source) where source ∈ {'db', 'env', 'unset'}."""
    from server import secrets as secrets_store
    val = await secrets_store.get_secret(SECRET_CHAT_IDS_NAME)
    if val:
        return _parse_chat_ids(val), "db"
    env = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if env:
        return _parse_chat_ids(env), "env"
    return set(), "unset"


async def _read_disabled_flag() -> bool:
    """True when team_config['telegram_disabled'] == '1'. Set by the
    Clear button so env-var fallback doesn't silently re-enable the
    bridge after the user explicitly turned it off."""
    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT value FROM team_config WHERE key = ?", (DISABLED_FLAG_KEY,)
        )
        row = await cur.fetchone()
    finally:
        await c.close()
    return bool(row and dict(row).get("value") == "1")


async def _set_disabled_flag(disabled: bool) -> None:
    """Upsert the disabled flag. True writes '1', False deletes the row."""
    c = await configured_conn()
    try:
        if disabled:
            await c.execute(
                "INSERT INTO team_config (key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET "
                "  value = excluded.value, "
                "  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
                (DISABLED_FLAG_KEY,),
            )
        else:
            await c.execute(
                "DELETE FROM team_config WHERE key = ?", (DISABLED_FLAG_KEY,)
            )
        await c.commit()
    finally:
        await c.close()


async def _resolve_config() -> tuple[str, set[int]] | None:
    """Read full config. Return (token, allowed_chat_ids) or None when
    the bridge is disabled or misconfigured."""
    if await _read_disabled_flag():
        return None
    token, _ = await _read_token()
    if not token:
        return None
    allowed, _ = await _read_chat_ids()
    if not allowed:
        logger.error(
            "telegram bridge: token set but allowed_chat_ids empty — "
            "refusing to start (anyone who finds the bot could pilot Coach)"
        )
        return None
    return token, allowed


def _split_chunks(text: str, n: int = _MAX_MSG_CHARS) -> list[str]:
    """Split on paragraph/line boundaries when possible, hard-cut otherwise."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= n:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > n:
        cut = remaining.rfind("\n\n", 0, n)
        if cut < n // 2:
            cut = remaining.rfind("\n", 0, n)
        if cut < n // 2:
            cut = n
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _post_human_message(chat_id: int, body: str) -> None:
    """Insert into messages table, emit event, wake Coach. Mirrors the
    /api/messages POST handler so we get the existing auto-wake for free."""
    body = body.strip()[:_INBOUND_BODY_MAX]
    if not body:
        return
    c = await configured_conn()
    try:
        cur = await c.execute(
            "INSERT INTO messages (from_id, to_id, subject, body, priority) "
            "VALUES ('human', 'coach', ?, ?, 'normal') RETURNING id",
            (f"telegram:{chat_id}", body),
        )
        row = await cur.fetchone()
        msg_id = dict(row)["id"] if row else None
        await c.commit()
    finally:
        await c.close()

    await bus.publish(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent_id": "human",
            "type": "message_sent",
            "message_id": msg_id,
            "to": "coach",
            "subject": f"telegram:{chat_id}",
            "body_preview": body[:120],
            "priority": "normal",
        }
    )

    # Late import — avoids a startup-time circular reference between
    # server.telegram and server.agents.
    from server.agents import maybe_wake_agent

    preview = body.replace("\n", " ")[:240]
    await maybe_wake_agent(
        "coach",
        f'New message from the human (via Telegram): "{preview}"\n\n'
        f"Call coord_read_inbox to mark it read and see any other "
        f"queued messages, then respond.",
        bypass_debounce=True,
    )


async def _send_telegram(
    client: httpx.AsyncClient, token: str, chat_id: int, text: str
) -> None:
    """POST to sendMessage. Splits long replies into multiple messages."""
    chunks = _split_chunks(text)
    for chunk in chunks:
        try:
            r = await client.post(
                f"{_API_BASE}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=30,
            )
            if r.status_code != 200:
                logger.warning(
                    "telegram sendMessage chat=%d status=%d body=%s",
                    chat_id, r.status_code, r.text[:200],
                )
        except Exception:
            logger.exception("telegram sendMessage chat=%d failed", chat_id)


async def _outbound_loop(
    client: httpx.AsyncClient, token: str, allowed: set[int]
) -> None:
    """Subscribe to the bus, buffer Coach text per turn, flush on stop.

    Forwarding rule: only flush turns triggered by a human message
    (UI composer or Telegram inbound). Autoloop / Player-driven turns
    are silent so the phone doesn't ping for routine internal chatter.
    `human_attention` is always forwarded regardless.

    Mechanism: `pending_user_msg` flips True on every
    `message_sent` event with `agent_id='human'` and `to='coach'`.
    On the next `agent_started(coach)` we capture that into the
    per-turn `forward_this_turn` decision and clear `pending_user_msg`.
    `agent_stopped(coach)` flushes only if `forward_this_turn`.
    """
    q = bus.subscribe()
    buffer: list[str] = []
    pending_user_msg = False
    forward_this_turn = False
    try:
        while True:
            evt = await q.get()
            etype = evt.get("type")
            agent = evt.get("agent_id")

            if etype == "message_sent" and agent == "human" and evt.get("to") == "coach":
                pending_user_msg = True
                continue

            if etype == "human_attention":
                subj = evt.get("subject") or "(no subject)"
                body = evt.get("body") or ""
                urgency = evt.get("urgency") or "normal"
                msg = f"[!] Human attention ({urgency}): {subj}\n\n{body}"
                for chat_id in allowed:
                    await _send_telegram(client, token, chat_id, msg)
                continue

            if agent != "coach":
                continue

            if etype == "text":
                content = (evt.get("content") or "").strip()
                if content:
                    buffer.append(content)
            elif etype == "agent_started":
                buffer.clear()
                forward_this_turn = pending_user_msg
                pending_user_msg = False
            elif etype == "agent_stopped":
                if buffer and forward_this_turn:
                    text = "\n\n".join(buffer)
                    for chat_id in allowed:
                        await _send_telegram(client, token, chat_id, text)
                buffer.clear()
                forward_this_turn = False
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("telegram outbound loop crashed (will not restart)")
    finally:
        bus.unsubscribe(q)


async def _escalate_auth_failure(reason: str) -> None:
    """Emit a `human_attention` event so the user sees the bridge
    needs intervention. Called once per outage, not per failed call."""
    await bus.publish({
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent_id": "system",
        "type": "human_attention",
        "subject": "Telegram bridge stopped — auth failure",
        "body": (
            f"{reason}\n\n"
            "Open Options → Telegram bridge and either re-paste a valid "
            "bot token (rotated via @BotFather?) or click Clear to "
            "disable the bridge."
        ),
        "urgency": "normal",
    })


async def _inbound_loop(
    client: httpx.AsyncClient, token: str, allowed: set[int]
) -> None:
    """Long-poll getUpdates, dispatch each whitelisted text message to Coach.

    Stops the loop after `_AUTH_FAIL_LIMIT` consecutive 401/403 responses
    so a stale token doesn't spam the log forever — emits a
    `human_attention` event before exiting so the user notices.
    """
    offset: int | None = None
    backoff = 1.0
    auth_fails = 0
    while True:
        try:
            params: dict[str, Any] = {
                "timeout": _POLL_TIMEOUT,
                "allowed_updates": ["message"],
            }
            if offset is not None:
                params["offset"] = offset
            r = await client.get(
                f"{_API_BASE}/bot{token}/getUpdates",
                params=params,
                timeout=_POLL_TIMEOUT + 10,
            )
            if r.status_code in (401, 403):
                auth_fails += 1
                logger.warning(
                    "telegram getUpdates auth failure %d/%d status=%d",
                    auth_fails, _AUTH_FAIL_LIMIT, r.status_code,
                )
                if auth_fails >= _AUTH_FAIL_LIMIT:
                    await _escalate_auth_failure(
                        f"getUpdates returned {r.status_code} "
                        f"{_AUTH_FAIL_LIMIT} times in a row — token rejected."
                    )
                    return
                await asyncio.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2, 30.0)
                continue
            if r.status_code != 200:
                logger.warning(
                    "telegram getUpdates status=%d body=%s",
                    r.status_code, r.text[:200],
                )
                await asyncio.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            auth_fails = 0
            data = r.json()
            if not data.get("ok"):
                logger.warning("telegram getUpdates not ok: %s", data)
                await asyncio.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = max(offset or 0, upd["update_id"] + 1)
                msg = upd.get("message")
                if not msg:
                    continue
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if not isinstance(chat_id, int) or chat_id not in allowed:
                    logger.info(
                        "telegram inbound rejected: chat_id=%r not whitelisted",
                        chat_id,
                    )
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    await _send_telegram(
                        client, token, chat_id,
                        "Only text messages are supported for now.",
                    )
                    continue
                if text.startswith("/start"):
                    await _send_telegram(
                        client, token, chat_id,
                        "Connected to TeamOfTen. Anything you send here "
                        "goes to Coach. Coach's replies come back to you "
                        "when each turn finishes.",
                    )
                    continue
                await _post_human_message(chat_id, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("telegram inbound loop hiccup (will retry)")
            await asyncio.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2, 30.0)


async def _run(token: str, allowed: set[int]) -> None:
    """Run inbound + outbound loops concurrently with a shared HTTP client.

    If either loop exits (e.g. inbound bails out after persistent
    auth failure), the other is cancelled so the bridge stops cleanly
    instead of half-running.
    """
    async with httpx.AsyncClient() as client:
        inbound = asyncio.create_task(_inbound_loop(client, token, allowed))
        outbound = asyncio.create_task(_outbound_loop(client, token, allowed))
        try:
            await asyncio.wait(
                {inbound, outbound}, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (inbound, outbound):
                if not t.done():
                    t.cancel()
            for t in (inbound, outbound):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


async def start_telegram_bridge() -> bool:
    """Spawn the bridge task if config is valid. Idempotent: a second
    call while a task is already running is a no-op (use reload to
    pick up new config). Returns True when a task is now running."""
    global _current_task
    async with _lock:
        if _current_task and not _current_task.done():
            return True
        cfg = await _resolve_config()
        if cfg is None:
            return False
        token, allowed = cfg
        logger.info(
            "telegram bridge starting (allowed chats: %d)", len(allowed),
        )
        _current_task = asyncio.create_task(_run(token, allowed))
        return True


async def stop_telegram_bridge() -> None:
    """Cancel the running bridge task, if any. Safe to call when nothing
    is running. Awaits cleanup so the next start gets a fresh poll cursor."""
    global _current_task
    async with _lock:
        task = _current_task
        _current_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def reload_telegram_bridge() -> bool:
    """Stop the current task (if any) and start a fresh one with the
    latest config. Returns True if a task is running afterwards."""
    await stop_telegram_bridge()
    return await start_telegram_bridge()


def is_running() -> bool:
    """Whether a bridge task is alive right now. Used by the status
    endpoint so the UI shows 'active' vs 'disabled' truthfully."""
    t = _current_task
    return bool(t and not t.done())
