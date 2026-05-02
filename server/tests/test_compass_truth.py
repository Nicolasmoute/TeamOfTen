"""Tests for `server.compass.truth` — the project-truth-folder adapter.

Compass reads truth from `<project>/truth/*.md|.txt` on every run.
This module covers that read path:
  - allowed extensions are surfaced; others are ignored
  - empty / missing folder → empty list
  - truncation cap (long files get a marker)
  - 1-based stable indices, sorted by relpath
  - the `index → path` map matches the fact list
  - subdirectories under truth/ are walked
  - the corpus hash is stable across calls and changes when content does
"""

from __future__ import annotations

import pytest

from server.compass import truth as cmp_truth
from server.compass.pipeline import truth_derive as pl_truth_derive
from server.paths import project_paths


def _seed(project_id: str, files: dict[str, str]) -> None:
    pp = project_paths(project_id)
    pp.truth.mkdir(parents=True, exist_ok=True)
    for relpath, body in files.items():
        target = pp.truth / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def test_read_truth_facts_empty_when_folder_missing(fresh_db: str) -> None:
    facts = cmp_truth.read_truth_facts("alpha")
    assert facts == []


def test_read_truth_facts_empty_when_folder_present_but_empty(fresh_db: str) -> None:
    pp = project_paths("alpha")
    pp.truth.mkdir(parents=True, exist_ok=True)
    facts = cmp_truth.read_truth_facts("alpha")
    assert facts == []


def test_read_truth_facts_picks_up_md_and_txt_only(fresh_db: str) -> None:
    _seed("alpha", {
        "00-pricing.md": "Per-task billing is required.",
        "10-customers.txt": "Customers are technical.",
        "schema.json": '{"x": 1}',
        "binary.png": "fake binary",
        "config.yaml": "key: value",
    })
    facts = cmp_truth.read_truth_facts("alpha")
    assert [t.index for t in facts] == [1, 2]
    paths_in_text = [t.text.split(")", 1)[0].lstrip("(") for t in facts]
    # Project-root-relative paths (commit 2026-05-02 — folded
    # project-objectives.md into the corpus, switched to project-root
    # paths so the dashboard can build /data/projects/<id>/<path>
    # uniformly across both sources).
    assert paths_in_text == ["truth/00-pricing.md", "truth/10-customers.txt"]


def test_read_truth_facts_walks_subdirectories(fresh_db: str) -> None:
    _seed("alpha", {
        "top.md": "top-level fact",
        "team/roles.md": "Coach delegates; Players execute.",
        "team/locked.md": "p10 is locked for v1.",
    })
    facts = cmp_truth.read_truth_facts("alpha")
    relpaths = [t.text.split(")", 1)[0].lstrip("(") for t in facts]
    # POSIX-style paths even on Windows; project-root-relative.
    assert "truth/team/locked.md" in relpaths
    assert "truth/team/roles.md" in relpaths
    assert "truth/top.md" in relpaths


def test_read_truth_facts_skips_blank_files(fresh_db: str) -> None:
    _seed("alpha", {"empty.md": "   \n\n", "real.md": "Real fact."})
    facts = cmp_truth.read_truth_facts("alpha")
    assert len(facts) == 1
    assert "Real fact" in facts[0].text


def test_read_truth_facts_truncates_long_content(fresh_db: str) -> None:
    big = "X" * (cmp_truth.MAX_FACT_CHARS + 200)
    _seed("alpha", {"big.md": big})
    facts = cmp_truth.read_truth_facts("alpha")
    assert len(facts) == 1
    assert "[truncated" in facts[0].text


def test_read_truth_index_to_path_matches_fact_list(fresh_db: str) -> None:
    _seed("alpha", {
        "a.md": "first",
        "b.md": "second",
        "z/sub.md": "third",
    })
    facts = cmp_truth.read_truth_facts("alpha")
    idx_to_path = cmp_truth.read_truth_index_to_path("alpha")
    for t in facts:
        assert t.index in idx_to_path
        assert idx_to_path[t.index] in t.text  # path is prefixed onto text


def test_truth_corpus_hash_stable_across_calls(fresh_db: str) -> None:
    _seed("alpha", {"a.md": "stable content"})
    facts1 = cmp_truth.read_truth_facts("alpha")
    facts2 = cmp_truth.read_truth_facts("alpha")
    h1 = pl_truth_derive.truth_corpus_hash(facts1)
    h2 = pl_truth_derive.truth_corpus_hash(facts2)
    assert h1 == h2


