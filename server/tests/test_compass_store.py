"""Phase 1 tests — Compass storage layer.

Verifies:
  - Bootstrap creates well-formed empty state files
  - Round-trip preserves every dataclass field
  - Atomic write doesn't leave temp files behind on success
  - JSONL append (audits, runs) accumulates rows correctly
  - Briefings + CLAUDE.md block read/write
  - ID allocation is monotonic across active + archived
  - load_state on a corrupt file falls back to empty defaults

WebDAV is disabled in tests (no env vars), so kDrive mirror calls are
no-ops — exactly what we want for hermetic unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.compass import store
from server.compass.paths import compass_paths


@pytest.mark.asyncio
async def test_bootstrap_creates_empty_state(fresh_db: str) -> None:
    cp = await store.bootstrap_state("alpha")
    assert cp.lattice.exists()
    assert cp.regions.exists()
    assert cp.questions.exists()
    assert cp.settle_proposals.exists()
    assert cp.stale_proposals.exists()
    assert cp.duplicate_proposals.exists()

    state = store.load_state("alpha")
    assert state.statements == []
    # Truth is folder-backed — empty when no files in <project>/truth/.
    assert state.truth == []
    assert state.regions == []
    assert state.questions == []
    assert state.settle_proposals == []
    assert state.stale_proposals == []
    assert state.duplicate_proposals == []
    assert state.schema_version == "0.2"


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent(fresh_db: str) -> None:
    await store.bootstrap_state("alpha")
    # Manually drop a non-empty payload, then re-bootstrap. Existing
    # files must not be overwritten.
    s = store.Statement(
        id="s1", text="seed", region="meta", weight=0.5, created_at="t",
    )
    await store.save_lattice("alpha", [s])
    await store.bootstrap_state("alpha")
    state = store.load_state("alpha")
    assert len(state.statements) == 1
    assert state.statements[0].id == "s1"


@pytest.mark.asyncio
async def test_lattice_round_trip(fresh_db: str) -> None:
    s1 = store.Statement(
        id="s1",
        text="Pricing favors usage-based",
        region="pricing",
        weight=0.62,
        created_at="2026-05-01T09:00:00Z",
        history=[
            {"run_id": "r1", "delta": 0.12, "rationale": "answer", "source": "answer:q1"},
        ],
        archived=False,
        settle_proposed=True,
    )
    s2 = store.Statement(
        id="s2",
        text="Customers are technical",
        region="customers",
        weight=0.05,
        created_at="2026-05-01T09:05:00Z",
        archived=True,
        archived_at="2026-05-02T10:00:00Z",
        settled_as="no",
        settled_by_human=True,
    )
    await store.save_lattice("alpha", [s1, s2])
    state = store.load_state("alpha")
    assert len(state.statements) == 2
    by_id = {s.id: s for s in state.statements}
    assert by_id["s1"].text == "Pricing favors usage-based"
    assert by_id["s1"].region == "pricing"
    assert by_id["s1"].weight == pytest.approx(0.62)
    assert by_id["s1"].settle_proposed is True
    assert by_id["s1"].history[0]["source"] == "answer:q1"
    assert by_id["s2"].archived is True
    assert by_id["s2"].settled_as == "no"
    assert by_id["s2"].settled_by_human is True


@pytest.mark.asyncio
async def test_truth_loads_from_project_folder(fresh_db: str) -> None:
    """Truth is folder-backed — `<project>/truth/*.md` becomes the
    truth corpus on every `load_state`. Indices are 1-based by sort
    order. Compass never writes truth; the harness's existing flow
    owns it (Files pane edit, Coach `coord_propose_file_write(scope='truth', ...)`)."""
    from server.paths import project_paths

    pp = project_paths("alpha")
    pp.truth.mkdir(parents=True, exist_ok=True)
    (pp.truth / "00-pricing.md").write_text(
        "Per-task billing is a hard constraint.", encoding="utf-8",
    )
    (pp.truth / "10-customers.md").write_text(
        "Initial customers are technical (engineers).", encoding="utf-8",
    )
    # Non-allowed extensions are ignored (json/yaml are reference docs,
    # not truth-check candidates).
    (pp.truth / "schema.json").write_text('{"x": 1}', encoding="utf-8")

    state = store.load_state("alpha")
    assert [t.index for t in state.truth] == [1, 2]
    # Filename is prefixed onto the text so the LLM has a name handle.
    assert "00-pricing.md" in state.truth[0].text
    assert "Per-task billing" in state.truth[0].text
    assert "10-customers.md" in state.truth[1].text
    # Compass treats truth as human-authored.
    assert all(t.added_by == "human" for t in state.truth)


@pytest.mark.asyncio
async def test_truth_empty_when_folder_absent(fresh_db: str) -> None:
    state = store.load_state("alpha")
    assert state.truth == []


@pytest.mark.asyncio
async def test_save_truth_no_longer_exists() -> None:
    """`save_truth` was removed when truth became folder-backed.
    Truth is a pure consumer for Compass."""
    assert not hasattr(store, "save_truth")


@pytest.mark.asyncio
async def test_regions_round_trip_with_merge_history(fresh_db: str) -> None:
    regions = [
        store.Region(name="pricing", created_at="t", created_by="compass"),
        store.Region(
            name="billing", created_at="t", created_by="compass", merged_into="pricing"
        ),
    ]
    history = [
        store.RegionMergeEvent(
            from_=["billing"], to="pricing", merged_at="t2", run_id="r5"
        ),
    ]
    await store.save_regions("alpha", regions, history)
    state = store.load_state("alpha")
    assert {r.name for r in state.regions} == {"pricing", "billing"}
    survivors = state.active_regions()
    assert [r.name for r in survivors] == ["pricing"]
    assert state.region_merge_history[0].from_ == ["billing"]
    assert state.region_merge_history[0].to == "pricing"

    # Sanity-check the JSON shape on disk uses bare 'from' (not 'from_').
    cp = compass_paths("alpha")
    raw = json.loads(cp.regions.read_text(encoding="utf-8"))
    assert raw["merge_history"][0]["from"] == ["billing"]
    assert "from_" not in raw["merge_history"][0]


@pytest.mark.asyncio
async def test_questions_round_trip(fresh_db: str) -> None:
    q = store.Question(
        id="q1",
        q="Will customers self-serve?",
        prediction="Yes — technical audience",
        targets=["s4", "s6"],
        rationale="entropy gap",
        asked_at="t1",
        asked_in_run="r1",
        answer="Mostly self-serve with occasional handholding",
        answered_at="t2",
        digested=False,
    )
    await store.save_questions("alpha", [q])
    state = store.load_state("alpha")
    assert len(state.questions) == 1
    got = state.questions[0]
    assert got.targets == ["s4", "s6"]
    assert got.answer == "Mostly self-serve with occasional handholding"
    assert got.digested is False


@pytest.mark.asyncio
async def test_proposals_partial_save(fresh_db: str) -> None:
    """Passing None to save_proposals leaves that file untouched."""
    settle = [
        store.SettleProposal(
            statement_id="s1",
            direction="yes",
            question="Confirm settle?",
            reasoning="weight 0.91",
            proposed_at="t1",
            proposed_in_run="r3",
        )
    ]
    await store.save_proposals("alpha", settle=settle, stale=None, dupes=None)
    state = store.load_state("alpha")
    assert len(state.settle_proposals) == 1
    # stale + dupes still empty defaults
    assert state.stale_proposals == []
    assert state.duplicate_proposals == []

    # Now write empty stale list — that clears it explicitly.
    await store.save_proposals("alpha", settle=None, stale=[], dupes=None)
    state2 = store.load_state("alpha")
    assert len(state2.settle_proposals) == 1  # untouched
    assert state2.stale_proposals == []


@pytest.mark.asyncio
async def test_audit_jsonl_append(fresh_db: str) -> None:
    a1 = store.AuditRecord(
        id="audit_1", ts="t1", artifact="commit msg",
        verdict="aligned", summary="ok", message_to_coach="OK",
    )
    a2 = store.AuditRecord(
        id="audit_2", ts="t2", artifact="another",
        verdict="confident_drift", summary="drift",
        contradicting_ids=["s1"], message_to_coach="conflict",
    )
    await store.append_audit("alpha", a1)
    await store.append_audit("alpha", a2)
    rows = store.read_audits("alpha")
    assert [r.id for r in rows] == ["audit_1", "audit_2"]
    assert rows[1].contradicting_ids == ["s1"]


@pytest.mark.asyncio
async def test_run_log_jsonl_append(fresh_db: str) -> None:
    r = store.RunLog(
        run_id="r1",
        started_at="t1",
        mode="bootstrap",
        completed=True,
        finished_at="t2",
        passive={"updates": 0, "summary": "no signals"},
        questions_generated=5,
    )
    await store.append_run_log("alpha", r)
    rows = store.read_run_log("alpha")
    assert len(rows) == 1
    assert rows[0].mode == "bootstrap"
    assert rows[0].questions_generated == 5
    assert rows[0].passive["updates"] == 0


@pytest.mark.asyncio
async def test_briefing_write_and_latest(fresh_db: str) -> None:
    await store.write_briefing("alpha", "2026-05-01", "# Briefing 1\n")
    await store.write_briefing("alpha", "2026-05-02", "# Briefing 2\n")
    assert store.read_briefing("alpha", "2026-05-01") == "# Briefing 1\n"
    assert store.list_briefing_dates("alpha") == ["2026-05-02", "2026-05-01"]
    assert store.latest_briefing_text("alpha") == "# Briefing 2\n"


@pytest.mark.asyncio
async def test_claude_md_block_round_trip(fresh_db: str) -> None:
    block = "## Compass\n\nWe stand here.\n"
    await store.write_claude_md_block("alpha", block)
    assert store.read_claude_md_block("alpha") == block


@pytest.mark.asyncio
async def test_id_allocators_are_monotonic(fresh_db: str) -> None:
    state = store.LatticeState(project_id="alpha")
    assert store.next_statement_id(state) == "s1"
    state.statements.append(
        store.Statement(id="s1", text="t", region="r", weight=0.5, created_at="t0")
    )
    assert store.next_statement_id(state) == "s2"
    # Archived ids count too.
    state.statements.append(
        store.Statement(
            id="s7",
            text="t",
            region="r",
            weight=0.0,
            created_at="t0",
            archived=True,
        )
    )
    assert store.next_statement_id(state) == "s8"

    assert store.next_question_id(state) == "q1"
    state.questions.append(
        store.Question(
            id="q3", q="?", prediction="p", targets=[], rationale="r",
            asked_at="t", asked_in_run="r1",
        )
    )
    assert store.next_question_id(state) == "q4"

    # Audit / run ids are time-based — uniqueness is monotonic per
    # tick, not strictly counter-driven.
    a = store.next_audit_id()
    assert a.startswith("audit_") and a[6:].isdigit()
    rid = store.next_run_id()
    assert rid.startswith("r") and rid[1:].isdigit()


@pytest.mark.asyncio
async def test_load_state_on_corrupt_file_returns_empty(fresh_db: str) -> None:
    """A botched edit shouldn't freeze Compass — load_state should
    log + fall back to empty defaults."""
    await store.bootstrap_state("alpha")
    cp = compass_paths("alpha")
    cp.lattice.write_text("{ this is not json", encoding="utf-8")
    state = store.load_state("alpha")
    # All other state still loads cleanly; lattice falls back.
    assert state.statements == []
    assert state.truth == []


@pytest.mark.asyncio
async def test_atomic_write_does_not_leave_temp_files(fresh_db: str) -> None:
    await store.bootstrap_state("alpha")
    cp = compass_paths("alpha")
    leftovers = [p for p in cp.root.iterdir() if p.name.startswith(".")]
    # Hidden tempfiles use prefix "." + filename + ... and should be
    # gone after each write. Allow nothing dotted.
    assert leftovers == [], f"leftover temp files: {leftovers}"


@pytest.mark.asyncio
async def test_state_isolation_between_projects(fresh_db: str) -> None:
    s_a = store.Statement(
        id="s1", text="alpha-only", region="x", weight=0.5, created_at="t"
    )
    s_b = store.Statement(
        id="s1", text="beta-only", region="y", weight=0.5, created_at="t"
    )
    await store.save_lattice("alpha", [s_a])
    await store.save_lattice("beta", [s_b])
    state_a = store.load_state("alpha")
    state_b = store.load_state("beta")
    assert state_a.statements[0].text == "alpha-only"
    assert state_b.statements[0].text == "beta-only"


@pytest.mark.asyncio
async def test_wipe_project_local_only(fresh_db: str) -> None:
    """Without webdav configured, wipe_project should still clear the
    local tree."""
    await store.bootstrap_state("alpha")
    cp = compass_paths("alpha")
    assert cp.root.exists()
    await store.wipe_project("alpha")
    assert not cp.root.exists()
