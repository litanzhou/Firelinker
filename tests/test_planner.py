"""Real SDK serialization through a local HTTP transport; no live credentials."""

import json

import httpx
from openai import OpenAI
import pytest

from experience_ledger.interviews.planner import OpenAIPlanner, PlannerError, PROMPT


def make_planner(handler, provider="compatible"):
    planner = OpenAIPlanner("synthetic-model", "http://127.0.0.1:9999/v1", "synthetic-placeholder", provider=provider)
    planner.close()
    planner._client = OpenAI(base_url="http://127.0.0.1:9999/v1", api_key="synthetic-placeholder", max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return planner


@pytest.mark.parametrize("provider", ["openai", "deepseek", "compatible"])
def test_sdk_tool_contract_context_and_provider_token_parameter(provider):
    def handler(request):
        payload = json.loads(request.content)
        assert payload["tools"][0]["function"]["name"] == "propose_interview_action"
        assert "gaps" in payload["tools"][0]["function"]["parameters"]["properties"]
        assert "goal" in payload["messages"][1]["content"]
        assert payload["messages"][0] == {"role": "system", "content": PROMPT}
        assert ("max_completion_tokens" in payload) == (provider == "openai")
        return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 0, "model": "synthetic-version-1",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "propose_interview_action", "arguments": json.dumps({"action": "pause", "reason": "合成测试"})}}
            ]}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    planner = make_planner(handler, provider)
    try:
        proposal = planner.propose({"goal": "合成目标"})
        assert proposal.tokens == 15 and proposal.model == "synthetic-version-1"
        assert proposal.action["action"] == "pause"
    finally:
        planner.close()


def test_provider_failures_do_not_expose_raw_headers_or_responses():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "SYNTHETIC_PRIVATE_MARKER", "type": "authentication_error"}})
    planner = make_planner(handler)
    try:
        with pytest.raises(PlannerError) as failure:
            planner.propose({"goal": "合成"})
        assert "SYNTHETIC_PRIVATE_MARKER" not in str(failure.value)
        assert "synthetic-placeholder" not in str(failure.value)
    finally:
        planner.close()


def test_truncated_model_output_is_an_error():
    planner = make_planner(lambda request: httpx.Response(200, json={"id": "s", "object": "chat.completion", "created": 0, "model": "s",
        "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "incomplete"}}]}))
    try:
        with pytest.raises(PlannerError):
            planner.propose({"goal": "合成"})
    finally:
        planner.close()
