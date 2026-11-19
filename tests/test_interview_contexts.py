"""Synthetic acceptance cases for flexible interviews and explicit source scope."""

import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from experience_ledger.interviews.contracts import InterviewAction
from experience_ledger.interviews.planner import Proposal
from experience_ledger.interviews.runtime import InterviewRuntime
from experience_ledger.service import Ledger, LedgerError


class ScriptPlanner:
    configured = True
    model = "synthetic-context-tests"

    def __init__(self, *actions):
        self.actions = list(actions)
        self.contexts = []

    def propose(self, context, feedback=""):
        self.contexts.append(context)
        action = self.actions.pop(0)
        return Proposal(action(context) if callable(action) else action, self.model, 1)


def opening(question="请讲一次实际发生的经历。"):
    return {"action": "ask_expert", "reason": "先保存自由叙述", "question": question,
            "expected_information": "实际观察与选择"}


def ask(key="observation", question="当时看到什么？"):
    return {**opening(question), "gap_key": key,
            "gaps": [{"key": key, "label": "当时观察", "reason": "围绕当前判断核对"}]}


def pause():
    return {"action": "pause", "reason": "合成测试暂存，保留缺口"}


def create_context(ledger, title="合成案例"):
    case = ledger.create_case("general", title, "合成背景")
    ledger.add_source(case["id"], "合成案例资料", "合成现场记录：已经定位案例，访谈补充具体观察和判断。")
    decision = ledger.add_decision(case["id"], "合成判断", "具体观察后的选择")
    return case["id"], decision["id"]


def focused(runtime, case_id, decision_id, request_id="start"):
    return runtime.start({"goal": "合成经验", "request_id": request_id,
                          "case_id": case_id, "decision_id": decision_id})


def answer(runtime, session, text="看到接头松动后检查连接。", request_id="answer"):
    return runtime.answer(session["id"], {"question_id": session["pending"]["id"],
        "request_id": request_id, "answer": text, "speaker": "合成专家"})


def direct_action(runtime, sid, action, *, legacy_context=False):
    store = runtime.store
    token = store.acquire(sid)
    action_id = uuid4().hex
    try:
        context = {} if legacy_context else store.context(sid)
        store.call_record(sid, token, action_id, context, "synthetic", "synthetic")
        proposal = Proposal(action, "synthetic", 0)
        store.proposal(sid, token, action_id, proposal)
        result = store.apply(sid, token, action_id, InterviewAction.model_validate(action))
        return result, action_id
    finally:
        store.release(sid, token)


def test_free_opening_preserves_answer_before_any_case_exists(tmp_path):
    ledger = Ledger(tmp_path)
    runtime = InterviewRuntime(ledger, ScriptPlanner(pause()))
    session = runtime.start({"goal": "留下感知经验", "request_id": "free"})
    assert session["case_id"] is None and session["decision_id"] is None
    assert session["cases"] == session["gaps"] == []
    original = "凭手感🔧觉得不对，当时没有想过理由。"
    session = answer(runtime, session, original)
    assert session["turns"][0]["answer"] == original
    assert session["turns"][0]["case_id"] is None
    assert ledger.list_cases() == []
    assert session["status"] == "paused"


def test_single_original_has_multiple_exact_fragment_contexts(tmp_path):
    ledger = Ledger(tmp_path)
    a, da = create_context(ledger, "合成甲")
    b, dbid = create_context(ledger, "合成乙")
    runtime = InterviewRuntime(ledger, ScriptPlanner())
    session = runtime.start({"goal": "分别整理两次判断", "request_id": "multi", "case_ids": [a, b], "start_only": True})
    original = "甲看到🔧松动。乙听到异常。"
    payload = {"request_id": "raw", "answer": original, "speaker": "合成专家"}
    session = runtime.supplement(session["id"], payload)
    turn_id = session["turns"][0]["id"]
    split = original.index("乙")
    for case_id, decision_id, start, end in ((a, da, 0, split), (b, dbid, split, len(original))):
        session = runtime.link_turn(session["id"], {"expected_revision": session["revision"], "turn_id": turn_id,
            "start": start, "end": end, "case_id": case_id, "decision_id": decision_id, "actor": "合成编辑"})
    assert len(session["turns"]) == 1 and len(session["turn_contexts"]) == 2
    session = runtime.supplement(session["id"], payload)
    assert len(session["turns"]) == 1
    with pytest.raises(LedgerError, match="相同请求"):
        runtime.supplement(session["id"], {**payload, "answer": "改写"})
    session = runtime.supplement(session["id"], {"request_id": "fix", "answer": "补充：甲当时只是怀疑。", "speaker": "合成专家", "correction_of": turn_id})
    assert session["turns"][0]["answer"] == original
    assert session["turns"][1]["correction_of"] == turn_id


