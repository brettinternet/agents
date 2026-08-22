# Agents

Agents is a local control plane for running a durable, observable team of AI agents who don't really do anything other than browse the web and poke around the internet. They're not very productive, but they're curious.

## Run

Requires [mise](https://mise.jdx.dev/) and Git.

```sh
mise install
task init
```

Open `http://127.0.0.1:9890`. Run `task dashboard` to print the login token's path.

## Commands

```sh
task server:status  # Show service status
task doctor         # Check prerequisites and ownership
task check          # Run formatting, lint, and type checks
task check:staged   # Run the pre-commit checks against staged files
task fix            # Format and autofix project files
task test           # Run tests
task smoke          # Run the isolated delivery smoke test
task shutdown       # Delete managed sessions and stop services
```

The tracked `Taskfile.dist.yaml` is Task's default project taskfile; a local
`Taskfile.yaml` may override it without being committed.

Configure the project, runtime, CAO provider, web server, and agent roster in
`agents.toml`. Varlock uses two environment files:

| File          | Git       | Purpose                                       |
| ------------- | --------- | --------------------------------------------- |
| `.env.schema` | committed | Optional override contract and secret marking |
| `.env.local`  | ignored   | Local overrides and encrypted secret values   |

`task init` creates `.env.local`, validates it, and installs Lefthook. Set
non-secret overrides directly in `.env.local`. For a fixed web login token, use
`AGENTS_WEB_TOKEN=varlock(prompt)`, then run `task env:check` to store the
device-encrypted value. Use `task env:run -- <command>` for direct commands that
need these overrides and `task env:lock` to lock the local encryption session.

## Scheduled work

Schedules are durable triggers configured in `agents.toml`. Use either a five-field
cron expression with an IANA timezone or an interval ending in `m`, `h`, or `d`.
A message schedule wakes a persistent actor or posts to an internal channel:

```toml
[[schedules]]
slug = "hourly-monitor"
every = "1h"
to = "@explorer"
message = "Check the specified public sources and report only meaningful changes to #findings."
overlap = "skip"
```

Use a work schedule when each occurrence should produce a committed artifact.
It creates a fresh intake item; the elder then refines it as normal. Open-ended
spikes still need lightweight, outcome-oriented acceptance criteria before they
can become ready, such as recording sourced findings, uncertainty, and a
recommendation without requiring a predetermined conclusion.

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

Only `overlap = "skip"` is supported. A message occurrence is active until all
of its deliveries are acknowledged; a work occurrence is active until its item
is delivered or cancelled. Due occurrences are skipped while the previous one
is active. After downtime, at most one missed occurrence runs; older missed
intervals are not replayed. Changing a schedule resets its next occurrence.
Restart the service after changing `agents.toml`.

## Model selection

Set one model for every new terminal run:

```toml
[cao]
model = "openai/gpt-5"
effort = "high"
```

Or choose a model/effort pair uniformly when each run is reserved:

```toml
[cao]
models = [
  { id = "openai/gpt-5", effort = "high" },
  { id = "openai/gpt-5", effort = "medium" },
  { id = "anthropic/claude-sonnet-4-6" },
]
```

An agent actor can override the global choice or pool:

```toml
[[actors]]
slug = "elder"
kind = "agent"
models = [
  { id = "openai/gpt-5", effort = "high" },
]

[[actors]]
slug = "explorer"
kind = "agent"
models = [
  { id = "openai/gpt-5-mini", effort = "medium" },
  { id = "anthropic/claude-sonnet-4-6" },
]
```

Actor choices take precedence over `[cao]`; actors without choices use the global configuration. The selected
pair is persisted for the run, so retries never re-roll it. `effort` is available only with the OpenCode
provider. `AGENTS_MODEL` and optional `AGENTS_EFFORT` override every actor and either TOML
form with one fixed choice.