def test_truth_corpus_hash_changes_when_content_changes(fresh_db: str) -> None:
    _seed("alpha", {"a.md": "version 1"})
    facts1 = cmp_truth.read_truth_facts("alpha")
    h1 = pl_truth_derive.truth_corpus_hash(facts1)

    _seed("alpha", {"a.md": "version 2 — changed"})
    facts2 = cmp_truth.read_truth_facts("alpha")
    h2 = pl_truth_derive.truth_corpus_hash(facts2)
    assert h1 != h2


def test_truth_corpus_hash_empty_corpus_is_stable(fresh_db: str) -> None:
    h1 = pl_truth_derive.truth_corpus_hash([])
    h2 = pl_truth_derive.truth_corpus_hash([])
    assert h1 == h2


# ============================================================
# project-objectives.md folded into the truth corpus
# ============================================================


def _seed_objectives(project_id: str, body: str) -> None:
    pp = project_paths(project_id)
    pp.root.mkdir(parents=True, exist_ok=True)
    pp.project_objectives.write_text(body, encoding="utf-8")


def test_objectives_file_appears_in_truth_corpus(fresh_db: str) -> None:
    """`project-objectives.md` at the project root is part of the
    truth corpus alongside files in `<project>/truth/`. Both drive
    truth-derive and truth-check identically."""
    _seed("alpha", {"specs.md": "Per-task billing is binding."})
    _seed_objectives(
        "alpha",
        "## Q3 objectives\n\n- Land per-task billing v1.\n- Win 5 design partners.",
    )

    facts = cmp_truth.read_truth_facts("alpha")
    relpaths = [
        t.text.split(")", 1)[0].lstrip("(") for t in facts
    ]
    assert "truth/specs.md" in relpaths
    assert "project-objectives.md" in relpaths


def test_truth_corpus_paths_are_project_root_relative(fresh_db: str) -> None:
    """Compass uses project-root-relative relpaths so the dashboard
    can build `/data/projects/<id>/<relpath>` for both `truth/foo.md`
    and `project-objectives.md` without special-casing."""
    _seed("alpha", {"specs.md": "x", "team/roles.md": "y"})
    _seed_objectives("alpha", "z")

    idx_to_path = cmp_truth.read_truth_index_to_path("alpha")
    paths = set(idx_to_path.values())
    assert "truth/specs.md" in paths
    assert "truth/team/roles.md" in paths
    assert "project-objectives.md" in paths
    # No path should accidentally start with a slash.
    assert all(not p.startswith("/") for p in paths)


def test_objectives_only_no_truth_folder(fresh_db: str) -> None:
    """Project with NO `truth/` folder but with project-objectives.md
    still has a non-empty corpus."""
    _seed_objectives("alpha", "Objectives content.")
    facts = cmp_truth.read_truth_facts("alpha")
    assert len(facts) == 1
    assert "project-objectives.md" in facts[0].text
    assert "Objectives content." in facts[0].text


def test_truth_only_no_objectives_file(fresh_db: str) -> None:
    """Symmetric — project with truth/ files but no objectives file
    still works (objectives file is optional)."""
    _seed("alpha", {"specs.md": "Spec."})
    facts = cmp_truth.read_truth_facts("alpha")
    assert len(facts) == 1
    assert "truth/specs.md" in facts[0].text


def test_corpus_hash_changes_when_objectives_change(fresh_db: str) -> None:
    """Editing project-objectives.md must trigger a fresh truth-derive
    on the next run — same as editing any other truth file."""
    _seed("alpha", {"specs.md": "stable spec"})
    _seed_objectives("alpha", "v1 objectives")
    h1 = pl_truth_derive.truth_corpus_hash(cmp_truth.read_truth_facts("alpha"))

    _seed_objectives("alpha", "v2 objectives — changed")
    h2 = pl_truth_derive.truth_corpus_hash(cmp_truth.read_truth_facts("alpha"))
    assert h1 != h2


def test_objectives_blank_file_skipped(fresh_db: str) -> None:
    """Empty/whitespace-only objectives shouldn't pollute the corpus
    with a no-content fact."""
    _seed("alpha", {"specs.md": "real spec"})
    _seed_objectives("alpha", "   \n\n  ")
    facts = cmp_truth.read_truth_facts("alpha")
    relpaths = [t.text.split(")", 1)[0].lstrip("(") for t in facts]
    assert "project-objectives.md" not in relpaths
    assert "truth/specs.md" in relpaths


# ============================================================
# project wiki folded into the truth corpus
# ============================================================


