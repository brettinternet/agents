"use strict";
const state = {
  snapshot: null,
  conversation: null,
  work: null,
  eventId: 0,
  eventSource: null,
  reloadTimer: null,
};
let agentWorld = null;
const $ = (id) => document.getElementById(id);
const csrf = () =>
  document.cookie
    .split("; ")
    .find((v) => v.startsWith("agents_csrf="))
    ?.split("=")[1] || "";
function text(tag, value, className = "") {
  const node = document.createElement(tag);
  node.textContent = value ?? "";
  if (className) node.className = className;
  return node;
}
function notify(message, isError = false) {
  const node = $("status");
  node.textContent = message;
  node.className = isError ? "error" : "";
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => {
    node.textContent = "";
  }, 5000);
}
const intentPrefix = "agents:pending:";
function pendingIntents() {
  const rows = [];
  for (let index = 0; index < sessionStorage.length; index++) {
    const key = sessionStorage.key(index);
    if (!key?.startsWith(intentPrefix)) continue;
    try {
      const value = JSON.parse(sessionStorage.getItem(key));
      if (value?.key && value?.method && value?.path) rows.push(value);
    } catch {
      sessionStorage.removeItem(key);
    }
  }
  return rows;
}
function renderPendingIntents() {
  const rows = pendingIntents(),
    panel = $("retry-panel"),
    list = $("pending-intents");
  list.replaceChildren();
  panel.hidden = !rows.length;
  for (const row of rows) {
    const item = document.createElement("li"),
      retry = text("button", `Retry ${row.method} ${row.path}`);
    retry.addEventListener("click", async () => {
      try {
        await api(row.path, {
          method: row.method,
          body: row.body,
          intent: true,
          idempotencyKey: row.key,
        });
        notify("Request completed");
        await hydrate();
      } catch (error) {
        notify(error.message, true);
      }
    });
    item.append(retry);
    list.append(item);
  }
}
function forgetIntent(key) {
  sessionStorage.removeItem(intentPrefix + key);
  renderPendingIntents();
}
async function api(
  path,
  { method = "GET", body = null, intent = false, idempotencyKey = null } = {},
) {
  const headers = { Accept: "application/json" };
  let key = idempotencyKey;
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    headers["X-CSRF-Token"] = csrf();
    headers["Origin"] = location.origin;
  }
  if (intent) {
    key = key || crypto.randomUUID();
    headers["Idempotency-Key"] = key;
    sessionStorage.setItem(intentPrefix + key, JSON.stringify({ key, method, path, body }));
    renderPendingIntents();
  }
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: body === null ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    renderPendingIntents();
    throw error;
  }
  if (intent && response.status < 500) forgetIntent(key);
  if (response.status === 401) {
    location.href = "/login";
    throw new Error("Unauthorized");
  }
  const value = await response.json();
  if (!response.ok || !value.ok) {
    const message =
      value.error?.message || value.detail?.message || `Request failed (${response.status})`;
    throw Object.assign(new Error(message), {
      status: response.status,
      current: value.error?.current,
    });
  }
  return value.data;
}
function renderRoster() {
  const list = $("roster");
  list.replaceChildren();
  for (const actor of state.snapshot.roster) {
    const item = document.createElement("li"),
      button = text("button", actor.slug),
      dot = text("span", "", "presence " + (actor.terminal_state === "live" ? "live" : ""));
    button.prepend(dot);
    button.title = actor.specialty || actor.kind;
    button.addEventListener("click", () => selectConversation(`dm:human:${actor.slug}`));
    item.append(button);
    if (actor.terminal_run_id) {
      const output = text("button", "output");
      output.title = `Read ${actor.slug} output`;
      output.addEventListener("click", () => loadTerminal(actor.terminal_run_id));
      item.append(output);
    }
    list.append(item);
  }
}
function renderConversations() {
  const list = $("conversations");
  list.replaceChildren();
  for (const row of state.snapshot.conversations) {
    const item = document.createElement("li"),
      button = text("button", row.address);
    button.addEventListener("click", () => selectConversation(row));
    item.append(button);
    list.append(item);
  }
}
function renderMessages(rows = state.snapshot?.messages || [], hasMore = rows.length === 50) {
  state.snapshot.messages = rows;
  const list = $("messages");
  list.replaceChildren();
  for (const row of [...rows].reverse()) {
    const message = text("article", "", "message"),
      header = text("div", "", `message-header ${row.urgency === "urgent" ? "urgent" : ""}`);
    header.append(text("strong", row.sender_slug), text("time", row.created_at));
    message.append(header, text("p", row.body));
    list.append(message);
  }
  if (!rows.length) list.append(text("p", "No messages yet.", "empty-state"));
  list.setAttribute("aria-busy", "false");
  $("older").disabled = !hasMore;
}
const groups = [
  { name: "Intake / refining", states: ["intake", "refining"] },
  { name: "Ready", states: ["ready"] },
  { name: "In progress", states: ["in_progress"] },
  { name: "Verification", states: ["verifying", "awaiting_approval"] },
  { name: "Accepted / done", states: ["accepted", "delivered", "blocked", "cancelled"] },
];
function renderQueues() {
  const container = $("queues");
  container.replaceChildren();
  const summary = [
    ["Consultations", state.snapshot.consultations.length, false],
    ["Decisions", state.snapshot.decisions.length, true],
    ["Blockers", state.snapshot.blockers.length, true],
    ["Approvals", state.snapshot.approvals.length, true],
    ["Incidents", state.snapshot.incidents.length, true],
  ];
  for (const [label, count, alert] of summary)
    container.append(text("span", `${label}: ${count}`, `queue ${alert && count ? "alert" : ""}`));
  for (const row of state.snapshot.decisions) {
    const button = text("button", `Decision ${row.id}: ${row.title}`, "queue alert");
    button.addEventListener("click", () => openDecision(row));
    container.append(button);
  }
  for (const row of state.snapshot.blockers) {
    const button = text("button", `${row.kind}: ${row.reason}`, "queue alert");
    button.addEventListener("click", () =>
      row.kind === "waiting_user_answer" ? openAnswer(row) : openBlocker(row),
    );
    container.append(button);
  }
}
function renderBoard() {
  const board = $("board");
  board.replaceChildren();
  for (const group of groups) {
    const column = text("section", "", "column");
    column.append(text("h3", group.name));
    for (const item of state.snapshot.board.filter((work) => group.states.includes(work.status))) {
      const card = text("article", "", `card ${item.status}`),
        title = text("button", `${item.id} ${item.title}`, "title");
      title.addEventListener("click", () => loadWork(item.id));
      card.append(
        title,
        text(
          "div",
          `${item.priority} · ${item.specialty || "unassigned"} · v${item.version}`,
          "meta",
        ),
        text("div", item.status.replaceAll("_", " ")),
      );
      if (
        [
          "intake",
          "refining",
          "ready",
          "in_progress",
          "verifying",
          "awaiting_approval",
          "accepted",
          "blocked",
        ].includes(item.status)
      ) {
        const action = text("button", "Actions");
        action.addEventListener("click", () => openAction(item));
        card.append(action);
      }
      column.append(card);
    }
    board.append(column);
  }
}
async function hydrate() {
  try {
    renderPendingIntents();
    const snapshot = await api("/api/v1/snapshot");
    state.snapshot = snapshot;
    state.eventId = Math.max(state.eventId, snapshot.event_high_water);
    if (agentWorld) agentWorld.reconcile(snapshot);
    renderRoster();
    renderConversations();
    renderBoard();
    renderQueues();
    if (!state.conversation) {
      state.conversation = snapshot.default_conversation;
      renderMessages();
    }
    $("connection").textContent = "Live";
    if (agentWorld) agentWorld.setConnection("live");
    $("status").textContent = "";
    $("status").className = "";
    connectEvents();
  } catch (error) {
    notify(error.message, true);
    $("connection").textContent = "Disconnected";
  }
}
function scheduleAuthoritativeHydration({ immediate = false } = {}) {
  clearTimeout(state.reloadTimer);
  state.reloadTimer = setTimeout(
    async () => {
      await hydrate();
      if (state.work) await loadWork(state.work.work.id);
      if (state.conversation) await selectConversation(state.conversation, false);
    },
    immediate ? 0 : 150,
  );
}
function connectEvents() {
  if (state.eventSource) return;
  const source = new EventSource(`/api/v1/events?after=${state.eventId}`);
  state.eventSource = source;
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
      if (agentWorld) agentWorld.onEvent(row);
    } catch {
      // Animation is best-effort. Hydration below remains mandatory.
    }

    scheduleAuthoritativeHydration({ immediate: gap });
  });
  source.onopen = () => {
    $("connection").textContent = "Live";
    if (agentWorld) agentWorld.setConnection("live");
  };
  source.onerror = () => {
    $("connection").textContent = "Reconnecting…";
    if (agentWorld) agentWorld.setConnection("reconnecting");
    source.close();
    state.eventSource = null;
    setTimeout(connectEvents, 1500);
  };
}
function initWorld() {
  const canvas = $("world-canvas");
  if (!canvas || agentWorld || typeof window.createAgentWorld !== "function") return;
  agentWorld = window.createAgentWorld(canvas, {
    onAgent: (actor) => selectConversation(`dm:human:${actor.slug}`),
    onWork: (workId) => loadWork(workId),
    onBlocker: (row) => (row.kind === "waiting_user_answer" ? openAnswer(row) : openBlocker(row)),
  });
}
async function selectConversation(value, announce = true) {
  const row =
    typeof value === "string"
      ? state.snapshot.conversations.find((item) => item.address === value)
      : value;
  if (!row) {
    if (announce) notify("That role has no active conversation", true);
    return;
  }
  state.conversation = row;
  $("thread-title").textContent = row.address;
  const rows = await api(`/api/v1/conversations/${row.id}/messages?limit=50`);
  renderMessages(rows);
}
async function loadTerminal(id) {
  try {
    const data = await api(`/api/v1/terminals/${id}/output`),
      section = $("detail");
    section.replaceChildren(
      text("h2", `${data.actor_slug} terminal`),
      text("p", `${data.state} · ${data.status || "unknown"} · ${data.updated_at}`),
      text("pre", data.output_tail || "No captured output", "evidence"),
    );
  } catch (error) {
    notify(error.message, true);
  }
}
async function loadWork(id) {
  try {
    const data = await api(`/api/v1/work/${id}`);
    state.work = data;
    const section = $("detail");
    section.replaceChildren(
      text("h2", `${data.work.id} ${data.work.title}`),
      text("p", `${data.work.status} · ${data.work.priority} · v${data.work.version}`),
      text("h3", "Problem"),
      text("p", data.work.problem),
      text("h3", "Outcome"),
      text("p", data.work.outcome),
    );
    const criteria = document.createElement("ul");
    for (const row of data.criteria) criteria.append(text("li", row.body));
    section.append(
      text("h3", "Acceptance criteria"),
      criteria,
      text("h3", "Dependencies"),
      text("p", data.dependencies.join(", ") || "None"),
      text("h3", "Consultations"),
      text(
        "p",
        data.consultations
          .map((row) => `${row.specialty}: ${row.state}${row.response ? ` — ${row.response}` : ""}`)
          .join(" · ") || "None",
      ),
      text("h3", "Decisions"),
      text("p", data.decisions.map((row) => `${row.title}: ${row.state}`).join(" · ") || "None"),
      text("h3", "Branches and submissions"),
      text(
        "pre",
        data.executions.map((row) => `${row.branch}\\nbase ${row.base_sha}`).join("\\n") || "None",
        "evidence",
      ),
      text(
        "pre",
        data.submissions
          .map((row) => `${row.state} ${row.commit_sha}\\n${row.summary}`)
          .join("\\n") || "None",
        "evidence",
      ),
      text("h3", "Checks"),
      text(
        "pre",
        data.checks
          .map(
            (row) =>
              `${row.scope}: ${row.state} (${row.duration_ms ?? 0}ms)\\n${row.stdout_tail || row.stderr_tail || ""}`,
          )
          .join("\\n") || "None",
        "evidence",
      ),
      text("h3", "Reviews"),
      text(
        "p",
        data.reviews.map((row) => `${row.gate}: ${row.verdict} — ${row.body || ""}`).join(" · ") ||
          "None",
      ),
      text("h3", "Blockers"),
      text(
        "p",
        data.blockers.map((row) => `${row.kind}: ${row.state} — ${row.reason}`).join(" · ") ||
          "None",
      ),
    );
  } catch (error) {
    notify(error.message, true);
  }
}
function openAction(item) {
  const dialog = $("action-dialog"),
    form = $("action-form"),
    select = form.elements.action;
  form.elements.item.value = item.id;
  select.replaceChildren();
  let actions;
  if (item.status === "intake")
    actions = [
      ["start-refinement", "Start refinement"],
      ["cancel", "Cancel"],
    ];
  else if (item.status === "refining")
    actions = [
      ["ready", "Mark ready"],
      ["cancel", "Cancel"],
    ];
  else if (item.status === "awaiting_approval")
    actions = [
      ["accept", "Accept"],
      ["reject", "Reject"],
    ];
  else if (item.status === "ready")
    actions = [
      ["reopen", "Reopen"],
      ["cancel", "Cancel"],
    ];
  else if (item.status === "blocked")
    actions = [
      ["resume", "Resolve and resume"],
      ["escalate", "Escalate"],
    ];
  else
    actions = [
      ["reopen", "Reopen"],
      ["cancel", "Cancel"],
    ];
  for (const [value, label] of actions) {
    const option = text("option", label);
    option.value = value;
    select.append(option);
  }
  $("action-title").textContent = `Action for ${item.id}`;
  dialog.showModal();
}
function openDecision(row) {
  const form = $("decision-form"),
    work = state.snapshot.board.find((item) => item.id === row.work_id);
  form.elements.decision_id.value = row.id;
  form.elements.item_id.value = row.work_id || "";
  form.elements.expected_version.value = work?.version || "";
  $("decision-title").textContent = row.title;
  $("decision-question").textContent = row.question;
  $("decision-recommendation").textContent = `Recommendation: ${row.recommendation}`;
  const select = form.elements.resolution;
  select.replaceChildren();
  for (const value of JSON.parse(row.options_json)) {
    const option = text("option", value);
    option.value = value;
    select.append(option);
  }
  $("decision-dialog").showModal();
}
function openBlocker(row) {
  const form = $("blocker-form"),
    work = state.snapshot.board.find((item) => item.id === row.work_id);
  form.elements.blocker_id.value = row.id;
  form.elements.item_id.value = row.work_id || "";
  form.elements.expected_version.value = work?.version || "";
  $("blocker-title").textContent = `Resolve blocker ${row.id}`;
  $("blocker-reason").textContent = row.reason;
  $("blocker-dialog").showModal();
}
function openAnswer(row) {
  const form = $("answer-form");
  form.elements.terminal_run_id.value = row.terminal_run_id;
  $("answer-title").textContent = `Answer ${row.actor_slug}`;
  $("answer-dialog").showModal();
}
$("new-intake").addEventListener("click", () => $("intake-dialog").showModal());
document
  .querySelectorAll("[data-close]")
  .forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
