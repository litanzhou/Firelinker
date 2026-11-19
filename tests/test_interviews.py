"""Workflow/transport acceptance with scripted models and synthetic content.

These tests prove execution policy and provenance, not real-model interview quality.
"""

import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from experience_ledger.interviews.planner import Proposal, PlannerError, UnconfiguredPlanner
from experience_ledger.interviews.runtime import InterviewRuntime
from experience_ledger.service import Ledger, LedgerError


class ScriptPlanner:
    configured = True
    model = "scripted-test-model"

    def __init__(self, *actions):
        self.actions = list(actions)
        self.contexts = []
        self.feedback = []

    def propose(self, context, feedback=""):
        self.contexts.append(context)
        self.feedback.append(feedback)
        item = self.actions.pop(0)
        if isinstance(item, Exception):
            raise item
        return Proposal(item(context) if callable(item) else item, self.model, 10)



def known_context(ledger, payload):
    """Historical graph regressions start with an explicitly selected synthetic case.

    Free narration and model-established cases are exercised in test_interview_contexts.
    This fixture matches the old empty decision without generating manual templates.
    """
    from experience_ledger.service import _id, _now
    payload = dict(payload)
    if payload.get("case_id"):
        return payload
    title = "合成已定位案例：" + payload["request_id"]
    existing = next((c for c in ledger.list_cases() if c["title"] == title), None)
    if existing:
        case = ledger.get_case(existing["id"])
        decision_id = case["decisions"][0]["id"]
    else:
        case = ledger.create_case(payload.get("domain_id", "general"), title, "已明确定位的合成案例")
        ledger.add_source(case["id"], "合成已提供资料", "合成现场记录：本次围绕已定位的判断补充观察与依据。")
        decision_id = _id()
        with ledger._connection(write=True) as db:
            db.execute("INSERT INTO decisions VALUES(?,?,?,?,?)", (decision_id, case["id"], "合成判断", "合成背景", _now()))
    payload.update(case_id=case["id"], decision_id=decision_id)
    return payload


def _start_known(runtime, payload):
    return runtime.start(known_context(runtime.ledger, payload))


def ask(key="incident", question="请讲述最近一次需要作出判断的具体经历。"):
    return {"action": "ask_expert", "reason": "先取得具体情境", "gap_key": key, "question": question,
            "expected_information": "当时的观察与实际选择", "gaps": [{"key": key, "label": "具体经历", "reason": "目标需要具体案例"}]}


def draft(context):
    turn = context["turns"][0]
    return {"action": "draft_experience", "reason": "整理已有原话，保留未知", "title": "合成待审核经验", "kind": "case_note",
            "content": [{"key": f["key"], "text": turn["answer"] if f["key"] == "statement" else "尚未说明，不能据此推定适用条件。",
                         "status": "supported" if f["key"] == "statement" else "unknown"} for f in context["experience_fields"]],
            "evidence": [{"field": "statement", "source_type": "turn", "source_id": turn["id"], "quote": turn["answer"]}]}


def answer(runtime, session, request_id="answer-1", text="合成经历：我先检查连接，因为看到连接处松动。"):
    return runtime.answer(session["id"], {"question_id": session["pending"]["id"], "request_id": request_id, "answer": text, "speaker": "合成专家"})


def control(runtime, session, command):
    return runtime.control(session["id"], {"command": command, "expected_revision": session["revision"]})


