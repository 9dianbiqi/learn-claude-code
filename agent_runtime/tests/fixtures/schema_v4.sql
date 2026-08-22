PRAGMA foreign_keys = ON;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL,
    checkpoint_id INTEGER,
    last_error TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    phase TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    cursor_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE model_calls (
    model_call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    turn INTEGER NOT NULL,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT,
    stop_reason TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    started_at REAL NOT NULL,
    finished_at REAL,
    error TEXT
);

CREATE TABLE tool_calls (
    tool_call_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    tool_use_id TEXT NOT NULL,
    turn INTEGER NOT NULL,
    name TEXT NOT NULL,
    args_json TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    permission TEXT,
    permission_rule TEXT,
    permission_reason TEXT,
    effect TEXT,
    before_state_json TEXT,
    expected_after_json TEXT,
    output TEXT,
    error TEXT,
    returncode INTEGER,
    stdout TEXT,
    stderr TEXT,
    timed_out INTEGER NOT NULL DEFAULT 0,
    execution_status TEXT,
    execution_attempts INTEGER NOT NULL DEFAULT 0,
    effect_attempts INTEGER NOT NULL DEFAULT 0,
    effect_confirmed INTEGER NOT NULL DEFAULT 0,
    effect_confirmation TEXT,
    effect_key TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    started_at REAL,
    finished_at REAL,
    UNIQUE(task_id, tool_use_id)
);

CREATE TABLE file_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    path TEXT NOT NULL,
    exists_now INTEGER NOT NULL,
    sha256 TEXT,
    identity_json TEXT,
    observed_at REAL NOT NULL,
    source_tool_use_id TEXT,
    UNIQUE(task_id, path)
);

CREATE TABLE events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE leases (
    repo_root TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    owner_id TEXT NOT NULL,
    heartbeat_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    fencing_token INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE effect_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    tool_use_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    fencing_token INTEGER NOT NULL,
    effect TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at REAL NOT NULL,
    deadline_at REAL,
    finished_at REAL,
    details_json TEXT,
    CHECK (state IN ('running', 'completed', 'unknown', 'cancelled'))
);

CREATE INDEX idx_events_task ON events(task_id, event_id);
CREATE INDEX idx_checkpoints_task ON checkpoints(task_id, checkpoint_id);
CREATE INDEX idx_model_calls_task ON model_calls(task_id, model_call_id);
CREATE INDEX idx_tool_calls_task ON tool_calls(task_id, tool_call_row_id);
CREATE INDEX idx_effect_reservations_task_tool
    ON effect_reservations(task_id, tool_use_id, reservation_id);
CREATE INDEX idx_effect_reservations_state
    ON effect_reservations(state, task_id);
CREATE UNIQUE INDEX uq_effect_reservation_running
    ON effect_reservations(task_id, tool_use_id) WHERE state = 'running';

INSERT INTO schema_migrations(version, applied_at) VALUES (4, 0.0);
