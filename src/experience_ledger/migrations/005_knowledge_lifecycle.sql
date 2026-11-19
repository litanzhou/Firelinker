CREATE TABLE candidates_v5 (
    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
    decision_id TEXT REFERENCES decisions(id), current_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
    validation_status TEXT NOT NULL DEFAULT 'unvalidated' CHECK(validation_status='unvalidated'),
    created_at TEXT NOT NULL, domain_id TEXT NOT NULL, domain_json TEXT NOT NULL,
    active_revision INTEGER,
    FOREIGN KEY(id,active_revision) REFERENCES revisions(candidate_id,revision)
);
INSERT INTO candidates_v5 SELECT c.*,a.domain_id,a.domain_json,
    CASE WHEN c.status='approved' THEN c.current_revision ELSE NULL END
    FROM candidates c JOIN cases a ON a.id=c.case_id;
DROP TABLE candidates;
ALTER TABLE candidates_v5 RENAME TO candidates;
CREATE INDEX candidate_case ON candidates(case_id);

CREATE TABLE revision_contexts (
    candidate_id TEXT NOT NULL, revision INTEGER NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id), decision_id TEXT REFERENCES decisions(id),
    FOREIGN KEY(candidate_id,revision) REFERENCES revisions(candidate_id,revision)
);
CREATE UNIQUE INDEX revision_context_scope ON revision_contexts(candidate_id,revision,case_id,COALESCE(decision_id,''));
INSERT INTO revision_contexts(candidate_id,revision,case_id,decision_id)
    SELECT r.candidate_id,r.revision,c.case_id,c.decision_id
    FROM revisions r JOIN candidates c ON c.id=r.candidate_id;

CREATE TABLE experience_feedback (
    id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, revision INTEGER NOT NULL,
    case_id TEXT NOT NULL REFERENCES cases(id), actor TEXT NOT NULL,
    original TEXT NOT NULL, actual_used INTEGER CHECK(actual_used IS NULL OR actual_used IN (0,1)),
    conditions TEXT NOT NULL, actions TEXT NOT NULL, outcome TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(candidate_id,revision) REFERENCES revisions(candidate_id,revision)
);
CREATE INDEX feedback_revision ON experience_feedback(candidate_id,revision,created_at);

CREATE TABLE experience_relations (
    id TEXT PRIMARY KEY, left_id TEXT NOT NULL, left_revision INTEGER NOT NULL,
    right_id TEXT NOT NULL, right_revision INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('duplicate','supplement','conflict')),
    status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','resolved')),
    conditions TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
    actor TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(left_id,left_revision) REFERENCES revisions(candidate_id,revision),
    FOREIGN KEY(right_id,right_revision) REFERENCES revisions(candidate_id,revision),
    CHECK(left_id!=right_id OR left_revision!=right_revision)
);
CREATE INDEX relation_left ON experience_relations(left_id,left_revision);
CREATE INDEX relation_right ON experience_relations(right_id,right_revision);
CREATE TABLE relation_reviews (
    id TEXT PRIMARY KEY, relation_id TEXT NOT NULL REFERENCES experience_relations(id),
    decision TEXT NOT NULL CHECK(decision IN ('accept','reject','resolve')),
    reviewer TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE evidence_v5 (
    candidate_id TEXT NOT NULL, revision INTEGER NOT NULL, position INTEGER NOT NULL,
    field TEXT NOT NULL CHECK(length(field) BETWEEN 1 AND 64),
    source_type TEXT NOT NULL CHECK(source_type IN ('turn','document','feedback','file')),
    source_id TEXT NOT NULL, start INTEGER, end INTEGER, quote TEXT NOT NULL,
    locator_json TEXT,
    PRIMARY KEY(candidate_id,revision,position),
    FOREIGN KEY(candidate_id,revision) REFERENCES revisions(candidate_id,revision),
    CHECK(source_type='file' OR (start>=0 AND end>start AND length(quote)>0)),
    CHECK(source_type='file' OR (start IS NOT NULL AND end IS NOT NULL))
);
INSERT INTO evidence_v5(candidate_id,revision,position,field,source_type,source_id,start,end,quote)
    SELECT candidate_id,revision,position,field,source_type,source_id,start,end,quote FROM evidence ORDER BY rowid;
DROP TABLE evidence;
ALTER TABLE evidence_v5 RENAME TO evidence;
PRAGMA user_version=5;
