PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  split TEXT NOT NULL CHECK (split IN ('training', 'domain')),
  pool_index INTEGER NOT NULL,
  category TEXT NOT NULL,
  source_path TEXT NOT NULL UNIQUE,
  instruction TEXT NOT NULL,
  answer_position TEXT NOT NULL,
  output_key TEXT NOT NULL,
  target_key TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (split, pool_index)
);

CREATE TABLE IF NOT EXISTS assignments (
  email TEXT NOT NULL,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  assignment_order INTEGER NOT NULL,
  PRIMARY KEY (email, task_id),
  UNIQUE (email, assignment_order)
);

CREATE TABLE IF NOT EXISTS audits (
  email TEXT NOT NULL,
  task_id TEXT NOT NULL,
  ground_truth_assessment TEXT NOT NULL
    CHECK (ground_truth_assessment IN ('yes', 'almost', 'no')),
  exact_match_reasonable INTEGER CHECK (exact_match_reasonable IN (0, 1)),
  failure_description TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (email, task_id),
  FOREIGN KEY (email, task_id) REFERENCES assignments(email, task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_assignments_email_order
  ON assignments(email, assignment_order);

CREATE INDEX IF NOT EXISTS idx_audits_task
  ON audits(task_id);
