# Agent factory options

Decision report, researched 2026-08-22.

## Recommendation

Do **not** replace this project's CAO-backed control plane with OpenHands wholesale.

OpenHands is the strongest turnkey candidate if the priority is a polished agent UI, reusable coding-agent runtime, automations, and managed or containerized execution. It is not a direct substitute for the project's durable actor roster, inbox and channels, request refinement, explicit acceptance criteria, worktree assignment, checks, review, approval, and integration lifecycle. Replacing the current system would discard differentiated behavior and then require rebuilding it around OpenHands conversations.

The best next step is a bounded **OpenHands backend pilot** behind the existing control plane. Keep the SQLite workflow, dashboard, actor policies, schedules, and Git lifecycle. Test whether an OpenHands Agent Server can replace CAO for one non-persistent worker type while preserving the same assignment, wake, status, submission, cancellation, and resource-cap semantics.

Also evaluate **Pydantic AI + Pydantic AI Harness** in the same bake-off. It is the better incremental fit for this Python repository and its combined web-research/coding goals; OpenHands is the better ready-made product. If neither materially improves completion quality, isolation, or operator effort, retain CAO.

## What this project already has

This repository is not merely a launcher for coding agents. It is a local, durable control plane with:

- persistent named actors and reporting relationships;
- durable direct messages, channels, wake delivery, and acknowledgement;
- tracked work from intake through refinement, dependencies, assignment, verification, review, human approval, and delivery;
- isolated Git branches and worktrees for executions;
- configured verification commands and review gates;
- scheduled messages and tracked work with overlap prevention;
- explicit launch, consultation, capacity, and maximum-agent budgets; and
- a local operator dashboard.

Evidence is in `README.md`, `docs/one-off-work.md`, `docs/scheduled-work.md`, `agents.toml`, and the schema in `src/agents/migrations/001_initial.sql`. Runtime coupling to CAO is relatively contained: `src/agents/cao_client.py` implements the HTTP adapter, and `src/agents/reconciler.py` already defines a `CaoApi` protocol used by the reconciler.

CAO itself is current rather than abandoned: this project pins v2.4.1, released 2026-08-04, and the upstream repository describes support for multiple terminal-native agent CLIs in isolated tmux sessions. The incumbent's main advantage is access to full native CLI agents and their existing authentication/tool ecosystems without making this project own an agent reasoning loop.

## What OpenHands would add

OpenHands is now an ecosystem rather than one monolith:

- **Agent Canvas**: open-source browser client and control center;
- **Software Agent SDK**: Python agent loop, tools, events, workspaces, security, and persistence;
- **Agent Server**: REST/WebSocket remote execution service;
- **Automation Server**: scheduled and event-driven conversation runs;
- **Sandbox Server**: community standalone sandbox control plane; and
- **Cloud/Enterprise**: commercial hosted or licensed self-hosted control planes.

The current Canvas release is v1.15.0 (2026-08-21), so the project is actively changing.

Material advantages over CAO alone:

1. **Purpose-built agent API.** Agent Server exposes conversations, tools, workspaces, streaming events, and an OpenAI-compatible endpoint. Integration is cleaner than scraping terminal output and sending terminal input.
2. **Execution environments.** The SDK supports local, Docker, remote API, and Kubernetes-oriented workspaces. Cloud and Enterprise provide managed isolated sandboxes. This is a stronger execution abstraction than tmux sessions.
3. **Agent capabilities.** Built-in browser use, MCP, custom tools, skills/plugins, model routing, context condensation, persistent memory, pause/resume, stuck detection, metrics, and action-confirmation policies reduce code this project would otherwise have to build.
4. **Operator experience.** Canvas displays conversations, files, terminal output, profiles, backends, and automations. Browser-session recording and OpenTelemetry tracing improve diagnosis.
5. **Automations.** Scheduled and event-driven automations create fresh conversations, retain run history, and connect to configured integrations.
6. **Extensibility.** The SDK is Python and LiteLLM-backed. ACP can delegate to compatible external agents. OpenHands' synchronous TaskToolSet supports resumable specialized sub-agents.

