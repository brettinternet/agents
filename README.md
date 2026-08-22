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
