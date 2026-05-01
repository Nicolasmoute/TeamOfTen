# Truth — manifest

User-validated source-of-truth for this project lives in this `truth/`
folder. Agents cannot write to files in here directly. Coach proposes
changes via `coord_propose_truth_update(path, content, summary)`; you
approve in the EnvPane "Truth proposals" section.

This file (`truth-index.md`) is the manifest: a bullet list of what
SHOULD live in this folder, with one-line descriptions. The harness's
EnvPane reads it and renders a checklist with create / open buttons.

## Expected files

- `specs.md` — project specs: goals, scope, structure, key constraints. As content grows, split into topic files (one concept per file) and link from this list.

## Adding files

Add a bullet here in the same `` `filename` — description `` shape, then
either click "create empty" in the EnvPane truth section, or wait for
Coach to propose initial content. `truth-index.md` is itself a normal
truth file — Coach can propose edits via
`coord_propose_truth_update(path="truth-index.md", ...)` and you approve.
