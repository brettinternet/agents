"use strict";
const state = {
  snapshot: null,
  conversation: null,
  work: null,
  eventId: 0,
  eventSource: null,
  reloadTimer: null,
  messagesStickToBottom: true,
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
function preview(value, limit = 120) {
  const compact = (value ?? "").replace(/\s+/g, " ").trim();
  return compact.length > limit ? `${compact.slice(0, limit - 1)}…` : compact;
}
function relativeTime(value, now = Date.now()) {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "";
  const seconds = Math.round((timestamp - now) / 1000);
  const absolute = Math.abs(seconds);
  if (absolute < 45) return "now";
  const amount = Math.abs(seconds);
  let compact;
  if (absolute < 3600) compact = `${Math.round(amount / 60)}m`;
  else if (absolute < 86400) compact = `${Math.round(amount / 3600)}h`;
  else if (absolute < 604800) compact = `${Math.round(amount / 86400)}d`;
  else return new Date(timestamp).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  return seconds < 0 ? `${compact} ago` : `in ${compact}`;
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
function isMessageListAtBottom(list) {
  return list.scrollHeight - list.scrollTop - list.clientHeight <= 24;
}
function renderMessages(
  rows = state.snapshot?.messages || [],
  hasMore = rows.length === 50,
  { forceBottom = false, preserveScroll = false } = {},
) {
  state.snapshot.messages = rows;
  const list = $("messages"),
    previousHeight = list.scrollHeight,
    previousTop = list.scrollTop,
    shouldStick = forceBottom || state.messagesStickToBottom;
  list.replaceChildren();
  for (const row of [...rows].reverse()) {
    const message = text("article", "", "message"),
      header = text("div", "", `message-header ${row.urgency === "urgent" ? "urgent" : ""}`),
      time = text("time", relativeTime(row.created_at));
    time.dateTime = row.created_at;
    time.title = row.created_at;
    header.append(text("strong", row.sender_slug), time);
    message.append(header, text("p", row.body));
    list.append(message);
  }
  if (!rows.length) list.append(text("p", "No messages yet.", "empty-state"));
  list.setAttribute("aria-busy", "false");
  $("older").disabled = !hasMore;
  if (preserveScroll) {
    list.scrollTop = previousTop + list.scrollHeight - previousHeight;
  } else if (shouldStick) {
    list.scrollTop = list.scrollHeight;
  } else {
    list.scrollTop = previousTop;
  }
}
const groups = [
  { name: "Prepare", states: ["intake", "refining"] },
  { name: "Ready", states: ["ready"] },
  { name: "In progress", states: ["in_progress"] },
  { name: "Follow-up", states: ["verifying", "awaiting_approval", "blocked"] },
  { name: "Done", states: ["accepted", "delivered", "cancelled"] },
];
function renderQueues() {
  const container = $("queues");
  container.replaceChildren();
  const summary = [
    ["Consultations", state.snapshot.consultations.length, false],
    ["Decisions", state.snapshot.decisions.length, true],
    ["Blockers", state.snapshot.blockers.length, true],
    ["Approvals", state.snapshot.approvals.length, true],
  ];
  for (const [label, count, alert] of summary)
    container.append(text("span", `${label}: ${count}`, `queue ${alert && count ? "alert" : ""}`));
  const incidentCount = state.snapshot.incidents.length,
    incidents = text(
      "button",
      `Incidents: ${incidentCount}`,
      `queue ${incidentCount ? "alert" : ""}`,
    );
  incidents.type = "button";
  incidents.setAttribute("aria-haspopup", "dialog");
  incidents.addEventListener("click", openIncidents);
  container.append(incidents);
  for (const row of state.snapshot.decisions) {
    const label = `Decision ${row.id}: ${row.title}`,
      button = text(
        "button",
        `Decision ${row.id}: ${preview(row.title)}`,
        "queue alert queue-preview",
      );
    button.title = label;
    button.addEventListener("click", () => openDecision(row));
    container.append(button);
  }
  for (const row of state.snapshot.blockers) {
    const waitingForAnswer = row.kind === "waiting_user_answer",
      label = `${row.kind}: ${row.reason}`,
      button = text("button", `${row.kind}: ${preview(row.reason)}`, "queue alert queue-preview");
    button.title = label;
    button.addEventListener("click", () => (waitingForAnswer ? openAnswer(row) : openBlocker(row)));
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
        const action = text("button", "Actions", "card-action");
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
  const changed = state.conversation?.id !== row.id;
  state.conversation = row;
  if (changed) state.messagesStickToBottom = true;
  $("thread-title").textContent = row.address;
  const rows = await api(`/api/v1/conversations/${row.id}/messages?limit=50`);
  renderMessages(rows, rows.length === 50, { forceBottom: changed });
}
function detailButton(label) {
  const button = text("button", label, "primary");
  button.type = "button";
  button.setAttribute("aria-haspopup", "dialog");
  button.addEventListener("click", () => $("detail-dialog").showModal());
  return button;
}
function setFullDetail(...nodes) {
  const heading = nodes.find((node) => node.tagName === "H2");
  if (heading) heading.id = "detail-dialog-title";
  $("detail-full").replaceChildren(...nodes);
}
async function loadTerminal(id) {
  try {
    const data = await api(`/api/v1/terminals/${id}/output`),
      title = `${data.actor_slug} terminal`,
      status = `${data.state} · ${data.status || "unknown"} · ${data.updated_at}`,
      output = data.output_tail || "No captured output";
    $("detail").replaceChildren(
      text("p", "Terminal preview", "eyebrow"),
      text("h2", title),
      text("p", status, "meta"),
      text("pre", output, "evidence preview-evidence"),
      detailButton("Open terminal output"),
    );
    setFullDetail(text("h2", title), text("p", status), text("pre", output, "evidence"));
  } catch (error) {
    notify(error.message, true);
  }
}
async function loadWork(id) {
  try {
    const data = await api(`/api/v1/work/${id}`);
    state.work = data;
    const title = `${data.work.id} ${data.work.title}`,
      status = `${data.work.status} · ${data.work.priority} · v${data.work.version}`,
      summary = text(
        "p",
        `${data.criteria.length} criteria · ${data.dependencies.length} dependencies`,
        "preview-summary",
      );
    $("detail").replaceChildren(
      text("p", "Task preview", "eyebrow"),
      text("h2", title),
      text("p", status, "meta"),
      text("h3", "Problem"),
      text("p", data.work.problem, "preview-copy"),
      text("h3", "Outcome"),
      text("p", data.work.outcome, "preview-copy"),
      summary,
      detailButton("Open full task"),
    );
    const criteria = document.createElement("ul");
    for (const row of data.criteria) criteria.append(text("li", row.body));
    setFullDetail(
      text("h2", title),
      text("p", status),
      text("h3", "Problem"),
      text("p", data.work.problem),
      text("h3", "Outcome"),
      text("p", data.work.outcome),
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
  const options = $("decision-options");
  options.querySelectorAll(".decision-option").forEach((option) => option.remove());
  const syncInputs = () => {
    for (const input of options.querySelectorAll("[data-decision-input]")) {
      const selected = document.getElementById(input.dataset.radio).checked;
      input.hidden = !selected;
      input.disabled = !selected;
      input.required = selected;
    }
  };
  for (const [index, option] of JSON.parse(row.options_json).entries()) {
    const definition = typeof option === "string" ? { label: option } : option,
      label = text("label", "", "decision-option"),
      input = document.createElement("input");
    input.type = "radio";
    input.id = `decision-option-${index}`;
    input.name = "resolution";
    input.value = definition.label;
    input.required = true;
    input.checked = index === 0;
    input.addEventListener("change", syncInputs);
    label.append(input, text("span", definition.label));
    if (definition.input) {
      const customInput = document.createElement("textarea");
      customInput.id = `decision-resolution-${index}`;
      customInput.dataset.decisionInput = "";
      customInput.dataset.radio = input.id;
      customInput.setAttribute("aria-label", definition.input.label);
      customInput.placeholder = definition.input.placeholder || "";
      customInput.rows = 2;
      customInput.addEventListener("input", () => customInput.setCustomValidity(""));
      input.dataset.customInput = customInput.id;
      label.append(text("span", definition.input.label, "decision-input-label"), customInput);
    }
    options.append(label);
  }
  syncInputs();
  $("decision-dialog").showModal();
}
function openIncidents() {
  const rows = [...state.snapshot.incidents].sort((left, right) => right.id - left.id),
    list = $("incident-list");
  $("incident-title").textContent = `Open incidents (${rows.length})`;
  list.replaceChildren();
  if (!rows.length) {
    list.append(text("p", "No open incidents.", "empty-state"));
  }
  for (const row of rows) {
    const incident = text("article", "", "incident-detail"),
      heading = text("h3", `Incident ${row.id} · ${row.kind}`),
      summary = text("p", row.summary, "callout"),
      metadata = document.createElement("dl");
    for (const [label, value] of [
      ["Severity", row.severity],
      ["State", row.state],
      ["Entity", `${row.entity_kind}:${row.entity_id}`],
      ["Created", row.created_at],
      ["Updated", row.updated_at],
    ]) {
      metadata.append(text("dt", label), text("dd", value));
    }
    const details = text("pre", "", "evidence");
    try {
      const parsed = JSON.parse(row.details_json || "{}");
      details.textContent =
        Object.keys(parsed).length > 0 ? JSON.stringify(parsed, null, 2) : "No additional details.";
    } catch {
      details.textContent = row.details_json || "No additional details.";
    }
    incident.append(heading, summary, metadata, text("h4", "Details"), details);
    list.append(incident);
  }
  $("incident-dialog").showModal();
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
  $("answer-question").textContent = row.reason;
  $("answer-dialog").showModal();
}
$("new-intake").addEventListener("click", () => $("intake-dialog").showModal());
document
  .querySelectorAll("[data-close]")
  .forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
$("new-dm").addEventListener("click", () => {
  const form = $("dm-form"),
    select = form.elements.actor,
    submit = form.querySelector('[type="submit"]'),
    actors = [
      ...new Map(
        state.snapshot.roster
          .filter((row) => row.kind === "agent")
          .map((actor) => [actor.slug, actor]),
      ).values(),
    ].sort(
      (a, b) =>
        Number(b.terminal_state === "live") - Number(a.terminal_state === "live") ||
        a.slug.localeCompare(b.slug),
    );
  select.replaceChildren();
  for (const actor of actors) {
    const live = actor.terminal_state === "live",
      option = text("option", live ? actor.slug : `${actor.slug} — unavailable`);
    option.value = actor.slug;
    option.disabled = !live;
    select.append(option);
  }
  const hasLiveActor = actors.some((actor) => actor.terminal_state === "live");
  submit.disabled = !hasLiveActor;
  select.title = hasLiveActor ? "" : "Persistent agents are still starting";
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
    state.messagesStickToBottom = true;
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
    item = form.elements.item_id.value,
    selected = form.querySelector('input[name="resolution"]:checked'),
    customInput = selected.dataset.customInput
      ? document.getElementById(selected.dataset.customInput)
      : null;
  const customValue = customInput?.value.trim();
  if (customInput && !customValue) {
    customInput.setCustomValidity("Enter a value.");
    customInput.reportValidity();
    return;
  }
  if (customInput) customInput.setCustomValidity("");
  const body = {
    item_id: item || null,
    expected_version: item ? Number(form.elements.expected_version.value) : null,
    resolution: customInput ? `${selected.value}\n${customValue}` : selected.value,
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
$("open-thread").addEventListener("click", () => {
  const thread = $("thread");
  thread.classList.remove("thread-preview");
  thread.classList.add("thread-expanded");
  $("thread-dialog").append(thread);
  $("thread-dialog").showModal();
  $("messages").scrollTop = $("messages").scrollHeight;
});
$("thread-dialog").addEventListener("close", () => {
  const thread = $("thread");
  thread.classList.remove("thread-expanded");
  thread.classList.add("thread-preview");
  $("thread-home").append(thread);
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
$("message-body").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  $("compose").requestSubmit();
});

$("compose").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.conversation) return;
  const body = $("message-body").value;
  try {
    await api("/api/v1/messages", {
      method: "POST",
      body: {
        to: state.conversation.address,
        body,
        urgency: $("message-urgency").value,
      },
      intent: true,
    });
    $("message-body").value = "";
    state.messagesStickToBottom = true;
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
  renderMessages([...current, ...rows], rows.length === 50, { preserveScroll: true });
});
$("messages").addEventListener(
  "scroll",
  (event) => {
    state.messagesStickToBottom = isMessageListAtBottom(event.currentTarget);
  },
  { passive: true },
);
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
