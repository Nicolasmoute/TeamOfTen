---
schema: teamoften-spec/v1
title: 'Verifier Auth Contract'
status: canonical
spec_group: security
source_index: truth-index.md
created: 2026-05-17
---
# Verifier Auth Contract

This contract defines how TeamOfTen verifier roles may perform authenticated smoke checks against protected harness endpoints without exposing operator credentials to agent subprocesses or reports.

## Scope

Verifier auth exists only for post-ship smoke verification of the active TeamOfTen deployments, including TOT-DEV and production TeamOfTen. It is not a general agent secret-access feature, not a replacement for `HARNESS_TOKEN`, and not a way for Players to call arbitrary protected APIs during implementation.

The allowed use cases are narrow:

- Read-only or smoke-style checks needed to prove a shipped task is live.
- Protected endpoints whose behavior cannot be verified through `/api/health`, static assets, public source evidence, or CI alone.
- Task-specific compact/runtime smokes that need the same auth boundary a normal browser/API user would need, while avoiding token disclosure.

## Non-Negotiable Security Rules

`HARNESS_TOKEN` remains deployment process env only. Codex and Claude agent subprocesses must not receive raw `HARNESS_TOKEN` in their environment, system prompt, handoff, task notes, reports, logs, tool outputs, or MCP arguments.

The encrypted secrets table is not a general environment-injection mechanism for agents. Providing `HARNESS_SECRETS_KEY` or raw stored secrets to verifier runtimes is not allowed as the default design.

Verifier-auth implementation must use one of these safe patterns:

- A server-side verifier-smoke proxy endpoint that performs an allowlisted check in-process using deployment credentials and returns only sanitized PASS/FAIL evidence.
- A short-lived, least-privilege smoke token minted server-side for a specific task, deployment, endpoint allowlist, and expiry, with redaction enforced at every reporting/logging boundary.
- An authenticated browser/session handoff controlled by the harness where the agent can observe sanitized result state but cannot read or print bearer secrets.

Directly injecting `HARNESS_TOKEN` into verifier agent env is forbidden. Directly printing or returning a bearer token to the model is forbidden. Any design that requires the verifier to manually assemble `Authorization: Bearer <secret>` from a visible secret is forbidden.

## Capability Boundaries

Verifier auth must be explicit and task-scoped. A verifier may only exercise endpoints and methods needed for the assigned verification.

Default permitted shape:

- `GET`/read-only checks for board, task, health-detail, deployment, or runtime status evidence.
- Purpose-built smoke endpoints that perform a bounded internal action and return sanitized evidence.

Risky or mutating checks require a task-specific allowlist and must be justified in the verifier wake note or task spec. The verifier must not gain broad write access to the harness API.

Verifier-auth artifacts must include:

- Task id.
- Deployment target (`TOT-DEV` or production).
- Endpoint or smoke name checked.
- Timestamp and observed version/SHA/deployment id when available.
- Sanitized PASS/FAIL result and limitation notes.

They must not include raw tokens, encrypted secret values, cookies, auth headers, session IDs, or full request dumps containing credentials.

## Redaction and Logging

All verifier-auth code paths must treat these as sensitive:

- `HARNESS_TOKEN`.
- `HARNESS_SECRETS_KEY`.
- Bearer tokens, cookies, and authorization headers.
- Any short-lived smoke token.
- Secret-store plaintext values.

Sensitive values must be redacted before entering:

- Project events.
- Verification reports.
- Agent messages.
- Runtime logs surfaced to agents.
- Knowledge docs.
- UI timelines.
- Test failure output where practical.

Redaction must be structural where possible: return booleans, labels, ids, and short status text instead of raw request/response payloads.

## Runtime Interaction

Codex coord proxy credentials (`HARNESS_COORD_PROXY_TOKEN`) remain separate from API verifier auth. They only authorize coord MCP proxy calls back to the main process and must not be reused for protected API smokes.

Verifier auth must not weaken Codex runtime sandboxing, the secret-path guard, or the rule that Codex Player subprocesses do not receive `HARNESS_TOKEN`.

If the implementation adds a new coord tool, it must be exposed only to verifier-stage roles when appropriate and included in role allowlists. If it adds an HTTP endpoint, it must be protected by the normal UI/API auth boundary and any additional verifier-smoke authorization needed by the chosen design.

## Configuration

The preferred configuration is no new operator-managed secret when the harness can safely perform the check server-side using the existing deployment auth boundary.

If a short-lived or read-only smoke token is introduced, its env/config must be documented in the environment-variable spec before use. Defaults must be fail-closed: absent verifier-auth configuration means protected smoke checks are skipped with an explicit limitation, not attempted unauthenticated and not performed with broad credentials.

## Tests

Verifier-auth implementation must include focused tests for:

- Verifier can obtain sanitized authenticated evidence for an allowlisted protected smoke.
- Non-verifier roles cannot access the verifier-auth path unless explicitly authorized by task/stage design.
- Raw `HARNESS_TOKEN`, `HARNESS_SECRETS_KEY`, bearer tokens, cookies, and smoke tokens are not returned in reports/events/tool output.
- Disallowed endpoints or methods are rejected.
- Missing configuration fails closed with an actionable limitation.

When implementation touches Codex role allowlists or coord tool exposure, tests must verify the tool is available to verifier roles and not leaked to unrelated executor/idle roles.

## Operational Guidance

If verifier auth is unavailable, verifiers must state the limitation explicitly and may rely on health, deployment status, static asset SHA checks, source evidence, and focused tests. They must not ask Coach or the human to paste secrets into chat.

Coach may require authenticated verification for high-risk protected behavior before archive once this contract has an implementation.
