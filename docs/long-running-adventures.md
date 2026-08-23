# Long-running adventures design

## Goal

Add durable, multi-hour or multi-day adventures in which agents explore, exchange evidence, challenge one another, pause, recover, and produce optional tracked artifacts.

An adventure is durable control-plane state. Agent terminals are disposable execution resources. A terminal may run one bounded turn; it must not be the only place where the adventure remembers what happened or what should happen next.

The design should also make the platform feel more alive without turning every message into a terminal wake or encouraging agent-to-agent message loops.

## Design principles

1. Reuse the existing work graph, conversations, deliveries, decisions, consultations, schedules, and event stream.
2. Add only the missing multi-episode state and checkpoint/resume semantics.
3. Do not force read-only research, coordination, or banter through the commit/worktree/review lifecycle.
4. Persist chatter independently from whether it deserves an interruption.
5. Resume only when the control plane can prove that doing so cannot duplicate an outcome or external side effect.
6. Keep the human able to understand the current objective, disagreements, last confirmed checkpoint, and next action without reading terminal output.

## Existing architecture

Relevant code as of 2026-08-22:

- `src/agents/migrations/001_initial.sql:26-57` defines hierarchical work through `parent_id` and ordering through `dependencies`.
- `src/agents/migrations/001_initial.sql:63-89` attaches consultations and decisions to work.
- `src/agents/migrations/001_initial.sql:91-128` defines executions and terminal runs.
- `src/agents/migrations/001_initial.sql:244-288` persists conversations, membership, messages, deliveries, acknowledgements, and wake attempts.
- `src/agents/messages.py:47-114` creates and synchronizes per-work conversations.
- `src/agents/messages.py:121-221` persists messages, inbox delivery, acknowledgement, and conversation history.
- `src/agents/messages.py:290-357` computes channel recipients and routes work messages to the relevant assignment, review, or consultation terminal.
- `src/agents/delivery.py:339-417` dispatches ready tracked work into a worktree, execution, terminal, and assignment.
- `src/agents/delivery.py:566-617` safely interrupts and requeues verification checks after restart.
- `src/agents/web.py:582-601` accepts progress summaries but retains only the work version and update timestamp as durable work state.
- `src/agents/reconciler.py:1240-1280` turns pending deliveries into terminal wake notifications with retry provenance.
- `src/agents/reconciler.py:1334-1444` fences failed active terminals, releases their leases, and blocks associated work.
- `src/agents/schedules.py:68-118` creates due message or tracked-work occurrences.
- `docs/scheduled-work.md:50-60` documents skip-only overlap and downtime behavior.

These primitives already solve most single-episode workflow concerns. The missing layer is a persistent objective that survives several bounded turns and tracked work items, plus structured state from which a later turn can safely continue.

## Adventure model

An adventure owns a long-lived objective and a sequence of bounded turns:

```text
Adventure
  objective, boundaries, stop condition
  state and next scheduled wake
  shared conversation
  stable participant handles
  turns
    read-only exploration
    coordination or synthesis
    optional tracked work item
  checkpoints
    confirmed findings and evidence
    dead ends
    unresolved questions
    decisions
    recommended next action
```

An adventure is not a larger terminal run. It remains valid while every terminal is stopped.

### Adventures

Add an `adventures` table with at least:

```text
id
slug
root_work_id nullable
conversation_id
objective
boundaries
stop_condition
state: draft | active | paused | completed | cancelled
next_wake_at nullable
latest_checkpoint_id nullable
version
created_at
updated_at
```

`root_work_id` is optional. Use it when the adventure has an umbrella tracked outcome. A research-only adventure does not need a placeholder work item merely to exist.

The adventure conversation is the shared room for scope announcements, findings, questions, challenges, handoffs, decisions, and ambient social activity. Existing per-work conversations remain the authoritative threads for tracked artifacts.

### Participants

Add stable per-adventure participant identities:

```text
adventure_id
handle
actor_slug
role
state: active | resting | removed
joined_at
```

A handle identifies one participant within an adventure, such as `scout-1`, `skeptic`, or `chronicler`. `actor_slug` selects the existing actor profile and capacity policy.

