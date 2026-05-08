"""Playbook — harness-wide AI orchestration-strategy engine.

A weighted lattice of conceptual, runtime-agnostic statements about
how to coordinate the team (e.g. "audit every code change except
trivially mechanical edits"). Coach reads it on every turn; a daily
reflection turn evolves weights from observed evidence (archived
tasks, audit fails, stalls, Compass verdicts, deviations).

Spec: [Docs/playbook-specs.md](../../Docs/playbook-specs.md). Sibling
to Compass; both share the lattice primitive but Compass is per-project
human intent and Playbook is harness-wide AI orchestration strategy.
"""
