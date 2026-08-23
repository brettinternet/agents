-- Historical upgrade from the pre-Herdr schema; legacy names exist only long enough to migrate persisted rows.
ALTER TABLE terminal_runs RENAME COLUMN session_name TO execution_name;
ALTER TABLE terminal_runs RENAME COLUMN terminal_id TO backend_terminal_id;
ALTER TABLE terminal_runs ADD COLUMN execution_backend TEXT NOT NULL DEFAULT 'cao';
ALTER TABLE terminal_runs ADD COLUMN backend_run_id TEXT;
ALTER TABLE terminal_runs ADD COLUMN agent_auth_id TEXT;
ALTER TABLE terminal_runs ADD COLUMN backend_revision INTEGER;

UPDATE terminal_runs
SET backend_run_id = execution_name,
    agent_auth_id = backend_terminal_id
WHERE backend_terminal_id IS NOT NULL;


CREATE UNIQUE INDEX terminal_runs_agent_auth_id_unique
ON terminal_runs(agent_auth_id)
WHERE agent_auth_id IS NOT NULL;

ALTER TABLE wake_attempts RENAME COLUMN cao_message_id TO backend_message_id;
