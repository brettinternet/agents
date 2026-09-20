# Repository memory

This directory is the agents' public, durable memory. It contains compact Markdown notes, not transcripts or hidden runtime state.

## Layout

- `journal/YYYY-MM.md` — dated outcomes, decisions, and lessons.
- `knowledge/<topic>.md` — reusable notes promoted after repeated use.

Current journal: [`journal/2026-08.md`](journal/2026-08.md)

Create `knowledge/` only when the first note qualifies; do not add placeholders.

## Search first

Search before reading broadly or writing a duplicate:

```sh
task memory:grep -- 'exact terms'
task memory:semantic -- 'natural-language description of the memory'
task memory:list
task memory:pick
```

The toolkit is intentionally plain:

- `rg` for fast exact and regular-expression search.
- `jegrep` for semantic search through Jev (`OPENROUTER_API_KEY` in `secrets.sops.env`).
- `fd` for finding notes.
- `fzf` for interactive selection.

Read only likely matches. Memory is evidence, not authority: recheck time-sensitive notes against current code, live services, and primary sources.

## Write selectively

Add a journal entry only when it:

- is safe to publish in this repository;
- records an outcome, decision, constraint, or reusable lesson rather than raw activity;
- is likely to change a future decision or prevent meaningful repeated work;
- is not already represented better by code, Git history, documentation, or a primary source; and
- includes a date and provenance.

Never store credentials, tokens, private messages, personal or proprietary data, authentication artifacts, raw transcripts, or unreviewed web content.

Use this shape:

```markdown
## YYYY-MM-DD — Short outcome

- Kind: experiment | decision | TIL | artifact
- Outcome: What was learned or produced.
- Why it matters: How this changes future work.
- Sources: Public URLs, repository paths, or commit identifiers.
- Freshness: Stable, or the condition/date that requires rechecking.
```

## Promote proven knowledge

Move a journal item to `knowledge/<topic>.md` only after it has affected a decision, prevented repeated work, or proved reusable more than once. Consolidate rather than copy. Include the last verified date, why the note matters, sources, and explicit recheck conditions. Update this index when adding or renaming a knowledge note, and supersede incorrect material in place so search does not return competing guidance.
