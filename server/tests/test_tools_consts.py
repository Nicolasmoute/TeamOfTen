"""Tests for the input-validation constants exposed by server.tools.

These aren't the MCP-tool logic itself (that lives inside closures that
only the SDK can drive), but they're the guardrails every tool call
passes through — regressing one of them opens real bugs (e.g. a typo
in the topic regex silently lets agents write arbitrary paths).

Kept narrow + behavior-focused.
"""

from __future__ import annotations

from server.tools import (
    ALLOWED_COACH_TOOLS,
    ALLOWED_COORD_TOOLS,
    ALLOWED_PLAYER_TOOLS,
    MEMORY_TOPIC_RE,
    STANDARD_READ_TOOLS,
    STANDARD_WRITE_TOOLS,
    VALID_RECIPIENTS,
)


def test_valid_recipients_is_exactly_coach_plus_players_plus_broadcast() -> None:
    expected = {"coach", "broadcast"} | {f"p{i}" for i in range(1, 11)}
    assert VALID_RECIPIENTS == expected


def test_memory_topic_regex_accepts_simple_names() -> None:
    for good in ("notes", "meeting-notes", "auth", "m2-spike", "a"):
        assert MEMORY_TOPIC_RE.fullmatch(good), f"should accept: {good}"


def test_memory_topic_regex_rejects_bad_names() -> None:
    bad = [
        "",                  # empty
        "-leading-dash",     # starts with dash (must start alnum)
        "UPPER",             # no uppercase
        "with space",        # no spaces
        "with/slash",        # no slashes → no path traversal
        "with.dot",          # no dots
        "a" * 65,            # longer than 64
    ]
    for b in bad:
        assert MEMORY_TOPIC_RE.fullmatch(b) is None, f"should reject: {b!r}"


def test_coord_tools_are_all_in_coord_namespace() -> None:
    # Two families now live in the `coord` MCP server:
    #  - `coord_*` — original coordination/task/memory/etc. tools.
    #  - `compass_*` — Compass strategy-engine tools (Coach-only at
    #    runtime; included in the allowlist for both roles so the SDK
    #    doesn't pre-reject the call before the in-handler gate fires).
    # The shared `mcp__coord__` namespace prefix means the MCP server
    # name stays singular; the second segment discriminates families.
    for name in ALLOWED_COORD_TOOLS:
        assert name.startswith("mcp__coord__"), name
        assert name.startswith("mcp__coord__coord_") or name.startswith(
            "mcp__coord__compass_"
        ), f"unrecognized tool family: {name}"


def test_coach_has_read_tools_but_no_write_tools() -> None:
    # Structural invariant from CLAUDE.md: "Coach never writes code".
    for t in STANDARD_READ_TOOLS:
        assert t in ALLOWED_COACH_TOOLS
    for t in STANDARD_WRITE_TOOLS:
        assert t not in ALLOWED_COACH_TOOLS


def test_players_have_read_plus_write_tools() -> None:
    for t in STANDARD_READ_TOOLS + STANDARD_WRITE_TOOLS:
        assert t in ALLOWED_PLAYER_TOOLS


def test_both_kinds_share_the_coord_allowlist() -> None:
    for t in ALLOWED_COORD_TOOLS:
        assert t in ALLOWED_COACH_TOOLS
        assert t in ALLOWED_PLAYER_TOOLS
