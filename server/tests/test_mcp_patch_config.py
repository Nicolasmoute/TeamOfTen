"""Tests for `PATCH /api/mcp/servers/{name}` config edits and the
`_merge_redacted_config` helper. Covers the round-trip safety
guarantee: editing a server's config via the redacted GET payload
must NOT overwrite stored secrets with the `"***"` sentinel.
"""

from __future__ import annotations

import json
import sqlite3


# ----- _merge_redacted_config unit tests -----


def test_merge_keeps_command_and_args() -> None:
    from server.main import _merge_redacted_config

    new = {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--browser", "chromium"]}
    stored = {"command": "old", "args": ["-y", "old-args"]}
    out = _merge_redacted_config(new, stored)
    assert out["command"] == "npx"
    assert out["args"] == ["-y", "@playwright/mcp@latest", "--browser", "chromium"]


def test_merge_restores_redacted_env_values() -> None:
    """The classic case: GET returns `{"env": {"GITHUB_PAT": "***"}}`,
    user edits args, sends PATCH with the masked env intact. We must
    restore the real stored value so the secret survives."""
    from server.main import _merge_redacted_config

    new = {"command": "npx", "env": {"GITHUB_PAT": "***", "OTHER": "literal"}}
    stored = {"command": "npx", "env": {"GITHUB_PAT": "ghp_real_token_value", "OTHER": "old"}}
    out = _merge_redacted_config(new, stored)
    assert out["env"]["GITHUB_PAT"] == "ghp_real_token_value"
    # Non-redacted user input survives even when stored had a different value.
    assert out["env"]["OTHER"] == "literal"


def test_merge_keeps_var_placeholders_intact() -> None:
    from server.main import _merge_redacted_config

    new = {"env": {"TOK": "${GITHUB_TOKEN}"}}
    stored = {"env": {"TOK": "${GITHUB_TOKEN}"}}
    out = _merge_redacted_config(new, stored)
    assert out["env"]["TOK"] == "${GITHUB_TOKEN}"


def test_merge_treats_headers_same_as_env() -> None:
    from server.main import _merge_redacted_config

    new = {"url": "https://x", "headers": {"Authorization": "***"}}
    stored = {"url": "https://x", "headers": {"Authorization": "Bearer secret_xyz"}}
    out = _merge_redacted_config(new, stored)
    assert out["headers"]["Authorization"] == "Bearer secret_xyz"


def test_merge_restores_masked_url_userinfo() -> None:
    """`_mask_repo_url` redacts `https://user:tok@host` to
    `https://***@host`. If the user PATCHes the masked form back,
    we restore the original."""
    from server.main import _merge_redacted_config

    new = {"url": "https://***@example.com/sse"}
    stored = {"url": "https://user:secret_pat@example.com/sse"}
    out = _merge_redacted_config(new, stored)
    assert out["url"] == "https://user:secret_pat@example.com/sse"


def test_merge_keeps_user_edited_url() -> None:
    """If the user actually changed the URL host/path, don't restore."""
    from server.main import _merge_redacted_config

    new = {"url": "https://different-host.com/sse"}
    stored = {"url": "https://user:tok@old-host.com/sse"}
    out = _merge_redacted_config(new, stored)
    assert out["url"] == "https://different-host.com/sse"


def test_merge_handles_missing_stored_section() -> None:
    """Stored config has no env at all — user adding env for the
    first time. Sentinel without backing storage stays as `"***"`,
    which would normally trip the secret scan but isn't matched by any
    pattern, so the user gets to save it (and presumably discover the
    issue at runtime). The merge function's job is only restoration."""
    from server.main import _merge_redacted_config

    new = {"env": {"NEW_KEY": "***"}}
    stored = {"command": "npx"}
    out = _merge_redacted_config(new, stored)
    assert out["env"]["NEW_KEY"] == "***"


def test_merge_returns_empty_for_non_dict_input() -> None:
    from server.main import _merge_redacted_config

    assert _merge_redacted_config([], {"command": "npx"}) == {}
    assert _merge_redacted_config({"command": "npx"}, "not-a-dict") == {"command": "npx"}


# ----- PATCH /api/mcp/servers/{name} HTTP tests -----


def _seed_server(
    name: str,
    config: dict,
    allowed_tools: list[str] | None = None,
    enabled: bool = True,
) -> None:
    """Direct DB insert to set up a starting state, bypassing the
    save endpoint's secret scan."""
    from server.db import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO mcp_servers (name, config_json, allowed_tools_json, enabled) "
            "VALUES (?, ?, ?, ?)",
            (
                name,
                json.dumps(config),
                json.dumps(allowed_tools or []),
                1 if enabled else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _read_stored_config(name: str) -> dict:
    from server.db import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT config_json FROM mcp_servers WHERE name = ?", (name,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else {}


def test_patch_config_json_round_trip(fresh_db) -> None:
    """Plain config edit on a no-secret server (e.g. playwright)
    persists exactly what was sent."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    new_cfg = {
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest", "--browser", "chromium", "--isolated"],
    }
    with TestClient(mainmod.app) as c:
        _seed_server(
            "playwright",
            {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--isolated"]},
        )
        r = c.patch(
            "/api/mcp/servers/playwright",
            json={"config_json": json.dumps(new_cfg)},
        )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert _read_stored_config("playwright") == new_cfg


def test_patch_config_json_preserves_redacted_env_secret(fresh_db) -> None:
    """User edits args, env GET-redacted to `"***"` round-trips intact —
    the actual stored token must NOT be overwritten."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    edited_cfg = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github", "--verbose"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "***"},  # what GET returned
    }
    with TestClient(mainmod.app) as c:
        _seed_server(
            "github",
            {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_real_token_aaaaaaaaaaaa"},
            },
        )
        r = c.patch(
            "/api/mcp/servers/github",
            json={"config_json": json.dumps(edited_cfg)},
        )
    assert r.status_code == 200, r.text
    stored = _read_stored_config("github")
    assert stored["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "ghp_real_token_aaaaaaaaaaaa"
    assert "--verbose" in stored["args"]


def test_patch_config_json_rejects_raw_token_without_allow_secrets(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    new_cfg = {
        "command": "npx",
        "args": ["-y"],
        "env": {"TOKEN": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
    }
    with TestClient(mainmod.app) as c:
        _seed_server("github", {"command": "npx", "args": ["-y"]})
        r = c.patch(
            "/api/mcp/servers/github",
            json={"config_json": json.dumps(new_cfg)},
        )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert "secret_warnings" in detail


def test_patch_config_json_accepts_raw_token_with_allow_secrets(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    new_cfg = {
        "command": "npx",
        "args": ["-y"],
        "env": {"TOKEN": "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAA"},
    }
    with TestClient(mainmod.app) as c:
        _seed_server("github", {"command": "npx", "args": ["-y"]})
        r = c.patch(
            "/api/mcp/servers/github",
            json={"config_json": json.dumps(new_cfg), "allow_secrets": True},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["secret_warnings"]


def test_patch_config_json_accepts_var_placeholder(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    new_cfg = {"command": "npx", "env": {"TOKEN": "${GITHUB_TOKEN}"}}
    with TestClient(mainmod.app) as c:
        _seed_server("github", {"command": "npx", "env": {"TOKEN": "old"}})
        r = c.patch(
            "/api/mcp/servers/github",
            json={"config_json": json.dumps(new_cfg)},
        )
    assert r.status_code == 200, r.text
    assert _read_stored_config("github")["env"]["TOKEN"] == "${GITHUB_TOKEN}"


def test_patch_config_json_invalid_json_returns_400(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    with TestClient(mainmod.app) as c:
        _seed_server("x", {"command": "npx"})
        r = c.patch("/api/mcp/servers/x", json={"config_json": "{this is not json"})
    assert r.status_code == 400
    assert "invalid config JSON" in r.json()["detail"]


def test_patch_config_json_non_dict_returns_400(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    with TestClient(mainmod.app) as c:
        _seed_server("x", {"command": "npx"})
        r = c.patch("/api/mcp/servers/x", json={"config_json": "[1, 2, 3]"})
    assert r.status_code == 400
    assert "object" in r.json()["detail"].lower()


def test_patch_config_json_unknown_server_returns_404(fresh_db) -> None:
    from fastapi.testclient import TestClient
    import server.main as mainmod

    with TestClient(mainmod.app) as c:
        r = c.patch(
            "/api/mcp/servers/nonexistent",
            json={"config_json": json.dumps({"command": "npx"})},
        )
    assert r.status_code == 404


def test_patch_config_and_tools_in_one_request(fresh_db) -> None:
    """Mixed PATCH: config_json + allowed_tools + enabled all land
    atomically in the same UPDATE."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    new_cfg = {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]}
    with TestClient(mainmod.app) as c:
        _seed_server("playwright", {"command": "old"}, allowed_tools=["browser_navigate"])
        r = c.patch(
            "/api/mcp/servers/playwright",
            json={
                "config_json": json.dumps(new_cfg),
                "allowed_tools": ["browser_navigate", "browser_click", "browser_snapshot"],
                "enabled": False,
            },
        )
    assert r.status_code == 200, r.text
    from server.db import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT config_json, allowed_tools_json, enabled FROM mcp_servers WHERE name = ?",
            ("playwright",),
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row[0]) == new_cfg
    assert json.loads(row[1]) == ["browser_navigate", "browser_click", "browser_snapshot"]
    assert row[2] == 0


def test_patch_with_no_fields_returns_400(fresh_db) -> None:
    """A PATCH that touches nothing is a programming error — surface it."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    with TestClient(mainmod.app) as c:
        _seed_server("x", {"command": "npx"})
        r = c.patch("/api/mcp/servers/x", json={})
    assert r.status_code == 400
    assert "nothing to update" in r.json()["detail"].lower()


def test_patch_url_userinfo_round_trip(fresh_db) -> None:
    """User edits headers via PATCH; URL with masked userinfo returns
    in the GET payload and round-trips back without losing the
    original token."""
    from fastapi.testclient import TestClient
    import server.main as mainmod

    edited_cfg = {
        "type": "http",
        "url": "https://***@mcp.notion.com/sse",
        "headers": {"X-Other": "updated-value"},
    }
    with TestClient(mainmod.app) as c:
        _seed_server(
            "notion",
            {
                "type": "http",
                "url": "https://user:realtoken@mcp.notion.com/sse",
                "headers": {"X-Other": "literal"},
            },
        )
        r = c.patch(
            "/api/mcp/servers/notion",
            json={"config_json": json.dumps(edited_cfg)},
        )
    assert r.status_code == 200, r.text
    stored = _read_stored_config("notion")
    assert stored["url"] == "https://user:realtoken@mcp.notion.com/sse"
    assert stored["headers"]["X-Other"] == "updated-value"
