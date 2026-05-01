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
    assert paths_in_text == ["00-pricing.md", "10-customers.txt"]


def test_read_truth_facts_walks_subdirectories(fresh_db: str) -> None:
    _seed("alpha", {
        "top.md": "top-level fact",
        "team/roles.md": "Coach delegates; Players execute.",
        "team/locked.md": "p10 is locked for v1.",
    })
    facts = cmp_truth.read_truth_facts("alpha")
    relpaths = [t.text.split(")", 1)[0].lstrip("(") for t in facts]
    # POSIX-style paths even on Windows.
    assert "team/locked.md" in relpaths
    assert "team/roles.md" in relpaths
    assert "top.md" in relpaths


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
