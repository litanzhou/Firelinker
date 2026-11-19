"""Browser + real SDK + local synthetic model server + durable LangGraph.

No real-model quality or expert approval is implied. All records are disposable.
"""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from experience_ledger.interviews.planner import OpenAIPlanner
from experience_ledger.web import create_app


def synthetic_action(context):
    turns = context["turns"]
    def question(key, text):
        return {"action": "ask_expert", "reason": "合成测试：提取具体判断", "question": text,
                "expected_information": "具体观察与当时的选择"}
    if not turns:
        return question("incident", "请回忆最近一次需要排查的情况，最开始看到了什么？")
    if len(turns) == 1:
        if not context.get("last_tool"):
            return {"action": "search_sources", "query": "合成", "reason": "合成测试：读取资料正文"}
        return question("reasoning", "当时是什么观察让你先检查连接？")
    return {"action": "draft_experience", "reason": "合成测试：保留原话与未知", "title": "[模拟联调] 先检查连接的判断", "kind": "case_note",
            "content": [{"key": f["key"], "text": turns[0]["answer"] if f["key"] == "statement" else "尚未说明，需在使用前补充核对。",
                         "status": "supported" if f["key"] == "statement" else "unknown"} for f in context["experience_fields"]],
            "evidence": [{"field": "statement", "source_type": "turn", "source_id": turns[0]["id"], "quote": turns[0]["answer"]}]}


