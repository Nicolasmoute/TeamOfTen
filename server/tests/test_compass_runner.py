"""Phase 4 tests — `server.compass.runner.run`.

Each pipeline stage is patched to a stub that returns canned data.
We verify:
  - Bootstrap mode generates QUESTIONS_PER_BOOTSTRAP_RUN questions
    and skips briefing.
  - Daily mode runs the full pipeline including briefing.
  - Daily mode skips with a reminder when no human is reachable.
  - Truth contradiction halts an answer digest (question marked).
  - Per-project run lock prevents concurrent runs (second call → skip).
  - last_run + bootstrapped flags are written to team_config on success.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.compass import config, runner, store
from server.compass.pipeline import (
    briefing as pl_briefing,
    claude_md as pl_claude_md,
    digest as pl_digest,
    questions as pl_questions,
    regions as pl_regions,
    reviews as pl_reviews,
    truth_check as pl_truth_check,
    truth_derive as pl_truth_derive,
)


# --------------------------------------------------------- stubs


def _stub_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    passive_summary: str = "no signals",
    questions_to_generate: list[dict[str, Any]] | None = None,
    truth_contradicts: bool = False,
    answer_updates: list[dict[str, Any]] | None = None,
    settle_proposals: list[dict[str, Any]] | None = None,
    stale_proposals: list[dict[str, Any]] | None = None,
    duplicates: list[dict[str, Any]] | None = None,
    region_merges: list[dict[str, Any]] | None = None,
    briefing_text: str = "## Briefing\n\nstub\n",
    claude_md_text: str = "## Compass\n\nstub\n",
) -> dict[str, list[Any]]:
    """Patch every pipeline call to a deterministic stub. Returns a
    dict whose values are the per-call invocation lists so tests can
    assert on what was called."""
    invocations: dict[str, list[Any]] = {
        "passive": [], "answer": [], "questions": [], "reviews": [],
        "duplicates": [], "regions": [], "truth_check": [],
        "briefing": [], "claude_md_gen": [], "claude_md_inject": [],
    }

    @dataclass
    class _D:
        surprise: float | None = None
        updates: list[dict[str, Any]] = field(default_factory=list)
        new_statements: list[dict[str, Any]] = field(default_factory=list)
        truth_candidates: list[str] = field(default_factory=list)
        summary: str = ""

        def summary_dict(self) -> dict[str, Any]:
            return {"updates": len(self.updates), "summary": self.summary}

    async def _passive(state: Any, signals: list[dict[str, Any]]) -> Any:
        invocations["passive"].append(signals)
        return _D(updates=[], summary=passive_summary)

    async def _answer(state: Any, **kw: Any) -> Any:
        invocations["answer"].append(kw)
        return _D(updates=answer_updates or [], surprise=0.5, summary="ok")

    @dataclass
    class _Q:
        q: str
        prediction: str
        targets: list[str]
        rationale: str

    async def _questions(state: Any, *, count: int) -> list[_Q]:
        invocations["questions"].append(count)
        out: list[_Q] = []
        for raw in (questions_to_generate or [])[:count]:
            out.append(_Q(
                q=raw.get("q", "?"),
                prediction=raw.get("prediction", "yes"),
                targets=raw.get("targets", []),
                rationale=raw.get("rationale", ""),
            ))
        return out

    @dataclass
    class _Reviews:
        settle: list = field(default_factory=list)
        stale: list = field(default_factory=list)

    async def _reviews_propose(state: Any, *, run_id: str, run_iso: str) -> _Reviews:
        invocations["reviews"].append((run_id, run_iso))
        out = _Reviews()
        for raw in settle_proposals or []:
            from server.compass.store import SettleProposal
            out.settle.append(SettleProposal(
                statement_id=raw["statement_id"], direction=raw.get("direction", "yes"),
                question=raw.get("question", ""), reasoning=raw.get("reasoning", ""),
                proposed_at=run_iso, proposed_in_run=run_id,
            ))
        for raw in stale_proposals or []:
            from server.compass.store import StaleProposal
            out.stale.append(StaleProposal(
                statement_id=raw["statement_id"],
                question=raw.get("question", ""),
                reasoning=raw.get("reasoning", ""),
                proposed_at=run_iso, proposed_in_run=run_id,
                reformulation=raw.get("reformulation"),
            ))
        return out

    async def _detect_dupes(state: Any, *, run_id: str, run_iso: str) -> list[Any]:
        invocations["duplicates"].append((run_id, run_iso))
        from server.compass.store import DuplicateProposal, next_dupe_proposal_id
        out: list[Any] = []
        for raw in duplicates or []:
            out.append(DuplicateProposal(
                id=next_dupe_proposal_id(state),
                cluster_ids=raw["cluster_ids"],
                merged_text=raw["merged_text"],
                merged_weight=raw.get("merged_weight", 0.5),
                region=raw.get("region", "general"),
                reasoning=raw.get("reasoning", ""),
                proposed_at=run_iso, proposed_in_run=run_id,
            ))
        return out

    @dataclass
    class _M:
        from_: list[str]
        to: str
        reasoning: str

    async def _regions_auto(state: Any) -> list[_M]:
        invocations["regions"].append(True)
        return [_M(**m) for m in (region_merges or [])]

    @dataclass
    class _TC:
        contradiction: bool
        conflicts: list = field(default_factory=list)
        summary: str = ""

    async def _truth_check_call(truth: list[Any], **kw: Any) -> _TC:
        invocations["truth_check"].append(kw)
        if truth_contradicts:
            return _TC(contradiction=True, conflicts=[{"truth_index": 1, "explanation": "x"}], summary="x")
        return _TC(contradiction=False)

    async def _briefing_gen(state: Any, *, recent: dict[str, Any]) -> str:
        invocations["briefing"].append(recent)
        return briefing_text

    async def _claude_md_generate(state: Any) -> str:
        invocations["claude_md_gen"].append(True)
        return claude_md_text

    async def _claude_md_inject(project_id: str, body: str) -> bool:
        invocations["claude_md_inject"].append((project_id, body))
        return True

    # Default: truth-derive returns nothing (most existing tests have
    # an empty truth/ folder, so the runner short-circuits; even when
    # truth is present, the dedicated truth-derive tests below
    # override this with their own fixture data).
    @dataclass
    class _TDRes:
        statements: list[dict[str, Any]] = field(default_factory=list)
        summary: str = ""

    async def _truth_derive_stub(state: Any) -> _TDRes:
        invocations.setdefault("truth_derive", []).append(state.project_id)
        return _TDRes()

    monkeypatch.setattr(pl_digest, "passive", _passive)
    monkeypatch.setattr(pl_digest, "answer", _answer)
    monkeypatch.setattr(pl_questions, "generate_batch", _questions)
    monkeypatch.setattr(pl_reviews, "propose", _reviews_propose)
    monkeypatch.setattr(pl_reviews, "detect_duplicates", _detect_dupes)
    monkeypatch.setattr(pl_regions, "auto_merge", _regions_auto)
    monkeypatch.setattr(pl_truth_check, "check", _truth_check_call)
    monkeypatch.setattr(pl_briefing, "generate", _briefing_gen)
    monkeypatch.setattr(pl_claude_md, "generate", _claude_md_generate)
    monkeypatch.setattr(pl_claude_md, "inject", _claude_md_inject)
    monkeypatch.setattr(pl_truth_derive, "derive_from_truth", _truth_derive_stub)
    return invocations


# --------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_bootstrap_run_generates_questions_skips_briefing(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")

    inv = _stub_pipeline(
        monkeypatch,
        questions_to_generate=[
            {"q": f"Q{i}", "prediction": "yes", "targets": [], "rationale": "r"}
            for i in range(config.QUESTIONS_PER_BOOTSTRAP_RUN)
        ],
    )
    log = await runner.run("misc", mode="bootstrap")
    assert log["completed"] is True
    assert log["mode"] == "bootstrap"
    assert log["questions_generated"] == config.QUESTIONS_PER_BOOTSTRAP_RUN
    assert log["briefing_path"] is None  # bootstrap skips briefing
    # Stages exercised:
    assert len(inv["passive"]) == 1
    assert len(inv["questions"]) == 1
    assert inv["questions"][0] == config.QUESTIONS_PER_BOOTSTRAP_RUN
    # CLAUDE.md still written on bootstrap.
    assert len(inv["claude_md_gen"]) == 1
    assert len(inv["claude_md_inject"]) == 1
    # Briefing NOT generated.
    assert inv["briefing"] == []
    # questions.json now has exactly N entries.
    state = store.load_state("misc")
    assert len(state.questions) == config.QUESTIONS_PER_BOOTSTRAP_RUN


@pytest.mark.asyncio
async def test_daily_run_full_pipeline(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.db import init_db, set_active_project, configured_conn
    await init_db()
    await set_active_project("misc")

    # Seed presence so daily isn't gated.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO messages (project_id, from_id, to_id, subject, body) "
            "VALUES (?, 'human', 'coach', 's', 'I am here')",
            ("misc",),
        )
        await c.commit()
    finally:
        await c.close()

    inv = _stub_pipeline(
        monkeypatch,
        questions_to_generate=[{"q": "Q1", "prediction": "yes", "targets": []}],
    )
    log = await runner.run("misc", mode="daily")
    assert log["completed"] is True
    assert log["mode"] == "daily"
    assert log["briefing_path"] is not None
    assert len(inv["briefing"]) == 1
    assert len(inv["claude_md_inject"]) == 1


@pytest.mark.asyncio
async def test_daily_run_skips_when_no_human(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")

    inv = _stub_pipeline(monkeypatch)
    log = await runner.run("misc", mode="daily")
    assert log["skipped"] is True
    assert "no human signal" in (log["skipped_reason"] or "")
    # No pipeline stages were invoked.
    assert inv["passive"] == []
    assert inv["questions"] == []


@pytest.mark.asyncio
async def test_truth_contradiction_marks_question(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.db import init_db, set_active_project, configured_conn
    await init_db()
    await set_active_project("misc")
    # Seed presence + a truth fact + an answered question.
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO messages (project_id, from_id, to_id, subject, body) "
            "VALUES (?, 'human', 'coach', 's', 'present')",
            ("misc",),
        )
        await c.commit()
    finally:
        await c.close()

    # Truth is folder-backed: drop a file in the project's truth/
    # folder and Compass picks it up on `load_state`.
    from server.paths import project_paths
    pp = project_paths("misc")
    pp.truth.mkdir(parents=True, exist_ok=True)
    (pp.truth / "operator.md").write_text(
        "There is one human operator.", encoding="utf-8",
    )

    await store.bootstrap_state("misc")
    state = store.load_state("misc")
    assert len(state.truth) == 1  # confirm truth was read from folder
    state.questions.append(store.Question(
        id="q1",
        q="?",
        prediction="yes",
        targets=[],
        rationale="r",
        asked_at="t",
        asked_in_run="r0",
        answer="contradicts truth",
        answered_at="t",
    ))
    await store.save_questions("misc", state.questions)

    inv = _stub_pipeline(monkeypatch, truth_contradicts=True)
    log = await runner.run("misc", mode="daily")
    assert log["completed"] is True
    assert log["contradictions"] == 1

    state2 = store.load_state("misc")
    q = state2.find_question("q1")
    assert q is not None
    assert q.contradicted is True
    assert q.digested is False  # truth-check halted the digest
    # Answer-digest pipeline should NOT have been invoked.
    assert inv["answer"] == []


@pytest.mark.asyncio
async def test_concurrent_run_skips(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.db import init_db, set_active_project, configured_conn
    await init_db()
    await set_active_project("misc")

    # Make the questions stub block to simulate a slow run.
    started = asyncio.Event()
    blocker = asyncio.Event()

    @dataclass
    class _Q:
        q: str
        prediction: str
        targets: list[str]
        rationale: str

    async def _slow_questions(state: Any, *, count: int) -> list[_Q]:
        started.set()
        await blocker.wait()
        return []

    _stub_pipeline(monkeypatch, questions_to_generate=[])
    monkeypatch.setattr(pl_questions, "generate_batch", _slow_questions)

    task1 = asyncio.create_task(runner.run("misc", mode="bootstrap"))
    await started.wait()
    # Second call should skip immediately.
    log2 = await runner.run("misc", mode="bootstrap")
    assert log2["skipped"] is True

    blocker.set()
    log1 = await task1
    assert log1["completed"] is True


@pytest.mark.asyncio
async def test_run_records_last_run_and_bootstrapped_flags(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.db import init_db, set_active_project, configured_conn
    await init_db()
    await set_active_project("misc")

    _stub_pipeline(monkeypatch)
    await runner.run("misc", mode="bootstrap")

    c = await configured_conn()
    try:
        cur = await c.execute(
            "SELECT key, value FROM team_config "
            "WHERE key IN (?, ?)",
            (config.last_run_key("misc"), config.bootstrapped_key("misc")),
        )
        rows = {dict(r)["key"]: dict(r)["value"] for r in await cur.fetchall()}
    finally:
        await c.close()
    assert config.bootstrapped_key("misc") in rows
    assert rows[config.bootstrapped_key("misc")] == "1"
    assert config.last_run_key("misc") in rows
    assert "T" in rows[config.last_run_key("misc")]  # iso8601


@pytest.mark.asyncio
async def test_run_invalid_mode_raises(fresh_db: str) -> None:
    with pytest.raises(ValueError):
        await runner.run("misc", mode="not-a-mode")


# ============================================================
# Truth-derive (Stage 0) — folder-backed truth seeds the lattice
# ============================================================


@pytest.mark.asyncio
async def test_truth_derive_seeds_lattice_on_first_run(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop a truth file in the project's truth/ folder; the runner's
    Stage 0 should pick it up and derive lattice statements at weight
    0.75 with `created_by='compass-truth'`."""
    from server.db import init_db, set_active_project
    from server.paths import project_paths
    await init_db()
    await set_active_project("misc")

    pp = project_paths("misc")
    pp.truth.mkdir(parents=True, exist_ok=True)
    (pp.truth / "pricing.md").write_text(
        "Per-task billing is a hard constraint from legal.",
        encoding="utf-8",
    )

    inv = _stub_pipeline(monkeypatch)

    @dataclass
    class _TDRes:
        statements: list[dict[str, Any]] = field(default_factory=list)
        summary: str = ""

    async def _truth_derive(state: Any) -> _TDRes:
        return _TDRes(statements=[
            {"text": "Pricing is per-task", "region": "pricing",
             "rationale": "from T1: pricing.md"},
            {"text": "Legal sign-off is required for billing changes",
             "region": "compliance", "rationale": "from T1: pricing.md"},
        ])

    monkeypatch.setattr(pl_truth_derive, "derive_from_truth", _truth_derive)

    log = await runner.run("misc", mode="bootstrap")
    assert log["completed"] is True
    state = store.load_state("misc")
    truth_grounded = [s for s in state.statements if s.created_by == "compass-truth"]
    assert len(truth_grounded) == 2
    assert all(abs(s.weight - 0.75) < 1e-9 for s in truth_grounded)
    # Truth-derive note recorded in the run log.
    assert any("truth_derive" in n for n in (log.get("notes") or []))