@pytest.mark.parametrize("domain", ["electronics_repair", "oral_history", "general"])
def test_goal_to_dynamic_interview_restart_draft_and_human_review(tmp_path, domain):
    ledger = Ledger(tmp_path)
    planner = ScriptPlanner(ask(), {"action": "search_sources", "reason": "查原始资料", "query": "合成"}, ask("rationale", "当时哪些观察支持了这个选择？"))
    runtime = InterviewRuntime(ledger, planner)
    session = _start_known(runtime, {"goal": "合成目标：传承判断过程", "domain_id": domain, "request_id": "start"})
    assert session["status"] == "waiting" and len(session["gaps"]) == 1
    assert len(ledger.get_case(session["case_id"])["gaps"]) == 1  # Not five forced template gaps.
    source = ledger.add_source(session["case_id"], "合成手册", "合成资料：可以先观察连接处是否松动。")
    session = answer(runtime, session)
    assert session["status"] == "waiting" and len(session["turns"]) == 1
    assert session["gaps"][0]["status"] == "open"  # A response alone does not resolve a gap.
    with pytest.raises(LedgerError):
        ledger.draft_candidate(session["case_id"], session["decision_id"])
    results = planner.contexts[-1]["last_tool"]["results"]
    assert results[0]["source_id"] == source["id"] and "连接" in results[0]["text"]
    assert (tmp_path / "interview-checkpoints.sqlite3").exists()

    restarted = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(draft))
    session = answer(restarted, session, "answer-2", "合成补充：看到接头有明显晃动。")
    assert session["status"] == "review" and len(session["turns"]) == 2
    assert len(session["candidate_ids"]) == 1
    candidate = ledger.get_candidate(session["candidate_ids"][0])
    assert candidate["structured"] and candidate["status"] == "pending"
    assert ledger.search_knowledge() == []
    approved = ledger.review_candidate(candidate["id"], 1, "approve", "测试模拟审核人", "测试模拟：确认忠实表达，并保留未知。")
    assert approved["validation_status"] == "unvalidated"
    assert len(ledger.search_knowledge(domain_id=domain)) == 1
    # A new draft preserves the previously approved revision in retrieval.
    revised = ledger.revise_content(candidate["id"], 1, "合成修订", candidate["kind"], candidate["content"], candidate["evidence"], "测试模拟编辑")
    assert revised["revision"] == 2 and ledger.search_knowledge()[0]["revision"] == 1
    assert len(revised["reviews"]) == 1 and revised["revisions"][0]["content_schema"]


def test_answer_idempotency_and_stale_question(tmp_path):
    ledger = Ledger(tmp_path)
    planner = ScriptPlanner(ask(), ask("next", "下一处需要解释的观察是什么？"))
    runtime = InterviewRuntime(ledger, planner)
    first = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    second = answer(runtime, first)
    repeated = answer(runtime, first)
    assert repeated["pending"]["id"] == second["pending"]["id"] and len(repeated["turns"]) == 1
    assert len(planner.contexts) == 2
    with pytest.raises(LedgerError):
        answer(runtime, first, "different-request")
    with pytest.raises(LedgerError):
        answer(runtime, first, text="同一请求却改了内容")


def test_model_failure_preserves_answer_and_can_retry_after_restart(tmp_path):
    ledger = Ledger(tmp_path)
    runtime = InterviewRuntime(ledger, ScriptPlanner(ask(), PlannerError("合成模型断线")))
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    session = answer(runtime, session)
    assert session["status"] == "error" and len(session["turns"]) == 1
    restarted = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(draft))
    session = control(restarted, session, "continue")
    assert session["status"] == "review" and len(session["turns"]) == 1


@pytest.mark.parametrize("violation", ["cross_case", "quote", "unknown_source", "unknown_action"])
def test_invalid_actions_are_repaired_once_then_paused_without_partial_writes(tmp_path, violation):
    ledger = Ledger(tmp_path)
    other = ledger.create_case("general", "另一个合成案例", "合成")
    source = ledger.add_source(other["id"], "另一个案例的资料", "来自另一案例的原文")
    def bad(context):
        if violation == "unknown_source":
            return {"action": "read_source", "reason": "测试不存在的来源", "source_id": "missing-source"}
        if violation == "unknown_action":
            return {"action": "approve_experience", "reason": "试图自动审核"}
        item = draft(context)
        item["gaps"] = [{"key": "rollback_me", "label": "不应部分写入", "reason": "测试"}]
        if violation == "cross_case":
            item["evidence"] = [{"field": "statement", "source_type": "document", "source_id": source["id"], "quote": source["text"]}]
        else:
            item["evidence"][0]["quote"] = "原文中不存在的引文"
        return item
    planner = ScriptPlanner(ask(), bad, bad)
    runtime = InterviewRuntime(ledger, planner)
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    session = answer(runtime, session)
    assert session["status"] == "paused" and not session["candidate_ids"]
    assert [g["key"] for g in session["gaps"]] == ["incident"]
    assert planner.feedback[-1] and session["calls"] == 3


