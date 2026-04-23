"""Tests for server/mcp_config.py — external MCP server config loader.

The loader must (1) never raise, (2) skip when no config is set,
(3) expand ${VAR} placeholders from os.environ, (4) produce fully-
qualified mcp__<server>__<tool> names for allowed_tools.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import server.mcp_config as mcp_config


@pytest.fixture
def tmp_config(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    """Point HARNESS_MCP_CONFIG at a tempfile, return the path so each
    test writes its own content. Cleaned up automatically via tempfile."""
    path = Path(tempfile.mkstemp(prefix="harness-mcp-", suffix=".json")[1])
    monkeypatch.setenv("HARNESS_MCP_CONFIG", str(path))
    # Tests write their own content; delete leftover empty file first.
    path.unlink(missing_ok=True)
    return path


# ---------- short-circuits ----------


def test_no_env_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_MCP_CONFIG", raising=False)
    servers, tools = mcp_config.load_external_servers()
    assert servers == {}
    assert tools == []


def test_missing_file_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_MCP_CONFIG", "/tmp/does/not/exist/harness-mcp.json")
    servers, tools = mcp_config.load_external_servers()
    assert servers == {}
    assert tools == []


def test_malformed_json_returns_empty(tmp_config: Path) -> None:
    tmp_config.write_text("{ not valid json", encoding="utf-8")
    servers, tools = mcp_config.load_external_servers()
    assert servers == {}
    assert tools == []


# ---------- happy path ----------


def test_loads_one_server(tmp_config: Path) -> None:
    tmp_config.write_text(json.dumps({
        "servers": {
            "github": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
            },
        },
        "allowed_tools": {"github": ["create_issue", "list_issues"]},
    }), encoding="utf-8")
    servers, tools = mcp_config.load_external_servers()
    assert "github" in servers
    assert servers["github"]["command"] == "npx"
    assert set(tools) == {
        "mcp__github__create_issue",
        "mcp__github__list_issues",
    }


def test_loads_multiple_servers(tmp_config: Path) -> None:
    tmp_config.write_text(json.dumps({
        "servers": {
            "github": {"type": "stdio", "command": "gh-mcp"},
            "notion": {"type": "http", "url": "https://x"},
        },
        "allowed_tools": {
            "github": ["a"],
            "notion": ["b", "c"],
        },
    }), encoding="utf-8")
    servers, tools = mcp_config.load_external_servers()
    assert set(servers.keys()) == {"github", "notion"}
    assert set(tools) == {
        "mcp__github__a",
        "mcp__notion__b",
        "mcp__notion__c",
    }


def test_server_without_allowed_tools_loads_but_exposes_none(tmp_config: Path) -> None:
    # Server listed but not in allowed_tools → config loads fine but
    # no tools are exposed to agents.
    tmp_config.write_text(json.dumps({
        "servers": {
            "lurking": {"type": "stdio", "command": "x"},
        },
    }), encoding="utf-8")
    servers, tools = mcp_config.load_external_servers()
    assert "lurking" in servers
    assert tools == []


# ---------- env var interpolation ----------


def test_env_var_expanded_in_string(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gh_secret_xyz")
    tmp_config.write_text(json.dumps({
        "servers": {
            "github": {
                "type": "stdio",
                "command": "gh-mcp",
                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"},
            },
        },
        "allowed_tools": {"github": ["list"]},
    }), encoding="utf-8")
    servers, _ = mcp_config.load_external_servers()
    assert servers["github"]["env"]["GITHUB_PERSONAL_ACCESS_TOKEN"] == "gh_secret_xyz"


def test_missing_env_var_expands_to_empty(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    tmp_config.write_text(json.dumps({
        "servers": {
            "notion": {
                "type": "http",
                "url": "https://x",
                "headers": {"Authorization": "Bearer ${NOTION_TOKEN}"},
            },
        },
        "allowed_tools": {"notion": ["x"]},
    }), encoding="utf-8")
    servers, _ = mcp_config.load_external_servers()
    assert servers["notion"]["headers"]["Authorization"] == "Bearer "


def test_env_interpolation_preserves_surrounding_text(
    tmp_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MY_ID", "123")
    tmp_config.write_text(json.dumps({
        "servers": {
            "x": {"type": "http", "url": "https://api/${MY_ID}/v1"},
        },
        "allowed_tools": {"x": ["y"]},
    }), encoding="utf-8")
    servers, _ = mcp_config.load_external_servers()
    assert servers["x"]["url"] == "https://api/123/v1"


# ---------- validation of bad input ----------


def test_bad_server_name_is_skipped(tmp_config: Path) -> None:
    tmp_config.write_text(json.dumps({
        "servers": {
            "valid": {"type": "stdio", "command": "a"},
            "bad-name!": {"type": "stdio", "command": "b"},
            "123start": {"type": "stdio", "command": "c"},
        },
        "allowed_tools": {"valid": ["t"], "bad-name!": ["t"]},
    }), encoding="utf-8")
    servers, tools = mcp_config.load_external_servers()
    assert "valid" in servers
    assert "bad-name!" not in servers
    assert "123start" not in servers
    assert tools == ["mcp__valid__t"]


def test_servers_key_not_a_dict_returns_empty(tmp_config: Path) -> None:
    tmp_config.write_text(json.dumps({"servers": ["not", "a", "dict"]}), encoding="utf-8")
    servers, tools = mcp_config.load_external_servers()
    assert servers == {}
    assert tools == []


def test_allowed_tools_not_a_dict_loads_servers_without_tools(tmp_config: Path) -> None:
    tmp_config.write_text(json.dumps({
        "servers": {"x": {"type": "stdio", "command": "y"}},
        "allowed_tools": "not a dict",
    }), encoding="utf-8")
    servers, tools = mcp_config.load_external_servers()
    assert "x" in servers
    assert tools == []


def test_empty_config_dict_is_fine(tmp_config: Path) -> None:
    tmp_config.write_text("{}", encoding="utf-8")
    servers, tools = mcp_config.load_external_servers()
    assert servers == {}
    assert tools == []
