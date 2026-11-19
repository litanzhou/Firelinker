"""Behavior checks using synthetic data; approvals below simulate a human reviewer."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from importlib.resources import files
import gc
import json
import sqlite3
from threading import Barrier
import warnings

import pytest

from experience_ledger.demo import seed_demo
from experience_ledger.service import Ledger, LedgerError


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger")


@pytest.fixture
def populated(ledger):
    return ledger, seed_demo(ledger)


def revision_args(candidate, **changes):
    args = {key: candidate[key] for key in (
        "title", "kind", "statement", "scope", "rationale", "exceptions", "evidence"
    )}
    args["evidence"] = deepcopy(args["evidence"])
    args.update(expected_revision=candidate["revision"], actor="测试模拟编辑")
    args.update(changes)
    return args


def approve(ledger, candidate):
    return ledger.review_candidate(
        candidate["id"], candidate["revision"], "approve", "测试模拟审核人",
        "测试模拟：核对所选版本的逐字引用，并确认候选表达。"
    )


def test_two_domains_run_complete_cycle_and_survive_restart(tmp_path):
    location = tmp_path / "durable"
    ledger = Ledger(location)
    cases = seed_demo(ledger)
    assert {case["domain_id"] for case in cases} == {"electronics_repair", "oral_history"}
    assert ledger.search_knowledge() == []
    approved_ids = []
    for case in cases:
        assert len(case["sources"]) == 2
        assert len(case["turns"]) == 5
        assert len(case["gaps"]) == 5
        assert all(gap["status"] == "answered" for gap in case["gaps"])
        candidate = ledger.get_candidate(case["candidates"][0]["id"])
        assert candidate["status"] == "pending"
        assert {ref["field"] for ref in candidate["evidence"]} == {
            "statement", "scope", "rationale", "exceptions"
        }
        approved = approve(ledger, candidate)
        assert approved["status"] == "approved"
        assert approved["validation_status"] == "unvalidated"
        approved_ids.append(approved["id"])

    reopened = Ledger(location)
    assert {item["id"] for item in reopened.search_knowledge()} == set(approved_ids)
    for candidate_id in approved_ids:
        candidate = reopened.get_candidate(candidate_id)
        assert candidate["reviews"][-1]["reviewer"] == "测试模拟审核人"
        assert candidate["revisions"]
        assert candidate["evidence"]
    assert len(reopened.search_knowledge(domain_id="oral_history")) == 1
    assert len(reopened.search_knowledge(query="犹豫")) == 1
    assert reopened.search_knowledge(query="不存在的合成检索词") == []


def test_demo_repeated_runs_never_approve_or_duplicate(ledger):
    initial = seed_demo(ledger)
    repeated = seed_demo(ledger)
    assert [case["id"] for case in repeated] == [case["id"] for case in initial]
    assert len(ledger.list_cases()) == 2
    assert ledger.search_knowledge() == []
    for case in repeated:
        assert len(case["candidates"]) == 1
        assert len(case["turns"]) == 5
        candidate = ledger.get_candidate(case["candidates"][0]["id"])
        assert candidate["reviews"] == []


def test_demo_reports_incomplete_existing_case_instead_of_claiming_success(ledger):
    ledger.create_case("electronics_repair", "[合成案例] USB 键盘偶发断连的排查记录", "合成")
    with pytest.raises(LedgerError) as incomplete:
        seed_demo(ledger)
    assert incomplete.value.status == 409
    assert len(ledger.list_cases()) == 1


def test_domain_configuration_changes_real_questions_and_is_snapshotted(tmp_path):
    domain_dir = tmp_path / "domains"
    domain_dir.mkdir()
    resource = files("experience_ledger").joinpath("domains", "oral_history.json")
    config = json.loads(resource.read_text(encoding="utf-8"))
    path = domain_dir / "oral_history.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    ledger = Ledger(tmp_path / "data", domains_dir=domain_dir)
    old_case = ledger.create_case("oral_history", "旧配置案例", "合成")
    config["version"] = "2.0"
    config["prompts"]["rationale"] = "新版问题：你依据什么处理{object}？"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    updated = Ledger(tmp_path / "data", domains_dir=domain_dir)
    updated.add_decision(old_case["id"], "保留停顿", "合成")
    old = updated.get_case(old_case["id"])
    assert old["domain"]["version"] == "1.0"
    assert not any(gap["question"].startswith("新版问题") for gap in old["gaps"])
    new_case = updated.create_case("oral_history", "新配置案例", "合成")
    updated.add_decision(new_case["id"], "保留停顿", "合成")
    current = updated.get_case(new_case["id"])
    assert current["domain"]["version"] == "2.0"
    assert any(gap["question"] == "新版问题：你依据什么处理访谈片段？" for gap in current["gaps"])


def test_domain_questions_are_not_only_a_different_label(populated):
    _, cases = populated
    questions = [{gap["kind"]: gap["question"] for gap in case["gaps"]} for case in cases]
    assert all(questions[0][kind] != questions[1][kind] for kind in questions[0])
    assert "供电" in questions[0]["scope"]
    assert "授权" in questions[1]["scope"]


def test_document_versions_and_old_citation_remain_immutable(populated):
    ledger, cases = populated
    case = cases[0]
    candidate = ledger.get_candidate(case["candidates"][0]["id"])
    first = ledger.add_source(case["id"], "同名.md", "旧版原文：线缆松动。")
    second = ledger.add_source(case["id"], "同名.md", "新版原文：原因仍待核实。", first["source_id"])
    assert second["version"] == first["version"] + 1
    assert second["id"] != first["id"]
    assert second["sha256"] != first["sha256"]
    assert first["source_id"] == second["source_id"]
    same = ledger.add_source(case["id"], "同名.md", first["text"], first["source_id"])
    assert same["id"] == first["id"]
    evidence = deepcopy(candidate["evidence"])
    evidence[0] = {
        "field": evidence[0]["field"], "source_type": "document", "source_id": first["id"],
        "start": 0, "end": len(first["text"]), "quote": first["text"]
    }
    revised = ledger.revise_candidate(candidate["id"], **revision_args(candidate, evidence=evidence))
    saved = ledger.get_candidate(revised["id"])
    old_ref = next(ref for ref in saved["evidence"] if ref["source_type"] == "document")
    assert old_ref["quote"] == first["text"]
    assert old_ref["source_id"] == first["id"]
    all_sources = ledger.get_case(case["id"])["sources"]
    assert {s["text"] for s in all_sources if s["source_id"] == first["source_id"]} == {
        first["text"], second["text"]
    }


@pytest.mark.parametrize("mutation", ["fabricated", "negative_start", "overflow", "wrong_case", "wrong_type"])
def test_bad_evidence_is_rejected_without_partial_revision(populated, mutation):
    ledger, cases = populated
    candidate = ledger.get_candidate(cases[0]["candidates"][0]["id"])
    evidence = deepcopy(candidate["evidence"])
    # Corrupt the last reference: earlier references must not be committed separately.
    ref = evidence[-1]
    if mutation == "fabricated":
        ref["quote"] = "并不存在于原始访谈的内容"
    elif mutation == "negative_start":
        ref["start"] = -1
    elif mutation == "overflow":
        ref["end"] = 10**9
    elif mutation == "wrong_case":
        other_turn = cases[1]["turns"][0]
        ref.update(source_type="turn", source_id=other_turn["id"], start=0,
                   end=len(other_turn["answer"]), quote=other_turn["answer"])
    else:
        ref["source_type"] = "model_output"
    before = ledger.get_case(cases[0]["id"])
    with pytest.raises(LedgerError):
        ledger.revise_candidate(candidate["id"], **revision_args(candidate, evidence=evidence))
    after = ledger.get_candidate(candidate["id"])
    assert after["revision"] == candidate["revision"]
    assert after["revisions"] == candidate["revisions"]
    assert ledger.get_case(cases[0]["id"])["events"] == before["events"]


def test_unicode_citation_offsets_use_codepoints(populated):
    ledger, cases = populated
    case = cases[0]
    candidate = ledger.get_candidate(case["candidates"][0]["id"])
    source = ledger.add_source(case["id"], "Unicode.txt", "观察🔎：先断开连接。")
    evidence = deepcopy(candidate["evidence"])
    evidence[0] = dict(field="statement", source_type="document", source_id=source["id"],
                       start=4, end=9, quote="先断开连接")
    revised = ledger.revise_candidate(candidate["id"], **revision_args(candidate, evidence=evidence))
    assert next(ref for ref in revised["evidence"] if ref["field"] == "statement")["quote"] == "先断开连接"


def test_cross_case_document_version_and_turn_are_refused(populated):
    ledger, cases = populated
    before = ledger.get_case(cases[0]["id"])
    with pytest.raises(LedgerError):
        ledger.add_source(cases[0]["id"], "错误归属", "新资料", cases[1]["sources"][0]["source_id"])
    with pytest.raises(LedgerError):
        ledger.add_turn(cases[0]["id"], cases[1]["gaps"][0]["id"], "答复", "合成角色")
    with pytest.raises(LedgerError):
        ledger.draft_candidate(cases[0]["id"], cases[1]["decisions"][0]["id"])
    after = ledger.get_case(cases[0]["id"])
    assert before == after


def test_latest_turn_forms_draft_without_erasing_previous_answer(ledger):
    case = ledger.create_case("oral_history", "自我修正", "合成")
    decision = ledger.add_decision(case["id"], "选择保留内容", "合成")
    gap = next(g for g in ledger.get_case(case["id"])["gaps"] if g["kind"] == "action")
    first = ledger.add_turn(case["id"], gap["id"], "起初我删去了停顿。", "合成编辑")
    second = ledger.add_turn(case["id"], gap["id"], "复核后我保留了停顿。", "合成编辑",
                             question="复核之后改变了吗？")
    draft = ledger.draft_candidate(case["id"], decision["id"])
    assert draft["statement"] == second["answer"]
    assert draft["evidence"][0]["source_id"] == second["id"]
    history = ledger.get_case(case["id"])["turns"]
    assert [turn["id"] for turn in history] == [first["id"], second["id"]]
    assert history[-1]["question"] == "复核之后改变了吗？"


def test_incomplete_draft_can_be_saved_but_not_approved(ledger):
    case = ledger.create_case("oral_history", "尚待访谈", "合成")
    decision = ledger.add_decision(case["id"], "尚未解释的取舍", "合成")
    gap = next(gap for gap in ledger.get_case(case["id"])["gaps"] if gap["kind"] == "action")
    ledger.add_turn(case["id"], gap["id"], "我保留了片段，但尚未说明条件和理由。", "合成编辑")
    draft = ledger.draft_candidate(case["id"], decision["id"])
    assert draft["status"] == "pending"
    with pytest.raises(LedgerError):
        approve(ledger, draft)
    assert ledger.get_candidate(draft["id"])["reviews"] == []
    assert ledger.search_knowledge() == []


@pytest.mark.parametrize("change", [{"scope": ""}, {"evidence": []}])
def test_approval_requires_content_and_field_evidence(populated, change):
    ledger, cases = populated
    candidate = ledger.get_candidate(cases[0]["candidates"][0]["id"])
    revised = ledger.revise_candidate(candidate["id"], **revision_args(candidate, **change))
    with pytest.raises(LedgerError):
        approve(ledger, revised)
    assert ledger.get_candidate(candidate["id"])["status"] == "pending"
    assert ledger.search_knowledge() == []


def test_review_is_bound_to_revision_and_requires_rereview_after_edit(populated):
    ledger, cases = populated
    candidate = ledger.get_candidate(cases[0]["candidates"][0]["id"])
    original = approve(ledger, candidate)
    assert len(ledger.search_knowledge()) == 1
    with pytest.raises(LedgerError) as repeated:
        approve(ledger, original)
    assert repeated.value.status == 409
    updated = ledger.revise_candidate(original["id"], **revision_args(original, title="经编辑的标题"))
    assert updated["revision"] == original["revision"] + 1
    assert updated["status"] == "pending"
    visible = ledger.search_knowledge()
    assert len(visible) == 1 and visible[0]["revision"] == original["revision"]
    assert visible[0]["title"] == original["title"]
    with pytest.raises(LedgerError) as stale:
        approve(ledger, original)
    assert stale.value.status == 409
    approved = approve(ledger, updated)
    assert approved["status"] == "approved"
    assert len(ledger.get_candidate(approved["id"])["reviews"]) == 2


def test_rejected_candidate_can_be_revised_and_reviewed_again(populated):
    ledger, cases = populated
    candidate = ledger.get_candidate(cases[1]["candidates"][0]["id"])
    rejected = ledger.review_candidate(candidate["id"], candidate["revision"], "reject",
                                       "测试模拟审核人", "标题需要突出条件。")
    assert rejected["status"] == "rejected"
    assert ledger.search_knowledge() == []
    revised = ledger.revise_candidate(candidate["id"], **revision_args(rejected, kind="case_note"))
    approved = approve(ledger, revised)
    assert approved["status"] == "approved"
    assert approved["kind"] == "case_note"
    assert len(ledger.get_candidate(candidate["id"])["reviews"]) == 2


def test_stale_editor_cannot_overwrite_another_revision(populated):
    ledger, cases = populated
    original = ledger.get_candidate(cases[0]["candidates"][0]["id"])
    revised = ledger.revise_candidate(original["id"], **revision_args(original, title="编辑甲"))
    with pytest.raises(LedgerError) as conflict:
        ledger.revise_candidate(original["id"], **revision_args(original, title="编辑乙"))
    assert conflict.value.status == 409
    saved = ledger.get_candidate(original["id"])
    assert saved["title"] == "编辑甲"
    assert saved["revision"] == revised["revision"]


def test_two_instances_cannot_both_review_same_revision(tmp_path):
    location = tmp_path / "shared"
    first = Ledger(location)
    cases = seed_demo(first)
    candidate = first.get_candidate(cases[0]["candidates"][0]["id"])
    second = Ledger(location)
    ready = Barrier(2)

    def review(ledger):
        ready.wait(timeout=10)
        try:
            return approve(ledger, candidate)["status"]
        except LedgerError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(review, (first, second)))
    assert sorted(results, key=str) == [409, "approved"]
    assert len(first.get_candidate(candidate["id"])["reviews"]) == 1


def test_concurrent_interview_writes_keep_unique_ordered_sequences(tmp_path):
    location = tmp_path / "concurrent_turns"
    first, second = Ledger(location), Ledger(location)
    case = first.create_case("oral_history", "合成并发记录", "两位记录员并发提交")
    first.add_decision(case["id"], "保留片段", "合成")
    gap = first.get_case(case["id"])["gaps"][0]
    ready = Barrier(2)

    def record(ledger):
        ready.wait(timeout=10)
        return ledger.add_turn(case["id"], gap["id"], "合成答复", "合成记录员")

    with ThreadPoolExecutor(max_workers=2) as pool:
        written = list(pool.map(record, (first, second)))
    assert len({turn["id"] for turn in written}) == 2
    assert sorted(turn["seq"] for turn in written) == [1, 2]
    assert [turn["seq"] for turn in Ledger(location).get_case(case["id"])["turns"]] == [1, 2]


def test_modified_original_blob_blocks_read_and_approval(populated, tmp_path):
    ledger, cases = populated
    case = cases[0]
    candidate = ledger.get_candidate(case["candidates"][0]["id"])
    source = ledger.add_source(case["id"], "校验资料.txt", "合成原文：应保留本句。")
    evidence = deepcopy(candidate["evidence"])
    evidence[0] = dict(field="statement", source_type="document", source_id=source["id"],
                       start=0, end=len(source["text"]), quote=source["text"])
    revised = ledger.revise_candidate(candidate["id"], **revision_args(candidate, evidence=evidence))
    blob = tmp_path / "ledger" / "blobs" / (source["sha256"] + ".txt")
    original = blob.read_bytes()
    blob.write_text("被修改的内容", encoding="utf-8")
    with pytest.raises(LedgerError) as corrupt_read:
        ledger.get_case(case["id"])
    assert corrupt_read.value.status == 409
    with pytest.raises(LedgerError) as corrupt_approval:
        approve(ledger, revised)
    assert corrupt_approval.value.status == 409
    blob.write_bytes(original)
    restored = ledger.get_candidate(candidate["id"])
    assert restored["status"] == "pending"
    assert restored["reviews"] == []
    assert approve(ledger, restored)["status"] == "approved"


def test_future_database_schema_is_not_silently_downgraded(tmp_path):
    location = tmp_path / "future"
    Ledger(location)
    database = location / "ledger.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(LedgerError) as unsupported:
        Ledger(location)
    assert unsupported.value.status == 409
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 999


@pytest.mark.parametrize("domain,title", [("not_installed", "案例"), ("oral_history", "  ")])
def test_invalid_case_does_not_write_partial_state(ledger, domain, title):
    with pytest.raises(LedgerError):
        ledger.create_case(domain, title, "合成")
    assert ledger.list_cases() == []


def test_review_and_turn_require_named_actor(populated):
    ledger, cases = populated
    case = cases[0]
    candidate = ledger.get_candidate(case["candidates"][0]["id"])
    with pytest.raises(LedgerError):
        ledger.review_candidate(candidate["id"], candidate["revision"], "approve", "", "备注")
    with pytest.raises(LedgerError):
        ledger.add_turn(case["id"], case["gaps"][0]["id"], "回答", "")


def test_unknown_review_decision_is_rejected(populated):
    ledger, cases = populated
    candidate = ledger.get_candidate(cases[0]["candidates"][0]["id"])
    with pytest.raises(LedgerError):
        ledger.review_candidate(candidate["id"], candidate["revision"], "auto_verified", "模型", "自动")
    assert ledger.get_candidate(candidate["id"])["status"] == "pending"


def test_sqlite_has_no_orphans_after_workflow(tmp_path):
    location = tmp_path / "integrity"
    ledger = Ledger(location)
    cases = seed_demo(ledger)
    for case in cases:
        approve(ledger, ledger.get_candidate(case["candidates"][0]["id"]))
    db_paths = [path for path in location.rglob("*") if path.suffix in (".db", ".sqlite", ".sqlite3")]
    assert len(db_paths) == 1
    with sqlite3.connect(db_paths[0]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert any(list(db.execute('PRAGMA foreign_key_list("' + table + '")')) for table in tables)


def test_sqlite_connections_are_released_after_initialization_and_operations(tmp_path):
    # Collect unrelated objects first so warnings from earlier tests cannot be
    # attributed to these Ledger operations.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always", ResourceWarning)
        location = tmp_path / "connection_release"
        ledger = Ledger(location)
        case = ledger.create_case("oral_history", "合成连接释放检查", "合成")
        assert ledger.get_case(case["id"])["title"] == case["title"]
        # On Windows a still-open SQLite handle prevents this move. Perform it
        # before collection, which could otherwise hide the leaked connection.
        location.rename(tmp_path / "released_connections")
        del ledger
        gc.collect()
    assert not [warning for warning in observed if issubclass(warning.category, ResourceWarning)]
