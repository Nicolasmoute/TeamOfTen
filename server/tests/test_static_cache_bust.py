"""Regression tests for browser-cache-sensitive static module imports."""

from __future__ import annotations

from pathlib import Path

from server.main import _cache_bust_module_imports

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def test_cache_bust_rewrites_kanban_module_import() -> None:
    src = (
        'import { CompassPane } from "/static/compass.js?v=111";\n'
        'import { KanbanPane } from "/static/kanban.js";\n'
        'import { PlaybookPane } from "/static/playbook.js";\n'
    )

    out = _cache_bust_module_imports(
        src,
        {
            "compass.js": "222",
            "kanban.js": "333",
        },
    )

    assert 'from "/static/compass.js?v=222"' in out
    assert 'from "/static/kanban.js?v=333"' in out
    assert 'from "/static/kanban.js";' not in out


def test_backlog_promote_event_reaches_kanban_refresh_paths() -> None:
    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    kanban_js = (STATIC_DIR / "kanban.js").read_text(encoding="utf-8")

    assert '"backlog_task_promoted"' in app_js
    assert 'evt.type === "backlog_task_promoted") refresh();' in kanban_js
    assert 'if (backlogWatched.has(evt.type)) refreshBacklog();' in kanban_js
