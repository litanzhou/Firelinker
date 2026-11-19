"""Real browser acceptance of multi-case narration and knowledge lifecycle.

All records, model replies and human reviews are synthetic and disposable.
"""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from experience_ledger.demo import seed_demo
from experience_ledger.interviews.planner import Proposal
from experience_ledger.web import create_app


class SyntheticPlanner:
    configured = True
    model = "synthetic-lifecycle-browser"

    def propose(self, context, feedback=""):
        if context.get("requested_action") == "compare_experiences":
            left, right = context["left"], context["right"]
            evidence = []
            for side, item in (("left", left), ("right", right)):
                evidence.append({"side": side, **{k: item["evidence"][0][k] for k in ("source_type", "source_id", "start", "end", "quote")}})
            action = {"action":"propose_relation","reason":"合成模型：请人核对不同案例的适用条件。",
                "left_id":left["id"],"left_revision":left["revision"],"right_id":right["id"],"right_revision":right["revision"],
                "relation_kind":"conflict","conditions":"合成案例条件不同，尚待人工核对","relation_evidence":evidence}
        elif context.get("requested_action") == "draft":
            turn = context["turns"][0]
            action = {"action":"draft_experience","reason":"合成测试：只整理原话并保留未知","title":"浏览器新经验","kind":"case_note",
                "contexts":[{"case_id":c["id"],"decision_id":c["decisions"][0]["id"]} for c in context["cases"]],
                "content":[{"key":f["key"],"text":turn["answer"] if f["key"]=="statement" else "尚未说明，使用前需要核对。",
                            "status":"supported" if f["key"]=="statement" else "unknown"} for f in context["experience_fields"]],
                "evidence":[{"field":"statement","source_type":"turn","source_id":turn["id"],"quote":turn["answer"]}]}
        elif not context["turns"]:
            action = {"action":"ask_expert","question":"请先讲述一段最近实际发生的经历。","reason":"合成自由叙述入口","expected_information":"具体经历，暂不要求建立案例"}
        else:
            action = {"action":"pause","reason":"合成测试：原话已保留，可关联案例、补充或整理候选。"}
        return Proposal(action, self.model, 10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".local-artifacts/v03-lifecycle"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="lifecycle-browser-") as directory:
        app = create_app(directory, planner=SyntheticPlanner())
        ledger = app.extensions["ledger"]
        cases = seed_demo(ledger)
        for case in cases:
            ledger.review_candidate(case["candidates"][0]["id"], 1, "approve", "合成审核人", "合成验收")
        right = ledger.get_candidate(cases[1]["candidates"][0]["id"])
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width":1440,"height":1000})
                errors=[]
                page.on("pageerror",lambda error:errors.append(str(error)))
                base=f"http://127.0.0.1:{server.server_port}"
                page.goto(base + "/knowledge")
                results = page.locator("#knowledge-list button")
                expect(results).to_have_count(2)
                for size in ({"width": 1366, "height": 768}, {"width": 1440, "height": 900}):
                    page.set_viewport_size(size)
                    results.filter(has_text="线缆").click()
                    expect(page.locator("#knowledge-reader h2")).to_contain_text("线缆")
                    assert page.locator(".quotation").first.bounding_box()["y"] < size["height"]
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output_dir / f"library-{size['width']}.png"))
                results.filter(has_text="犹豫").click()
                page.reload()
                expect(page.locator("#knowledge-reader h2")).to_contain_text("犹豫")
                page.locator("#search-domain").select_option("electronics_repair")
                page.get_by_role("searchbox").fill("断连")
                page.get_by_role("button", name="查询", exact=True).click()
                expect(results).to_have_count(1)
                expect(results.locator("mark").first).to_have_text("断连")
                page.screenshot(path=str(args.output_dir / "library-search.png"))
                page.get_by_role("link", name="版本、关联与使用反馈").click()
                page.go_back()
                expect(page.get_by_role("searchbox")).to_have_value("断连")
                expect(page.locator("#search-domain")).to_have_value("electronics_repair")
                expect(page.locator("#knowledge-reader h2")).to_contain_text("线缆")
                page.get_by_role("searchbox").fill("无匹配词-xyz")
                page.get_by_role("button", name="查询", exact=True).click()
                expect(page.locator("#library-message")).to_contain_text("没有找到匹配的经验")
                page.get_by_role("link", name="查看全部经验").click()
                expect(results).to_have_count(2)
                page.goto(base+"/interviews")
                page.wait_for_load_state("networkidle")
                page.locator('#start-interview [name="goal"]').fill("[合成] 跨案例保留感知经验")
                page.get_by_role("button",name="开始访谈",exact=True).click()
                expect(page.locator("#session-status")).to_have_text("等待回答")
                page.get_by_role("button",name="新建访谈",exact=True).click()
                expect(page.locator("#start-interview")).to_be_visible()
                expect(page.locator('#start-interview [name="goal"]')).to_have_value("")
                page.locator("#session-list button").first.click()
                expect(page.locator("#current-question")).to_be_visible()
                session=app.extensions["interviews"].store.get(app.extensions["interviews"].store.list()[0]["id"])
                assert session["cases"] == [] and session["case_id"] is None
                raw="合成原话😀：当时凭手感作出判断，具体理由无法解释。"
                page.locator('#answer-interview [name="speaker"]').fill("合成专家")
                page.locator('#answer-interview [name="answer"]').fill(raw)
                page.get_by_role("button",name="保存回答并继续").click()
                expect(page.locator("#session-status")).to_have_text("已暂停")
                page.get_by_text("关联其他案例",exact=True).click()
                for case in cases:
                    page.locator("#link-case-select").select_option(case["id"])
                    page.get_by_role("button",name="关联到本次访谈",exact=True).click()
                    expect(page.locator("#focus-case option")).to_have_count(2+cases.index(case))
                page.get_by_text("选择当前案例与判断",exact=True).click()
                page.locator("#focus-case").select_option(cases[0]["id"])
                page.locator("#focus-decision").select_option(cases[0]["decisions"][0]["id"])
                page.get_by_role("button",name="切换到此关注点",exact=True).click()
                expect(page.locator("#focus-heading")).to_contain_text(cases[0]["title"])
                page.locator("#conversation .turn-association summary").first.click()
                association=page.locator("#conversation .turn-association form").first
                for case in cases:
                    association.get_by_label("关联案例",exact=True).select_option(case["id"])
                    association.get_by_label("关联判断",exact=True).select_option(case["decisions"][0]["id"])
                    association.get_by_label("关联人",exact=True).fill("合成记录人")
                    association.get_by_role("button",name="保存片段关联",exact=True).click()
                    expect(page.locator("#conversation .turn-association > p.hint")).to_have_count(cases.index(case)+1)
                    # Rendering replaces the association form, so rediscover it.
                    page.locator("#conversation .turn-association summary").first.click()
                    association=page.locator("#conversation .turn-association form").first
                session=app.extensions["interviews"].store.get(session["id"])
                assert len(session["turns"]) == 1
                assert len([c for c in session["turn_contexts"] if c["turn_id"] == session["turns"][0]["id"]]) == 2
                page.screenshot(path=str(args.output_dir/"multi-case.png"),full_page=True)
                page.get_by_role("button",name="整理候选经验",exact=True).click()
                expect(page.locator("#session-status")).to_have_text("待审核经验")
                page.get_by_role("link",name="查看候选经验与证据 →").click()
                page.wait_for_load_state("networkidle")
                page.locator("#edit-link").click()
                expect(page.locator("#candidate-status")).to_have_text("待审核")
                page.locator('#review-form [name="reviewer"]').fill("合成审核人")
                page.locator('#review-form [name="note"]').fill("合成核对：保留感知与未知")
                page.get_by_role("button",name="确认并入库",exact=True).click()
                expect(page.locator("#candidate-status")).to_contain_text("已确认")
                page.locator('#candidate-form [name="title"]').fill("尚未审核的新版标题")
                page.locator('#candidate-form [name="actor"]').fill("合成编辑")
                page.get_by_role("button",name="保存为新修订",exact=True).click()
                expect(page.locator("#candidate-heading")).to_have_text("修订 2")
                expect(page.locator("#candidate-version-note")).to_contain_text("经验库显示 v1")
                page.get_by_role("link",name="经验库",exact=True).click()
                page.locator('#search-form [name="q"]').fill("浏览器新经验")
                page.get_by_role("button",name="查询",exact=True).click()
                expect(page.locator("#knowledge-list .knowledge-card")).to_have_count(1)
                expect(page.locator("#knowledge-list")).not_to_contain_text("尚未审核的新版标题")
                expect(page.locator("#knowledge-reader")).to_contain_text("v2 待审核")
                page.get_by_role("link", name="版本、关联与使用反馈").click()
                expect(page.locator("#version-state")).to_contain_text("v1")
                expect(page.locator("#version-notice")).to_contain_text("v2")
                page.locator("#revision-select").select_option("2")
                expect(page.locator("#feedback-panel")).to_be_hidden()
                page.locator("#revision-select").select_option("1")
                page.locator("#feedback-panel summary").click()
                for key,value in {"actor":"合成使用者","original":"仅参考，尚未实际采用。","conditions":"合成条件","actions":"仅阅读","outcome":"尚不明确"}.items():
                    page.locator(f'#feedback-form [name="{key}"]').fill(value)
                page.locator('#feedback-form [name="actual_used"]').select_option("false")
                page.get_by_role("button",name="保存本版反馈",exact=True).click()
                expect(page.locator("#feedback-list .feedback-record")).to_have_count(1)
                page.locator("#compare-panel summary").click()
                page.get_by_role("button",name="查找相关经验",exact=True).click()
                page.locator("#related-results button").filter(has_text=right["title"]).click()
                page.locator("#left-evidence input").first.check()
                page.locator("#right-evidence input").first.check()
                page.locator('#relation-form [name="kind"]').select_option("conflict")
                for key,value in {"actor":"合成比较人","conditions":"目标不同，需要核对适用条件","reason":"保留双方原话，先核对背景"}.items():
                    page.locator(f'#relation-form [name="{key}"]').fill(value)
                page.get_by_role("button",name="保存关联建议",exact=True).click()
                expect(page.locator("#relation-list .relation-record")).to_have_count(1)
                review=page.locator("#relation-list form").first
                review.locator('input[name="reviewer"]').fill("合成核对人")
                review.locator('textarea[name="note"]').fill("目标不同，两条并存，保留出处")
                review.get_by_role("button",name="确认关联",exact=True).click()
                expect(page.locator("#relation-list")).to_contain_text("已确认关联")
                page.get_by_role("button",name="请当前模型比较",exact=True).click()
                expect(page.locator("#relation-list .relation-record")).to_have_count(2)
                page.screenshot(path=str(args.output_dir/"relations-feedback.png"),full_page=True)
                page.set_viewport_size({"width":390,"height":844})
                page.screenshot(path=str(args.output_dir/"mobile-experience.png"),full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
                page.goto(base+"/interviews")
                page.wait_for_load_state("networkidle")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
                assert not errors,errors
                browser.close()
                print("Passed: free narration → two cases/one original → human focus → draft/approval → old confirmed version during edit → version-bound feedback → manual/AI relation proposals/review → mobile. Synthetic only.")
        finally:
            server.shutdown();thread.join(timeout=5);server.server_close()


if __name__ == "__main__":
    main()