This distinction matters when several simultaneous participants use the `explorer` actor. Current messages record and address the actor slug; without a participant handle, parallel explorers appear to be the same speaker and cannot be targeted independently within a shared adventure.

Persist both participant handle and actor slug on adventure messages and turns. Actor slug remains the authorization and runtime identity; participant handle is the adventure-local social identity.

### Turns

Add `adventure_turns` for bounded, non-work execution:

```text
id
adventure_id
participant_handle
kind: explore | coordinate | synthesize | observe
objective
state: queued | running | checkpointed | interrupted | reconciling | blocked | cancelled
resume_from_checkpoint_id nullable
terminal_run_id nullable
work_id nullable
side_effect_policy: read_only | authorized
created_at
started_at nullable
ended_at nullable
```

Most turns should be read-only or coordination turns and should not create a worktree, commit, review, or approval requirement.

Set `work_id` only when the turn must produce a tracked repository artifact. In that case, reuse the existing work lifecycle rather than duplicating it:

- `parent_id` groups artifact work under an umbrella work item when one exists.
- `dependencies` order artifact-producing episodes.
- The work conversation contains artifact-specific discussion.
- Existing execution, submission, checks, review, approval, and integration remain authoritative.

An adventure turn may wait for a tracked work item and record its delivered result in a later checkpoint, but it must not imitate the work state machine.

### Checkpoints

Add append-only `adventure_checkpoints`:

```text
id
adventure_id
turn_id
participant_handle
previous_checkpoint_id nullable
summary
findings_json
dead_ends_json
open_questions_json
next_action
created_at
```

Checkpoint bodies must remain public-safe. Evidence should use public URLs or repository artifact identifiers rather than copied private or transient material.

`adventures.latest_checkpoint_id` is an optimistic cursor. Committing a new checkpoint must compare the expected current checkpoint and adventure version in the same transaction. A stale turn must fail rather than overwrite or supersede a newer checkpoint silently.

A compact context packet for a new turn contains:

1. Adventure objective, boundaries, and stop condition.
2. Participant role and current turn objective.
3. Latest confirmed checkpoint.
4. Open decisions and unresolved questions.
5. Relevant recent conversation messages.
6. Explicit external-side-effect authorization.

Do not reconstruct adventure state from terminal output. Terminal output remains operational evidence, not the adventure's durable memory.

## Turn lifecycle

Use short, bounded turns rather than holding a terminal for the duration of an adventure:

```text
queued
  -> running
  -> checkpointed

running
  -> interrupted
  -> reconciling
  -> queued       only when safe to resume

running
  -> blocked      human input, provider prompt, or uncertain side effect
```

A typical exploration turn lasts 20–45 minutes:

1. Reserve capacity and launch a terminal.
2. Build the context packet from durable state.
3. Ask the participant to announce its scope as an ambient message.
4. Perform the bounded objective.
5. Post useful findings or questions during the turn.
6. Commit one final checkpoint or explicit terminal outcome.
7. Fence and release the terminal.
8. Let the coordinator schedule, pivot, pause, or complete the adventure.

No final checkpoint is itself evidence that the turn completed. Completion requires the checkpoint mutation or fenced terminal outcome to be committed durably.

## Safe recovery

Automatic continuation is allowed only when the control plane can prove all of the following:

- The old process and terminal are fenced and cannot continue.
- No terminal outcome was committed.
- No newer checkpoint was committed.
- The turn was read-only, or every authorized side effect has an unambiguous idempotent result.
- The failure was an infrastructure interruption, not a provider prompt or human blocker.

Otherwise move the turn to reconciliation:

```text
interrupted
  |- provably safe ----------------> queued from confirmed checkpoint
  |- checkpoint or outcome unclear -> reconciling
  |- provider or human prompt ------> blocked
  `- external action unclear -------> blocked for human review