def test_repeat_question_is_a_quality_warning(tmp_path):
    planner = ScriptPlanner(ask(), ask(), {"action": "pause", "reason": "没有新的追问角度，保留未知。"})
    runtime = InterviewRuntime(Ledger(tmp_path), planner)
    session = answer(runtime, _start_known(runtime, {"goal": "合成目标", "request_id": "start"}))
    assert session["status"] == "waiting" and len(session["questions"]) == 2
    assert session["pending"]["quality_warnings"]
    assert session["calls"] == 2


def test_pause_resume_skip_close_and_exclusive_execution(tmp_path):
    runtime = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(ask(), ask("another", "请谈谈另一次观察。")))
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    session = control(runtime, session, "pause")
    assert session["status"] == "paused" and session["pending"]
    session = control(runtime, session, "continue")
    assert session["status"] == "waiting"  # Resume the same question without consuming a model call.
    session = control(runtime, session, "skip")
    assert session["gaps"][0]["status"] == "deferred" and session["turns"] == []
    session = control(runtime, session, "close")
    assert session["status"] == "closed"
    with pytest.raises(LedgerError):
        runtime.run(session["id"])
    # A second store/process cannot concurrently execute the same interview.
    fresh = runtime.store.start(__import__('experience_ledger.interviews.contracts', fromlist=['StartInterview']).StartInterview(goal="合成", request_id="exclusive"))
    token = runtime.store.acquire(fresh)
    with pytest.raises(LedgerError):
        InterviewRuntime(Ledger(tmp_path), ScriptPlanner()).store.acquire(fresh)
    runtime.store.release(fresh, token)


def test_resource_budget_pauses_instead_of_declaring_completion(tmp_path):
    runtime = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(ask()))
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start", "limits": {"max_calls": 1}})
    session = answer(runtime, session)
    assert session["status"] == "paused" and session["calls"] == 1 and len(session["turns"]) == 1
    assert "上限" in session["notice"]


def test_unconfigured_is_explicit_and_does_not_use_model_budget(tmp_path):
    runtime = InterviewRuntime(Ledger(tmp_path), UnconfiguredPlanner())
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    assert session["status"] == "error" and session["calls"] == 0
    assert "尚未配置" in session["notice"]


def test_schema_v1_migration_preserves_original_records(tmp_path):
    schema = Path(__import__('experience_ledger').__file__).with_name("schema.sql").read_text(encoding="utf-8")
    db = sqlite3.connect(tmp_path / "ledger.sqlite3")
    try:
        db.executescript(schema)
        profile = json.loads((Path(__import__('experience_ledger').__file__).parent / "domains" / "oral_history.json").read_text(encoding="utf-8"))
        db.execute("INSERT INTO cases VALUES('old','oral_history','旧合成案例','旧背景',?,'2026-01-01')", (json.dumps(profile),))
        db.commit()
    finally:
        db.close()
    ledger = Ledger(tmp_path)
    assert ledger.get_case("old")["title"] == "旧合成案例"
    with ledger._connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_custom_domain_fields_and_gap_types_without_core_changes(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    profile = {"id": "exploration", "name": "合成探索任务", "version": "1", "description": "合成", "object_label": "方案",
               "prompts": {"hypothesis": "当时想检验什么假设？"},
               "experience_fields": [{"key": "statement", "label": "假设", "required": True}, {"key": "acceptance", "label": "接受标准", "required": True}]}
    (profiles / "domain.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    ledger = Ledger(tmp_path / "records", profiles)
    runtime = InterviewRuntime(ledger, ScriptPlanner(ask("hypothesis"), draft))
    session = _start_known(runtime, {"goal": "合成探索目标", "domain_id": "exploration", "request_id": "start"})
    session = answer(runtime, session)
    candidate = ledger.get_candidate(session["candidate_ids"][0])
    assert [f["key"] for f in candidate["content"]] == ["statement", "acceptance"]
    assert candidate["content"][1]["status"] == "unknown"


def test_saved_action_replay_does_not_duplicate_candidate(tmp_path, monkeypatch):
    runtime = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(ask(), draft))
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    apply = runtime.store.apply
    crashed = False
    def crash_after_write(*args):
        nonlocal crashed
        result = apply(*args)
        if result["kind"] == "draft_experience" and not crashed:
            crashed = True
            raise RuntimeError("synthetic crash after business commit")
        return result
    monkeypatch.setattr(runtime.store, "apply", crash_after_write)
    with pytest.raises(RuntimeError):
        answer(runtime, session)
    saved = runtime.store.get(session["id"])
    assert len(saved["candidate_ids"]) == 1
    recovered = control(runtime, saved, "continue")
    assert recovered["candidate_ids"] == saved["candidate_ids"]
    assert recovered["status"] == "review"
    assert len(recovered["questions"]) == 1


def test_answered_question_is_not_repeated_after_crash_before_checkpoint(tmp_path, monkeypatch):
    runtime = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(ask(), draft))
    apply = runtime.store.apply
    crashed = False
    def crash_after_question(*args):
        nonlocal crashed
        result = apply(*args)
        if result["kind"] == "ask_expert" and not crashed:
            crashed = True
            raise RuntimeError("synthetic crash before checkpoint")
        return result
    monkeypatch.setattr(runtime.store, "apply", crash_after_question)
    with pytest.raises(RuntimeError):
        _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    sid = runtime.store.list()[0]["id"]
    saved = runtime.store.get(sid)
    assert saved["status"] == "waiting"
    recovered = answer(runtime, saved)
    assert recovered["status"] == "review" and len(recovered["questions"]) == 1


