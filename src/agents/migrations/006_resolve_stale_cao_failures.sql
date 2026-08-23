UPDATE blockers
SET state='resolved',
    resolution='Superseded stale CAO session from a previous project working directory',
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE state IN ('open','escalated')
  AND target_kind='persistent'
  AND terminal_run_id IN (
    SELECT id
    FROM terminal_runs
    WHERE purpose_kind='persistent'
      AND error LIKE 'CAO terminal working directory mismatch:%'
  );

UPDATE incidents
SET state='resolved',
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE state='open'
  AND entity_kind='terminal'
  AND entity_id IN (
    SELECT CAST(id AS TEXT)
    FROM terminal_runs
    WHERE purpose_kind='persistent'
      AND error LIKE 'CAO terminal working directory mismatch:%'
  );