## What OpenHands would not replace

OpenHands' primary unit is a user conversation or automation run. This project's primary units are durable actors, messages, and governed work items. The following remain custom requirements:

- named persistent team members with reporting lines and bounded capacity;
- inbox/channel semantics and durable acknowledgement;
- intake and refinement before an item becomes runnable;
- dependencies, decisions, blockers, consultations, and specialty review gates;
- one worktree and branch per governed execution;
- deterministic checks, independent review, approval, integration, and delivery;
- a global maximum of four agents and launch budgets; and
- the exact public-safety and external-side-effect policies in `AGENTS.md`.

OpenHands sub-agent delegation is not equivalent. Its documented TaskToolSet is synchronous and blocks the parent until a child returns. This repository supports independently reconciled actors, assignments, consultations, and reviews with durable ownership.

## Trade-offs

| Dimension                   | Keep CAO + current control plane                       | Adopt OpenHands as the whole product                                                 | Use OpenHands as another execution backend               |
| --------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| Existing governed lifecycle | Native; already implemented                            | Must be simplified or rebuilt                                                        | Preserved                                                |
| Agent/CLI portability       | Broad terminal-native CLI support                      | Strong model portability; external agents depend on ACP support                      | Both, if CAO remains available                           |
| Web and coding tools        | Depends on selected CLI/MCP tools                      | Built in or documented integrations                                                  | Available to selected worker types                       |
| Isolation                   | tmux/worktree process boundary; not a security sandbox | Docker/remote/Kubernetes options; strongest isolation is managed Cloud/Enterprise    | Can improve selected runs without a full cutover         |
| Persistence                 | Durable project state; terminal session reconciliation | Conversation/event persistence; separate automation state                            | Project state remains authoritative                      |
| Multi-agent behavior        | Durable independent actors                             | Conversations plus synchronous resumable sub-agents                                  | Existing team model can dispatch OpenHands conversations |
| UI and observability        | Purpose-built local dashboard and terminal tails       | Rich Canvas plus OpenTelemetry                                                       | Two UIs unless events are normalized into this dashboard |
| Operations                  | Python service, SQLite, CAO, tmux                      | Multi-repository Canvas/Agent/Automation/Sandbox stack; optionally Docker/Kubernetes | Additional Agent Server and sandbox runtime              |
| Migration risk              | None                                                   | High: state and lifecycle semantic mismatch                                          | Medium: adapter and event normalization                  |
| Reversibility               | Highest                                                | Lowest                                                                               | High                                                     |

### Security and operations

OpenHands is not automatically safer because it has a sandbox abstraction.

- The self-hosting guide warns that a host-process agent can read/write the host filesystem, execute shell commands, and reach the network; anyone able to call that server can exercise the same authority.
- The open-source edition comparison says local and VM backends do not currently provide the same isolated-sandbox guarantees as Cloud/Enterprise. Docker can still provide a deployment boundary, but mounts, network policy, and per-conversation isolation must be verified for the selected topology.
- Agent Server's session API key is optional. Any deployment exposed beyond trusted loopback should enable API authentication and network controls; remote use should also use TLS or a trusted tunnel.
- Canvas stores backend connection information. The self-hosting documentation flags shared-browser-origin/local-storage exposure as a risk when multiple backends are connected.
- OpenTelemetry is available, but a useful trace UI requires operating or buying an OTLP backend.
- The ecosystem is split across independently released repositories. Pin and test Canvas, SDK/Agent Server, Automation Server, and sandbox components as a compatible set. Their licenses must also be checked individually; the overview explicitly warns not to infer one ecosystem-wide license.

The likely cost is engineering and operations rather than only software licensing: adapter work, event/state mapping, container images, storage, network hardening, observability, model tokens, upgrades, and incident response. Cloud and Enterprise trade some of that work for commercial dependency. No current pricing was used in this report, so no dollar comparison is asserted.