def test_shared_decision_gap_and_separate_interview_turns(tmp_path):
    ledger = Ledger(tmp_path)
    case_id, decision_id = create_context(ledger)
    first_runtime = InterviewRuntime(ledger, ScriptPlanner(ask(), pause()))
    first = answer(first_runtime, focused(first_runtime, case_id, decision_id))
    first_gap = next(g["id"] for g in first["gaps"] if g["key"] == "observation")
    second_runtime = InterviewRuntime(ledger, ScriptPlanner(ask(), pause()))
    second = focused(second_runtime, case_id, decision_id, "second")
    assert second["turns"] == []
    assert next(g["id"] for g in second["gaps"] if g["key"] == "observation") == first_gap
    second = answer(second_runtime, second, "后续补充的观察")
    assert len(second["turns"]) == len(first["turns"]) == 1
    assert second_runtime.store.budget_reason(second["id"]) == ""


def test_same_gap_key_is_scoped_to_decision(tmp_path):
    ledger = Ledger(tmp_path)
    a, da = create_context(ledger, "合成甲")
    b, dbid = create_context(ledger, "合成乙")
    runtime = InterviewRuntime(ledger, ScriptPlanner(ask()))
    session = focused(runtime, a, da)
    old_gap = session["pending"]["gap_id"]
    session = runtime.set_focus(session["id"], {"expected_revision": session["revision"], "case_id": b, "decision_id": dbid})
    runtime.planner = ScriptPlanner(ask())
    session = runtime.control(session["id"], {"command": "continue", "expected_revision": session["revision"]})
    assert session["pending"]["gap_id"] != old_gap
    assert len([g for g in session["gaps"] if g["key"] == "observation"]) == 2


def test_repeated_question_is_soft_warning_and_quality_correction_is_saved(tmp_path):
    ledger = Ledger(tmp_path)
    case_id, decision_id = create_context(ledger)
    runtime = InterviewRuntime(ledger, ScriptPlanner(ask(), ask()))
    first = focused(runtime, case_id, decision_id)
    session = answer(runtime, first)
    assert session["pending"]["quality_warnings"][0]["kind"] == "repeated"
    old_question = session["pending"]["id"]
    session = runtime.quality_feedback(session["id"], {"expected_revision": session["revision"],
        "question_id": old_question, "kind": "wrong_premise", "note": "我没有作出你假定的选择", "actor": "合成专家"})
    assert session["pending"] is None and len(session["quality_feedback"]) == 1
    with pytest.raises(LedgerError, match="问题已改变"):
        runtime.answer(session["id"], {"question_id": old_question, "request_id": "late", "answer": "迟到回答", "speaker": "合成专家"})


def test_finish_preserves_open_gaps_for_expert_decision(tmp_path):
    ledger = Ledger(tmp_path)
    case_id, decision_id = create_context(ledger)
    runtime = InterviewRuntime(ledger, ScriptPlanner(ask(), {"action": "suggest_finish", "reason": "专家暂时想不起更多，建议暂存并保留缺口。"}))
    session = answer(runtime, focused(runtime, case_id, decision_id))
    assert session["status"] == "paused"
    assert any(g["status"] == "open" for g in session["gaps"])
    session = runtime.control(session["id"], {"command": "close", "expected_revision": session["revision"]})
    assert session["status"] == "closed" and session["gaps"]


