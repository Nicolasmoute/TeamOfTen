"""Role-scoped tool allowlists for Player turns.

The lists are intentionally generous for native Codex/Claude tools and
selective for coord MCP tools, where schema size dominates Codex prompt
cost. Tool names use the SDK-facing shape stored on `agents.allowed_tools`.
"""

from __future__ import annotations

import json
from typing import Final


READ_TOOLS: Final[list[str]] = ["Read", "Grep", "Glob", "ToolSearch"]
WRITE_TOOLS: Final[list[str]] = ["Write", "Edit", "Bash"]
INTERACTIVE_TOOLS: Final[list[str]] = ["AskUserQuestion"]


def _coord(name: str) -> str:
    return f"mcp__coord__{name}"


COORD_BASE: Final[list[str]] = [
    _coord("coord_my_assignments"),
    _coord("coord_read_inbox"),
    _coord("coord_send_message"),
    _coord("coord_list_tasks"),
    _coord("coord_read_file"),
    _coord("coord_list_memory"),
    _coord("coord_read_memory"),
    _coord("coord_list_knowledge"),
    _coord("coord_read_knowledge"),
    _coord("coord_list_team"),
    _coord("coord_request_human"),
]

COORD_WRITE_CONTEXT: Final[list[str]] = [
    _coord("coord_update_memory"),
    _coord("coord_write_knowledge"),
    _coord("coord_save_output"),
    _coord("coord_set_task_blocked"),
    _coord("coord_propose_truth_amendment"),
]

COORD_SELF_CHECK: Final[list[str]] = [
    _coord("coord_run_truth_score"),
]

ROLE_TOOL_ALLOWLISTS: Final[dict[str, list[str]]] = {
    "idle": READ_TOOLS + COORD_BASE + INTERACTIVE_TOOLS,
    "planner": (
        READ_TOOLS
        + WRITE_TOOLS
        + COORD_BASE
        + COORD_WRITE_CONTEXT
        + [
            _coord("coord_write_task_spec"),
            _coord("coord_role_complete"),
        ]
        + INTERACTIVE_TOOLS
    ),
    "executor": (
        READ_TOOLS
        + WRITE_TOOLS
        + COORD_BASE
        + COORD_WRITE_CONTEXT
        + COORD_SELF_CHECK
        + [
            _coord("coord_commit_push"),
            _coord("coord_role_complete"),
        ]
        + INTERACTIVE_TOOLS
    ),
    "auditor_syntax": (
        READ_TOOLS
        + COORD_BASE
        + COORD_WRITE_CONTEXT
        + COORD_SELF_CHECK
        + [
            _coord("coord_submit_audit_report"),
            _coord("coord_role_complete"),
        ]
        + INTERACTIVE_TOOLS
    ),
    "auditor_semantics": (
        READ_TOOLS
        + COORD_BASE
        + COORD_WRITE_CONTEXT
        + COORD_SELF_CHECK
        + [
            _coord("coord_submit_audit_report"),
            _coord("coord_role_complete"),
        ]
        + INTERACTIVE_TOOLS
    ),
    # Alias accepted so callers using the singular wording from old notes
    # land on the real kanban role name.
    "auditor_semantic": (
        READ_TOOLS
        + COORD_BASE
        + COORD_WRITE_CONTEXT
        + COORD_SELF_CHECK
        + [
            _coord("coord_submit_audit_report"),
            _coord("coord_role_complete"),
        ]
        + INTERACTIVE_TOOLS
    ),
    "shipper": (
        READ_TOOLS
        + WRITE_TOOLS
        + COORD_BASE
        + COORD_WRITE_CONTEXT
        + [
            _coord("coord_ship_to_dev"),
            _coord("coord_role_complete"),
        ]
        + INTERACTIVE_TOOLS
    ),
    "verifier": (
        READ_TOOLS
        + WRITE_TOOLS
        + COORD_BASE
        + COORD_WRITE_CONTEXT
        + [
            _coord("coord_run_verifier_smoke"),
            _coord("coord_submit_verification_report"),
        ]
        + INTERACTIVE_TOOLS
    ),
}


def tools_for_role(role: str | None) -> list[str]:
    key = (role or "idle").strip().lower() or "idle"
    tools = ROLE_TOOL_ALLOWLISTS.get(key, ROLE_TOOL_ALLOWLISTS["idle"])
    return list(dict.fromkeys(tools))


def tools_json_for_role(role: str | None) -> str:
    return json.dumps(tools_for_role(role), separators=(",", ":"))
