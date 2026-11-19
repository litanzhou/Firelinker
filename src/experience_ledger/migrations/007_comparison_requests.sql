CREATE TABLE comparison_requests (
 id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, selection_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','completed','error')),
 run_token TEXT NOT NULL, lease_until REAL NOT NULL,
 relation_id TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', prompt_version TEXT NOT NULL,
 tokens INTEGER NOT NULL DEFAULT 0, action_json TEXT, error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
PRAGMA user_version=7;
