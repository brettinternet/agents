# Agent progress visualization plan

## Goal

Add a small, entertaining, generative visualization above the existing work board. It should make agent and workflow activity legible through motion without becoming another control plane.

The visualization is a read-only projection:

- `/api/v1/snapshot` is the authoritative durable state.
- `/api/v1/events` supplies short-lived visual effects and prompts snapshot reconciliation.
- Existing roster, queue, board, detail, and conversation controls remain authoritative and accessible.
- The visualization never schedules agents, transitions work, writes workflow state, or invents progress percentages.

Working name: **The Internet Garden**. Agents are deterministic procedural rovers or fireflies moving between workflow regions. Work items are signals, specimens, or constellations. Messages travel as packets; blockers raise beacons; delivered work briefly blooms before settling in an archive.

## Why this shape

Munder Difflin demonstrates the useful interaction principle: motion itself can communicate agent state. Its implementation uses Electron, React, Pixi.js, maps, pathfinding, character controllers, and licensed tile assets. This repository has a much smaller vanilla HTML/CSS/JavaScript dashboard, so copying that architecture would add more machinery than the feature needs.

Use native Canvas 2D, deterministic procedural drawing, and the control plane's existing event stream. Do not add a frontend framework, bundler, graphics dependency, asset pack, or second progress protocol.

## Existing architecture

Relevant code as of 2026-08-22:

- `src/agents/web.py:278-336`: authenticated `/api/v1/snapshot` returns the roster, work board, operational queues, conversations, recent messages, and `event_high_water`.
- `src/agents/web.py:850-877`: authenticated `/api/v1/events` streams complete `events` rows as Server-Sent Events. It supports both `after` and `Last-Event-ID` cursors.
- `src/agents/static/app.js:239-285`: `hydrate()` fetches the snapshot; `connectEvents()` advances the cursor and debounces another hydration. The listener currently ignores `event.data`.
- `src/agents/db.py:196-233`: durable mutations append an event atomically with the domain mutation.
- `src/agents/migrations/001_initial.sql:26-43`: work lifecycle states and timestamps.
- `src/agents/migrations/001_initial.sql:103-128`: terminal lifecycle state, provider status, purpose, and bounded output fields.
- `src/agents/migrations/001_initial.sql:312-320`: event schema: ID, actor, kind, entity kind/ID, metadata JSON, and creation time.
- `src/agents/reconciler.py:1009-1015`: provider polling updates terminal status/output directly, currently without an event.
- `src/agents/static/index.html:30-50`: main header, queues, and board provide the narrow insertion point.
- `src/agents/static/styles.css`: plain CSS; there is no graphics or frontend runtime dependency.

Useful existing event kinds include:

- `message.posted`
- `work.progress`
- `consultation.requested`
- `consultation.completed`
- `work.submitted`
- `review.submitted`
- `blocker.created`
- `blocker.resolved`
- `decision.proposed`
- `decision.resolved`

`work.progress` retains its summary in event `metadata_json`, but the summary is not a durable progress percentage. Use the event as an activity pulse only.

## Data flow and invariants

```text
SQLite snapshot ──> scene.reconcile(snapshot) ──> durable visual state
SQLite events   ──> scene.onEvent(event)       ──> bounded transient effect
SSE event       ──> existing debounced hydrate ─> authoritative repair
```

Invariants:

1. `reconcile(snapshot)` is idempotent and can reconstruct the entire durable scene after reload or reconnect.
2. Event payloads never become authoritative workflow state.
3. Missing, duplicate, unknown, or malformed events may lose an animation, but must never lose or stop board refreshes.
4. No animation survives reload. Do not replay historical completion effects after initial hydration.
5. Do not infer tool use or progress from terminal output text.
6. Do not render message bodies, terminal output, paths, prompts, or event summaries into the scene.
7. The event stream remains the only progress notification protocol.

## Scope

### Included

- Deterministic procedural avatar for every agent.
- Fixed responsive workflow regions.
- Agent movement derived from active terminal state and purpose.
- Work objects derived from board state.
- Bounded event particles for messages, progress, review, submission, blockers, decisions, and completion.
- Explicit live, reconnecting, paused, and reduced-motion states.
- Mouse click-through to existing agent, work, and blocker surfaces.
- Accessible textual summary; existing controls remain the keyboard interface.
- One low-volume `terminal.status_changed` event on real normalized provider-status transitions.

### Excluded

- Pixi.js, React, or a frontend build pipeline.
- Munder Difflin art, maps, characters, or other licensed assets.
- AI-generated images.
- Physics, pathfinding, drag-and-drop, or editable floor layout.
- Progress percentages.
- Tool-specific bubbles without real tool events.
- Terminal-output parsing for inferred activity.
- Persisted animation state, themes, sound, or configuration.
- Workflow mutations initiated by the canvas.