## Can this project achieve similar results without OpenHands?

**Yes for the project's actual goals; not cheaply for every OpenHands product feature.**

The existing system already owns the harder domain-specific control-plane behavior. Comparable outcomes can be reached by retaining CAO and adding only demonstrated gaps:

1. add browser/search/MCP capabilities to the relevant agent profiles;
2. add a real container or remote sandbox boundary for untrusted execution;
3. normalize agent events, token/cost data, and traces into the existing run model;
4. add event-triggered schedules only when a real use case exceeds the current cron/interval scheduler; and
5. improve the local dashboard only where operator evidence shows Canvas is better.

Rebuilding Canvas, Agent Server, a general plugin system, and a sandbox fleet would not be advantageous. Using OpenHands selectively is cheaper than recreating those generic capabilities. Conversely, migrating the request lifecycle into OpenHands would be more expensive than preserving it.

## Alternatives worth considering

### 1. Pydantic AI + Pydantic AI Harness — best incremental fit

This is the strongest alternative to test **over OpenHands** when the objective is to improve the agents inside the current factory rather than replace the factory.

Why it fits:

- Python-native and composable in this existing Python service;
- complete Coder and Researcher harnesses;
- workspace-rooted, traversal-safe file tools and an allowlisted shell with credential stripping;
- web search/fetch, Playwright/browser integrations, MCP, named sub-agents, planning, and context management;
- persistent memory and file/SQLite/Mongo step persistence with restore, resume, and fork;
- human tool approval, spend limits, and OpenTelemetry; and
- optional durable execution through Temporal, DBOS, Prefect, Restate, Kitaru, or Airflow integrations.

Trade-off: it is a toolkit, not a factory. It supplies no actor inbox, channels, scheduler, worktree/check/review/integration lifecycle, or equivalent local control-plane UI. Strong durability may add another runtime such as Temporal. Pydantic AI Harness is still v0.x (v0.24.0 released 2026-08-19), so API churn risk is real.

### 2. LangGraph — best if explicit workflow graphs become the bottleneck

LangGraph has strong checkpointed durable execution, resumable interrupts for human input, state inspection/modification, and short/long-term memory. It could model the tracked-request state machine.

It is not the first choice here because this repository already has an explicit SQLite state machine and reconciler. LangGraph would add a second orchestration model while still requiring custom sandboxes, Git worktrees, schedules, inbox/channels, and a dashboard. Consider it only if new workflows become sufficiently branching or replay-heavy that the current reconciler is demonstrably difficult to maintain.

### 3. CrewAI — easy role metaphor, weaker fit

CrewAI's Crews and Flows map intuitively to elder/explorer/publisher roles, and it supports task dependencies and human review. Flows can persist state to SQLite or a custom backend, recover across restarts, resume by flow ID, and fork from saved state. Its strongest control-plane and observability experience is commercial AMP, while the open-source framework still does not supply this project's worktrees, governed checks/reviews/integration, actor inbox/channels, resource caps, or local operator dashboard. Its published Python constraint is below 3.14, while this repository pins Python 3.14.6. Do not adopt without a compelling pilot result and a deliberate runtime downgrade.

### 4. AutoGen — do not start greenfield

AutoGen has useful multi-agent patterns, human-in-the-loop support, a distributed runtime, Docker execution, MCP, and Studio. Its official README now places it in maintenance mode, promises no new features, and directs new users to Microsoft Agent Framework. That makes it a poor new dependency despite its historical influence.

## Decision scorecard

Scores are judgement calls from 1 (poor) to 5 (strong), weighted for this repository rather than the general market. The result is $\sum(\text{weight} \times \text{score}/5)$.

