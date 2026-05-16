# TruthGate Approach

## Implementation Status

Phase 2 classifier-core implementation is present as a library-only package
under `server/truthgate/`.

Implemented modules:

- `config.py`: TruthGate environment parsing, budget knobs, and strict classifier
  model validation. Defaults are `latest_sonnet` primary and `latest_mini`
  fallback. `latest_opus`, `latest_gpt`, and their current concrete model
  targets are rejected for classifier use.
- `corpus.py`: capped `truth/**/*.{md,txt}` corpus slicing. It does not read
  `Docs/`, repo source, uploads, conversation logs, or secrets.
- `prompts.py`: strict JSON classifier prompt and amendment-draft prompt helper.
- `llm.py`: one-shot primary/fallback wrapper with `agent_id='truthgate'` and
  `cost_basis='truthgate:classifier'` for classifier ledger attribution.
- `classifier.py`: per-project lock, cost-cap preflight, sparse-mode routing,
  strict whole-response JSON parsing, verdict normalization, and truth-basis
  validation.
- `targeted.py`: targeted truth-basis reader for later audit integration.
- `amendments.py`: metadata helper for later truth-amendment wrapper work.
- `sparse.py`: permissive sparse-corpus pass result without an LLM call.

Current tests use mocked LLM calls and cover sparse mode, strict parser failure,
model validation, basis validation, and per-project concurrency locking.

## Current Limits

This phase does not wire full kanban tools or stage gates. Later phases still
need to connect these library APIs to `coord_run_truthgate`, override recording,
truthgate stage exit gating, amendment proposals, attention surfaces, audit
context, and provisional closure checks.

Protected truth mirror tests are temporarily waived by human directive. This
Docs projection records implementation status, but the matching `truth/` mirror
still needs a Coach-mediated protected proposal once the waiver is lifted.
