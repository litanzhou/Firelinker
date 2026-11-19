"""Transactional interview records; no model or workflow framework dependency."""

from hashlib import sha256
import json
import re
import time

from ..service import LedgerError, _id, _json, _now


class InterviewStore:
    def __init__(self, ledger):
        self.ledger = ledger

    def _get(self, db, interview_id):
        item = self.ledger._one(db, "interviews", interview_id)
        item["limits"] = json.loads(item.pop("limits_json"))
        item["pending"] = json.loads(item.pop("pending_json") or "null")
        item["domain"] = json.loads(item["domain_json"])
        return item

    def _event(self, db, item, kind, payload):
        db.execute("INSERT INTO interview_events(interview_id,type,payload,created_at) VALUES(?,?,?,?)",
                   (item["id"], kind, _json(payload), _now()))
        if item.get("case_id"):
            self.ledger._event(db, item["case_id"], kind, payload)

    def _scope(self, db, sid, case_id, decision_id=None):
        if not case_id or not db.execute("SELECT 1 FROM interview_cases WHERE interview_id=? AND case_id=?", (sid, case_id)).fetchone():
            raise LedgerError("案例尚未由用户关联到本次访谈")
        if decision_id:
            decision = self.ledger._one(db, "decisions", decision_id)
            self.ledger._belongs(decision, case_id)

    def _link_case(self, db, item, case_id, actor):
        self.ledger._case(db, case_id)
        db.execute("INSERT OR IGNORE INTO interview_cases VALUES(?,?,?,?)", (item["id"], case_id, actor, _now()))

    def _readable_turn(self, db, item, turn_id):
        turn = self.ledger._one(db, "turns", turn_id)
        if turn["interview_id"] != item["id"] and not db.execute(
                "SELECT 1 FROM turn_contexts t JOIN interview_cases c ON c.case_id=t.case_id WHERE t.turn_id=? AND c.interview_id=?",
                (turn_id, item["id"])).fetchone():
            raise LedgerError("原话不属于本次访谈或已关联的案例片段")
        return turn

    def _link_turn(self, db, item, turn_id, start, end, case_id, decision_id=None, gap_id=None, actor="system:interviewer"):
        self._scope(db, item["id"], case_id, decision_id)
        turn = self._readable_turn(db, item, turn_id)
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(turn["answer"]):
            raise LedgerError("原话片段位置无效，使用Unicode码点")
        if not any(a <= start < end <= b for a, b in self._turn_ranges(db, item, turn)):
            raise LedgerError("不能把未获读取权限的原话范围关联到新案例")
        if gap_id:
            gap = self.ledger._one(db, "gaps", gap_id)
            if gap["case_id"] != case_id or (decision_id and gap["decision_id"] != decision_id):
                raise LedgerError("缺口与关联的案例或判断不同")
        old = db.execute("SELECT id FROM turn_contexts WHERE turn_id=? AND start=? AND end=? AND case_id=? AND decision_id IS ? AND gap_id IS ?",
                         (turn_id, start, end, case_id, decision_id, gap_id)).fetchone()
        context_id = old[0] if old else _id()
        if not old:
            db.execute("INSERT INTO turn_contexts VALUES(?,?,?,?,?,?,?,?,?)",
                       (context_id, turn_id, start, end, case_id, decision_id, gap_id, actor, _now()))
        return context_id

    def start(self, request):
        sid = sha256(("interview:" + request.request_id).encode()).hexdigest()[:32]
        with self.ledger._connection(write=True) as db:
            existing = db.execute("SELECT * FROM interviews WHERE id=?", (sid,)).fetchone()
            expected = request.model_dump(exclude={"start_only"})
            if existing:
                previous = json.loads(existing["start_json"])
                domain = json.loads(existing["domain_json"])
                if ((previous and previous != expected) or existing["goal"] != request.goal or domain["id"] != request.domain_id
                        or json.loads(existing["limits_json"]) != request.limits.model_dump()):
                    raise LedgerError("相同启动请求 ID 的内容不同", 409)
                return sid
            profile = next((p for p in self.ledger.domains() if p["id"] == request.domain_id), None)
            if not profile:
                raise LedgerError("领域不存在；新领域可显式选择通用经验访谈")
            if not request.goal.strip():
                raise LedgerError("请填写访谈目标")
            if request.decision_id and not request.case_id:
                raise LedgerError("选择判断时必须明确案例")
            db.execute("""INSERT INTO interviews(id,case_id,decision_id,goal,scope,status,limits_json,created_at,updated_at,domain_json,start_json)
                          VALUES(?,?,?,?,?,'ready',?,?,?,?,?)""",
                       (sid, request.case_id, request.decision_id, request.goal, request.goal,
                        _json(request.limits.model_dump()), _now(), _now(), _json(profile), _json(expected)))
            item = self._get(db, sid)
            for case_id in set(request.case_ids + ([request.case_id] if request.case_id else [])):
                self._link_case(db, item, case_id, "user:start")
            if request.case_id:
                self._scope(db, sid, request.case_id, request.decision_id)
                self._attach_gaps(db, item, request.decision_id)
            self._event(db, item, "interview.started", {"id": sid, "goal": request.goal})
        return sid

    def get(self, sid):
        with self.ledger._connection() as db:
            item = self._get(db, sid)
            item["executing"] = bool(item["lease_token"] and item["lease_until"] > time.time())
            item.pop("lease_token")
            item.pop("lease_until")
            item.pop("start_json")
            item.pop("domain_json")
            item["gaps"] = []
            for row in db.execute("SELECT * FROM interview_gaps WHERE interview_id=? ORDER BY rowid", (sid,)):
                gap = dict(row)
                gap["evidence"] = json.loads(gap.pop("evidence_json"))
                item["gaps"].append(gap)
            item["questions"] = [dict(r) for r in db.execute("SELECT * FROM interview_questions WHERE interview_id=? ORDER BY rowid", (sid,))]
            for question in item["questions"]:
                question["quality_warnings"] = json.loads(question.pop("quality_json"))
            item["actions"] = []
            for row in db.execute("SELECT id,model,prompt_version,attempts,action_json,usage_json,outcome_json,error,created_at FROM interview_actions WHERE interview_id=? ORDER BY rowid", (sid,)):
                action = dict(row)
                for field in ("action", "usage", "outcome"):
                    action[field] = json.loads(action.pop(field + "_json") or "null")
                item["actions"].append(action)
            item["turns"] = [dict(r) for r in db.execute("SELECT * FROM turns WHERE interview_id=? ORDER BY interview_seq", (sid,))]
            item["cases"] = []
            for row in db.execute("SELECT c.* FROM cases c JOIN interview_cases i ON i.case_id=c.id WHERE i.interview_id=? ORDER BY i.created_at,c.id", (sid,)):
                case = dict(row)
                case.pop("domain_json")
                case["decisions"] = [dict(d) for d in db.execute("SELECT * FROM decisions WHERE case_id=? ORDER BY created_at,id", (case["id"],))]
                item["cases"].append(case)
            item["turn_contexts"] = [dict(r) for r in db.execute("SELECT t.* FROM turn_contexts t JOIN interview_cases c ON c.case_id=t.case_id WHERE c.interview_id=? ORDER BY t.created_at,t.id", (sid,))]
            item["focus_proposals"] = []
            for row in db.execute("SELECT * FROM focus_proposals WHERE interview_id=? ORDER BY created_at,id", (sid,)):
                proposal = dict(row)
                proposal["remaining"] = json.loads(proposal.pop("remaining_json"))
                item["focus_proposals"].append(proposal)
            item["quality_feedback"] = [dict(r) for r in db.execute("SELECT * FROM interview_quality_feedback WHERE interview_id=? ORDER BY created_at,id", (sid,))]
            # A case may be reused by many interviews. Only the durable outcomes
            # of this interview identify candidates generated in this session.
            # Candidate and outcome commit together, including crash recovery.
            item["candidate_ids"] = list(dict.fromkeys(
                action["outcome"]["candidate_id"] for action in item["actions"]
                if isinstance(action["outcome"], dict) and action["outcome"].get("kind") == "draft_experience"
                and action["outcome"].get("candidate_id")))
            item["related_candidate_ids"] = [r[0] for r in db.execute("SELECT DISTINCT c.id FROM candidates c JOIN revision_contexts r ON r.candidate_id=c.id JOIN interview_cases i ON i.case_id=r.case_id WHERE i.interview_id=? ORDER BY c.created_at", (sid,))
                                             if r[0] not in item["candidate_ids"]]
            item["events"] = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute("SELECT * FROM interview_events WHERE interview_id=? ORDER BY id", (sid,))]
            item["deferred_topics"] = [{"id": event["id"], **event["payload"]} for event in item["events"] if event["type"] == "topic.deferred"]
            return item

    def list(self):
        with self.ledger._connection() as db:
            return [dict(r) for r in db.execute("SELECT id,case_id,goal,status,revision,notice,updated_at FROM interviews ORDER BY created_at DESC")]

    def acquire(self, sid):
        token = _id()
        with self.ledger._connection(write=True) as db:
            current = self._get(db, sid)
            if current["status"] in ("waiting", "closed", "review"):
                raise LedgerError("请先回答当前问题，或明确选择继续访谈", 409)
            if current["lease_token"] and current["lease_until"] > time.time():
                raise LedgerError("此访谈正在处理，请稍后刷新", 409)
            db.execute("UPDATE interviews SET status='running',lease_token=?,lease_until=?,revision=revision+1,notice='',updated_at=? WHERE id=?",
                       (token, time.time() + 180, _now(), sid))
        return token

    def _check(self, db, sid, token):
        item = self._get(db, sid)
        if item["lease_token"] != token or item["status"] != "running":
            raise LedgerError("访谈已暂停、结束或由其他请求接管", 409)
        db.execute("UPDATE interviews SET lease_until=? WHERE id=?", (time.time() + 180, sid))
        return item

    def release(self, sid, token):
        with self.ledger._connection(write=True) as db:
            db.execute("UPDATE interviews SET lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?", (sid, token))

    def mark(self, sid, token, status, notice):
        with self.ledger._connection(write=True) as db:
            current = self._get(db, sid)
            if current["lease_token"] != token or current["status"] != "running":
                return
            db.execute("UPDATE interviews SET status=?,notice=?,revision=revision+1,updated_at=? WHERE id=?", (status, notice, _now(), sid))
            self._event(db, current, "interview." + status, {"id": sid, "reason": notice})

    def budget_reason(self, sid):
        current = self.get(sid)
        limits = current["limits"]
        if current["calls"] >= limits["max_calls"]:
            return "达到本次模型调用上限；已保存进度，尚未宣告信息充分。"
        if current["tokens"] >= limits["max_tokens"]:
            return "达到本次 token 预算；已保存进度。"
        if len(current["turns"]) >= limits["max_turns"]:
            return "达到本次回答轮次上限；已保存全部回答。"
        return ""

    @staticmethod
    def _input_summary(db, sid):
        return {
            "text_sources": db.execute("""SELECT COUNT(*) FROM sources s
                JOIN interview_cases c ON c.case_id=s.case_id WHERE c.interview_id=?""", (sid,)).fetchone()[0],
            "files": db.execute("""SELECT COUNT(*) FROM file_versions f
                JOIN interview_cases c ON c.case_id=f.case_id WHERE c.interview_id=?""", (sid,)).fetchone()[0],
            "turns": db.execute("""SELECT COUNT(DISTINCT t.id) FROM turns t
                LEFT JOIN turn_contexts x ON x.turn_id=t.id
                LEFT JOIN interview_cases c ON c.case_id=x.case_id AND c.interview_id=?
                WHERE t.interview_id=? OR c.interview_id=?""", (sid, sid, sid)).fetchone()[0],
        }

    def _save_question(self, db, item, question):
        pending = {"id": _id(), "gap_id": None, "gap_key": None, "hypothesis": "",
                   "case_id": item["case_id"], "decision_id": item["decision_id"],
                   "quality_warnings": [], **question}
        db.execute("""INSERT INTO interview_questions(id,interview_id,gap_id,question,hypothesis,reason,expected_information,
            created_at,case_id,decision_id,quality_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (pending["id"], item["id"], pending["gap_id"], pending["question"], pending["hypothesis"], pending["reason"],
             pending["expected_information"], _now(), pending["case_id"], pending["decision_id"], _json(pending["quality_warnings"])))
        db.execute("UPDATE interviews SET status='waiting',pending_json=?,notice=? WHERE id=?",
                   (_json(pending), pending["reason"], item["id"]))
        return pending

    def request_initial_text(self, sid, token):
        """Collect original content before spending a model call on an empty start."""
        with self.ledger._connection(write=True) as db:
            item = self._check(db, sid, token)
            if any(self._input_summary(db, sid).values()):
                return False
            if item["intent"] == "draft":
                db.execute("UPDATE interviews SET status='paused',notice=?,revision=revision+1,updated_at=? WHERE id=?",
                           ("尚无原始内容，请先输入文字或添加资料，再整理候选经验。", _now(), sid))
            else:
                question = self._save_question(db, item, {
                    "origin": "initial_text",
                    "question": "请先用文字写下与你的访谈目标有关的一段经历或说明。",
                    "reason": "目前只有访谈主题，尚无原始内容。先保存你的文字，再确定从哪里展开。",
                    "expected_information": "一段经历、当前做法，或你希望整理的原文；记不清的地方可以注明。",
                })
                db.execute("UPDATE interviews SET revision=revision+1,updated_at=? WHERE id=?", (_now(), sid))
                self._event(db, item, "interview.initial_text_requested", {"question_id": question["id"]})
            return True

    def context(self, sid, last_tool=None, *, max_chars=54000, model_capabilities=None):
        current = self.get(sid)
        turns = []
        for turn in current["turns"][-12:]:
            turns.append({"id": turn["id"], "seq": turn["interview_seq"], "question": turn["question"][:600],
                          "answer": turn["answer"][:1400], "hypothesis": turn["hypothesis"][:600], "speaker": turn["speaker"],
                          "correction_of": turn["correction_of"], "truncated": len(turn["answer"]) > 1400})
        catalog = []
        with self.ledger._connection() as db:
            input_summary = self._input_summary(db, sid)
            for case in current["cases"]:
                for row in db.execute("SELECT id,source_id,title,version,sha256 FROM sources WHERE case_id=? ORDER BY created_at DESC LIMIT 30", (case["id"],)):
                    catalog.append({**dict(row), "source_type": "document", "case_id": case["id"]})
                if hasattr(self.ledger, "list_file_metadata"):
                    catalog.extend({**v, "source_type": "file"} for v in self.ledger.list_file_metadata(db, case["id"]))
            for row in db.execute("""SELECT DISTINCT t.id,t.interview_id,t.speaker,t.created_at FROM turns t
                LEFT JOIN turn_contexts x ON x.turn_id=t.id LEFT JOIN interview_cases c ON c.case_id=x.case_id AND c.interview_id=?
                WHERE t.interview_id=? OR c.interview_id=? ORDER BY t.created_at DESC LIMIT 30""", (sid, sid, sid)):
                catalog.append({"id": row["id"], "source_type": "turn", "speaker": row["speaker"],
                                "interview_id": row["interview_id"], "created_at": row["created_at"]})
        focus_gaps = []
        for gap in sorted(current["gaps"], key=lambda g: g["decision_id"] != current["decision_id"])[:20]:
            value = {**gap, "reason": gap["reason"][:800], "label": gap["label"][:300]}
            value["evidence"] = [{**{k: v for k, v in ref.items() if k != "quote"}, "quote_preview": ref.get("quote", "")[:200]} for ref in gap["evidence"][:5]]
            focus_gaps.append(value)
        domain = {k: v for k, v in current["domain"].items() if k not in ("question_templates", "prompts")}
        domain["interview_guidance"] = str(domain.get("interview_guidance", ""))[:3000]
        context = {"goal": current["goal"], "scope": current["scope"], "domain": domain, "requested_action": current["intent"],
                   "input_summary": input_summary,
                   "experience_fields": current["domain"]["experience_fields"], "gaps": focus_gaps[:20],
                   "focus": {"case_id": current["case_id"], "decision_id": current["decision_id"]}, "context_epoch": current["context_epoch"],
                   "cases": [{**{k: c[k] for k in ("id", "title", "domain_id")}, "context": c["context"][:500],
                              "decisions": [{**d, "description": d["description"][:500], "title": d["title"][:300]} for d in c["decisions"][:15]]} for c in current["cases"]],
                   "turns": turns, "earlier_turns_omitted": max(0, len(current["turns"]) - len(turns)),
                   "question_history": [{"id": q["id"], "gap_id": q["gap_id"], "case_id": q["case_id"], "decision_id": q["decision_id"], "question": q["question"][:300]} for q in current["questions"][-15:]],
                   "source_catalog": catalog[:40], "catalog_omitted": max(0, len(catalog) - 40),
                   "quality_feedback": current["quality_feedback"][-5:], "last_tool": last_tool, "limits": current["limits"],
                   "model_capabilities": model_capabilities or {}, "deferred_topics": current["deferred_topics"][-8:],
                   "focus_proposals": current["focus_proposals"][-3:],
                   "focus_feedback": [{"type": e["type"], **e["payload"]} for e in current["events"]
                                      if e["type"] in ("interview.focus_changed", "interview.focus_rejected")][-3:],
                   "selection_note": "仅显示部分原话和目录，未显示不表示不存在。search_sources可搜索已关联案例原话，read_source可按原始ID读取全文片段。"}
        # Selection discards only derived context, never original records. Every
        # selected list records omitted counts and originals remain searchable.
        for key in ("gaps", "cases", "question_history", "source_catalog", "turns"):
            while len(_json(context)) > max_chars and len(context[key]) > 1:
                context[key].pop(0 if key in ("turns", "question_history") else -1)
                context[key + "_omitted"] = context.get(key + "_omitted", 0) + 1
        return context

    def call_record(self, sid, token, action_id, context, model, prompt_version):
        with self.ledger._connection(write=True) as db:
            self._check(db, sid, token)
            old = db.execute("SELECT * FROM interview_actions WHERE id=?", (action_id,)).fetchone()
            if old and old["action_json"]:
                return json.loads(old["action_json"])
            db.execute("UPDATE interviews SET calls=calls+1 WHERE id=?", (sid,))
            db.execute("""INSERT INTO interview_actions(id,interview_id,model,prompt_version,context_json,attempts,created_at)
                          VALUES(?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET attempts=attempts+1,error=NULL,
                          context_json=excluded.context_json,model=excluded.model,prompt_version=excluded.prompt_version""",
                       (action_id, sid, model, prompt_version, _json(context), _now()))
            return None

    def cached_action(self, sid, action_id):
        with self.ledger._connection() as db:
            row = db.execute("SELECT action_json FROM interview_actions WHERE interview_id=? AND id=?", (sid, action_id)).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def proposal(self, sid, token, action_id, proposal):
        if type(proposal.tokens) is not int or proposal.tokens < 0:
            raise LedgerError("模型使用量无效")
        with self.ledger._connection(write=True) as db:
            item = self._check(db, sid, token)
            db.execute("UPDATE interview_actions SET action_json=?,usage_json=?,model=? WHERE id=?",
                       (_json(proposal.action), _json({"tokens": proposal.tokens}), proposal.model, action_id))
            db.execute("UPDATE interviews SET tokens=tokens+? WHERE id=?", (proposal.tokens, sid))
            self._event(db, item, "interview.model_returned", {"interview_id": sid, "action_id": action_id, "model": proposal.model, "tokens": proposal.tokens})

    def action_error(self, sid, action_id, message):
        with self.ledger._connection(write=True) as db:
            db.execute("UPDATE interview_actions SET error=? WHERE id=? AND interview_id=?", (message[:3000], action_id, sid))

    def _attach_gaps(self, db, item, decision_id):
        if not decision_id:
            return
        for gap in db.execute("SELECT * FROM gaps WHERE decision_id=?", (decision_id,)):
            status = gap["status"] if gap["status"] != "answered" else "open"
            db.execute("""INSERT OR IGNORE INTO interview_gaps
                (id,interview_id,key,label,required,status,reason,evidence_json,decision_id) VALUES(?,?,?,?,?,?,?,?,?)""",
                (gap["id"], item["id"], gap["kind"], gap["question"], gap["required"], status,
                 gap["reason"], gap["evidence_json"], decision_id))

    def _citations(self, db, item, citations, contexts, field="gap"):
        resolved = []
        for cite in citations:
            ref = {"field": getattr(cite, "field", field), "source_type": cite.source_type,
                   "source_id": cite.source_id, "quote": cite.quote}
            if cite.source_type == "file":
                if not cite.locator:
                    raise LedgerError("文件引用需要固定解析版本和位置")
                ref["locator"] = cite.locator
            else:
                if cite.source_type == "document":
                    source = self.ledger._source(db, cite.source_id)
                    original = source["text"]
                elif cite.source_type == "turn":
                    source = self._readable_turn(db, item, cite.source_id)
                    original = source["answer"]
                else:
                    source = self.ledger.get_feedback(cite.source_id)
                    original = source["original"]
                start = cite.start
                if start is None:
                    if not cite.quote or original.count(cite.quote) != 1:
                        raise LedgerError("引文无法唯一定位，请提供start或更完整原文")
                    start = original.find(cite.quote)
                ref.update(start=start, end=start + len(cite.quote))
            resolved.append(ref)
        allowed = {r["field"] for r in resolved}
        primary = contexts[0]["case_id"] if contexts else None
        return self.ledger._validate_evidence(db, primary, resolved, allowed, contexts=contexts)

    def _upsert_gaps(self, db, item, updates):
        for update in updates:
            case_id = update.case_id or item["case_id"]
            decision_id = update.decision_id or item["decision_id"]
            self._scope(db, item["id"], case_id, decision_id)
            if not decision_id:
                raise LedgerError("知识缺口需要明确判断，开放叙述可以不填写gaps")
            refs = self._citations(db, item, update.evidence, [{"case_id": case_id, "decision_id": decision_id}])
            if update.status == "covered" and not refs:
                raise LedgerError("已覆盖建议必须提供原文依据")
            old = db.execute("SELECT * FROM gaps WHERE decision_id=? AND kind=?", (decision_id, update.key)).fetchone()
            gap_id = old["id"] if old else _id()
            if old:
                db.execute("UPDATE gaps SET question=?,status=?,required=?,reason=?,evidence_json=? WHERE id=?",
                           (update.label, update.status, int(update.required), update.reason, _json(refs), gap_id))
            else:
                db.execute("INSERT INTO gaps(id,case_id,decision_id,kind,question,status,required,reason,evidence_json) VALUES(?,?,?,?,?,?,?,?,?)",
                           (gap_id, case_id, decision_id, update.key, update.label, update.status, int(update.required), update.reason, _json(refs)))
            db.execute("""INSERT INTO interview_gaps(id,interview_id,key,label,required,status,reason,evidence_json,decision_id)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(interview_id,id) DO UPDATE SET label=excluded.label,
                required=excluded.required,status=excluded.status,reason=excluded.reason,evidence_json=excluded.evidence_json""",
                (gap_id, item["id"], update.key, update.label, int(update.required), update.status, update.reason, _json(refs), decision_id))
            for ref in refs:
                if ref["source_type"] == "turn":
                    db.execute("INSERT OR IGNORE INTO interview_gap_turns VALUES(?,?)", (gap_id, ref["source_id"]))

    def _proposal(self, db, item, case_id, decision_id, summary, remaining, reason):
        self.ledger._case(db, case_id)
        if decision_id:
            self.ledger._belongs(self.ledger._one(db, "decisions", decision_id), case_id)
        proposal_id = _id()
        db.execute("INSERT INTO focus_proposals VALUES(?,?,?,?,?,?,?,'pending',?)",
                   (proposal_id, item["id"], case_id, decision_id, summary, _json(remaining), reason, _now()))
        db.execute("UPDATE interviews SET status='paused',notice=? WHERE id=?",
                   ("请核对当前整理与剩余问题，再决定是否关联案例或切换重点。", item["id"]))
        return proposal_id

    def apply(self, sid, token, action_id, action):
        with self.ledger._connection(write=True) as db:
            item = self._check(db, sid, token)
            saved = db.execute("SELECT outcome_json,context_json FROM interview_actions WHERE id=?", (action_id,)).fetchone()
            # Old checkpoints use epoch zero. Human changes invalidate only the
            # obsolete proposal, not its original source records or audit trail.
            if saved and json.loads(saved["context_json"]).get("context_epoch", 0) != item["context_epoch"]:
                return {"kind": "context_changed"}
            if saved and saved["outcome_json"]:
                outcome = json.loads(saved["outcome_json"])
                if outcome["kind"] == "ask_expert":
                    if db.execute("SELECT 1 FROM interview_inputs WHERE question_id=?", (outcome["question"]["id"],)).fetchone():
                        return {"kind": "input_already_saved"}
                    db.execute("UPDATE interviews SET status='waiting',pending_json=?,revision=revision+1 WHERE id=?", (_json(outcome["question"]), sid))
                elif outcome["kind"] == "draft_experience":
                    db.execute("UPDATE interviews SET status='review',pending_json=NULL,revision=revision+1 WHERE id=?", (sid,))
                elif outcome["kind"] in ("pause", "suggest_finish", "suggest_focus", "link_case"):
                    db.execute("UPDATE interviews SET status='paused',revision=revision+1 WHERE id=?", (sid,))
                return outcome
            if item["intent"] == "draft" and action.action not in ("draft_experience", "pause", "search_sources", "read_source", "create_case", "create_decision", "link_turn", "suggest_focus"):
                raise LedgerError("用户要求整理已有记录，请保留未知，不继续扩展追问")
            if action.action == "propose_relation":
                raise LedgerError("请在经验比较入口明确选择两端修订，再提出关联建议")
            self._upsert_gaps(db, item, action.gaps)
            outcome = {"kind": action.action}
            case_id = action.case_id or item["case_id"]
            decision_id = action.decision_id or item["decision_id"]
            if action.action == "ask_expert":
                if (action.case_id and action.case_id != item["case_id"]) or (action.decision_id and action.decision_id != item["decision_id"]):
                    raise LedgerError("改变访谈重点前请先suggest_focus，由用户确认")
                gap = None
                if action.gap_id or action.gap_key:
                    gap = db.execute("SELECT * FROM interview_gaps WHERE interview_id=? AND id=?", (sid, action.gap_id)).fetchone() if action.gap_id else db.execute(
                        "SELECT * FROM interview_gaps WHERE interview_id=? AND decision_id IS ? AND key=?", (sid, decision_id, action.gap_key)).fetchone()
                    if not gap:
                        raise LedgerError("问题指定的缺口不存在，请明确当前判断；开放叙述可不指定gap")
                    if gap["decision_id"] != item["decision_id"]:
                        raise LedgerError("该缺口属于另一判断，请先提出焦点切换")
                normalized = re.sub(r"\s+", "", action.question).casefold()
                warnings = []
                if not normalized:
                    raise LedgerError("问题不能为空")
                for prior in db.execute("SELECT id,question,case_id,decision_id FROM interview_questions WHERE interview_id=?", (sid,)):
                    if re.sub(r"\s+", "", prior["question"]).casefold() == normalized:
                        warnings.append({"kind": "repeated", "prior_question_id": prior["id"],
                                         "same_focus": prior["case_id"] == case_id and prior["decision_id"] == decision_id,
                                         "message": "与已有问题措辞相同，请结合案例和判断核对是否有必要。"})
                if "是不是" in action.question or "是否因为" in action.question:
                    warnings.append({"kind": "possible_leading", "message": "问题可能预设了答案，请由专家核对前提。"})
                if gap and gap["status"] in ("deferred", "not_applicable", "covered"):
                    warnings.append({"kind": "revisit_gap", "message": "该缺口已有处置，请核对重访理由。"})
                outcome["question"] = self._save_question(db, item, {
                    "gap_id": gap["id"] if gap else None, "gap_key": action.gap_key,
                    "question": action.question, "hypothesis": action.hypothesis, "reason": action.reason,
                    "expected_information": action.expected_information, "quality_warnings": warnings,
                })
            elif action.action in ("search_sources", "read_source"):
                outcome["results"] = self._search(db, item, action)
            elif action.action == "create_case":
                case_id = _id()
                db.execute("INSERT INTO cases(id,domain_id,title,context,domain_json,created_at) VALUES(?,?,?,?,?,?)",
                           (case_id, item["domain"]["id"], action.title, action.description, item["domain_json"], _now()))
                self._link_case(db, item, case_id, "system:interviewer")
                outcome["case_id"] = case_id
                outcome["requires_focus_confirmation"] = True
            elif action.action == "create_decision":
                self._scope(db, sid, action.case_id)
                new_id = _id()
                db.execute("INSERT INTO decisions(id,case_id,title,description,created_at) VALUES(?,?,?,?,?)",
                           (new_id, action.case_id, action.title, action.description, _now()))
                outcome["case_id"], outcome["decision_id"] = action.case_id, new_id
            elif action.action in ("suggest_focus", "link_case"):
                outcome["proposal_id"] = self._proposal(db, item, action.case_id, action.decision_id,
                    action.summary or "建议关联此案例，关联后再选择访谈重点。", action.remaining, action.reason)
            elif action.action == "link_turn":
                outcome["context_id"] = self._link_turn(db, item, action.turn_id, action.start if action.start is not None else 0,
                    action.end, action.case_id, action.decision_id, action.gap_id)
            elif action.action == "defer_topic":
                if action.turn_id:
                    self._readable_turn(db, item, action.turn_id)
                topic = {"title": action.title, "description": action.description, "reason": action.reason, "turn_id": action.turn_id}
                self._event(db, item, "topic.deferred", topic)
                outcome["topic"] = topic
            elif action.action == "draft_experience":
                contexts = [c.model_dump() for c in action.contexts] or ([{"case_id": case_id, "decision_id": decision_id}] if case_id else [])
                if not contexts:
                    raise LedgerError("先根据真实叙述建立案例并关联原话，再整理候选经验")
                for scope in contexts:
                    self._scope(db, sid, scope["case_id"], scope.get("decision_id"))
                refs = self._citations(db, item, action.evidence, contexts)
                if not any(ref["source_type"] == "turn" for ref in refs):
                    raise LedgerError("候选经验至少需要一处专家原话引用")
                content = [v.model_dump() for v in action.content]
                for value in content:
                    if value["status"] != "unknown" and not any(ref["field"] == value["key"] for ref in refs):
                        raise LedgerError("有依据或假设字段需要原话引用，信息缺失请明确保留unknown")
                schema = item["domain"]["experience_fields"]
                primary = contexts[0]
                cid = _id()
                db.execute("""INSERT INTO candidates(id,case_id,decision_id,current_revision,status,validation_status,created_at,domain_id,domain_json)
                    VALUES(?,?,?,1,'pending','unvalidated',?,?,?)""",
                    (cid, primary["case_id"], primary.get("decision_id"), _now(), item["domain"]["id"], item["domain_json"]))
                self.ledger._insert_content_revision(db, cid, 1, primary["case_id"], action.title, action.kind,
                    content, schema, refs, "system:interviewer", contexts=contexts)
                db.execute("UPDATE interviews SET status='review',intent='continue',pending_json=NULL,notice=? WHERE id=?",
                           ("候选已保存，请核对原话、适用条件和未知部分。", sid))
                outcome["candidate_id"] = cid
                self._event(db, item, "candidate.drafted", {"id": cid, "revision": 1, "interview_id": sid})
            else:
                # A finish suggestion is a proposal for the user, never a claim
                # that every information gap has been resolved.
                outcome["remaining_gaps"] = [dict(r) for r in db.execute(
                    "SELECT id,key,label,status FROM interview_gaps WHERE interview_id=? AND status IN ('open','conflict')", (sid,))]
                db.execute("UPDATE interviews SET status='paused',pending_json=NULL,notice=? WHERE id=?", (action.reason, sid))
            db.execute("UPDATE interviews SET revision=revision+1,updated_at=? WHERE id=?", (_now(), sid))
            db.execute("UPDATE interview_actions SET outcome_json=? WHERE id=?", (_json(outcome), action_id))
            self._event(db, item, "interview.action", {"id": sid, "action_id": action_id, "action": action.action, "reason": action.reason})
            return outcome

    def _turn_ranges(self, db, item, turn):
        if turn["interview_id"] == item["id"]:
            return [(0, len(turn["answer"]))]
        return [(r[0], r[1]) for r in db.execute("""SELECT DISTINCT t.start,t.end FROM turn_contexts t
            JOIN interview_cases c ON c.case_id=t.case_id WHERE c.interview_id=? AND t.turn_id=? ORDER BY t.start""", (item["id"], turn["id"]))]

    def _search(self, db, item, action):
        if action.action == "read_source":
            if action.source_type == "file":
                source = self.ledger.get_file(action.source_id)
                self._scope(db, item["id"], source["case_id"])
                return [self.ledger.read_file(action.source_id, locator=action.locator, offset=action.offset, native=action.native)]
            if action.source_type == "feedback":
                source = self.ledger.get_feedback(action.source_id)
                allowed = {r[0] for r in db.execute("SELECT case_id FROM interview_cases WHERE interview_id=?", (item["id"],))}
                if source.get("case_id") not in allowed:
                    raise LedgerError("反馈不属于已关联的案例范围")
                original = source["original"]
                start, end = action.offset, min(len(original), action.offset + 4000)
            elif action.source_type == "turn":
                source = self._readable_turn(db, item, action.source_id)
                original = source["answer"]
                ranges = self._turn_ranges(db, item, source)
                target = next(((a, b) for a, b in ranges if a <= action.offset < b), None)
                if target is None and action.offset == 0 and ranges:
                    target = ranges[0]
                if target is None:
                    raise LedgerError("读取位置未关联到本次访谈范围")
                start = max(action.offset, target[0])
                end = min(start + 4000, target[1])
            else:
                source = self.ledger._source(db, action.source_id)
                self._scope(db, item["id"], source["case_id"])
                original = source["text"]
                start, end = action.offset, min(len(original), action.offset + 4000)
            if start >= len(original):
                raise LedgerError("读取起点超出原文")
            return [{"source_id": source["id"], "source_type": action.source_type, "start": start,
                     "text": original[start:end], "total_length": len(original)}]
        terms = [t.casefold() for t in re.findall(r"[\w]+", action.query) if t]
        matches = []
        for row in db.execute("""SELECT s.id FROM sources s JOIN interview_cases c ON c.case_id=s.case_id
            WHERE c.interview_id=? AND s.version=(SELECT MAX(t.version) FROM sources t WHERE t.source_id=s.source_id)
            ORDER BY s.created_at DESC""", (item["id"],)):
            source = self.ledger._source(db, row[0])
            self._search_excerpt(matches, terms, source["id"], "document", source["text"], 0, source["title"], version=source["version"])
        for row in db.execute("""SELECT DISTINCT t.* FROM turns t LEFT JOIN turn_contexts x ON x.turn_id=t.id
            LEFT JOIN interview_cases c ON c.case_id=x.case_id AND c.interview_id=?
            WHERE t.interview_id=? OR c.interview_id=? ORDER BY t.created_at DESC""", (item["id"], item["id"], item["id"])):
            turn = dict(row)
            for start, end in self._turn_ranges(db, item, turn):
                self._search_excerpt(matches, terms, turn["id"], "turn", turn["answer"][start:end], start, turn["speaker"])
        if hasattr(self.ledger, "_file_search"):
            case_ids = [r[0] for r in db.execute("SELECT case_id FROM interview_cases WHERE interview_id=?", (item["id"],))]
            matches.extend(self.ledger._file_search(db, case_ids, terms))
        return [result for _, result in sorted(matches, key=lambda pair: pair[0], reverse=True)[:6]]

    @staticmethod
    def _search_excerpt(matches, terms, source_id, source_type, text, base, title, **extra):
        score = sum(text.casefold().count(term) + title.casefold().count(term) for term in terms)
        if score:
            positions = [text.casefold().find(term) for term in terms if term in text.casefold()]
            offset = max(0, min(positions, default=0) - 200)
            matches.append((score, {"source_id": source_id, "source_type": source_type, "title": title,
                "start": base + offset, "text": text[offset:offset + 2500], "total_length": base + len(text), **extra}))

    def _save_turn(self, db, item, request, question=None):
        question = question or {}
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE case_id IS ?", (item["case_id"],)).fetchone()[0]
        interview_seq = db.execute("SELECT COALESCE(MAX(interview_seq),0)+1 FROM turns WHERE interview_id=?", (item["id"],)).fetchone()[0]
        turn_id = _id()
        db.execute("""INSERT INTO turns(id,case_id,gap_id,decision_id,seq,kind,question,answer,speaker,created_at,
            hypothesis,interview_id,interview_seq,correction_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (turn_id, item["case_id"], question.get("gap_id"), item["decision_id"], seq, "interview" if question else "supplement",
             question.get("question", "主动补充或纠正"), request.answer, request.speaker, _now(), question.get("hypothesis", ""),
             item["id"], interview_seq, getattr(request, "correction_of", None)))
        if item["case_id"]:
            self._link_turn(db, item, turn_id, 0, len(request.answer), item["case_id"], item["decision_id"], question.get("gap_id"), "user:answer")
        if question.get("gap_id"):
            db.execute("INSERT OR IGNORE INTO interview_gap_turns VALUES(?,?)", (question["gap_id"], turn_id))
        self._event(db, item, "turn.recorded", {"id": turn_id, "interview_id": item["id"], "seq": interview_seq})
        return turn_id

    def answer(self, sid, request):
        payload_hash = sha256(_json(request.model_dump()).encode()).hexdigest()
        with self.ledger._connection(write=True) as db:
            item = self._get(db, sid)
            prior = db.execute("SELECT * FROM interview_inputs WHERE interview_id=? AND request_id=?", (sid, request.request_id)).fetchone()
            if prior:
                if prior["payload_hash"] != payload_hash:
                    raise LedgerError("相同请求ID不能提交不同回答", 409)
                return False
            if item["status"] != "waiting" or not item["pending"] or item["pending"]["id"] != request.question_id:
                raise LedgerError("当前问题已改变或已回答，请刷新", 409)
            if not request.answer.strip() or not request.speaker.strip():
                raise LedgerError("回答和发言人不能为空")
            question = dict(self.ledger._one(db, "interview_questions", request.question_id))
            if question["interview_id"] != sid or question["case_id"] != item["case_id"] or question["decision_id"] != item["decision_id"]:
                raise LedgerError("回答对应的访谈重点已变化", 409)
            turn_id = self._save_turn(db, item, request, question)
            db.execute("INSERT INTO interview_inputs VALUES(?,?,?,?,?,?,?)", (sid, request.request_id, payload_hash, request.question_id, turn_id, "answer", _now()))
            db.execute("UPDATE interviews SET status='ready',pending_json=NULL,notice='',revision=revision+1,updated_at=? WHERE id=?", (_now(), sid))
            return True

    def _human_revision(self, item, expected):
        if item["revision"] != expected:
            raise LedgerError("访谈状态已变化，请刷新后操作", 409)
        if item["status"] == "closed":
            raise LedgerError("访谈已结束", 409)

    def _cancel_question(self, db, item, kind):
        if item["pending"]:
            db.execute("INSERT OR IGNORE INTO interview_inputs VALUES(?,?,?,?,NULL,?,?)",
                       (item["id"], _id(), kind, item["pending"]["id"], kind, _now()))

    def supplement(self, sid, request):
        payload_hash = sha256(_json(request.model_dump()).encode()).hexdigest()
        with self.ledger._connection(write=True) as db:
            item = self._get(db, sid)
            prior = db.execute("SELECT payload_hash FROM interview_inputs WHERE interview_id=? AND request_id=?", (sid, request.request_id)).fetchone()
            if prior:
                if prior[0] != payload_hash:
                    raise LedgerError("相同请求ID不能改变补充内容", 409)
                return False
            if item["status"] == "closed":
                raise LedgerError("访谈已结束，可新建访谈继续补充同一案例", 409)
            if not request.answer.strip() or not request.speaker.strip():
                raise LedgerError("补充内容和发言人不能为空")
            if request.correction_of:
                self._readable_turn(db, item, request.correction_of)
            turn_id = self._save_turn(db, item, request)
            db.execute("INSERT INTO interview_inputs VALUES(?,?,?,NULL,?,'supplement',?)", (sid, request.request_id, payload_hash, turn_id, _now()))
            if request.continue_interview:
                self._cancel_question(db, item, "supplement_continue")
            status = "ready" if request.continue_interview else "paused"
            db.execute("UPDATE interviews SET status=?,pending_json=?,context_epoch=context_epoch+1,revision=revision+1,notice=?,updated_at=? WHERE id=?",
                       (status, None if request.continue_interview else _json(item["pending"]) if item["pending"] else None,
                        "补充原话已保存，原记录保留。", _now(), sid))
            return True

    def link_case(self, sid, request):
        with self.ledger._connection(write=True) as db:
            item = self._get(db, sid)
            self._human_revision(item, request.expected_revision)
            self._link_case(db, item, request.case_id, "user:link_case")
            db.execute("UPDATE interviews SET revision=revision+1,context_epoch=context_epoch+1,status='paused',notice=? WHERE id=?", (request.note or "案例已关联，访谈重点尚未改变。", sid))
            self._event(db, item, "interview.case_linked", {"case_id": request.case_id, "note": request.note})

    def set_focus(self, sid, request):
        with self.ledger._connection(write=True) as db:
            item = self._get(db, sid)
            self._human_revision(item, request.expected_revision)
            case_id, decision_id = request.case_id, request.decision_id
            if request.decision == "reject" and not request.proposal_id:
                raise LedgerError("继续当前重点时请指定要处理的焦点建议")
            if request.proposal_id:
                proposal = self.ledger._one(db, "focus_proposals", request.proposal_id)
                if proposal["interview_id"] != sid or proposal["status"] != "pending":
                    raise LedgerError("焦点建议已处理或不属于本次访谈", 409)
                if request.decision == "reject":
                    db.execute("UPDATE focus_proposals SET status='rejected' WHERE id=?", (request.proposal_id,))
                    note = request.note or "用户选择继续当前关注点。"
                    db.execute("UPDATE interviews SET status='paused',context_epoch=context_epoch+1,revision=revision+1,notice=?,updated_at=? WHERE id=?",
                               (note, _now(), sid))
                    self._event(db, item, "interview.focus_rejected", {"proposal_id": request.proposal_id, "note": note,
                                "case_id": item["case_id"], "decision_id": item["decision_id"]})
                    return
                case_id, decision_id = proposal["case_id"], proposal["decision_id"]
                db.execute("UPDATE focus_proposals SET status='accepted' WHERE id=?", (request.proposal_id,))
            if decision_id and not case_id:
                raise LedgerError("选择判断时必须明确案例")
            if case_id:
                # This is the explicit human authorization, unlike an AI link proposal.
                self._link_case(db, item, case_id, "user:focus")
                self._scope(db, sid, case_id, decision_id)
            self._cancel_question(db, item, "focus_changed")
            db.execute("UPDATE interviews SET case_id=?,decision_id=?,status='paused',pending_json=NULL,context_epoch=context_epoch+1,revision=revision+1,notice=?,updated_at=? WHERE id=?",
                       (case_id, decision_id, request.note or "重点已由用户确认，请继续访谈。", _now(), sid))
            self._attach_gaps(db, item, decision_id)
            self._event(db, item, "interview.focus_changed", {"case_id": case_id, "decision_id": decision_id, "note": request.note})

    def link_turn(self, sid, request):
        with self.ledger._connection(write=True) as db:
            item = self._get(db, sid)
            self._human_revision(item, request.expected_revision)
            if not request.actor.strip():
                raise LedgerError("请填写关联操作人")
            result = self._link_turn(db, item, request.turn_id, request.start, request.end, request.case_id,
                                     request.decision_id, request.gap_id, request.actor)
            db.execute("UPDATE interviews SET revision=revision+1,context_epoch=context_epoch+1,status=CASE WHEN status='running' THEN 'paused' ELSE status END WHERE id=?", (sid,))
            self._event(db, item, "turn.context_linked", {"id": result, **request.model_dump()})
            return result

    def quality_feedback(self, sid, request):
        with self.ledger._connection(write=True) as db:
            item = self._get(db, sid)
            self._human_revision(item, request.expected_revision)
            if not request.actor.strip() or not request.note.strip():
                raise LedgerError("请填写问题反馈与操作人")
            question = self.ledger._one(db, "interview_questions", request.question_id)
            if question["interview_id"] != sid:
                raise LedgerError("问题不属于本次访谈")
            db.execute("INSERT INTO interview_quality_feedback VALUES(?,?,?,?,?,?,?)",
                       (_id(), sid, request.question_id, request.kind, request.note, request.actor, _now()))
            if item["pending"] and item["pending"]["id"] == request.question_id:
                self._cancel_question(db, item, "quality_feedback")
                db.execute("UPDATE interviews SET pending_json=NULL WHERE id=?", (sid,))
            db.execute("UPDATE interviews SET status='paused',revision=revision+1,context_epoch=context_epoch+1,notice=? WHERE id=?", ("问题反馈已保存，请继续以便重新规划。", sid))
            self._event(db, item, "interview.quality_feedback", request.model_dump())

    def control(self, sid, command):
        with self.ledger._connection(write=True) as db:
            item = self._get(db, sid)
            self._human_revision(item, command.expected_revision)
            pending = item["pending"]
            status = {"pause": "paused", "close": "closed", "continue": "ready", "skip": "ready", "draft": "ready"}[command.command]
            if command.command == "continue" and pending:
                status = "waiting"
            if command.command == "skip":
                if not pending:
                    raise LedgerError("当前没有可跳过的问题", 409)
                if pending.get("gap_id"):
                    reason = command.note or "专家选择暂缓"
                    db.execute("UPDATE interview_gaps SET status='deferred',reason=? WHERE interview_id=? AND id=?", (reason, sid, pending["gap_id"]))
                    db.execute("UPDATE gaps SET status='deferred',reason=? WHERE id=?", (reason, pending["gap_id"]))
                self._cancel_question(db, item, "skip")
                pending = None
            if command.command in ("close", "draft"):
                self._cancel_question(db, item, command.command)
                pending = None
            intent = "draft" if command.command == "draft" else item["intent"] if command.command in ("pause", "continue") else "continue"
            notice = command.note or {"pause": "用户暂停", "close": "用户结束，未解决缺口保留", "continue": "", "skip": "已暂缓当前问题", "draft": "用户要求整理已有记录并保留未知"}[command.command]
            # Keep the old execution lease until its worker finally releases it.
            db.execute("UPDATE interviews SET intent=?,status=?,pending_json=?,notice=?,revision=revision+1,updated_at=? WHERE id=?",
                       (intent, status, _json(pending) if pending else None, notice, _now(), sid))
            self._event(db, item, "interview.control", {"id": sid, "command": command.command, "note": command.note})
