CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY, domain_id TEXT NOT NULL, title TEXT NOT NULL,
    context TEXT NOT NULL, domain_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, case_id TEXT NOT NULL REFERENCES cases(id),
    version INTEGER NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
    text TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(source_id, version), UNIQUE(source_id, sha256)
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
    title TEXT NOT NULL, description TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gaps (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
    decision_id TEXT NOT NULL REFERENCES decisions(id), kind TEXT NOT NULL,
    question TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','answered')),
    UNIQUE(decision_id, kind)
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
    gap_id TEXT NOT NULL REFERENCES gaps(id), decision_id TEXT NOT NULL REFERENCES decisions(id),
    seq INTEGER NOT NULL, kind TEXT NOT NULL, question TEXT NOT NULL,
    answer TEXT NOT NULL, speaker TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(case_id, seq)
);
CREATE TABLE IF NOT EXISTS candidates (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
    decision_id TEXT NOT NULL REFERENCES decisions(id), current_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
    validation_status TEXT NOT NULL DEFAULT 'unvalidated' CHECK(validation_status='unvalidated'),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    candidate_id TEXT NOT NULL REFERENCES candidates(id), revision INTEGER NOT NULL,
    title TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('conditional','perceptual','case_note')),
    statement TEXT NOT NULL, scope TEXT NOT NULL, rationale TEXT NOT NULL,
    exceptions TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, revision)
);
CREATE TABLE IF NOT EXISTS evidence (
    candidate_id TEXT NOT NULL, revision INTEGER NOT NULL, position INTEGER NOT NULL,
    field TEXT NOT NULL CHECK(field IN ('statement','scope','rationale','exceptions')),
    source_type TEXT NOT NULL CHECK(source_type IN ('turn','document')),
    source_id TEXT NOT NULL, start INTEGER NOT NULL CHECK(start>=0),
    end INTEGER NOT NULL CHECK(end>start), quote TEXT NOT NULL,
    PRIMARY KEY(candidate_id, revision, position),
    FOREIGN KEY(candidate_id, revision) REFERENCES revisions(candidate_id, revision)
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, revision INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    reviewer TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(candidate_id, revision),
    FOREIGN KEY(candidate_id, revision) REFERENCES revisions(candidate_id, revision)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL REFERENCES cases(id),
    type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_case ON sources(case_id);
CREATE INDEX IF NOT EXISTS turn_case ON turns(case_id, seq);
CREATE INDEX IF NOT EXISTS candidate_case ON candidates(case_id);
CREATE INDEX IF NOT EXISTS event_case ON events(case_id, id);
PRAGMA user_version=1;
