# Truth — index

User-validated source-of-truth for this project lives in this `truth/`
folder. Agents cannot write to files in here directly. Coach proposes
changes via `coord_propose_file_write(scope='truth', path, content,
summary)`; you review a diff and approve in the EnvPane "File-write
proposals" section.

This file (`truth-index.md`) is intentionally a normal markdown file —
use it however serves the project: as a table of contents linking to
sibling files, as a glossary, or just as a single document with the
canonical content inline. There is no enforced structure.

## Adding files

Create new files in the Files pane (use the "+ new file" button on
the pane header). Coach can also propose new files via
`coord_propose_file_write(scope='truth', ...)`; you approve in the
EnvPane. `truth-index.md` is itself a normal truth file — Coach
proposes edits via `coord_propose_file_write(scope='truth',
path="truth-index.md", ...)` and you approve.
