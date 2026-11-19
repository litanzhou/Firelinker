"""Synthetic records exercise revision visibility, source scope and human review."""

from copy import deepcopy
from importlib.resources import files
import json
import sqlite3

import pytest

from experience_ledger.service import Ledger, LedgerError


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "ledger")


def make_candidate(ledger, label="甲", approve=True):
    case = ledger.create_case("general", f"合成案例{label}", "仅供自动测试")
    decision = ledger.add_decision(case["id"], f"合成判断{label}", "合成")
    for gap in ledger.get_case(case["id"])["gaps"]:
        ledger.add_turn(case["id"], gap["id"], f"{label}的{gap['kind']}原始回答。", f"合成专家{label}")
    candidate = ledger.draft_candidate(case["id"], decision["id"])
    if approve:
        candidate = ledger.review_candidate(candidate["id"], 1, "approve", "模拟审核", "测试：逐字核对")
    return candidate


def revise(ledger, candidate, **changes):
    args = {key: deepcopy(candidate[key]) for key in ("title", "kind", "statement", "scope", "rationale", "exceptions", "evidence")}
    args.update(expected_revision=candidate["latest_revision"], actor="模拟修订人")
    args.update(changes)
    return ledger.revise_candidate(candidate["id"], **args)


def relation_refs(left, right):
    return [{"side": side, **{key: candidate["evidence"][0][key]
                             for key in ("source_type", "source_id", "start", "end", "quote")}}
            for side, candidate in (("left", left), ("right", right))]


def propose(ledger, left, right, **changes):
    args = dict(left_id=left["id"], left_revision=left["revision"], right_id=right["id"], right_revision=right["revision"],
                kind="conflict", conditions="双方条件尚需核对，不能先认定相同。", reason="两段表述需并列核对。",
                evidence=relation_refs(left, right), actor="model:synthetic")
    args.update(changes)
    return ledger.propose_relation(**args)


def test_pending_and_rejected_drafts_do_not_replace_or_leak_into_active_knowledge(ledger):
    original = make_candidate(ledger)
    draft = revise(ledger, original, title="仅草稿可见的私有测试词")
    assert draft["active_revision"] == 1
    assert draft["status"] == "pending"
    active = ledger.search_knowledge()
    assert len(active) == 1
    assert active[0]["revision"] == 1
    assert active[0]["status"] == "approved"
    assert active[0]["latest_revision"] == 2
    assert "仅草稿可见的私有测试词" not in json.dumps(active, ensure_ascii=False)
    assert ledger.search_knowledge("仅草稿可见的私有测试词") == []
    ledger.review_candidate(draft["id"], 2, "reject", "模拟审核", "不接收草稿")
    assert ledger.search_knowledge()[0]["revision"] == 1
    assert ledger.get_candidate(draft["id"], 1)["title"] == original["title"]


def test_approval_switches_active_revision_and_withdrawal_preserves_history(ledger):
    original = make_candidate(ledger)
    draft = revise(ledger, original, title="已审核的新名称")
    approved = ledger.review_candidate(draft["id"], 2, "approve", "模拟审核", "接收新版")
    assert approved["active_revision"] == 2
    assert ledger.search_knowledge("已审核的新名称")[0]["revision"] == 2
    with pytest.raises(LedgerError) as stale:
        ledger.withdraw_candidate(approved["id"], 1, "模拟人", "旧页面撤下")
    assert stale.value.status == 409
    withdrawn = ledger.withdraw_candidate(approved["id"], 2, "模拟人", "明确撤下，保留原文")
    assert withdrawn["active_revision"] is None
    assert ledger.search_knowledge() == []
    assert len(ledger.get_candidate(approved["id"])["reviews"]) == 2
    assert ledger.get_candidate(approved["id"], 1)["revision"] == 1


def test_withdrawal_cannot_remove_a_newly_approved_version_from_a_stale_page(ledger):
    original = make_candidate(ledger)
    draft = revise(ledger, original)
    ledger.review_candidate(draft["id"], 2, "approve", "另一个模拟审核人", "在另一页面批准新版")
    with pytest.raises(LedgerError) as changed:
        ledger.withdraw_candidate(draft["id"], 2, "旧页面操作者", "原本打算撤下v1", expected_active_revision=1)
    assert changed.value.status == 409
    assert ledger.search_knowledge()[0]["revision"] == 2


