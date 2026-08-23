-- migrate: foreign-keys-off
-- Rename actor identities and add implementation as a durable work specialty.

CREATE TABLE work_items_new (
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  parent_id TEXT REFERENCES work_items_new(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK (kind IN ('story','bug','task','spike')),
  title TEXT NOT NULL,
  problem TEXT NOT NULL,
  outcome TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('intake','refining','ready','in_progress','verifying','awaiting_approval','accepted','delivered','blocked','cancelled')),
  blocked_from TEXT,
  priority TEXT NOT NULL CHECK (priority IN ('urgent','high','normal','low')),
  specialty TEXT CHECK (specialty IN ('implementation','research','publishing')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  active_execution_id INTEGER REFERENCES executions(id) ON DELETE RESTRICT,
  accepted_submission_id INTEGER REFERENCES submissions(id) ON DELETE RESTRICT,
  integration_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
INSERT INTO work_items_new SELECT * FROM work_items;
DROP TABLE work_items;
ALTER TABLE work_items_new RENAME TO work_items;

CREATE TABLE review_requirements_new (
  work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  gate TEXT NOT NULL CHECK (gate IN ('implementation','research','publishing','coordination')),
  PRIMARY KEY (work_id, gate)
);
INSERT INTO review_requirements_new SELECT * FROM review_requirements;
DROP TABLE review_requirements;
ALTER TABLE review_requirements_new RENAME TO review_requirements;

CREATE TABLE consultations_new (
  id INTEGER PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  specialty TEXT NOT NULL CHECK (specialty IN ('implementation','research','publishing','coordination')),
  question TEXT NOT NULL,
  requester TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  responder TEXT REFERENCES actors(slug) ON DELETE RESTRICT,
  state TEXT NOT NULL CHECK (state IN ('queued','assigned','completed','failed','cancelled')),
  response TEXT,
  terminal_run_id INTEGER REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
INSERT INTO consultations_new SELECT * FROM consultations;
DROP TABLE consultations;
ALTER TABLE consultations_new RENAME TO consultations;
CREATE UNIQUE INDEX one_nonterminal_consultation ON consultations(work_id,specialty) WHERE state IN ('queued','assigned');

CREATE TABLE reviews_new (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
  gate TEXT NOT NULL CHECK (gate IN ('implementation','research','publishing','coordination')),
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  terminal_run_id INTEGER REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  worktree_path TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('pending','pass','changes_requested','superseded')),
  body TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
INSERT INTO reviews_new SELECT * FROM reviews;
DROP TABLE reviews;
ALTER TABLE reviews_new RENAME TO reviews;
CREATE UNIQUE INDEX one_current_review_gate ON reviews(submission_id,gate) WHERE verdict='pending';

INSERT INTO actors(slug,kind,reports_to,profile_template,specialty,persistent,capacity,created_at,updated_at)
SELECT 'manager',kind,reports_to,'manager',specialty,persistent,capacity,created_at,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM actors WHERE slug='elder';
INSERT INTO actors(slug,kind,reports_to,profile_template,specialty,persistent,capacity,created_at,updated_at)
SELECT 'researcher',kind,reports_to,'researcher',specialty,persistent,capacity,created_at,strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM actors WHERE slug='explorer';

UPDATE deliveries SET terminal_run_id=NULL WHERE state='pending' AND terminal_run_id IN (SELECT id FROM terminal_runs WHERE actor_slug IN ('elder','explorer') AND purpose_kind='persistent');
UPDATE consultations SET responder=NULL,terminal_run_id=NULL,state='queued',version=version+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE state='assigned' AND terminal_run_id IN (SELECT id FROM terminal_runs WHERE actor_slug IN ('elder','explorer') AND purpose_kind='persistent');
UPDATE terminal_inputs SET state='failed',error='persistent actor renamed',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE state IN ('pending','sending') AND terminal_run_id IN (SELECT id FROM terminal_runs WHERE actor_slug IN ('elder','explorer') AND purpose_kind='persistent');
UPDATE terminal_runs SET state='ending',token_revoked_at=COALESCE(token_revoked_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),error=COALESCE(error,'persistent actor renamed'),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE actor_slug IN ('elder','explorer') AND purpose_kind='persistent' AND state IN ('reserved','creating','live','retained');
UPDATE actor_leases SET released_at=COALESCE(released_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) WHERE actor_slug IN ('elder','explorer') AND purpose_kind='persistent';

UPDATE actors SET reports_to=CASE reports_to WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE reports_to END;
UPDATE consultations SET requester=CASE requester WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE requester END;
UPDATE consultations SET responder=CASE responder WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE responder END;
UPDATE decisions SET proposed_by=CASE proposed_by WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE proposed_by END;
UPDATE decisions SET resolved_by=CASE resolved_by WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE resolved_by END;
UPDATE terminal_runs SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE actor_leases SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE assignments SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE reviews SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE blockers SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE blockers SET requested_role=CASE requested_role WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE requested_role END;
UPDATE approvals SET decided_by=CASE decided_by WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE decided_by END;
UPDATE conversation_members SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE messages SET sender_slug=CASE sender_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE sender_slug END;
UPDATE deliveries SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE terminal_inputs SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE events SET actor_slug=CASE actor_slug WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE actor_slug END;
UPDATE mutation_requests SET identity='agent:manager' WHERE identity='agent:elder';
UPDATE mutation_requests SET identity='agent:researcher' WHERE identity='agent:explorer';
UPDATE conversations SET address=replace(replace(address,':elder',':manager'),':explorer',':researcher') WHERE kind='dm';
UPDATE terminal_runs SET purpose_id=CASE purpose_id WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE purpose_id END WHERE purpose_kind='persistent';
UPDATE actor_leases SET purpose_id=CASE purpose_id WHEN 'elder' THEN 'manager' WHEN 'explorer' THEN 'researcher' ELSE purpose_id END WHERE purpose_kind='persistent';
DELETE FROM actors WHERE slug IN ('elder','explorer');