## Implementation sequence

### 1. Extend the snapshot roster contract

**Files:** `src/agents/web.py`, `tests/test_web.py`

Extend the active terminal fields selected for each roster row:

```sql
tr.purpose_kind AS terminal_purpose_kind,
tr.purpose_id AS terminal_purpose_id
```

The roster currently aliases terminal ID, status, and state but does not expose purpose. These two fields truthfully associate a live agent with its current work, consultation, review, or persistent role.

Expected shape:

```json
{
  "slug": "explorer",
  "terminal_run_id": 17,
  "terminal_state": "live",
  "terminal_status": "processing",
  "terminal_purpose_kind": "work",
  "terminal_purpose_id": "W-18"
}
```

Both purpose fields may be `null` when no active terminal exists. Do not add terminal output or filesystem fields to the snapshot.

Tests:

- Active terminal purpose appears in `/api/v1/snapshot`.
- Actor without an active terminal gets null terminal-purpose fields.
- Existing roster and snapshot behavior remains unchanged.

### 2. Emit terminal status transitions

**Files:** `src/agents/reconciler.py`, `tests/test_reconciler.py`

The visualization otherwise cannot learn that provider status changed unless an unrelated durable mutation occurs. During the reconciler's existing terminal poll, compare the prior normalized status with the newly normalized status.

When and only when they differ, append:

```text
kind: terminal.status_changed
actor_slug: terminal run actor
entity_kind: terminal
entity_id: terminal:<run-id>
metadata_json:
  previous_status
  status
  state
  purpose_kind
  purpose_id
```

Requirements:

- Insert the event in the **same existing database transaction** as the `terminal_runs` status update. It must not commit separately.
- Use the repository's canonical JSON representation for metadata.
- A transition from null/empty to a real normalized status counts as a transition.
- An unchanged poll emits no event.
- Do not emit events for output-tail changes, output-digest changes, or every reconciliation tick.
- Do not route this system observation through the request-id mutation helper; it is not a client mutation. Keep the insertion local unless an existing event helper already fits without changing semantics.

Tests must demonstrate:

1. One event on a real normalized status transition.
2. No event on an unchanged subsequent poll.
3. A later different status creates exactly one additional event.
4. Actor, terminal entity, purpose, and prior/new statuses are correct.
5. The event and terminal status update share transaction outcome; no state/event split is possible.

### 3. Add the scene surface

**Files:** `src/agents/static/index.html`, `src/agents/static/styles.css`

Insert a section between the main header and operational queues:

```html
<section id="world" class="world" aria-labelledby="world-title">
  <header class="world-header">
    <div>
      <p class="eyebrow">Live control plane</p>
      <h3 id="world-title">The Internet Garden</h3>
    </div>
    <button id="world-motion" class="quiet compact" type="button">Pause motion</button>
  </header>
  <canvas id="world-canvas" aria-hidden="true"></canvas>
  <p id="world-summary" class="sr-only" aria-live="polite"></p>
</section>
```

Load `/static/world.js` before the existing `/static/app.js` script. Keep the current non-module script arrangement.

Styling requirements:

- Reuse the current dashboard palette and panel treatment.
- Desktop scene height: approximately 240-300 CSS pixels.
- Narrow viewport: compact or collapsible scene; do not force the dashboard wider.
- Add an `sr-only` utility only if none exists.
- Honor `prefers-reduced-motion`.
- Canvas is decorative and `aria-hidden`; `world-summary` reports stable counts and connection state, never particle activity.

### 4. Implement the Canvas 2D renderer

**New file:** `src/agents/static/world.js`

Expose one global factory because the current frontend has no module/bundler boundary:

```js
window.createAgentWorld = function createAgentWorld(canvas, callbacks) {
  return {
    reconcile(snapshot),
    onEvent(event),
    setConnection(status),
    setMotionEnabled(enabled),
    destroy(),
  };
};
```

Callbacks:

```js
{
  onAgent(actor),
  onWork(workId),
  onBlocker(blocker),
}
```

Keep the implementation in three plain concepts:

```js
entities = {
  agents: new Map(),
  work: new Map(),
};

effects = [];
viewport = { width, height, devicePixelRatio };
```

Do not introduce an entity-component system, scene graph, physics abstraction, or general plugin interface.

#### Deterministic appearance

Implement a small stable string hash and seeded PRNG, such as FNV-1a plus Mulberry32. Seed agent appearance with `actor.slug` and work appearance/archive placement with `work.id`.

Derive a limited set of:

- Body silhouettes.
- Existing-palette accent colors.
- One accessory detail.
- Idle offset and movement cadence.
- Work-object shape.

The same actor and work item must look the same after reload. Seeded randomness must not affect workflow interpretation or destination.

#### Workflow regions

Use responsive fixed regions:

| Durable state                    | Region                  |
| -------------------------------- | ----------------------- |
| `intake`, `refining`, `ready`    | Signal dock             |
| `in_progress`                    | Research grove/workshop |
| `verifying`, `awaiting_approval` | Verification tower      |
| `blocked`                        | Distress clearing       |
| `accepted`, `delivered`          | Archive garden          |
| `cancelled`                      | Fade at edge, then omit |

For `terminal_purpose_kind == "work"`, use `terminal_purpose_id` to associate the agent with a board item. For consultation or review purposes, show the agent in the corresponding activity region without claiming a work association that the snapshot does not expose. Persistent terminals remain at the agent's home station.

#### Status mapping

| Control-plane fact          | Visual state                    |
| --------------------------- | ------------------------------- |
| No active terminal          | Resting at home                 |
| Terminal `processing`       | Moving/working pulse            |
| `waiting_user_answer`       | Stationary blue question beacon |
| Terminal error/failed state | Red incident signal             |
| Work `in_progress`          | Active work object              |
| Work `verifying`            | Checkpoint orbit/ring           |
| Work `blocked`              | Distress beacon                 |
| Work `accepted`/`delivered` | Short bloom, then archive       |
| Work `cancelled`            | Fade without success effect     |

Normalize known provider completion aliases consistently with the reconciler. Unknown statuses render as neutral, not as working or successful.

#### Rendering and motion

Canvas layers, back to front:

1. Flat background and region landmarks.
2. Work objects.
3. Message trails.
4. Agents.
5. Status indicators.
6. Temporary effects.
7. Compact labels.

Use timestamp-based interpolation with integer or stable coordinates; no pathfinding. Bound all transient effects by both count and lifetime. Drop the oldest effect at capacity.

Use `ResizeObserver` and a backing buffer scaled for device pixel ratio. Cap the effective ratio if necessary to prevent unnecessary large buffers. Retain CSS-pixel logical coordinates.

Run `requestAnimationFrame` only while motion is enabled, the document is visible, and an entity/effect needs animation. A single static redraw is sufficient when idle. Stop the loop on `destroy()`.

### 5. Integrate with hydration and SSE

**File:** `src/agents/static/app.js`

Create the world once during application initialization. Reuse existing functions for callbacks:

- Agent: select `dm:human:<slug>` using the existing conversation path.
- Work: call `loadWork(id)`.
- Blocker: call the existing blocker or terminal-answer dialog function.

After every successful snapshot fetch:

```js
state.snapshot = snapshot;
agentWorld.reconcile(snapshot);
```

Keep the existing roster, board, queue, detail, and message renders.

#### Defensive event handling

The SSE endpoint sends the complete event row, but `metadata_json` is itself a JSON string. Parse both layers defensively. A malformed visual payload must still advance the event cursor and schedule authoritative hydration so one bad row cannot freeze the live board.

Required control flow:

```js
source.addEventListener("agents", (event) => {
  const id = Number(event.lastEventId);
  if (!Number.isSafeInteger(id) || id <= state.eventId) return;

  const gap = id > state.eventId + 1;
  state.eventId = id;

  try {
    const row = JSON.parse(event.data);
    try {
      row.metadata = JSON.parse(row.metadata_json);
    } catch {
      row.metadata = null;
    }
    agentWorld.onEvent(row);
  } catch {
    // Animation is best-effort. Hydration below remains mandatory.
  }

  scheduleAuthoritativeHydration({ immediate: gap });
});
```

Adapt the naming to the existing debounce rather than creating competing reload timers. The important ordering is:

1. Validate and advance the cursor.
2. Attempt the optional visual effect.
3. Always schedule hydration.

Unknown event kinds must be ignored by `onEvent()` without throwing.

#### Event-to-effect mapping

| Event kind                | Bounded effect                                                         |
| ------------------------- | ---------------------------------------------------------------------- |
| `message.posted`          | Packet from sender toward DM recipient, or toward a shared channel hub |
| `work.progress`           | Pulse around work/responsible agent; do not display summary text       |
| `consultation.requested`  | Signal toward consultation region                                      |
| `consultation.completed`  | Return signal                                                          |
| `work.submitted`          | Work moves toward verification                                         |
| `review.submitted`        | Verification ring/stamp                                                |
| `blocker.created`         | Distress flare                                                         |
| `blocker.resolved`        | Flare dissolves                                                        |
| `decision.proposed`       | Amber human-attention signal                                           |
| `decision.resolved`       | Signal clears                                                          |
| `terminal.status_changed` | Agent transition pulse/movement                                        |
| Unknown/malformed         | No effect; hydration still occurs                                      |

