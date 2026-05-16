from __future__ import annotations

from server.role_tool_allowlists import tools_for_role


def test_executor_allowlist_has_delivery_tools_but_not_stage_approval() -> None:
    tools = set(tools_for_role("executor"))

    assert "Bash" in tools
    assert "Edit" in tools
    assert "mcp__coord__coord_commit_push" in tools
    assert "mcp__coord__coord_role_complete" in tools
    assert "mcp__coord__coord_approve_stage" not in tools


def test_shipper_allowlist_has_ship_gate_but_not_stage_approval() -> None:
    tools = set(tools_for_role("shipper"))

    assert "Bash" in tools
    assert "mcp__coord__coord_ship_to_dev" in tools
    assert "mcp__coord__coord_role_complete" in tools
    assert "mcp__coord__coord_approve_stage" not in tools


def test_verifier_allowlist_has_report_tool_but_not_ship_or_stage() -> None:
    tools = set(tools_for_role("verifier"))

    assert "Bash" in tools
    assert "mcp__coord__coord_submit_verification_report" in tools
    assert "mcp__coord__coord_role_complete" not in tools
    assert "mcp__coord__coord_ship_to_dev" not in tools
    assert "mcp__coord__coord_commit_push" not in tools
    assert "mcp__coord__coord_approve_stage" not in tools


def test_idle_allowlist_keeps_read_and_coord_status_surface_only() -> None:
    tools = set(tools_for_role("idle"))

    assert "Read" in tools
    assert "mcp__coord__coord_my_assignments" in tools
    assert "mcp__coord__coord_read_inbox" in tools
    assert "Bash" not in tools
    assert "mcp__coord__coord_commit_push" not in tools


def test_singular_semantic_alias_matches_real_role() -> None:
    assert tools_for_role("auditor_semantic") == tools_for_role("auditor_semantics")
