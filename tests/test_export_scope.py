"""A case excerpt never embeds another case's raw interview or source data."""

from base64 import b64decode
from copy import deepcopy
import json

from experience_ledger.exports import export_case
from experience_ledger.service import Ledger, _json


def manual_candidate(ledger, label):
    case = ledger.create_case("general", "合成案例" + label, "合成背景" + label)
    decision = ledger.add_decision(case["id"], "合成判断" + label, "仅供测试")
    for gap in ledger.get_case(case["id"])["gaps"]:
        ledger.add_turn(case["id"], gap["id"], f"{label}:{gap['kind']}:合成原话", "专家" + label)
    candidate = ledger.draft_candidate(case["id"], decision["id"])
    return ledger.review_candidate(candidate["id"], 1, "approve", "模拟审核", "合成审核")


def test_shared_interview_exports_only_selected_turn_spans_and_no_global_context(tmp_path):
    ledger = Ledger(tmp_path / "data")
    left = ledger.create_case("general", "甲案例", "甲背景")
    right = ledger.create_case("general", "乙案例", "乙背景")
    text_a, secret = "甲的已授权原话🙂", "乙的禁止泄漏原话ONLY_B"
    raw = text_a + "\n" + secret
    with ledger._connection(write=True) as db:
        domain_json = _json(left["domain"])
        db.execute("""INSERT INTO interviews(id,goal,scope,status,limits_json,created_at,updated_at,domain_json)
            VALUES(?,?,?,?,?,?,?,?)""", ("shared", secret, secret, "paused", "{}", "t", "t", domain_json))
        for case in (left, right):
            db.execute("INSERT INTO interview_cases VALUES(?,?,?,?)", ("shared", case["id"], "测试", "t"))
        db.execute("""INSERT INTO turns(id,seq,kind,question,answer,speaker,created_at,interview_id,interview_seq)
            VALUES(?,?,?,?,?,?,?,?,?)""", ("shared-turn", 1, "narration", secret, raw, "合成专家", "t", "shared", 1))
        for link_id, case, start, end in (("la", left, 0, len(text_a)), ("lb", right, len(text_a) + 1, len(raw))):
            db.execute("INSERT INTO turn_contexts VALUES(?,?,?,?,?,?,?,?,?)",
                       (link_id, "shared-turn", start, end, case["id"], None, None, "测试", "t"))
        db.execute("INSERT INTO interview_actions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("action", "shared", "mock", "test", _json({"all_turns": raw}), 1, _json({"private": secret}), "{}", "{}", secret, "t"))
    exported = export_case(ledger, left["id"])
    serialized = json.dumps(exported, ensure_ascii=False)
    assert text_a in serialized
    assert secret not in serialized
    assert exported["format_version"] == 3
    assert exported["scope"]["complete_workspace_backup"] is False
    assert len(exported["case"]["turns"]) == 1
    turn = exported["case"]["turns"][0]
    assert turn["id"] == "shared-turn"
    assert turn["fragments"] == [{"start": 0, "end": len(text_a), "text": text_a}]
    assert "answer" not in turn
    assert "question" not in turn
    assert exported["interviews"][0]["turn_ids"] == ["shared-turn"]
    assert {d["type"] for d in exported["external_dependencies"]} == {"interview"}
    other = export_case(ledger, right["id"])
    assert text_a not in json.dumps(other, ensure_ascii=False)
    assert other["case"]["turns"][0]["fragments"][0]["start"] == len(text_a) + 1


def test_cross_case_candidate_relation_and_feedback_export_external_ids_without_other_originals(tmp_path):
    ledger = Ledger(tmp_path / "data")
    left, right = manual_candidate(ledger, "甲"), manual_candidate(ledger, "乙")
    secret = "OTHER_CASE_SECRET_原始资料"
    source = ledger.add_source(right["case_id"], "乙私有资料", secret)
    refs = deepcopy(left["evidence"])
    refs.append({"field": "scope", "source_type": "document", "source_id": source["id"], "start": 0, "end": len(secret), "quote": secret})
    args = {key: deepcopy(left[key]) for key in ("title", "kind", "statement", "scope", "rationale", "exceptions")}
    args.update(title=secret, statement="结合外部案例：" + secret, evidence=refs, actor="模拟编辑", expected_revision=1,
                contexts=left["contexts"] + right["contexts"])
    shared = ledger.revise_candidate(left["id"], **args)
    ledger.review_candidate(left["id"], 2, "approve", "模拟审核", secret)
    foreign_feedback = ledger.add_feedback(left["id"], 2, "使用者", secret + "反馈", True, secret, secret, secret, right["case_id"])
    own_feedback = ledger.add_feedback(left["id"], 1, "甲使用者", "甲的使用反馈", None, "未知", "", "", left["case_id"])
    evidence = [{"side": side, **{key: candidate["evidence"][0][key] for key in ("source_type", "source_id", "start", "end", "quote")}}
                for side, candidate in (("left", shared), ("right", right))]
    relation = ledger.propose_relation(left["id"], 2, right["id"], 1, "conflict", secret, secret, evidence, "模型")
    ledger.review_relation(relation["id"], "accept", "模拟审核", secret)
    exported = export_case(ledger, left["case_id"])
    serialized = json.dumps(exported, ensure_ascii=False)
    assert secret not in serialized
    assert "甲的使用反馈" in serialized
    candidate = next(c for c in exported["case"]["candidates"] if c["id"] == left["id"])
    assert len(candidate["revisions"]) == 2
    assert candidate["revisions"][0]["statement"] == left["statement"]
    assert candidate["revisions"][1]["content_omitted"] is True
    external_quote = next(e for e in candidate["revisions"][1]["evidence"] if e["source_id"] == source["id"])
    assert external_quote["external"] is True
    assert "quote" not in external_quote
    relation = exported["case"]["relations"][0]
    assert relation["content_omitted"] is True
    assert "reason" not in relation
    assert "note" not in relation["reviews"][0]
    feedback_ids = {item["id"] for item in exported["case"]["feedback"]}
    assert feedback_ids == {own_feedback["id"]}
    assert foreign_feedback["id"] in {d["id"] for d in exported["external_dependencies"]}


def test_all_selected_file_versions_original_bytes_and_parse_attempts_are_embedded(tmp_path):
    ledger = Ledger(tmp_path / "data")
    left = ledger.create_case("general", "甲", "合成")
    right = ledger.create_case("general", "乙", "合成")
    first_bytes = "甲资料第一版\r\n保持原始字节🙂".encode("utf-8")
    second_bytes = "甲资料第二版".encode("utf-8")
    first = ledger.add_file(left["id"], "甲.txt", first_bytes)
    ledger.parse_file(first["id"])
    second = ledger.add_file(left["id"], "甲.txt", second_bytes, file_id=first["file_id"])
    secret = "外部文件ONLY_B"
    foreign = ledger.add_file(right["id"], "乙.txt", secret.encode("utf-8"))
    ledger.add_source(left["id"], "甲旧文本", "选定案例的UTF-8文本")
    ledger.add_source(right["id"], "乙旧文本", secret)
    exported = export_case(ledger, left["id"])
    files = exported["case"]["files"]
    assert {f["id"] for f in files} == {first["id"], second["id"]}
    assert foreign["id"] not in json.dumps(exported)
    assert secret not in json.dumps(exported, ensure_ascii=False)
    assert b64decode(files[0]["bytes_base64"]) == first_bytes
    assert b64decode(files[1]["bytes_base64"]) == second_bytes
    assert len(files[0]["parses"]) == 2
    assert len({parse["id"] for parse in files[0]["parses"]}) == 2
    assert files[0]["parses"][0]["anchors"][0]["text"] == first_bytes.decode("utf-8")
    assert exported["case"]["sources"][0]["text"] == "选定案例的UTF-8文本"


def test_legacy_whole_turn_remains_complete_and_repeated_context_spans_are_deduplicated(tmp_path):
    ledger = Ledger(tmp_path / "data")
    candidate = manual_candidate(ledger, "甲")
    case = ledger.get_case(candidate["case_id"])
    first = case["turns"][0]
    with ledger._connection(write=True) as db:
        db.execute("INSERT INTO turn_contexts VALUES(?,?,?,?,?,?,?,?,?)",
                   ("additional-scope", first["id"], 0, len(first["answer"]), candidate["case_id"], None, None, "测试", "t"))
    exported = export_case(ledger, candidate["case_id"])
    turns = exported["case"]["turns"]
    assert len(turns) == len({turn["id"] for turn in turns}) == len(case["turns"])
    saved = next(turn for turn in turns if turn["id"] == first["id"])
    assert saved["fragments"] == [{"start": 0, "end": len(first["answer"]), "text": first["answer"]}]
    assert saved["question"] == first["question"]