def test_feedback_is_exact_version_bound_and_does_not_validate_experience(ledger):
    original = make_candidate(ledger)
    raw = "实际没有采用。\r\n只是提出下次试验的建议。🙂"
    feedback = ledger.add_feedback(original["id"], 1, "合成使用者", raw, False, "条件未知", "", "尚无结果")
    assert feedback["original"] == raw
    assert feedback["actual_used"] is False
    draft = revise(ledger, original)
    with pytest.raises(LedgerError):
        ledger.add_feedback(original["id"], 2, "合成使用者", "草稿未通过审核", None, "", "", "")
    ledger.review_candidate(draft["id"], 2, "approve", "模拟审核", "新版审核")
    assert ledger.get_candidate(original["id"], 2)["feedback"] == []
    assert ledger.get_candidate(original["id"], 1)["feedback"][0]["original"] == raw
    assert ledger.get_candidate(original["id"])["validation_status"] == "unvalidated"
    assert ledger.list_feedback(original["id"])[0]["revision"] == 1


def test_feedback_can_be_quoted_only_after_its_usage_case_is_explicitly_linked(ledger):
    original = make_candidate(ledger)
    usage = ledger.create_case("general", "新的使用案例", "合成使用条件")
    raw = "实际采用后发现该边界需要补充。"
    feedback = ledger.add_feedback(original["id"], 1, "合成使用者", raw, True, "新条件", "按说明采用", "观察到例外", usage["id"])
    refs = deepcopy(original["evidence"])
    refs.append(dict(field="exceptions", source_type="feedback", source_id=feedback["id"], start=0, end=len(raw), quote=raw))
    with pytest.raises(LedgerError):
        revise(ledger, original, evidence=refs)
    assert ledger.get_candidate(original["id"])["revision"] == 1
    draft = revise(ledger, original, evidence=refs, contexts=original["contexts"] + [{"case_id": usage["id"]}])
    assert draft["evidence"][-1]["source_text"] == raw
    assert draft["evidence"][-1]["source_case_id"] == usage["id"]
    assert ledger.get_feedback(feedback["id"])["revision"] == 1


def test_cross_case_sources_require_explicit_revision_contexts(ledger):
    left = make_candidate(ledger)
    right = make_candidate(ledger, "乙")
    text = "乙案例提供的新增条件。"
    source = ledger.add_source(right["case_id"], "合成资料", text)
    refs = deepcopy(left["evidence"])
    refs.append(dict(field="scope", source_type="document", source_id=source["id"], start=0, end=len(text), quote=text))
    with pytest.raises(LedgerError):
        revise(ledger, left, evidence=refs)
    updated = revise(ledger, left, evidence=refs, contexts=left["contexts"] + right["contexts"])
    assert len(updated["contexts"]) == 2
    assert updated["id"] in {c["id"] for c in ledger.get_case(right["case_id"])["candidates"]}
    with pytest.raises(LedgerError) as stale:
        revise(ledger, left, evidence=refs, contexts=left["contexts"] + right["contexts"])
    assert stale.value.status == 409
    assert ledger.get_candidate(left["id"], 1)["contexts"] == left["contexts"]


def test_shared_raw_answer_only_authorizes_the_linked_fragment(ledger):
    left = make_candidate(ledger)
    right = make_candidate(ledger, "乙")
    source_id = left["evidence"][0]["source_id"]
    original = left["evidence"][0]["source_text"]
    with ledger._connection(write=True) as db:
        db.execute("INSERT INTO turn_contexts VALUES(?,?,?,?,?,?,?,?,?)",
                   ("synthetic-fragment", source_id, 0, 2, right["case_id"], right["decision_id"], None, "合成关联", "test"))
    refs = deepcopy(right["evidence"])
    refs[0] = dict(field="statement", source_type="turn", source_id=source_id, start=0, end=3, quote=original[:3])
    with pytest.raises(LedgerError):
        revise(ledger, right, evidence=refs)
    refs[0].update(end=2, quote=original[:2])
    updated = revise(ledger, right, evidence=refs)
    assert updated["evidence"][0]["source_id"] == source_id
    assert updated["evidence"][0]["quote"] == original[:2]
    with ledger._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM turns WHERE id=?", (source_id,)).fetchone()[0] == 1


def test_knowledge_search_does_not_expand_a_citation_to_unlinked_shared_narration(ledger):
    original = make_candidate(ledger)
    own, secret = "可关联的原话片段", "不属于本经验引用范围的另一案例原话SECRET"
    with ledger._connection() as db:
        gap_id = db.execute("SELECT id FROM gaps WHERE decision_id=? AND kind='action'", (original["decision_id"],)).fetchone()[0]
    turn = ledger.add_turn(original["case_id"], gap_id, own + secret, "合成专家")
    with ledger._connection(write=True) as db:
        db.execute("UPDATE turn_contexts SET end=? WHERE turn_id=?", (len(own), turn["id"]))
    refs = deepcopy(original["evidence"])
    refs[0] = dict(field="statement", source_type="turn", source_id=turn["id"], start=0, end=len(own), quote=own)
    draft = revise(ledger, original, statement=own, evidence=refs)
    ledger.review_candidate(draft["id"], 2, "approve", "模拟审核", "只核对关联片段")
    results = ledger.search_knowledge()
    assert secret not in json.dumps(results, ensure_ascii=False)
    citation = results[0]["evidence"][0]
    assert citation["source_id"] == turn["id"]
    assert citation["source_text"] == own
    assert citation["source_excerpt_start"] == 0


