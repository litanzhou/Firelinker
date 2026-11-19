"""One selected pair, one durable proposal; never compare unseen draft history."""

from hashlib import sha256
import json
import time

from pydantic import ValidationError

from .interviews.contracts import InterviewAction
from .interviews.planner import PROMPT_VERSION, PlannerError, Proposal
from .service import LedgerError, _id, _json, _now, _text


def comparison_view(candidate):
    visible = {key: candidate[key] for key in ("id", "revision", "domain_id", "title", "kind", "content", "contexts")}
    visible["evidence"] = [
        {key: ref[key] for key in ("field", "source_type", "source_id", "start", "end", "quote", "locator") if key in ref}
        for ref in candidate["evidence"]
    ]
    return visible


def compare_experiences(ledger, planner, selected, request_id):
    _text(request_id, "比较请求 ID", limit=128)
    if selected["left_id"] == selected["right_id"] and selected["left_revision"] == selected["right_revision"]:
        raise LedgerError("请选择两个不同的经验修订进行比较。")
    payload_hash = sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest()
    relation_id = sha256(("comparison:" + request_id).encode()).hexdigest()
    token = _id()
    with ledger._connection(write=True) as db:
        row = db.execute("SELECT * FROM comparison_requests WHERE id=?", (request_id,)).fetchone()
        previous = dict(row) if row else None
        if previous and previous["payload_hash"] != payload_hash:
            raise LedgerError("同一比较请求 ID 的版本选择不同", 409)
        if previous:
            relation = db.execute("SELECT id FROM experience_relations WHERE id=?", (relation_id,)).fetchone()
            if relation:
                db.execute("UPDATE comparison_requests SET status='completed',error='',updated_at=? WHERE id=?", (_now(), request_id))
                return ledger._relation(db, relation_id)
            if previous["status"] == "running" and previous["lease_until"] > time.time():
                raise LedgerError("本次比较仍在运行，请稍后重试同一请求", 409)
        if not planner.configured:
            raise LedgerError("请先配置你选择的模型；也可以直接手工提出关联。")
        if row is None:
            db.execute("""INSERT INTO comparison_requests
                (id,payload_hash,selection_json,status,run_token,lease_until,relation_id,prompt_version,created_at,updated_at)
                VALUES(?,?,?,'running',?,?,?,?,?,?)""",
                (request_id, payload_hash, _json(selected), token, time.time()+120, relation_id, PROMPT_VERSION, _now(), _now()))
        else:
            db.execute("UPDATE comparison_requests SET status='running',run_token=?,lease_until=?,updated_at=? WHERE id=?",
                       (token, time.time()+120, _now(), request_id))
    left = None
    try:
        left = ledger.get_candidate(selected["left_id"], selected["left_revision"], include_history=False)
        right = ledger.get_candidate(selected["right_id"], selected["right_revision"], include_history=False)
        if previous and previous["action_json"]:
            proposal = Proposal(json.loads(previous["action_json"]), previous["model"], previous["tokens"])
        else:
            proposal = planner.propose({"requested_action":"compare_experiences", "left":comparison_view(left), "right":comparison_view(right)})
            with ledger._connection(write=True) as db:
                current = ledger._one(db, "comparison_requests", request_id)
                if current["run_token"] != token:
                    raise LedgerError("比较执行状态已变化，请读取当前记录", 409)
                db.execute("UPDATE comparison_requests SET action_json=?,model=?,tokens=?,updated_at=? WHERE id=?",
                           (_json(proposal.action), proposal.model, proposal.tokens, _now(), request_id))
        action = InterviewAction.model_validate(proposal.action)
        if action.action != "propose_relation" or any(getattr(action, key) != value for key, value in selected.items()):
            raise LedgerError("模型未对选定的两个修订提出关系建议；未修改任何经验。")
        with ledger._connection() as db:
            if ledger._one(db, "comparison_requests", request_id)["run_token"] != token:
                raise LedgerError("比较执行状态已变化，请读取当前记录", 409)
        result = ledger.propose_relation(**selected, kind=action.relation_kind, conditions=action.conditions,
                    reason=action.reason, evidence=action.relation_evidence, actor="AI："+proposal.model, proposal_id=relation_id)
        with ledger._connection(write=True) as db:
            db.execute("UPDATE comparison_requests SET status='completed',error='',updated_at=? WHERE id=? AND run_token=?", (_now(), request_id, token))
            ledger._event(db, left["case_id"], "relation.model_proposed", {
                "id":result["id"], "request_id":request_id, "model":proposal.model,
                "prompt_version":PROMPT_VERSION, "tokens":proposal.tokens})
        return result
    except (LedgerError, PlannerError, ValidationError) as exc:
        message = str(exc) if not isinstance(exc, ValidationError) else "模型关系建议结构无效，请重试或手工记录。"
        with ledger._connection(write=True) as db:
            current = ledger._one(db, "comparison_requests", request_id)
            if current["run_token"] == token:
                # Keep failed output in the local event audit; a deliberate retry
                # may call the model again to repair invalid output.
                if left:
                    ledger._event(db, left["case_id"], "relation.comparison_failed", {
                        "request_id":request_id, "error":message, "action":json.loads(current["action_json"] or "null")})
                db.execute("UPDATE comparison_requests SET status='error',action_json=NULL,error=?,updated_at=? WHERE id=?",
                           (message, _now(), request_id))
        if isinstance(exc, LedgerError):
            raise
        raise LedgerError(message, 502) from None
