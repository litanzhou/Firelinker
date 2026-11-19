"""Application boundary: transactional records, provenance and human review.

No HTTP or model SDK imports. Model output must use the same revision/review path.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from string import Formatter
from uuid import uuid4

from .files import FileMixin
from .knowledge import KnowledgeMixin


FIELDS = ("statement", "scope", "rationale", "exceptions")
GAP_FIELDS = {"action": "statement", "scope": "scope", "rationale": "rationale", "exceptions": "exceptions"}
KINDS = {"conditional", "perceptual", "case_note"}
MAX_TEXT = 1_000_000


class LedgerError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _id():
    return uuid4().hex


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _text(value, name, *, empty=False, limit=MAX_TEXT):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise LedgerError(f"{name} 必须是{'非空' if not empty else ''}字符串")
    if len(value.encode("utf-8")) > limit:
        raise LedgerError(f"{name} 超过 {limit} 字节")
    return value  # Preserve exact original text and Unicode offsets.


class Ledger(KnowledgeMixin, FileMixin):
    def __init__(self, data_dir, domains_dir=None):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blobs = self.data_dir / "blobs"
        self.blobs.mkdir(exist_ok=True)
        self.db_path = self.data_dir / "ledger.sqlite3"
        self.domains_dir = Path(domains_dir) if domains_dir else Path(__file__).parent / "domains"
        self._domains = self._load_domains()
        migrations = {int(path.name.split("_", 1)[0]): path
                      for path in (Path(__file__).parent / "migrations").glob("[0-9][0-9][0-9]_*.sql")}
        target_version = max(migrations, default=1)
        with closing(sqlite3.connect(self.db_path, timeout=15)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            # Table rebuilds preserve original IDs. Disable FK enforcement only
            # for this one atomic migration, then validate before committing.
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in range(target_version + 1):
                raise LedgerError(f"不支持数据库版本 {version}；请使用匹配版本的程序", 409)
            try:
                if version == 0:
                    self._migration_sql(db, Path(__file__).with_name("schema.sql"))
                    version = 1
                for next_version in range(version + 1, target_version + 1):
                    path = migrations.get(next_version)
                    if path is None:
                        raise LedgerError(f"缺少数据库迁移 {next_version}", 409)
                    self._migration_sql(db, path)
                    if db.execute("PRAGMA user_version").fetchone()[0] != next_version:
                        raise LedgerError("数据库迁移版本不一致", 409)
                if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise LedgerError("数据库迁移外键校验失败", 409)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _migration_sql(db, path):
        for statement in path.read_text(encoding="utf-8").split(";"):
            if statement.strip():
                db.execute(statement)

    def _load_domains(self):
        profiles = {}
        for path in sorted(self.domains_dir.glob("*.json")):
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise LedgerError(f"领域配置无法读取：{path.name}") from exc
            if not isinstance(profile, dict):
                raise LedgerError(f"领域配置必须是对象：{path.name}")
            for field in ("id", "name", "version", "description", "object_label"):
                _text(profile.get(field), f"领域 {field}", limit=2000)
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", profile["id"]):
                raise LedgerError("领域 id 必须是小写字母、数字和下划线")
            if profile["id"] in profiles:
                raise LedgerError("领域 id 重复")
            prompts = profile.get("prompts")
            if not isinstance(prompts, dict) or not prompts or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) for key in prompts):
                raise LedgerError("领域 prompts 需要有效的问题类别")
            fields = profile.get("experience_fields", [{"key": f, "label": f, "required": True} for f in FIELDS])
            if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
                raise LedgerError("experience_fields 必须有1至20个字段")
            keys = []
            for field in fields:
                if not isinstance(field, dict) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(field.get("key", ""))):
                    raise LedgerError("经验字段 key 无效")
                _text(field.get("label"), "字段名称", limit=200)
                if type(field.get("required")) is not bool:
                    raise LedgerError("经验字段 required 必须是布尔值")
                keys.append(field["key"])
            if len(set(keys)) != len(keys) or "statement" not in keys:
                raise LedgerError("经验字段需包含 statement 且不得重名")
            profile["experience_fields"] = fields
            for prompt in prompts.values():
                _text(prompt, "追问模板", limit=4000)
                try:
                    parts = list(Formatter().parse(prompt))
                    if any(field not in (None, "decision", "object") or spec or conv for _, field, spec, conv in parts):
                        raise ValueError("unsupported format field")
                    prompt.format(decision="判断", object="对象")
                except (ValueError, KeyError) as exc:
                    raise LedgerError("追问模板仅允许 {decision} 与 {object}") from exc
            profiles[profile["id"]] = profile
        if not profiles:
            raise LedgerError("至少需要一份领域配置")
        return profiles

    @contextmanager
    def _connection(self, *, write=False):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _one(self, db, table, record_id):
        # All table names are constants supplied by application code.
        _text(record_id, "记录 ID", limit=128)
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise LedgerError("记录不存在", 404)
        return dict(row)

    def _case(self, db, case_id):
        case = self._one(db, "cases", case_id)
        case["domain"] = json.loads(case.pop("domain_json"))
        return case

    def _belongs(self, record, case_id):
        if record["case_id"] != case_id:
            raise LedgerError("引用记录不属于当前案例")

    def _event(self, db, case_id, event_type, payload):
        db.execute("INSERT INTO events(case_id,type,payload,created_at) VALUES(?,?,?,?)",
                   (case_id, event_type, _json(payload), _now()))

    def domains(self):
        return json.loads(_json(list(self._domains.values())))

    def create_case(self, domain_id, title, context):
        _text(domain_id, "领域 ID", limit=64)
        _text(title, "案例标题", limit=1000)
        _text(context, "案例背景", empty=True)
        if domain_id not in self._domains:
            raise LedgerError("未知领域")
        case_id = _id()
        with self._connection(write=True) as db:
            db.execute("INSERT INTO cases VALUES(?,?,?,?,?,?)",
                       (case_id, domain_id, title, context, _json(self._domains[domain_id]), _now()))
            self._event(db, case_id, "case.created", {"id": case_id, "domain_id": domain_id})
            return self._case(db, case_id)

    def list_cases(self):
        with self._connection() as db:
            return [self._case(db, r[0]) for r in db.execute("SELECT id FROM cases ORDER BY created_at DESC")]

    def _source(self, db, source_version_id):
        record = self._one(db, "sources", source_version_id)
        path = self.blobs / (record["sha256"] + ".txt")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise LedgerError("资料原文文件缺失；请恢复数据备份", 409) from exc
        if sha256(raw).hexdigest() != record["sha256"] or raw != record["text"].encode("utf-8"):
            raise LedgerError("资料原文完整性校验失败", 409)
        return record

    def add_source(self, case_id, title, text, source_id=None):
        _text(title, "资料标题", limit=1000)
        _text(text, "资料正文")
        digest = sha256(text.encode("utf-8")).hexdigest()
        with self._connection(write=True) as db:
            self._case(db, case_id)
            version = 1
            if source_id is not None:
                _text(source_id, "资料 ID", limit=128)
                old = db.execute("SELECT * FROM sources WHERE source_id=? ORDER BY version DESC", (source_id,)).fetchall()
                if not old:
                    raise LedgerError("需要更新的资料不存在", 404)
                self._belongs(dict(old[0]), case_id)
                for item in old:
                    if item["sha256"] == digest:
                        return self._source(db, item["id"])
                version = old[0]["version"] + 1
            else:
                source_id = _id()
            path = self.blobs / (digest + ".txt")
            # A complete blob is installed atomically before its metadata is committed.
            # A crash can leave an unreferenced blob, never a half-written referenced one.
            if path.exists():
                if path.read_bytes() != text.encode("utf-8"):
                    raise LedgerError("资料文件校验失败", 409)
            else:
                temporary = self.blobs / (_id() + ".tmp")
                try:
                    temporary.write_bytes(text.encode("utf-8"))
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            record_id = _id()
            db.execute("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?)",
                       (record_id, source_id, case_id, version, title, digest, text, _now()))
            self._event(db, case_id, "source.added", {"id": record_id, "source_id": source_id, "version": version, "sha256": digest})
            return self._source(db, record_id)

    def add_decision(self, case_id, title, description):
        _text(title, "判断标题", limit=1000)
        _text(description, "判断说明", empty=True)
        with self._connection(write=True) as db:
            case = self._case(db, case_id)
            record_id = _id()
            db.execute("INSERT INTO decisions VALUES(?,?,?,?,?)", (record_id, case_id, title, description, _now()))
            for kind, prompt in case["domain"]["prompts"].items():
                question = prompt.format(decision=title, object=case["domain"]["object_label"])
                db.execute("INSERT INTO gaps(id,case_id,decision_id,kind,question,status) VALUES(?,?,?,?,?,?)",
                           (_id(), case_id, record_id, kind, question, "open"))
            self._event(db, case_id, "decision.created", {"id": record_id})
            return self._one(db, "decisions", record_id)

    def add_turn(self, case_id, gap_id, answer, speaker, question=None):
        _text(answer, "专家回答")
        _text(speaker, "记录的发言人", limit=200)
        with self._connection(write=True) as db:
            gap = self._one(db, "gaps", gap_id)
            self._belongs(gap, case_id)
            question = gap["question"] if question is None else _text(question, "问题")
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE case_id=?", (case_id,)).fetchone()[0]
            record_id = _id()
            db.execute("INSERT INTO turns(id,case_id,gap_id,decision_id,seq,kind,question,answer,speaker,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (record_id, case_id, gap_id, gap["decision_id"], seq, gap["kind"], question, answer, speaker, _now()))
            db.execute("INSERT INTO turn_contexts(id,turn_id,start,end,case_id,decision_id,gap_id,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (_id(), record_id, 0, len(answer), case_id, gap["decision_id"], gap_id, speaker, _now()))
            db.execute("UPDATE gaps SET status='answered' WHERE id=?", (gap_id,))
            self._event(db, case_id, "turn.recorded", {"id": record_id, "gap_id": gap_id, "seq": seq})
            return self._one(db, "turns", record_id)

    def _validate_evidence(self, db, case_id, evidence, fields=FIELDS, *, contexts=None):
        if not isinstance(evidence, list) or len(evidence) > 100:
            raise LedgerError("证据必须是数组，最多100项")
        contexts = self._normalize_contexts(db, [{"case_id": case_id, "decision_id": None}] if contexts is None else contexts)
        allowed_cases = {context["case_id"] for context in contexts}
        result = []
        for item in evidence:
            if not isinstance(item, dict):
                raise LedgerError("证据条目必须是对象")
            field, source_type = item.get("field"), item.get("source_type")
            if not isinstance(field, str) or field not in fields or source_type not in ("turn", "document", "feedback", "file"):
                raise LedgerError("证据字段或来源类型无效")
            if source_type == "file":
                result.append(self._validate_file_reference(db, item, allowed_cases))
                continue
            if source_type == "document":
                source = self._source(db, item.get("source_id"))
                original = source["text"]
            elif source_type == "feedback":
                source = self._one(db, "experience_feedback", item.get("source_id"))
                original = source["original"]
            else:
                source = self._one(db, "turns", item.get("source_id"))
                original = source["answer"]
            start, end, quote = item.get("start"), item.get("end"), item.get("quote")
            if type(start) is not int or type(end) is not int or not (0 <= start < end <= len(original)):
                raise LedgerError("证据起止位置越界或无效（使用 Unicode 码点）")
            _text(quote, "证据原文")
            if original[start:end] != quote:
                raise LedgerError("证据引文与指定位置的原文不一致")
            if source_type == "turn":
                links = db.execute("SELECT * FROM turn_contexts WHERE turn_id=? AND start<=? AND end>=?",
                                   (source["id"], start, end)).fetchall()
                if not any(link["case_id"] == context["case_id"] and
                           (context["decision_id"] is None or link["decision_id"] == context["decision_id"])
                           for link in links for context in contexts):
                    raise LedgerError("引用原话片段未关联到本修订允许的案例或判断范围")
            elif source["case_id"] not in allowed_cases:
                raise LedgerError("引用记录不属于本修订关联的案例范围")
            result.append({"field": field, "source_type": source_type, "source_id": source["id"], "start": start, "end": end, "quote": quote})
        return result

    def _insert_revision(self, db, candidate_id, revision, values, evidence, actor, contexts=None):
        db.execute("INSERT INTO revisions(candidate_id,revision,title,kind,statement,scope,rationale,exceptions,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (candidate_id, revision, values["title"], values["kind"], *(values[f] for f in FIELDS), actor, _now()))
        for position, item in enumerate(evidence):
            db.execute("INSERT INTO evidence(candidate_id,revision,position,field,source_type,source_id,start,end,quote,locator_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (candidate_id, revision, position, item["field"], item["source_type"], item["source_id"],
                        item.get("start"), item.get("end"), item.get("quote", ""),
                        _json(item["locator"]) if item.get("locator") is not None else None))
        self._save_revision_contexts(db, candidate_id, revision, contexts)

    def draft_candidate(self, case_id, decision_id):
        with self._connection(write=True) as db:
            decision = self._one(db, "decisions", decision_id)
            self._belongs(decision, case_id)
            case = self._case(db, case_id)
            turns = db.execute("SELECT * FROM turns WHERE decision_id=? ORDER BY seq", (decision_id,)).fetchall()
            if not turns:
                raise LedgerError("请先记录访谈回答")
            latest = {turn["kind"]: dict(turn) for turn in turns}
            if not (set(latest) & set(GAP_FIELDS)):
                raise LedgerError("这些记录使用动态访谈字段，请回到智能访谈整理候选经验")
            values = {"title": decision["title"], "kind": "conditional", **{f: "" for f in FIELDS}}
            evidence = []
            for kind, field in GAP_FIELDS.items():
                if kind in latest:
                    turn = latest[kind]
                    values[field] = turn["answer"]
                    evidence.append({"field": field, "source_type": "turn", "source_id": turn["id"], "start": 0, "end": len(turn["answer"]), "quote": turn["answer"]})
            candidate_id = _id()
            db.execute("INSERT INTO candidates(id,case_id,decision_id,current_revision,status,validation_status,created_at,domain_id,domain_json) VALUES(?,?,?,?,?,?,?,?,?)",
                       (candidate_id, case_id, decision_id, 1, "pending", "unvalidated", _now(), case["domain_id"], _json(case["domain"])))
            self._insert_revision(db, candidate_id, 1, values, evidence, "system:verbatim-draft-v1")
            self._event(db, case_id, "candidate.drafted", {"id": candidate_id, "revision": 1, "method": "verbatim-draft-v1"})
            return self._candidate(db, candidate_id)

    def _expected(self, candidate, expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise LedgerError("expected_revision 必须是正整数")
        if candidate["current_revision"] != expected_revision:
            raise LedgerError("此经验已被修改，请刷新后重新审核或编辑", 409)

    def revise_candidate(self, candidate_id, expected_revision, title, kind, statement, scope, rationale, exceptions, evidence, actor, contexts=None):
        _text(title, "经验标题", limit=1000)
        _text(kind, "经验类型", limit=100)
        if kind not in KINDS:
            raise LedgerError("未知经验类型")
        _text(actor, "修订人", limit=200)
        values = dict(title=title, kind=kind, statement=statement, scope=scope, rationale=rationale, exceptions=exceptions)
        for field in FIELDS:
            _text(values[field], field, empty=True)
        with self._connection(write=True) as db:
            candidate = self._one(db, "candidates", candidate_id)
            self._expected(candidate, expected_revision)
            current = db.execute("SELECT content_json FROM revisions WHERE candidate_id=? AND revision=?", (candidate_id, expected_revision)).fetchone()
            if current[0] is not None:
                raise LedgerError("此经验使用领域字段，请通过领域内容修订接口编辑")
            revision = expected_revision + 1
            contexts = self._contexts_for_revision(db, candidate_id, revision, contexts)
            evidence = self._validate_evidence(db, candidate["case_id"], evidence, contexts=contexts)
            self._insert_revision(db, candidate_id, revision, values, evidence, actor, contexts)
            db.execute("UPDATE candidates SET current_revision=?, status='pending' WHERE id=?", (revision, candidate_id))
            self._event(db, candidate["case_id"], "candidate.revised", {"id": candidate_id, "revision": revision, "actor": actor})
            return self._candidate(db, candidate_id)

    def review_candidate(self, candidate_id, expected_revision, decision, reviewer, note):
        if decision not in ("approve", "reject"):
            raise LedgerError("审核决定必须是 approve 或 reject")
        _text(reviewer, "审核人", limit=200)
        _text(note, "审核说明", limit=10000)
        with self._connection(write=True) as db:
            record = self._one(db, "candidates", candidate_id)
            self._expected(record, expected_revision)
            if record["status"] != "pending":
                raise LedgerError("此修订已有审核决定；需先修订才能再次审核", 409)
            candidate = self._candidate(db, candidate_id)
            if decision == "approve":
                if candidate["structured"]:
                    evidence = self._content(db, record["case_id"], candidate["content"], candidate["content_schema"], candidate["evidence"],
                                             approve=True, contexts=candidate["contexts"])
                else:
                    evidence = self._validate_evidence(db, record["case_id"], candidate["evidence"], contexts=candidate["contexts"])
                    for field in FIELDS:
                        _text(candidate[field], f"确认前需填写 {field}")
                        if not any(e["field"] == field for e in evidence):
                            raise LedgerError(f"确认前需为 {field} 提供可定位的证据")
                if any(ref["source_type"] == "file" and ref.get("locator", {}).get("verification") == "pending_manual"
                       for ref in [*candidate["evidence"], *evidence]):
                    raise LedgerError("文件引用仍待人工对照原件确认；请先确认位置和引文，再保存含确认记录的新修订")
            review_id = _id()
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)",
                       (review_id, candidate_id, expected_revision, decision, reviewer, note, _now()))
            db.execute("UPDATE candidates SET status=? WHERE id=?", ("approved" if decision == "approve" else "rejected", candidate_id))
            if decision == "approve":
                db.execute("UPDATE candidates SET active_revision=? WHERE id=?", (expected_revision, candidate_id))
            self._event(db, record["case_id"], "candidate.reviewed", {"id": candidate_id, "revision": expected_revision, "review_id": review_id, "decision": decision, "reviewer": reviewer})
            return self._candidate(db, candidate_id)

    def _candidate(self, db, candidate_id, revision=None, *, include_history=True):
        candidate = self._one(db, "candidates", candidate_id)
        domain = json.loads(candidate.pop("domain_json"))
        candidate["domain_name"] = domain["name"]
        latest_revision = candidate.pop("current_revision")
        candidate["latest_revision"] = latest_revision
        candidate["latest_status"] = candidate["status"]
        revision = latest_revision if revision is None else revision
        self._revision_record(db, candidate_id, revision)
        revisions = [dict(r) for r in db.execute("SELECT * FROM revisions WHERE candidate_id=? AND (? OR revision=?) ORDER BY revision",
                                                (candidate_id, include_history, revision))]
        for item in revisions:
            item["structured"] = item["content_json"] is not None
            item["content"] = json.loads(item.pop("content_json")) if item["structured"] else [dict(key=f, text=item[f], status="supported" if item[f] else "unknown") for f in FIELDS]
            item["content_schema"] = json.loads(item.pop("schema_json")) if item["structured"] else [dict(key=f, label=f, required=True) for f in FIELDS]
            if not item["structured"]:
                item.pop("schema_json", None)
                item.pop("content_json", None)
            item["contexts"] = self._revision_contexts(db, candidate_id, item["revision"])
            item["evidence"] = []
            for row in db.execute("SELECT * FROM evidence WHERE candidate_id=? AND revision=? ORDER BY position", (candidate_id, item["revision"])):
                evidence = dict(row)
                for key in ("candidate_id", "revision", "position"):
                    evidence.pop(key)
                locator_json = evidence.pop("locator_json")
                if locator_json is not None:
                    evidence["locator"] = json.loads(locator_json)
                if evidence["source_type"] == "document":
                    source = self._source(db, evidence["source_id"])
                    evidence["source_title"] = f"{source['title']} · v{source['version']}"
                    evidence["source_text"] = source["text"]
                elif evidence["source_type"] == "turn":
                    source = self._one(db, "turns", evidence["source_id"])
                    evidence["source_title"] = f"访谈 #{source['seq']} · {source['speaker']}"
                    evidence["source_text"] = source["answer"]
                    evidence["hypothesis"] = source.get("hypothesis", "")
                elif evidence["source_type"] == "feedback":
                    source = self._one(db, "experience_feedback", evidence["source_id"])
                    evidence["source_title"] = f"使用反馈 · {source['actor']} · 经验v{source['revision']}"
                    evidence["source_text"] = source["original"]
                else:
                    source = self._file(db, evidence["source_id"])
                    evidence["source_title"] = f"{source['filename']} · v{source['version']}"
                    evidence["source_text"] = self._file_reference_text(db, evidence)
                    evidence["requires_visual_review"] = evidence.get("start") is None
                evidence["source_case_id"] = source["case_id"]
                if not include_history and evidence["source_type"] == "turn":
                    evidence["source_text"] = evidence["quote"]
                    evidence["source_excerpt_start"] = evidence["start"]
                item["evidence"].append(evidence)
        current = next(r for r in revisions if r["revision"] == revision)
        candidate.update({k: v for k, v in current.items() if k not in ("candidate_id", "created_at")})
        candidate["is_active_revision"] = candidate["active_revision"] == revision
        review = db.execute("SELECT decision FROM reviews WHERE candidate_id=? AND revision=?", (candidate_id, revision)).fetchone()
        candidate["status"] = {"approve": "approved", "reject": "rejected"}.get(review[0], "pending") if review else "pending"
        candidate["revisions"] = revisions
        candidate["reviews"] = [dict(r) for r in db.execute("SELECT * FROM reviews WHERE candidate_id=? AND (? OR revision=?) ORDER BY revision",
                                                           (candidate_id, include_history, revision))]
        relations = self._relations(db, candidate_id)
        for relation in relations:
            relation["applies_to_selected_revision"] = any(relation[side + "_id"] == candidate_id and relation[side + "_revision"] == revision
                                                             for side in ("left", "right"))
        if not include_history:
            relations = [r for r in relations if r["status"] in ("accepted", "resolved") and r["applies_to_selected_revision"] and
                         all(self._approved_revision(db, r[side + "_id"], r[side + "_revision"]) for side in ("left", "right"))]
        candidate["relations"] = relations
        candidate["feedback"] = self._feedback_list(db, candidate_id, revision)
        if include_history:
            candidate["feedback_history"] = self._feedback_list(db, candidate_id)
        return candidate

    def get_candidate(self, candidate_id, revision=None, *, include_history=True):
        with self._connection() as db:
            return self._candidate(db, candidate_id, revision, include_history=include_history)

    def get_case(self, case_id):
        with self._connection() as db:
            case = self._case(db, case_id)
            case["sources"] = [self._source(db, row[0]) for row in db.execute("SELECT id FROM sources WHERE case_id=? ORDER BY created_at", (case_id,))]
            for table, order in (("decisions", "created_at"), ("gaps", "rowid"), ("events", "id")):
                case[table] = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE case_id=? ORDER BY {order}", (case_id,))]
            case["turns"] = [dict(r) for r in db.execute("""SELECT DISTINCT t.* FROM turns t LEFT JOIN turn_contexts tc ON tc.turn_id=t.id
                WHERE t.case_id=? OR tc.case_id=? ORDER BY t.created_at,t.seq""", (case_id, case_id))]
            for turn in case["turns"]:
                turn["contexts"] = [dict(r) for r in db.execute("SELECT * FROM turn_contexts WHERE turn_id=? ORDER BY rowid", (turn["id"],))]
            for event in case["events"]:
                event["payload"] = json.loads(event["payload"])
            case["candidates"] = [self._candidate(db, row[0]) for row in db.execute("""SELECT DISTINCT c.id,c.created_at FROM candidates c
                JOIN revision_contexts rc ON rc.candidate_id=c.id WHERE rc.case_id=? ORDER BY c.created_at""", (case_id,))]
            case["interview_ids"] = [row[0] for row in db.execute("""SELECT DISTINCT i.id,i.created_at FROM interviews i
                LEFT JOIN interview_cases ic ON ic.interview_id=i.id WHERE i.case_id=? OR ic.case_id=? ORDER BY i.created_at""", (case_id, case_id))]
            case["files"] = self.list_file_metadata(db, case_id)
            case["feedback"] = [self._feedback(db, row[0]) for row in db.execute("SELECT id FROM experience_feedback WHERE case_id=? ORDER BY created_at,id", (case_id,))]
            return case

    def search_knowledge(self, query="", domain_id=None):
        _text(query, "检索词", empty=True, limit=1000)
        if domain_id is not None:
            _text(domain_id, "领域 ID", limit=64)
        with self._connection() as db:
            rows = db.execute("""SELECT c.id FROM candidates c
                JOIN revisions r ON r.candidate_id=c.id AND r.revision=c.active_revision
                WHERE c.active_revision IS NOT NULL AND (:domain IS NULL OR c.domain_id=:domain)
                AND (instr(lower(r.title), lower(:q)) > 0
                    OR EXISTS (SELECT 1 FROM json_each(r.content_json) AS f
                               WHERE instr(lower(json_extract(f.value,'$.text')), lower(:q)) > 0)
                    OR (r.content_json IS NULL AND (
                        instr(lower(r.statement), lower(:q)) > 0 OR instr(lower(r.scope), lower(:q)) > 0
                        OR instr(lower(r.rationale), lower(:q)) > 0 OR instr(lower(r.exceptions), lower(:q)) > 0)))
                ORDER BY c.created_at DESC""", {"domain": domain_id, "q": query.strip()}).fetchall()
            return [self._candidate(db, row[0], self._one(db, "candidates", row[0])["active_revision"], include_history=False) for row in rows]

    def _content(self, db, case_id, content, schema, evidence, *, approve=False, contexts=None):
        if not isinstance(content, list) or not all(isinstance(v, dict) for v in content):
            raise LedgerError("经验内容必须是字段数组")
        allowed = {field["key"] for field in schema}
        keys = [v.get("key") for v in content]
        if any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys) or not set(keys) <= allowed:
            raise LedgerError("经验字段未在领域结构中定义或重复")
        for value in content:
            if set(value) != {"key", "text", "status"}:
                raise LedgerError("经验字段必须且仅包含 key/text/status")
            _text(value.get("text"), "经验内容", empty=not approve)
            if value.get("status") not in ("supported", "unknown", "hypothesis"):
                raise LedgerError("字段状态必须是 supported/unknown/hypothesis")
        evidence = self._validate_evidence(db, case_id, evidence, allowed, contexts=contexts)
        if approve:
            for field in schema:
                if field["required"] and field["key"] not in keys:
                    raise LedgerError(f"缺少必需字段 {field['label']}")
            for value in content:
                if value["status"] != "unknown" and not any(e["field"] == value["key"] for e in evidence):
                    raise LedgerError(f"字段 {value['key']} 需要原文依据")
            statement = next((v for v in content if v["key"] == "statement"), None)
            if statement is None or statement["status"] == "unknown":
                raise LedgerError("经验表述本身需要原文依据")
        return evidence

    def _insert_content_revision(self, db, candidate_id, revision, case_id, title, kind, content, schema, evidence, actor, contexts=None):
        _text(title, "经验标题", limit=1000)
        if not isinstance(kind, str) or kind not in KINDS:
            raise LedgerError("未知经验类型")
        _text(actor, "修订人", limit=200)
        contexts = self._contexts_for_revision(db, candidate_id, revision, contexts)
        evidence = self._content(db, case_id, content, schema, evidence, contexts=contexts)
        legacy = {v["key"]: v["text"] for v in content}
        values = {"title": title, "kind": kind, **{f: legacy.get(f, "") for f in FIELDS}}
        self._insert_revision(db, candidate_id, revision, values, evidence, actor, contexts)
        db.execute("UPDATE revisions SET content_json=?,schema_json=? WHERE candidate_id=? AND revision=?",
                   (_json(content), _json(schema), candidate_id, revision))

    def revise_content(self, candidate_id, expected_revision, title, kind, content, evidence, actor, contexts=None):
        with self._connection(write=True) as db:
            candidate = self._one(db, "candidates", candidate_id)
            self._expected(candidate, expected_revision)
            current = self._candidate(db, candidate_id)
            if not current["structured"]:
                raise LedgerError("旧版经验请使用原有修订接口")
            self._insert_content_revision(db, candidate_id, expected_revision + 1, candidate["case_id"], title, kind,
                                          content, current["content_schema"], evidence, actor, contexts)
            db.execute("UPDATE candidates SET current_revision=?,status='pending' WHERE id=?", (expected_revision + 1, candidate_id))
            self._event(db, candidate["case_id"], "candidate.revised", {"id": candidate_id, "revision": expected_revision + 1, "actor": actor})
            return self._candidate(db, candidate_id)
