"""Regression guard for human_attention replies from EnvAttentionSection.

The attention card now exposes a reply composer that sends a human → coach
message through the existing /api/messages pipeline. This test pins the
source-level wiring so the card doesn't silently fall back to dismiss-only
behaviour again.
"""
from __future__ import annotations


def test_human_attention_reply_ui_wires_to_messages_api() -> None:
    with open("server/static/app.js", encoding="utf-8") as fh:
        src = fh.read()

    assert "HumanAttentionReplyForm" in src
    assert 'ev.type === "human_attention"' in src
    assert 'to: "coach"' in src
    assert 'subject: replySubject(event.subject)' in src
    assert 'authFetch("/api/messages"' in src
