"""PR 5 — Codex runtime feature-flag gate.

Per Docs/CODEX_RUNTIME_SPEC.md §K PR 5: `HARNESS_CODEX_ENABLED=true`
env gate; `PUT /api/agents/{id}/runtime` rejects `codex` when the
flag is unset.
"""

from __future__ import annotations

from server.runtimes import CodexRuntime, get_runtime, is_codex_enabled
from server.runtimes.base import AgentRuntime


def test_codex_runtime_satisfies_protocol() -> None:
    rt = CodexRuntime()
    assert isinstance(rt, AgentRuntime)
    assert rt.name == "codex"


def test_get_runtime_resolves_codex() -> None:
    rt = get_runtime("codex")
    assert rt.name == "codex"


def test_is_codex_enabled_default_off(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_CODEX_ENABLED", raising=False)
    assert is_codex_enabled() is False


def test_is_codex_enabled_recognizes_truthy_values(monkeypatch) -> None:
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("HARNESS_CODEX_ENABLED", value)
        assert is_codex_enabled() is True


def test_is_codex_enabled_rejects_falsy_values(monkeypatch) -> None:
    for value in ("0", "false", "no", "off", "", "maybe"):
        monkeypatch.setenv("HARNESS_CODEX_ENABLED", value)
        assert is_codex_enabled() is False


async def test_codex_resolve_auth_returns_none_when_unset(
    monkeypatch,
    tmp_path,
) -> None:
    """No ChatGPT session file, no saved API key → ('none', {})."""
    from server.runtimes.codex import resolve_auth
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # empty dir, no auth.json
    method, env = await resolve_auth()
    assert method == "none"
    assert env == {}


async def test_codex_resolve_auth_chatgpt_session(monkeypatch, tmp_path) -> None:
    """auth.json present and non-empty → ('chatgpt', {})."""
    from server.runtimes.codex import resolve_auth
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"some": "session"}')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    method, env = await resolve_auth()
    assert method == "chatgpt"
    assert env == {}


async def test_codex_resolve_auth_chatgpt_empty_file_falls_through(
    monkeypatch,
    tmp_path,
    fresh_db,
) -> None:
    """Empty auth.json should NOT count as a valid session — fall
    through to API-key resolution. Requires fresh DB so the secrets
    store import path resolves cleanly."""
    import server.db as dbmod
    await dbmod.init_db()
    from server.runtimes.codex import resolve_auth
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("")  # empty
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    method, env = await resolve_auth()
    # No api key set → none
    assert method == "none"
    assert env == {}


