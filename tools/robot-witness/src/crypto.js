import { createHash, verify as verifySignature } from "node:crypto";

const HEX_32 = /^[0-9a-f]{64}$/;

export function fromBase64url(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error("expected unpadded base64url");
  }
  return Buffer.from(value, "base64url");
}

function fromHex(value) {
  if (typeof value !== "string" || !HEX_32.test(value)) {
    throw new Error("expected a lowercase 32-byte hex digest");
  }
  return Buffer.from(value, "hex");
}

function nodeHash(left, right) {
  return createHash("sha256").update(Buffer.from([1])).update(left).update(right).digest();
}

export function checkpointPayload(checkpoint) {
  return `1f916.checkpoint.v1:${checkpoint.log}:${checkpoint.tree_size}:${checkpoint.root}:${checkpoint.created_at}`;
}

export function verifyCheckpointSignature(checkpoint, publicKey) {
  if (!Number.isSafeInteger(checkpoint.tree_size) || checkpoint.tree_size < 0) {
    throw new Error("checkpoint tree_size must be a non-negative safe integer");
  }
  if (!Number.isSafeInteger(checkpoint.created_at) || checkpoint.created_at < 0) {
    throw new Error("checkpoint created_at must be a non-negative safe integer");
  }
  fromHex(checkpoint.root);
  const rawKey = fromBase64url(publicKey);
  const signature = fromBase64url(checkpoint.sig);
  if (rawKey.length !== 32 || signature.length !== 64) return false;

  const key = {
    key: Buffer.concat([
      Buffer.from("302a300506032b6570032100", "hex"),
      rawKey,
    ]),
    format: "der",
    type: "spki",
  };
  return verifySignature(null, Buffer.from(checkpointPayload(checkpoint)), key, signature);
}

export function verifyConsistency(oldSize, newSize, oldRoot, newRoot, proof) {
  if (!Number.isSafeInteger(oldSize) || !Number.isSafeInteger(newSize) || oldSize < 0 || newSize < oldSize) return false;
  if (!Array.isArray(proof)) return false;
  try {
    fromHex(oldRoot);
    fromHex(newRoot);
    if (oldSize === newSize) return proof.length === 0 && oldRoot === newRoot;
    if (oldSize === 0) return proof.length === 0;
    if (proof.length === 0) return false;

    let oldIndex = oldSize - 1;
    let newIndex = newSize - 1;
    while (oldIndex % 2 === 1) {
      oldIndex = Math.floor(oldIndex / 2);
      newIndex = Math.floor(newIndex / 2);
    }

    const path = proof.map(fromHex);
    let cursor = 0;
    let oldHash;
    let newHash;
    if (oldIndex === 0) {
      oldHash = fromHex(oldRoot);
      newHash = oldHash;
    } else {
      oldHash = path[0];
      newHash = path[0];
      cursor = 1;
    }

    for (; cursor < path.length; cursor += 1) {
      if (newIndex === 0) return false;
      const hash = path[cursor];
      if (oldIndex % 2 === 1 || oldIndex === newIndex) {
        oldHash = nodeHash(hash, oldHash);
        newHash = nodeHash(hash, newHash);
        while (oldIndex % 2 === 0 && oldIndex !== 0) {
          oldIndex = Math.floor(oldIndex / 2);
          newIndex = Math.floor(newIndex / 2);
        }
      } else {
        newHash = nodeHash(newHash, hash);
      }
      oldIndex = Math.floor(oldIndex / 2);
      newIndex = Math.floor(newIndex / 2);
    }

    return oldHash.toString("hex") === oldRoot
      && newHash.toString("hex") === newRoot
      && newIndex === 0;
  } catch {
    return false;
  }
}
