"""Local configuration, credential handling and in-flight connection ownership."""

import json
import os
from unittest.mock import patch

import pytest

from experience_ledger.interviews.planner import OpenAIPlanner, Proposal
from experience_ledger.web import create_app


@pytest.fixture(autouse=True)
def clean_model_env(monkeypatch):
    for name in os.environ:
        if name.startswith("EXPERIENCE_LLM_"):
            monkeypatch.delenv(name)


def selection(client, **changes):
    value = client.get("/api/model-settings").json
    selected = {key: value[key] for key in ("provider", "protocol", "input_types", "context_chars",
                                           "max_output_tokens", "max_attachment_bytes")}
    selected.update(expected_revision=value["revision"], base_url="http://127.0.0.1:9999/v1",
                    model="synthetic-model-a", api_key="synthetic-private-key")
    selected.update(changes)
    return selected


def test_save_reload_and_keep_secret_out_of_read_interfaces(tmp_path):
    app = create_app(tmp_path)
    client = app.test_client()
    result = client.put("/api/model-settings", json=selection(client))
    assert result.status_code == 200 and result.json["has_api_key"]
    assert result.json["source"] == "local"
    for path in ("/api/model-settings", "/api/interview-config", "/api/cases"):
        assert b"synthetic-private-key" not in client.get(path).data
    assert result.json["model"] == "synthetic-model-a"
    restarted = create_app(tmp_path).test_client()
    assert restarted.get("/api/model-settings").json == result.json
    assert restarted.get("/api/interview-config").json["model"] == "synthetic-model-a"
    assert client.get("/settings").status_code == 200
    assert client.get("/static/settings.js").status_code == 200
    assert client.get("/api/model-settings").headers["Cache-Control"] == "no-store"
    if os.name != "nt":
        assert (tmp_path / "model.keys.local.json").stat().st_mode & 0o777 == 0o600


def test_blank_keeps_key_and_changed_endpoint_requires_explicit_key_choice(tmp_path):
    client = create_app(tmp_path).test_client()
    assert client.put("/api/model-settings", json=selection(client)).status_code == 200
    assert client.put("/api/model-settings", json=selection(client, api_key=None, model="synthetic-b")).status_code == 200
    path = tmp_path / "model.keys.local.json"
    assert json.loads(path.read_text())["api_key"] == "synthetic-private-key"
    before = path.read_bytes()
    result = client.put("/api/model-settings", json=selection(client, api_key=None, base_url="https://elsewhere.example/v1"))
    assert result.status_code == 400
    assert path.read_bytes() == before
    assert b"synthetic-private-key" not in result.data
    result = client.put("/api/model-settings", json=selection(client, api_key="", base_url="http://localhost:9998/v1"))
    assert result.status_code == 200 and not result.json["has_api_key"]
    assert json.loads(path.read_text())["api_key"] == ""


def test_stale_form_invalid_provider_and_cross_site_write_preserve_settings(tmp_path):
    client = create_app(tmp_path).test_client()
    original = selection(client)
    assert client.put("/api/model-settings", json=original).status_code == 200
    assert client.put("/api/model-settings", json=original).status_code == 409
    assert client.post("/api/model-settings/probe", json=original).status_code == 409
    for change in ({"provider": "unknown"}, {"model": " "}, {"base_url": "https://example.com/v1?key=do-not-echo"},
                   {"context_chars": 10}, {"protocol": "responses", "provider": "deepseek"}):
        result = client.put("/api/model-settings", json=selection(client, **change))
        assert result.status_code == 400
        assert "do-not-echo" not in result.get_data(as_text=True)
    assert client.put("/api/model-settings", json=selection(client), headers={"Origin": "https://elsewhere.example"}).status_code == 403
    assert client.get("/api/interview-config").json["model"] == "synthetic-model-a"


def test_unsaved_probe_and_failed_write_do_not_replace_working_connection(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    client = app.test_client()
    assert client.put("/api/model-settings", json=selection(client)).status_code == 200
    before = client.get("/api/model-settings").json
    seen = []

    def propose(planner, context, feedback=""):
        seen.append((planner.model, context["goal"]))
        return Proposal({"action": "pause", "reason": "合成连接测试"}, planner.model)

    monkeypatch.setattr(OpenAIPlanner, "propose", propose)
    result = client.post("/api/model-settings/probe", json=selection(client, api_key=None, model="unsaved-test-model"))
    assert result.status_code == 200 and result.json["ok"]
    assert seen[0][0] == "unsaved-test-model" and "合成连接测试" in seen[0][1]
    assert client.get("/api/model-settings").json == before

    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr("experience_ledger.model_settings.os.replace", fail)
    result = client.put("/api/model-settings", json=selection(client, model="failed-save-model"))
    assert result.status_code == 500 and b"synthetic-private-key" not in result.data
    assert client.get("/api/model-settings").json == before
    assert not list(tmp_path.glob(".model.keys.local.*.tmp"))
    assert create_app(tmp_path).test_client().get("/api/model-settings").json == before


def test_inflight_run_uses_one_connection_and_closes_it_after_swap(tmp_path):
    app = create_app(tmp_path)
    client = app.test_client()
    client.put("/api/model-settings", json=selection(client))
    runtime = app.extensions["interviews"]
    old = runtime.planner
    seen = []

    def propose(planner, context, feedback=""):
        seen.append(planner.model)
        if len(seen) == 1:
            response = client.put("/api/model-settings", json=selection(client, api_key=None, model="synthetic-b"))
            assert response.status_code == 200
            assert runtime.planner.model == "synthetic-b" and not old._client.is_closed()
            return Proposal({"action": "search_sources", "query": "合成", "reason": "合成检索"}, planner.model)
        return Proposal({"action": "pause", "reason": "合成结束"}, planner.model)

    with patch.object(OpenAIPlanner, "propose", propose):
        session = runtime.start({"goal": "合成热切换测试", "request_id": "switch-during-run"})
        runtime.answer(session["id"], {"question_id": session["pending"]["id"], "request_id": "initial-text",
            "speaker": "合成专家", "answer": "合成原话：观察到接头松动。"})
    assert seen == ["synthetic-model-a", "synthetic-model-a"]
    assert old._client.is_closed()
    assert runtime.planner.model == "synthetic-b" and not runtime.planner._client.is_closed()


def test_environment_remains_available_until_workspace_settings_are_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPERIENCE_LLM_PROVIDER", "compatible")
    monkeypatch.setenv("EXPERIENCE_LLM_MODEL", "synthetic-env-model")
    monkeypatch.setenv("EXPERIENCE_LLM_BASE_URL", "http://127.0.0.1:9987/v1")
    client = create_app(tmp_path).test_client()
    assert client.get("/api/model-settings").json["source"] == "environment"
    assert client.get("/api/interview-config").json["model"] == "synthetic-env-model"
    client.put("/api/model-settings", json=selection(client))
    assert create_app(tmp_path).test_client().get("/api/interview-config").json["model"] == "synthetic-model-a"
