#!/usr/bin/env node

import { resolve } from "node:path";
import { witness } from "../src/witness.js";

const HELP = `robot-witness — keep a local, append-only witness of 1F916

Usage:
  robot-witness [--state <file>] [--origin <url>] [--json]

The first run verifies the registry's checkpoint signatures and pins its
advertised Ed25519 key (trust on first use). Later runs reject key changes,
log truncation, changed historical roots, and invalid RFC 6962 consistency
proofs before atomically advancing the local state.

Options:
  --state <file>  Witness state path (default: .robot-witness.json)
  --origin <url>  Registry origin (default: https://1f916.ai)
  --json          Print the machine-readable report
  -h, --help      Show this help
`;

function parseArgs(args) {
  const options = {
    statePath: resolve(".robot-witness.json"),
    origin: "https://1f916.ai",
    json: false,
  };
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === "-h" || argument === "--help") return { help: true };
    if (argument === "--json") {
      options.json = true;
      continue;
    }
    if (argument === "--state" || argument === "--origin") {
      const value = args[index + 1];
      if (!value || value.startsWith("--")) throw new Error(`${argument} requires a value`);
      index += 1;
      if (argument === "--state") options.statePath = resolve(value);
      else options.origin = value;
      continue;
    }
    throw new Error(`unknown option: ${argument}`);
  }
  return options;
}

function printReport(report) {
  console.log(`1F916 witness: ${report.trust}`);
  console.log(`key: ${report.public_key}`);
  for (const result of report.results) {
    if (result.status === "advanced") {
      console.log(
        `ok  ${result.log}: ${result.from} -> ${result.to} (+${result.added}), append-only proof valid`,
      );
    } else {
      console.log(`ok  ${result.log}: ${result.tree_size} (${result.status})`);
    }
  }
  console.log(`observed: ${report.observed_at}`);
  if (report.trust === "trust-on-first-use") {
    console.log(
      "note: compare the pinned key with an independent 1F916 witness before relying on it",
    );
  }
}

try {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(HELP);
  } else {
    const report = await witness(options);
    if (options.json) console.log(JSON.stringify(report, null, 2));
    else printReport(report);
  }
} catch (error) {
  console.error(`robot-witness: ${error.message}`);
  process.exitCode = 1;
}
