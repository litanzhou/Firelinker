"""Reusable synthetic planning evaluation; no business tool is executed here.

Mechanical checks establish structure and verbatim provenance only. The report
deliberately leaves interview quality for a human reviewer instead of assigning
an automatic semantic score to a selected model.
"""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from importlib.resources import files
import json
from time import perf_counter

from pydantic import ValidationError

from .interviews.contracts import InterviewAction
from .interviews.planner import PROMPT_VERSION, PlannerError


def _load_fixtures():
    raw = files("experience_ledger").joinpath("examples/interview_evaluation.json").read_text(encoding="utf-8")
    fixtures = json.loads(raw)
    return fixtures, sha256(raw.encode("utf-8")).hexdigest()


def _fixture_issues(scenario, context):
    issues = []
    if not isinstance(scenario.get("id"), str) or not scenario["id"].startswith("synthetic-"):
        issues.append("情境必须有明确的合成标识")
    questions = scenario.get("review_questions")
    if not isinstance(questions, list) or not questions or any(
            not isinstance(q, dict) or not q.get("dimension") or not q.get("question") for q in questions):
        issues.append("缺少供人工评阅的维度和问题")
    sources = scenario.get("sources", [])
    identifiers = [(s.get("source_type"), s.get("source_id")) for s in sources]
    if len(set(identifiers)) != len(identifiers):
        issues.append("合成来源标识重复")
    if any(s.get("source_type") not in ("turn", "document") or not isinstance(s.get("text"), str)
           or not s.get("source_id") for s in sources):
        issues.append("合成来源类型、标识或正文无效")
    by_id = {(s.get("source_type"), s.get("source_id")): s for s in sources}
    for turn in context.get("turns", []):
        source = by_id.get(("turn", turn.get("id")))
        if source is None or source["text"] != turn.get("answer"):
            issues.append("上下文中的回答与合成原话不一致")
        if turn.get("correction_of") and ("turn", turn["correction_of"]) not in by_id:
            issues.append("纠正指向不存在的原话")
    for result in (context.get("last_tool") or {}).get("results", []):
        source = by_id.get((result.get("source_type"), result.get("source_id")))
        start, text = result.get("start"), result.get("text")
        if source is None or type(start) is not int or not isinstance(text, str) or source["text"][start:start + len(text)] != text:
            issues.append("上下文工具结果与合成来源位置不一致")
    focus = context.get("focus") or {}
    cases = {case.get("id"): case for case in context.get("cases", [])}
    if focus.get("case_id") not in cases:
        issues.append("当前焦点未指向合成案例")
    elif focus.get("decision_id") not in {d.get("id") for d in cases[focus["case_id"]].get("decisions", [])}:
        issues.append("当前焦点未指向该案例中的判断")
    return issues


def _check_references(action, scenario, context):
    source_map = {(s["source_type"], s["source_id"]): s for s in scenario["sources"]}
    references = [("evidence", ref.model_dump()) for ref in action.evidence]
    for gap in action.gaps:
        references.extend(("gaps." + gap.key, ref.model_dump()) for ref in gap.evidence)
    references.extend(("relation_evidence", ref) for ref in action.relation_evidence)
    checks = []
    for path, ref in references:
        errors = []
        source = source_map.get((ref.get("source_type"), ref.get("source_id")))
        if source is None:
            errors.append("来源ID或类型不在本合成情境中")
        else:
            quote, start, original = ref.get("quote"), ref.get("start"), source["text"]
            if not isinstance(quote, str) or not quote:
                errors.append("文本引用缺少逐字引文")
            else:
                if start is None:
                    if original.count(quote) != 1:
                        errors.append("省略位置的引文无法唯一定位")
                    else:
                        start = original.find(quote)
                if start is not None and (type(start) is not int or not 0 <= start < start + len(quote) <= len(original)
                                          or original[start:start + len(quote)] != quote):
                    errors.append("引文与Unicode码点位置的原文不一致")
            if path == "evidence" and action.action == "draft_experience":
                contexts = [c.model_dump() for c in action.contexts] or [context.get("focus", {})]
                if not any(source.get("case_id") == c.get("case_id") and
                           (c.get("decision_id") is None or source.get("decision_id") is None
                            or source.get("decision_id") == c["decision_id"]) for c in contexts):
                    errors.append("引文不属于本次草稿明确列出的案例或判断")
        checks.append({"path": path, "source_id": ref.get("source_id"), "source_type": ref.get("source_type"),
                       "status": "failed" if errors else "passed", "issues": errors})
    return {"status": "failed" if any(c["status"] == "failed" for c in checks) else "passed" if checks else "not_applicable",
            "checked": len(checks), "items": checks,
            "meaning": "只检查来源与逐字位置，不判断引文是否支持模型的解释。"}


def _literal_question_signals(action, context):
    if action.action != "ask_expert":
        return []
    normalize = lambda text: "".join(text.split()).casefold()
    focus = context.get("focus", {})
    signals = []
    for old in context.get("question_history", []):
        if normalize(old.get("question", "")) == normalize(action.question):
            signals.append({"kind": "same_wording", "prior_question_id": old.get("id"),
                            "same_focus": old.get("case_id") == focus.get("case_id") and old.get("decision_id") == focus.get("decision_id"),
                            "interpretation": "相同措辞可能合理，交由人工结合案例和判断核对。"})
    return signals


