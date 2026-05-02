# Wiki entry properties (frontmatter) reference

Properties live in YAML frontmatter at the top of a wiki `.md` file,
between two `---` lines. They're indexed by Obsidian (when the human
views the wiki via kDrive sync) and also drive the harness's wiki
INDEX automation.

```yaml
---
title: WebDAV conflict detection
date: 2026-04-25
tags:
  - sync
  - webdav
aliases:
  - kDrive sync conflicts
status: in-progress
links:
  - /data/wiki/sync-state.md
  - /data/wiki/misc/upload-flow.md
---
```

## Property types

| Type | Example | Notes |
|------|---------|-------|
| Text | `title: My title` | Quote if value contains `:`, `#`, `[`, or starts with a number |
| Number | `rating: 4.5` | Integers and floats |
| Checkbox | `completed: true` | YAML booleans (`true`/`false`) |
| Date | `date: 2026-04-25` | ISO 8601 (`YYYY-MM-DD`) — Obsidian and the wiki INDEX both expect this format |
| Date & Time | `due: 2026-04-25T14:30:00` | ISO 8601 with `T` separator |
| List | `tags: [one, two]` or YAML block list | Block list (one item per line, `- ` prefix) is preferred for readability |
| Links | `links: ["[[Other Note]]"]` (Obsidian) OR plain absolute paths | This wiki uses **standard markdown link paths** (e.g. `/data/wiki/foo.md`) instead of `[[wikilinks]]` — see the SKILL.md "Linking" section |

## Standard properties

These get special handling in Obsidian and/or the harness:

- **`title`** — display title for the entry. Defaults to filename if absent.
- **`tags`** — list of topic tags. Searchable in Obsidian's tag pane;
  also feeds the wiki INDEX category grouping.
- **`aliases`** — alternative names for the entry. Obsidian uses
  these for link-suggestion auto-complete; the harness ignores them.
- **`cssclasses`** — CSS classes applied to the note in Obsidian
  reading view. Harmless in the harness (we don't read this).
- **`created`** / **`updated`** — ISO dates. The harness wiki INDEX
  shows `updated`; Obsidian sorts by `date` if present.
- **`links`** — list of related wiki entries. Authored by the agent
  during ingest/lint; supplements the inline body links for graph
  view in Obsidian.
- **`status`** — free-form text. Common values in this wiki:
  `draft`, `in-progress`, `stable`, `superseded-by:<other-entry>`.

## Tag syntax

Tags can contain letters, numbers (not as the first character),
underscores, hyphens, and forward slashes (for hierarchy):

```yaml
tags:
  - sync
  - sync/webdav        # nested
  - perf-2026-q2
  - notebook_2
```

Inline `#tag` syntax also works in the body and is indexed by
Obsidian — useful when you want to tag a specific paragraph rather
than the whole entry.

## When properties matter vs. don't

For **harness-only** consumption: only `title`, `tags`, `created`,
`updated`, `links` are read. Everything else is documentation for
the human viewing in Obsidian (or for future automation).

For **Obsidian-side** consumption: any of the above plus user-defined
properties (e.g. `priority: 3`) become filterable in Obsidian Bases
queries. Useful if you're maintaining structured metadata.
