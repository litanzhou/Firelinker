"""Evaluation verifies mechanical evidence without inventing semantic grades."""

import json

import pytest

from experience_ledger.evaluation import run_evaluation
from experience_ledger.interviews.planner import PlannerError, Proposal


class Planner:
    configured = True
    model = "user-chosen-synthetic-model"

    def __init__(self, callback=None):
        self.calls = 0
        self.callback = callback

    def propose(self, context):
        self.calls += 1
        action = self.callback(context, self.calls) if self.callback else {"action": "pause", "reason": "仅合成规划，不执行工具。"}
        return Proposal(action, self.model, 3)


def test_default_checks_packaged_synthetic_fixtures_without_touching_planner():
    class NeverUse:
        @property
        def configured(self):
            raise AssertionError("默认评测不应检查或调用模型")

        def propose(self, context):
            raise AssertionError("默认评测不应调用模型")

    report = run_evaluation(NeverUse())
    assert report["status"] == "fixtures_validated"
    assert 6 <= report["summary"]["scenarios"] <= 8
    assert report["summary"]["fixtures_valid"] == report["summary"]["scenarios"]
    assert report["summary"]["calls_attempted"] == 0
    assert report["business_tools_executed"] is False
    assert report["semantic_quality"] == "not_evaluated"
    assert all(r["model_run"] == "not_requested" for r in report["scenarios"])
    json.dumps(report, ensure_ascii=False)


def test_explicit_model_plans_once_per_scenario_without_automatic_semantic_score():
    planner = Planner()
    report = run_evaluation(planner, run_model=True)
    assert planner.calls == report["summary"]["scenarios"]
    assert report["summary"]["calls_completed"] == planner.calls
    assert report["summary"]["reported_tokens"] == planner.calls * 3
    assert report["status"] == "awaiting_human_review"
    assert report["semantic_quality"] == "awaiting_human_review"
    assert "score" not in report and "passed" not in report
    for row in report["scenarios"]:
        assert row["structure"]["status"] == "passed"
        assert row["references"]["status"] == "not_applicable"
        assert row["model"] == planner.model
        assert row["prompt_version"] == report["prompt_version"]
        assert all(q["verdict"] is None and q["status"] == "pending" for q in row["human_review"])


def test_model_run_requires_explicit_configured_planner():
    report = run_evaluation(run_model=True)
    assert report["status"] == "contains_failures"
    assert report["errors"][0]["type"] == "planner_required"
    assert report["summary"]["calls_attempted"] == 0
    assert all(row["model_run"] == "skipped" for row in report["scenarios"])


def test_fabricated_quotes_fail_mechanical_checks_even_when_action_schema_is_valid():
    def fabricated(context, _):
        return {"action": "draft_experience", "title": "合成待审条目", "reason": "合成反例",
                "contexts": [context["focus"]],
                "content": [{"key": "statement", "text": "合成陈述", "status": "supported"}],
                "evidence": [{"field": "statement", "source_type": "turn", "source_id": context["turns"][0]["id"],
                              "quote": "这段引文没有出现在合成原话中。", "start": 0}]}
    report = run_evaluation(Planner(fabricated), run_model=True)
    assert report["status"] == "contains_failures"
    assert all(row["structure"]["status"] == "passed" and row["references"]["status"] == "failed" for row in report["scenarios"])
    assert all(row["action"]["evidence"][0]["quote"] for row in report["scenarios"])


def test_valid_unicode_quote_is_only_a_provenance_check():
    def quoted(context, _):
        turn = context["turns"][0]
        return {"action": "draft_experience", "title": "合成待审条目", "reason": "核对原话位置",
                "contexts": [context["focus"]],
                "content": [{"key": "statement", "text": turn["answer"], "status": "supported"}],
                "evidence": [{"field": "statement", "source_type": "turn", "source_id": turn["id"], "quote": turn["answer"], "start": 0}]}
    report = run_evaluation(Planner(quoted), run_model=True)
    perceptual = next(row for row in report["scenarios"] if row["id"] == "synthetic-perceptual-unknown")
    assert perceptual["references"]["status"] == "passed"
    assert "🔧" in perceptual["action"]["evidence"][0]["quote"]
    assert all(q["verdict"] is None for q in perceptual["human_review"])
    # One scenario deliberately quotes case A while focus is case B, exposing
    # missing draft scope without pretending to judge either expert's opinion.
    cross_case = next(row for row in report["scenarios"] if row["id"] == "synthetic-same-wording-new-case")
    assert cross_case["references"]["status"] == "failed"


def test_structurally_invalid_output_is_preserved_and_reported():
    report = run_evaluation(Planner(lambda context, count: {"action": "approve", "reason": "模型没有审核权限"}), run_model=True)
    assert report["status"] == "contains_failures"
    assert all(row["structure"]["status"] == "failed" and row["action"]["action"] == "approve" for row in report["scenarios"])
    json.dumps(report)


def test_one_provider_failure_does_not_hide_later_results():
    def fail_once(context, count):
        if count == 1:
            raise PlannerError("合成网络超时")
        return {"action": "pause", "reason": "后续合成情境继续评测"}
    planner = Planner(fail_once)
    report = run_evaluation(planner, run_model=True)
    assert report["scenarios"][0]["model_run"] == "failed"
    assert report["scenarios"][0]["errors"][0]["message"] == "合成网络超时"
    assert report["summary"]["calls_completed"] == planner.calls - 1
    assert report["summary"]["calls_with_unknown_usage"] == 1
    assert report["status"] == "contains_failures"


def test_unknown_exception_body_is_not_written_into_report():
    def unsafe(context, count):
        raise RuntimeError("SYNTHETIC_SECRET_KEY_DO_NOT_LOG")
    report = run_evaluation(Planner(unsafe), run_model=True)
    assert "SYNTHETIC_SECRET_KEY_DO_NOT_LOG" not in json.dumps(report)
    assert report["scenarios"][0]["errors"][0]["type"] == "RuntimeError"


def test_same_wording_in_different_case_is_signal_not_automatic_failure():
    def repeated(context, count):
        return {"action": "ask_expert", "reason": "当前判断尚无依据", "question": "当时哪个观察让你作出这个选择？",
                "expected_information": "当前案例的实际观察"}
    report = run_evaluation(Planner(repeated), run_model=True)
    row = next(row for row in report["scenarios"] if row["id"] == "synthetic-same-wording-new-case")
    assert row["signals"][0]["same_focus"] is False
    assert row["structure"]["status"] == "passed" and report["status"] == "awaiting_human_review"
    assert all(q["verdict"] is None for q in row["human_review"])


def test_model_call_opt_in_is_a_real_boolean():
    with pytest.raises(TypeError):
        run_evaluation(Planner(), run_model="true")