def test_model_link_proposal_does_not_authorize_existing_case(tmp_path):
    ledger = Ledger(tmp_path)
    case_id, decision_id = create_context(ledger)
    runtime = InterviewRuntime(ledger, ScriptPlanner({"action": "link_case", "case_id": case_id, "reason": "可能属于后续同一案例"}))
    session = runtime.start({"goal": "合成目标", "request_id": "proposal"})
    session = answer(runtime, session)
    assert session["cases"] == []
    proposal_id = session["focus_proposals"][0]["id"]
    session = runtime.set_focus(session["id"], {"expected_revision": session["revision"], "proposal_id": proposal_id})
    assert session["case_id"] == case_id and len(session["cases"]) == 1


def test_other_interview_cannot_read_or_expand_unlinked_fragment(tmp_path):
    ledger = Ledger(tmp_path)
    case_id, decision_id = create_context(ledger)
    runtime = InterviewRuntime(ledger, ScriptPlanner())
    first = runtime.start({"goal": "合成原话", "request_id": "owner", "case_ids": [case_id], "start_only": True})
    first = runtime.supplement(first["id"], {"request_id": "original", "answer": "公开片段。未关联的秘密片段。", "speaker": "合成专家"})
    turn = first["turns"][0]
    first = runtime.link_turn(first["id"], {"expected_revision": first["revision"], "turn_id": turn["id"], "start": 0, "end": 5,
        "case_id": case_id, "decision_id": decision_id, "actor": "合成编辑"})
    second = runtime.start({"goal": "合成后续", "request_id": "reader", "case_id": case_id, "decision_id": decision_id, "start_only": True})
    outcome, _ = direct_action(runtime, second["id"], {"action": "read_source", "source_type": "turn", "source_id": turn["id"], "reason": "核对已关联片段"})
    assert outcome["results"][0]["text"] == "公开片段。"
    with pytest.raises(LedgerError, match="读取位置"):
        direct_action(runtime, second["id"], {"action": "read_source", "source_type": "turn", "source_id": turn["id"], "offset": 6, "reason": "尝试越界"})
    second = runtime.store.get(second["id"])
    with pytest.raises(LedgerError, match="未获读取权限"):
        runtime.link_turn(second["id"], {"expected_revision": second["revision"], "turn_id": turn["id"], "start": 0, "end": len(turn["answer"]),
            "case_id": case_id, "decision_id": decision_id, "actor": "合成编辑"})


def test_selected_context_keeps_long_history_searchable(tmp_path):
    ledger = Ledger(tmp_path)
    runtime = InterviewRuntime(ledger, ScriptPlanner())
    session = runtime.start({"goal": "合成长访谈", "request_id": "long", "start_only": True})
    for index in range(24):
        text = ("早期独有线索" if index == 0 else "合成后续") + "内容" * 4500
        session = runtime.supplement(session["id"], {"request_id": f"long-{index}", "answer": text, "speaker": "合成专家"})
    context = runtime.store.context(session["id"])
    assert len(json.dumps(context, ensure_ascii=False)) < 60000
    assert context["earlier_turns_omitted"] > 0
    result, _ = direct_action(runtime, session["id"], {"action": "search_sources", "query": "早期独有线索", "reason": "检索被选择省略的早期原话"})
    assert result["results"][0]["source_id"] == session["turns"][0]["id"]
    assert result["results"][0]["text"].startswith("早期独有线索")


def test_legacy_cached_question_not_repeated_after_answer_or_focus_change(tmp_path):
    ledger = Ledger(tmp_path)
    case_id, decision_id = create_context(ledger)
    runtime = InterviewRuntime(ledger, ScriptPlanner())
    session = runtime.start({"goal": "合成旧缓存", "request_id": "legacy", "case_id": case_id, "decision_id": decision_id, "start_only": True})
    outcome, action_id = direct_action(runtime, session["id"], opening(), legacy_context=True)
    from experience_ledger.interviews.contracts import AnswerInterview
    runtime.store.answer(session["id"], AnswerInterview(question_id=outcome["question"]["id"], request_id="legacy-answer", answer="旧原话保留", speaker="合成专家"))
    token = runtime.store.acquire(session["id"])
    try:
        replay = runtime.store.apply(session["id"], token, action_id, InterviewAction.model_validate(opening()))
        assert replay["kind"] == "input_already_saved"
    finally:
        runtime.store.release(session["id"], token)
    session = runtime.store.get(session["id"])
    runtime.set_focus(session["id"], {"expected_revision": session["revision"], "case_id": case_id, "decision_id": decision_id})
    token = runtime.store.acquire(session["id"])
    try:
        assert runtime.store.apply(session["id"], token, action_id, InterviewAction.model_validate(opening()))["kind"] == "context_changed"
    finally:
        runtime.store.release(session["id"], token)
    assert runtime.store.get(session["id"])["turns"][0]["answer"] == "旧原话保留"


