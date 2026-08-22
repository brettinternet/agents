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

## Model selection

Set one model for every new terminal run:

```toml
[cao]
model = "openai/gpt-5"
reasoning_effort = "high"
```

Or choose a model/reasoning pair uniformly when each run is reserved:

```toml
[cao]
models = [
  { id = "openai/gpt-5", reasoning_effort = "high" },
  { id = "openai/gpt-5", reasoning_effort = "medium" },
  { id = "anthropic/claude-sonnet-4-6" },
]
```

The selected pair is persisted for the run, so retries never re-roll it. `reasoning_effort` is available only
with the OpenCode provider. `AGENTS_MODEL` and optional `AGENTS_REASONING_EFFORT` force a single choice and
override either TOML form.
