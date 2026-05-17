"""Static regressions for the Playbook dashboard workflow surface."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK_JS = ROOT / "static" / "playbook.js"
PLAYBOOK_CSS = ROOT / "static" / "playbook.css"


def test_dashboard_exposes_capacity_warning_merge_and_ordered_create_controls() -> None:
    js = PLAYBOOK_JS.read_text(encoding="utf-8")
    css = PLAYBOOK_CSS.read_text(encoding="utf-8")

    assert "aboveSoft" in js
    assert "Above soft cap: growth is constrained" in js
    assert "/proposals/merge/" in js
    assert "/proposals/batch" in js
    assert "Apply in Order" in js
    assert "moveCreation" in js

    assert ".pb-banner-warn" in css
    assert ".pb-workflows" in css
    assert ".pb-create-item" in css
