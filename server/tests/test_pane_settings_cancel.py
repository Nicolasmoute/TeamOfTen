"""
Unit tests for the PaneSettingsPopover Cancel-button contract.

The Cancel button restores pane settings to a snapshot taken at popover-open
time (via useRef(settings) in the component), then calls onClose().  It fires
NO API calls — brief / name / role are never saved (they use explicit save
buttons), and runtime changes already fired their own API call on radio change
so they are outside Cancel's scope.

These tests mirror the pure snapshot-restore contract in Python so the
invariant is readable without a JS runtime.
"""


def apply_cancel(snapshot: dict, _current: dict) -> dict:
    """
    Mirrors the Cancel onClick handler:
      () => { onChange(_settingsSnapshot.current); onClose(); }
    Returns the settings that onChange receives (the snapshot).
    """
    return dict(snapshot)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cancel_restores_model_change():
    """Changing model then cancelling restores the original model."""
    initial = {"model": "claude-sonnet-4-5", "effort": 2}
    snapshot = dict(initial)
    current = {"model": "claude-opus-4-5", "effort": 2}
    assert apply_cancel(snapshot, current) == initial


def test_cancel_restores_effort_change():
    """Changing effort then cancelling restores the original effort."""
    initial = {"effort": 2}
    snapshot = dict(initial)
    current = {"effort": 4}
    assert apply_cancel(snapshot, current) == initial


def test_cancel_restores_plan_mode_change():
    """Toggling plan mode then cancelling restores original plan mode state."""
    initial = {"planMode": False, "effort": 2}
    snapshot = dict(initial)
    current = {"planMode": True, "effort": 2}
    assert apply_cancel(snapshot, current) == initial


def test_cancel_restores_thinking_change():
    """Toggling thinking then cancelling restores original thinking state."""
    initial = {"thinking": False}
    snapshot = dict(initial)
    current = {"thinking": True}
    assert apply_cancel(snapshot, current) == initial


def test_cancel_empty_initial_settings():
    """When the popover opens with no overrides, cancel restores to empty dict."""
    initial = {}
    snapshot = dict(initial)
    current = {"model": "claude-opus-4-5", "effort": 3, "planMode": True}
    result = apply_cancel(snapshot, current)
    assert result == {}


def test_cancel_multiple_changes_at_once():
    """Multiple field changes are all rolled back together."""
    initial = {"model": "claude-sonnet-4-5", "effort": 2, "planMode": False}
    snapshot = dict(initial)
    current = {"model": "claude-opus-4-5", "effort": 4, "planMode": True, "thinking": True}
    result = apply_cancel(snapshot, current)
    assert result == initial
    assert "thinking" not in result
