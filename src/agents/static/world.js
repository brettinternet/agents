"use strict";

// Deterministic, read-only projection of the control plane. The snapshot is
// authoritative; events drive only bounded transient effects. No workflow
// state is read from or written by this canvas.
(function () {
  function fnv1a(str) {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 0x01000193);
    }
    return h >>> 0;
  }

  function mulberry32(seed) {
    let a = seed >>> 0;
    return function () {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  const COMPLETION_STATUSES = new Set(["completed", "complete", "done", "exited", "stopped"]);
  function normalizeStatus(status) {
    const value = (status == null ? "" : String(status)).trim().toLowerCase();
    return COMPLETION_STATUSES.has(value) ? "completed" : value;
  }

  const PALETTE = [
    "#265d97",
    "#168554",
    "#9b7d25",
    "#a33131",
    "#6d28d9",
    "#0e7490",
    "#b45309",
    "#4d7c0f",
  ];
  const SILHOUETTES = ["rover", "firefly", "spark", "beetle", "moth"];
  const WORK_SHAPES = ["node", "specimen", "constellation"];

  const REGIONS = [
    {
      key: "dock",
      label: "Intake",
      x: 0.015,
      y: 0.1,
      w: 0.2,
      h: 0.52,
      fill: "rgba(38,93,151,0.05)",
      stroke: "rgba(38,93,151,0.24)",
    },
    {
      key: "workshop",
      label: "In progress",
      x: 0.235,
      y: 0.1,
      w: 0.2,
      h: 0.52,
      fill: "rgba(22,133,84,0.05)",
      stroke: "rgba(22,133,84,0.24)",
    },
    {
      key: "verification",
      label: "Verification",
      x: 0.455,
      y: 0.1,
      w: 0.2,
      h: 0.52,
      fill: "rgba(109,40,217,0.05)",
      stroke: "rgba(109,40,217,0.26)",
    },
    {
      key: "archive",
      label: "Completed",
      x: 0.675,
      y: 0.1,
      w: 0.31,
      h: 0.52,
      fill: "rgba(155,125,37,0.05)",
      stroke: "rgba(155,125,37,0.26)",
    },
    {
      key: "distress",
      label: "Blocked",
      x: 0.015,
      y: 0.66,
      w: 0.97,
      h: 0.16,
      fill: "rgba(163,49,49,0.05)",
      stroke: "rgba(163,49,49,0.24)",
    },
    {
      key: "home",
      label: "Idle",
      x: 0.015,
      y: 0.86,
      w: 0.97,
      h: 0.1,
      fill: "rgba(100,116,139,0.05)",
      stroke: "rgba(100,116,139,0.22)",
    },
  ];

  function regionKeyForStatus(status) {
    switch (status) {
      case "intake":
      case "refining":
      case "ready":
        return "dock";
      case "in_progress":
        return "workshop";
      case "verifying":
      case "awaiting_approval":
        return "verification";
      case "blocked":
        return "distress";
      case "accepted":
      case "delivered":
        return "archive";
      default:
        return "dock";
    }
  }

  function clamp(value, min, max) {
    return value < min ? min : value > max ? max : value;
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function ease(t) {
    return t * t * (3 - 2 * t);
  }

  function rr(ctx, x, y, w, h, r) {
    const radius = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + radius, y);
    ctx.arcTo(x + w, y, x + w, y + h, radius);
    ctx.arcTo(x + w, y + h, x, y + h, radius);
    ctx.arcTo(x, y + h, x, y, radius);
    ctx.arcTo(x, y, x + w, y, radius);
    ctx.closePath();
  }

  window.createAgentWorld = function createAgentWorld(canvas, callbacks) {
    const ctx = canvas.getContext("2d");
    const section = canvas.closest(".world");
    const summaryEl = document.getElementById("world-summary");
    const motionButton = document.getElementById("world-motion");
    const cb = callbacks || {};

    const entities = { agents: new Map(), work: new Map() };
    let blockers = [];
    let effects = [];
    let hitboxes = [];
    let viewport = { width: 0, height: 0, dpr: Math.min(window.devicePixelRatio || 1, 2) };
    let lastSnapshot = null;
    let connection = "live";
    let motion = true;
    let destroyed = false;
    let rafId = 0;
    let lastT = 0;
    let resizeObserver = null;

    const reducedMotion = window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false;

    // --- deterministic appearance ---
    function agentAppearance(slug) {
      const rnd = mulberry32(fnv1a("agent:" + slug));
      return {
        silhouette: SILHOUETTES[Math.floor(rnd() * SILHOUETTES.length)],
        color: PALETTE[Math.floor(rnd() * PALETTE.length)],
        accessory: Math.floor(rnd() * 3),
        idleOffset: rnd() * Math.PI * 2,
        cadence: 0.8 + rnd() * 1.4,
        radius: 5 + rnd() * 3,
        speed: 0.028 + rnd() * 0.02,
        anchor: { fx: 0.08 + rnd() * 0.84, fy: 0.2 + rnd() * 0.6 },
      };
    }

    function workAppearance(id) {
      const rnd = mulberry32(fnv1a("work:" + id));
      return {
        shape: WORK_SHAPES[Math.floor(rnd() * WORK_SHAPES.length)],
        color: PALETTE[Math.floor(rnd() * PALETTE.length)],
        radius: 4 + rnd() * 3,
        phase: rnd() * Math.PI * 2,
        anchor: { fx: 0.12 + rnd() * 0.76, fy: 0.15 + rnd() * 0.7 },
      };
    }

    // --- geometry ---
    function regionRects() {
      const w = viewport.width || 1;
      const h = viewport.height || 1;
      const out = {};
      for (const region of REGIONS) {
        out[region.key] = {
          x: region.x * w,
          y: region.y * h,
          w: region.w * w,
          h: region.h * h,
          cx: (region.x + region.w / 2) * w,
          cy: (region.y + region.h / 2) * h,
        };
      }
      return out;
    }

    function workPosition(work, rects) {
      const rect = rects[regionKeyForStatus(work.status)];
      return {
        x: rect.x + work.appearance.anchor.fx * rect.w,
        y: rect.y + work.appearance.anchor.fy * rect.h,
      };
    }

    function channelHub() {
      return { x: viewport.width * 0.5, y: viewport.height * 0.035 };
    }

    function humanHub() {
      const rect = regionRects().home;
      return { x: rect.x + rect.w * 0.02, y: rect.y + rect.h * 0.5 };
    }

    function isAtHome(agent) {
      const actor = agent.actor;
      return (
        !actor ||
        !actor.terminal_run_id ||
        !["work", "review", "consultation"].includes(actor.terminal_purpose_kind)
      );
    }

    function homePosition(agent) {
      const rect = regionRects().home;
      const residents = [...entities.agents.values()]
        .filter(isAtHome)
        .sort((a, b) => a.slug.localeCompare(b.slug));
      const index = Math.max(
        0,
        residents.findIndex((resident) => resident.slug === agent.slug),
      );
      return {
        x: rect.x + (rect.w * (index + 1)) / (residents.length + 1),
        y: rect.y + rect.h * 0.5,
      };
    }

    function agentMode(actor) {
      if (!actor.terminal_run_id) return "resting";
      const status = normalizeStatus(actor.terminal_status);
      if (status === "waiting_user_answer") return "question";
      if (status === "error" || status === "failed") return "error";
      if (status === "processing") return "working";
      return "neutral";
    }

    function computeTarget(agent) {
      const actor = agent.actor;
      const rects = regionRects();
      if (!actor || !actor.terminal_run_id) return homePosition(agent);
      if (agent.mode === "question" && (agent.x !== 0 || agent.y !== 0)) {
        return { x: agent.x, y: agent.y };
      }
      const purpose = actor.terminal_purpose_kind;
      if (purpose === "work") {
        const work = entities.work.get(String(actor.terminal_purpose_id));
        if (work) return workPosition(work, rects);
        return { x: rects.workshop.cx, y: rects.workshop.cy };
      }
      if (purpose === "review") return { x: rects.verification.cx, y: rects.verification.cy };
      if (purpose === "consultation") return { x: rects.workshop.cx, y: rects.workshop.cy };
      return homePosition(agent);
    }

    function recomputeTargets() {
      for (const agent of entities.agents.values()) {
        const target = computeTarget(agent);
        agent.tx = target.x;
        agent.ty = target.y;
      }
    }

    function snapAgents() {
      for (const agent of entities.agents.values()) {
        agent.x = agent.tx;
        agent.y = agent.ty;
      }
    }

    // --- effects ---
    const MAX_EFFECTS = 140;

    function pushEffect(effect) {
      if (effects.length >= MAX_EFFECTS) effects.shift();
      effects.push(effect);
    }

    function agentPosition(slug) {
      const agent = entities.agents.get(slug);
      return agent ? { x: agent.x, y: agent.y } : channelHub();
    }

    function packet(from, to, color) {
      pushEffect({
        kind: "packet",
        x0: from.x,
        y0: from.y,
        x1: to.x,
        y1: to.y,
        born: now(),
        ttl: 900,
        color,
      });
    }

    function pulse(x, y, color) {
      pushEffect({ kind: "pulse", x0: x, y0: y, born: now(), ttl: 1200, color });
    }

    function ring(x, y, color) {
      pushEffect({ kind: "ring", x0: x, y0: y, born: now(), ttl: 900, color });
    }

    function flare(x, y, color) {
      pushEffect({ kind: "flare", x0: x, y0: y, born: now(), ttl: 1400, color });
    }

    function dissolve(x, y, color) {
      pushEffect({ kind: "dissolve", x0: x, y0: y, born: now(), ttl: 800, color });
    }

    function bloom(x, y) {
      pushEffect({ kind: "bloom", x0: x, y0: y, born: now(), ttl: 1000, color: "#168554" });
    }

    function now() {
      return performance.now();
    }

    function workIdFrom(entityId) {
      const value = entityId || "";
      return value.startsWith("work:") ? value.slice(5) : null;
    }

    function workPoint(workId) {
      const work = entities.work.get(String(workId));
      if (work) return workPosition(work, regionRects());
      return null;
    }

    function dispatch(event) {
      const kind = event.kind;
      const metadata = event.metadata || {};
      switch (kind) {
        case "message.posted": {
          const address = typeof metadata.address === "string" ? metadata.address : "";
          const sender = event.actor_slug;
          const from = entities.agents.has(sender) ? agentPosition(sender) : humanHub();
          let to = channelHub();
          if (address.startsWith("dm:")) {
            const peers = address.slice(3).split(":").filter(Boolean);
            const other = peers.find((p) => p !== sender && entities.agents.has(p));
            if (other) to = agentPosition(other);
            else if (peers.includes("human")) to = humanHub();
          }
          packet(from, to, "#2f6fb0");
          break;
        }
        case "work.progress": {
          const point = workPoint(workIdFrom(event.entity_id));
          if (point) pulse(point.x, point.y, "#168554");
          else if (entities.agents.has(event.actor_slug)) {
            const pos = agentPosition(event.actor_slug);
            pulse(pos.x, pos.y, "#168554");
          }
          break;
        }
        case "consultation.requested": {
          const rects = regionRects();
          const target = { x: rects.workshop.cx, y: rects.workshop.cy };
          packet(agentPosition(event.actor_slug), target, "#6d28d9");
          break;
        }
        case "consultation.completed": {
          const rects = regionRects();
          const from = { x: rects.workshop.cx, y: rects.workshop.cy };
          const to = entities.agents.has(event.actor_slug)
            ? agentPosition(event.actor_slug)
            : { x: rects.workshop.cx, y: rects.workshop.cy };
          packet(from, to, "#6d28d9");
          break;
        }
        case "work.submitted": {
          const rects = regionRects();
          const target = { x: rects.verification.cx, y: rects.verification.cy };
          packet(agentPosition(event.actor_slug), target, "#0e7490");
          break;
        }
        case "review.submitted": {
          const point = workPoint(workIdFrom(event.entity_id));
          if (point) ring(point.x, point.y, "#0e7490");
          break;
        }
        case "blocker.created": {
          const point = workPoint(workIdFrom(event.entity_id)) || workPoint(metadata.id);
          if (point) flare(point.x, point.y, "#a33131");
          break;
        }
        case "blocker.resolved": {
          const point = workPoint(workIdFrom(event.entity_id));
          if (point) dissolve(point.x, point.y, "#9b7d25");
          break;
        }
        case "decision.proposed": {
          const point = workPoint(workIdFrom(event.entity_id));
          const target = point || channelHub();
          flare(target.x, target.y, "#b45309");
          break;
        }
        case "decision.resolved": {
          dissolve(channelHub().x, channelHub().y, "#9b7d25");
          break;
        }
        case "terminal.status_changed": {
          const pos = entities.agents.has(event.actor_slug)
            ? agentPosition(event.actor_slug)
            : null;
          if (pos) pulse(pos.x, pos.y, "#265d97");
          break;
        }
        default:
          break;
      }
    }

    // --- reconcile ---
    function reconcile(snapshot) {
      lastSnapshot = snapshot || lastSnapshot;
      if (!snapshot) return;

      const roster = Array.isArray(snapshot.roster) ? snapshot.roster : [];
      const seenAgents = new Set();
      for (const actor of roster) {
        if (actor.kind !== "agent") continue;
        seenAgents.add(actor.slug);
        let agent = entities.agents.get(actor.slug);
        if (!agent) {
          agent = {
            slug: actor.slug,
            appearance: agentAppearance(actor.slug),
            x: 0,
            y: 0,
            tx: 0,
            ty: 0,
          };
          entities.agents.set(actor.slug, agent);
        }
        agent.actor = actor;
        agent.mode = agentMode(actor);
      }
      for (const slug of [...entities.agents.keys()]) {
        if (!seenAgents.has(slug)) entities.agents.delete(slug);
      }

      const board = Array.isArray(snapshot.board) ? snapshot.board : [];
      const seenWork = new Set();
      const rectsNow = regionRects();
      for (const work of board) {
        if (work.status === "cancelled") continue;
        seenWork.add(work.id);
        let item = entities.work.get(work.id);
        if (!item) {
          item = { id: work.id, appearance: workAppearance(work.id) };
          entities.work.set(work.id, item);
        }
        const previous = item.status;
        item.status = work.status;
        if (
          previous &&
          previous !== work.status &&
          (work.status === "accepted" || work.status === "delivered")
        ) {
          const pos = workPosition(item, rectsNow);
          bloom(pos.x, pos.y);
        }
      }
      for (const [id, item] of entities.work) {
        if (!seenWork.has(id)) {
          const pos = workPosition(item, rectsNow);
          dissolve(pos.x, pos.y, item.appearance.color);
        }
      }
      for (const id of [...entities.work.keys()]) {
        if (!seenWork.has(id)) entities.work.delete(id);
      }

      blockers = Array.isArray(snapshot.blockers) ? snapshot.blockers : [];

      recomputeTargets();
      for (const agent of entities.agents.values()) {
        if (agent.x === 0 && agent.y === 0 && agent.tx !== 0) {
          agent.x = agent.tx;
          agent.y = agent.ty;
        }
      }
      renderSummary();
      if (!motion) snapAgents();
      draw(performance.now());
      ensureLoop();
    }

    function onEvent(event) {
      if (!event || typeof event.kind !== "string") return;
      if (connection !== "live") return;
      try {
        dispatch(event);
        ensureLoop();
      } catch {
        // Best-effort animation; hydration remains authoritative.
      }
    }

    function setConnection(status) {
      connection = status === "reconnecting" ? "reconnecting" : "live";
      if (section) section.classList.toggle("reconnecting", connection === "reconnecting");
      renderSummary();
      if (connection === "reconnecting") {
        if (rafId) cancelAnimationFrame(rafId);
        rafId = 0;
        draw(performance.now());
      } else {
        ensureLoop();
      }
    }

    function setMotionEnabled(enabled) {
      const next = Boolean(enabled);
      if (next === motion) return;
      motion = next;
      updateMotionButton();
      if (!motion) {
        snapAgents();
        if (rafId) cancelAnimationFrame(rafId);
        rafId = 0;
        draw(performance.now());
      } else {
        ensureLoop();
      }
    }

    // --- summary ---
    function renderSummary() {
      if (!summaryEl) return;
      const snapshot = lastSnapshot;
      if (!snapshot) {
        summaryEl.textContent = "";
        return;
      }
      const roster = Array.isArray(snapshot.roster) ? snapshot.roster : [];
      const board = Array.isArray(snapshot.board) ? snapshot.board : [];
      let active = 0;
      let waiting = 0;
      for (const actor of roster) {
        if (actor.kind !== "agent" || !actor.terminal_run_id) continue;
        const status = normalizeStatus(actor.terminal_status);
        if (status === "waiting_user_answer") waiting += 1;
        else if (status === "processing") active += 1;
      }
      const blocked = board.filter((work) => work.status === "blocked").length;
      const verifying = board.filter(
        (work) => work.status === "verifying" || work.status === "awaiting_approval",
      ).length;
      const blockerCount = blockers.length;
      if (connection === "reconnecting") {
        summaryEl.textContent = `Reconnecting. Last known state: ${active} agent${
          active === 1 ? "" : "s"
        } active and ${blockerCount} blocker${blockerCount === 1 ? "" : "s"} needing attention.`;
        return;
      }
      const parts = [`${active} agent${active === 1 ? "" : "s"} active`];
      if (waiting) parts.push(`${waiting} waiting`);
      if (blocked) parts.push(`${blocked} work item${blocked === 1 ? "" : "s"} blocked`);
      if (verifying) parts.push(`${verifying} awaiting verification`);
      summaryEl.textContent = `Live. ${parts.join(", ")}.`;
    }

    function updateMotionButton() {
      if (!motionButton) return;
      motionButton.textContent = motion ? "Pause motion" : "Resume motion";
      motionButton.setAttribute("aria-pressed", String(!motion));
    }

    // --- motion loop ---
    function needsMotion() {
      if (effects.length) return true;
      for (const agent of entities.agents.values()) {
        if (agent.mode === "working" || agent.mode === "error") return true;
        if (Math.abs(agent.x - agent.tx) > 0.5 || Math.abs(agent.y - agent.ty) > 0.5) return true;
      }
      return false;
    }

    function step(t) {
      const dt = lastT ? clamp(t - lastT, 0, 50) : 16;
      lastT = t;
      for (const agent of entities.agents.values()) {
        const dx = agent.tx - agent.x;
        const dy = agent.ty - agent.y;
        const dist = Math.hypot(dx, dy);
        if (dist < 0.5) {
          agent.x = agent.tx;
          agent.y = agent.ty;
          continue;
        }
        const travel = Math.min(dist, agent.appearance.speed * dt);
        agent.x += (dx / dist) * travel;
        agent.y += (dy / dist) * travel;
      }
      effects = effects.filter((effect) => t - effect.born < effect.ttl);
    }

    function frame(t) {
      if (destroyed) return;
      const animate = motion && connection === "live" && !document.hidden;
      if (animate) step(t);
      draw(t);
      if (animate && needsMotion()) {
        rafId = requestAnimationFrame(frame);
      } else {
        rafId = 0;
      }
    }

    function ensureLoop() {
      if (destroyed || rafId) return;
      if (!motion || document.hidden || connection !== "live") return;
      if (!needsMotion()) return;
      rafId = requestAnimationFrame(frame);
    }

    // --- drawing ---
    function draw(t) {
      const width = viewport.width;
      const height = viewport.height;
      if (!width || !height) return;
      const ratio = viewport.dpr;
      const pw = Math.round(width * ratio);
      const ph = Math.round(height * ratio);
      if (canvas.width !== pw || canvas.height !== ph) {
        canvas.width = pw;
        canvas.height = ph;
      }
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      hitboxes = [];
      const rects = regionRects();
      drawBackground(width, height, rects);
      drawWork(rects, t);
      drawTrails(t);
      drawAgents(t);
      drawIndicators(rects, t);
      drawEffects(t);
      drawLabels();
    }

    function drawBackground(width, height, rects) {
      ctx.fillStyle = "#f8fafc";
      ctx.fillRect(0, 0, width, height);
      for (const region of REGIONS) {
        const rect = rects[region.key];
        ctx.fillStyle = region.fill;
        rr(ctx, rect.x, rect.y, rect.w, rect.h, 8);
        ctx.fill();
        ctx.strokeStyle = region.stroke;
        ctx.lineWidth = 1;
        rr(ctx, rect.x, rect.y, rect.w, rect.h, 8);
        ctx.stroke();
        ctx.fillStyle = "#64748b";
        ctx.font = "10px Inter, system-ui, sans-serif";
        ctx.textBaseline = "top";
        ctx.textAlign = "left";
        ctx.fillText(region.label, rect.x + 6, rect.y + 4);
      }
      const hub = channelHub();
      ctx.fillStyle = "#94a3b8";
      ctx.beginPath();
      ctx.arc(hub.x, hub.y, 3, 0, Math.PI * 2);
      ctx.fill();
      const operator = humanHub();
      ctx.fillStyle = "#64748b";
      ctx.beginPath();
      ctx.arc(operator.x, operator.y, 4, 0, Math.PI * 2);
      ctx.fill();
    }

    function drawWork(rects, t) {
      for (const work of entities.work.values()) {
        const pos = workPosition(work, rects);
        if (work.status === "verifying" || work.status === "awaiting_approval") {
          const orbit =
            work.appearance.radius + 5 + Math.sin(t * 0.003 + work.appearance.phase) * 1.5;
          ctx.save();
          ctx.strokeStyle = "#0e7490";
          ctx.globalAlpha = 0.8;
          ctx.lineWidth = 1.2;
          ctx.beginPath();
          ctx.arc(pos.x, pos.y, orbit, 0, Math.PI * 2);
          ctx.stroke();
          ctx.restore();
        }
        drawWorkShape(work, pos, t);
        hitboxes.push({
          x: pos.x,
          y: pos.y,
          r: work.appearance.radius + 5,
          activate: () => cb.onWork && cb.onWork(work.id),
        });
      }
    }

    function drawWorkShape(work, pos, t) {
      const app = work.appearance;
      const settled = work.status === "accepted" || work.status === "delivered";
      ctx.save();
      ctx.globalAlpha = settled ? 0.7 : 1;
      ctx.translate(pos.x, pos.y);
      ctx.fillStyle = app.color;
      ctx.strokeStyle = app.color;
      ctx.lineWidth = 1;
      const r = app.radius;
      switch (app.shape) {
        case "node":
          ctx.beginPath();
          ctx.arc(0, 0, r, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = "#ffffff";
          ctx.beginPath();
          ctx.arc(0, 0, r * 0.4, 0, Math.PI * 2);
          ctx.fill();
          break;
        case "specimen":
          rr(ctx, -r * 0.6, -r * 0.8, r * 1.2, r * 1.6, 2);
          ctx.fill();
          ctx.fillStyle = "#ffffff";
          rr(ctx, -r * 0.6, -r * 0.8, r * 1.2, r * 0.45, 2);
          ctx.fill();
          break;
        case "constellation": {
          const n = 4;
          ctx.beginPath();
          for (let i = 0; i < n; i++) {
            const angle = app.phase + (i / n) * Math.PI * 2;
            const radius = r * (0.5 + ((i * 7919) % 5) / 8);
            const px = Math.cos(angle) * radius;
            const py = Math.sin(angle) * radius;
            if (i === 0) ctx.moveTo(px, py);
            else ctx.lineTo(px, py);
          }
          ctx.stroke();
          for (let i = 0; i < n; i++) {
            const angle = app.phase + (i / n) * Math.PI * 2;
            const radius = r * (0.5 + ((i * 7919) % 5) / 8);
            ctx.beginPath();
            ctx.arc(Math.cos(angle) * radius, Math.sin(angle) * radius, 1.4, 0, Math.PI * 2);
            ctx.fill();
          }
          break;
        }
        default:
          break;
      }
      ctx.restore();
    }

    function drawAgents(t) {
      for (const agent of entities.agents.values()) {
        drawAgent(agent, t);
        hitboxes.push({
          x: agent.x,
          y: agent.y,
          r: agent.appearance.radius + 6,
          activate: () => cb.onAgent && cb.onAgent(agent.actor),
        });
      }
    }

    function drawAgent(agent, t) {
      const app = agent.appearance;
      const phase = t * 0.001 * app.cadence + app.idleOffset;
      let bob = 0;
      if (agent.mode === "working") bob = Math.sin(phase * 2) * 2;
      else if (agent.mode === "resting" || agent.mode === "neutral") bob = Math.sin(phase) * 1;
      ctx.save();
      ctx.translate(agent.x, agent.y + bob);
      ctx.fillStyle = app.color;
      ctx.strokeStyle = app.color;
      ctx.lineWidth = 1.2;
      const r = app.radius;
      switch (app.silhouette) {
        case "rover":
          ctx.fillRect(-r, -r * 0.5, r * 2, r);
          ctx.beginPath();
          ctx.arc(-r * 0.6, r * 0.5, r * 0.35, 0, Math.PI * 2);
          ctx.fill();
          ctx.beginPath();
          ctx.arc(r * 0.6, r * 0.5, r * 0.35, 0, Math.PI * 2);
          ctx.fill();
          break;
        case "firefly": {
          const glow = 0.5 + (Math.sin(phase) + 1) * 0.25;
          ctx.globalAlpha = glow;
          ctx.beginPath();
          ctx.arc(0, 0, r, 0, Math.PI * 2);
          ctx.fill();
          ctx.globalAlpha = 1;
          break;
        }
        case "spark": {
          ctx.beginPath();
          ctx.moveTo(0, -r);
          ctx.lineTo(r * 0.7, 0);
          ctx.lineTo(0, r);
          ctx.lineTo(-r * 0.7, 0);
          ctx.closePath();
          ctx.fill();
          break;
        }
        case "beetle":
          ctx.beginPath();
          ctx.ellipse(0, 0, r, r * 0.7, 0, 0, Math.PI * 2);
          ctx.fill();
          break;
        case "moth":
          ctx.beginPath();
          ctx.moveTo(0, 0);
          ctx.lineTo(-r, -r * 0.8);
          ctx.lineTo(-r * 0.4, 0);
          ctx.closePath();
          ctx.fill();
          ctx.beginPath();
          ctx.moveTo(0, 0);
          ctx.lineTo(r, -r * 0.8);
          ctx.lineTo(r * 0.4, 0);
          ctx.closePath();
          ctx.fill();
          ctx.beginPath();
          ctx.arc(0, 0, r * 0.3, 0, Math.PI * 2);
          ctx.fill();
          break;
        default:
          ctx.beginPath();
          ctx.arc(0, 0, r, 0, Math.PI * 2);
          ctx.fill();
          break;
      }
      if (app.accessory === 1) {
        ctx.beginPath();
        ctx.moveTo(0, -r);
        ctx.lineTo(0, -r - 3);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(0, -r - 4, 1.5, 0, Math.PI * 2);
        ctx.fill();
      } else if (app.accessory === 2) {
        ctx.globalAlpha = 0.5;
        ctx.beginPath();
        ctx.arc(r * 0.6, r * 0.6, 1.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      ctx.restore();
    }

    function drawIndicators(rects, t) {
      for (const agent of entities.agents.values()) {
        const status = normalizeStatus(agent.actor && agent.actor.terminal_status);
        if (agent.mode === "question") {
          drawBeacon(agent.x, agent.y, 10 + Math.sin(t * 0.004) * 2, "#265d97");
        } else if (agent.mode === "error") {
          drawBeacon(agent.x, agent.y, 12 + Math.sin(t * 0.008) * 3, "#a33131");
        } else if (status === "completed") {
          drawBeacon(agent.x, agent.y, 9, "#168554");
        }
      }
      for (const blocker of blockers) {
        const point = blocker.work_id
          ? workPoint(blocker.work_id)
          : { x: rects.distress.cx, y: rects.distress.cy };
        const pos = point || { x: rects.distress.cx, y: rects.distress.cy };
        drawBeacon(pos.x, pos.y, 11 + Math.sin(t * 0.005) * 2, "#a33131");
        hitboxes.push({
          x: pos.x,
          y: pos.y,
          r: 14,
          activate: () => cb.onBlocker && cb.onBlocker(blocker),
        });
      }
    }

    function drawBeacon(x, y, radius, color) {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.globalAlpha = 0.5;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.stroke();
      ctx.globalAlpha = 0.25;
      ctx.beginPath();
      ctx.arc(x, y, radius * 0.6, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }

    function drawTrails(t) {
      for (const effect of effects) {
        if (effect.kind !== "packet") continue;
        const progress = clamp((t - effect.born) / effect.ttl, 0, 1);
        if (progress >= 1) continue;
        const x = lerp(effect.x0, effect.x1, ease(progress));
        const y = lerp(effect.y0, effect.y1, ease(progress));
        const tailX = lerp(effect.x0, effect.x1, ease(Math.max(0, progress - 0.08)));
        const tailY = lerp(effect.y0, effect.y1, ease(Math.max(0, progress - 0.08)));
        ctx.save();
        ctx.strokeStyle = effect.color;
        ctx.fillStyle = effect.color;
        ctx.globalAlpha = 0.6;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(tailX, tailY);
        ctx.lineTo(x, y);
        ctx.stroke();
        ctx.globalAlpha = 0.9;
        ctx.beginPath();
        ctx.arc(x, y, 2.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.restore();
      }
    }

    function drawEffects(t) {
      for (const effect of effects) {
        if (effect.kind === "packet") continue;
        const progress = clamp((t - effect.born) / effect.ttl, 0, 1);
        if (progress >= 1) continue;
        const alpha = 1 - progress;
        ctx.save();
        ctx.globalAlpha = alpha;
        ctx.strokeStyle = effect.color;
        ctx.fillStyle = effect.color;
        ctx.lineWidth = 1.5;
        switch (effect.kind) {
          case "pulse":
            ctx.beginPath();
            ctx.arc(effect.x0, effect.y0, 4 + progress * 22, 0, Math.PI * 2);
            ctx.stroke();
            break;
          case "ring":
            ctx.beginPath();
            ctx.arc(effect.x0, effect.y0, 3 + progress * 18, 0, Math.PI * 2);
            ctx.stroke();
            break;
          case "flare": {
            const rise = effect.y0 - progress * 24;
            ctx.beginPath();
            ctx.arc(effect.x0, rise, 3 + progress * 4, 0, Math.PI * 2);
            ctx.fill();
            break;
          }
          case "dissolve":
            ctx.beginPath();
            ctx.arc(effect.x0, effect.y0, 18 * (1 - progress), 0, Math.PI * 2);
            ctx.stroke();
            break;
          case "bloom": {
            const radius = 2 + progress * 14;
            ctx.beginPath();
            ctx.arc(effect.x0, effect.y0, radius, 0, Math.PI * 2);
            ctx.fill();
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.arc(effect.x0, effect.y0, radius + 3, 0, Math.PI * 2);
            ctx.stroke();
            break;
          }
          default:
            break;
        }
        ctx.restore();
      }
    }

    function drawLabels() {
      ctx.save();
      ctx.font = "9px Inter, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillStyle = "#94a3b8";
      for (const agent of entities.agents.values()) {
        const home = isAtHome(agent);
        ctx.textBaseline = home ? "bottom" : "top";
        ctx.fillText(
          agent.slug,
          agent.x,
          home ? agent.y - agent.appearance.radius - 5 : agent.y + agent.appearance.radius + 7,
        );
      }
      ctx.restore();
    }

    // --- interaction ---
    function onPointer(event) {
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      for (let i = hitboxes.length - 1; i >= 0; i--) {
        const box = hitboxes[i];
        const dx = x - box.x;
        const dy = y - box.y;
        if (dx * dx + dy * dy <= box.r * box.r) {
          box.activate();
          return;
        }
      }
    }

    function measure() {
      const rect = canvas.getBoundingClientRect();
      const width = Math.max(1, Math.round(rect.width));
      const height = Math.max(1, Math.round(rect.height));
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      if (width === viewport.width && height === viewport.height && ratio === viewport.dpr) return;
      viewport.width = width;
      viewport.height = height;
      viewport.dpr = ratio;
      if (lastSnapshot) {
        recomputeTargets();
        if (!motion) snapAgents();
        else ensureLoop();
      }
    }

    function onVisibility() {
      if (document.hidden) return;
      ensureLoop();
    }

    function destroy() {
      destroyed = true;
      if (rafId) cancelAnimationFrame(rafId);
      rafId = 0;
      if (resizeObserver) resizeObserver.disconnect();
      resizeObserver = null;
      if (motionButton) motionButton.removeEventListener("click", toggleMotion);
      canvas.removeEventListener("click", onPointer);
      document.removeEventListener("visibilitychange", onVisibility);
      entities.agents.clear();
      entities.work.clear();
      blockers = [];
      effects = [];
      hitboxes = [];
    }

    function toggleMotion() {
      setMotionEnabled(!motion);
    }

    // --- init ---
    motion = !reducedMotion;
    measure();
    resizeObserver = new ResizeObserver(() => {
      measure();
      draw(performance.now());
    });
    resizeObserver.observe(canvas);
    if (motionButton) motionButton.addEventListener("click", toggleMotion);
    canvas.addEventListener("click", onPointer);
    document.addEventListener("visibilitychange", onVisibility);
    updateMotionButton();
    renderSummary();
    draw(performance.now());

    return { reconcile, onEvent, setConnection, setMotionEnabled, destroy };
  };
})();
