"""Source-aware opening policy with disposable original text and scripted models."""
import json

import pytest

from experience_ledger.interviews.planner import UnconfiguredPlanner
from experience_ledger.interviews.runtime import InterviewRuntime
from experience_ledger.service import Ledger, LedgerError
from test_interviews import ScriptPlanner


FOLLOW_UP = {"action": "ask_expert", "question": "记录中的这个选择，当时依据了哪些观察？",
             "reason": "只补充尚未说明的判断依据", "expected_information": "当时的观察"}


def submit(runtime, session, text, request_id="initial-text"):
    return runtime.answer(session["id"], {"question_id": session["pending"]["id"],
        "request_id": request_id, "answer": text, "speaker": "合成专家"})


def test_empty_start_waits_without_model_then_understands_committed_original(tmp_path):
    ledger = Ledger(tmp_path)
    original = "  合成原话🔧：昨晚听见异响。\n我先停机检查，当时没有想过其他理由。  "

    def understand(context):
        with ledger._connection() as db:
            assert db.execute("SELECT answer FROM turns WHERE interview_id=?", (session["id"],)).fetchone()[0] == original
        assert context["goal"] == "整理停机判断的经验"
        assert context["turns"][0]["answer"] == original
        assert context["input_summary"] == {"text_sources": 0, "files": 0, "turns": 1}
        return FOLLOW_UP

    planner = ScriptPlanner(understand)
    runtime = InterviewRuntime(ledger, planner)
    payload = {"goal": "整理停机判断的经验", "request_id": "empty"}
    session = runtime.start(payload)
    question_id = session["pending"]["id"]
    assert session["status"] == "waiting" and session["pending"]["origin"] == "initial_text"
    assert session["actions"] == planner.contexts == [] and session["calls"] == 0
    assert not session["executing"] and not ledger.list_cases()
    assert runtime.start(payload)["pending"]["id"] == question_id
    session = runtime.control(session["id"], {"command": "pause", "expected_revision": session["revision"]})
    session = runtime.control(session["id"], {"command": "continue", "expected_revision": session["revision"]})
    assert session["pending"]["id"] == question_id and session["calls"] == 0
    restarted = InterviewRuntime(Ledger(tmp_path), planner)
    assert restarted.store.get(session["id"])["pending"]["origin"] == "initial_text"
    session = submit(restarted, session, original)
    assert session["pending"]["question"] == FOLLOW_UP["question"]
    assert "origin" not in session["pending"] and len(session["turns"]) == 1
    assert session["calls"] == len(planner.contexts) == 1
    replay = restarted.answer(session["id"], {"question_id": question_id, "request_id": "initial-text",
        "answer": original, "speaker": "合成专家"})
    assert len(replay["turns"]) == 1 and replay["calls"] == 1


def test_initial_text_is_saved_without_model_configuration_and_rejects_blank(tmp_path):
    runtime = InterviewRuntime(Ledger(tmp_path), UnconfiguredPlanner())
    session = runtime.start({"goal": "合成目标", "request_id": "unconfigured"})
    assert session["status"] == "waiting" and session["calls"] == 0
    with pytest.raises(LedgerError, match="不能为空"):
        submit(runtime, session, " \n ")
    assert runtime.store.get(session["id"])["status"] == "waiting"
    session = submit(runtime, session, "合成输入：我当时先停机检查。")
    assert session["status"] == "error" and "尚未配置" in session["notice"]
    assert session["turns"][0]["answer"] == "合成输入：我当时先停机检查。"
    assert session["calls"] == 0


@pytest.mark.parametrize("kind", ["document", "file", "turn"])
def test_existing_authorized_material_can_be_read_before_the_first_question(tmp_path, kind):
    ledger = Ledger(tmp_path)
    case = ledger.create_case("general", "合成材料案例", "围绕材料补充判断依据")
    text = "合成原文：出现异响后，我先停机检查。"
    expected = {"text_sources": 0, "files": 0, "turns": 0}
    if kind == "document":
        source_id = ledger.add_source(case["id"], "合成记录", text)["id"]
        expected["text_sources"] = 1
    elif kind == "file":
        source_id = ledger.add_file(case["id"], "original.txt", text.encode())["id"]
        expected["files"] = 1
    else:
        previous = InterviewRuntime(ledger, ScriptPlanner())
        old = previous.start({"goal": "合成原话记录", "request_id": "owner", "case_id": case["id"], "start_only": True})
        old = previous.supplement(old["id"], {"request_id": "raw", "speaker": "合成专家", "answer": text})
        source_id = old["turns"][0]["id"]
        expected["turns"] = 1

    def read(context):
        assert context["input_summary"] == expected
        assert any(item["id"] == source_id for item in context["source_catalog"])
        return {"action": "read_source", "source_type": kind, "source_id": source_id, "reason": "先理解已有原文"}

    def understand(context):
        assert text in json.dumps(context["last_tool"], ensure_ascii=False)
        return FOLLOW_UP

    planner = ScriptPlanner(read, understand)
    runtime = InterviewRuntime(ledger, planner)
    session = runtime.start({"goal": "核对材料里的停机依据", "request_id": "materials", "case_id": case["id"]})
    assert session["pending"]["question"] == FOLLOW_UP["question"]
    assert len(planner.contexts) == 2 and session["calls"] == 2
    assert session["turns"] == []
    assert not any(event["type"] == "interview.initial_text_requested" for event in session["events"])


def test_unlinked_material_does_not_bypass_initial_text(tmp_path):
    ledger = Ledger(tmp_path)
    other = ledger.create_case("general", "其他案例", "未授权资料")
    ledger.add_source(other["id"], "未关联记录", "其他案例的原文")
    ledger.add_file(other["id"], "private.txt", b"unlinked original")
    empty = ledger.create_case("general", "只有背景的案例", "背景本身不是可引用的原话")
    planner = ScriptPlanner()
    runtime = InterviewRuntime(ledger, planner)
    session = runtime.start({"goal": "合成当前目标", "request_id": "scoped", "case_id": empty["id"]})
    assert session["pending"]["origin"] == "initial_text" and session["calls"] == 0
    assert runtime.store.context(session["id"])["input_summary"] == {"text_sources": 0, "files": 0, "turns": 0}
    assert planner.contexts == []


def test_drafting_an_empty_interview_pauses_without_fabricating_content(tmp_path):
    planner = ScriptPlanner()
    runtime = InterviewRuntime(Ledger(tmp_path), planner)
    session = runtime.start({"goal": "合成目标", "request_id": "empty-draft", "start_only": True})
    session = runtime.control(session["id"], {"command": "draft", "expected_revision": session["revision"]})
    assert session["status"] == "paused" and "尚无原始内容" in session["notice"]
    assert session["questions"] == session["candidate_ids"] == planner.contexts == []