def test_proposed_conflict_preserves_both_experiences_until_human_processing(ledger):
    left = make_candidate(ledger)
    right = make_candidate(ledger, "乙")
    proposed = propose(ledger, left, right)
    assert proposed["status"] == "proposed"
    assert len(ledger.search_knowledge()) == 2
    assert all(not item["relations"] for item in ledger.search_knowledge())
    accepted = ledger.review_relation(proposed["id"], "accept", "模拟审核", "确认需保留双方分歧，未裁定正确性")
    assert accepted["status"] == "accepted"
    assert all(item["relations"][0]["id"] == proposed["id"] for item in ledger.search_knowledge())
    assert all(item["validation_status"] == "unvalidated" for item in ledger.search_knowledge())
    with pytest.raises(LedgerError) as duplicate:
        ledger.review_relation(proposed["id"], "reject", "另一审核人", "过期操作")
    assert duplicate.value.status == 409
    resolved = ledger.review_relation(proposed["id"], "resolve", "模拟审核", "核对后记录双方条件不同", expected_status="accepted")
    assert resolved["status"] == "resolved"
    assert len(resolved["reviews"]) == 2
    assert len(ledger.search_knowledge()) == 2


def test_duplicate_relation_never_deletes_or_merges_originals_automatically(ledger):
    left = make_candidate(ledger)
    right = make_candidate(ledger, "乙")
    relation = propose(ledger, left, right, kind="duplicate")
    ledger.review_relation(relation["id"], "accept", "模拟审核", "仅确认关联，分别保留来源")
    assert ledger.get_candidate(left["id"])["evidence"][0]["source_title"] != ledger.get_candidate(right["id"])["evidence"][0]["source_title"]
    assert len(ledger.search_knowledge()) == 2


def test_internal_relation_operation_id_replays_without_creating_another_suggestion(ledger):
    left = make_candidate(ledger)
    right = make_candidate(ledger, "乙")
    original = propose(ledger, left, right, proposal_id="fixed-comparison-operation")
    replay = propose(ledger, left, right, proposal_id="fixed-comparison-operation")
    assert replay["id"] == original["id"]
    assert len(ledger.list_relations()) == 1
    with pytest.raises(LedgerError) as reused:
        propose(ledger, right, left, proposal_id="fixed-comparison-operation")
    assert reused.value.status == 409


def test_relationship_and_feedback_do_not_silently_follow_a_new_revision(ledger):
    left = make_candidate(ledger)
    right = make_candidate(ledger, "乙")
    relation = propose(ledger, left, right)
    ledger.review_relation(relation["id"], "accept", "模拟审核", "保留待解决分歧")
    changed = revise(ledger, left, title="新的适用条件")
    ledger.review_candidate(left["id"], changed["revision"], "approve", "模拟审核", "新范围另审")
    saved = ledger.get_relation(relation["id"])
    assert saved["left_revision"] == 1
    assert saved["requires_recheck"] is True
    assert saved["applies_to_active"] is False
    current = next(c for c in ledger.search_knowledge() if c["id"] == left["id"])
    assert current["relations"] == []
    assert ledger.get_candidate(left["id"], 1)["relations"][0]["left_revision"] == 1


def test_relation_evidence_cannot_borrow_an_unlinked_other_case(ledger):
    left = make_candidate(ledger)
    right = make_candidate(ledger, "乙")
    refs = relation_refs(left, right)
    refs[0].update({key: refs[1][key] for key in ("source_type", "source_id", "start", "end", "quote")})
    with pytest.raises(LedgerError):
        propose(ledger, left, right, evidence=refs)
    assert ledger.list_relations() == []


def test_native_only_reference_can_be_drafted_but_requires_recorded_manual_check_before_approval(ledger):
    original = make_candidate(ledger)
    native = ledger.add_file(original["case_id"], "native.pdf", b"%PDF-synthetic-native-only", parse=False)
    refs = deepcopy(original["evidence"])
    quote = "模型提出、尚待人工核对的文件引文。"
    refs[0] = {"field": refs[0]["field"], "source_type": "file", "source_id": native["id"],
               "quote": quote, "locator": {"kind": "pdf", "page": 1, "verification": "human_checked"}}
    draft = revise(ledger, original, evidence=refs)
    assert draft["evidence"][0]["locator"]["verification"] == "pending_manual"
    with pytest.raises(LedgerError):
        ledger.review_candidate(draft["id"], 2, "approve", "模拟审核", "不能跳过原件确认")
    assert ledger.get_candidate(draft["id"])["status"] == "pending"
    checked = ledger.confirm_file_reference(native["id"], {"kind": "pdf", "page": 1}, quote,
                                             "模拟核对人", "测试模拟：已对照原件核对位置及引文")
    refs = deepcopy(draft["evidence"])
    refs[0] = {"field": refs[0]["field"], **checked}
    checked_draft = revise(ledger, draft, evidence=refs)
    approved = ledger.review_candidate(checked_draft["id"], 3, "approve", "模拟审核", "确认固定原件与引用记录")
    assert approved["active_revision"] == 3
    assert approved["validation_status"] == "unvalidated"
    assert ledger.get_candidate(original["id"], 2)["evidence"][0]["locator"]["verification"] == "pending_manual"


