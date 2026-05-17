from __future__ import annotations

import asyncio
import json

import pytest

from server.paths import ensure_project_scaffold
from server.shared.llm_types import LLMResult
from server.truthgate.classifier import (
    TruthGateClassificationError,
    TruthGateTaskInput,
    parse_classifier_output,
    run_truthgate_classifier,
)
from server.truthgate.config import TruthGateConfigError, load_config
from server.truthgate.corpus import gather_truth_corpus


def _write_truth(project_id: str, name: str, body: str) -> None:
    pp = ensure_project_scaffold(project_id)
    target = pp.truth / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.mark.asyncio
async def test_sparse_corpus_passes_without_llm(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "sparse"
    _write_truth(project_id, "one.md", "# One\n")

    called = False

    async def fake_call(*args, **kwargs):
        nonlocal called
        called = True
        return LLMResult(text="{}")

    import server.truthgate.llm as tg_llm

    monkeypatch.setattr(tg_llm, "call_classifier", fake_call)
    monkeypatch.setenv("HARNESS_TRUTHGATE_MIN_CORPUS_FILES", "3")

    result = await run_truthgate_classifier(
        project_id,
        TruthGateTaskInput(title="Do thing", task_id="t-2026-05-16-aaaaaaaa"),
    )

    assert result["verdict"] == "truthgate_pass"
    assert result["method"] == "classifier_sparse"
    assert result["truth_basis"] == []
    assert "sparse mode" in result["warning"]
    assert called is False


@pytest.mark.asyncio
async def test_classifier_parses_mocked_llm_and_validates_basis(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "full"
    _write_truth(project_id, "alpha.md", "# Alpha\n")
    _write_truth(project_id, "beta.md", "# Beta\n")
    _write_truth(project_id, "nested/gamma.txt", "Gamma\n")

    async def fake_call(*args, **kwargs):
        return LLMResult(
            text=json.dumps({
                "verdict": "truthgate_pass",
                "truth_basis": ["truth/alpha.md#scope"],
                "truth_concerns": ["keep within alpha"],
                "rationale": "authorized",
                "suggested_amendment": None,
                "confidence": 0.8,
            })
        )

    import server.truthgate.llm as tg_llm

    monkeypatch.setattr(tg_llm, "call_classifier", fake_call)
    monkeypatch.setenv("HARNESS_TRUTHGATE_MIN_CORPUS_FILES", "3")

    result = await run_truthgate_classifier(
        project_id,
        TruthGateTaskInput(
            title="Implement alpha",
            description="Must match alpha.",
            success_criteria="passes",
            workflow="code",
            trajectory='[{"stage":"execute","to":["p1"]}]',
        ),
    )

    assert result["method"] == "classifier"
    assert result["truth_basis"] == ["truth/alpha.md#scope"]
    assert result["confidence"] == 0.8
    assert result["model_alias"] == "latest_sonnet"
    assert result["fallback_model"] == "gpt-5.4-mini"


def test_parse_invalid_json_fails_closed(fresh_db: str) -> None:
    project_id = "parsefail"
    _write_truth(project_id, "a.md", "A")
    corpus = gather_truth_corpus(
        project_id,
        total_budget_chars=32_000,
        per_file_chars=16_000,
    )
    with pytest.raises(TruthGateClassificationError, match="invalid JSON") as exc:
        parse_classifier_output("not json", project_id=project_id, corpus=corpus)
    assert "decode error" in str(exc.value)
    assert "not json" in str(exc.value)


def test_parse_invalid_json_diagnostic_is_bounded(fresh_db: str) -> None:
    project_id = "parsebounded"
    _write_truth(project_id, "a.md", "A")
    corpus = gather_truth_corpus(
        project_id,
        total_budget_chars=32_000,
        per_file_chars=16_000,
    )
    raw = "not json " + ("x" * 1000)
    with pytest.raises(TruthGateClassificationError, match="invalid JSON") as exc:
        parse_classifier_output(raw, project_id=project_id, corpus=corpus)
    message = str(exc.value)
    assert "excerpt=" in message
    assert len(message) < 400


@pytest.mark.parametrize(
    "raw",
    [
        'prefix {"verdict":"truthgate_pass"}',
        '{"verdict":"truthgate_pass"} trailing',
        '```json\n{"verdict":"truthgate_pass"}\n```',
    ],
)
def test_parse_rejects_non_whole_response_json(
    fresh_db: str,
    raw: str,
) -> None:
    project_id = "strictparse"
    _write_truth(project_id, "a.md", "A")
    corpus = gather_truth_corpus(
        project_id,
        total_budget_chars=32_000,
        per_file_chars=16_000,
    )
    with pytest.raises(TruthGateClassificationError, match="invalid JSON"):
        parse_classifier_output(raw, project_id=project_id, corpus=corpus)


def test_parse_rejects_basis_outside_truth(fresh_db: str) -> None:
    project_id = "badbasis"
    _write_truth(project_id, "a.md", "A")
    corpus = gather_truth_corpus(
        project_id,
        total_budget_chars=32_000,
        per_file_chars=16_000,
    )
    raw = json.dumps({
        "verdict": "truthgate_pass",
        "truth_basis": ["Docs/TOT-specs.md"],
        "truth_concerns": [],
        "rationale": "bad",
        "suggested_amendment": None,
        "confidence": 0.5,
    })
    with pytest.raises(TruthGateClassificationError, match="truth/"):
        parse_classifier_output(raw, project_id=project_id, corpus=corpus)


def test_corpus_always_includes_core_files_before_alphabetical(
    fresh_db: str,
) -> None:
    project_id = "corefirst"
    _write_truth(project_id, "aaa.md", "alpha\n")
    _write_truth(project_id, "TOT-specs.md", "core\n")

    corpus = gather_truth_corpus(
        project_id,
        total_budget_chars=10,
        per_file_chars=5,
        query_text="unrelated",
    )

    assert corpus.eligible_files == 3
    assert corpus.files == ("truth/truth-index.md", "truth/TOT-specs.md")
    assert corpus.skipped == ("truth/aaa.md",)


def test_corpus_keyword_relevance_beats_alphabetical_order(
    fresh_db: str,
) -> None:
    project_id = "keywordfirst"
    _write_truth(project_id, "aaa.md", "alpha\n")
    _write_truth(project_id, "zzz.md", "rocket guidance\n")

    corpus = gather_truth_corpus(
        project_id,
        total_budget_chars=10,
        per_file_chars=5,
        query_text="rocket task",
    )

    assert corpus.files == ("truth/truth-index.md", "truth/zzz.md")
    assert corpus.skipped == ("truth/aaa.md",)


def test_corpus_alphabetical_fallback_and_skipped_accounting(
    fresh_db: str,
) -> None:
    project_id = "fallback"
    _write_truth(project_id, "b.md", "bravo\n")
    _write_truth(project_id, "a.md", "alpha\n")
    _write_truth(project_id, "c.md", "charlie\n")

    corpus = gather_truth_corpus(
        project_id,
        total_budget_chars=10,
        per_file_chars=5,
    )

    assert corpus.files == ("truth/truth-index.md", "truth/a.md")
    assert corpus.skipped == ("truth/b.md", "truth/c.md")


@pytest.mark.asyncio
async def test_sparse_uses_actual_eligible_file_count_not_included_slice(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "densebudget"
    _write_truth(project_id, "TOT-specs.md", "core\n")
    _write_truth(project_id, "b.md", "bravo\n")
    _write_truth(project_id, "c.md", "charlie\n")
    called = False

    async def fake_call(*args, **kwargs):
        nonlocal called
        called = True
        return LLMResult(
            text=json.dumps({
                "verdict": "truthgate_pass",
                "truth_basis": ["truth/TOT-specs.md"],
                "truth_concerns": [],
                "rationale": "ok",
                "suggested_amendment": None,
                "confidence": 0.7,
            })
        )

    import server.truthgate.llm as tg_llm

    monkeypatch.setattr(tg_llm, "call_classifier", fake_call)
    monkeypatch.setenv("HARNESS_TRUTHGATE_MIN_CORPUS_FILES", "4")
    monkeypatch.setenv("HARNESS_TRUTHGATE_TRUTH_BUDGET_CHARS", "10")
    monkeypatch.setenv("HARNESS_TRUTHGATE_TRUTH_PER_FILE_CHARS", "5")

    result = await run_truthgate_classifier(
        project_id,
        TruthGateTaskInput(title="dense corpus"),
    )

    assert called is True
    assert result["method"] == "classifier"
    assert result["corpus_eligible_files"] == 4
    assert result["corpus_files"] == ["truth/truth-index.md", "truth/TOT-specs.md"]


def test_config_rejects_opus_and_gpt_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TRUTHGATE_MODEL", "latest_opus")
    with pytest.raises(TruthGateConfigError):
        load_config()

    monkeypatch.setenv("HARNESS_TRUTHGATE_MODEL", "latest_sonnet")
    monkeypatch.setenv("HARNESS_TRUTHGATE_FALLBACK_MODEL", "latest_gpt")
    with pytest.raises(TruthGateConfigError):
        load_config()

    monkeypatch.setenv("HARNESS_TRUTHGATE_FALLBACK_MODEL", "gpt-5.5")
    with pytest.raises(TruthGateConfigError):
        load_config()


@pytest.mark.asyncio
async def test_concurrent_runs_are_locked(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = "locked"
    _write_truth(project_id, "a.md", "A")
    _write_truth(project_id, "b.md", "B")
    _write_truth(project_id, "c.md", "C")
    release = asyncio.Event()

    async def fake_call(*args, **kwargs):
        await release.wait()
        return LLMResult(
            text=json.dumps({
                "verdict": "truthgate_pass",
                "truth_basis": ["truth/a.md"],
                "truth_concerns": [],
                "rationale": "ok",
                "suggested_amendment": None,
                "confidence": 0.9,
            })
        )

    import server.truthgate.llm as tg_llm

    monkeypatch.setattr(tg_llm, "call_classifier", fake_call)
    first = asyncio.create_task(
        run_truthgate_classifier(project_id, TruthGateTaskInput(title="one"))
    )
    await asyncio.sleep(0)
    with pytest.raises(TruthGateClassificationError, match="already running"):
        await run_truthgate_classifier(project_id, TruthGateTaskInput(title="two"))
    release.set()
    await first
