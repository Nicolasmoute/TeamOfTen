"""Pipeline stages for a Compass run.

Each module is a thin function: take state, build a prompt, call the
LLM, parse JSON, return a typed result. The runner applies the
results via `server.compass.mutate` helpers.

Stages:
  - `digest.passive(state, signals)` — passive update from chat /
    commits / notes; small deltas (±0.15).
  - `digest.answer(state, question, answer_text)` — full answer
    digest; deltas up to ±0.5.
  - `questions.generate_batch(state, count)` — daily-run question
    batch.
  - `questions.generate_single(state, asked)` — Q&A session next
    question.
  - `reviews.propose(state, run_count)` — settle + stale candidates.
  - `reviews.detect_duplicates(state)` — duplicate clusters.
  - `regions.auto_merge(state)` — region housekeeping when active
    count > soft cap.
  - `truth_check.check(...)` — contradiction subroutine.
  - `briefing.generate(state, recent)` — daily briefing markdown.
  - `claude_md.generate(state)` + `claude_md.inject(project_id, block)`
    — render and write the marker block.

Pipeline modules are kept dependency-free of one another. The runner
sequences them; tests stub `llm.call` and exercise each in isolation.
"""

from __future__ import annotations