| Criterion                           |  Weight | Current CAO | OpenHands wholesale | OpenHands backend | Pydantic Harness backend | LangGraph layer |
| ----------------------------------- | ------: | ----------: | ------------------: | ----------------: | -----------------------: | --------------: |
| Preserve governed lifecycle         |      20 |           5 |                   2 |                 5 |                        5 |               4 |
| Tools, browser, execution isolation |      20 |           3 |                   5 |                 5 |                        4 |               2 |
| Durable state and human control     |      15 |           4 |                   3 |                 4 |                        4 |               5 |
| Persistent/team orchestration fit   |      15 |           4 |                   3 |                 4 |                        4 |               4 |
| Operational simplicity              |      10 |           5 |                   2 |                 3 |                        3 |               3 |
| UI and observability                |      10 |           3 |                   5 |                 4 |                        3 |               3 |
| Model/runtime portability           |      10 |           5 |                   4 |                 5 |                        5 |               5 |
| **Weighted result / 100**           | **100** |      **82** |              **68** |            **88** |                   **82** |          **73** |

The score does not prove OpenHands wins. It shows why the **backend** shape is more attractive than wholesale adoption: it captures generic runtime gains without throwing away project-specific control-plane value. Pydantic Harness scores lower mainly because it does not bring a comparable ready-made control UI or sandbox service.

## Proposed bake-off

Do not migrate data or alter the dashboard first. Define one backend-neutral execution contract around the operations already required by `CaoApi` and the reconciler:

- create/resume/cancel a run;
- send a durable wake or message;
- stream or poll normalized status and events;
- report workspace identity and enforce one execution boundary;
- submit a commit and terminal result;
- enforce actor capacity and the global four-agent limit; and
- revoke credentials and destroy the runtime cleanly.

Run the same three tasks through CAO, an OpenHands Agent Server worker, and a Pydantic Harness worker:

1. a sourced public-web research report;
2. a small repository change through checks and review; and
3. an interrupted long-running task that must resume without duplicate side effects.

Measure task success, human interventions, elapsed time, model cost, retained diagnostic evidence, isolation failures, resume correctness, adapter complexity, and upgrade surface. Adopt a backend only if it improves at least one primary outcome without regressing lifecycle correctness or external-action safety.

## Sources

Primary sources, accessed 2026-08-22:

- [OpenHands ecosystem introduction and component map](https://docs.openhands.dev/overview/introduction)
- [Agent Canvas architecture](https://docs.openhands.dev/openhands/usage/agent-canvas/architecture)
- [OpenHands open-source vs. Cloud/Enterprise comparison](https://docs.openhands.dev/enterprise/enterprise-vs-oss)
- [OpenHands automations](https://docs.openhands.dev/openhands/usage/automations/overview)
- [OpenHands SDK persistence](https://docs.openhands.dev/sdk/guides/convo-persistence)
- [OpenHands sub-agent TaskToolSet](https://docs.openhands.dev/sdk/guides/task-tool-set)
- [OpenHands v1.15.0 release](https://github.com/OpenHands/OpenHands/releases/tag/v1.15.0)
- [OpenHands self-hosting security notes](https://github.com/OpenHands/OpenHands/blob/main/docs/SELF_HOSTING.md)
- [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)
- [CAO repository](https://github.com/awslabs/cli-agent-orchestrator) and [v2.4.1 release](https://github.com/awslabs/cli-agent-orchestrator/releases/tag/v2.4.1)
- [Pydantic AI Harness](https://github.com/pydantic/pydantic-ai-harness) and [v0.24.0 release](https://github.com/pydantic/pydantic-ai-harness/releases/tag/v0.24.0)
- [Pydantic AI durable execution](https://ai.pydantic.dev/capabilities/durable_execution/overview/)
- [LangGraph repository](https://github.com/langchain-ai/langgraph), [persistence](https://docs.langchain.com/oss/python/langgraph/persistence), and [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [CrewAI repository](https://github.com/crewAIInc/crewAI), [flow persistence](https://docs.crewai.com/en/concepts/flows), and [task/human-input documentation](https://docs.crewai.com/en/concepts/tasks)
- [AutoGen repository and maintenance notice](https://github.com/microsoft/autogen)
