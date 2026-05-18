from __future__ import annotations

import json

import pytest

from server.db import configured_conn, init_db
from server.verifier_smokes import (
    VerifierSmokeError,
    contains_live_secret,
    redact_sensitive_text,
    run_verifier_smoke,
    sanitize_verifier_evidence,
    validate_smoke_params,
)


async def _seed_task(task_id: str = "t-2026-05-17-6d918984") -> None:
    c = await configured_conn()
    try:
        await c.execute(
            "INSERT INTO tasks (id, project_id, title, status, owner, "
            "created_by, trajectory) VALUES (?, 'misc', 'smoke task', "
            "'verify', 'p3', 'coach', '[]')",
            (task_id,),
        )
        await c.commit()
    finally:
        await c.close()


async def test_allowlisted_smoke_returns_sanitized_pass_evidence(
    fresh_db: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "live-token-123")
    await init_db()
    await _seed_task()

    result = await run_verifier_smoke(
        caller_id="p4",
        task_id="t-2026-05-17-6d918984",
        target="local",
        smoke="health_detail",
        params={},
    )

    assert result["status"] == "PASS"
    assert result["task_id"] == "t-2026-05-17-6d918984"
    assert result["target"] == "local"
    assert result["smoke"] == "health_detail"
    assert result["observed"]["auth_required"] is True
    assert "live-token-123" not in json.dumps(result)


def test_disallowed_smoke_and_params_reject() -> None:
    with pytest.raises(VerifierSmokeError):
        validate_smoke_params("raw_http", {})
    with pytest.raises(VerifierSmokeError):
        validate_smoke_params("health_detail", {"url": "https://example.test"})
    with pytest.raises(VerifierSmokeError):
        validate_smoke_params("health_detail", {"headers": {"x": "y"}})
    with pytest.raises(VerifierSmokeError):
        validate_smoke_params("health_detail", {"method": "POST"})
    with pytest.raises(VerifierSmokeError):
        validate_smoke_params("health_detail", {"body": "{}"})
    with pytest.raises(VerifierSmokeError):
        validate_smoke_params("health_detail", {"unexpected": True})


async def test_missing_remote_target_config_blocks_fail_closed(
    fresh_db: str,
) -> None:
    await init_db()
    await _seed_task()

    result = await run_verifier_smoke(
        caller_id="p4",
        task_id="t-2026-05-17-6d918984",
        target="tot-dev",
        smoke="status_summary",
        params={},
    )

    assert result["status"] == "BLOCKED"
    assert "not configured" in result["limitations"][0]


def test_sanitizer_redacts_secret_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_TOKEN", "exact-harness-token")
    monkeypatch.setenv("HARNESS_SECRETS_KEY", "exact-secret-key")

    raw = (
        "HARNESS_TOKEN=exact-harness-token\n"
        "HARNESS_SECRETS_KEY=exact-secret-key\n"
        "Authorization: Bearer abcdefghijklmnop\n"
        "Cookie: sid=secret-session\n"
        "Set-Cookie: other=secret\n"
        "smoke_token=very-secret-smoke-token\n"
    )
    redacted = redact_sensitive_text(raw)

    assert contains_live_secret(raw)
    assert "exact-harness-token" not in redacted
    assert "exact-secret-key" not in redacted
    assert "abcdefghijklmnop" not in redacted
    assert "sid=secret-session" not in redacted
    assert "very-secret-smoke-token" not in redacted


def test_sanitized_output_omits_session_and_auth_fields() -> None:
    clean = sanitize_verifier_evidence({
        "ok": True,
        "session_id": "raw-session-id",
        "codex_thread_id": "raw-thread-id",
        "headers": {"Authorization": "Bearer rawbearertoken"},
        "nested": ["Cookie: sid=abc123"],
    })

    dumped = json.dumps(clean)
    assert "raw-session-id" not in dumped
    assert "raw-thread-id" not in dumped
    assert "rawbearertoken" not in dumped
    assert "sid=abc123" not in dumped
