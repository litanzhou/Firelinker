"""Exercise the real UI against disposable synthetic data; no running app needed."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from experience_ledger.web import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".local-artifacts"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="experience-browser-") as location:
        app = create_app(location)
        server = make_server("127.0.0.1", 0, app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}")
                page.wait_for_load_state("networkidle")
                page.locator("#case-domain").select_option("oral_history")
                page.locator('#case-form [name="title"]').fill("[浏览器合成测试] 保留叙述中的停顿")
                page.locator('#case-form [name="context"]').fill("完全合成的验收案例，审核由测试模拟。")
                page.get_by_role("button", name="创建案例", exact=True).click()
                expect(page.locator("#case-title")).to_have_text("[浏览器合成测试] 保留叙述中的停顿")

                raw = "合成原文：保留停顿，并在片段旁说明记忆的不确定性。📝\n仅用于已经获得授权的课堂材料。"
                page.locator("#source-file").set_input_files({"name": "synthetic.md", "mimeType": "text/markdown", "buffer": raw.encode("utf-8")})
                expect(page.locator("#source-text")).to_have_value(raw)
                page.get_by_role("button", name="保存资料版本").click()
                expect(page.locator("#source-list .source-card")).to_have_count(1)
                page.locator("#source-parent").select_option(index=1)
                page.locator('#source-form [name="title"]').fill("synthetic.md")
                page.locator("#source-text").fill(raw + "\n第二版：授权范围需要重新核对。")
                page.get_by_role("button", name="保存资料版本").click()
                expect(page.locator("#source-list .source-card")).to_have_count(2)

                page.get_by_role("tab", name="判断与访谈").click()
                page.get_by_text("添加一个判断", exact=True).click()
                page.locator('#decision-form [name="title"]').fill("如何保留叙述者的不确定性")
                page.locator('#decision-form [name="description"]').fill("合成片段存在停顿和自我修正。")
                page.get_by_role("button", name="添加判断与追问").click()
                expect(page.locator("#gap-list button")).to_have_count(5)
                answers = {
                    "做出的选择": "保留停顿，并在片段旁说明记忆的不确定性。📝",
                    "适用条件": "仅用于已经获得授权的课堂材料。",
                    "判断理由": "停顿之后的自我修正说明叙述者对时间没有把握。",
                    "例外与边界": "撤回授权或包含第三方信息时暂停使用。",
                    "替代方案": "删去停顿会改变这段话的语气，所以没有采用。",
                }
                for index, (label, answer) in enumerate(answers.items(), 1):
                    page.locator("#gap-list button").filter(has_text=label).click()
                    page.locator('#turn-form [name="speaker"]').fill("测试模拟发言人")
                    page.locator('#turn-form [name="answer"]').fill(answer)
                    page.get_by_role("button", name="保存这一轮").click()
                    expect(page.locator("#turn-list .record-card")).to_have_count(index)
                page.get_by_role("button", name="整理候选经验").click()
                expect(page.locator("#candidate-status")).to_have_text("待审核")
                expect(page.locator("#evidence-list .evidence-row")).to_have_count(4)

                page.get_by_text("补充资料或访谈引文", exact=True).click()
                page.locator("#evidence-source").select_option(index=0)
                page.locator("#evidence-quote").fill(answers["做出的选择"])
                page.get_by_role("button", name="添加到本次修订").click()
                page.locator('#candidate-form [name="title"]').fill("[浏览器合成测试] 保留停顿及其条件")
                page.locator('#candidate-form [name="actor"]').fill("测试模拟编辑")
                expect(page.locator("#approve")).to_be_disabled()
                page.get_by_role("button", name="保存为新修订").click()
                expect(page.locator("#candidate-heading")).to_have_text("修订 2")
                expect(page.locator("#evidence-list .evidence-row")).to_have_count(5)
                page.locator('#review-form [name="reviewer"]').fill("测试模拟审核人")
                page.locator('#review-form [name="note"]').fill("自动化测试模拟确认：已核对合成原文和适用条件。")
                page.get_by_role("button", name="确认并入库").click()
                expect(page.locator("#candidate-status")).to_have_text("已确认 · 尚无案例验证")
                page.screenshot(path=str(args.output_dir / "review.png"), full_page=True)

                page.get_by_role("link", name="经验库", exact=True).click()
                expect(page.locator("#knowledge-list .knowledge-card")).to_have_count(1)
                page.locator('#search-form [name="q"]').fill("保留停顿")
                page.get_by_role("button", name="查询", exact=True).click()
                expect(page.locator("#knowledge-list .knowledge-card")).to_have_count(1)
                page.screenshot(path=str(args.output_dir / "knowledge.png"), full_page=True)
                page.get_by_role("link", name="查看原始案例").click()
                expect(page.locator("#candidate-heading")).to_have_text("修订 2")
                page.reload()
                page.wait_for_load_state("networkidle")
                page.get_by_role("tab", name="经验审核").click()
                page.locator("#candidate-list button").first.click()
                expect(page.locator("#candidate-status")).to_have_text("已确认 · 尚无案例验证")
                expect(page.locator("#evidence-list .evidence-row")).to_have_count(5)

                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=str(args.output_dir / "mobile.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "Mobile layout overflows"
                assert not errors, errors
                cases = app.extensions["ledger"].list_cases()
                case = app.extensions["ledger"].get_case(cases[0]["id"])
                assert len(case["sources"]) == 2 and len(case["turns"]) == 5
                candidate = case["candidates"][0]
                assert candidate["revision"] == 2 and candidate["status"] == "approved"
                assert candidate["validation_status"] == "unvalidated"
                assert any(e["source_type"] == "document" for e in candidate["evidence"])
                browser.close()
                print("Browser smoke passed: import/version/interview/revise/cite/review/search/reload/mobile; synthetic data only.")
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    main()