def _seed_wiki(project_id: str, files: dict[str, str]) -> None:
    """Seed `/data/wiki/<project_id>/<relpath>` with the given files."""
    from server.paths import global_paths

    wiki_root = global_paths().wiki / project_id
    wiki_root.mkdir(parents=True, exist_ok=True)
    for relpath, body in files.items():
        target = wiki_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def test_wiki_entries_appear_in_truth_corpus(fresh_db: str) -> None:
    """Per-project wiki entries are folded into the truth corpus and
    surface with the synthetic `wiki/` prefix on their relpath."""
    _seed("alpha", {"specs.md": "Specs body."})
    _seed_wiki("alpha", {
        "stakeholders.md": "Acme cares about response latency.",
        "domain/glossary.md": "MAU = monthly active users.",
    })
    facts = cmp_truth.read_truth_facts("alpha")
    relpaths = [t.text.split(")", 1)[0].lstrip("(") for t in facts]
    assert "truth/specs.md" in relpaths
    assert "wiki/stakeholders.md" in relpaths
    assert "wiki/domain/glossary.md" in relpaths


def test_wiki_index_to_path_returns_synthetic_prefix(fresh_db: str) -> None:
    """`read_truth_index_to_path` returns the same display relpath the
    LLM sees — wiki entries keep the `wiki/` prefix so the dashboard
    can branch on it when composing Files-pane links."""
    _seed_wiki("alpha", {"gotchas.md": "Don't run migrations on Friday."})
    idx_to_path = cmp_truth.read_truth_index_to_path("alpha")
    paths = set(idx_to_path.values())
    assert "wiki/gotchas.md" in paths


def test_wiki_only_no_truth_or_objectives(fresh_db: str) -> None:
    """A project with no truth/ folder and no objectives but a populated
    wiki still has a non-empty corpus."""
    _seed_wiki("alpha", {"context.md": "Wiki-only project context."})
    facts = cmp_truth.read_truth_facts("alpha")
    assert len(facts) == 1
    assert "wiki/context.md" in facts[0].text
    assert "Wiki-only project context." in facts[0].text


def test_wiki_walk_skips_unsupported_extensions(fresh_db: str) -> None:
    """Wiki tree walk only picks up the same extensions as truth/ —
    `.md`, `.markdown`, `.txt`. Other files are ignored."""
    _seed_wiki("alpha", {
        "good.md": "Wiki entry.",
        "asset.png": "binary",
        "settings.json": '{"x": 1}',
    })
    facts = cmp_truth.read_truth_facts("alpha")
    relpaths = [t.text.split(")", 1)[0].lstrip("(") for t in facts]
    assert relpaths == ["wiki/good.md"]


def test_wiki_walk_isolated_per_project(fresh_db: str) -> None:
    """`/data/wiki/<other_id>/` entries must NOT bleed into project A's
    corpus — each project sees only its own wiki sub-tree."""
    _seed_wiki("alpha", {"alpha-only.md": "alpha entry"})
    _seed_wiki("beta", {"beta-only.md": "beta entry"})
    alpha_facts = cmp_truth.read_truth_facts("alpha")
    alpha_paths = [t.text.split(")", 1)[0].lstrip("(") for t in alpha_facts]
    assert "wiki/alpha-only.md" in alpha_paths
    assert "wiki/beta-only.md" not in alpha_paths


def test_corpus_hash_changes_when_wiki_changes(fresh_db: str) -> None:
    """Editing a wiki entry must trigger a fresh truth-derive on the
    next run — same as editing any other corpus source."""
    _seed_wiki("alpha", {"context.md": "v1 context"})
    h1 = pl_truth_derive.truth_corpus_hash(cmp_truth.read_truth_facts("alpha"))

    _seed_wiki("alpha", {"context.md": "v2 context — changed"})
    h2 = pl_truth_derive.truth_corpus_hash(cmp_truth.read_truth_facts("alpha"))
    assert h1 != h2


def test_blank_wiki_entry_skipped(fresh_db: str) -> None:
    """Empty/whitespace-only wiki files don't pollute the corpus."""
    _seed("alpha", {"specs.md": "real spec"})
    _seed_wiki("alpha", {"empty.md": "   \n\n  ", "real.md": "real wiki body"})
    facts = cmp_truth.read_truth_facts("alpha")
    relpaths = [t.text.split(")", 1)[0].lstrip("(") for t in facts]
    assert "wiki/empty.md" not in relpaths
    assert "wiki/real.md" in relpaths
    assert "truth/specs.md" in relpaths
