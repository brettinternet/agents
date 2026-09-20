# Scheduled jobs

Supercronic reads [`crontab`](crontab) in UTC and reloads it when the file changes. No job is enabled by default.

Keep prompts as reviewed files under `jobs/prompts/` and schedule only `/workspace/bin/run-agent PROMPT_FILE`. A scheduled run gets a fresh Pi session and stores raw stdout, stderr, and status under ignored `.pi-data/runs/`.

Runs are terminated after six hours by default. Prefix a crontab command with `AGENT_RUN_TIMEOUT=2h` (or another GNU `timeout` duration) to use a shorter bound.

A prompt should identify:

- the bounded objective and stopping condition;
- whether external writes are forbidden or explicitly authorized;
- for external writes, the exact account, venue, action, cadence, and verification requirement;
- the expected public-safe result path under `results/`; and
- what, if anything, is worth promoting to `memory/`.

Before enabling a schedule:

1. Run its prompt manually with `task job:run -- jobs/prompts/NAME.md`.
2. Review the result and any external effects.
3. Ensure concurrent account or browser access is coordinated with Worklease.
4. Add the crontab entry and run `task scheduler:reload`.

Supercronic does not catch up jobs missed while the sandbox or host is stopped. Never automatically retry an external write after an ambiguous result; inspect the venue first.

To provide the full encrypted environment to a job, make that explicit in the crontab command:

```cron
0 9 * * * sops exec-env /workspace/secrets.sops.env '/workspace/bin/run-agent /workspace/jobs/prompts/NAME.md'
```

This exposes every value in `secrets.sops.env` to that Pi process. Prefer persistent Pi login for model authentication and narrow, venue-specific command wrappers where possible.
