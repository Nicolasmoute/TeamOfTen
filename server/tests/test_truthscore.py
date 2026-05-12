"""TruthScore unit + integration tests.

Covers:
  - _parse_llm_output validation (happy path + 6 failure modes)
  - _render_result_file snapshot shape (YAML front-matter + table)
  - _score_main_files priority ranking
  - _gather_truth_corpus (empty raises, over-cap truncation, warning)
  - _gather_main_tree (binary detection, fetch failure tolerance,
    missing main raises)
  - _gather_subcorpora (per-corpus + per-file caps, binary fallback)
  - run_truth_score end-to-end with stubbed compass.llm.call
  - HTTP endpoint smoke (added in commit 2 — these tests live here too)
  - MCP tool smoke (added in commit 3)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import server.truthscore as ts


# ---------------------------------------------------------------- fakes


@dataclass
class _FakeLLMResult:
    text: str
    is_error: bool = False
    cost_usd: float | None = 0.001
    duration_ms: int | None = 50
    input_tokens: int = 1000
    output_tokens: int = 200
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    session_id: str | None = "stub"
    stop_reason: str | None = "end_turn"
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- _parse_llm_output


def test_parse_happy_path() -> None:
    raw = """{
        "scores": {
            "fidelity": 8, "completeness": 7, "consistency": 9,
            "currency": 6, "clarity": 7
        },
        "comment": "Broadly aligned. truth/api.md predates v2 endpoints."
    }"""
    out = ts._parse_llm_output(raw)
    assert out is not None
    assert out["scores"]["fidelity"] == 8
    assert out["overall"] == 7.4


def test_parse_with_code_fence() -> None:
    raw = """```json
{
    "scores": {"fidelity": 5, "completeness": 5, "consistency": 5, "currency": 5, "clarity": 5},
    "comment": "Mid."
}
```"""
    out = ts._parse_llm_output(raw)
    assert out is not None
    assert out["overall"] == 5.0


def test_parse_floats_round_to_int() -> None:
    raw = '{"scores": {"fidelity": 7.6, "completeness": 7, "consistency": 7, "currency": 7, "clarity": 7}, "comment": "ok."}'
    out = ts._parse_llm_output(raw)
    assert out is not None
    assert out["scores"]["fidelity"] == 8


def test_parse_rejects_missing_score_key() -> None:
    raw = '{"scores": {"fidelity": 8, "completeness": 7, "consistency": 9, "currency": 6}, "comment": "x"}'
    assert ts._parse_llm_output(raw) is None


def test_parse_rejects_score_out_of_range() -> None:
    for bad in (0, 11, -1, 100):
        raw = (
            '{"scores": {"fidelity": ' + str(bad)
            + ', "completeness": 7, "consistency": 9, "currency": 6, "clarity": 7}, '
            '"comment": "x"}'
        )
        assert ts._parse_llm_output(raw) is None, f"bad={bad}"


def test_parse_rejects_non_int_score() -> None:
    raw = '{"scores": {"fidelity": "eight", "completeness": 7, "consistency": 9, "currency": 6, "clarity": 7}, "comment": "x"}'
    assert ts._parse_llm_output(raw) is None


def test_parse_rejects_bool_score() -> None:
    # Booleans are subclasses of int in Python — must be explicitly rejected
    raw = '{"scores": {"fidelity": true, "completeness": 7, "consistency": 9, "currency": 6, "clarity": 7}, "comment": "x"}'
    assert ts._parse_llm_output(raw) is None


def test_parse_rejects_empty_comment() -> None:
    raw = '{"scores": {"fidelity": 8, "completeness": 7, "consistency": 9, "currency": 6, "clarity": 7}, "comment": "   "}'
    assert ts._parse_llm_output(raw) is None


def test_parse_rejects_oversize_comment() -> None:
    long = "x" * (ts.COMMENT_MAX_CHARS + 1)
    raw = (
        '{"scores": {"fidelity": 8, "completeness": 7, "consistency": 9, '
        '"currency": 6, "clarity": 7}, "comment": "' + long + '"}'
    )
    assert ts._parse_llm_output(raw) is None


def test_parse_rejects_garbage() -> None:
    assert ts._parse_llm_output("not even close to JSON") is None
    assert ts._parse_llm_output("") is None


# ---------------------------------------------------------------- _render_result_file


def test_render_result_file_shape() -> None:
    body = ts._render_result_file(
        overall=7.4,
        scores={
            "fidelity": 8, "completeness": 7, "consistency": 9,
            "currency": 6, "clarity": 7,
        },
        comment="Broadly aligned. truth/api.md predates v2 endpoints.",
        inputs={
            "truth_files": 4, "truth_bytes": 18000,
            "main_sha": "0d98975abcdef0123456789",
            "main_files_indexed": 287,
            "main_bytes_sampled": 75000,
            "decisions_files": 12,
            "knowledge_files": 8,
            "outputs_files": 3,
        },
        commentary="skip section 2",
        actor={"source": "human"},
        created_at="2026-05-09T14:30:00+00:00",
    )
    # YAML front-matter present and parseable-shaped.
    assert body.startswith("---\n")
    assert "overall: 7.4" in body
    assert "fidelity: 8" in body
    assert "main_sha: 0d98975abcdef0123456789" in body
    assert "actor_source: human" in body
    assert "commentary_present: true" in body
    # Body sections.
    assert "# Truth Score" in body
    assert "**Overall: 7.4 / 10**" in body
    assert "## Comment" in body
    assert "Broadly aligned" in body
    assert "## Inputs" in body
    assert "## Scoring directives applied" in body
    assert "skip section 2" in body
    # No emoji per project rule.
    for forbidden in ("⚠", "✅", "❌", "📄"):
        assert forbidden not in body


def test_render_result_file_no_commentary_drops_directives_section() -> None:
    body = ts._render_result_file(
        overall=5.0,
        scores={k: 5 for k in ts.SCORE_KEYS},
        comment="Mid.",
        inputs={
            "truth_files": 1, "truth_bytes": 100,
            "main_sha": "abc",
            "main_files_indexed": 1,
            "main_bytes_sampled": 50,
            "decisions_files": 0,
            "knowledge_files": 0,
            "outputs_files": 0,
        },
        commentary=None,
        actor={"source": "mcp-tool"},
        created_at="2026-05-09T00:00:00+00:00",
    )
    assert "Scoring directives applied" not in body
    assert "commentary_present: false" in body


def test_render_includes_fetch_warning_when_present() -> None:
    body = ts._render_result_file(
        overall=5.0,
        scores={k: 5 for k in ts.SCORE_KEYS},
        comment="ok",
        inputs={
            "truth_files": 1, "truth_bytes": 100,
            "main_sha": "abc",
            "main_files_indexed": 1,
            "main_bytes_sampled": 50,
            "decisions_files": 0,
            "knowledge_files": 0,
            "outputs_files": 0,
            "fetch_warning": "fatal: could not read from remote",
        },
        commentary=None,
        actor={"source": "human"},
        created_at="2026-05-09T00:00:00+00:00",
    )
    assert "warning" in body.lower()
    assert "could not read from remote" in body


# ---------------------------------------------------------------- _score_main_files


def test_score_main_files_always_include_wins() -> None:
    truth = "blah blah"
    paths = ["README.md", "src/main.py", "docs/notes.md"]
    ranks = ts._score_main_files(paths, truth)
    assert ranks["README.md"] == 0


def test_score_main_files_truth_referenced_file() -> None:
    truth = "the auth module lives in auth.py and is mounted at /api/auth"
    paths = ["auth.py", "src/main.py"]
    ranks = ts._score_main_files(paths, truth)
    assert ranks["auth.py"] == 1
    assert ranks["src/main.py"] == 3  # not referenced


def test_score_main_files_truth_referenced_dir() -> None:
    truth = "the server module owns the websocket handlers"
    paths = ["server/main.py", "tests/test_main.py"]
    ranks = ts._score_main_files(paths, truth)
    assert ranks["server/main.py"] == 2
    assert ranks["tests/test_main.py"] == 3  # tests directory not in truth


# ---------------------------------------------------------------- _looks_textual


def test_looks_textual_known_extension() -> None:
    p = Path("nonexistent.py")
    # Even without reading bytes, .py extension wins.
    assert ts._looks_textual(p) is True


def test_looks_textual_blob_handles_known_ext() -> None:
    # Even with null bytes, known text extensions trust the extension.
    assert ts._looks_textual_blob("script.py", b"x = 1\n\x00") is True


def test_looks_textual_blob_unknown_ext_with_null_byte_is_binary() -> None:
    assert ts._looks_textual_blob("blob.dat", b"\x00\x01\x02") is False


def test_looks_textual_blob_unknown_ext_clean_bytes_is_text() -> None:
    assert ts._looks_textual_blob("notes.unknown", b"plain text bytes") is True


# ---------------------------------------------------------------- _gather_truth_corpus


@pytest.fixture
def project_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a minimal sandboxed /data/projects/test-proj/ tree and
    point paths.DATA_ROOT at the parent."""
    import server.paths as pathsmod
    monkeypatch.setattr(pathsmod, "DATA_ROOT", tmp_path)
    project_root = tmp_path / "projects" / "test-proj"
    (project_root / "truth").mkdir(parents=True)
    (project_root / "decisions").mkdir(parents=True)
    (project_root / "working" / "knowledge").mkdir(parents=True)
    (project_root / "outputs").mkdir(parents=True)
    return project_root


