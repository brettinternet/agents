import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { verifyCheckpointSignature, verifyConsistency } from "../src/crypto.js";
import { witness } from "../src/witness.js";

const PUBLIC_KEY = "mpQPa0FjyynqoSg2Z9j91hRhb8WckxIpRGod43CQqLw";
const OLD = {
  log: "identity_events",
  tree_size: 1868,
  root: "1935c57755ea7807b6f4d23b11ac4ecf37be2c3451b1632aa49e96a47f8097bb",
  sig: "HNu6L78oaA_M-bHhg_ESrLH9r_4fPAdVSYf9P0xGxxsuVslaP3epuQUH4ppMPkLtb6xBZNYJhZc4rRusnN2NCA",
  created_at: 1787269513447,
};
const CURRENT = {
  log: "identity_events",
  tree_size: 2222,
  root: "4016215963d052aa7f2a3f2c18661461c6784b1fbf6d825eaa0410cf2de5cb1e",
  sig: "7ErW3cJyxGQrLqkVRPx4y3kCql7gX373JILzDLjHERpQHZRWagjgeLlROQ7jTcaJYRrivd2E7IAvvuypFFpIAw",
  created_at: 1787373340271,
};
const PROOF = [
  "db82017b15ee80c054d54006b00460372046d631441c2f00115471faf05036fd",
  "2029769f68056a8aff9eeff4a2f9e9cc0e4e85932670495a90a25f06c72e17d0",
  "c2c97979baa92b2dd13a5f10d707a54ce6814047536e583d49e3c1c32d7d53b3",
  "b4af564b5edc02d7e647ff67dfd41c25cb93ecb611642d955e493cbbf6c4b4d3",
  "31892bc09c0de48e849a3ce5464f21d86845800292588bb022c42be278a44f4e",
  "7ff1961675527eec332e7e33a06ecc38c02a46ef1c88ad5cdd17fcf8e3b6ada9",
  "5b60c3233d206fa2c446741c5e18037501c2b7997762e01842b54832c17a47e3",
  "25e68b1876893525720dd2458d23a3da702bc17dbb5781bcf04951749753ea21",
  "f5fb507b79effe63dde55d9c7f02f243e093db676c3d7724782d4b8af658b669",
  "b0900dfc739b99b9f96713ce3220fa1853434f2ce8c84cfd868d1ad2bbd7a605",
  "ba57e261d1a0f51406647f91abee7c78502539598413481ce83af393232c693f",
];

function checkpointResponse(checkpoint, publicKey = PUBLIC_KEY) {
  return {
    now_utc: "2026-08-22T04:39:54.723Z",
    registry_public_key: { kty: "OKP", crv: "Ed25519", x: publicKey },
    checkpoints: [checkpoint],
  };
}

function jsonResponse(body) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

test("verifies registry signatures over exact checkpoint payloads", () => {
  assert.equal(verifyCheckpointSignature(OLD, PUBLIC_KEY), true);
  assert.equal(verifyCheckpointSignature({ ...OLD, tree_size: OLD.tree_size + 1 }, PUBLIC_KEY), false);
});

test("verifies a live RFC 6962 append-only proof and rejects tampering", () => {
  assert.equal(verifyConsistency(OLD.tree_size, CURRENT.tree_size, OLD.root, CURRENT.root, PROOF), true);
  const tampered = [...PROOF];
  tampered[3] = `0${tampered[3].slice(1)}`;
  assert.equal(verifyConsistency(OLD.tree_size, CURRENT.tree_size, OLD.root, CURRENT.root, tampered), false);
});

test("pins on first use, then advances only through a valid consistency proof", async () => {
  const directory = await mkdtemp(join(tmpdir(), "robot-witness-"));
  const statePath = join(directory, "state.json");
  let latest = OLD;
  const requests = [];
  const fetchImpl = async (url) => {
    requests.push(url.toString());
    if (url.pathname === "/api/checkpoint") return jsonResponse(checkpointResponse(latest));
    assert.equal(url.searchParams.get("from"), String(OLD.tree_size));
    assert.equal(url.searchParams.get("to"), String(CURRENT.tree_size));
    return jsonResponse({ log: "identity_events", from: OLD, to: CURRENT, proof: PROOF });
  };

  const first = await witness({ statePath, fetchImpl });
  assert.equal(first.trust, "trust-on-first-use");
  assert.deepEqual(first.results, [{ log: "identity_events", status: "pinned", tree_size: OLD.tree_size }]);

  latest = CURRENT;
  const second = await witness({ statePath, fetchImpl });
  assert.deepEqual(second.results, [{
    log: "identity_events",
    status: "advanced",
    from: OLD.tree_size,
    to: CURRENT.tree_size,
    added: CURRENT.tree_size - OLD.tree_size,
  }]);
  assert.equal(requests.length, 3);
  const saved = JSON.parse(await readFile(statePath, "utf8"));
  assert.equal(saved.checkpoints.identity_events.root, CURRENT.root);
  assert.equal(saved.public_key, PUBLIC_KEY);
});

test("rejects a consistency endpoint that names another log", async () => {
  const directory = await mkdtemp(join(tmpdir(), "robot-witness-"));
  const statePath = join(directory, "state.json");
  await witness({ statePath, fetchImpl: async () => jsonResponse(checkpointResponse(OLD)) });
  const before = await readFile(statePath, "utf8");
  const fetchImpl = async (url) => {
    if (url.pathname === "/api/checkpoint") return jsonResponse(checkpointResponse(CURRENT));
    return jsonResponse({
      log: "identity_events",
      from: { ...OLD, log: "ledger" },
      to: CURRENT,
      proof: PROOF,
    });
  };

  await assert.rejects(witness({ statePath, fetchImpl }), /does not match the witnessed endpoints/);
  assert.equal(await readFile(statePath, "utf8"), before);
});

test("rejects an advertised key change without replacing witnessed state", async () => {
  const directory = await mkdtemp(join(tmpdir(), "robot-witness-"));
  const statePath = join(directory, "state.json");
  await witness({ statePath, fetchImpl: async () => jsonResponse(checkpointResponse(OLD)) });
  const before = await readFile(statePath, "utf8");
  const changedKey = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";

  await assert.rejects(
    witness({ statePath, fetchImpl: async () => jsonResponse(checkpointResponse(OLD, changedKey)) }),
    /registry key changed/,
  );
  assert.equal(await readFile(statePath, "utf8"), before);
});

test("refuses concurrent writers instead of regressing witnessed state", async () => {
  const directory = await mkdtemp(join(tmpdir(), "robot-witness-"));
  const statePath = join(directory, "state.json");
  await witness({ statePath, fetchImpl: async () => jsonResponse(checkpointResponse(OLD)) });

  let releaseFetch;
  let fetchStarted;
  const started = new Promise((resolve) => {
    fetchStarted = resolve;
  });
  const release = new Promise((resolve) => {
    releaseFetch = resolve;
  });
  const running = witness({
    statePath,
    fetchImpl: async () => {
      fetchStarted();
      await release;
      return jsonResponse(checkpointResponse(OLD));
    },
  });
  await started;

  await assert.rejects(
    witness({ statePath, fetchImpl: async () => jsonResponse(checkpointResponse(OLD)) }),
    /another witness is already using/,
  );
  releaseFetch();
  await running;
  const saved = JSON.parse(await readFile(statePath, "utf8"));
  assert.equal(saved.checkpoints.identity_events.tree_size, OLD.tree_size);
});