For a DM, parse the durable conversation address to locate the other actor. For a channel or unknown address, animate toward a shared channel hub. Never inspect message body text.

#### Cursor, gaps, and reconnects

- Initialize `state.eventId` from `snapshot.event_high_water`.
- Connect with `/api/v1/events?after=<eventId>` as today.
- Ignore duplicate/older IDs.
- On an unexpected ID gap, do not fabricate missing animations; request immediate hydration.
- On normal events, retain the existing short debounce.
- On reconnect, resume from the last accepted event ID.
- A hydration may advance to a snapshot high-water mark before every transient event is animated. Losing those animations is acceptable because the snapshot is authoritative.
- Never fetch or replay historical events solely to restore decoration after a page reload.

Forward connection state to the world:

```js
agentWorld.setConnection("live");
agentWorld.setConnection("reconnecting");
```

Reconnecting behavior:

- Pause destination changes and transient effects.
- Desaturate the scene.
- Report reconnecting in the textual summary.
- Do not continue motion that implies fresh activity.

### 6. Interaction and accessibility

Maintain hitboxes from the latest rendered frame for optional pointer interaction:

- Agent click selects its existing direct-message conversation.
- Work click opens existing work detail.
- Blocker beacon click opens the existing blocker or human-answer dialog.

Do not implement drag-and-drop. Canvas interaction is supplemental: every action must remain available in the existing keyboard-accessible roster, board, and queues.

Motion controls:

- Manual pause/resume button updates its label and pressed state.
- `prefers-reduced-motion: reduce` starts with decorative motion disabled.
- When paused or reduced, transitions snap or use a single static redraw; semantic status indicators remain visible.
- Stop animation while the document is hidden and redraw/reconcile on visibility return.

Textual summary examples:

```text
Live. Three agents active, two waiting, one work item blocked, and two awaiting verification.
Reconnecting. Last known state: two agents active and one blocker needing attention.
```

Do not announce every event or frame through `aria-live`.

## Verification

### Automated

Run focused tests while implementing, then run the repository's standard checks once at the end.

Required backend coverage:

- Snapshot includes terminal purpose fields.
- Terminal transition emits one event in the same transaction.
- Unchanged terminal poll emits no event.
- A second real transition emits one additional event.
- Existing mutation/event atomicity remains green.
- Existing snapshot/auth behavior remains green.

Do not add a JavaScript test framework solely for this feature. The new frontend behavior is best verified against the actual browser surface.

### Browser smoke test

Run the dashboard and verify with Chrome DevTools:

1. Initial snapshot renders all agents and current board state.
2. Agent and work appearance remains stable across reloads.
3. A work transition moves the correct work object.
4. A `terminal.status_changed` transition updates the correct agent.
5. An unchanged terminal poll creates no repeated visible transition.
6. `message.posted` creates one bounded packet effect.
7. Blocker creation/resolution creates and clears the beacon.
8. Malformed `event.data` and malformed `metadata_json` do not stop later hydration or event handling.
9. An event-ID gap triggers immediate authoritative hydration without fabricated effects.
10. SSE disconnect pauses/desaturates the scene; reconnect restores snapshot state without duplicate effects.
11. Pause and reduced-motion modes retain legible status while stopping decoration.
12. Canvas resizing stays sharp and does not disturb board layout.
13. Existing board actions, messages, dialogs, search, and terminal output still work.
14. Effect counts remain bounded during a burst of events.

Capture a before/after screenshot and a short recording because motion is the primary changed behavior.

## Expected file changes

```text
src/agents/web.py                    snapshot purpose fields
src/agents/reconciler.py             terminal.status_changed emission
src/agents/static/index.html         scene markup and script include
src/agents/static/styles.css         scene/responsive/accessibility styles
src/agents/static/app.js             world reconciliation and defensive SSE effects
src/agents/static/world.js           new dependency-free Canvas 2D renderer
tests/test_web.py                    snapshot contract coverage
tests/test_reconciler.py             terminal event transition coverage
docs/agent-progress-visualization.md this implementation plan
```

Avoid unrelated dashboard restyling or workflow changes.

## Definition of done

The dashboard reconstructs a deterministic scene from the authoritative snapshot, uses the existing SSE stream only for bounded transient effects, truthfully reflects terminal lifecycle transitions, remains correct after duplicate/malformed/missed events and reconnects, adds no graphics dependency, and leaves every existing workflow operation available through the current controls.