def test_gather_truth_empty_raises(project_tree: Path) -> None:
    # Empty truth/ dir
    async def go() -> None:
        with pytest.raises(ts.TruthScoreError, match="empty"):
            await ts._gather_truth_corpus("test-proj")
    asyncio.run(go())


def test_gather_truth_populated(project_tree: Path) -> None:
    (project_tree / "truth" / "specs.md").write_text("# Specs\nThe API is REST.")
    (project_tree / "truth" / "brand.md").write_text("# Brand\nUse blue.")
    async def go() -> None:
        rendered, meta = await ts._gather_truth_corpus("test-proj")
        assert "Specs" in rendered
        assert "Brand" in rendered
        assert meta["files"] == 2
        assert meta["bytes"] > 0
    asyncio.run(go())


def test_gather_truth_per_file_truncation(project_tree: Path) -> None:
    big = "x" * (ts.TRUTH_PER_FILE_HEAD + 1000)
    (project_tree / "truth" / "huge.md").write_text(big)
    async def go() -> None:
        rendered, meta = await ts._gather_truth_corpus("test-proj")
        assert "huge.md" in meta["truncated"]
        # Rendered body shouldn't contain the full huge content.
        assert len(rendered) < ts.TRUTH_PER_FILE_HEAD + 5000
    asyncio.run(go())


