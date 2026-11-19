"""Durable single-planner graph. Only the store may change business records."""

from hashlib import sha256
from dataclasses import asdict, is_dataclass
import json
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command
from langgraph.checkpoint.sqlite import SqliteSaver
from langsmith import tracing_context
from pydantic import ValidationError

from ..service import LedgerError, _id
from .contracts import (InterviewAction, StartInterview, AnswerInterview, ControlInterview,
                        SupplementInterview, FocusInterview, LinkCase, TurnContext, QualityFeedback)
from .planner import PROMPT, PROMPT_VERSION, PlannerError
from ..model_settings import ModelSettings
from .store import InterviewStore


class FlowState(TypedDict, total=False):
    interview_id: str
    run_id: str
    step: int
    action_id: str
    action: dict
    outcome: dict
    feedback: str
    invalid_count: int
    route: str


class InterviewRuntime:
    def __init__(self, ledger, planner=None):
        self.ledger = ledger
        self.store = InterviewStore(ledger)
        self.models = ModelSettings(ledger.data_dir, planner)
        if hasattr(self.planner, "bind_file_loader") and hasattr(ledger, "file_attachment"):
            self.planner.bind_file_loader(ledger.file_attachment)
        self.checkpoint_path = ledger.data_dir / "interview-checkpoints.sqlite3"

    @property
    def planner(self):
        return self.models.planner

    @planner.setter
    def planner(self, value):
        self.models.override(value)

    def start(self, payload):
        request = StartInterview.model_validate(payload)
        sid = self.store.start(request)
        current = self.store.get(sid)
        if request.start_only:
            return current
        return self.run(sid) if current["status"] == "ready" else current

    def answer(self, sid, payload):
        request = AnswerInterview.model_validate(payload)
        fresh = self.store.answer(sid, request)
        current = self.store.get(sid)
        return self.run(sid) if fresh and current["status"] == "ready" else current

    def control(self, sid, payload):
        request = ControlInterview.model_validate(payload)
        self.store.control(sid, request)
        current = self.store.get(sid)
        return self.run(sid) if current["status"] == "ready" else current

    def supplement(self, sid, payload):
        request = SupplementInterview.model_validate(payload)
        fresh = self.store.supplement(sid, request)
        current = self.store.get(sid)
        return self.run(sid) if fresh and request.continue_interview and not current["executing"] else current

    def set_focus(self, sid, payload):
        self.store.set_focus(sid, FocusInterview.model_validate(payload))
        return self.store.get(sid)

    def link_case(self, sid, payload):
        self.store.link_case(sid, LinkCase.model_validate(payload))
        return self.store.get(sid)

    def link_turn(self, sid, payload):
        self.store.link_turn(sid, TurnContext.model_validate(payload))
        return self.store.get(sid)

    def quality_feedback(self, sid, payload):
        self.store.quality_feedback(sid, QualityFeedback.model_validate(payload))
        return self.store.get(sid)

    def _graph(self, saver, token, planner):
        graph = StateGraph(FlowState)

        def plan(state):
            sid, step = state["interview_id"], state.get("step", 0)
            current = self.store.get(sid)
            action_id = sha256(f"{sid}:{state['run_id']}:{step}".encode()).hexdigest()
            cached = self.store.cached_action(sid, action_id)
            if cached is not None:
                return {"action_id": action_id, "action": cached, "route": "execute"}
            reason = self.store.budget_reason(sid)
            if not reason and step >= current["limits"]["steps_per_run"]:
                reason = "达到本轮工具执行上限；请检查记录后选择继续。"
            if reason:
                self.store.mark(sid, token, "paused", reason)
                return {"route": "end"}
            capabilities = getattr(planner, "capabilities", None)
            declared = asdict(capabilities) if is_dataclass(capabilities) else capabilities if isinstance(capabilities, dict) else {}
            available = declared.get("context_chars", 60000) - len(PROMPT) - len(state.get("feedback", "")) - 1000
            context = self.store.context(sid, state.get("outcome"), max_chars=max(0, available), model_capabilities=declared)
            if available <= 0 or len(json.dumps(context, ensure_ascii=False)) > available:
                self.store.mark(sid, token, "paused", "当前模型的上下文配置容纳不下访谈方法与所选信息，请调整容量或关注范围；全部原话仍保留且可检索。")
                return {"route": "end"}
            cached = self.store.call_record(sid, token, action_id, context, planner.model, PROMPT_VERSION)
            if cached is None:
                try:
                    proposal = planner.propose(context, state.get("feedback", ""))
                    self.store.proposal(sid, token, action_id, proposal)
                    cached = proposal.action
                except PlannerError as exc:
                    self.store.action_error(sid, action_id, str(exc))
                    raise
            return {"action_id": action_id, "action": cached, "route": "execute"}

        def execute(state):
            sid = state["interview_id"]
            try:
                action = InterviewAction.model_validate(state["action"])
                outcome = self.store.apply(sid, token, state["action_id"], action)
            except (ValidationError, LedgerError) as exc:
                if isinstance(exc, LedgerError) and exc.status == 409:
                    raise
                error = (json.dumps(exc.errors(include_input=False, include_context=False), ensure_ascii=False)[:2500]
                         if isinstance(exc, ValidationError) else str(exc))
                self.store.action_error(sid, state["action_id"], error)
                count = state.get("invalid_count", 0) + 1
                if count >= 2:
                    self.store.mark(sid, token, "paused", "模型连续两次动作未通过校验，已暂停：" + error)
                    return {"route": "end"}
                return {"step": state.get("step", 0) + 1, "feedback": error, "invalid_count": count, "route": "plan"}
            route = "wait" if outcome["kind"] == "ask_expert" else "plan" if outcome["kind"] in (
                "search_sources", "read_source", "input_already_saved", "context_changed", "create_case", "create_decision", "link_turn", "defer_topic") else "end"
            return {"outcome": outcome, "feedback": "", "invalid_count": 0, "step": state.get("step", 0) + 1, "route": route}

        def wait(state):
            # No business writes before interrupt: this node restarts on resume.
            interrupt(state["outcome"]["question"])
            return {"run_id": _id(), "step": 0, "feedback": "", "invalid_count": 0, "outcome": {}}

        graph.add_node("plan", plan)
        graph.add_node("execute", execute)
        graph.add_node("wait", wait)
        graph.add_edge(START, "plan")
        graph.add_conditional_edges("plan", lambda s: s["route"], {"execute": "execute", "end": END})
        graph.add_conditional_edges("execute", lambda s: s["route"], {"wait": "wait", "plan": "plan", "end": END})
        graph.add_edge("wait", "plan")
        return graph.compile(checkpointer=saver)

    def run(self, sid):
        with self.models.use() as planner:
            return self._run(sid, planner)

    def _run(self, sid, planner):
        if hasattr(planner, "bind_file_loader"):
            planner.bind_file_loader(self.ledger.file_attachment)
        token = self.store.acquire(sid)
        try:
            # Initial text is an input request, not a model-generated question.
            # It also works before a model is configured and survives restarts.
            if self.store.request_initial_text(sid, token):
                pass
            elif not planner.configured:
                self.store.mark(sid, token, "error", "尚未配置模型。访谈主题已保存，请在“模型设置”中配置后继续。")
            else:
                self._run_graph(sid, token, planner)
        except PlannerError as exc:
            self.store.mark(sid, token, "error", str(exc))
        except LedgerError:
            raise
        except Exception:
            self.store.mark(sid, token, "error", "访谈流程执行失败，原始记录已保存；请查看本地日志。")
            raise
        finally:
            self.store.release(sid, token)
        return self.store.get(sid)

    def _run_graph(self, sid, token, planner):
        with SqliteSaver.from_conn_string(str(self.checkpoint_path)) as saver:
            graph = self._graph(saver, token, planner)
            config = {"configurable": {"thread_id": sid}, "recursion_limit": 40}
            snapshot = graph.get_state(config)
            if "wait" in snapshot.next:
                value = Command(resume={"input_saved": True})
            elif snapshot.next:
                value = None  # Retry the failed node, keeping its operation key.
            else:
                value = {"interview_id": sid, "run_id": _id(), "step": 0, "feedback": "", "invalid_count": 0, "outcome": {}}
            # Interview data remains local even if ambient LangSmith tracing is on.
            with tracing_context(enabled=False):
                graph.invoke(value, config=config)
