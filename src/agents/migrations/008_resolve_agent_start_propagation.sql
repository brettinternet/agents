UPDATE blockers
SET state = 'resolved',
    resolution = 'Superseded Herdr agent-start propagation race',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE state IN ('open', 'escalated')
  AND target_kind = 'persistent'
  AND terminal_run_id IN (
    SELECT id
    FROM terminal_runs
    WHERE purpose_kind = 'persistent'
      AND error = 'backend run identity, occupant, or cwd mismatch'
  );

UPDATE incidents
SET state = 'resolved',
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE state = 'open'
  AND entity_kind = 'terminal'
  AND entity_id IN (
    SELECT CAST(id AS TEXT)
    FROM terminal_runs
    WHERE purpose_kind = 'persistent'
      AND error = 'backend run identity, occupant, or cwd mismatch'
  );

UPDATE terminal_runs
SET error = 'transient Herdr cutover: ' || error,
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE purpose_kind = 'persistent'
  AND state IN ('ended', 'failed')
  AND error = 'backend run identity, occupant, or cwd mismatch';