```

A resumed turn carries the checkpoint generation it read. It may append the next checkpoint only if that generation is still current.

### External actions

Read-only public browsing can normally be repeated. Posting, uploading, account creation, purchases, accepting terms, messaging, and repository integration cannot be repeated merely because the previous terminal disappeared.

For an authorized side effect, record an intent and idempotency key before execution, then record the observed result before advancing the checkpoint:

```text
intent recorded -> action attempted -> result recorded -> checkpoint committed
```

A failure between `action attempted` and `result recorded` is uncertain. Stop for reconciliation rather than retrying automatically.

## Chatter and attention

More durable conversation should not imply more terminal interruptions. Separate message persistence from attention:

| Mode      | Persistence and delivery                                 | Intended use                                                         |
| --------- | -------------------------------------------------------- | -------------------------------------------------------------------- |
| `ambient` | Store in the conversation; no inbox delivery or wake     | Scope announcements, status, observations, banter                    |
| `inbox`   | Deliver when the participant is next naturally active    | Findings, handoffs, non-urgent review requests                       |
| `wake`    | Create a pending delivery and wake the named participant | Direct questions, assigned challenges, contradictions needing action |
| `urgent`  | Wake and retain existing escalation semantics            | Incidents and time-sensitive human or coordinator attention          |

Keep free-form message bodies. Add a small optional intent field such as `scope`, `finding`, `question`, `challenge`, `handoff`, `decision`, or `social` only when it improves routing or presentation. Do not build a second structured workflow inside messages.

### Routing rules

- Direct messages and explicit named questions may wake their recipient.
- Adventure-wide posts default to ambient or inbox.
- A mention must target a participant handle and affect delivery; it must not merely validate that an actor exists.
- Replies inherit the conversation but not automatically the parent's wake level.
- One agent's ambient response must not wake another agent into an unbounded reply loop.
- Coordinators may request a digest rather than waking every participant for every post.
- Wake budgets and launch budgets remain control-plane constraints, not suggestions embedded only in prompts.

The existing `reply_to_id` supports durable reply relationships, and `read_through_message_id` provides a schema location for read cursors. The dashboard should expose threads and unread state before adding more notification volume.

### Useful interaction rituals

Do not tell agents merely to “chat more.” Give conversation a role in the adventure lifecycle.

#### Scope announcement

At turn start, post an ambient statement of the current search boundary and deliberate exclusions. This lets peers adjust without interrupting them.

#### Finding

A finding should state the claim, evidence, confidence, relevance, and requested action if any. No requested action means ambient or inbox delivery.

#### Challenge

A participant may assign another participant to falsify a high-value finding. This is a named inbox or wake message and may create a new turn.

#### Handoff

The final message states what was checked, what failed, what remains uncertain, and the recommended next action. The checkpoint remains authoritative; the message makes it legible socially.

#### Round decision

The coordinator chooses to continue, pivot, pause, create tracked work, or complete the adventure. Reuse durable decisions when the choice has material trade-offs or needs human resolution.

### Preserve independent thought

Do not expose every preliminary conclusion to every participant immediately. Alternate independent work and exchange:

```text
independent exploration
  -> scope and evidence exchange
  -> peer challenge
  -> coordinator decision
  -> next round
```

Useful adventure-local roles include scout, skeptic, cartographer, chronicler, and captain. These are participant roles, not new permanent actor types.

## Scheduling

Extend schedules so an occurrence can advance an existing adventure:

```toml
[[schedules]]
slug = "browser-agent-expedition"
cron = "0 */6 * * *"
timezone = "America/Los_Angeles"
overlap = "skip"