def test_later_local_parsing_does_not_silently_verify_an_old_unpinned_draft(ledger):
    original = make_candidate(ledger)
    quote = "先由原生模型读到、后由本地解析定位的合成文字。"
    native = ledger.add_file(original["case_id"], "native.txt", quote.encode("utf-8"), parse=False)
    refs = deepcopy(original["evidence"])
    refs[0] = {"field": refs[0]["field"], "source_type": "file", "source_id": native["id"],
               "quote": quote, "locator": {"kind": "text"}}
    draft = revise(ledger, original, evidence=refs)
    assert draft["evidence"][0]["locator"]["verification"] == "pending_manual"
    parsed = ledger.parse_file(native["id"])
    with pytest.raises(LedgerError):
        ledger.review_candidate(draft["id"], 2, "approve", "模拟审核", "原草稿未绑定本次解析")
    pinned = revise(ledger, draft)
    assert pinned["evidence"][0]["locator"]["parse_id"] == parsed["parse_id"]
    assert pinned["evidence"][0]["locator"]["verification"] == "position_checked"
    approved = ledger.review_candidate(pinned["id"], 3, "approve", "模拟审核", "已固定解析依据后人工审核")
    assert approved["active_revision"] == 3
    assert ledger.get_candidate(original["id"], 2)["evidence"][0]["locator"]["verification"] == "pending_manual"


def test_v3_migration_preserves_old_values_and_does_not_revive_historical_approvals(tmp_path):
    data_dir = tmp_path / "old"
    data_dir.mkdir()
    db_path = data_dir / "ledger.sqlite3"
    package = files("experience_ledger")
    domain = package.joinpath("domains/general.json").read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as db:
        db.executescript(package.joinpath("schema.sql").read_text(encoding="utf-8"))
        db.executescript(package.joinpath("migrations/002_interviews.sql").read_text(encoding="utf-8"))
        db.executescript(package.joinpath("migrations/003_evidence_fields.sql").read_text(encoding="utf-8"))
        db.execute("INSERT INTO cases VALUES(?,?,?,?,?,?)", ("case", "general", "旧合成案例", "保留\r\n背景", domain, "old"))
        db.execute("INSERT INTO decisions VALUES(?,?,?,?,?)", ("decision", "case", "旧判断", "旧描述", "old"))
        db.execute("INSERT INTO gaps VALUES(?,?,?,?,?,?)", ("gap", "case", "decision", "action", "旧问题", "answered"))
        answer = "旧原话\r\n无改变。🙂"
        db.execute("INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("turn", "case", "gap", "decision", 1, "action", "旧问题", answer, "合成专家", "old", ""))
        for candidate_id, status, current in (("pending", "pending", 2), ("approved", "approved", 1), ("rejected", "rejected", 2)):
            db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?)", (candidate_id, "case", "decision", current, status, "unvalidated", "old"))
            for revision in range(1, current + 1):
                db.execute("INSERT INTO revisions(candidate_id,revision,title,kind,statement,scope,rationale,exceptions,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (candidate_id, revision, f"旧标题{revision}", "case_note", answer, "旧条件", "旧理由", "未知", "合成人", "old"))
                db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)", (candidate_id, revision, 0, "statement", "turn", "turn", 0, len(answer), answer))
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)", (candidate_id + "-review1", candidate_id, 1, "approve", "模拟审核", "旧审核", "old"))
            if status == "rejected":
                db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)", (candidate_id + "-review2", candidate_id, 2, "reject", "模拟审核", "旧驳回", "old"))
        snapshot = {}
        for table in ("cases", "decisions", "gaps", "turns", "candidates", "revisions", "evidence", "reviews"):
            columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            snapshot[table] = columns, db.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY rowid").fetchall()
    ledger = Ledger(data_dir)
    with sqlite3.connect(db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, (columns, rows) in snapshot.items():
            assert db.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY rowid").fetchall() == rows
        assert dict(db.execute("SELECT id,active_revision FROM candidates")) == {"pending": None, "approved": 1, "rejected": None}
    assert [candidate["id"] for candidate in ledger.search_knowledge()] == ["approved"]
    assert ledger.get_candidate("pending", 1)["statement"] == answer
