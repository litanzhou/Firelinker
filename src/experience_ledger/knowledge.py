"""Version-bound experience relationships and exact, immutable usage reports.

The model may propose relationships. Reviewing, withdrawing and approving remain
explicit human operations exposed by the workbench, never interview tools.
"""


class KnowledgeMixin:
    def _revision_record(self, db, candidate_id, revision):
        from .service import LedgerError
        self._one(db, "candidates", candidate_id)
        if type(revision) is not int or not 1 <= revision <= 9_223_372_036_854_775_807:
            raise LedgerError("修订号必须是有效正整数")
        row = db.execute("SELECT * FROM revisions WHERE candidate_id=? AND revision=?", (candidate_id, revision)).fetchone()
        if row is None:
            raise LedgerError("经验修订不存在", 404)
        return dict(row)

    def _revision_contexts(self, db, candidate_id, revision):
        return [dict(r) for r in db.execute(
            "SELECT case_id,decision_id FROM revision_contexts WHERE candidate_id=? AND revision=? ORDER BY rowid",
            (candidate_id, revision))]

    def _normalize_contexts(self, db, contexts):
        from .service import LedgerError
        if not isinstance(contexts, list) or not 1 <= len(contexts) <= 100:
            raise LedgerError("修订需明确关联1至100项案例或判断范围")
        result, seen = [], set()
        for context in contexts:
            if not isinstance(context, dict) or set(context) - {"case_id", "decision_id"}:
                raise LedgerError("经验关联范围仅允许 case_id/decision_id")
            case_id, decision_id = context.get("case_id"), context.get("decision_id")
            self._case(db, case_id)
            if decision_id is not None:
                self._belongs(self._one(db, "decisions", decision_id), case_id)
            key = (case_id, decision_id)
            if key not in seen:
                result.append({"case_id": case_id, "decision_id": decision_id})
                seen.add(key)
        return result

    def _contexts_for_revision(self, db, candidate_id, revision, contexts=None):
        if contexts is None:
            contexts = self._revision_contexts(db, candidate_id, revision - 1) if revision > 1 else []
            if not contexts:
                candidate = self._one(db, "candidates", candidate_id)
                contexts = [{"case_id": candidate["case_id"], "decision_id": candidate["decision_id"]}]
        return self._normalize_contexts(db, contexts)

    def _save_revision_contexts(self, db, candidate_id, revision, contexts=None):
        contexts = self._contexts_for_revision(db, candidate_id, revision, contexts)
        for context in contexts:
            db.execute("INSERT INTO revision_contexts(candidate_id,revision,case_id,decision_id) VALUES(?,?,?,?)",
                       (candidate_id, revision, context["case_id"], context["decision_id"]))
        return contexts

    def _approved_revision(self, db, candidate_id, revision):
        return db.execute("SELECT 1 FROM reviews WHERE candidate_id=? AND revision=? AND decision='approve'",
                          (candidate_id, revision)).fetchone() is not None

    def withdraw_candidate(self, candidate_id, expected_revision, actor, note, expected_active_revision=None):
        from .service import LedgerError, _text
        _text(actor, "操作者", limit=200)
        _text(note, "撤下说明", limit=10000)
        with self._connection(write=True) as db:
            candidate = self._one(db, "candidates", candidate_id)
            self._expected(candidate, expected_revision)
            if expected_active_revision is not None and (type(expected_active_revision) is not int or expected_active_revision < 1):
                raise LedgerError("expected_active_revision 必须是正整数")
            if expected_active_revision is not None and candidate["active_revision"] != expected_active_revision:
                raise LedgerError("默认可查阅版本已改变，请刷新后再撤下", 409)
            if candidate["active_revision"] is None:
                raise LedgerError("该经验没有可撤下的当前查阅版本", 409)
            previous = candidate["active_revision"]
            db.execute("UPDATE candidates SET active_revision=NULL WHERE id=?", (candidate_id,))
            self._event(db, candidate["case_id"], "candidate.withdrawn", {
                "id": candidate_id, "revision": previous, "actor": actor, "note": note})
            return self._candidate(db, candidate_id)

    def _relation(self, db, relation_id):
        import json
        relation = self._one(db, "experience_relations", relation_id)
        relation["evidence"] = json.loads(relation.pop("evidence_json"))
        relation["reviews"] = [dict(r) for r in db.execute(
            "SELECT * FROM relation_reviews WHERE relation_id=? ORDER BY rowid", (relation_id,))]
        for side in ("left", "right"):
            candidate = self._one(db, "candidates", relation[side + "_id"])
            relation[side + "_active_revision"] = candidate["active_revision"]
            relation[side + "_latest_revision"] = candidate["current_revision"]
        relation["applies_to_active"] = all(relation[side + "_active_revision"] == relation[side + "_revision"]
                                               for side in ("left", "right"))
        relation["requires_recheck"] = any(relation[side + "_latest_revision"] != relation[side + "_revision"]
                                              for side in ("left", "right"))
        return relation

    def get_relation(self, relation_id):
        with self._connection() as db:
            return self._relation(db, relation_id)

    def _relations(self, db, candidate_id=None, status=None):
        from .service import LedgerError
        if status is not None and status not in ("proposed", "accepted", "rejected", "resolved"):
            raise LedgerError("关系状态无效")
        if candidate_id is not None:
            self._one(db, "candidates", candidate_id)
        rows = db.execute("""SELECT id FROM experience_relations
            WHERE (? IS NULL OR left_id=? OR right_id=?) AND (? IS NULL OR status=?) ORDER BY created_at,id""",
            (candidate_id, candidate_id, candidate_id, status, status)).fetchall()
        return [self._relation(db, row[0]) for row in rows]

    def list_relations(self, candidate_id=None, status=None):
        with self._connection() as db:
            return self._relations(db, candidate_id, status)

    def propose_relation(self, left_id, left_revision, right_id, right_revision, kind, conditions, reason, evidence, actor, proposal_id=None):
        from .service import LedgerError, _id, _json, _now, _text
        if kind not in ("duplicate", "supplement", "conflict"):
            raise LedgerError("关系类型必须为 duplicate/supplement/conflict")
        if left_id == right_id and left_revision == right_revision:
            raise LedgerError("不能将同一修订与自身建立关系")
        _text(conditions, "条件比较", limit=20000)
        _text(reason, "关系理由", limit=20000)
        _text(actor, "提出者", limit=200)
        if proposal_id is not None:
            _text(proposal_id, "建议 ID", limit=128)
        if not isinstance(evidence, list) or not 2 <= len(evidence) <= 100:
            raise LedgerError("关系需提供双方原文依据")
        with self._connection(write=True) as db:
            if proposal_id is not None:
                existing = db.execute("SELECT * FROM experience_relations WHERE id=?", (proposal_id,)).fetchone()
                if existing is not None:
                    if any(existing[key] != value for key, value in (("left_id", left_id), ("left_revision", left_revision),
                                                                      ("right_id", right_id), ("right_revision", right_revision))):
                        raise LedgerError("建议 ID 已用于不同的经验修订组合", 409)
                    return self._relation(db, proposal_id)
            normalized = []
            for side, candidate_id, revision in (("left", left_id, left_revision), ("right", right_id, right_revision)):
                self._revision_record(db, candidate_id, revision)
                contexts = self._revision_contexts(db, candidate_id, revision)
                refs = []
                for ref in evidence:
                    if not isinstance(ref, dict) or ref.get("side") not in ("left", "right"):
                        raise LedgerError("关系证据必须标明 side=left/right")
                    if ref["side"] == side:
                        refs.append({**{k: v for k, v in ref.items() if k != "side"}, "field": side})
                if not refs:
                    raise LedgerError("关系需提供双方原文依据")
                refs = self._validate_evidence(db, contexts[0]["case_id"], refs, (side,), contexts=contexts)
                for ref in refs:
                    normalized.append({"side": side, **{k: v for k, v in ref.items() if k != "field"}})
            relation_id = _id() if proposal_id is None else proposal_id
            db.execute("INSERT INTO experience_relations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (relation_id, left_id, left_revision, right_id, right_revision, kind, "proposed", conditions, reason,
                        _json(normalized), actor, _now()))
            for case_id in {self._one(db, "candidates", left_id)["case_id"], self._one(db, "candidates", right_id)["case_id"]}:
                self._event(db, case_id, "relation.proposed", {"id": relation_id, "actor": actor})
            return self._relation(db, relation_id)

    def review_relation(self, relation_id, decision, reviewer, note, expected_status="proposed"):
        from .service import LedgerError, _id, _now, _text
        if decision not in ("accept", "reject", "resolve"):
            raise LedgerError("关系处理决定必须为 accept/reject/resolve")
        _text(reviewer, "审核人", limit=200)
        _text(note, "处理说明", limit=20000)
        with self._connection(write=True) as db:
            relation = self._one(db, "experience_relations", relation_id)
            if relation["status"] != expected_status:
                raise LedgerError("关系已被处理，请刷新后重试", 409)
            if decision == "resolve":
                if relation["status"] != "accepted":
                    raise LedgerError("仅可为已接收关系记录后续处理结果", 409)
                status = "resolved"
            else:
                if relation["status"] != "proposed":
                    raise LedgerError("该关系已有审核决定", 409)
                status = "accepted" if decision == "accept" else "rejected"
            db.execute("INSERT INTO relation_reviews VALUES(?,?,?,?,?,?)", (_id(), relation_id, decision, reviewer, note, _now()))
            db.execute("UPDATE experience_relations SET status=? WHERE id=?", (status, relation_id))
            for case_id in {self._one(db, "candidates", relation[side + "_id"])["case_id"] for side in ("left", "right")}:
                self._event(db, case_id, "relation.reviewed", {"id": relation_id, "decision": decision, "reviewer": reviewer, "note": note})
            return self._relation(db, relation_id)

    def _feedback(self, db, feedback_id):
        feedback = self._one(db, "experience_feedback", feedback_id)
        if feedback["actual_used"] is not None:
            feedback["actual_used"] = bool(feedback["actual_used"])
        return feedback

    def get_feedback(self, feedback_id):
        with self._connection() as db:
            return self._feedback(db, feedback_id)

    def _feedback_list(self, db, candidate_id, revision=None):
        self._one(db, "candidates", candidate_id)
        if revision is not None:
            self._revision_record(db, candidate_id, revision)
        rows = db.execute("SELECT id FROM experience_feedback WHERE candidate_id=? AND (? IS NULL OR revision=?) ORDER BY created_at,id",
                          (candidate_id, revision, revision)).fetchall()
        return [self._feedback(db, row[0]) for row in rows]

    def list_feedback(self, candidate_id, revision=None):
        with self._connection() as db:
            return self._feedback_list(db, candidate_id, revision)

    def add_feedback(self, candidate_id, revision, actor, original, actual_used, conditions, actions, outcome, case_id=None):
        from .service import LedgerError, _id, _now, _text
        _text(actor, "反馈人", limit=200)
        _text(original, "反馈原话")
        for label, value in (("使用条件", conditions), ("实际做法", actions), ("观察结果", outcome)):
            _text(value, label, empty=True)
        if actual_used is not None and type(actual_used) is not bool:
            raise LedgerError("actual_used 必须是布尔值或 null")
        with self._connection(write=True) as db:
            self._revision_record(db, candidate_id, revision)
            if not self._approved_revision(db, candidate_id, revision):
                raise LedgerError("使用反馈需绑定已获得人工确认的具体修订")
            candidate = self._one(db, "candidates", candidate_id)
            case_id = candidate["case_id"] if case_id is None else case_id
            self._case(db, case_id)
            feedback_id = _id()
            db.execute("INSERT INTO experience_feedback VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (feedback_id, candidate_id, revision, case_id, actor, original, actual_used, conditions, actions, outcome, _now()))
            self._event(db, case_id, "feedback.added", {"id": feedback_id, "candidate_id": candidate_id, "revision": revision, "actor": actor})
            return self._feedback(db, feedback_id)