def test_unfocused_gap_does_not_fabricate_case(tmp_path):
    ledger = Ledger(tmp_path)
    runtime = InterviewRuntime(ledger, ScriptPlanner())
    session = runtime.start({"goal": "合成未知领域", "request_id": "no-fake", "start_only": True})
    with pytest.raises(LedgerError, match="案例尚未"):
        direct_action(runtime, session["id"], ask())
    assert ledger.list_cases() == []


def test_structure_focus_confirmation_and_candidate_complete_cycle(tmp_path):
    ledger = Ledger(tmp_path)

    def create_decision(context):
        return {"action": "create_decision", "case_id": context["last_tool"]["case_id"],
                "title": "合成判断", "description": "看到接头松动，决定先检查连接。", "reason": "从原话整理具体选择"}

    def link(context):
        case = context["cases"][0]
        return {"action": "link_turn", "turn_id": context["turns"][0]["id"], "start": 0,
                "end": len(context["turns"][0]["answer"]), "case_id": case["id"],
                "decision_id": case["decisions"][0]["id"], "reason": "关联专家原话"}

    def suggest(context):
        case = context["cases"][0]
        return {"action": "suggest_focus", "case_id": case["id"], "decision_id": case["decisions"][0]["id"],
                "summary": "已记录看到松动后先检查连接。", "remaining": ["适用条件尚未说明"], "reason": "请专家确认继续重点"}

    def draft(context):
        turn = context["turns"][0]
        return {"action": "draft_experience", "title": "合成个案经验", "kind": "case_note", "reason": "保留未知形成待审核草稿",
                "content": [{"key": f["key"], "text": turn["answer"] if f["key"] == "statement" else "未知，尚未说明。",
                             "status": "supported" if f["key"] == "statement" else "unknown"} for f in context["experience_fields"]],
                "contexts": [context["focus"]],
                "evidence": [{"field": "statement", "source_type": "turn", "source_id": turn["id"], "quote": turn["answer"]}]}

    planner = ScriptPlanner({"action": "create_case", "title": "合成排查经历", "description": "看到松动，先检查连接。", "reason": "原话已有具体经历"},
                            create_decision, link, suggest, draft)
    runtime = InterviewRuntime(ledger, planner)
    session = answer(runtime, runtime.start({"goal": "合成判断经验", "request_id": "cycle"}))
    assert session["status"] == "paused" and session["case_id"] is None
    assert len(session["turns"]) == 1 and len(session["cases"]) == 1
    proposal = session["focus_proposals"][0]
    session = runtime.set_focus(session["id"], {"expected_revision": session["revision"], "proposal_id": proposal["id"]})
    session = runtime.control(session["id"], {"command": "draft", "expected_revision": session["revision"]})
    assert session["status"] == "review"
    candidate = ledger.get_candidate(session["candidate_ids"][0])
    assert candidate["status"] == "pending" and candidate["validation_status"] == "unvalidated"
    assert candidate["evidence"][0]["source_id"] == session["turns"][0]["id"]
    assert candidate["contexts"][0]["case_id"] == session["case_id"]


def test_out_of_scope_topic_is_saved_without_new_case(tmp_path):
    ledger = Ledger(tmp_path)
    runtime = InterviewRuntime(ledger, ScriptPlanner({"action": "defer_topic", "title": "另一领域的经历", "description": "本次不展开。", "reason": "超出目标"}, opening()))
    session = runtime.start({"goal": "只整理连接排查", "request_id": "topic"})
    session = answer(runtime, session)
    assert session["deferred_topics"][0]["title"] == "另一领域的经历"
    assert session["cases"] == [] and session["status"] == "waiting"


