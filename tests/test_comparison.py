"""Durable pair comparison: scoped inputs, retries, leases and crash windows."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Event

import pytest

from experience_ledger.comparison import compare_experiences
from experience_ledger.interviews.planner import PlannerError, Proposal
from experience_ledger.service import Ledger, LedgerError
from experience_ledger.web import create_app


class ComparisonPlanner:
    configured = True
    model = "synthetic-comparison"

    def __init__(self):
        self.calls = 0
        self.contexts = []
        self.fail = False
        self.entered = None
        self.release = None

    def propose(self, context, feedback=""):
        self.calls += 1
        self.contexts.append(deepcopy(context))
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("synthetic planner was not released")
        if self.fail:
            raise PlannerError("合成模型请求失败，原记录保留")
        left, right = context["left"], context["right"]
        evidence = []
        for side, candidate in (("left", left), ("right", right)):
            ref = candidate["evidence"][0]
            evidence.append({"side": side, **{key: ref[key] for key in
                ("source_type", "source_id", "start", "end", "quote", "locator") if key in ref}})
        return Proposal({"action": "propose_relation", "reason": "合成比较：条件尚需人工核对。",
            "left_id": left["id"], "left_revision": left["revision"],
            "right_id": right["id"], "right_revision": right["revision"],
            "relation_kind": "conflict", "conditions": "暂未认定双方条件相同。",
            "relation_evidence": evidence}, self.model, 13)


def candidate(ledger, label, *, secret_suffix=""):
    case = ledger.create_case("general", label + "合成案例", "合成背景")
    decision = ledger.add_decision(case["id"], label + "合成判断", "仅供测试")
    action_turn = None
    for gap in ledger.get_case(case["id"])["gaps"]:
        own = f"{label}:{gap['kind']}:原始回答"
        raw = own + secret_suffix if gap["kind"] == "action" else own
        turn = ledger.add_turn(case["id"], gap["id"], raw, label + "合成专家")
        if gap["kind"] == "action":
            action_turn = turn
            if secret_suffix:
                with ledger._connection(write=True) as db:
                    db.execute("UPDATE turn_contexts SET end=? WHERE turn_id=?", (len(own), turn["id"]))
    result = ledger.draft_candidate(case["id"], decision["id"])
    if secret_suffix:
        refs = deepcopy(result["evidence"])
        own = action_turn["answer"][:-len(secret_suffix)]
        for ref in refs:
            if ref["source_id"] == action_turn["id"]:
                ref.update(start=0, end=len(own), quote=own)
        result = revise(ledger, result, statement=own, evidence=refs)
    return ledger.review_candidate(result["id"], result["revision"], "approve", "模拟审核", "合成审核")


def revise(ledger, original, **changes):
    values = {key: deepcopy(original[key]) for key in ("title", "kind", "statement", "scope", "rationale", "exceptions", "evidence")}
    values.update(expected_revision=original["latest_revision"], actor="模拟编辑")
    values.update(changes)
    return ledger.revise_candidate(original["id"], **values)


def selection(left, right):
    return {"left_id": left["id"], "left_revision": left["revision"],
            "right_id": right["id"], "right_revision": right["revision"]}


@pytest.fixture
def setup(tmp_path):
    ledger = Ledger(tmp_path / "data")
    return ledger, ComparisonPlanner(), candidate(ledger, "甲"), candidate(ledger, "乙")


def test_completed_request_replays_once_even_after_model_connection_is_removed(setup):
    ledger, planner, left, right = setup
    selected = selection(left, right)
    first = compare_experiences(ledger, planner, selected, "same-request")
    planner.configured = False
    replay = compare_experiences(ledger, planner, selected, "same-request")
    assert first["id"] == replay["id"]
    assert planner.calls == 1
    assert len(ledger.list_relations()) == 1
    with ledger._connection() as db:
        record = dict(db.execute("SELECT * FROM comparison_requests WHERE id='same-request'").fetchone())
    assert record["status"] == "completed"
    assert record["tokens"] == 13
    assert record["model"] == planner.model
    assert record["action_json"]


def test_request_id_cannot_be_reused_for_a_different_selected_pair(setup):
    ledger, planner, left, right = setup
    compare_experiences(ledger, planner, selection(left, right), "bound-request")
    with pytest.raises(LedgerError) as changed:
        compare_experiences(ledger, planner, selection(right, left), "bound-request")
    assert changed.value.status == 409
    assert planner.calls == 1
    assert len(ledger.list_relations()) == 1


def test_http_comparison_only_sends_selected_versions_and_cited_quotes(tmp_path):
    planner = ComparisonPlanner()
    app = create_app(tmp_path / "web", planner=planner)
    ledger = app.extensions["ledger"]
    secret = "SHARED_UNLINKED_RAW_SENTINEL"
    left = candidate(ledger, "甲", secret_suffix=secret)
    right = candidate(ledger, "乙")
    revise(ledger, left, title="PENDING_DRAFT_SENTINEL", statement="PENDING_DRAFT_STATEMENT_SENTINEL")
    ledger.add_feedback(left["id"], left["revision"], "合成使用者", "FEEDBACK_SENTINEL", False, "未知", "", "")
    client = app.test_client()
    value = {**selection(left, right), "request_id": "http-scoped-comparison"}
    response = client.post("/api/experience-relations/suggest", json=value)
    assert response.status_code == 201, response.get_json()
    repeated = client.post("/api/experience-relations/suggest", json=value)
    assert repeated.status_code == 201
    assert planner.calls == 1
    sent = planner.contexts[0]
    serialized = json.dumps(sent, ensure_ascii=False)
    for prohibited in (secret, "PENDING_DRAFT_SENTINEL", "PENDING_DRAFT_STATEMENT_SENTINEL", "FEEDBACK_SENTINEL", "source_text", "revisions", "feedback_history"):
        assert prohibited not in serialized
    assert sent["left"]["revision"] == left["revision"]
    assert sent["right"]["revision"] == right["revision"]
    assert set(sent["left"]["evidence"][0]) <= {"field", "source_type", "source_id", "start", "end", "quote", "locator"}
    assert response.get_json()["status"] == "proposed"


def test_relation_committed_before_request_mark_is_reused_after_a_crash(setup, monkeypatch):
    ledger, planner, left, right = setup
    selected = selection(left, right)
    original_event = ledger._event

    def crash_before_final_commit(db, case_id, event_type, payload):
        if event_type == "relation.model_proposed":
            raise RuntimeError("synthetic process crash after relation commit")
        return original_event(db, case_id, event_type, payload)

    monkeypatch.setattr(ledger, "_event", crash_before_final_commit)
    with pytest.raises(RuntimeError):
        compare_experiences(ledger, planner, selected, "relation-commit-window")
    assert len(ledger.list_relations()) == 1
    assert planner.calls == 1
    with ledger._connection() as db:
        assert db.execute("SELECT status FROM comparison_requests WHERE id='relation-commit-window'").fetchone()[0] == "running"
    monkeypatch.setattr(ledger, "_event", original_event)
    restored = compare_experiences(ledger, planner, selected, "relation-commit-window")
    assert restored["id"] == ledger.list_relations()[0]["id"]
    assert planner.calls == 1
    with ledger._connection() as db:
        assert db.execute("SELECT status FROM comparison_requests WHERE id='relation-commit-window'").fetchone()[0] == "completed"


def test_cached_successful_model_output_survives_a_crash_before_relation_write(setup, monkeypatch):
    ledger, planner, left, right = setup
    selected = selection(left, right)
    original_propose = ledger.propose_relation

    def crash_before_relation(**kwargs):
        raise RuntimeError("synthetic process crash before relation write")

    monkeypatch.setattr(ledger, "propose_relation", crash_before_relation)
    with pytest.raises(RuntimeError):
        compare_experiences(ledger, planner, selected, "cached-output-window")
    assert ledger.list_relations() == []
    assert planner.calls == 1
    with ledger._connection(write=True) as db:
        assert db.execute("SELECT action_json FROM comparison_requests WHERE id='cached-output-window'").fetchone()[0]
        db.execute("UPDATE comparison_requests SET lease_until=0 WHERE id='cached-output-window'")
    monkeypatch.setattr(ledger, "propose_relation", original_propose)
    restored = compare_experiences(ledger, planner, selected, "cached-output-window")
    assert restored["status"] == "proposed"
    assert planner.calls == 1
    assert len(ledger.list_relations()) == 1


def test_live_request_lease_prevents_a_second_concurrent_model_call(setup):
    ledger, planner, left, right = setup
    planner.entered, planner.release = Event(), Event()
    selected = selection(left, right)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(compare_experiences, ledger, planner, selected, "concurrent-request")
        try:
            assert planner.entered.wait(3)
            with pytest.raises(LedgerError) as concurrent:
                compare_experiences(ledger, planner, selected, "concurrent-request")
            assert concurrent.value.status == 409
            assert planner.calls == 1
        finally:
            planner.release.set()
        assert first.result(timeout=5)["status"] == "proposed"
    assert len(ledger.list_relations()) == 1


def test_model_failure_preserves_original_experiences_and_records_visible_error(setup):
    ledger, planner, left, right = setup
    before = [ledger.get_candidate(c["id"]) for c in (left, right)]
    planner.fail = True
    with pytest.raises(LedgerError) as failed:
        compare_experiences(ledger, planner, selection(left, right), "failed-request")
    assert failed.value.status == 502
    assert ledger.list_relations() == []
    assert [ledger.get_candidate(c["id"]) for c in (left, right)] == before
    with ledger._connection() as db:
        row = dict(db.execute("SELECT * FROM comparison_requests WHERE id='failed-request'").fetchone())
    assert row["status"] == "error"
    assert row["error"]
    assert row["action_json"] is None


@pytest.mark.parametrize("selected_revision", [1, 2])
def test_comparison_reads_only_selected_revision_sources_and_does_not_hide_its_corruption(setup, selected_revision):
    ledger, planner, left, right = setup
    text = "只属于v2草稿的资料"
    source = ledger.add_source(left["case_id"], "v2资料.txt", text)
    refs = deepcopy(left["evidence"])
    refs.append(dict(field="scope", source_type="document", source_id=source["id"], start=0, end=len(text), quote=text))
    revise(ledger, left, evidence=refs)
    (ledger.blobs / (source["sha256"] + ".txt")).write_bytes(b"synthetic-corruption")
    selected = {**selection(left, right), "left_revision": selected_revision}
    if selected_revision == 1:
        result = compare_experiences(ledger, planner, selected, "unrelated-source")
        assert result["left_revision"] == 1
        assert planner.calls == 1
    else:
        with pytest.raises(LedgerError) as corrupted:
            compare_experiences(ledger, planner, selected, "selected-source")
        assert corrupted.value.status == 409
        assert planner.calls == 0
        assert ledger.list_relations() == []
        with ledger._connection() as db:
            assert db.execute("SELECT status FROM comparison_requests WHERE id='selected-source'").fetchone()[0] == "error"