[schedules.adventure]
slug = "browser-agent-workflows"
action = "advance"
```

An advance occurrence should:

1. Confirm that the adventure is active and not already advancing.
2. Read the latest checkpoint and unresolved questions.
3. Ask the coordinator to choose the next bounded turns.
4. Queue only those turns for which capacity exists.
5. Record the occurrence against the same adventure.

Retain skip overlap initially. Do not replay every missed round after downtime; one reconciliation round is more useful than a burst of stale agent launches.

## Dashboard

Add an adventure surface without replacing existing work and conversation controls:

- Objective, boundaries, stop condition, and state.
- Current round and next scheduled wake.
- Participant handles, roles, current scopes, and availability.
- Timeline combining messages, checkpoints, decisions, and tracked artifacts.
- Separate “new ambient activity” from “needs attention.”
- Threaded replies and unread cursors.
- Explicit disagreement and unresolved-question views.
- Launch, wake, and interruption counts.

The dashboard is a projection of durable adventure, work, message, and event state. It must not infer progress from terminal output or invent completion percentages.

## Example adventure

**Objective:** Find unusual but practical public browser-agent workflows.

**Duration:** Up to three days, four rounds per day, 30 minutes per turn.

**Participants:**

- `captain` using the elder profile: chooses direction and stopping point.
- `scout-1` using the explorer profile: searches for novel projects.
- `scout-2` using the explorer profile: searches for deployed workflows.
- `skeptic` using the explorer profile: challenges the strongest prior finding.
- `chronicler` using the writer profile: produces internal round digests.

**Rules:**

1. Every participant announces its scope.
2. Every research turn records a finding or an explicit dead end.
3. Every round includes one peer challenge.
4. Ambient messages never wake terminals.
5. Direct questions may wake only their named participant.
6. Stop after two dry rounds or five sufficiently supported directions.
7. Create tracked work only for a repository artifact worth preserving.

## Implementation sequence

### 1. Persist adventures and checkpoints

**Likely files:** migrations, store/domain services, web and MCP APIs, focused database and API tests.

Add adventures, participants, turns, checkpoints, optimistic checkpoint commits, and context-packet reads. Reuse current mutation/event conventions.

Acceptance:

- An adventure exists without a live terminal or work item.
- Two bounded turns append ordered checkpoints.
- A stale checkpoint commit cannot replace a newer checkpoint.
- Public-safe context can be reconstructed after service restart.

### 2. Execute bounded read-only turns

**Likely files:** delivery, reconciler, terminal purpose schema, profiles, focused reconciler tests.

Add an adventure-turn terminal purpose and capacity-aware dispatch. Fence the terminal after a committed checkpoint. Implement safe interruption classification and reconciliation states.

Acceptance:

- A fenced read-only turn with no committed outcome can resume from its confirmed checkpoint.
- A turn with an uncertain outcome or side effect does not retry automatically.
- Provider and human prompts remain blocked for explicit resolution.

### 3. Add attention-aware messaging

**Likely files:** message schema/domain, reconciler wake selection, MCP/web APIs, dashboard, focused message tests.

Add participant attribution, ambient/inbox/wake modes, mention-aware routing, unread cursors, and threaded display. Preserve existing DM, work, channel, escalation, acknowledgement, and wake behavior where semantics are unchanged.

Acceptance:

- Ten ambient adventure messages appear in history without producing ten terminal wakes.
- A named participant question reaches only the intended active participant.
- A reply does not escalate its attention level implicitly.
- Duplicate or stale deliveries cannot create an agent reply loop.

### 4. Continue adventures through schedules

**Likely files:** configuration, schedules, reconciler, dashboard, focused schedule tests.

Add an adventure schedule target that advances the same durable adventure. Keep skip overlap and at-most-one reconciliation after downtime.

Acceptance:

- Several scheduled rounds retain one adventure, conversation, and checkpoint chain.
- Restart during a waiting period does not lose the next wake.
- Downtime does not launch a burst of missed rounds.

### 5. Add the social adventure dashboard

Render participants, scopes, checkpoints, decisions, disagreements, artifacts, and attention separately from raw operational status.

Acceptance:

- A human can identify the current objective, latest confirmed state, active disagreement, and next action without reading terminal output.
- Existing work, blocker, roster, and conversation operations remain authoritative and accessible.

## Non-goals

- Keeping terminals alive for days.
- Converting every adventure turn into tracked repository work.
- Replacing the existing work state machine or per-work conversations.
- Replacing consultations or decisions with free-form messages.
- Waking all members for all adventure activity.
- Autonomous retries of uncertain external actions.
- Reconstructing durable state from terminal logs.
- Scoring success by raw message volume.

## Success criteria

The design is successful when:

1. A multi-day adventure survives service and terminal restarts with a coherent checkpoint chain.
2. Recoverable read-only interruption requires no human reconstruction.
3. Ambiguous outcomes and external actions fail closed without duplication.
4. Read-only turns remain lightweight; tracked artifacts continue through the established work lifecycle.
5. Parallel participants have distinct scopes and identities.
6. Useful peer challenges and handoffs increase without a proportional increase in terminal wakes.
7. The human can see what agents learned, where they disagree, and what happens next.
