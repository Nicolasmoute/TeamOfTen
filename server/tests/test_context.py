"""Tests for server/context.py — the governance-layer doc store.

All tests isolate CONTEXT_DIR to a tempfile so they don't write into
/data/context on the host. kDrive is left disabled (env vars unset), so
only the local cache path is exercised — which matches how the harness
runs in CI / on a dev laptop.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import server.context as ctxmod


@pytest.fixture
def tmp_ctx(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect CONTEXT_DIR to a fresh tempdir per test + bust the
    module-level TTL cache so tests don't see each other's state."""
    d = Path(tempfile.mkdtemp(prefix="harness-ctx-"))
    monkeypatch.setattr(ctxmod, "CONTEXT_DIR", d)
    ctxmod._invalidate_list_cache()
    return d


# ---------- validate ----------


def test_validate_accepts_root_blank_and_claude() -> None:
    assert ctxmod.validate("root", "") is None
    assert ctxmod.validate("root", "CLAUDE") is None


def test_validate_rejects_unknown_kind() -> None:
    assert ctxmod.validate("bogus", "x") is not None


def test_validate_rejects_bad_name_chars() -> None:
    for bad in ("../etc", "a/b", "has space", "", "-leadingdash", "x" * 80):
        assert ctxmod.validate("skills", bad) is not None, bad


def test_validate_accepts_reasonable_names() -> None:
    for good in ("a", "debug-via-logs", "v1.2", "X_123"):
        assert ctxmod.validate("skills", good) is None, good


# ---------- write / read / delete ----------


async def test_write_then_read_roundtrip(tmp_ctx: Path) -> None:
    await ctxmod.write("skills", "foo", "hello\n")
    assert (tmp_ctx / "skills" / "foo.md").read_text() == "hello\n"
    assert await ctxmod.read("skills", "foo") == "hello\n"


async def test_write_root_ignores_name_and_lands_as_claude_md(tmp_ctx: Path) -> None:
    await ctxmod.write("root", "", "top-level brief")
    assert (tmp_ctx / "CLAUDE.md").read_text() == "top-level brief"
    # Round-trip with explicit CLAUDE name too.
    assert await ctxmod.read("root", "CLAUDE") == "top-level brief"


async def test_write_rejects_empty_body(tmp_ctx: Path) -> None:
    with pytest.raises(ValueError):
        await ctxmod.write("skills", "foo", "")
    with pytest.raises(ValueError):
        await ctxmod.write("skills", "foo", "   \n  ")


async def test_write_rejects_oversize_body(tmp_ctx: Path) -> None:
    huge = "x" * (ctxmod.MAX_BODY_CHARS + 1)
    with pytest.raises(ValueError):
        await ctxmod.write("skills", "foo", huge)


async def test_write_rejects_bad_name(tmp_ctx: Path) -> None:
    with pytest.raises(ValueError):
        await ctxmod.write("skills", "../etc/passwd", "body")


async def test_delete_removes_and_is_idempotent(tmp_ctx: Path) -> None:
    await ctxmod.write("rules", "no-mocks", "body")
    target = tmp_ctx / "rules" / "no-mocks.md"
    assert target.exists()
    await ctxmod.delete("rules", "no-mocks")
    assert not target.exists()
    # Second delete should not raise — the module is idempotent.
    await ctxmod.delete("rules", "no-mocks")


# ---------- list_all / cache ----------


async def test_list_all_reflects_writes(tmp_ctx: Path) -> None:
    await ctxmod.write("root", "", "top")
    await ctxmod.write("skills", "alpha", "a")
    await ctxmod.write("skills", "beta", "b")
    await ctxmod.write("rules", "one", "r")
    listing = await ctxmod.list_all()
    assert listing == {
        "root": ["CLAUDE"],
        "skills": ["alpha", "beta"],
        "rules": ["one"],
    }


async def test_list_all_cache_busts_on_write(tmp_ctx: Path) -> None:
    first = await ctxmod.list_all()
    assert first == {"root": [], "skills": [], "rules": []}
    await ctxmod.write("skills", "new", "body")
    # Cache must be busted by the write, otherwise 'new' wouldn't
    # appear until the TTL expires.
    second = await ctxmod.list_all()
    assert second["skills"] == ["new"]


async def test_list_all_returns_defensive_copy(tmp_ctx: Path) -> None:
    await ctxmod.write("skills", "x", "body")
    a = await ctxmod.list_all()
    a["skills"].append("MUTATED")
    b = await ctxmod.list_all()
    assert "MUTATED" not in b["skills"]


# ---------- system prompt assembly ----------


async def test_build_system_prompt_suffix_empty_when_no_docs(tmp_ctx: Path) -> None:
    assert await ctxmod.build_system_prompt_suffix() == ""


async def test_build_system_prompt_suffix_orders_claude_rules_skills(tmp_ctx: Path) -> None:
    await ctxmod.write("skills", "zskill", "skill-body")
    await ctxmod.write("rules", "arule", "rule-body")
    await ctxmod.write("root", "", "claude-body")
    suffix = await ctxmod.build_system_prompt_suffix()
    # CLAUDE.md first, then rules, then skills — invariant so agents
    # see hard rules before soft skills.
    claude_pos = suffix.index("claude-body")
    rule_pos = suffix.index("rule-body")
    skill_pos = suffix.index("skill-body")
    assert claude_pos < rule_pos < skill_pos
