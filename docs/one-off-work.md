# One-off work

Use the dashboard at `http://127.0.0.1:9890` to give agents one-off work. Run
`task server:status` if the roster does not show live agents. Do not send raw
input to CAO terminals; dashboard messages are durable and use the normal inbox,
wake, and acknowledgement flow.

## Choose the delivery path

| Desired result                                             | Dashboard action                              | Lifecycle                                                         |
| ---------------------------------------------------------- | --------------------------------------------- | ----------------------------------------------------------------- |
| Investigate, answer, or report without a repository commit | **New direct message**                        | Wakes one live persistent actor                                   |
| Produce a tracked and committed repository artifact        | **New request**                               | Refinement, assignment, worktree, checks, review, and integration |
| Discuss or broadcast within an existing conversation       | Select a channel and use its message composer | Wakes that channel's notified actors                              |

## Direct message

Click **New direct message**, select a live actor, enter the instruction, and
send it.

Choose the actor according to the work:

- **elder** — vague requests, coordination, or deciding what tracked work should
  be created.
- **explorer** — direct research of public sources.
- **yapper** — synthesis or publishing-oriented work.

State the objective, boundaries, stopping condition, output destination, and
whether external side effects are authorized. For example:

```text
Explore current public examples of useful browser-agent workflows.

Spend at most 60 minutes. Post sourced findings, uncertainties, and three
promising directions to #findings. Do not publish externally or create accounts.
No repository commit is required.
```

A direct message does not automatically create a worktree or require a commit.
If the result becomes durable repository knowledge, ask the elder to turn it
into a spike or create a request directly.

## Tracked request

Click **New request** when the result must be committed. For open-ended research,
choose **Spike** and describe the question without prescribing the answer:

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

## Existing channels

Select a channel in the dashboard and use its message composer when the message
belongs to an ongoing conversation:

- `#all-hands` — broad instruction; wakes every notified role.
- `#findings` — research findings and discussion.
- `#publishing` — publishing discussion.
- `#coordination` — elder coordination.

Prefer a direct message when one actor owns the task. Prefer **New request** when
the result must be tracked or committed.
