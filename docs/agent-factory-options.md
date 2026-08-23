# Agent factory options

Decision report, updated 2026-08-22.

## Recommendation

Keep the Agents control plane and use Herdr 0.8.2 as its owned local execution
service. Herdr provides the PTY, workspace, native provider process, status,
output, input, and event boundary while Agents remains responsible for durable
actors, inboxes, schedules, worktrees, checks, review, approval, and
integration.

Do not replace this project wholesale with a hosted agent factory. A generic
factory can provide a polished UI or managed sandboxes, but it cannot replace
Agents' durable workflow without discarding the policies and state transitions
that make an execution safe to operate.

## What Agents owns

The SQLite control plane remains authoritative for:

- persistent actors, reporting relationships, capacity, and model precedence;
- direct messages, channels, delivery retries, wake acknowledgement, and
  durable human input;
- intake, refinement, dependencies, decisions, blockers, and consultations;
- one worktree and branch per governed execution;
- deterministic checks, independent review, human approval, and integration;
- launch and consultation budgets, actor leases, token fencing, and generation
  recovery; and
- the local operator dashboard and shutdown cleanup.

Herdr resources are external execution state. Agents maps each run to exactly
one labeled workspace and root pane, persists their IDs, and adopts resources
only when the label, project path, provider agent, and generation-owned
artifacts match.

## Local Herdr lifecycle

`agents.toml` configures:

```toml
[execution]
backend = "herdr"
version = "0.8.2"
# Omit this to derive agents-{project.instance_id}.
provider = "opencode"
```

`mise.toml` pins the Herdr executable at 0.8.2. `task init` initializes the
Agents database, checks the configured provider executable, runs the idempotent
provider integration installer, writes the mode-0600 `.agents/herdr.toml`, and
starts the owned Herdr server plus `agentsd`.

The server is local-socket-only. Agents resolves Herdr with `shutil.which`,
records the resolved executable in `.agents/herdr.pid`, and connects to the
project-owned socket under Herdr's session directory. Readiness is a successful
`ping` that reports version 0.8.2, not a listener or HTTP check. `task doctor`
checks the binary version, generated `herdr api schema --json`, socket health,
provider executable, provider integration, manifest-owned artifacts, and
agentsd/Herdr ownership.

`task server:stop` stops only `agentsd` and intentionally retains Herdr's
server, panes, and provider processes. `task server:start` accepts the
intermediate state with Herdr healthy and `agentsd` stopped, then starts only
Agents. `task shutdown` stops Agents first, acquires the daemon lock, fences
and revokes mapped runs, closes and confirms absence of only exact
`agents-{instance_id}-` workspaces, removes manifest-owned provider artifacts,
stops the owned Herdr process, deletes the named empty session, and removes
only the verified PID record.

A full Herdr restart is process loss. Restored shells are treated as terminated
resources, old tokens are revoked, and a new Agents generation is reserved;
provider processes are never resumed under an old credential.

## Provider integrations

The configured providers remain OpenCode, Claude Code, and the test-only mock.
Agents renders provider-native prompt and MCP artifacts under manifest ownership
and injects `AGENTS_AGENT_TOKEN`, `AGENTS_API_URL`, and
`AGENTS_EXECUTION_ID` into the provider environment.

- OpenCode receives its named agent file and MCP/tool grant in the managed
  OpenCode configuration and launches with `--agent` plus an optional model.
- Claude Code receives manifest-owned prompt/MCP files in `.agents/runtime` and
  launches with strict MCP configuration, optional model, and its explicit
  disallowed-tool policy.
- The mock provider installs no user-level configuration and exercises the same
  token, API, and execution identity contract.

Provider files are mode-0600, secret values are hashed in manifests, and
cleanup removes only exact unchanged fragments or files owned by the run.
Tampering fails closed.

## Alternatives

A hosted factory, an SDK with an embedded reasoning loop, or a workflow graph
may be useful for a separate pilot. None is a drop-in replacement for the
current governed lifecycle. Any future backend must implement the same typed
execution boundary, prove identity and cwd on adoption, preserve launch
uncertainty fencing, expose durable status/output transitions, and provide
exact cleanup confirmation before it can be considered.

## Verification plan

Run the same isolated delivery smoke through the mock provider and direct
Herdr backend with `task smoke`. It must complete intake through integration,
exercise durable wake and human-input behavior, and finish with no mapped
workspace and no active mapped `terminal_runs` row.

Also verify:

1. stopping and restarting Agents reuses the same healthy Herdr workspace and
   generation;
2. duplicate labels, mismatched cwd, and restored shells fail closed and cause
   a new generation;
3. an event disconnect is recovered by `ping`, `session.snapshot`, and
   resubscription;
4. unchanged pane revisions do not trigger continuous output reads; and
5. `task doctor` reports the exact Herdr version, compatible generated schema,
   mode-0600 socket/config ownership, provider executable, and integration.
