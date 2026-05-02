"""Env-scrub utilities for agent subprocesses.

Agents (Claude / Codex) run with shell tools that can read their own
environment via `printenv`, `env`, `cat /proc/self/environ`, etc. The
harness process holds sensitive values in env (HARNESS_TOKEN,
HARNESS_SECRETS_KEY, KDRIVE_*, TELEGRAM_*, GITHUB_TOKEN, OAuth bits)
that the agent CLIs themselves do NOT need. By scrubbing these from
the subprocess env at spawn time, a prompt-injected agent can't lift
them via a single shell call.

Two helpers, picked by the spawn site:

  - `build_clean_agent_env(extra=...)` — returns a fresh dict starting
    from os.environ, dropping anything not on the allowlist or matching
    a sensitive pattern. Use when the caller controls the env dict
    end-to-end (e.g. `asyncio.create_subprocess_exec(env=...)`).

  - `build_agent_env_overrides()` — returns `{var: ""}` for every
    sensitive var currently in os.environ. Use when the spawn API
    only lets you MERGE INTO the parent env (e.g. the Claude SDK's
    `ClaudeAgentOptions.env`, where the merge order is
    `{**inherited, **options.env}`). Sensitive keys remain present in
    the subprocess env but with empty values — the secret is gone.

This is one defense layer; see the threat-model section in
`Docs/TOT-specs.md` for the full picture (Files API denylist, tool
path denylist, CSP, etc.).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

# Vars the agent CLIs and shell tools actually need. Anything else
# inherited from the harness process is dropped.
#
# Keep this tight — it's easier to allowlist a missing var when we
# discover an agent breaks than to clean up after a leak. Add new
# entries when a real failure happens, with a comment explaining why.
_BASE_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Process basics.
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "OLDPWD",
        "HOSTNAME",
        # Temp dirs (some toolchains crash without these).
        "TMPDIR",
        "TEMP",
        "TMP",
        # Terminal capability detection.
        "TERM",
        "TERMINFO",
        "TERM_PROGRAM",
        "COLORTERM",
        # Locale + timezone.
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_TIME",
        "LC_NUMERIC",
        "LC_COLLATE",
        "TZ",
        # Claude CLI: OAuth persistence directory.
        "CLAUDE_CONFIG_DIR",
        # Codex CLI: ChatGPT session persistence directory.
        "CODEX_HOME",
        # Python toolchain — agents need these to run uv / pytest /
        # ad-hoc python -c calls in their worktrees.
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONDONTWRITEBYTECODE",
        # CA bundles for HTTPS toolchains (curl, requests, npm).
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        # Linux loader paths (rarely set in containers, but harmless).
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        # Node.js — agents may run npm/node in worktrees.
        "NODE_PATH",
        "NODE_OPTIONS",
        # Git author identity (the harness sets defaults at boot;
        # agents inherit so commits attribute correctly). Per-repo
        # config in the worktree's .git/config takes precedence.
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        # The Claude SDK's own filter handles CLAUDECODE; we leave it
        # alone here so SDK behavior stays consistent.
        "CLAUDECODE",
    }
)

# Patterns that ALWAYS match as sensitive, regardless of allowlist
# membership. Anchored by ^ / $ where appropriate to avoid spurious
# substring matches.
_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^HARNESS_"),         # everything harness-config / token / cap
    re.compile(r"^KDRIVE_"),          # WebDAV credentials
    re.compile(r"^WEBDAV_"),          # generic WebDAV vars
    re.compile(r"^TELEGRAM_"),        # bot token + whitelist
    re.compile(r"^OPENAI_"),          # API key fallback path
    re.compile(r"^ANTHROPIC_"),       # belt-and-braces; Max-OAuth means we shouldn't have these
    re.compile(r"^AWS_"),
    re.compile(r"^GCP_"),
    re.compile(r"^GOOGLE_"),
    re.compile(r"^AZURE_"),
    re.compile(r"^DATABASE_URL$"),
    re.compile(r"^MCP_"),             # external-MCP secret bundles
    # Catch-all suffixes — match the common naming conventions for
    # secrets so a future deploy that adds e.g. NOTION_API_KEY is
    # scrubbed automatically without code changes.
    re.compile(r"_TOKEN$"),
    re.compile(r"_SECRET$"),
    re.compile(r"_API_KEY$"),
    re.compile(r"_PASSWORD$"),
    re.compile(r"_PASSWD$"),
    re.compile(r"_PRIVATE_KEY$"),
    re.compile(r"_CREDENTIALS$"),
)


def is_sensitive(name: str) -> bool:
    """True if `name` matches any sensitive pattern. Used both for the
    overrides path (set to "") and the clean path (drop entirely)."""
    return any(p.search(name) for p in _SENSITIVE_PATTERNS)


def is_allowed(name: str) -> bool:
    """A var passes if it's on the allowlist AND not sensitive. Sensitive
    patterns always win — even if a sensitive var is mistakenly added
    to the allowlist, this returns False."""
    if is_sensitive(name):
        return False
    return name in _BASE_ALLOWLIST


def build_clean_agent_env(
    *, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build a fresh subprocess env from os.environ, dropping anything
    not allowlisted and anything sensitive. `extra` is overlaid last
    so callers can add per-spawn vars (e.g. HARNESS_COORD_PROXY_TOKEN
    for the Codex coord proxy).

    Use with `asyncio.create_subprocess_exec(..., env=<this>)` and any
    other API where the caller fully controls the subprocess env dict.
    """
    out: dict[str, str] = {}
    for k, v in os.environ.items():
        if is_allowed(k):
            out[k] = v
    if extra:
        for k, v in extra.items():
            out[k] = v
    return out


def build_agent_env_overrides() -> dict[str, str]:
    """Return `{var: ""}` for every sensitive var currently in os.environ.

    Use when the spawn API merges options.env INTO the parent env
    (e.g. the Claude SDK's `ClaudeAgentOptions.env`, where the SDK
    builds `{**inherited, **options.env}`). Each sensitive key will
    appear in the subprocess env with an empty value — the secret
    string is gone, even though the key itself remains.

    Setting empty rather than deleting is a limitation of the merge-only
    API, not a design choice; the security goal (no readable secret
    values in subprocess env) is met.
    """
    return {k: "" for k in os.environ if is_sensitive(k)}
