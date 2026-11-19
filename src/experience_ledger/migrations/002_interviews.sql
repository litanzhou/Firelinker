ALTER TABLE revisions ADD COLUMN content_json TEXT;
ALTER TABLE revisions ADD COLUMN schema_json TEXT;
ALTER TABLE turns ADD COLUMN hypothesis TEXT NOT NULL DEFAULT '';
CREATE TABLE interviews (
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
 decision_id TEXT NOT NULL REFERENCES decisions(id), goal TEXT NOT NULL,
 scope TEXT NOT NULL, intent TEXT NOT NULL DEFAULT 'continue', status TEXT NOT NULL CHECK(status IN ('ready','running','waiting','paused','review','closed','error')),
 revision INTEGER NOT NULL DEFAULT 1, calls INTEGER NOT NULL DEFAULT 0,
 tokens INTEGER NOT NULL DEFAULT 0, limits_json TEXT NOT NULL,
 pending_json TEXT, notice TEXT NOT NULL DEFAULT '',
 lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE interview_gaps (
 id TEXT PRIMARY KEY REFERENCES gaps(id), interview_id TEXT NOT NULL REFERENCES interviews(id),
 key TEXT NOT NULL, label TEXT NOT NULL, required INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','covered','deferred','conflict','not_applicable')),
 reason TEXT NOT NULL, evidence_json TEXT NOT NULL, UNIQUE(interview_id,key)
);
CREATE TABLE interview_questions (
 id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id),
 gap_id TEXT NOT NULL REFERENCES gaps(id), question TEXT NOT NULL,
 hypothesis TEXT NOT NULL, reason TEXT NOT NULL, expected_information TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE interview_inputs (
 interview_id TEXT NOT NULL REFERENCES interviews(id), request_id TEXT NOT NULL,
 payload_hash TEXT NOT NULL, question_id TEXT REFERENCES interview_questions(id),
 turn_id TEXT REFERENCES turns(id), kind TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(interview_id,request_id), UNIQUE(question_id)
);
CREATE TABLE interview_gap_turns (
 gap_id TEXT NOT NULL REFERENCES interview_gaps(id), turn_id TEXT NOT NULL REFERENCES turns(id),
 PRIMARY KEY(gap_id,turn_id)
);
CREATE TABLE interview_actions (
 id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id),
 model TEXT NOT NULL, prompt_version TEXT NOT NULL, context_json TEXT NOT NULL,
 attempts INTEGER NOT NULL, action_json TEXT, usage_json TEXT, outcome_json TEXT,
 error TEXT, created_at TEXT NOT NULL
);
CREATE INDEX interview_case ON interviews(case_id);
PRAGMA user_version=2;
