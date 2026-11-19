CREATE TABLE file_versions (
    id TEXT PRIMARY KEY, file_id TEXT NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id), version INTEGER NOT NULL,
    filename TEXT NOT NULL, mime_type TEXT NOT NULL, sha256 TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size>0), created_at TEXT NOT NULL,
    UNIQUE(file_id,version), UNIQUE(file_id,sha256)
);
CREATE INDEX file_case ON file_versions(case_id,file_id,version);
CREATE TABLE file_parses (
    id TEXT PRIMARY KEY, file_version_id TEXT NOT NULL REFERENCES file_versions(id),
    parser TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('ready','unsupported','unavailable','failed')),
    error TEXT NOT NULL, anchors_json TEXT NOT NULL, warnings_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX file_parse_version ON file_parses(file_version_id,created_at);
CREATE TABLE file_reference_checks (
    id TEXT PRIMARY KEY, file_version_id TEXT NOT NULL REFERENCES file_versions(id),
    locator_json TEXT NOT NULL, quote TEXT NOT NULL, start INTEGER, end INTEGER,
    actor TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX file_check_version ON file_reference_checks(file_version_id);
PRAGMA user_version=6;
