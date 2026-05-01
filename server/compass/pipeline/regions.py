"""Region taxonomy housekeeping — auto-merge close regions.

Fires only when active region count > REGION_SOFT_CAP. The merge
is autonomous (spec §3.3, §10.9) — no human approval. Re-tags
active AND archived statements (spec §10.11).

Returns a list of `{from: [...], to: ...}` decisions; runner applies
each via `mutate.apply_region_merge`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.compass import config, llm, prompts
from server.compass.store import LatticeState


@dataclass
class RegionMergeDecision:
    from_: list[str]
    to: str
    reasoning: str


async def auto_merge(state: LatticeState) -> list[RegionMergeDecision]:
    active = state.active_regions()
    if len(active) <= config.REGION_SOFT_CAP:
        return []
    target = max(config.REGION_SOFT_CAP - 2, 8)  # leave a little headroom
    res = await llm.call(
        prompts.region_merge_system(len(active), target),
        prompts.region_merge_user(state),
        max_tokens=config.LLM_MAX_TOKENS_DEFAULT,
        project_id=state.project_id,
        label="compass:regions",
    )
    parsed = llm.parse_json_safe(res.text) or {}
    raw = parsed.get("merges") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list):
        return []

    active_names = {r.name for r in active}
    out: list[RegionMergeDecision] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        froms = item.get("from")
        to = str(item.get("to") or "").strip()
        if not isinstance(froms, list) or not to:
            continue
        # The `to` region either already exists in active OR will be
        # created on apply (mutate.apply_region_merge ensures it).
        clean_from = [
            str(x).strip()
            for x in froms
            if isinstance(x, str) and x.strip()
            and str(x).strip() != to
            and str(x).strip() in active_names
        ]
        if not clean_from:
            continue
        out.append(
            RegionMergeDecision(
                from_=clean_from,
                to=to,
                reasoning=str(item.get("reasoning") or "").strip(),
            )
        )
    return out
