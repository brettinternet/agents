# Scheduled work

Schedules are durable triggers configured in `agents.toml`. Use either a
five-field cron expression with an IANA timezone or an interval ending in `m`,
`h`, or `d`.

The examples below are inactive until copied into `agents.toml`. Restart the
service after adding or changing a schedule.

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

Use a message schedule when the occurrence should produce a report or other
non-committed response.

## Tracked work schedule

Use a work schedule when each occurrence should produce a committed artifact.
It creates a fresh intake item; the elder then refines it through the normal
tracked-work lifecycle.

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
