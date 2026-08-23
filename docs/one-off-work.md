# One-off work

Use the dashboard at `http://127.0.0.1:9890` to give agents one-off work. Run
`task server:status` if the roster does not show live agents. Dashboard messages
are durable and use the normal inbox, wake, and acknowledgement flow over the
owned Herdr workspace.

## Choose the mode: play first, integrate when durable

There are two useful delivery modes. Direct messages (and the scheduled nudges
described in [Scheduled work](scheduled-work.md)) are the default for wandering,
conversation, experiments, drafts, and “see what happens” prompts. Use a
tracked request when the result must be integrated into the repository, committed,
approved, or carried through a durable workflow.

| Desired result                                           | Dashboard action                              | Lifecycle                                                         |
| -------------------------------------------------------- | --------------------------------------------- | ----------------------------------------------------------------- |
| Wander, converse, experiment, draft, or see what happens | **New direct message**                        | Wakes one live persistent actor                                   |
| Produce a tracked and committed repository artifact      | **New request**                               | Refinement, assignment, worktree, checks, review, and integration |
| Discuss or broadcast within an existing conversation     | Select a channel and use its message composer | Wakes that channel's notified actors                              |

Do not turn a playful prompt into tracked work merely to make it feel
official. Generated experiments and drafts may be provisional or speculative;
make the distinction from observed results clear. Factual claims based on
sources still need evidence and source links. If a direct exploration produces
something worth keeping in the repository, convert that result into a tracked
request then.

## Dashboard lanes

The board presents every durable work status exactly once in five human-friendly
lanes. These labels describe the view only; the underlying durable status values
and state machine remain unchanged.

| Dashboard lane  | Durable statuses                            | Meaning                                                          |
| --------------- | ------------------------------------------- | ---------------------------------------------------------------- |
| **Prepare**     | `intake`, `refining`                        | New work is being accepted and shaped.                           |
| **Ready**       | `ready`                                     | Refined work is ready for an actor to pick up.                   |
| **In progress** | `in_progress`                               | An actor is actively working on the item.                        |
| **Follow-up**   | `verifying`, `awaiting_approval`, `blocked` | Work is being checked, waiting for approval, or needs attention. |
| **Done**        | `accepted`, `delivered`, `cancelled`        | Terminal outcomes are retained as repository history.            |

## Direct message

Click **New direct message**, select a live actor, enter the instruction, and
send it. The default actor slugs are:

- **elder** — vague requests, coordination, or deciding what tracked work should
  be created.
- **explorer** — direct research of public sources.
- **writer** — synthesis, publishing-oriented work, and all final public-post copy.

Route every public-post request to writer with its audience, venue, purpose,
constraints, source material, and authorization status. Elder and explorer may
coordinate or supply research, but writer authors the final copy.

State the objective, boundaries, stopping condition, output destination, and
whether external side effects are authorized. For example:

```text
Explore current public examples of useful browser-agent workflows.

Spend at most 60 minutes. Post sourced findings, uncertainties, and three
promising directions to #findings. Do not publish externally or create accounts.
No repository commit is required.
```

A direct message does not automatically create a worktree or require a commit.
It is the right place to ask for a conversation, a rough draft, an odd
experiment, or a useful dead end. If the result becomes durable repository
knowledge, ask the elder to turn it into a spike or create a request directly.

## Tracked request

Click **New request** only when the result must be committed, approved, or
otherwise carried through the tracked workflow. For open-ended research, choose
**Spike** and describe the question without prescribing the answer:

```text
Title:
Explore browser-agent workflows

Problem:
We want open-ended exploration of useful public examples without requiring a
predetermined conclusion.

Outcome:
Commit a dated public-safe memory containing source URLs, findings,
uncertainties, dead ends, and recommended follow-up work.
```

The elder refines the request before it becomes ready. A spike still requires at
least one lightweight, outcome-oriented acceptance criterion, such as recording
sourced findings, uncertainty, and a recommendation. The criterion should not
require a predetermined conclusion.

The normal tracked-work lifecycle then assigns the work in a worktree, requires
a commit, runs configured checks and reviews, and presents the result for
approval and integration.

## Stewardship and safety

Only **elder** may propose self-tuning of the tracked agent configuration:
`agents.toml` and `agents/*.md`. Such tuning is repository work: it must be
based on observed evidence and proceed through a reviewable tracked request and
change. Elder never edits `.agents/` directly. Self-tuning must not relax
repository safety rules or external-authorization gates, and a round that
correctly concludes that no change is needed is a valid no-op.

## Existing channels

Select a channel in the dashboard and use its message composer when the message
belongs to an ongoing conversation:

- `#general` — broad instruction; wakes every notified role.
- `#findings` — research findings and discussion.
- `#publishing` — publishing discussion.
- `#coordination` — elder coordination.

Prefer a direct message when one actor owns the task. Prefer **New request** when
the result must be tracked, committed, approved, or integrated.

## Herdr session lifecycle

Agents owns one local Herdr session named `agents-{project.instance_id}` and a
mode-0600 Unix socket under Herdr's session directory. `task server:start`
starts or reuses that session and starts `agentsd`; readiness is a socket `ping`
plus the Agents health endpoint. `task server:stop` intentionally stops only
`agentsd`, so live Herdr panes and provider processes remain available for a
later `task server:start`.

Use `task shutdown` for a destructive cleanup. It fences active runs and revokes
their tokens, closes only workspaces with the exact project prefix, removes
manifest-owned provider artifacts, stops the owned Herdr process, and deletes the
now-empty named session. A full Herdr restart loses provider processes; Agents
detects that loss and creates a new generation instead of resuming an old token.
