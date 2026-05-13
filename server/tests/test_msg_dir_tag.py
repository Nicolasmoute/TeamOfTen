"""
Unit tests for the msgDirTag helper logic (t-967cf722).

The JS function msgDirTag(event, viewerSlot) is a pure function.
We mirror its contract in Python here to validate all 6 cases without
needing a JS runtime. The Python implementation EXACTLY mirrors the JS:

  def msg_dir_tag(event, viewer_slot):
      if not viewer_slot:
          return None
      from_id = event.get("agent_id")
      to_id   = event.get("to")
      if to_id == "broadcast":
          return {"cls": "msg-dir-bc", "label": "all", "incoming": False}
      if from_id == viewer_slot:
          return {"cls": "msg-dir-out", "label": slot_short(to_id), "incoming": False}
      if to_id == viewer_slot:
          return {"cls": "msg-dir-in", "label": slot_short(from_id), "incoming": True}
      # third-party observer
      return {"cls": "msg-dir-bc", "label": f"{slot_short(from_id)}{slot_short(to_id)}", "incoming": False}

slotShortLabel mirrors the JS function of the same name.
"""


def slot_short(slot_id: str) -> str:
    """Mirror of JS slotShortLabel()."""
    if slot_id == "coach":
        return "C"
    if slot_id.startswith("p"):
        return slot_id[1:]
    return slot_id[:2]


def msg_dir_tag(event: dict, viewer_slot: str | None) -> dict | None:
    """Mirror of JS msgDirTag(event, viewerSlot)."""
    if not viewer_slot:
        return None
    from_id = event.get("agent_id", "")
    to_id   = event.get("to", "")
    if to_id == "broadcast":
        return {"cls": "msg-dir-bc", "label": "all", "incoming": False}
    if from_id == viewer_slot:
        return {"cls": "msg-dir-out", "label": slot_short(to_id), "incoming": False}
    if to_id == viewer_slot:
        return {"cls": "msg-dir-in", "label": slot_short(from_id), "incoming": True}
    # third-party observer
    return {"cls": "msg-dir-bc", "label": f"{slot_short(from_id)}{slot_short(to_id)}", "incoming": False}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_outgoing_coach_to_p3():
    """Coach pane: coach sends to p3 → outgoing tag → 3."""
    ev = {"agent_id": "coach", "to": "p3"}
    tag = msg_dir_tag(ev, "coach")
    assert tag is not None
    assert tag["cls"] == "msg-dir-out"
    assert tag["label"] == "3"
    assert tag["incoming"] is False


def test_incoming_p3_from_coach():
    """p3 pane: coach sends to p3 → incoming tag ← C."""
    ev = {"agent_id": "coach", "to": "p3"}
    tag = msg_dir_tag(ev, "p3")
    assert tag is not None
    assert tag["cls"] == "msg-dir-in"
    assert tag["label"] == "C"
    assert tag["incoming"] is True


def test_broadcast():
    """Any pane: broadcast message → bidirectional tag, label 'all'."""
    ev = {"agent_id": "coach", "to": "broadcast"}
    tag = msg_dir_tag(ev, "coach")
    assert tag is not None
    assert tag["cls"] == "msg-dir-bc"
    assert tag["label"] == "all"
    assert tag["incoming"] is False


def test_third_party_observer():
    """p3 pane: p1 sends to p2 (p3 is observer via fan-out) → observer tag."""
    ev = {"agent_id": "p1", "to": "p2"}
    tag = msg_dir_tag(ev, "p3")
    assert tag is not None
    assert tag["cls"] == "msg-dir-bc"
    assert tag["label"] == "12"   # slot_short("p1")="1", slot_short("p2")="2"
    assert tag["incoming"] is False


def test_human_thread_outgoing():
    """Coach pane: coach sends to human → outgoing tag, label 'hu' (fallback slice)."""
    ev = {"agent_id": "coach", "to": "human"}
    tag = msg_dir_tag(ev, "coach")
    assert tag is not None
    assert tag["cls"] == "msg-dir-out"
    # slotShortLabel("human") = "hu" (first 2 chars, fallback branch)
    assert tag["label"] == "hu"
    assert tag["incoming"] is False


def test_null_viewer_slot_returns_none():
    """When viewerSlot is None (prop not passed), tag is omitted."""
    ev = {"agent_id": "coach", "to": "p3"}
    assert msg_dir_tag(ev, None) is None
    assert msg_dir_tag(ev, "") is None
