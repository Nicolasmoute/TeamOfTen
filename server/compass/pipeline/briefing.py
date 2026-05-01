"""Daily briefing generator (spec §3.9).

Produces a markdown document the human reads on the dashboard and
that Coach can pull via `compass_brief()`. Sections are fixed:
CONFIRMED YES / CONFIRMED NO / LEANING / OPEN / COVERAGE / DRIFT /
RECOMMENDATION.

If the lattice is empty (e.g. fresh bootstrap), returns a one-liner
placeholder instead of calling the LLM — there's nothing to brief.
"""

from __future__ import annotations

from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import LatticeState


async def generate(state: LatticeState, *, recent: dict[str, Any]) -> str:
    """Return the briefing markdown body. Caller writes to the briefing
    file via `store.write_briefing`."""
    if not state.active_statements() and not state.archived_statements():
        return _placeholder()
    res = await llm.call(
        prompts.BRIEFING_SYSTEM,
        prompts.briefing_user(state, recent),
        max_tokens=config.LLM_MAX_TOKENS_BRIEFING,
        project_id=state.project_id,
        label="compass:briefing",
    )
    text = (res.text or "").strip()
    if not text:
        return _placeholder()
    return text + "\n"


def _placeholder() -> str:
    return (
        "# Compass briefing — placeholder\n\n"
        "_The lattice is empty so far. Run a Q&A session or wait for the next "
        "passive digest before checking back._\n"
    )
