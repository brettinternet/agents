import { mkdir, open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { checkpointPayload, verifyCheckpointSignature, verifyConsistency } from "./crypto.js";

const DEFAULT_ORIGIN = "https://1f916.ai";

async function getJson(fetchImpl, url) {
  const response = await fetchImpl(url, {
    headers: { accept: "application/json" },
    redirect: "error",
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`${url.pathname} returned HTTP ${response.status}`);
  return response.json();
}

async function loadState(path) {
  try {
    const state = JSON.parse(await readFile(path, "utf8"));
    if (state.version !== 1 || typeof state.public_key !== "string" || typeof state.checkpoints !== "object" || state.checkpoints === null) {
      throw new Error("unsupported or malformed witness state");
    }
    return state;
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

async function saveState(path, state) {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.tmp`;
  await writeFile(temporary, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
  await rename(temporary, path);
}

async function acquireLock(path, statePath) {
  let handle;
  try {
    handle = await open(path, "wx", 0o600);
  } catch (error) {
    if (error?.code === "EEXIST") {
      throw new Error(`another witness is already using ${statePath}; if it was killed, inspect and remove ${path}`);
    }
    throw error;
  }

  try {
    await handle.writeFile(`${process.pid}\n`);
    return handle;
  } catch (error) {
    await handle.close().catch(() => {});
    await unlink(path).catch(() => {});
    throw error;
  }
}

function publicKeyFrom(response) {
  const key = response?.registry_public_key;
  if (key?.kty !== "OKP" || key?.crv !== "Ed25519" || typeof key.x !== "string") {
    throw new Error("checkpoint response has no usable Ed25519 registry key");
  }
  return key.x;
}

function checkpointMap(checkpoints) {
  if (!Array.isArray(checkpoints) || checkpoints.length === 0) {
    throw new Error("checkpoint response contains no logs");
  }
  const map = Object.create(null);
  for (const checkpoint of checkpoints) {
    if (!checkpoint || typeof checkpoint.log !== "string" || Object.hasOwn(map, checkpoint.log)) {
      throw new Error("checkpoint response contains an invalid or duplicate log");
    }
    map[checkpoint.log] = checkpoint;
  }
  return map;
}

function sameCheckpoint(left, right) {
  return left.log === right.log
    && left.tree_size === right.tree_size
    && left.root === right.root
    && left.sig === right.sig
    && left.created_at === right.created_at;
}

function assertSigned(checkpoint, publicKey, label = checkpoint.log) {
  if (!verifyCheckpointSignature(checkpoint, publicKey)) {
    throw new Error(`${label} checkpoint signature is invalid`);
  }
}

async function runWitness({
  origin,
  statePath,
  fetchImpl,
}) {
  const base = new URL(origin);
  if (base.pathname !== "/" || base.search || base.hash) throw new Error("origin must not contain a path, query, or fragment");

  const saved = await loadState(statePath);
  const latestUrl = new URL("/api/checkpoint", base);
  const latest = await getJson(fetchImpl, latestUrl);
  const advertisedKey = publicKeyFrom(latest);
  const publicKey = saved?.public_key ?? advertisedKey;
  if (saved && advertisedKey !== publicKey) {
    throw new Error(`registry key changed: pinned ${publicKey}, advertised ${advertisedKey}`);
  }

  const current = checkpointMap(latest.checkpoints);
  for (const checkpoint of Object.values(current)) assertSigned(checkpoint, publicKey);

  const results = [];
  if (saved) {
    for (const log of Object.keys(saved.checkpoints)) {
      if (!Object.hasOwn(current, log)) throw new Error(`previously witnessed log disappeared: ${log}`);
    }
  }

  for (const [log, checkpoint] of Object.entries(current)) {
    const previous = saved && Object.hasOwn(saved.checkpoints, log) ? saved.checkpoints[log] : undefined;
    if (!previous) {
      results.push({ log, status: saved ? "new" : "pinned", tree_size: checkpoint.tree_size });
      continue;
    }
    if (checkpoint.tree_size < previous.tree_size) {
      throw new Error(`${log} shrank from ${previous.tree_size} to ${checkpoint.tree_size}`);
    }
    if (checkpoint.tree_size === previous.tree_size) {
      if (checkpoint.root !== previous.root) throw new Error(`${log} root changed at tree size ${checkpoint.tree_size}`);
      results.push({ log, status: "unchanged", tree_size: checkpoint.tree_size });
      continue;
    }

    const proofUrl = new URL("/api/checkpoint/consistency", base);
    proofUrl.searchParams.set("log", log);
    proofUrl.searchParams.set("from", String(previous.tree_size));
    proofUrl.searchParams.set("to", String(checkpoint.tree_size));
    const consistency = await getJson(fetchImpl, proofUrl);
    if (
      consistency.log !== log
      || (consistency.from?.log !== undefined && consistency.from.log !== log)
      || (consistency.to?.log !== undefined && consistency.to.log !== log)
      || !sameCheckpoint({ ...consistency.from, log }, previous)
      || !sameCheckpoint({ ...consistency.to, log }, checkpoint)
    ) {
      throw new Error(`${log} consistency response does not match the witnessed endpoints`);
    }
    assertSigned({ ...consistency.from, log }, publicKey, `${log} previous`);
    assertSigned({ ...consistency.to, log }, publicKey, `${log} current`);
    if (!verifyConsistency(previous.tree_size, checkpoint.tree_size, previous.root, checkpoint.root, consistency.proof)) {
      throw new Error(`${log} append-only consistency proof is invalid`);
    }
    results.push({
      log,
      status: "advanced",
      from: previous.tree_size,
      to: checkpoint.tree_size,
      added: checkpoint.tree_size - previous.tree_size,
    });
  }

  const nextState = {
    version: 1,
    origin: base.origin,
    public_key: publicKey,
    observed_at: latest.now_utc ?? new Date().toISOString(),
    checkpoints: current,
  };
  await saveState(statePath, nextState);

  return {
    trust: saved ? "pinned-key" : "trust-on-first-use",
    public_key: publicKey,
    observed_at: nextState.observed_at,
    results,
    payloads: Object.values(current).map(checkpointPayload),
  };
}

export async function witness({
  origin = DEFAULT_ORIGIN,
  statePath = ".robot-witness.json",
  fetchImpl = fetch,
} = {}) {
  await mkdir(dirname(statePath), { recursive: true });
  const lockPath = `${statePath}.lock`;
  const lock = await acquireLock(lockPath, statePath);

  try {
    return await runWitness({ origin, statePath, fetchImpl });
  } finally {
    await lock.close().catch(() => {});
    await unlink(lockPath);
  }
}