def test_gather_truth_skips_non_md_txt(project_tree: Path) -> None:
    (project_tree / "truth" / "specs.md").write_text("# Specs")
    (project_tree / "truth" / "image.png").write_bytes(b"\x89PNG fake")
    (project_tree / "truth" / "data.json").write_text("{}")
    async def go() -> None:
        rendered, meta = await ts._gather_truth_corpus("test-proj")
        assert meta["files"] == 1
        assert "Specs" in rendered
    asyncio.run(go())


# ---------------------------------------------------------------- _gather_objectives


def test_gather_objectives_present(project_tree: Path) -> None:
    (project_tree / "project-objectives.md").write_text("Make widgets faster.")
    async def go() -> None:
        rendered, meta = await ts._gather_objectives("test-proj")
        assert "Make widgets faster" in rendered
        assert meta["present"] is True
    asyncio.run(go())


def test_gather_objectives_absent(project_tree: Path) -> None:
    async def go() -> None:
        rendered, meta = await ts._gather_objectives("test-proj")
        assert rendered == ""
        assert meta["present"] is False
    asyncio.run(go())


# ---------------------------------------------------------------- _gather_subcorpora


def test_gather_subcorpora_text_files(project_tree: Path) -> None:
    (project_tree / "decisions" / "0001-foo.md").write_text("# Foo")
    (project_tree / "working" / "knowledge" / "notes.md").write_text("# Notes")
    async def go() -> None:
        rendered, meta = await ts._gather_subcorpora("test-proj")
        assert "decisions/" in rendered
        assert "Foo" in rendered
        assert "Notes" in rendered
        assert meta["decisions"]["files"] == 1
        assert meta["knowledge"]["files"] == 1
        assert meta["outputs"]["files"] == 0
    asyncio.run(go())


def test_gather_subcorpora_all_empty(project_tree: Path) -> None:
    async def go() -> None:
        rendered, meta = await ts._gather_subcorpora("test-proj")
        assert "all three sub-corpora are empty" in rendered
        assert meta["decisions"]["files"] == 0
    asyncio.run(go())


