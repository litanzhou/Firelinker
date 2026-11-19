CREATE TABLE evidence_v3 (
    candidate_id TEXT NOT NULL, revision INTEGER NOT NULL, position INTEGER NOT NULL,
    field TEXT NOT NULL CHECK(length(field) BETWEEN 1 AND 64),
    source_type TEXT NOT NULL CHECK(source_type IN ('turn','document')),
    source_id TEXT NOT NULL, start INTEGER NOT NULL CHECK(start>=0),
    end INTEGER NOT NULL CHECK(end>start), quote TEXT NOT NULL,
    PRIMARY KEY(candidate_id, revision, position),
    FOREIGN KEY(candidate_id, revision) REFERENCES revisions(candidate_id, revision)
);
INSERT INTO evidence_v3 SELECT * FROM evidence ORDER BY rowid;
DROP TABLE evidence;
ALTER TABLE evidence_v3 RENAME TO evidence;
PRAGMA user_version=3;
