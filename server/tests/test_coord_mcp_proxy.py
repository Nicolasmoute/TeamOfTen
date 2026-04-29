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
                {"tools": ["coord_list_team", "coord_send_message"]},
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
                ]

                result = await session.call_tool("coord_list_team", {"verbose": True})
                assert result.isError is False
                assert json.loads(result.content[0].text) == {
                    "team": [{"id": "coach"}],
                }

        assert calls == [
            {
                "path": "/api/_coord/coord_list_team",
                "authorization": f"Bearer {token}",
                "payload": {
                    "caller_id": "coach",
                    "args": {"verbose": True},
                },
            }
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


async def test_dispatcher_revokes_codex_proxy_token_after_turn(
    fresh_db: str,
    monkeypatch,
) -> None:
    """Audit-item-3 contract: a Codex turn must mint a coord-proxy
    token at spawn and revoke it before the turn finishes (success
    path). We stub the runtime to short-circuit before any SDK work
    so the test stays DB-only.
    """
    import server.db as dbmod
    await dbmod.init_db()

    import server.agents as agentsmod
    from server.runtimes.base import TurnContext

    captured_tokens: list[str] = []

    class StubCodexRuntime:
        name = "codex"

        async def maybe_auto_compact(self, tc: TurnContext) -> bool:
            return False

        async def run_turn(self, tc: TurnContext) -> None:
            tok = tc.turn_ctx.get("coord_proxy_token")
            if tok:
                captured_tokens.append(tok)
                # Confirm token resolves while the turn is in flight.
                assert st.resolve(tok) == tc.agent_id

        async def run_manual_compact(self, tc: TurnContext) -> None:
            await self.run_turn(tc)

    # Force runtime resolution to codex without touching env or DB.
    async def _stub_resolve(_agent_id):
        return "codex"

    def _stub_get_runtime(name):
        if name == "codex":
            return StubCodexRuntime()
        raise ValueError(name)

    monkeypatch.setattr(agentsmod, "_resolve_runtime_for", _stub_resolve)
    import server.runtimes as runtimes_pkg
    monkeypatch.setattr(runtimes_pkg, "get_runtime", _stub_get_runtime)

    # Drive a turn through the dispatcher. run_agent does a lot of
    # prelude work (autoname, cost cap, system prompt assembly) — the
    # stub runtime no-ops the SDK part.
    await agentsmod.run_agent("p1", "hello")

    assert captured_tokens, "runtime did not see a coord_proxy_token"
    # After the finally block, every token for this caller should be
    # revoked — resolve must return None.
    for tok in captured_tokens:
        assert st.resolve(tok) is None, (
            "dispatcher must revoke per-spawn coord proxy tokens after the turn"
        )


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
