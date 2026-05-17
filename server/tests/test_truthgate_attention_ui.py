"""Static checks for TruthGate attention affordances in EnvPane."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "server" / "static" / "app.js"
STYLE_CSS = ROOT / "server" / "static" / "style.css"
TRUTHGATE_DOC = ROOT / "Docs" / "truthgate-approach.md"
FRONTEND_SPEC = ROOT / "Docs" / "tot-specs-16-frontend-specification.md"


def test_truthgate_attention_proposal_cards_open_existing_review_surface() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "onReviewProposal" in app_js
    assert "Review proposal" in app_js
    assert "focusProposal" in app_js
    assert 'stored["File-write proposals"] = false;' in app_js
    assert 'data-proposal-id=${String(p.id)}' in app_js
    assert 'row.scrollIntoView({ block: "center", behavior: "smooth" });' in app_js
    assert 'row.querySelector(".env-decision-head")' in app_js

    # The attention card should route to the existing proposal section;
    # approve/deny remain in EnvFileWriteProposalsSection's resolution path.
    assert '"/api/file-write-proposals/" + id + "/" + action' in app_js
    assert "env-truth-approve" in app_js
    assert "env-truth-deny" in app_js


def test_proposal_review_outcomes_require_notes_and_notify_coach() -> None:
    app_js = APP_JS.read_text(encoding="utf-8")

    assert "Note to Coach" in app_js
    assert "Add a reason before denying, dropping, or requesting changes." in app_js
    assert 'body: JSON.stringify({ note: resolutionNote })' in app_js
    assert '"/api/messages"' in app_js
    assert 'to: "coach"' in app_js
    assert "deny/drop" in app_js
    assert "request changes" in app_js
    assert "comment to Coach" in app_js
    assert "Proposal remains pending. Coach should answer the comment" in app_js
    assert "Coach was notified and should revise, archive, or ask" in app_js
    assert 'role="status"' in app_js
    assert 'aria-live="polite"' in app_js


def test_truthgate_attention_review_action_is_styled_and_documented() -> None:
    style_css = STYLE_CSS.read_text(encoding="utf-8")
    truthgate_doc = TRUTHGATE_DOC.read_text(encoding="utf-8")
    frontend_spec = FRONTEND_SPEC.read_text(encoding="utf-8")

    assert ".env-attention-actions" in style_css
    assert ".env-attention-action" in style_css
    assert ".env-truth-note-input" in style_css
    assert ".env-truth-comment" in style_css
    assert ".env-fw-outcome" in style_css
    assert "pending amendment entries include a clear review action" in truthgate_doc
    assert "deny/drop and request-changes outcomes require a human note" in truthgate_doc
    assert "review action opens this same" in frontend_spec
    assert "file-write proposal row" in frontend_spec
    assert "request-changes is" in frontend_spec
    assert "represented as denial" in frontend_spec
    assert "with a prefixed note" in frontend_spec
