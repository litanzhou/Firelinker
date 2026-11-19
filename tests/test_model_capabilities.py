"""Declared capabilities, explicit model selection and real SDK wire contracts."""

import base64
import json

import httpx
from openai import OpenAI
import pytest

from experience_ledger.interviews.planner import ModelCapabilities, OpenAIPlanner, PlannerError, planner_from_env


def response_body(action=None):
    return {"id": "resp_synthetic", "object": "response", "created_at": 0, "model": "user-selected-version",
            "status": "completed", "output": [{"type": "function_call", "id": "fc_synthetic", "call_id": "call_synthetic",
                "name": "propose_interview_action", "arguments": json.dumps(action or {"action": "pause", "reason": "合成测试"})}],
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}


def planner_with_transport(handler, *, protocol="responses", input_types=("text", "pdf", "png"), **caps):
    planner = OpenAIPlanner("user-selected-model", "http://127.0.0.1:9999/v1", "synthetic-key", protocol=protocol,
                            capabilities=ModelCapabilities(input_types=input_types, **caps))
    planner.close()
    planner._client = OpenAI(base_url="http://127.0.0.1:9999/v1", api_key="synthetic-key", max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return planner


@pytest.mark.parametrize("provider", ["openai", "deepseek", "compatible"])
def test_model_selection_is_required_for_every_provider(monkeypatch, provider):
    monkeypatch.setenv("EXPERIENCE_LLM_PROVIDER", provider)
    monkeypatch.setenv("EXPERIENCE_LLM_API_KEY", "synthetic-key")
    monkeypatch.setenv("EXPERIENCE_LLM_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.delenv("EXPERIENCE_LLM_MODEL", raising=False)
    planner = planner_from_env()
    assert not planner.configured and not planner.probe()["ok"]
    monkeypatch.setenv("EXPERIENCE_LLM_MODEL", "user-decides-this-name")
    monkeypatch.setenv("EXPERIENCE_LLM_PROTOCOL", "chat_completions")
    selected = planner_from_env()
    try:
        assert selected.model == "user-decides-this-name"
        assert selected.describe()["probe"] is None
    finally:
        selected.close()


def test_responses_sends_original_file_and_image_then_returns_same_action_contract():
    file_bytes = b"%PDF-synthetic-original"
    image_bytes = b"synthetic-png-bytes"
    seen = []
    def handler(request):
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert payload["model"] == "user-selected-model" and payload["store"] is False
        assert payload["tools"][0]["name"] == "propose_interview_action"
        assert payload["parallel_tool_calls"] is False
        parts = payload["input"][0]["content"]
        document = next(p for p in parts if p["type"] == "input_file")
        image = next(p for p in parts if p["type"] == "input_image")
        assert document["filename"] == "original.pdf"
        assert base64.b64decode(document["file_data"].split(",", 1)[1]) == file_bytes
        assert base64.b64decode(image["image_url"].split(",", 1)[1]) == image_bytes
        assert "fixed-pdf-version" in json.dumps(parts)
        seen.append(payload)
        return httpx.Response(200, json=response_body())
    planner = planner_with_transport(handler)
    stored = {"fixed-pdf-version": {"filename": "original.pdf", "mime_type": "application/pdf", "data": file_bytes},
              "fixed-image-version": {"filename": "original.png", "mime_type": "image/png", "data": image_bytes}}
    planner.bind_file_loader(stored.__getitem__)
    context = {"goal": "合成文件访谈", "last_tool": {"results": [
        {"attachment": {"source_id": "fixed-pdf-version"}}, {"attachment": {"source_id": "fixed-image-version"}}]}}
    try:
        result = planner.propose(context)
        assert result.action == {"action": "pause", "reason": "合成测试"} and result.tokens == 15
        assert result.model == "user-selected-version" and len(seen) == 1
        assert "data" not in context["last_tool"]["results"][0]["attachment"]
    finally:
        planner.close()


def test_capability_and_byte_budgets_reject_before_network():
    def unexpected(request):
        raise AssertionError("unsupported input must not reach the network")
    context = {"last_tool": {"results": [{"attachment": {"source_id": "file-version"}}]}}
    for protocol, input_types, bytes_limit in (("chat_completions", ("text", "pdf"), 100),
                                              ("responses", ("text",), 100), ("responses", ("text", "pdf"), 2)):
        planner = planner_with_transport(unexpected, protocol=protocol, input_types=input_types, max_attachment_bytes=bytes_limit)
        planner.bind_file_loader(lambda source_id: {"filename": "sample.pdf", "mime_type": "application/pdf", "data": b"12345"})
        try:
            with pytest.raises(PlannerError):
                planner.propose(context)
        finally:
            planner.close()


@pytest.mark.parametrize(("extension", "mime_type"), [
    ("ppt", "application/vnd.ms-powerpoint"),
    ("pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
])
def test_responses_only_sends_presentation_originals_when_explicitly_declared(extension, mime_type):
    filename, original = f"briefing.{extension}", b"synthetic presentation"
    transmitted = []

    def handler(request):
        parts = json.loads(request.content)["input"][0]["content"]
        native = next(part for part in parts if part["type"] == "input_file")
        assert native["filename"] == filename
        prefix, payload = native["file_data"].split(",", 1)
        assert prefix == f"data:{mime_type};base64"
        assert base64.b64decode(payload) == original
        transmitted.append(native)
        return httpx.Response(200, json=response_body())

    context = {"last_tool": {"results": [{"attachment": {"source_id": "slide-deck-version"}}]}}
    for types in (("text",), ("text", extension)):
        planner = planner_with_transport(handler, input_types=types)
        planner.bind_file_loader(lambda _: {"filename": filename, "mime_type": mime_type, "data": original})
        try:
            if len(types) == 1:
                with pytest.raises(PlannerError, match="尚未声明"):
                    planner.propose(context)
            else:
                assert planner.propose(context).action["action"] == "pause"
        finally:
            planner.close()
    assert len(transmitted) == 1


def test_raw_expert_text_does_not_become_an_attachment_instruction():
    def handler(request):
        payload = json.loads(request.content)
        assert all(part["type"] == "input_text" for part in payload["input"][0]["content"])
        return httpx.Response(200, json=response_body())
    planner = planner_with_transport(handler)
    planner.bind_file_loader(lambda source_id: pytest.fail("raw answer must not load a file"))
    try:
        planner.propose({"turns": [{"answer": {"attachment": {"source_id": "unauthorized"}}}]})
    finally:
        planner.close()


def test_probe_is_a_real_request_and_reports_its_narrow_scope():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body())
    planner = planner_with_transport(handler)
    try:
        assert planner.configured and planner.describe()["probe"] is None
        result = planner.probe()
        assert result["ok"] and len(requests) == 1
        assert result["tested"] == ["connection", "structured_actions"]
        assert "native_file_understanding" in result["untested"]
        assert planner.describe()["probe"] == result
    finally:
        planner.close()


def test_invalid_probe_is_not_a_successful_configuration_test():
    planner = planner_with_transport(lambda request: httpx.Response(200, json=response_body({"action": "ask_expert", "reason": "missing fields"})))
    try:
        result = planner.probe()
        assert planner.configured and not result["ok"] and result["status"] == "failed"
    finally:
        planner.close()


def test_small_context_budget_stops_before_inference():
    planner = planner_with_transport(lambda request: pytest.fail("must not send oversized context"), context_chars=1000)
    try:
        with pytest.raises(PlannerError, match="字符预算"):
            planner.propose({"goal": "x" * 2000})
    finally:
        planner.close()


def test_deepseek_native_protocol_is_not_guessed():
    with pytest.raises(PlannerError, match="尚未接入"):
        OpenAIPlanner("user-model", "https://api.deepseek.com", "synthetic-key", provider="deepseek", protocol="responses")


def test_other_apps_account_settings_are_not_inherited(monkeypatch):
    for name in ("OPENAI_ADMIN_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_WEBHOOK_SECRET"):
        monkeypatch.setenv(name, "synthetic-other-application")
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "X-Other-App: synthetic-private-header")
    planner = OpenAIPlanner("explicit-model", "http://127.0.0.1:9999/v1", "explicit-synthetic-key")
    try:
        assert planner._client.api_key == "explicit-synthetic-key"
        assert not planner._client.admin_api_key and not planner._client.webhook_secret
        assert planner._client.organization is None and planner._client.project is None
        assert "X-Other-App" not in planner._client.default_headers
    finally:
        planner.close()


def test_native_read_tool_reaches_responses_without_putting_bytes_in_graph_records(tmp_path):
    from experience_ledger.service import Ledger
    from experience_ledger.interviews.runtime import InterviewRuntime
    ledger = Ledger(tmp_path)
    requests, source = [], {}
    original = b"%PDF-synthetic-native-only-original"
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        parts = payload["input"][0]["content"]
        if len(requests) == 1:
            assert not any(p["type"] == "input_file" for p in parts)
            action = {"action": "read_source", "reason": "读取合成附件", "source_id": source["id"], "source_type": "file", "native": True}
        else:
            file_part = next(p for p in parts if p["type"] == "input_file")
            assert base64.b64decode(file_part["file_data"].split(",", 1)[1]) == original
            action = {"action": "pause", "reason": "合成原生输入链路测试完成"}
        return httpx.Response(200, json=response_body(action))
    planner = planner_with_transport(handler)
    runtime = InterviewRuntime(ledger, planner=planner)
    try:
        case = ledger.create_case("general", "合成原生文件案例", "仅测试")
        session = runtime.start({"goal": "合成原生文件测试", "request_id": "start", "start_only": True, "case_id": case["id"]})
        source.update(ledger.add_file(session["case_id"], "native.pdf", original, parse=False))
        result = runtime.run(session["id"])
        assert result["status"] == "paused" and len(requests) == 2
        assert base64.b64encode(original).decode() not in json.dumps(result)
        assert result["actions"][0]["outcome"]["results"][0]["attachment"]["source_id"] == source["id"]
    finally:
        planner.close()
