"""Phase 2 — Coach + Player system prompts use v2 vocabulary only.

Regression net for the v1→v2 prompt rewrite. Asserts the live
`_system_prompt_for(slot)` output never names the removed v1 tools and
does name the v2 tools that replaced them.

Background: a system prompt that lists `coord_assign_task` /
`coord_claim_task` / `coord_mark_shipped` is functionally equivalent to
broken code — the LLM follows the prompt's instructions, hits "tool
not visible," and either stalls or hallucinates. v2 removes those
tools at the registry level; the prompt must remove them too.
"""

from __future__ import annotations

from server.agents import _system_prompt_for


_REMOVED_V1_TOOLS = (
    "coord_claim_task",
    "coord_accept_role",
    "coord_advance_task_stage",
    "coord_assign_task",
    "coord_assign_planner",
    "coord_assign_auditor",
    "coord_assign_shipper",
    "coord_mark_shipped",
    "coord_complete_execution",
)


def test_coach_prompt_omits_all_v1_tools() -> None:
    body = _system_prompt_for("coach")
    for name in _REMOVED_V1_TOOLS:
        assert name not in body, (
            f"Coach prompt names removed v1 tool {name!r}; rewrite needed"
        )


def test_player_prompt_omits_all_v1_tools() -> None:
    body = _system_prompt_for("p3")
    for name in _REMOVED_V1_TOOLS:
        assert name not in body, (
            f"Player prompt names removed v1 tool {name!r}; rewrite needed"
        )


def test_coach_prompt_names_v2_tools() -> None:
    """As of the 2026-05-11 role_baseline trim, the prose tool
    catalogue is gone — the SDK injects per-tool descriptions via
    the MCP schema, and the system prompt only mentions tools where
    cross-tool precedence or harness-specific framing matters. So
    only the load-bearing names (those referenced in the
    cross-tool precedence section, the stage-transition rule,
    or the archive rule) need to appear by name."""
    body = _system_prompt_for("coach")
    assert "coord_approve_stage" in body, (
        "Coach prompt must name the single v2 transition tool — "
        "cross-tool precedence rule references it"
    )
    assert "coord_archive_task" in body, (
        "Coach prompt must name coord_archive_task — the "
        "'no auto-archive' rule references it"
    )
    assert "coord_update_task" in body, (
        "Coach prompt must name coord_update_task — the "
        "deprecation note references it"
    )


def test_player_prompt_names_v2_tools() -> None:
    body = _system_prompt_for("p3")
    assert "coord_role_complete" in body, (
        "Player prompt must name coord_role_complete (replaces "
        "coord_complete_execution + coord_mark_shipped) — the "
        "'you report to Coach' framing names it"
    )
    # message_to_coach must be teachable to Players.
    assert "message_to_coach" in body


def test_coach_prompt_drops_auto_routing_language() -> None:
    """The v1 prompts taught 'kanban auto-advances on pass; reverts to
    execute on fail'. v2 explicitly removes that behavior."""
    body = _system_prompt_for("coach")
    # 'auto-advance' / 'auto-revert' should appear ONLY in the v2 framing
    # ('no auto-advance', 'never auto-reverts'). Allow the negated forms;
    # forbid the positive forms.
    forbidden = (
        "kanban auto-advances",
        "auto-advances on success",
        "reverts to execute on fail",
        "task auto-archives",
    )
    for phrase in forbidden:
        assert phrase not in body, (
            f"Coach prompt contains v1 auto-routing phrase {phrase!r}"
        )


def test_player_prompt_drops_auto_routing_language() -> None:
    body = _system_prompt_for("p3")
    forbidden = (
        "kanban auto-advances",
        "Kanban auto-advances",
        "reverts to execute on fail",
        "task archives.\n",  # the v1 shipper line "task archives."
    )
    for phrase in forbidden:
        assert phrase not in body, (
            f"Player prompt contains v1 auto-routing phrase {phrase!r}"
        )
    # Positive: v2 framing is present.
    assert "do NOT auto-advance" in body or "does NOT auto-advance" in body or "kanban does NOT auto-advance" in body or "DOES NOT auto-advance" in body or "does not auto-advance" in body or "no auto-advance" in body or "auto-advance" not in body