def test_expert_can_reject_focus_switch_and_continue_current_judgment(tmp_path):
    ledger = Ledger(tmp_path)
    a, da = create_context(ledger, "合成当前案例")
    b, dbid = create_context(ledger, "合成建议案例")
    runtime = InterviewRuntime(ledger, ScriptPlanner())
    session = runtime.start({"goal": "合成当前目标", "request_id": "reject-focus", "case_id": a, "decision_id": da, "start_only": True})
    outcome, _ = direct_action(runtime, session["id"], {"action": "suggest_focus", "case_id": b, "decision_id": dbid,
        "summary": "建议转到另一案例。", "remaining": ["当前案例还有依据没说清"], "reason": "合成焦点建议"})
    session = runtime.store.get(session["id"])
    session = runtime.set_focus(session["id"], {"expected_revision": session["revision"], "proposal_id": outcome["proposal_id"],
        "decision": "reject", "note": "先继续当前判断。"})
    assert session["case_id"] == a and session["decision_id"] == da
    assert [case["id"] for case in session["cases"]] == [a]
    assert session["focus_proposals"][0]["status"] == "rejected"
    assert session["events"][-1]["payload"]["note"] == "先继续当前判断。"
    assert runtime.store.context(session["id"])["focus_proposals"][0]["status"] == "rejected"


def test_session_candidates_exclude_other_interviews_and_manual_case_candidates(tmp_path):
    ledger = Ledger(tmp_path)
    case_id, decision_id = create_context(ledger)
    gap = next(g for g in ledger.get_case(case_id)["gaps"] if g["kind"] == "action")
    ledger.add_turn(case_id, gap["id"], "合成旧经历：先检查连接。", "合成旧专家")
    old_ids = {ledger.draft_candidate(case_id, decision_id)["id"] for _ in range(2)}
    runtime = InterviewRuntime(ledger, ScriptPlanner())
    first = runtime.start({"goal": "本场合成整理", "request_id": "candidate-owner", "case_id": case_id,
                           "decision_id": decision_id, "start_only": True})
    assert first["candidate_ids"] == [] and set(first["related_candidate_ids"]) == old_ids
    first = runtime.supplement(first["id"], {"request_id": "new-original", "answer": "本场合成原话：我看到松动。", "speaker": "合成本场专家"})
    turn = first["turns"][0]
    action = {"action": "draft_experience", "title": "本场合成候选", "reason": "只整理本场原话", "kind": "case_note",
              "contexts": [{"case_id": case_id, "decision_id": decision_id}],
              "content": [{"key": "statement", "text": turn["answer"], "status": "supported"}],
              "evidence": [{"field": "statement", "source_type": "turn", "source_id": turn["id"], "quote": turn["answer"]}]}
    outcome, action_id = direct_action(runtime, first["id"], action)
    first = runtime.store.get(first["id"])
    assert first["candidate_ids"] == [outcome["candidate_id"]]
    assert set(first["related_candidate_ids"]) == old_ids
    second = runtime.start({"goal": "另一场合成整理", "request_id": "another-candidate-owner", "case_id": case_id,
                            "decision_id": decision_id, "start_only": True})
    assert second["candidate_ids"] == []
    assert set(second["related_candidate_ids"]) == old_ids | {outcome["candidate_id"]}

    # Replay an already committed business outcome without relying on a graph
    # checkpoint. It must recover this session's candidate, never draft again.
    from experience_ledger.interviews.contracts import ControlInterview
    runtime.store.control(first["id"], ControlInterview(command="continue", expected_revision=first["revision"]))
    token = runtime.store.acquire(first["id"])
    try:
        replay = runtime.store.apply(first["id"], token, action_id, InterviewAction.model_validate(action))
        assert replay["candidate_id"] == outcome["candidate_id"]
    finally:
        runtime.store.release(first["id"], token)
    assert runtime.store.get(first["id"])["candidate_ids"] == [outcome["candidate_id"]]
    assert len(ledger.get_case(case_id)["candidates"]) == 3