$("new-dm").addEventListener("click", () => {
  const form = $("dm-form"),
    select = form.elements.actor;
  select.replaceChildren();
  for (const actor of state.snapshot.roster.filter((row) => row.terminal_state === "live")) {
    const option = text("option", actor.slug);
    option.value = actor.slug;
    select.append(option);
  }
  $("dm-dialog").showModal();
});
$("dm-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    actor = form.elements.actor.value,
    body = form.elements.body.value;
  try {
    await api("/api/v1/messages", {
      method: "POST",
      body: { to: `@${actor}`, body },
      intent: true,
    });
    form.reset();
    $("dm-dialog").close();
    notify("Direct message sent");
    await hydrate();
    await selectConversation(`dm:human:${actor}`, false);
  } catch (error) {
    notify(error.message, true);
  }
});
$("intake-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    body = Object.fromEntries(new FormData(form));
  try {
    await api("/api/v1/intake", { method: "POST", body, intent: true });
    form.reset();
    $("intake-dialog").close();
    notify("Request submitted");
    await hydrate();
  } catch (error) {
    notify(error.message, true);
  }
});
$("action-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    item = state.snapshot.board.find((row) => row.id === form.elements.item.value),
    action = form.elements.action.value,
    reason = form.elements.reason.value;
  if (!item) return;
  let path = `/api/v1/work/${item.id}/${action}`,
    body = { expected_version: item.version, reason };
  if (action === "accept" || action === "reject") {
    path = `/api/v1/work/${item.id}/${action}`;
    body = { expected_version: item.version, feedback: reason };
  } else if (action === "resume" || action === "escalate") {
    const blocker = state.snapshot.blockers.find((row) => row.work_id === item.id);
    if (!blocker) {
      notify("No open blocker", true);
      return;
    }
    path = `/api/v1/blockers/${blocker.id}/resolve`;
    body = { item_id: item.id, expected_version: item.version, resolution: reason, action };
  }
  try {
    await api(path, { method: "POST", body, intent: true });
    $("action-dialog").close();
    notify(`${action} recorded`);
    await hydrate();
  } catch (error) {
    notify(error.message, true);
  }
});
$("decision-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    id = form.elements.decision_id.value,
    item = form.elements.item_id.value;
  const body = {
    item_id: item || null,
    expected_version: item ? Number(form.elements.expected_version.value) : null,
    resolution: form.elements.resolution.value,
  };
  try {
    await api(`/api/v1/decisions/${id}/resolve`, { method: "POST", body, intent: true });
    $("decision-dialog").close();
    await hydrate();
  } catch (error) {
    notify(error.message, true);
  }
});
$("blocker-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    id = form.elements.blocker_id.value,
    item = form.elements.item_id.value;
  const body = {
    item_id: item || null,
    expected_version: item ? Number(form.elements.expected_version.value) : null,
    resolution: form.elements.resolution.value,
    action: form.elements.action.value,
  };
  try {
    await api(`/api/v1/blockers/${id}/resolve`, { method: "POST", body, intent: true });
    $("blocker-dialog").close();
    await hydrate();
  } catch (error) {
    notify(error.message, true);
  }
});
$("answer-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    id = form.elements.terminal_run_id.value;
  try {
    await api(`/api/v1/terminals/${id}/answer`, {
      method: "POST",
      body: { body: form.elements.body.value },
      intent: true,
    });
    form.reset();
    $("answer-dialog").close();
    notify("Answer queued once");
    await hydrate();
  } catch (error) {
    notify(error.message, true);
  }
});
$("compose").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.conversation) return;
  const body = $("message-body").value;
  try {
    await api("/api/v1/messages", {
      method: "POST",
      body: { to: state.conversation.address, body },
      intent: true,
    });
    $("message-body").value = "";
    await selectConversation(state.conversation, false);
  } catch (error) {
    notify(error.message, true);
  }
});
$("older").addEventListener("click", async () => {
  if (!state.conversation) return;
  const current = state.snapshot.messages || [],
    oldest = current.at(-1);
  if (!oldest) return;
  const rows = await api(
    `/api/v1/conversations/${state.conversation.id}/messages?before_id=${oldest.id}&limit=50`,
  );
  renderMessages([...current, ...rows], rows.length === 50);
});
$("search").addEventListener("input", (event) => {
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(async () => {
    const query = event.target.value.trim();
    if (!query) {
      if (state.conversation) selectConversation(state.conversation, false);
      return;
    }
    const rows = await api(`/api/v1/search?query=${encodeURIComponent(query)}&limit=50`);
    $("thread-title").textContent = `Search: ${query}`;
    renderMessages(rows, false);
  }, 250);
});
$("logout").addEventListener("click", async () => {
  await api("/auth/logout", { method: "POST", body: {}, intent: true });
  location.href = "/login";
});
initWorld();
hydrate();