async def test_codex_resolve_auth_api_key_fallback(
    monkeypatch,
    tmp_path,
    fresh_db,
) -> None:
    """No ChatGPT session, but secrets.openai_api_key set → returns
    api_key + OPENAI_API_KEY env override."""
    import server.db as dbmod
    await dbmod.init_db()
    monkeypatch.setenv(
        "HARNESS_SECRETS_KEY",
        "GsTLxlpTvgYFjJxkhBcGWpXFkHjMVlkJxmJgJmBtmJ8=",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # empty dir

    from server import secrets as secrets_store
    secrets_store.bump_cache_version()
    ok = await secrets_store.set_secret("openai_api_key", "sk-test-fake")
    assert ok

    from server.runtimes.codex import resolve_auth
    method, env = await resolve_auth()
    assert method == "api_key"
    assert env == {"OPENAI_API_KEY": "sk-test-fake"}


async def test_codex_maybe_auto_compact_returns_false() -> None:
    """v1 has no Codex auto-compact — context-pressure signal isn't
    exposed yet."""
    from server.runtimes.base import TurnContext

    rt = CodexRuntime()
    tc = TurnContext(
        agent_id="p1",
        project_id="default",
        prompt="hi",
        system_prompt="",
        workspace_cwd="",
        allowed_tools=[],
        external_mcp_servers={},
    )
    assert await rt.maybe_auto_compact(tc) is False


# ---- HTTP endpoint env-gate coverage (skipped when fastapi missing) ----


async def test_runtime_endpoint_rejects_codex_when_flag_unset(
    fresh_db: str,
    monkeypatch,
) -> None:
    """PR 5 gate — `PUT /api/agents/{id}/runtime` must 400 on
    `runtime=codex` when HARNESS_CODEX_ENABLED is unset, even if the
    user's HARNESS_TOKEN is correct.
    """
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_CODEX_ENABLED", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await dbmod.init_db()
    with TestClient(mainmod.app) as c:
        resp = c.put(
            "/api/agents/p1/runtime",
            json={"runtime": "codex"},
        )
        assert resp.status_code == 400, resp.text
        assert "codex" in resp.json().get("detail", "").lower()


async def test_runtime_endpoint_accepts_codex_when_flag_set(
    fresh_db: str,
    monkeypatch,
) -> None:
    """Same endpoint, flag flipped on → 200 + persisted."""
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.setenv("HARNESS_CODEX_ENABLED", "true")
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await dbmod.init_db()
    with TestClient(mainmod.app) as c:
        resp = c.put(
            "/api/agents/p1/runtime",
            json={"runtime": "codex"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("runtime_override") == "codex"


# ---- /api/team/codex endpoint family (audit-item-5) ----


async def test_team_codex_get_returns_status_when_unset(
    fresh_db: str,
    monkeypatch,
) -> None:
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_CODEX_ENABLED", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    await dbmod.init_db()
    with TestClient(mainmod.app) as c:
        resp = c.get("/api/team/codex")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["enabled"] is False
        assert body["chatgpt_session_present"] is False
        assert body["api_key_set"] is False
        assert body["method"] == "none"
        assert body["secret_name"] == "openai_api_key"


async def test_team_codex_put_rejects_empty(
    fresh_db: str,
    monkeypatch,
) -> None:
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    # Provide a master key so the secrets store reports OK.
    monkeypatch.setenv("HARNESS_SECRETS_KEY", "GsTLxlpTvgYFjJxkhBcGWpXFkHjMVlkJxmJgJmBtmJ8=")
    await dbmod.init_db()
    with TestClient(mainmod.app) as c:
        # Missing api_key field
        r1 = c.put("/api/team/codex", json={})
        assert r1.status_code == 400

        # Empty string
        r2 = c.put("/api/team/codex", json={"api_key": ""})
        assert r2.status_code == 400

        # Wrong shape — must look like an OpenAI key
        r3 = c.put("/api/team/codex", json={"api_key": "tg_xxxxx"})
        assert r3.status_code == 400
        assert "api_key" in r3.json().get("detail", "").lower()


async def test_team_codex_put_then_get_then_delete_round_trip(
    fresh_db: str,
    monkeypatch,
) -> None:
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    monkeypatch.setenv("HARNESS_SECRETS_KEY", "GsTLxlpTvgYFjJxkhBcGWpXFkHjMVlkJxmJgJmBtmJ8=")
    await dbmod.init_db()
    with TestClient(mainmod.app) as c:
        r_put = c.put("/api/team/codex", json={"api_key": "sk-test-fake-key-1234"})
        assert r_put.status_code == 200, r_put.text

        r_get = c.get("/api/team/codex")
        assert r_get.status_code == 200
        body = r_get.json()
        assert body["api_key_set"] is True
        assert body["method"] == "api_key"
        # Must NEVER include the plaintext.
        assert "sk-test-fake-key-1234" not in r_get.text

        r_del = c.delete("/api/team/codex")
        assert r_del.status_code == 200

        r_get2 = c.get("/api/team/codex")
        assert r_get2.json()["api_key_set"] is False


async def test_runtime_endpoint_accepts_claude_regardless_of_flag(
    fresh_db: str,
    monkeypatch,
) -> None:
    """`runtime=claude` and `runtime=null` (clear) must work even
    with the Codex gate off — the gate only restricts codex."""
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    from fastapi.testclient import TestClient
    import server.main as mainmod

    monkeypatch.delenv("HARNESS_CODEX_ENABLED", raising=False)
    monkeypatch.delenv("HARNESS_TOKEN", raising=False)
    await dbmod.init_db()
    with TestClient(mainmod.app) as c:
        r1 = c.put("/api/agents/p1/runtime", json={"runtime": "claude"})
        assert r1.status_code == 200
        assert r1.json().get("runtime_override") == "claude"

        r2 = c.put("/api/agents/p1/runtime", json={"runtime": ""})
        assert r2.status_code == 200
        assert r2.json().get("runtime_override") is None


# Audit item #8 — `_codex_clients` lifecycle cache.
# Stubs `codex_app_server_sdk` so the test can run anywhere — the live
# SDK shape is pinned in Docs/CODEX_PROBE_OUTPUT.md.

class _FakeClient:
    """Minimal CodexClient stand-in. Records start/initialize/close
    calls and the env that connect_stdio was given."""

    instances: list["_FakeClient"] = []

    def __init__(self, *, command, cwd, env, **kwargs) -> None:
        self.command = command
        self.cwd = cwd
        self.env = env
        self.started = 0
        self.initialized = 0
        self.closed = 0
        self.fail_on_initialize = False
        _FakeClient.instances.append(self)

    @classmethod
    def connect_stdio(cls, **kwargs):
        return cls(**kwargs)

    def start(self):
        self.started += 1
        return self

    def initialize(self):
        self.initialized += 1
        if self.fail_on_initialize:
            raise RuntimeError("simulated initialize failure")
        return object()

    def close(self):
        self.closed += 1


class _FakeSdk:
    CodexClient = _FakeClient


def _install_fake_sdk(monkeypatch):
    """Replace `_import_codex_sdk` so the cache helpers don't try to
    import the real SDK during tests."""
    _FakeClient.instances.clear()
    from server.runtimes import codex as codex_mod
    codex_mod._codex_clients.clear()
    codex_mod._client_locks.clear()
    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _FakeSdk)


async def test_get_client_caches_per_slot(monkeypatch, tmp_path) -> None:
    _install_fake_sdk(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:/usr/local/bin")  # known os.environ key
    from server.runtimes.codex import get_client

    c1 = await get_client("p1", cwd=str(tmp_path), env_overrides={"X": "1"})
    c2 = await get_client("p1", cwd=str(tmp_path), env_overrides={"X": "1"})
    assert c1 is c2, "same slot must return the cached client"
    assert len(_FakeClient.instances) == 1
    inst = _FakeClient.instances[0]
    # Verify connect_stdio was invoked with the spec-correct command/cwd
    # and the env was os.environ + overrides (not overrides alone).
    assert inst.command == ["codex", "app-server"]
    assert inst.cwd == str(tmp_path)
    assert inst.env.get("X") == "1"
    assert inst.env.get("PATH") == "/usr/bin:/usr/local/bin"
    # start + initialize each ran exactly once during construction.
    assert c1.started == 1
    assert c1.initialized == 1


async def test_get_client_separate_slots_get_separate_clients(
    monkeypatch, tmp_path,
) -> None:
    _install_fake_sdk(monkeypatch)
    from server.runtimes.codex import get_client

    a = await get_client("p1", cwd=str(tmp_path))
    b = await get_client("p2", cwd=str(tmp_path))
    assert a is not b
    assert len(_FakeClient.instances) == 2


async def test_close_client_drops_and_closes(monkeypatch, tmp_path) -> None:
    _install_fake_sdk(monkeypatch)
    from server.runtimes.codex import get_client, close_client
    from server.runtimes import codex as codex_mod

    client = await get_client("p1", cwd=str(tmp_path))
    await close_client("p1")
    assert client.closed == 1
    assert "p1" not in codex_mod._codex_clients

    # Calling close on an already-empty slot is a no-op (no exception).
    await close_client("p1")
    assert client.closed == 1


async def test_close_all_clients(monkeypatch, tmp_path) -> None:
    _install_fake_sdk(monkeypatch)
    from server.runtimes.codex import get_client, close_all_clients
    from server.runtimes import codex as codex_mod

    a = await get_client("p1", cwd=str(tmp_path))
    b = await get_client("p2", cwd=str(tmp_path))
    await close_all_clients()
    assert a.closed == 1 and b.closed == 1
    assert codex_mod._codex_clients == {}


async def test_failed_handshake_does_not_poison_cache(
    monkeypatch, tmp_path,
) -> None:
    """A construction error mid-handshake (e.g. initialize raises) must
    not cache a half-open client. Next get_client should rebuild."""
    import pytest
    _install_fake_sdk(monkeypatch)
    from server.runtimes.codex import get_client
    from server.runtimes import codex as codex_mod

    # Patch the FakeClient to fail on initialize for the first attempt.
    original_init = _FakeClient.__init__

    def init_with_failure(self, **kwargs):
        original_init(self, **kwargs)
        if len(_FakeClient.instances) == 1:
            self.fail_on_initialize = True

    monkeypatch.setattr(_FakeClient, "__init__", init_with_failure)

    with pytest.raises(RuntimeError, match="simulated initialize failure"):
        await get_client("p1", cwd=str(tmp_path))

    # First client was closed best-effort during the rollback, NOT cached.
    assert "p1" not in codex_mod._codex_clients
    assert _FakeClient.instances[0].closed == 1

    # Second attempt rebuilds successfully.
    client = await get_client("p1", cwd=str(tmp_path))
    assert client is _FakeClient.instances[1]
    assert client.initialized == 1
    assert codex_mod._codex_clients["p1"] is client


# Audit item #9 — codex_thread_id persistence + open_thread auto-heal.

class _FakeThread:
    instances: list["_FakeThread"] = []

    def __init__(self, thread_id: str = "thread_new", config=None) -> None:
        self.thread_id = thread_id
        self.config = config
        _FakeThread.instances.append(self)


class _ThreadFakeClient:
    """Client stub that records start/resume calls and can be configured
    to fail on resume. Independent of `_FakeClient` to keep the lifecycle
    + thread tests decoupled."""

    def __init__(self) -> None:
        self.start_calls: list = []
        self.resume_calls: list = []
        self.fail_resume_with: Exception | None = None
        self._counter = 0

    def start_thread(self, config=None):
        self._counter += 1
        self.start_calls.append({"config": config})
        return _FakeThread(thread_id=f"thread_new_{self._counter}", config=config)

    def resume_thread(self, thread_id: str, *, overrides=None):
        self.resume_calls.append({"thread_id": thread_id, "overrides": overrides})
        if self.fail_resume_with is not None:
            raise self.fail_resume_with
        return _FakeThread(thread_id=thread_id, config=overrides)


async def test_get_set_clear_codex_thread_id_round_trip(fresh_db) -> None:
    import server.db as dbmod
    from server.runtimes.codex import (
        _get_codex_thread_id, _set_codex_thread_id, _clear_codex_thread_id,
    )
    await dbmod.init_db()

    assert (await _get_codex_thread_id("p1")) is None

    await _set_codex_thread_id("p1", "thread_abc")
    assert (await _get_codex_thread_id("p1")) == "thread_abc"

    await _clear_codex_thread_id("p1")
    assert (await _get_codex_thread_id("p1")) is None


async def test_set_codex_thread_id_ignores_none_and_system(fresh_db) -> None:
    import server.db as dbmod
    from server.runtimes.codex import _get_codex_thread_id, _set_codex_thread_id
    await dbmod.init_db()

    await _set_codex_thread_id("p1", None)  # no-op
    assert (await _get_codex_thread_id("p1")) is None

    await _set_codex_thread_id("system", "thread_x")  # ignored
    assert (await _get_codex_thread_id("system")) is None


async def test_open_thread_starts_fresh_when_no_stored_id(fresh_db) -> None:
    import server.db as dbmod
    from server.runtimes.codex import open_thread
    await dbmod.init_db()
    _FakeThread.instances.clear()
    client = _ThreadFakeClient()

    thread, resumed = await open_thread("p1", client)
    assert resumed is False
    assert client.start_calls == [{"config": None}]
    assert client.resume_calls == []
    assert thread.thread_id == "thread_new_1"


async def test_open_thread_resumes_when_stored_id_present(fresh_db) -> None:
    import server.db as dbmod
    from server.runtimes.codex import open_thread, _set_codex_thread_id
    await dbmod.init_db()
    _FakeThread.instances.clear()
    client = _ThreadFakeClient()

    await _set_codex_thread_id("p1", "thread_existing")
    thread, resumed = await open_thread("p1", client)
    assert resumed is True
    assert client.resume_calls == [
        {"thread_id": "thread_existing", "overrides": None}
    ]
    assert client.start_calls == []
    assert thread.thread_id == "thread_existing"


async def test_open_thread_falls_back_to_start_on_resume_failure(
    fresh_db,
) -> None:
    """Stale-thread auto-heal: if resume_thread raises, clear the stored
    id and retry with start_thread once."""
    import server.db as dbmod
    from server.runtimes.codex import (
        open_thread, _set_codex_thread_id, _get_codex_thread_id,
    )
    await dbmod.init_db()
    _FakeThread.instances.clear()
    client = _ThreadFakeClient()
    client.fail_resume_with = RuntimeError("thread not found")

    await _set_codex_thread_id("p1", "thread_stale")
    thread, resumed = await open_thread("p1", client)

    # Resume was tried, then start was called as the fallback.
    assert client.resume_calls == [
        {"thread_id": "thread_stale", "overrides": None}
    ]
    assert client.start_calls == [{"config": None}]
    assert resumed is False, (
        "even though we tried resume first, the successful path was "
        "start_thread — UI should NOT show the resumed indicator"
    )
    assert thread.thread_id == "thread_new_1"

    # Stale id was nulled so the next turn doesn't re-trigger the same
    # failed resume.
    assert (await _get_codex_thread_id("p1")) is None


async def test_open_thread_propagates_cancellation_during_resume(
    fresh_db,
) -> None:
    """CancelledError must NOT trigger the auto-heal path. Cancellations
    are user/dispatcher intent, not stale-thread signals — clearing the
    stored id on cancel would lose context unnecessarily."""
    import asyncio
    import pytest
    import server.db as dbmod
    from server.runtimes.codex import (
        open_thread, _set_codex_thread_id, _get_codex_thread_id,
    )
    await dbmod.init_db()
    _FakeThread.instances.clear()
    client = _ThreadFakeClient()
    client.fail_resume_with = asyncio.CancelledError()

    await _set_codex_thread_id("p1", "tid_alive")
    with pytest.raises(asyncio.CancelledError):
        await open_thread("p1", client)

    # Stored id MUST still be intact after a cancellation.
    assert (await _get_codex_thread_id("p1")) == "tid_alive"
    # start_thread must NOT have been called as a fallback.
    assert client.start_calls == []


async def test_open_thread_passes_config_to_start_and_resume(
    fresh_db,
) -> None:
    import server.db as dbmod
    from server.runtimes.codex import open_thread, _set_codex_thread_id
    await dbmod.init_db()
    _FakeThread.instances.clear()
    client = _ThreadFakeClient()

    sentinel = object()  # stand-in for ThreadConfig
    thread, _ = await open_thread("p1", client, config=sentinel)
    assert client.start_calls == [{"config": sentinel}]

    await _set_codex_thread_id("p1", "tid")
    thread, _ = await open_thread("p1", client, config=sentinel)
    assert client.resume_calls[-1] == {"thread_id": "tid", "overrides": sentinel}


# Audit item #10 — ConversationStep → harness event mapping.
# Captures emitted events so we can assert what handle_step did.

class _FakeStep:
    """Stand-in for codex_app_server_sdk.ConversationStep. Mirrors the
    fields observed in the live spike (see Docs/CODEX_PROBE_OUTPUT.md)."""

    def __init__(
        self,
        *,
        step_type: str,
        item_type: str,
        item_id: str = "item_x",
        text: str | None = None,
        item: dict | None = None,
    ) -> None:
        self.thread_id = "thread_test"
        self.turn_id = "turn_test"
        self.item_id = item_id
        self.step_type = step_type
        self.item_type = item_type
        self.status = "completed"
        self.text = text
        self.data = {
            "params": {
                "item": item or {},
                "threadId": self.thread_id,
                "turnId": self.turn_id,
            },
            "item": item or {},
        }


def _capture_emit(monkeypatch):
    """Monkeypatch `server.agents._emit` to record calls in-memory."""
    captured: list[dict] = []

    async def fake_emit(agent_id, event_type, **payload):
        captured.append({"agent_id": agent_id, "type": event_type, **payload})

    import server.agents as agentsmod
    monkeypatch.setattr(agentsmod, "_emit", fake_emit)
    return captured


async def test_handle_step_skips_userMessage(monkeypatch) -> None:
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    step = _FakeStep(
        step_type="userMessage",
        item_type="userMessage",
        item={"type": "userMessage", "content": [{"type": "text", "text": "hi"}]},
    )
    ctx: dict = {}
    await handle_step(step, "p1", ctx)
    assert captured == []
    assert ctx == {}


async def test_handle_step_emits_text_for_agentMessage(monkeypatch) -> None:
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    step = _FakeStep(
        step_type="codex",
        item_type="agentMessage",
        item_id="msg_abc",
        text="hello",
        item={"type": "agentMessage", "id": "msg_abc", "text": "hello",
              "phase": "final_answer", "memoryCitation": None},
    )
    ctx: dict = {}
    await handle_step(step, "p1", ctx)

    assert len(captured) == 1
    assert captured[0]["type"] == "text"
    assert captured[0]["text"] == "hello"
    assert captured[0]["agent_id"] == "p1"
    # Final-answer phase flips got_result for the dispatcher.
    assert ctx.get("got_result") is True
    assert ctx.get("accumulated_text") == "hello"


async def test_handle_step_accumulates_text_across_steps(monkeypatch) -> None:
    """Streaming agentMessage steps before the final_answer should
    accumulate. got_result stays False until phase=='final_answer'."""
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    s1 = _FakeStep(
        step_type="codex", item_type="agentMessage", item_id="m",
        text="hel", item={"phase": "in_progress"},
    )
    s2 = _FakeStep(
        step_type="codex", item_type="agentMessage", item_id="m",
        text="lo", item={"phase": "final_answer"},
    )
    ctx: dict = {}
    await handle_step(s1, "p1", ctx)
    assert ctx.get("got_result") is not True  # not final yet
    assert ctx["accumulated_text"] == "hel"

    await handle_step(s2, "p1", ctx)
    assert ctx["accumulated_text"] == "hello"
    assert ctx["got_result"] is True
    assert [c["text"] for c in captured] == ["hel", "lo"]


async def test_handle_step_empty_text_is_noop(monkeypatch) -> None:
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    step = _FakeStep(
        step_type="codex", item_type="agentMessage",
        text=None, item={"phase": "in_progress"},
    )
    await handle_step(step, "p1", {})
    assert captured == []


async def test_handle_step_emits_tool_use_for_shell(monkeypatch) -> None:
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    item_payload = {
        "type": "shell",
        "id": "tool_42",
        "command": ["ls", "-la"],
        "cwd": "/workspaces/p1",
    }
    step = _FakeStep(
        step_type="codex", item_type="shell",
        item_id="tool_42", item=item_payload,
    )
    await handle_step(step, "p1", {})

    assert len(captured) == 1
    e = captured[0]
    assert e["type"] == "tool_use"
    assert e["tool"] == "Bash"
    assert e["id"] == "tool_42"
    # Permissive arg extraction: full item payload comes through under
    # `input` so existing renderers can pick keys they want.
    assert e["input"] == item_payload


async def test_handle_step_emits_tool_use_for_apply_patch(monkeypatch) -> None:
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    item_payload = {
        "type": "apply_patch",
        "id": "patch_1",
        "patch": "*** Begin Patch\n+hello\n*** End Patch",
        "path": "foo.py",
    }
    step = _FakeStep(
        step_type="codex", item_type="apply_patch",
        item_id="patch_1", item=item_payload,
    )
    await handle_step(step, "p1", {})
    assert captured[0]["tool"] == "Edit"
    assert captured[0]["input"] == item_payload


async def test_handle_step_emits_tool_use_for_web_search(monkeypatch) -> None:
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    item_payload = {"type": "web_search", "id": "ws_1", "query": "foo"}
    step = _FakeStep(
        step_type="codex", item_type="web_search",
        item_id="ws_1", item=item_payload,
    )
    await handle_step(step, "p1", {})
    assert captured[0]["tool"] == "WebSearch"
    assert captured[0]["input"] == item_payload


async def test_handle_step_emits_thinking_for_reasoning(monkeypatch) -> None:
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    step = _FakeStep(
        step_type="codex", item_type="reasoning",
        item={"summary": "considering options..."},
    )
    await handle_step(step, "p1", {})
    assert captured[0]["type"] == "thinking"
    assert captured[0]["text"] == "considering options..."


async def test_handle_step_unknown_item_type_logs_and_skips(monkeypatch) -> None:
    """Newer SDKs may add item types we haven't mapped yet — handle_step
    must NOT crash the turn for them."""
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    step = _FakeStep(
        step_type="codex", item_type="future_unmapped_type",
        item={"foo": "bar"},
    )
    await handle_step(step, "p1", {})
    assert captured == []  # skipped, no exception


async def test_handle_step_final_answer_flips_got_result_even_when_empty(
    monkeypatch,
) -> None:
    """A tool-only turn ending with empty-text final_answer must still
    flip got_result so the dispatcher's post-result handling triggers.
    Mirrors Claude's ResultMessage discipline: presence of the marker
    matters, not whether content is non-empty."""
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    step = _FakeStep(
        step_type="codex", item_type="agentMessage",
        text=None,
        item={"type": "agentMessage", "phase": "final_answer", "text": ""},
    )
    ctx: dict = {}
    await handle_step(step, "p1", ctx)

    assert ctx.get("got_result") is True
    assert captured == []  # no text emit (nothing to say)


async def test_handle_step_emits_mcp_tool_use_with_prefixed_name(
    monkeypatch,
) -> None:
    """Audit item #11. mcp_tool_call → tool_use(tool='mcp__<server>__<name>')
    so the existing renderers + tool-name allow-list logic keep working."""
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    item_payload = {
        "type": "mcp_tool_call",
        "id": "mcp_call_1",
        "server": "coord",
        "name": "coord_send_message",
        "args": {"to_id": "p2", "body": "hello"},
    }
    step = _FakeStep(
        step_type="codex", item_type="mcp_tool_call",
        item_id="mcp_call_1", item=item_payload,
    )
    await handle_step(step, "p1", {})

    assert len(captured) == 1
    assert captured[0]["type"] == "tool_use"
    assert captured[0]["tool"] == "mcp__coord__coord_send_message"
    assert captured[0]["id"] == "mcp_call_1"
    assert captured[0]["input"] == item_payload


async def test_handle_step_mcp_tool_use_falls_back_when_keys_missing(
    monkeypatch,
) -> None:
    """If probe-2 reveals the SDK uses different key names than we
    guessed (server/name), `_resolve_mcp_tool_name` should still produce
    a non-crashing name (`mcp__unknown__unknown`) so the UI shows
    *something* rather than emitting an error."""
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    step = _FakeStep(
        step_type="codex", item_type="mcp_tool_call",
        item={"type": "mcp_tool_call", "id": "x", "args": {}},
    )
    await handle_step(step, "p1", {})
    assert captured[0]["tool"] == "mcp__unknown__unknown"


async def test_resolve_mcp_tool_name_accepts_alternate_key_spellings() -> None:
    """Forward-compat for plausible alternate key names: server_name,
    mcp_server, tool_name, tool. Update once probe-2 confirms which the
    SDK actually emits."""
    from server.runtimes.codex import _resolve_mcp_tool_name

    assert (
        _resolve_mcp_tool_name({"server_name": "coord", "tool_name": "x"})
        == "mcp__coord__x"
    )
    assert (
        _resolve_mcp_tool_name({"mcp_server": "github", "tool": "search"})
        == "mcp__github__search"
    )


async def test_step_item_payload_falls_back_to_bare_item_key(
    monkeypatch,
) -> None:
    """If a future SDK build drops the params wrapper and only sets the
    bare `data['item']` key, _step_item_payload still finds the item."""
    captured = _capture_emit(monkeypatch)
    from server.runtimes.codex import handle_step

    class _StepNoParams:
        thread_id = "t"
        turn_id = "tu"
        item_id = "i_42"
        step_type = "codex"
        item_type = "shell"
        status = "completed"
        text = None
        # No 'params' wrapper — only the bare item key.
        data = {"item": {"command": ["ls"]}}

    await handle_step(_StepNoParams(), "p1", {})
    assert captured[0]["tool"] == "Bash"
    assert captured[0]["input"] == {"command": ["ls"]}
