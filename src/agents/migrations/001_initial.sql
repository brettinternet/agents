CREATE TABLE project (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  instance_id TEXT NOT NULL UNIQUE CHECK (length(instance_id) = 8),
  name TEXT NOT NULL,
  canonical_path TEXT NOT NULL,
  git_common_dir TEXT NOT NULL,
  default_branch TEXT NOT NULL,
  verify_json TEXT NOT NULL,
  next_work_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_work_seq >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE actors (
  slug TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('human','system','agent')),
  reports_to TEXT REFERENCES actors(slug) ON DELETE RESTRICT,
  profile_template TEXT,
  specialty TEXT,
  persistent INTEGER NOT NULL CHECK (persistent IN (0,1)),
  capacity INTEGER NOT NULL CHECK (capacity >= 1),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE work_items (
  id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  parent_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK (kind IN ('story','bug','task','spike')),
  title TEXT NOT NULL,
  problem TEXT NOT NULL,
  outcome TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('intake','refining','ready','in_progress','verifying','awaiting_approval','accepted','delivered','blocked','cancelled')),
  blocked_from TEXT,
  priority TEXT NOT NULL CHECK (priority IN ('urgent','high','normal','low')),
  specialty TEXT CHECK (specialty IN ('research','publishing')),
  version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
  active_execution_id INTEGER REFERENCES executions(id) ON DELETE RESTRICT,
  accepted_submission_id INTEGER REFERENCES submissions(id) ON DELETE RESTRICT,
  integration_sha TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE acceptance_criteria (
  work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  position INTEGER NOT NULL CHECK (position >= 1),
  body TEXT NOT NULL,
  PRIMARY KEY (work_id, position)
);
CREATE TABLE dependencies (
  work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  depends_on_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  PRIMARY KEY (work_id, depends_on_id),
  CHECK (work_id <> depends_on_id)
);
CREATE TABLE review_requirements (
  work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  gate TEXT NOT NULL CHECK (gate IN ('research','publishing','coordination')),
  PRIMARY KEY (work_id, gate)
);
CREATE TABLE consultations (
  id INTEGER PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  specialty TEXT NOT NULL CHECK (specialty IN ('research','publishing','coordination')),
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
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY,
  work_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  question TEXT NOT NULL,
  options_json TEXT NOT NULL,
  recommendation TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open','resolved','cancelled')),
  resolution TEXT,
  proposed_by TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  resolved_by TEXT REFERENCES actors(slug) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE executions (
  id INTEGER PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  number INTEGER NOT NULL,
  base_sha TEXT NOT NULL,
  branch TEXT NOT NULL UNIQUE,
  worktree_path TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK (state IN ('preparing','active','superseded','closed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(work_id, number)
);
CREATE TABLE terminal_runs (
  id INTEGER PRIMARY KEY,
  session_name TEXT NOT NULL UNIQUE,
  profile_name TEXT NOT NULL,
  mcp_name TEXT NOT NULL,
  profile_sha256 TEXT NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK (generation >= 1),
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  purpose_kind TEXT NOT NULL CHECK (purpose_kind IN ('persistent','work','consultation','review')),
  purpose_id TEXT NOT NULL,
  working_directory TEXT NOT NULL,
  token_digest TEXT NOT NULL,
  token_revoked_at TEXT,
  terminal_id TEXT,
  profile_state TEXT NOT NULL CHECK (profile_state IN ('reserved','staged','installed','removed','failed')),
  state TEXT NOT NULL CHECK (state IN ('reserved','creating','live','retained','ending','ended','failed')),
  status TEXT,
  output_digest TEXT,
  output_tail TEXT NOT NULL DEFAULT '',
  digest_since TEXT,
  launch_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE terminal_artifacts (
  id INTEGER PRIMARY KEY,
  terminal_run_id INTEGER NOT NULL REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL CHECK (kind IN ('source','store','context','agent','tool','mcp','config','runtime_prompt','runtime_mcp')),
  path TEXT NOT NULL,
  fragment_key TEXT,
  expected_sha256 TEXT NOT NULL,
  expected_json_redacted TEXT,
  secret_fields_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('staged','installed','removed','failed')),
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(terminal_run_id, kind, path, fragment_key)
);
CREATE TABLE launch_attempts (
  id INTEGER PRIMARY KEY,
  terminal_run_id INTEGER NOT NULL UNIQUE REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  budget_exempt INTEGER NOT NULL CHECK (budget_exempt IN (0,1)),
  counted INTEGER NOT NULL CHECK (counted IN (0,1)),
  state TEXT NOT NULL CHECK (state IN ('reserved','posting','succeeded','failed','aborted','uncertain')),
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE actor_leases (
  id INTEGER PRIMARY KEY,
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  purpose_kind TEXT NOT NULL CHECK (purpose_kind IN ('persistent','work','consultation','review')),
  purpose_id TEXT NOT NULL,
  terminal_run_id INTEGER REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  acquired_at TEXT NOT NULL,
  released_at TEXT
);
CREATE TABLE assignments (
  id INTEGER PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE RESTRICT,
  execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE RESTRICT,
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  terminal_run_id INTEGER NOT NULL REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  state TEXT NOT NULL CHECK (state IN ('open','closed')),
  reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE submissions (
  id INTEGER PRIMARY KEY,
  execution_id INTEGER NOT NULL REFERENCES executions(id) ON DELETE RESTRICT,
  revision INTEGER NOT NULL,
  commit_sha TEXT NOT NULL,
  summary TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('checking','reviewing','awaiting_approval','superseded','accepted')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(execution_id, revision)
);
CREATE TABLE checks (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
  scope TEXT NOT NULL CHECK (scope IN ('submission','integration')),
  target_sha TEXT NOT NULL,
  position INTEGER NOT NULL,
  command TEXT NOT NULL,
  worktree_path TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('queued','running','passed','failed','interrupted')),
  pid INTEGER,
  process_started_at TEXT,
  exit_code INTEGER,
  duration_ms INTEGER,
  stdout_tail TEXT NOT NULL DEFAULT '',
  stderr_tail TEXT NOT NULL DEFAULT '',
  stdout_truncated INTEGER NOT NULL DEFAULT 0 CHECK (stdout_truncated IN (0,1)),
  stderr_truncated INTEGER NOT NULL DEFAULT 0 CHECK (stderr_truncated IN (0,1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(submission_id, scope, target_sha, position)
);
CREATE TABLE reviews (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
  gate TEXT NOT NULL CHECK (gate IN ('research','publishing','coordination')),
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  terminal_run_id INTEGER REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  worktree_path TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('pending','pass','changes_requested','superseded')),
  body TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE blockers (
  id INTEGER PRIMARY KEY,
  work_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
  target_kind TEXT NOT NULL CHECK (target_kind IN ('work','consultation','review','persistent')),
  target_id TEXT NOT NULL,
  terminal_run_id INTEGER REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  kind TEXT NOT NULL,
  reason TEXT NOT NULL,
  requested_role TEXT NOT NULL,
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  resume_state TEXT,
  state TEXT NOT NULL CHECK (state IN ('open','resolved','escalated')),
  resolution TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE approvals (
  id INTEGER PRIMARY KEY,
  submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE RESTRICT,
  state TEXT NOT NULL CHECK (state IN ('pending','accepted','rejected','superseded')),
  feedback TEXT,
  decided_by TEXT REFERENCES actors(slug) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE conversations (
  id INTEGER PRIMARY KEY,
  address TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('channel','work','dm','escalation')),
  work_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE conversation_members (
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  notify INTEGER NOT NULL CHECK (notify IN (0,1)),
  read_through_message_id INTEGER REFERENCES messages(id) ON DELETE RESTRICT,
  PRIMARY KEY(conversation_id, actor_slug)
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
  sender_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  reply_to_id INTEGER REFERENCES messages(id) ON DELETE RESTRICT,
  body TEXT NOT NULL,
  urgency TEXT NOT NULL CHECK (urgency IN ('normal','urgent')),
  created_at TEXT NOT NULL
);
CREATE TABLE deliveries (
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE RESTRICT,
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  terminal_run_id INTEGER REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  state TEXT NOT NULL CHECK (state IN ('pending','acknowledged')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT,
  acknowledged_at TEXT,
  UNIQUE(message_id, actor_slug)
);
CREATE TABLE wake_attempts (
  id INTEGER PRIMARY KEY,
  delivery_id INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE RESTRICT,
  terminal_run_id INTEGER NOT NULL REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  nonce TEXT NOT NULL,
  cao_message_id TEXT,
  result TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE terminal_inputs (
  id INTEGER PRIMARY KEY,
  terminal_run_id INTEGER NOT NULL REFERENCES terminal_runs(id) ON DELETE RESTRICT,
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  body TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','sending','sent','uncertain','failed')),
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE incidents (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  severity TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open','resolved')),
  summary TEXT NOT NULL,
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  actor_slug TEXT NOT NULL REFERENCES actors(slug) ON DELETE RESTRICT,
  kind TEXT NOT NULL,
  entity_kind TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE mutation_requests (
  identity TEXT NOT NULL,
  request_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  entity TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(identity, request_id)
);

CREATE UNIQUE INDEX one_active_terminal_purpose ON terminal_runs(purpose_kind,purpose_id) WHERE state IN ('reserved','creating','live','retained');
CREATE UNIQUE INDEX one_open_assignment_work ON assignments(work_id) WHERE state='open';
CREATE UNIQUE INDEX one_active_execution_work ON executions(work_id) WHERE state IN ('preparing','active');
CREATE UNIQUE INDEX one_nonterminal_consultation ON consultations(work_id,specialty) WHERE state IN ('queued','assigned');
CREATE UNIQUE INDEX one_current_review_gate ON reviews(submission_id,gate) WHERE verdict='pending';
CREATE UNIQUE INDEX one_open_blocker_target ON blockers(target_kind,target_id) WHERE state IN ('open','escalated');
CREATE UNIQUE INDEX one_open_approval_submission ON approvals(submission_id) WHERE state='pending';
CREATE UNIQUE INDEX one_pending_terminal_input ON terminal_inputs(terminal_run_id) WHERE state IN ('pending','sending');
CREATE INDEX pending_delivery_target ON deliveries(actor_slug,terminal_run_id,message_id) WHERE state='pending';
