# Artifacts

Public-safe, durable files produced by agents belong here when they are not task summaries (`results/`) or curated knowledge (`memory/`). Examples include generated datasets, reusable reports, and small exported assets intended to be committed.

Before committing an artifact:

- include a nearby Markdown file that records its purpose, provenance, generation date, and reproduction steps when applicable;
- exclude credentials, private data, raw authenticated pages, cookies, logs, and transcripts;
- keep the file small enough for ordinary Git; the pre-commit hook rejects staged files larger than 10 MiB; and
- prefer an authoritative public URL plus reproduction instructions over checking in a large generated file.

Uncommitted clones, downloads, scratch files, and private or raw working data belong under ignored `.pi-data/work/` instead.