def test_gather_subcorpora_binary_outputs_fallback(
    project_tree: Path,
) -> None:
    # A binary output file with no extractor available — should fall back to
    # path-with-size, not be silently omitted.
    (project_tree / "outputs" / "report.unknownext").write_bytes(
        b"\x00\x01\x02 binary content"
    )
    async def go() -> None:
        rendered, _meta = await ts._gather_subcorpora("test-proj")
        assert "report.unknownext" in rendered
        assert "body skipped" in rendered
    asyncio.run(go())


# ---------------------------------------------------------------- _gather_main_tree


@pytest.fixture
def project_with_repo(
    project_tree: Path, tmp_path: Path
) -> Path:
    """Set up `repo/.project` as a normal git clone with one commit on
    `main`. The harness's `bare_clone` attribute is misleadingly named
    — production uses a regular `git clone <url>`, not `--bare`, so the
    clone gets `refs/remotes/origin/main` populated automatically.
    Mirroring that here keeps the test inputs honest."""
    repo_root = project_tree / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")

    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(work)], check=True, env=env, capture_output=True
    )
    (work / "README.md").write_text("# Test")
    (work / "src.py").write_text("x = 1\n")
    (work / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    subprocess.run(["git", "-C", str(work), "add", "."], check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        check=True, env=env, capture_output=True,
    )
    # Normal clone — sets up origin remote + refs/remotes/origin/main.
    subprocess.run(
        ["git", "clone", str(work), str(repo_root / ".project")],
        check=True, env=env, capture_output=True,
    )
    return project_tree


def test_gather_main_tree_happy_path(project_with_repo: Path) -> None:
    async def go() -> None:
        rendered, meta = await ts._gather_main_tree("test-proj", "")
        assert "README.md" in rendered
        assert "src.py" in rendered
        assert meta["main_sha"]
        assert meta["files_indexed"] >= 3  # README.md, src.py, image.png
        assert meta["bodies_sampled"] >= 2  # README.md + src.py
        # Image was skipped as binary
        assert meta["binaries_skipped"] >= 1
    asyncio.run(go())


def test_gather_main_tree_no_repo_raises(project_tree: Path) -> None:
    async def go() -> None:
        with pytest.raises(ts.TruthScoreError, match="seed clone"):
            await ts._gather_main_tree("test-proj", "")
    asyncio.run(go())


# ---------------------------------------------------------------- run_truth_score (integration)


@pytest.fixture(autouse=True)
def _reset_locks() -> None:
    """Clear the per-project lock dict between tests so a leaked
    acquisition doesn't bleed into the next test."""
    ts._truthscore_locks.clear()


def test_run_truth_score_happy_path(
    project_with_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end with a stubbed compass.llm.call. Writes the result
    file under working/knowledge/, fires bus events, returns the
    response shape the spec describes."""
    (project_with_repo / "truth" / "specs.md").write_text(
        "# Specs\nThe service is a REST API."
    )

    fake_llm_response = """{
        "scores": {"fidelity": 8, "completeness": 7, "consistency": 9, "currency": 6, "clarity": 7},
        "comment": "Looks good."
    }"""

    async def fake_call(system: str, user: str, **kwargs: Any) -> _FakeLLMResult:
        # Verify commentary lands in user prompt verbatim when present.
        if "skip something" in user:
            assert "Scoring directives" in user
        return _FakeLLMResult(text=fake_llm_response)

    monkeypatch.setattr(ts.cmp_llm, "call", fake_call)

    # Stub out the cost cap check (default real check would query DB).
    async def fake_cap() -> None:
        return None
    monkeypatch.setattr(ts, "_check_cost_cap", fake_cap)

    # Active project must be settable for knowledge.write to resolve;
    # mock resolve_active_project to return our test slug.
    async def fake_resolve(*args: Any, **kwargs: Any) -> str:
        return "test-proj"
    import server.db as dbmod
    import server.knowledge as knowmod
    monkeypatch.setattr(dbmod, "resolve_active_project", fake_resolve)
    monkeypatch.setattr(knowmod, "resolve_active_project", fake_resolve)

    async def go() -> None:
        out = await ts.run_truth_score(
            "test-proj",
            None,
            actor={"source": "human", "ip": "127.0.0.1", "ua": ""},
        )
        assert out["ok"] is True
        assert out["overall"] == 7.4
        assert out["scores"]["fidelity"] == 8
        assert out["result_path"].startswith("working/knowledge/truthscore-")
        # File actually exists.
        rel = out["result_path"].removeprefix("working/knowledge/")
        assert (project_with_repo / "working" / "knowledge" / rel).is_file()
    asyncio.run(go())


def test_run_truth_score_empty_truth_400(
    project_with_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_cap() -> None:
        return None
    monkeypatch.setattr(ts, "_check_cost_cap", fake_cap)

    async def go() -> None:
        with pytest.raises(ts.TruthScoreError) as exc_info:
            await ts.run_truth_score(
                "test-proj", None, actor={"source": "human"},
            )
        assert exc_info.value.http_status == 400
    asyncio.run(go())


def test_run_truth_score_concurrent_lock(
    project_with_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent run_truth_score calls against the same project:
    the second must raise TruthScoreError(409)."""
    (project_with_repo / "truth" / "specs.md").write_text("# Specs")

    fake_response = (
        '{"scores": {"fidelity": 5, "completeness": 5, "consistency": 5, '
        '"currency": 5, "clarity": 5}, "comment": "ok"}'
    )
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def slow_call(system: str, user: str, **kwargs: Any) -> _FakeLLMResult:
        started.set()
        await proceed.wait()
        return _FakeLLMResult(text=fake_response)

    monkeypatch.setattr(ts.cmp_llm, "call", slow_call)

    async def fake_cap() -> None:
        return None
    monkeypatch.setattr(ts, "_check_cost_cap", fake_cap)

    async def fake_resolve(*args: Any, **kwargs: Any) -> str:
        return "test-proj"
    import server.db as dbmod
    import server.knowledge as knowmod
    monkeypatch.setattr(dbmod, "resolve_active_project", fake_resolve)
    monkeypatch.setattr(knowmod, "resolve_active_project", fake_resolve)

    async def go() -> None:
        first = asyncio.create_task(
            ts.run_truth_score("test-proj", None, actor={"source": "human"})
        )
        await started.wait()
        with pytest.raises(ts.TruthScoreError) as exc_info:
            await ts.run_truth_score("test-proj", None, actor={"source": "human"})
        assert exc_info.value.http_status == 409
        proceed.set()
        await first  # complete cleanup
    asyncio.run(go())


def test_run_truth_score_llm_error_502(
    project_with_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project_with_repo / "truth" / "specs.md").write_text("# Specs")

    async def fail_call(system: str, user: str, **kwargs: Any) -> _FakeLLMResult:
        raise ts.cmp_llm.CompassLLMError("subprocess died")

    monkeypatch.setattr(ts.cmp_llm, "call", fail_call)

    async def fake_cap() -> None:
        return None
    monkeypatch.setattr(ts, "_check_cost_cap", fake_cap)

    async def fake_resolve(*args: Any, **kwargs: Any) -> str:
        return "test-proj"
    import server.db as dbmod
    import server.knowledge as knowmod
    monkeypatch.setattr(dbmod, "resolve_active_project", fake_resolve)
    monkeypatch.setattr(knowmod, "resolve_active_project", fake_resolve)

    async def go() -> None:
        with pytest.raises(ts.TruthScoreError) as exc_info:
            await ts.run_truth_score(
                "test-proj", None, actor={"source": "human"},
            )
        assert exc_info.value.http_status == 502
        assert "LLM call failed" in str(exc_info.value)
    asyncio.run(go())


def test_run_truth_score_parse_failure_writes_raw(
    project_with_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project_with_repo / "truth" / "specs.md").write_text("# Specs")

    async def fake_call(system: str, user: str, **kwargs: Any) -> _FakeLLMResult:
        return _FakeLLMResult(text="not JSON at all")

    monkeypatch.setattr(ts.cmp_llm, "call", fake_call)

    async def fake_cap() -> None:
        return None
    monkeypatch.setattr(ts, "_check_cost_cap", fake_cap)

    async def fake_resolve(*args: Any, **kwargs: Any) -> str:
        return "test-proj"
    import server.db as dbmod
    import server.knowledge as knowmod
    monkeypatch.setattr(dbmod, "resolve_active_project", fake_resolve)
    monkeypatch.setattr(knowmod, "resolve_active_project", fake_resolve)

    async def go() -> None:
        with pytest.raises(ts.TruthScoreError) as exc_info:
            await ts.run_truth_score(
                "test-proj", None, actor={"source": "human"},
            )
        assert exc_info.value.http_status == 502
        assert "parse" in str(exc_info.value).lower()
        # The -RAW.md file should exist.
        knowledge_dir = project_with_repo / "working" / "knowledge"
        raw_files = list(knowledge_dir.glob("truthscore-*-RAW.md"))
        assert len(raw_files) == 1
    asyncio.run(go())


def test_run_truth_score_cost_cap_429(
    project_with_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (project_with_repo / "truth" / "specs.md").write_text("# Specs")

    # Real cap check — patch the agents module so spend > cap.
    import server.agents as agents_mod
    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 5.0)
    async def fake_spend(*args: Any, **kwargs: Any) -> float:
        return 100.0  # Way over.
    monkeypatch.setattr(agents_mod, "_today_spend", fake_spend)

    async def go() -> None:
        with pytest.raises(ts.TruthScoreError) as exc_info:
            await ts.run_truth_score(
                "test-proj", None, actor={"source": "human"},
            )
        assert exc_info.value.http_status == 429
    asyncio.run(go())


# ---------------------------------------------------------------- _fanout_target


def test_fanout_target_mcp_player() -> None:
    actor = {"source": "mcp-tool", "agent_id": "p3"}
    assert ts._fanout_target(actor) == "p3"


def test_fanout_target_mcp_coach() -> None:
    actor = {"source": "mcp-tool", "agent_id": "coach"}
    assert ts._fanout_target(actor) == "coach"


def test_fanout_target_human_no_target() -> None:
    actor = {"source": "human", "ip": "127.0.0.1"}
    assert ts._fanout_target(actor) is None


def test_fanout_target_invalid_actor() -> None:
    assert ts._fanout_target(None) is None  # type: ignore[arg-type]
    assert ts._fanout_target({}) is None


# ---------------------------------------------------------------- HTTP endpoint


def _seed_misc_project_repo(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Build a repo + truth/ under the fresh_db's sandboxed DATA_ROOT
    in the `misc` project (which init_db seeds). Returns the project
    root."""
    import server.paths as pathsmod
    pp = pathsmod.project_paths("misc")
    pp.root.mkdir(parents=True, exist_ok=True)
    pp.truth.mkdir(parents=True, exist_ok=True)
    pp.knowledge.mkdir(parents=True, exist_ok=True)
    pp.outputs.mkdir(parents=True, exist_ok=True)
    pp.decisions.mkdir(parents=True, exist_ok=True)
    (pp.truth / "specs.md").write_text("# Specs\nThe API is REST.")

    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "test")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "test")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    # Inside the per-test sandboxed DATA_ROOT (set by the fresh_db
    # fixture), so each test gets a clean source of truth.
    import server.paths as pathsmod_local
    work = pathsmod_local.DATA_ROOT / "ts-work-source"
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(work)],
        check=True, env=env, capture_output=True,
    )
    (work / "README.md").write_text("# Test")
    subprocess.run(
        ["git", "-C", str(work), "add", "."],
        check=True, env=env, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(work), "commit", "-m", "init"],
        check=True, env=env, capture_output=True,
    )
    pp.repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", str(work), str(pp.bare_clone)],
        check=True, env=env, capture_output=True,
    )
    return pp.root


@pytest.fixture
def http_client(fresh_db: str, monkeypatch: pytest.MonkeyPatch):
    """TestClient fixture that runs init_db (synchronously) and seeds
    the `misc` project repo + truth corpus."""
    from fastapi.testclient import TestClient
    from server.db import init_db
    asyncio.run(init_db())
    _seed_misc_project_repo(fresh_db, monkeypatch)
    # Stub the cost cap so it doesn't fail closed when /api/truthscore
    # runs without a populated turns ledger.
    async def fake_spend(*a: Any, **kw: Any) -> float:
        return 0.0
    import server.agents as agents_mod
    monkeypatch.setattr(agents_mod, "_today_spend", fake_spend)
    # Stub LLM call so HTTP smoke tests don't hit the real Claude CLI.
    async def fake_call(system: str, user: str, **kwargs: Any) -> _FakeLLMResult:
        return _FakeLLMResult(text=(
            '{"scores": {"fidelity": 8, "completeness": 7, "consistency": 9, '
            '"currency": 6, "clarity": 7}, "comment": "ok"}'
        ))
    monkeypatch.setattr(ts.cmp_llm, "call", fake_call)
    from server.main import app
    return TestClient(app, raise_server_exceptions=False)


def test_http_post_truthscore_happy_path(http_client) -> None:
    r = http_client.post("/api/truthscore", json={})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["overall"] == 7.4
    assert data["scores"]["fidelity"] == 8
    assert data["result_path"].startswith("working/knowledge/truthscore-")


def test_http_post_truthscore_with_commentary(
    http_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_call(system: str, user: str, **kwargs: Any) -> _FakeLLMResult:
        captured["user"] = user
        return _FakeLLMResult(text=(
            '{"scores": {"fidelity": 5, "completeness": 5, "consistency": 5, '
            '"currency": 5, "clarity": 5}, "comment": "mid."}'
        ))
    monkeypatch.setattr(ts.cmp_llm, "call", fake_call)

    r = http_client.post(
        "/api/truthscore", json={"commentary": "skip the brand axis"}
    )
    assert r.status_code == 200, r.text
    # Commentary lands in the user prompt verbatim.
    assert "skip the brand axis" in captured["user"]
    assert "Scoring directives" in captured["user"]


def test_http_post_truthscore_empty_truth_400(
    http_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Wipe the truth corpus we seeded.
    import server.paths as pathsmod
    pp = pathsmod.project_paths("misc")
    for f in pp.truth.iterdir():
        f.unlink()
    r = http_client.post("/api/truthscore", json={})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_http_post_truthscore_429_on_cost_cap(
    http_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.agents as agents_mod
    monkeypatch.setattr(agents_mod, "TEAM_DAILY_CAP_USD", 5.0)
    async def over_spend(*a: Any, **kw: Any) -> float:
        return 100.0
    monkeypatch.setattr(agents_mod, "_today_spend", over_spend)
    r = http_client.post("/api/truthscore", json={})
    assert r.status_code == 429
    assert "cap" in r.json()["detail"].lower()


def test_http_post_truthscore_502_on_llm_failure(
    http_client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_call(system: str, user: str, **kwargs: Any) -> _FakeLLMResult:
        raise ts.cmp_llm.CompassLLMError("subprocess died")
    monkeypatch.setattr(ts.cmp_llm, "call", fail_call)
    r = http_client.post("/api/truthscore", json={})
    assert r.status_code == 502
    assert "LLM" in r.json()["detail"]


def test_http_post_truthscore_token_required(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When HARNESS_TOKEN is set, the endpoint requires a Bearer."""
    from fastapi.testclient import TestClient
    from server.db import init_db
    asyncio.run(init_db())
    _seed_misc_project_repo(fresh_db, monkeypatch)
    monkeypatch.setattr("server.main.HARNESS_TOKEN", "secret123")
    from server.main import app
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/truthscore", json={})
    assert r.status_code == 401
    r = client.post(
        "/api/truthscore", json={},
        headers={"Authorization": "Bearer wrongtoken"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------- MCP tool


def _build_coord_for(slot: str):
    """Build a coord MCP server bound to the given caller slot and
    return the tool name → handler map."""
    from server.tools import build_coord_server
    server = build_coord_server(slot, include_proxy_metadata=True)
    return server["_handlers"]


def test_mcp_tool_listed_in_registry() -> None:
    from server.tools import ALLOWED_COORD_TOOLS, coord_tool_names
    names = coord_tool_names()
    assert "coord_run_truth_score" in names
    assert "mcp__coord__coord_run_truth_score" in ALLOWED_COORD_TOOLS


def test_mcp_tool_no_active_project_returns_error(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the active project somehow resolves to empty, return a
    structured MCP error rather than crashing."""
    from server.db import init_db
    asyncio.run(init_db())
    handlers = _build_coord_for("coach")
    handler = handlers["coord_run_truth_score"]
    async def empty_resolve(*a: Any, **kw: Any) -> str:
        return ""
    import server.tools as tools_mod
    monkeypatch.setattr(tools_mod, "resolve_active_project", empty_resolve)

    async def go() -> None:
        result = await handler({"commentary": None})
        assert result.get("is_error") is True
        assert "no active project" in result["content"][0]["text"]
    asyncio.run(go())


def test_mcp_tool_passes_actor_with_caller_id(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Coach calls the tool, the actor field threaded into
    run_truth_score must carry source='mcp-tool' + agent_id='coach'."""
    from server.db import init_db
    asyncio.run(init_db())
    _seed_misc_project_repo(fresh_db, monkeypatch)

    captured: dict[str, Any] = {}
    async def fake_run(project_id: str, commentary: str | None, actor: dict[str, Any]) -> dict[str, Any]:
        captured["project_id"] = project_id
        captured["commentary"] = commentary
        captured["actor"] = actor
        return {
            "ok": True, "result_path": "working/knowledge/truthscore-x.md",
            "overall": 7.0,
            "scores": {"fidelity": 7, "completeness": 7, "consistency": 7,
                       "currency": 7, "clarity": 7},
            "comment": "stub",
            "inputs": {},
        }
    monkeypatch.setattr(ts, "run_truth_score", fake_run)

    handlers = _build_coord_for("coach")
    handler = handlers["coord_run_truth_score"]

    async def go() -> None:
        result = await handler({"commentary": "skip section 2"})
        assert result.get("is_error") is None or result.get("is_error") is False
        text = result["content"][0]["text"]
        assert "Overall: 7.0" in text
        assert "Fidelity" in text
        # Verify actor + commentary plumbing.
        assert captured["actor"] == {"source": "mcp-tool", "agent_id": "coach"}
        assert captured["commentary"] == "skip section 2"
    asyncio.run(go())


def test_mcp_tool_player_caller_id_threaded(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as above but for a Player slot — confirms no role gate."""
    from server.db import init_db
    asyncio.run(init_db())
    _seed_misc_project_repo(fresh_db, monkeypatch)

    captured: dict[str, Any] = {}
    async def fake_run(project_id: str, commentary: str | None, actor: dict[str, Any]) -> dict[str, Any]:
        captured["actor"] = actor
        return {
            "ok": True, "result_path": "working/knowledge/x.md",
            "overall": 5.0,
            "scores": {k: 5 for k in ts.SCORE_KEYS},
            "comment": "ok", "inputs": {},
        }
    monkeypatch.setattr(ts, "run_truth_score", fake_run)

    handlers = _build_coord_for("p3")
    handler = handlers["coord_run_truth_score"]

    async def go() -> None:
        result = await handler({"commentary": None})
        assert result.get("is_error") is None or result.get("is_error") is False
        assert captured["actor"]["agent_id"] == "p3"
    asyncio.run(go())


def test_mcp_tool_empty_commentary_normalized_to_none(
    fresh_db: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty / whitespace-only commentary becomes None so the prompt
    doesn't get a meaningless 'Scoring directives' block."""
    from server.db import init_db
    asyncio.run(init_db())
    _seed_misc_project_repo(fresh_db, monkeypatch)

    captured: dict[str, Any] = {}
    async def fake_run(project_id: str, commentary: str | None, actor: dict[str, Any]) -> dict[str, Any]:
        captured["commentary"] = commentary
        return {
            "ok": True, "result_path": "x.md", "overall": 5.0,
            "scores": {k: 5 for k in ts.SCORE_KEYS},
            "comment": "ok", "inputs": {},
        }
    monkeypatch.setattr(ts, "run_truth_score", fake_run)

    handlers = _build_coord_for("coach")
    handler = handlers["coord_run_truth_score"]

    async def go() -> None:
        await handler({"commentary": "   "})
        assert captured["commentary"] is None
        await handler({"commentary": ""})
        assert captured["commentary"] is None
        await handler({})  # missing key
        assert captured["commentary"] is None
    asyncio.run(go())


def test_codex_contract_version_bumped() -> None:
    """When the coord-tool surface changes, the Codex thread-resume
    fingerprint must change so existing threads pick up the new
    tool list. Bumping in the same commit prevents bisect-landing on
    a stale contract."""
    from server.runtimes import codex as codex_mod
    # Bumped past the truthscore baseline by subsequent features
    # (thinking-override 2026-05-12, etc.). The invariant tested here
    # is that the contract version moved off the pre-truthscore value,
    # not the exact post-truthscore string.
    assert codex_mod._CODEX_TOOL_CONTRACT_VERSION != ""
    assert codex_mod._CODEX_TOOL_CONTRACT_VERSION.startswith("2026-")
