---
schema: teamoften-spec/v1
title: 'TruthGate Workflow Contract'
status: canonical
spec_group: truthgate-contract
source_index: truth-index.md
last_reorganized: 2026-05-17
---
# TruthGate Workflow Contract

TruthGate is a gate in the task lifecycle, not a silent metadata update.

When TruthGate records a blocked or action-needed verdict, the harness must surface an actionable Coach-facing signal. This includes verdicts such as:

- `truthgate_needs_truth_change`
- `truthgate_needs_human_clarification`
- classifier failures that leave the task blocked in `truthgate`

The signal may be implemented as a Coach inbox message, wake, or equivalent high-signal notification, but it must be visible enough that Coach can act without manually polling the task board.

The signal must name:

- the task id and title
- the recorded verdict or classifier failure
- the reason, concerns, and truth basis when available
- the expected Coach action, such as proposing a protected truth amendment, asking the human a clarification question, recording an allowed override, or leaving the task blocked deliberately

TruthGate must not wake a Player, auto-advance the stage, or weaken fail-closed semantics when emitting this signal. The task remains in `truthgate` until Coach explicitly decides the next step.

When the human approves a protected truth amendment that originated from a TruthGate-blocked task, the harness must notify Coach to rerun TruthGate with `force=true` or make a deliberate decision. Approval of the truth amendment must not automatically rerun the classifier, auto-advance the task, or wake a Player.

This contract applies to automatic TruthGate runs from Backlog promotion and to manual reruns through `coord_run_truthgate`.