def test_user_stop_during_model_call_prevents_late_action(tmp_path):
    started, release = Event(), Event()
    def delayed(context):
        started.set()
        assert release.wait(timeout=10)
        return draft(context)
    runtime = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(ask(), delayed))
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(answer, runtime, session)
        try:
            assert started.wait(timeout=10)
            running = runtime.store.get(session["id"])
            assert running["status"] == "running" and len(running["turns"]) == 1
            stopped = control(runtime, running, "close")
            assert stopped["status"] == "closed"
        finally:
            release.set()
        with pytest.raises(LedgerError):
            future.result(timeout=10)
    saved = runtime.store.get(session["id"])
    assert saved["status"] == "closed" and not saved["candidate_ids"]


def test_semantic_contract_error_can_be_repaired_without_serialization_failure(tmp_path):
    runtime = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(
        {"action": "ask_expert", "reason": "缺必需字段"}, ask()))
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    assert session["status"] == "waiting" and session["calls"] == 2


def test_multiple_gaps_can_link_to_one_turn(tmp_path):
    def cover(context):
        turn = context["turns"][0]
        action = draft(context)
        action["gaps"] = [{"key": key, "label": key, "status": "covered", "reason": "同一原话支持两个方面",
                          "evidence": [{"source_type": "turn", "source_id": turn["id"], "quote": turn["answer"]}]} for key in ("incident", "rationale")]
        return action
    ledger = Ledger(tmp_path)
    runtime = InterviewRuntime(ledger, ScriptPlanner(ask(), cover))
    session = answer(runtime, _start_known(runtime, {"goal": "合成目标", "request_id": "start"}))
    assert all(g["status"] == "covered" for g in session["gaps"])
    with ledger._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM interview_gap_turns").fetchone()[0] == 2


def test_user_can_request_draft_while_another_question_is_pending(tmp_path):
    planner = ScriptPlanner(ask(), ask("another", "还有什么观察？"), draft)
    runtime = InterviewRuntime(Ledger(tmp_path), planner)
    session = answer(runtime, _start_known(runtime, {"goal": "合成目标", "request_id": "start"}))
    assert session["status"] == "waiting" and len(session["turns"]) == 1
    session = control(runtime, session, "draft")
    assert session["status"] == "review" and len(session["turns"]) == 1
    assert planner.contexts[-1]["requested_action"] == "draft"
    assert all(g["status"] == "open" for g in session["gaps"])


def test_keyword_search_defaults_to_latest_version_but_can_read_old_version(tmp_path):
    ledger = Ledger(tmp_path)
    planner = ScriptPlanner(ask(), {"action": "search_sources", "query": "过时指引", "reason": "检查关键词"}, {"action": "pause", "reason": "无结果，保留未知"})
    runtime = InterviewRuntime(ledger, planner)
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    old = ledger.add_source(session["case_id"], "合成资料", "过时指引")
    ledger.add_source(session["case_id"], "合成资料", "最新指引", source_id=old["source_id"])
    session = answer(runtime, session)
    assert planner.contexts[-1]["last_tool"]["results"] == []
    reader = ScriptPlanner({"action": "read_source", "source_id": old["id"], "source_type": "document", "reason": "明确读取历史版本"}, {"action": "pause", "reason": "保留旧版来源"})
    runtime.planner = reader
    control(runtime, session, "continue")
    assert reader.contexts[-1]["last_tool"]["results"][0]["text"] == "过时指引"


