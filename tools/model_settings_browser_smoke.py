"""Browser configuration through real SDK calls to a disposable local model."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from flask import Flask, jsonify, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from experience_ledger.interviews.planner import UnconfiguredPlanner
from experience_ledger.web import create_app
from interview_browser_smoke import synthetic_action


def serve(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    expect.set_options(timeout=20000)
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".local-artifacts/model-settings"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_app = Flask("synthetic-settings-model")
    calls = []

    @model_app.post("/v1/chat/completions")
    @model_app.post("/v1/responses")
    def completion():
        payload = request.get_json()
        model = payload["model"]
        calls.append((request.path, model, request.headers.get("Authorization")))
        if model == "synthetic-auth-failure":
            return jsonify(error={"message": "synthetic-secret-must-not-be-echoed"}), 401
        if request.path.endswith("responses"):
            action = {"action": "pause", "reason": "合成 Responses 连接测试"}
            return jsonify(id="resp_synthetic", object="response", created_at=0, status="completed", model=model,
                           output=[{"type": "function_call", "id": "fc_synthetic", "call_id": "call_synthetic",
                                    "name": "propose_interview_action", "arguments": json.dumps(action)}],
                           usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        context = json.loads(payload["messages"][1]["content"])
        action = synthetic_action(context) if "turns" in context else {"action": "pause", "reason": "合成连接测试"}
        if len(context.get("turns", [])) >= 2:
            action = {"action": "ask_expert", "question": "这次观察与上一次有什么不同？",
                      "reason": "合成测试：切换后的模型读取新回答", "expected_information": "新增观察"}
        return jsonify(id="synthetic", object="chat.completion", created=0, model=model,
                       choices=[{"index": 0, "finish_reason": "tool_calls",
                                 "message": {"role": "assistant", "content": None, "tool_calls": [
                                     {"id": "synthetic-call", "type": "function",
                                      "function": {"name": "propose_interview_action", "arguments": json.dumps(action)}}
                                 ]}}], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    model_server, model_thread = serve(model_app)
    try:
        with TemporaryDirectory(prefix="model-settings-browser-") as directory:
            app = create_app(directory, planner=UnconfiguredPlanner())
            server, thread = serve(app)
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base + "/interviews", wait_until="networkidle")
                    page.get_by_role("link", name="模型设置", exact=True).click()
                    expect(page.get_by_role("heading", name="模型设置", exact=True)).to_be_visible()
                    page.get_by_label("API 地址", exact=True).fill(f"http://127.0.0.1:{model_server.server_port}/v1")
                    page.get_by_label("模型名称", exact=True).fill("synthetic-browser-a")
                    page.get_by_label("API Key", exact=True).fill("synthetic-secret-must-not-be-echoed")
                    page.get_by_role("button", name="测试连接", exact=True).click()
                    expect(page.locator("#settings-message")).to_contain_text("连接成功")
                    assert not page.request.get(base + "/api/interview-config").json()["configured"]
                    assert not (Path(directory) / "model.keys.local.json").exists()
                    page.get_by_role("button", name="保存并应用", exact=True).click()
                    expect(page.locator("#settings-message")).to_contain_text("配置已保存")
                    expect(page.locator("#api-key")).to_have_value("")
                    assert calls[0][2] == "Bearer synthetic-secret-must-not-be-echoed"
                    for path in ("/api/model-settings", "/api/interview-config"):
                        assert "synthetic-secret-must-not-be-echoed" not in page.request.get(base + path).text()
                    page.reload(wait_until="networkidle")
                    expect(page.get_by_label("模型名称", exact=True)).to_have_value("synthetic-browser-a")
                    expect(page.locator("#api-key")).to_have_attribute("placeholder", "已设置，留空保留")
                    for width, height in ((1366, 768), (1440, 900)):
                        page.set_viewport_size({"width": width, "height": height})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        page.screenshot(path=str(args.output_dir / f"settings-{width}.png"))
                    page.get_by_role("link", name="智能访谈", exact=True).click()
                    calls_before_start = len(calls)
                    page.locator('#start-interview [name="goal"]').fill("合成 UI 配置后访谈")
                    page.get_by_role("button", name="开始访谈", exact=True).click()
                    expect(page.locator("#session-status")).to_have_text("等待回答")
                    expect(page.locator("#question-label")).to_have_text("先提供一段内容")
                    assert len(calls) == calls_before_start
                    page.locator('#answer-interview [name="speaker"]').fill("合成专家")
                    page.locator('#answer-interview [name="answer"]').fill("合成经历：看到接头松动后先检查连接。")
                    page.get_by_role("button", name="保存回答并继续", exact=True).click()
                    expect(page.locator("#question-text")).to_have_text("当时是什么观察让你先检查连接？")
                    assert calls[-1][1] == "synthetic-browser-a"
                    config_page = browser.new_page()
                    config_page.goto(base + "/settings", wait_until="networkidle")
                    config_page.get_by_label("模型名称", exact=True).fill("synthetic-browser-b")
                    config_page.get_by_role("button", name="保存并应用", exact=True).click()
                    expect(config_page.locator("#settings-message")).to_contain_text("配置已保存")
                    page.locator('#answer-interview [name="speaker"]').fill("合成专家")
                    page.locator('#answer-interview [name="answer"]').fill("合成回答：当时先观察接头是否松动。")
                    page.get_by_role("button", name="保存回答并继续", exact=True).click()
                    expect(page.locator("#question-text")).to_have_text("这次观察与上一次有什么不同？")
                    assert calls[-1][1] == "synthetic-browser-b"
                    config_page.get_by_label("模型名称", exact=True).fill("synthetic-auth-failure")
                    config_page.get_by_role("button", name="测试连接", exact=True).click()
                    expect(config_page.locator("#settings-message")).to_contain_text("身份验证失败")
                    expect(config_page.locator("#settings-message")).not_to_contain_text("synthetic-secret")
                    assert page.request.get(base + "/api/interview-config").json()["model"] == "synthetic-browser-b"
                    config_page.get_by_label("模型名称", exact=True).fill("synthetic-browser-b")
                    config_page.get_by_text("高级设置", exact=True).click()
                    config_page.locator('#connection-form [name="protocol"]').select_option("responses")
                    config_page.get_by_label("原生文件输入格式", exact=True).fill("text,pdf,png")
                    config_page.get_by_role("button", name="测试连接", exact=True).click()
                    expect(config_page.locator("#settings-message")).to_contain_text("连接成功")
                    assert calls[-1][0] == "/v1/responses"
                    config_page.get_by_role("button", name="保存并应用", exact=True).click()
                    expect(config_page.locator("#settings-message")).to_contain_text("配置已保存")
                    restarted = create_app(directory).test_client()
                    assert restarted.get("/api/interview-config").json["protocol"] == "responses"
                    assert restarted.get("/api/model-settings").json["has_api_key"]
                    assert not errors, errors
                    browser.close()
                    print("Passed: UI settings/test/save/reload/key privacy/live model switch/failed probe/Responses/restart. Synthetic only.")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
    finally:
        model_server.shutdown()
        model_thread.join(timeout=5)
        model_server.server_close()


if __name__ == "__main__":
    main()
