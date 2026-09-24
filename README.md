# Agents

A small, persistent [Pi](https://pi.dev) sandbox for long-running internet research, explicitly authorized account actions, public-safe results, and durable Markdown memory.

There is no dashboard, database-backed control plane, message bus, or agent hierarchy. One named Docker Compose service runs standard container tools: Supercronic for reviewed schedules and Herdr for attachable concurrent Pi sessions.

## Included

- Pi with [`brettinternet/pi-extensions`](https://github.com/brettinternet/pi-extensions), including `/loop`
- An attachable, persistent [Herdr](https://herdr.dev) session for concurrent Pi runtimes
- [Supercronic](https://github.com/aptible/supercronic) for optional reviewed schedules
- [Worklease](https://github.com/brettinternet/worklease) for cooperative account, browser-profile, and workspace leases
- Internet tools: Brave Search helper, `curl`, `wget`, Git, GitHub CLI, and `jq`
- Memory search: [`jegrep`](https://github.com/can1357/jegrep), `rg`, `fd`, and `fzf`
- SOPS + age for a committed encrypted environment
- Dedicated locations for checked-in artifacts and ignored working data
- Lefthook pre-commit checks for secrets, oversized files, formatting, and shell scripts
- Agent-owned optional tools in [`.pi/mise.toml`](.pi/mise.toml)

## Start and attach

Requires [mise](https://mise.jdx.dev), Docker, and Git.

```sh
task init
task agent
```

`task init` installs the host tools and Git hooks, prepares ignored state, builds the image, and starts the named `sandbox` service. `task agent` creates or attaches to the `agents` Herdr session inside that service. Every new Herdr pane starts a configured Pi runtime; use workspaces, tabs, and splits to run several concurrently. `.pi/settings.json` points to the container-only `/workspace/.pi/settings.container.json`, keeping its Linux-installed Pi extensions isolated from host Pi.

Detach without stopping Pi using `Ctrl-b q`. Reattach later with `task agent`. Herdr layout state survives in the `herdr-state` Docker volume; Pi sessions, provider logins, raw runs, and Worklease state survive under ignored `.pi-data/`, with optional installed tools under `.tools/`. The live Codex view uses `Ctrl+L`; the container remaps Pi's model selector to `Alt+P`.

The committed [`.herdr/config.toml`](.herdr/config.toml) controls the sandbox UI and pane defaults. Edit it and run `task agent:reload` to apply reloadable options. Print the complete option reference with `herdr --default-config` inside `task shell`.

```sh
task agent:status     # server state and all detected Pi runtimes
task agent:reload     # reload committed Herdr options
task agent:stop       # explicitly ends every interactive Pi runtime
task shell            # raw shell in the running sandbox
task sandbox:logs
task sandbox:stop
```

The entire repository is mounted read/write at `/workspace`, including `memory/`, `results/`, `artifacts/`, the encrypted `secrets.sops.env`, and the ignored `.env.sops-age` identity. The host UID and GID are passed into the image so container agents can update repository files without creating root-owned files. The container has internet access but no published ports, Docker socket, or host home mount.

## Results, artifacts, memory, and working data

Public-safe task summaries go in [`results/`](results/README.md). Other durable, public-safe files intended for Git go in [`artifacts/`](artifacts/README.md). Durable reusable knowledge goes in [`memory/`](memory/README.md) only when it meets the memory policy. All three directories are writable from the sandbox and covered by the repository's secret-leak and 10 MiB staged-file checks.

Clones, downloads, scratch files, private evidence, and other uncommitted working data go in ignored `.pi-data/work/`; raw run output remains in `.pi-data/runs/`. These directories persist across container replacement because `.pi-data/` lives on the host. They are available to every agent in this sandbox, so they are persistence boundaries, not isolation or access-control boundaries.

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

The sandbox and Herdr server start without decrypted values. An agent decrypts only around a command that needs them:

```sh
sops exec-env secrets.sops.env 'command args'
```

`OPENROUTER_API_KEY` enables `jegrep`, and `BRAVE_SEARCH_API_KEY` enables `bin/web-search`. Pi provider login is stored only in ignored `.pi-data/auth.json`; run `/login` once in the container to configure a model provider. Provider credentials are never committed. All values in the encrypted file are exposed to a process launched with `sops exec-env`.

## Long-running loops

Use `/loop` inside an attached Pi session for bounded repeated work. Herdr protects it from terminal disconnects, but not from host or container failure. After a Herdr server restart, supported Pi conversations resume through their saved session references; reconcile external state before resuming any loop that may already have posted, messaged, or otherwise changed a service.

When concurrent work could use the same account or browser profile, coordinate it with Worklease:

```sh
worklease instructions safety
worklease instructions loop
worklease acquire --resource account:forum-main --session "$PI_LOOP_RUN_ID" --ttl 30m
worklease exec --session "$PI_LOOP_RUN_ID" -- command args
worklease release --session "$PI_LOOP_RUN_ID" --reason done
```

Worklease is cooperative coordination, not a secret-access boundary or an exactly-once guarantee.

## Resume, handoff, and recovery

Use `/name` to identify an interactive Pi session and `/resume` to find it later. Pi saves session history under ignored `.pi-data/sessions/` (scheduled runs use `.pi-data/sessions/scheduled/`); its automatic compaction keeps long conversations within context limits without deleting the original session. Herdr restore resumes supported conversations, but **neither a resumed conversation nor a compaction summary proves that an interrupted external action succeeded or failed**.

For unfinished work that another session may need to pick up, write a short checkpoint under ignored `.pi-data/work/` (for example, `.pi-data/work/handoff-topic.md`):

```text
Objective and current status:
Next concrete step:
Files and public results to inspect:
External actions already attempted (verified public URL/ID and outcome):
Ambiguous actions to inspect at the venue before any retry:
Worklease resource to reacquire, if applicable:
```

Do not put credentials, private handles, personal details, raw pages, or transcripts in a handoff; even ignored files should contain only the minimum needed. A new session should read the checkpoint, verify it against the repository and live venue, and acquire its **own** Worklease claim before competing work. Never treat a prior claim or an unverified external write as transferable. Completed public-safe outcomes belong in `results/`; only repeatedly useful, sourced lessons belong in `memory/`.

The bind-mounted `.pi-data/` survives container replacement, but a single copy on the host is **not a backup**. Set up a private, encrypted backup outside this repository for `.pi-data/` and `.env.sops-age` (the identity needed to decrypt `secrets.sops.env`). Back up the `herdr-state` Docker volume too if restoring the pane layout matters. These sources can contain provider credentials, private sessions, and Worklease state: never commit or share the backup, and use a consistent snapshot or stop the sandbox while copying. Periodically test a restore into an isolated environment: confirm the Pi session appears in `/resume`, check that the age identity decrypts the environment with `task secrets:check`, and inspect any external actions before continuing. Do not assume restored Worklease claims are valid; establish fresh ownership before writes.

Search `memory/`, `results/`, and Git history before opening raw sessions. Pi's `/resume` helps locate sessions, but a saved transcript is private evidence, not curated cross-session memory. Add private local transcript search only if these paths repeatedly fail to recover needed details; do not automatically ingest transcripts into tracked files or a hosted index.

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
