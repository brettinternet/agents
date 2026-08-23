-- Rename the publishing actor without leaving the old persistent actor active.
INSERT INTO actors(
  slug,kind,reports_to,profile_template,specialty,persistent,capacity,created_at,updated_at
)
SELECT
  'writer',kind,reports_to,'writer',specialty,persistent,capacity,created_at,
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM actors
WHERE slug='yapper';

UPDATE deliveries
SET terminal_run_id=NULL
WHERE state='pending'
  AND terminal_run_id IN (
    SELECT id FROM terminal_runs WHERE actor_slug='yapper' AND purpose_kind='persistent'
  );

UPDATE consultations
SET responder=NULL,
    terminal_run_id=NULL,
    state='queued',
    version=version+1,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE state='assigned'
  AND terminal_run_id IN (
    SELECT id FROM terminal_runs WHERE actor_slug='yapper' AND purpose_kind='persistent'
  );

UPDATE terminal_inputs
SET state='failed',
    error='persistent actor renamed to writer',
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE state IN ('pending','sending')
  AND terminal_run_id IN (
    SELECT id FROM terminal_runs WHERE actor_slug='yapper' AND purpose_kind='persistent'
  );

UPDATE terminal_runs
SET state='ending',
    token_revoked_at=COALESCE(token_revoked_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    error=COALESCE(error,'persistent actor renamed to writer'),
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE actor_slug='yapper'
  AND purpose_kind='persistent'
  AND state IN ('reserved','creating','live','retained');

UPDATE actor_leases
SET released_at=COALESCE(released_at,strftime('%Y-%m-%dT%H:%M:%fZ','now'))
WHERE actor_slug='yapper' AND purpose_kind='persistent';

UPDATE actors SET reports_to='writer' WHERE reports_to='yapper';
UPDATE consultations SET requester='writer' WHERE requester='yapper';
UPDATE consultations SET responder='writer' WHERE responder='yapper';
UPDATE decisions SET proposed_by='writer' WHERE proposed_by='yapper';
UPDATE decisions SET resolved_by='writer' WHERE resolved_by='yapper';
UPDATE terminal_runs SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE actor_leases SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE assignments SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE reviews SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE blockers SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE blockers SET requested_role='writer' WHERE requested_role='yapper';
UPDATE approvals SET decided_by='writer' WHERE decided_by='yapper';
UPDATE conversation_members SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE messages SET sender_slug='writer' WHERE sender_slug='yapper';
UPDATE deliveries SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE terminal_inputs SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE events SET actor_slug='writer' WHERE actor_slug='yapper';
UPDATE mutation_requests SET identity='agent:writer' WHERE identity='agent:yapper';
UPDATE conversations
SET address=replace(address,':yapper',':writer')
WHERE kind='dm' AND instr(address,':yapper')>0;
UPDATE terminal_runs
SET purpose_id='writer'
WHERE purpose_kind='persistent' AND purpose_id='yapper';
UPDATE actor_leases
SET purpose_id='writer'
WHERE purpose_kind='persistent' AND purpose_id='yapper';

DELETE FROM actors WHERE slug='yapper';