def test_v3_sources_and_persisted_wait_checkpoint_resume_without_reasking(tmp_path):
    package = Path(__file__).parents[1] / "src" / "experience_ledger"
    domain = (package / "domains" / "general.json").read_text(encoding="utf-8")
    pending = {"id": "q-old", "gap_id": "g", "gap_key": "observation", "question": "合成旧问题", "hypothesis": "",
               "reason": "保留旧问题", "expected_information": "旧观察"}
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        for path in (package / "schema.sql", package / "migrations" / "002_interviews.sql", package / "migrations" / "003_evidence_fields.sql"):
            db.executescript(path.read_text(encoding="utf-8"))
        db.execute("INSERT INTO cases VALUES('c','general','合成旧案例','旧背景',?,'2026-01-01')", (domain,))
        db.execute("INSERT INTO decisions VALUES('d','c','合成旧判断','旧选择','2026-01-01')")
        db.execute("INSERT INTO gaps VALUES('g','c','d','observation','旧关注点','answered')")
        db.execute("INSERT INTO turns VALUES('t-old','c','g','d',1,'interview','旧已答问题','旧原话🔧保留','合成专家','2026-01-01','')")
        db.execute("""INSERT INTO interviews(id,case_id,decision_id,goal,scope,status,limits_json,pending_json,created_at,updated_at)
            VALUES('s-old','c','d','旧目标','旧目标','waiting',?,?,'2026-01-01','2026-01-01')""",
            (json.dumps({"max_calls": 40, "max_turns": 30, "max_tokens": 60000, "steps_per_run": 5}), json.dumps(pending, ensure_ascii=False)))
        db.execute("INSERT INTO interview_gaps VALUES('g','s-old','observation','旧关注点',1,'open','待核对','[]')")
        db.execute("INSERT INTO interview_questions VALUES('q-prev','s-old','g','旧已答问题','','旧理由','旧信息','2026-01-01')")
        db.execute("INSERT INTO interview_questions VALUES('q-old','s-old','g','合成旧问题','','保留旧问题','旧观察','2026-01-01')")
        db.execute("INSERT INTO interview_inputs VALUES('s-old','old-answer','oldhash','q-prev','t-old','answer','2026-01-01')")
        db.execute("INSERT INTO interview_gap_turns VALUES('g','t-old')")
        before_turn = tuple(db.execute("SELECT * FROM turns WHERE id='t-old'").fetchone())

    # Recreate a v0.2 wait checkpoint using the same durable node and state names.
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt
    from langgraph.checkpoint.sqlite import SqliteSaver
    from experience_ledger.interviews.runtime import FlowState
    with SqliteSaver.from_conn_string(str(tmp_path / "interview-checkpoints.sqlite3")) as saver:
        builder = StateGraph(FlowState)
        builder.add_node("wait", lambda state: interrupt(state["outcome"]["question"]))
        builder.add_edge(START, "wait")
        builder.add_edge("wait", END)
        builder.compile(checkpointer=saver).invoke({"interview_id": "s-old", "run_id": "legacy-run", "step": 1,
            "outcome": {"kind": "ask_expert", "question": pending}}, {"configurable": {"thread_id": "s-old"}})

    ledger = Ledger(tmp_path)
    with ledger._connection() as db:
        after = tuple(db.execute("SELECT * FROM turns WHERE id='t-old'").fetchone())
        assert after[:len(before_turn)] == before_turn
        assert db.execute("SELECT status FROM gaps WHERE id='g'").fetchone()[0] == "answered"
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None
    planner = ScriptPlanner(pause())
    runtime = InterviewRuntime(ledger, planner)
    session = runtime.answer("s-old", {"question_id": "q-old", "request_id": "new-answer", "answer": "合成恢复后的新回答", "speaker": "合成专家"})
    assert session["status"] == "paused" and len(session["turns"]) == 2
    assert len(session["questions"]) == 2 and len(planner.contexts) == 1
    assert session["turns"][0]["id"] == "t-old" and session["turns"][0]["answer"] == "旧原话🔧保留"