@pytest.mark.asyncio
async def test_truth_derive_idempotent_on_unchanged_truth(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If truth doesn't change between runs and the lattice already
    has truth-derived statements, Stage 0 must NOT call the LLM
    again. The user's explicit principle: 'if the truth does not
    change, then it will not infer new statement.'"""
    from server.db import init_db, set_active_project
    from server.paths import project_paths
    await init_db()
    await set_active_project("misc")

    pp = project_paths("misc")
    pp.truth.mkdir(parents=True, exist_ok=True)
    (pp.truth / "pricing.md").write_text("Stable truth.", encoding="utf-8")

    inv = _stub_pipeline(monkeypatch)
    call_count = {"n": 0}

    @dataclass
    class _TDRes:
        statements: list[dict[str, Any]] = field(default_factory=list)
        summary: str = ""

    async def _truth_derive(state: Any) -> _TDRes:
        call_count["n"] += 1
        return _TDRes(statements=[
            {"text": "Stable claim", "region": "x", "rationale": "from T1"},
        ])

    monkeypatch.setattr(pl_truth_derive, "derive_from_truth", _truth_derive)

    # Run #1 — derives.
    await runner.run("misc", mode="bootstrap")
    assert call_count["n"] == 1
    # Run #2 — same truth, should short-circuit.
    await runner.run("misc", mode="on_demand")
    assert call_count["n"] == 1, "second run with unchanged truth must skip derive"


@pytest.mark.asyncio
async def test_truth_derive_re_runs_when_truth_changes(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing a truth file should produce a new corpus hash and
    trigger Stage 0 again on the next run."""
    from server.db import init_db, set_active_project
    from server.paths import project_paths
    await init_db()
    await set_active_project("misc")

    pp = project_paths("misc")
    pp.truth.mkdir(parents=True, exist_ok=True)
    target = pp.truth / "pricing.md"
    target.write_text("Original truth.", encoding="utf-8")

    inv = _stub_pipeline(monkeypatch)
    call_count = {"n": 0}

    @dataclass
    class _TDRes:
        statements: list[dict[str, Any]] = field(default_factory=list)
        summary: str = ""

    async def _truth_derive(state: Any) -> _TDRes:
        call_count["n"] += 1
        return _TDRes(statements=[
            {"text": f"Claim v{call_count['n']}", "region": "x", "rationale": "from T1"},
        ])

    monkeypatch.setattr(pl_truth_derive, "derive_from_truth", _truth_derive)

    await runner.run("misc", mode="bootstrap")
    assert call_count["n"] == 1
    # Edit truth file — corpus hash changes.
    target.write_text("Updated truth — different content.", encoding="utf-8")
    await runner.run("misc", mode="on_demand")
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_truth_derive_skips_when_truth_folder_empty(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty truth/ folder → no LLM call, no statements added, log
    notes record the skip."""
    from server.db import init_db, set_active_project
    await init_db()
    await set_active_project("misc")

    inv = _stub_pipeline(monkeypatch)
    call_count = {"n": 0}

    @dataclass
    class _TDRes:
        statements: list[dict[str, Any]] = field(default_factory=list)
        summary: str = ""

    async def _truth_derive(state: Any) -> _TDRes:
        call_count["n"] += 1
        return _TDRes()

    monkeypatch.setattr(pl_truth_derive, "derive_from_truth", _truth_derive)

    log = await runner.run("misc", mode="bootstrap")
    assert call_count["n"] == 0
    state = store.load_state("misc")
    assert all(s.created_by != "compass-truth" for s in state.statements)
    assert any("truth/" in n for n in (log.get("notes") or []))
