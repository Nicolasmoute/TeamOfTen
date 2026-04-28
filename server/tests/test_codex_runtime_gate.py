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
    from server.runtimes.codex import get_client

    c1 = await get_client("p1", cwd=str(tmp_path), env_overrides={"X": "1"})
    c2 = await get_client("p1", cwd=str(tmp_path), env_overrides={"X": "1"})
    assert c1 is c2, "same slot must return the cached client"
    assert len(_FakeClient.instances) == 1
    # connect_stdio received the env-overrides merged on top of os.environ.
    assert _FakeClient.instances[0].env.get("X") == "1"
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

    import pytest
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
