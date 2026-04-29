"""PR 4 — coord_mcp proxy contract.

Per Docs/CODEX_RUNTIME_SPEC.md §J test_coord_mcp_proxy.py:
- enumerate the proxy tool catalog and assert it matches the
  in-process registry.
- token resolution / mismatch → 403.
- loopback bind check.

We don't spawn the actual stdio subprocess here (that would need an
event-loop pipeline + a live FastAPI bind). The contract under test
is at the boundary: the names returned by `coord_tool_names()` and
the dispatcher's behavior in `coord_proxy_call`. The subprocess in
`server.coord_mcp` is a thin stdio↔HTTP bridge over those.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import types
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import server.spawn_tokens as st
from server.tools import build_coord_server, coord_tool_names


def test_coord_tool_names_matches_in_process_registry() -> None:
    """The static name list the proxy advertises must match what the
    in-process server registers — drift here means a Codex agent
    would call a tool name that 404s on the dispatch endpoint.
    """
    names = coord_tool_names()
    server = build_coord_server("p1", include_proxy_metadata=True)
    assert "_handlers" in server, "proxy mode must stash _handlers"
    handler_names = set(server["_handlers"].keys())
    assert set(names) == handler_names, (
        f"proxy catalog drift: catalog={set(names)} handlers={handler_names}"
    )


def test_coord_server_default_has_no_non_json_proxy_metadata() -> None:
    """Claude receives the default coord server; it must not include
    function-valued proxy metadata because Claude serializes MCP config
    before spawning the CLI."""
    server = build_coord_server("coach")
    assert "_handlers" not in server
    assert "_tool_names" not in server
    assert not _contains_function(server)


def _contains_function(value: object) -> bool:
    if isinstance(value, types.FunctionType):
        return True
    if isinstance(value, dict):
        return any(
            _contains_function(k) or _contains_function(v)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return any(_contains_function(item) for item in value)
    return False


def test_coord_handlers_include_required_set() -> None:
    """Sanity floor: a handful of must-have coord tools are present.
    Catches the catastrophic case of the closure list silently
    losing entries.
    """
    names = set(coord_tool_names())
    required = {
        "coord_send_message",
        "coord_read_inbox",
        "coord_create_task",
        "coord_assign_task",
        "coord_update_memory",
        "coord_request_human",
    }
    missing = required - names
    assert not missing, f"missing required coord tools: {missing}"


async def test_coord_mcp_stdio_subprocess_lists_and_calls_tool() -> None:
    """Spawn the real coord_mcp stdio bridge and speak MCP to it."""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    token = "tok_coord_stdio_test"
    calls: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, body: dict[str, object]) -> None:
            raw = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path != "/api/_coord/_tools":
                self._send_json(404, {"detail": "not found"})
                return
            self._send_json(
                200,
                {
                    "tools": [
                        "coord_list_team",
                        "coord_send_message",
                        "coord_assign_task",
                    ]
                },
            )

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length).decode("utf-8")
            payload = json.loads(body) if body else {}
            calls.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": payload,
            })
            if self.headers.get("Authorization") != f"Bearer {token}":
                self._send_json(401, {"ok": False, "error": "bad token"})
                return
            if self.path != "/api/_coord/coord_list_team":
                if self.path == "/api/_coord/coord_send_message":
                    self._send_json(
                        403,
                        {"detail": "caller_id mismatch (token bound to 'coach')"},
                    )
                    return
                if self.path == "/api/_coord/coord_assign_task":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "result": {
                                "content": [
                                    {"type": "text", "text": "ERROR: task is not open"}
                                ],
                                "isError": True,
                            },
                        },
                    )
                    return
                self._send_json(404, {"ok": False, "error": "unknown"})
                return
            self._send_json(200, {"ok": True, "result": {"team": [{"id": "coach"}]}})

        def log_message(self, _fmt: str, *_args: object) -> None:
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        env = dict(os.environ)
        env["HARNESS_COORD_PROXY_TOKEN"] = token
        port = httpd.server_address[1]
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "server.coord_mcp",
                "--caller-id",
                "coach",
                "--proxy-url",
                f"http://127.0.0.1:{port}",
            ],
            env=env,
            cwd=os.getcwd(),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read,
                write,
                read_timeout_seconds=timedelta(seconds=10),
            ) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "coord-proxy"

                listed = await session.list_tools()
                assert [t.name for t in listed.tools] == [
                    "coord_list_team",
                    "coord_send_message",
                    "coord_assign_task",
                ]

                result = await session.call_tool("coord_list_team", {"verbose": True})
                assert result.isError is False
                assert json.loads(result.content[0].text) == {
                    "team": [{"id": "coach"}],
                }

                http_error = await session.call_tool(
                    "coord_send_message",
                    {"to": "p1", "body": "hello"},
                )
                assert http_error.isError is True
                assert "HTTP 403" in http_error.content[0].text
                assert "caller_id mismatch" in http_error.content[0].text

                tool_error = await session.call_tool(
                    "coord_assign_task",
                    {"task_id": "t-1", "to": "p1"},
                )
                assert tool_error.isError is True
                assert tool_error.content[0].text == "ERROR: task is not open"

        assert calls == [
            {
                "path": "/api/_coord/coord_list_team",
                "authorization": f"Bearer {token}",
                "payload": {
                    "caller_id": "coach",
                    "args": {"verbose": True},
                },
            },
            {
                "path": "/api/_coord/coord_send_message",
                "authorization": f"Bearer {token}",
                "payload": {
                    "caller_id": "coach",
                    "args": {"to": "p1", "body": "hello"},
                },
            },
            {
                "path": "/api/_coord/coord_assign_task",
                "authorization": f"Bearer {token}",
                "payload": {
                    "caller_id": "coach",
                    "args": {"task_id": "t-1", "to": "p1"},
                },
            },
        ]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_spawn_token_mint_resolve_round_trip() -> None:
    token = st.mint("p3")
    assert st.resolve(token) == "p3"


def test_spawn_token_revoke() -> None:
    token = st.mint("p3")
    st.revoke(token)
    assert st.resolve(token) is None


def test_spawn_token_revoke_for_caller() -> None:
    a = st.mint("p1")
    b = st.mint("p1")
    c = st.mint("p2")
    n = st.revoke_for_caller("p1")
    assert n == 2
    assert st.resolve(a) is None
    assert st.resolve(b) is None
    assert st.resolve(c) == "p2"
    st.revoke(c)


def test_spawn_token_unknown_returns_none() -> None:
    assert st.resolve("definitely-not-a-token") is None


async def test_dispatcher_does_not_touch_codex_proxy_token(
    fresh_db: str,
    monkeypatch,
) -> None:
    """The dispatcher must NOT mint or revoke the coord-proxy token
    for Codex turns. The codex app-server subprocess is cached per
    slot across turns and captures its env at first spawn, so a
    per-turn dispatcher mint/revoke would invalidate the in-flight
    subprocess's token after turn 1 (HTTP 401 on every subsequent
    `coord_*` call). The token's lifecycle is owned by CodexRuntime
    — minted in `get_client`, revoked in `close_client`. See the
    sibling test `test_codex_runtime_owns_proxy_token_lifecycle`.
    """
    import server.db as dbmod
    await dbmod.init_db()

    import server.agents as agentsmod
    from server.runtimes.base import TurnContext

    seen_token_in_turn_ctx: list[str | None] = []

    class StubCodexRuntime:
        name = "codex"

        async def maybe_auto_compact(self, tc: TurnContext) -> bool:
            return False

        async def run_turn(self, tc: TurnContext) -> None:
            seen_token_in_turn_ctx.append(tc.turn_ctx.get("coord_proxy_token"))

        async def run_manual_compact(self, tc: TurnContext) -> None:
            await self.run_turn(tc)

    async def _stub_resolve(_agent_id):
        return "codex"

    def _stub_get_runtime(name):
        if name == "codex":
            return StubCodexRuntime()
        raise ValueError(name)

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", _stub_resolve)
    import server.runtimes as runtimes_pkg
    monkeypatch.setattr(runtimes_pkg, "get_runtime", _stub_get_runtime)

    # Snapshot the token registry before the turn so we can assert
    # the dispatcher didn't add or remove anything.
    before = set(st._tokens.keys())

    await agentsmod.run_agent("p1", "hello")

    # Dispatcher must not have stashed a token on turn_ctx.
    assert seen_token_in_turn_ctx == [None]
    # Dispatcher must not have minted or revoked any token.
    assert set(st._tokens.keys()) == before


async def test_codex_runtime_owns_proxy_token_lifecycle(monkeypatch) -> None:
    """`get_client` mints a token and stashes it in
    `_codex_client_tokens`; `_coord_mcp_env` reads from there so the
    same token reaches the coord_mcp subprocess every turn;
    `close_client` revokes the token. This is what guarantees the
    cached codex app-server subprocess never sees its token expire
    mid-life.
    """
    import server.runtimes.codex as codex_mod
    from server.runtimes.base import TurnContext

    # Fake CodexClient: we only need start()/initialize()/close().
    class _FakeClient:
        def start(self): return None
        def initialize(self): return None
        def close(self): return None

    class _FakeSdk:
        @staticmethod
        def CodexClient():
            return None

    # Stub `connect_stdio` and the SDK importer so get_client never
    # actually launches a subprocess.
    captured_env: dict[str, str] = {}

    def _connect_stdio(*, command, cwd, env):
        captured_env.update(env)
        return _FakeClient()
    _FakeSdk.CodexClient = type("CC", (), {"connect_stdio": staticmethod(_connect_stdio)})

    monkeypatch.setattr(codex_mod, "_import_codex_sdk", lambda: _FakeSdk)
    monkeypatch.setattr(codex_mod, "_install_captured_stdio_transport", lambda sdk: None)

    # Belt-and-braces: clear any leaked state from previous tests.
    codex_mod._codex_clients.pop("p1", None)
    codex_mod._codex_client_tokens.pop("p1", None)

    await codex_mod.get_client("p1", cwd="C:/work/p1", env_overrides={"X": "1"})
    token = codex_mod._codex_client_tokens.get("p1")
    assert token, "get_client must mint a token and cache it"
    assert captured_env.get("HARNESS_COORD_PROXY_TOKEN") == token, (
        "subprocess env must carry the cached token"
    )
    assert st.resolve(token) == "p1", "minted token must resolve to slot"

    # `_coord_mcp_env` should pick up the cached token (no turn_ctx
    # override).
    tc = TurnContext(
        agent_id="p1",
        project_id="default",
        prompt="x",
        system_prompt="x",
        workspace_cwd="C:/work/p1",
        allowed_tools=[],
        external_mcp_servers={},
        turn_ctx={},
    )
    env = codex_mod._coord_mcp_env(tc)
    assert env["HARNESS_COORD_PROXY_TOKEN"] == token

    # close_client revokes.
    await codex_mod.close_client("p1")
    assert "p1" not in codex_mod._codex_client_tokens
    assert st.resolve(token) is None, "close_client must revoke the cached token"


def test_spawn_token_expiry() -> None:
    # ttl_seconds=0 → expires_at = now → resolve must return None on
    # next read.
    token = st.mint("p1", ttl_seconds=0)
    assert st.resolve(token) is None


# ---------------- HTTP endpoint coverage ----------------


def test_loopback_check_accepts_known_hosts() -> None:
    import pytest
    pytest.importorskip("fastapi")
    from server.main import _is_loopback
    assert _is_loopback("127.0.0.1") is True
    assert _is_loopback("::1") is True
    assert _is_loopback("localhost") is True
    assert _is_loopback("::ffff:127.0.0.1") is True


def test_loopback_check_rejects_external_and_empty() -> None:
    import pytest
    pytest.importorskip("fastapi")
    from server.main import _is_loopback
    assert _is_loopback(None) is False
    assert _is_loopback("") is False
    assert _is_loopback("10.0.0.1") is False
    assert _is_loopback("1.2.3.4") is False
    assert _is_loopback("example.com") is False


async def test_proxy_endpoint_rejects_non_loopback(fresh_db: str) -> None:
    """Even with a valid token, a non-loopback client must be rejected."""
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    await dbmod.init_db()
    from fastapi.testclient import TestClient
    from server.main import app

    token = st.mint("p1")
    try:
        with TestClient(app) as c:
            # TestClient defaults to "testclient" as the host, which
            # is not in our loopback set — exactly the wrong-source
            # case we want to lock out.
            resp = c.post(
                "/api/_coord/coord_send_message",
                headers={"Authorization": f"Bearer {token}"},
                json={"caller_id": "p1", "args": {}},
            )
            assert resp.status_code == 403, resp.text
            assert "loopback" in resp.json().get("detail", "").lower()
    finally:
        st.revoke(token)


async def test_proxy_endpoint_token_caller_mismatch(
    fresh_db: str,
    monkeypatch,
) -> None:
    """Token bound to p1; body claims p2 → 403."""
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    import server.main as mainmod
    await dbmod.init_db()
    # Bypass the loopback check so we can exercise the auth path
    # (TestClient host isn't in the loopback set).
    monkeypatch.setattr(mainmod, "_is_loopback", lambda _h: True)

    from fastapi.testclient import TestClient
    token = st.mint("p1")
    try:
        with TestClient(mainmod.app) as c:
            resp = c.post(
                "/api/_coord/coord_read_inbox",
                headers={"Authorization": f"Bearer {token}"},
                json={"caller_id": "p2", "args": {}},
            )
            assert resp.status_code == 403
            assert "mismatch" in resp.json().get("detail", "").lower()
    finally:
        st.revoke(token)


async def test_proxy_endpoint_missing_or_invalid_token(
    fresh_db: str,
    monkeypatch,
) -> None:
    import pytest
    pytest.importorskip("fastapi")
    import server.db as dbmod
    import server.main as mainmod
    await dbmod.init_db()
    monkeypatch.setattr(mainmod, "_is_loopback", lambda _h: True)

    from fastapi.testclient import TestClient
    with TestClient(mainmod.app) as c:
        # No Authorization header
        r1 = c.post("/api/_coord/coord_list_team", json={"args": {}})
        assert r1.status_code == 401

        # Wrong scheme
        r2 = c.post(
            "/api/_coord/coord_list_team",
            headers={"Authorization": "Basic abc"},
            json={"args": {}},
        )
        assert r2.status_code == 401

        # Bearer with garbage
        r3 = c.post(
            "/api/_coord/coord_list_team",
            headers={"Authorization": "Bearer not-a-real-token"},
            json={"args": {}},
        )
        assert r3.status_code == 401
