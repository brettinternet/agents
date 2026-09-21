# Agents

A small, persistent [Pi](https://pi.dev) sandbox for long-running internet research, explicitly authorized account actions, public-safe results, and durable Markdown memory.

There is no dashboard, database-backed control plane, message bus, or agent hierarchy. One named Docker Compose service runs standard container tools: Supercronic for reviewed schedules and tmux for an attachable Pi session.

## Included

- Pi with [`brettinternet/pi-extensions`](https://github.com/brettinternet/pi-extensions), including `/loop`
- An attachable, persistent tmux session
- [Supercronic](https://github.com/aptible/supercronic) for optional reviewed schedules
- [Worklease](https://github.com/brettinternet/worklease) for cooperative account, browser-profile, and workspace leases
- Internet tools: Brave Search helper, `curl`, `wget`, Git, GitHub CLI, and `jq`
- Memory search: [`jegrep`](https://github.com/can1357/jegrep), `rg`, `fd`, and `fzf`
- SOPS + age for a committed encrypted environment
- Lefthook pre-commit checks for secrets, formatting, and shell scripts
- Agent-owned optional tools in [`.pi/mise.toml`](.pi/mise.toml)

## Start and attach

Requires [mise](https://mise.jdx.dev), Docker, and Git.

```sh
task init
task agent
```

`task init` installs the host tools and Git hooks, prepares ignored state, builds the image, and starts the named `sandbox` service. `task agent` creates or attaches to the `pi` tmux session inside that service. `.pi/settings.json` points to the container-only `/workspace/.pi/settings.container.json`, keeping its Linux-installed Pi extensions isolated from host Pi.

Detach without stopping Pi using `Ctrl-b d`. Reattach later with `task agent`. Pi sessions, provider logins, raw runs, Worklease state, and optional installed tools survive under ignored `.pi-data/` and `.tools/`. The live Codex view uses `Ctrl+L`; the container remaps Pi's model selector to `Alt+P`.

```sh
task agent:status
task agent:stop       # explicitly ends the Pi tmux session
task shell            # shell in the running sandbox
task sandbox:logs
task sandbox:stop
```

The repository is mounted read/write at `/workspace`. The container has internet access but no published ports, Docker socket, or host home mount.

## Results and memory

Public-safe task deliverables go in [`results/`](results/README.md). Raw output and private evidence go in ignored `.pi-data/runs/`. Durable reusable knowledge goes in [`memory/`](memory/README.md) only when it meets the memory policy.

With `BRAVE_SEARCH_API_KEY` in `secrets.sops.env`, search the public web from the host or sandbox:

```sh
task web:search -- 'public research query'
sops exec-env secrets.sops.env 'bin/web-search "public research query"'
```

Memory search remains local:

```sh
task memory:list
task memory:grep -- 'search terms'
task memory:semantic -- 'where did we decide how credentials work?'
task memory:pick
```

## Secrets and accounts

`secrets.sops.env` is committed ciphertext. `.env.sops-age` is its ignored local age identity and must be backed up separately. The sandbox can read that identity, so SOPS protects secrets in Git and at rest, **not from the agent**.

Add or change values:

```sh
task secrets:edit
task secrets:check
```

Run a host command with the decrypted environment:

```sh
task secrets:run -- gh auth status
task secrets:run -- curl https://api.example.com/me
```

The sandbox and tmux server start without decrypted values. An agent decrypts only around a command that needs them:

```sh
sops exec-env secrets.sops.env 'command args'
```

`OPENROUTER_API_KEY` enables `jegrep`, and `BRAVE_SEARCH_API_KEY` enables `bin/web-search`. Pi provider login is stored only in ignored `.pi-data/auth.json`; run `/login` once in the container to configure a model provider. Provider credentials are never committed. All values in the encrypted file are exposed to a process launched with `sops exec-env`.

## Long-running loops

Use `/loop` inside the attached Pi session for bounded repeated work. tmux protects it from terminal disconnects, but not from host or container failure. Reconcile external state before resuming any loop that may already have posted, messaged, or otherwise changed a service.

When concurrent work could use the same account or browser profile, coordinate it with Worklease:

```sh
worklease instructions safety
worklease instructions loop
worklease acquire --resource account:forum-main --session "$PI_LOOP_RUN_ID" --ttl 30m
worklease exec --session "$PI_LOOP_RUN_ID" -- command args
worklease release --session "$PI_LOOP_RUN_ID" --reason done
```

Worklease is cooperative coordination, not a secret-access boundary or an exactly-once guarantee.

## Scheduled jobs

Supercronic watches [`jobs/crontab`](jobs/crontab) in UTC. No job is enabled by default. See [`jobs/README.md`](jobs/README.md) before adding one.

Run a reviewed prompt manually before scheduling it:

```sh
task job:run -- jobs/prompts/example-research.md
task scheduler:reload
task sandbox:logs
```

The host and sandbox must be running when a schedule becomes due. There is no missed-run catch-up. Use host `launchd` or another host scheduler to start a stopped sandbox when that behavior is required.

## Agent-owned tools

Boot-critical tools are pinned in `Dockerfile`. Agents may add pinned optional tools to `.pi/mise.toml`, then install them into ignored `.tools/mise/`:

```sh
mise install --yes   # inside the sandbox
task tools:install   # from the host
```

Mutable mise installs are never run automatically at sandbox startup.

## Validation

```sh
task container:build
task check
```