def test_all_domain_fields_can_save_evidence_and_be_reviewed(tmp_path):
    def fully_supported(context):
        action = draft(context)
        for value in action["content"]:
            value.update(text=context["turns"][0]["answer"], status="supported")
        action["evidence"] = [{**action["evidence"][0], "field": value["key"]} for value in action["content"]]
        return action
    ledger = Ledger(tmp_path)
    runtime = InterviewRuntime(ledger, ScriptPlanner(ask(), fully_supported))
    session = answer(runtime, _start_known(runtime, {"goal": "合成领域字段引用测试", "request_id": "start"}))
    assert session["status"] == "review"
    candidate = ledger.get_candidate(session["candidate_ids"][0])
    expected = {f["key"] for f in candidate["content_schema"]}
    assert {ref["field"] for ref in candidate["evidence"]} == expected
    assert {"context", "reasoning", "boundaries"} <= expected
    reviewed = ledger.review_candidate(candidate["id"], 1, "approve", "测试模拟审核", "仅测试任意字段的引用存取")
    assert reviewed["status"] == "approved"
    assert {r["field"] for r in Ledger(tmp_path).get_candidate(candidate["id"])["evidence"]} == expected


def test_v2_migration_keeps_all_old_evidence(tmp_path):
    package = Path(__import__('experience_ledger').__file__).parent
    profile = json.loads((package / "domains" / "general.json").read_text(encoding="utf-8"))
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as db:
        db.executescript((package / "schema.sql").read_text(encoding="utf-8"))
        db.executescript((package / "migrations" / "002_interviews.sql").read_text(encoding="utf-8"))
        db.execute("INSERT INTO cases VALUES('c','general','旧案例','旧背景',?,'old')", (json.dumps(profile),))
        db.execute("INSERT INTO decisions VALUES('d','c','旧判断','','old')")
        db.execute("INSERT INTO gaps VALUES('g','c','d','action','旧问题','answered')")
        db.execute("INSERT INTO turns VALUES('t','c','g','d',1,'action','旧问题','旧原话','旧专家','old','')")
        db.execute("INSERT INTO candidates VALUES('x','c','d',1,'pending','unvalidated','old')")
        db.execute("INSERT INTO revisions VALUES('x',1,'旧经验','case_note','旧原话','旧原话','旧原话','旧原话','旧整理人','old',NULL,NULL)")
        for index, field in enumerate(('statement','scope','rationale','exceptions')):
            db.execute("INSERT INTO evidence VALUES('x',1,?,?, 'turn','t',0,3,'旧原话')", (index,field))
        before = db.execute("SELECT * FROM evidence ORDER BY rowid").fetchall()
    upgraded = Ledger(tmp_path)
    with upgraded._connection() as db:
        assert [tuple(r) for r in db.execute("SELECT candidate_id,revision,position,field,source_type,source_id,start,end,quote FROM evidence ORDER BY rowid")] == before
        assert db.execute('PRAGMA user_version').fetchone()[0] == 7
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_pause_continue_cannot_overlap_a_model_call(tmp_path):
    started, release = Event(), Event()
    def delayed(context):
        started.set()
        assert release.wait(timeout=10)
        return draft(context)
    runtime = InterviewRuntime(Ledger(tmp_path), ScriptPlanner(ask(), delayed, draft))
    session = _start_known(runtime, {"goal": "合成目标", "request_id": "start"})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(answer, runtime, session)
        try:
            assert started.wait(timeout=10)
            paused = control(runtime, runtime.store.get(session["id"]), "pause")
            with pytest.raises(LedgerError) as conflict:
                control(runtime, paused, "continue")
            assert conflict.value.status == 409
            assert not runtime.store.get(session["id"])["candidate_ids"]
        finally:
            release.set()
        with pytest.raises(LedgerError):
            future.result(timeout=10)
    recovered = control(runtime, runtime.store.get(session["id"]), "continue")
    assert recovered["status"] == "review" and len(recovered["candidate_ids"]) == 1
