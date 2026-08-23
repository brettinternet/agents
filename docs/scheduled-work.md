# Scheduled work

Schedules are durable triggers configured in `agents.toml`. Use either a
five-field cron expression with an IANA timezone or an interval ending in `m`,
`h`, or `d`.

The examples below are inactive until copied into `agents.toml`. Restart the
service after adding or changing a schedule.

## Default mode: recurring nudges for open-ended play

Scheduled messages are the default for recurring wandering, conversations,
experiments, drafts, and “see what happens” prompts. A nudge can produce a
report, a playful synthesis, a reversible experiment, or simply a useful dead
end without creating a worktree or demanding a commit. Generated material may
be exploratory rather than factually rigorous; distinguish generated ideas from
observations. Factual claims drawn from sources still require evidence and
source links.

The default garden has three recurring message rounds:

| Round                                                       | Cadence and recipient   | What it invites                                                                                                                                                                                                                                                                  |
| ----------------------------------------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Elder garden round** (`elder-garden-round`)               | Every 6h to `@elder`    | Notice a loose end or promising thread, start one bounded reversible public-safe action or conversation, and say `nothing worth doing` when there is no worthwhile move.                                                                                                         |
| **Explorer curiosity wander** (`explorer-curiosity-wander`) | Every 8h to `@explorer` | Wander one public web, repository, or memory thread; take at most one bounded reversible public-safe experiment or artifact; share the learning, or say `nothing worth doing`.                                                                                                   |
| **Writer editorial round** (`writer-editorial-round`)       | Every 12h to `@writer`  | Review current publishing requests and take one bounded writing assignment. Writer produces clear, well-structured copy from supplied evidence and returns it to elder unless the account and venue are explicitly authorized. Say `nothing worth doing` if no request is ready. |

All three use `overlap = "skip"` and are message schedules, not
`[schedules.work]` entries. A round may end with no action; no-op rounds are
expected when there is no useful or safe thread to follow. Keep every external
side effect within its explicit authorization and the repository safety rules.

## Message schedule

A message schedule wakes a persistent actor or posts to an internal channel:

```toml
[[schedules]]
slug = "hourly-monitor"
every = "1h"
to = "@explorer"
message = "Check the specified public sources and report only meaningful changes to #findings."
overlap = "skip"
```

Use a message schedule when the occurrence should produce a report, a
non-committed response, or an open-ended prompt for play. Prefer a direct
message or message schedule when the outcome can remain in the conversation.
The five dashboard lanes are presentation labels for tracked work; see
[One-off work](one-off-work.md#dashboard-lanes) for their mapping to unchanged
durable statuses.

## Tracked work schedule

Use a work schedule only when each occurrence should produce a committed
artifact, requires approval or integration, or depends on the durable workflow.
It creates a fresh intake item; the elder then refines it through the normal
tracked-work lifecycle. Do not use a tracked schedule just to make an
experiment, draft, or wandering round feel official.

Open-ended spikes still need lightweight, outcome-oriented acceptance criteria
before they can become ready. A suitable criterion requires sourced findings,
uncertainty, and a recommendation without requiring a predetermined conclusion.

```toml
[[schedules]]
slug = "daily-exploration-memory"
cron = "0 9 * * *"
timezone = "America/Los_Angeles"
overlap = "skip"

[schedules.work]
kind = "spike"
title = "Daily public-web exploration"
problem = "Look for useful developments related to the repository's current interests."
outcome = "Commit a dated public-safe memory with source URLs, uncertainty, and recommended follow-up work."
```

## Stewardship and safety

Only **elder** may propose self-tuning of tracked `agents.toml` or
`agents/*.md`. A proposal must be grounded in observed evidence and carried as
reviewable tracked repository work. It must never mutate `.agents/`, relax
safety rules, or weaken external-authorization gates. Elder may recommend no
change, and a no-op round is a successful outcome.

## Occurrence behavior

Only `overlap = "skip"` is supported.

- A message occurrence remains active until all of its deliveries are
  acknowledged.
- A work occurrence remains active until its item is delivered or cancelled.
- A due occurrence is skipped while the previous occurrence remains active.
- After downtime, at most one missed occurrence runs; older missed intervals are
  not replayed.
- Changing a schedule resets its next occurrence.

## Activate a schedule

1. Add one or more `[[schedules]]` entries to `agents.toml`.
2. Run `task check` to validate configuration and formatting.
3. Restart the service so it reloads `agents.toml`.
4. Confirm the resulting message or intake item in the dashboard at
   `http://127.0.0.1:9890`.
