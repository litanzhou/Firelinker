CREATE TABLE interviews_v4 (
 id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), decision_id TEXT REFERENCES decisions(id),
 goal TEXT NOT NULL, scope TEXT NOT NULL, intent TEXT NOT NULL DEFAULT 'continue',
 status TEXT NOT NULL CHECK(status IN ('ready','running','waiting','paused','review','closed','error')),
 revision INTEGER NOT NULL DEFAULT 1, calls INTEGER NOT NULL DEFAULT 0,
 tokens INTEGER NOT NULL DEFAULT 0, limits_json TEXT NOT NULL, pending_json TEXT,
 notice TEXT NOT NULL DEFAULT '', lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, domain_json TEXT NOT NULL,
 start_json TEXT NOT NULL DEFAULT '{}', context_epoch INTEGER NOT NULL DEFAULT 0
);
INSERT INTO interviews_v4 SELECT i.*,c.domain_json,'{}',0 FROM interviews i JOIN cases c ON c.id=i.case_id;
DROP TABLE interviews;
ALTER TABLE interviews_v4 RENAME TO interviews;
CREATE INDEX interview_case ON interviews(case_id);
CREATE TABLE interview_cases (
 interview_id TEXT NOT NULL REFERENCES interviews(id), case_id TEXT NOT NULL REFERENCES cases(id),
 actor TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(interview_id,case_id)
);
INSERT INTO interview_cases SELECT id,case_id,'migration:v4',created_at FROM interviews WHERE case_id IS NOT NULL;
CREATE TABLE gaps_v4 (
 id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), decision_id TEXT REFERENCES decisions(id),
 kind TEXT NOT NULL, question TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','answered','covered','deferred','conflict','not_applicable')),
 required INTEGER NOT NULL DEFAULT 1, reason TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '[]',
 UNIQUE(decision_id,kind)
);
INSERT INTO gaps_v4 SELECT g.id,g.case_id,g.decision_id,g.kind,g.question,
 g.status,COALESCE(i.required,1),COALESCE(i.reason,''),COALESCE(i.evidence_json,'[]')
 FROM gaps g LEFT JOIN interview_gaps i ON i.id=g.id;
DROP TABLE gaps;
ALTER TABLE gaps_v4 RENAME TO gaps;
CREATE TABLE interview_gaps_v4 (
 id TEXT NOT NULL REFERENCES gaps(id), interview_id TEXT NOT NULL REFERENCES interviews(id),
 key TEXT NOT NULL, label TEXT NOT NULL, required INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','covered','deferred','conflict','not_applicable')),
 reason TEXT NOT NULL, evidence_json TEXT NOT NULL, decision_id TEXT REFERENCES decisions(id),
 PRIMARY KEY(interview_id,id), UNIQUE(interview_id,decision_id,key)
);
INSERT INTO interview_gaps_v4 SELECT i.*,g.decision_id FROM interview_gaps i JOIN gaps g ON g.id=i.id;
DROP TABLE interview_gaps;
ALTER TABLE interview_gaps_v4 RENAME TO interview_gaps;
CREATE TABLE turns_v4 (
 id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), gap_id TEXT REFERENCES gaps(id),
 decision_id TEXT REFERENCES decisions(id), seq INTEGER NOT NULL, kind TEXT NOT NULL,
 question TEXT NOT NULL, answer TEXT NOT NULL, speaker TEXT NOT NULL, created_at TEXT NOT NULL,
 hypothesis TEXT NOT NULL DEFAULT '', interview_id TEXT REFERENCES interviews(id),
 interview_seq INTEGER, correction_of TEXT REFERENCES turns(id),
 UNIQUE(case_id,seq), UNIQUE(interview_id,interview_seq)
);
INSERT INTO turns_v4 SELECT t.*,
 (SELECT x.interview_id FROM interview_inputs x WHERE x.turn_id=t.id LIMIT 1),
 CASE WHEN EXISTS(SELECT 1 FROM interview_inputs x WHERE x.turn_id=t.id) THEN t.seq ELSE NULL END,NULL FROM turns t;
DROP TABLE turns;
ALTER TABLE turns_v4 RENAME TO turns;
CREATE INDEX turn_case ON turns(case_id,seq);
CREATE INDEX turn_interview ON turns(interview_id,interview_seq);
CREATE TABLE turn_contexts (
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), start INTEGER NOT NULL CHECK(start>=0),
 end INTEGER NOT NULL CHECK(end>start), case_id TEXT NOT NULL REFERENCES cases(id),
 decision_id TEXT REFERENCES decisions(id), gap_id TEXT REFERENCES gaps(id), actor TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(turn_id,start,end,case_id,decision_id,gap_id)
);
INSERT INTO turn_contexts SELECT lower(hex(randomblob(16))),id,0,length(answer),case_id,decision_id,gap_id,
 'migration:v4',created_at FROM turns WHERE case_id IS NOT NULL AND length(answer)>0;
CREATE INDEX turn_context_case ON turn_contexts(case_id,decision_id,turn_id);
CREATE TABLE interview_questions_v4 (
 id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id), gap_id TEXT REFERENCES gaps(id),
 question TEXT NOT NULL, hypothesis TEXT NOT NULL, reason TEXT NOT NULL,
 expected_information TEXT NOT NULL, created_at TEXT NOT NULL,
 case_id TEXT REFERENCES cases(id), decision_id TEXT REFERENCES decisions(id), quality_json TEXT NOT NULL DEFAULT '[]'
);
INSERT INTO interview_questions_v4 SELECT q.*,g.case_id,g.decision_id,'[]' FROM interview_questions q LEFT JOIN gaps g ON g.id=q.gap_id;
DROP TABLE interview_questions;
ALTER TABLE interview_questions_v4 RENAME TO interview_questions;
CREATE TABLE interview_gap_turns_v4 (
 gap_id TEXT NOT NULL REFERENCES gaps(id), turn_id TEXT NOT NULL REFERENCES turns(id), PRIMARY KEY(gap_id,turn_id)
);
INSERT INTO interview_gap_turns_v4 SELECT * FROM interview_gap_turns;
DROP TABLE interview_gap_turns;
ALTER TABLE interview_gap_turns_v4 RENAME TO interview_gap_turns;
CREATE TABLE interview_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, interview_id TEXT NOT NULL REFERENCES interviews(id),
 type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE focus_proposals (
 id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id), case_id TEXT NOT NULL REFERENCES cases(id),
 decision_id TEXT REFERENCES decisions(id), summary TEXT NOT NULL, remaining_json TEXT NOT NULL,
 reason TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected')), created_at TEXT NOT NULL
);
CREATE TABLE interview_quality_feedback (
 id TEXT PRIMARY KEY, interview_id TEXT NOT NULL REFERENCES interviews(id),
 question_id TEXT NOT NULL REFERENCES interview_questions(id), kind TEXT NOT NULL,
 note TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL
);
PRAGMA user_version=4;