def run_evaluation(planner=None, run_model=False):
    """Return a JSON-serializable report for seven packaged synthetic scenarios.

    The default never inspects or invokes a planner. ``run_model=True`` requires
    an explicitly supplied, configured planner and calls ``propose`` once per
    valid scenario. Returned actions are recorded and checked, never executed.
    Exceptions are recorded per scenario so later scenarios remain evaluable.
    """
    if type(run_model) is not bool:
        raise TypeError("run_model必须是显式布尔值")
    fixtures, fixture_hash = _load_fixtures()
    errors = []
    if fixtures.get("format_version") != 1 or fixtures.get("synthetic") is not True:
        errors.append({"stage": "fixtures", "type": "invalid_manifest", "message": "评测包必须声明版本1及纯合成来源"})
    scenarios = fixtures.get("scenarios", [])
    if not 6 <= len(scenarios) <= 8 or len({s.get("id") for s in scenarios}) != len(scenarios):
        errors.append({"stage": "fixtures", "type": "invalid_scenarios", "message": "评测包应有6至8个不同的合成情境"})
    configured = False
    model = None
    if run_model:
        configured = planner is not None and getattr(planner, "configured", False) is True
        model = getattr(planner, "model", None) if planner is not None else None
        if not configured:
            errors.append({"stage": "configuration", "type": "planner_required", "message": "需要调用者显式提供已配置的模型规划器；没有自动选择或调用模型。"})
    rows = []
    attempted, completed, tokens, unknown_usage = 0, 0, 0, 0
    for scenario in scenarios:
        context = deepcopy(fixtures.get("base_context", {}))
        context.update(deepcopy(scenario.get("context", {})))
        issues = _fixture_issues(scenario, context)
        row = {"id": scenario.get("id"), "title": scenario.get("title"), "tags": scenario.get("tags", []),
               "context": deepcopy(context), "sources": deepcopy(scenario.get("sources", [])),
               "fixture_check": {"status": "failed" if issues else "passed", "issues": issues},
               "model_run": "not_requested", "model": model, "prompt_version": PROMPT_VERSION,
               "action": None, "tokens": None, "elapsed_ms": None,
               "structure": {"status": "not_run", "issues": []},
               "references": {"status": "not_run", "checked": 0, "items": []},
               "signals": [], "errors": [],
               "human_review": [{**q, "status": "not_run", "verdict": None, "notes": ""}
                                for q in scenario.get("review_questions", [])]}
        rows.append(row)
        if not run_model:
            continue
        if errors or issues or not configured:
            row["model_run"] = "skipped"
            continue
        attempted += 1
        started = perf_counter()
        usage_seen = False
        try:
            proposal = planner.propose(deepcopy(context))
            row["model"] = proposal.model
            if type(proposal.tokens) is int and proposal.tokens >= 0:
                row["tokens"] = proposal.tokens
                tokens += proposal.tokens
            else:
                unknown_usage += 1
                row["errors"].append({"stage": "usage", "type": "invalid_usage", "message": "模型未报告有效的非负整数用量。"})
            usage_seen = True
            row["action"] = json.loads(json.dumps(proposal.action, ensure_ascii=False))
            for question in row["human_review"]:
                question["status"] = "pending"
            try:
                action = InterviewAction.model_validate(proposal.action)
            except ValidationError as exc:
                row["structure"] = {"status": "failed", "issues": exc.errors(include_input=False, include_context=False)}
            else:
                row["structure"] = {"status": "passed", "issues": []}
                row["references"] = _check_references(action, scenario, context)
                row["signals"] = _literal_question_signals(action, context)
            row["model_run"] = "completed"
            completed += 1
        except PlannerError as exc:
            row["model_run"] = "failed"
            row["errors"].append({"stage": "planning", "type": "PlannerError", "message": str(exc)[:1000]})
            if not usage_seen:
                unknown_usage += 1
        except Exception as exc:
            row["model_run"] = "failed"
            # Unknown provider exceptions may include credentials or raw bodies.
            # Preserve the failure category without recording their string body.
            row["errors"].append({"stage": "planning", "type": type(exc).__name__, "message": "规划器或返回内容异常，未执行工具；请检查本地调用日志。"})
            if not usage_seen:
                unknown_usage += 1
        finally:
            row["elapsed_ms"] = round((perf_counter() - started) * 1000, 2)
    mechanical_failures = sum(r["fixture_check"]["status"] == "failed" or r["structure"]["status"] == "failed"
                              or r["references"]["status"] == "failed" or bool(r["errors"]) for r in rows)
    status = "contains_failures" if errors or mechanical_failures else "awaiting_human_review" if run_model else "fixtures_validated"
    return {"format_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
            "synthetic": True, "fixture_version": fixtures.get("format_version"), "fixture_sha256": fixture_hash,
            "mode": "model" if run_model else "fixtures_only", "status": status,
            "prompt_version": PROMPT_VERSION, "requested_model": model, "business_tools_executed": False,
            "semantic_quality": "awaiting_human_review" if completed else "not_evaluated",
            "not_assessed": ["专业结论是否正确", "引文是否支持模型解释", "真实业务权限执行", "多轮访谈整体质量", "真实专家经验可靠性"],
            "summary": {"scenarios": len(rows), "fixtures_valid": sum(r["fixture_check"]["status"] == "passed" for r in rows),
                        "calls_attempted": attempted, "calls_completed": completed, "mechanical_failures": mechanical_failures,
                        "reported_tokens": tokens, "calls_with_unknown_usage": unknown_usage,
                        "human_review_items": sum(len(r["human_review"]) for r in rows)},
            "errors": errors, "scenarios": rows}
