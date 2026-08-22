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
task test           # Run tests
task smoke          # Run the isolated delivery smoke test
task shutdown       # Delete managed sessions and stop services
```

Configure the project, runtime, CAO provider, web server, and agent roster in `agents.toml`. Optional environment overrides are listed in `example.env`.

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