def serve(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".local-artifacts/interviews"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_app = Flask("synthetic-model-only")
    requests = []
    first_call_started = Event()
    browser_reloaded = Event()

    @model_app.post("/v1/chat/completions")
    def completion():
        payload = request.get_json()
        context = json.loads(payload["messages"][1]["content"])
        requests.append(context)
        if len(requests) == 1:
            first_call_started.set()
            assert browser_reloaded.wait(20), "The browser did not reload during the first model call"
        action = synthetic_action(context)
        return jsonify(id="synthetic", object="chat.completion", created=0, model="local-synthetic-test-v1",
            choices=[{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "synthetic-call", "type": "function", "function": {"name": "propose_interview_action", "arguments": json.dumps(action, ensure_ascii=False)}}
            ]}}], usage={"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50})

    model_server, model_thread = serve(model_app)
    planner = OpenAIPlanner("local-synthetic-test-v1", f"http://127.0.0.1:{model_server.server_port}/v1", "synthetic-placeholder")
    try:
        with TemporaryDirectory(prefix="interview-browser-") as tmp:
            app = create_app(tmp, planner=planner)
            selected_case = app.extensions["ledger"].create_case("general", "[合成联调] 已有设备案例", "合成背景")
            server, thread = serve(app)
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    base = f"http://127.0.0.1:{server.server_port}"
                    page.goto(base + "/interviews")
                    page.wait_for_load_state("networkidle")
                    page.locator('#start-interview [name="goal"]').fill("[合成联调] 保留设备排查的判断经验")
                    page.locator("#start-case").select_option(selected_case["id"])
                    page.get_by_role("button", name="开始访谈", exact=True).click()
                    expect(page.locator("#session-goal")).to_have_text("[合成联调] 保留设备排查的判断经验")
                    expect(page.locator("#session-status")).to_have_text("等待回答")
                    expect(page.locator("#question-label")).to_have_text("先提供一段内容")
                    assert not requests, "An empty opening must not call the model"
                    for width, height in ((1366, 768), (1440, 900)):
                        page.set_viewport_size({"width": width, "height": height})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        box = page.get_by_role("button", name="保存回答并继续", exact=True).bounding_box()
                        assert box["y"] + box["height"] <= height
                        page.screenshot(path=str(args.output_dir / f"initial-text-{width}.png"))
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#question-label")).to_have_text("先提供一段内容")
                    assert not requests
                    sid = app.extensions["interviews"].store.list()[0]["id"]
                    session = app.extensions["interviews"].store.get(sid)
                    materials = browser.new_page()
                    materials.goto(base + f"/?case_id={session['case_id']}")
                    materials.wait_for_load_state("networkidle")
                    materials.get_by_text("添加资料", exact=True).click()
                    materials.locator('#file-form [name="file"]').set_input_files({"name": "synthetic.md", "mimeType": "text/markdown", "buffer": "合成资料：观察接头是否松动。".encode("utf-8")})
                    materials.get_by_role("button", name="保存文件原件").click()
                    expect(materials.locator("#materials-list .source-card")).to_have_count(1)
                    materials.close()
                    page.locator('#answer-interview [name="speaker"]').fill("测试模拟专家")
                    page.locator('#answer-interview [name="answer"]').fill("合成经历：我看到连接处松动，所以先检查连接。📝")
                    page.get_by_role("button", name="保存回答并继续").click()
                    assert first_call_started.wait(15), "The first model call did not start after saving original text"
                    assert requests[0]["turns"][0]["answer"] == "合成经历：我看到连接处松动，所以先检查连接。📝"
                    assert requests[0]["input_summary"]["turns"] == 1
                    page.reload()
                    browser_reloaded.set()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#question-text")).to_have_text("当时是什么观察让你先检查连接？")
                    expect(page.locator("#question-label")).to_have_text("当前问题")
                    expect(page.locator("#answer-label")).to_have_text("你的回答")
                    assert any(
                        result.get("source_type") == "file" and result.get("text") == "合成资料：观察接头是否松动。"
                        for call in requests for result in (call.get("last_tool") or {}).get("results", [])
                    )
                    previous_calls = len(requests)
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#question-text")).to_have_text("当时是什么观察让你先检查连接？")
                    page.get_by_role("button", name="暂停", exact=True).click()
                    expect(page.locator("#session-status")).to_have_text("已暂停")
                    page.get_by_role("button", name="继续访谈", exact=True).click()
                    expect(page.locator("#session-status")).to_have_text("等待回答")
                    assert len(requests) == previous_calls
                    page.locator('#answer-interview [name="speaker"]').fill("测试模拟专家")
                    page.locator('#answer-interview [name="answer"]').fill("合成补充：接头存在明显晃动。")
                    page.get_by_role("button", name="保存回答并继续").click()
                    expect(page.locator("#session-status")).to_have_text("待审核经验")
                    page.screenshot(path=str(args.output_dir / "conversation.png"), full_page=True)
                    page.get_by_role("link", name="查看候选经验与证据 →").click()
                    page.wait_for_load_state("networkidle")
                    page.locator("#edit-link").click()
                    expect(page.locator("#candidate-status")).to_have_text("待审核")
                    expect(page.locator("#dynamic-content .content-editor")).to_have_count(4)
                    expect(page.locator("#evidence-list .evidence-row")).to_have_count(1)
                    page.locator('#candidate-form [name="title"]').fill("[模拟联调] 经人工核对的合成表述")
                    page.locator('#candidate-form [name="actor"]').fill("测试模拟编辑")
                    page.get_by_role("button", name="保存为新修订").click()
                    expect(page.locator("#candidate-heading")).to_have_text("修订 2")
                    page.locator('#review-form [name="reviewer"]').fill("测试模拟审核人")
                    page.locator('#review-form [name="note"]').fill("仅为测试模拟确认，保留明确未知。")
                    page.get_by_role("button", name="确认并入库").click()
                    expect(page.locator("#candidate-status")).to_have_text("已确认 · 尚无案例验证")
                    page.get_by_role("link", name="经验库", exact=True).click()
                    expect(page.locator("#knowledge-list .knowledge-card")).to_have_count(1)
                    expect(page.locator("#knowledge-reader")).to_contain_text("未知")
                    page.screenshot(path=str(args.output_dir / "knowledge.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.goto(base + "/interviews")
                    page.wait_for_load_state("networkidle")
                    page.screenshot(path=str(args.output_dir / "mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "Mobile overflow"
                    assert not errors, errors
                    assert len(app.extensions["ledger"].search_knowledge()) == 1
                    browser.close()
                    print("Passed: goal → zero-call text intake/reload → saved original → SDK/search/follow-up → reload/pause/resume → draft → edit/review → search/mobile. Synthetic model only.")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
    finally:
        planner.close()
        model_server.shutdown()
        model_thread.join(timeout=5)
        model_server.server_close()


if __name__ == "__main__":
    main()
