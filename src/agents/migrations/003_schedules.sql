CREATE TABLE schedule_states (
  slug TEXT PRIMARY KEY,
  config_hash TEXT NOT NULL,
  next_run_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE schedule_runs (
  id INTEGER PRIMARY KEY,
  schedule_slug TEXT NOT NULL,
  scheduled_for TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('posted','created','skipped')),
  message_id INTEGER REFERENCES messages(id) ON DELETE RESTRICT,
  work_id TEXT REFERENCES work_items(id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  UNIQUE(schedule_slug, scheduled_for)
);
