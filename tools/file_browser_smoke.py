"""Headless file upload and human citation checks using disposable synthetic data.

No live model, real expert identity, user data directory or fixed port is used.
"""

import argparse
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from werkzeug.serving import make_server

from experience_ledger.interviews.planner import UnconfiguredPlanner
from experience_ledger.web import create_app


NATIVE_QUOTE = "Synthetic native-only quote for manual confirmation."


def make_pdf(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=240)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 10 Tf 20 160 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    target = BytesIO()
    writer.write(target)
    return target.getvalue()


def upload_payloads():
    word = Document()
    word.add_paragraph("Synthetic DOCX record: 合成段落，仅用于浏览器测试。")
    word.add_table(rows=1, cols=1).cell(0, 0).text = "合成表格单元格"
    word_bytes = BytesIO()
    word.save(word_bytes)
    book = Workbook()
    book.active.title = "Synthetic"
    book.active["A1"], book.active["B1"], book.active["B2"] = "合成工况", 80, "=B1+1"
    spreadsheet_bytes = BytesIO()
    book.save(spreadsheet_bytes)
    book.close()
    picture = Image.new("RGB", (160, 90), "#f9f5e8")
    ImageDraw.Draw(picture).rectangle((20, 20, 100, 60), fill="#9a6326")
    image_bytes = BytesIO()
    picture.save(image_bytes, format="PNG")
    return [
        ("synthetic-upload.pdf", "application/pdf", make_pdf("Synthetic upload PDF record.")),
        ("synthetic-upload.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", word_bytes.getvalue()),
        ("synthetic-upload.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", spreadsheet_bytes.getvalue()),
        ("synthetic-upload.png", "image/png", image_bytes.getvalue()),
    ]


def seed_native_candidate(ledger):
    case = ledger.create_case("general", "[合成文件验收] 原件与人工引用核对", "完全合成；所有发言、编辑、核对和审核均为自动化测试模拟。")
    decision = ledger.add_decision(case["id"], "[合成判断] 保留原件中的表述", "用于验证未解析文件引用的审核过程。")
    for gap in ledger.get_case(case["id"])["gaps"]:
        answer = NATIVE_QUOTE if gap["kind"] == "action" else "合成原话，仅供测试：" + gap["kind"]
        ledger.add_turn(case["id"], gap["id"], answer, "测试模拟专家")
    candidate = ledger.draft_candidate(case["id"], decision["id"])
    original = make_pdf(NATIVE_QUOTE)
    source = ledger.add_file(case["id"], "synthetic-native-reference.pdf", original, parse=False)
    replacement_bytes = make_pdf("Synthetic replacement: old citations stay with version one.")
    replacement = ledger.add_file(case["id"], "synthetic-native-reference.pdf", replacement_bytes,
                                  file_id=source["file_id"], parse=False)
    values = {key: candidate[key] for key in ("title", "kind", "statement", "scope", "rationale", "exceptions")}
    evidence = deepcopy(candidate["evidence"])
    evidence.append({"field": "statement", "source_type": "file", "source_id": source["id"],
                     "locator": {"kind": "pdf", "page": 1}, "quote": NATIVE_QUOTE})
    candidate = ledger.revise_candidate(candidate["id"], expected_revision=1, actor="测试模拟初始编辑", evidence=evidence, **values)
    assert candidate["revision"] == 2 and candidate["status"] == "pending"
    assert candidate["evidence"][-1]["locator"]["verification"] == "pending_manual"
    return case, candidate, source, original, replacement, replacement_bytes


def assert_no_overflow(page, label):
    widths = page.evaluate("({content:document.documentElement.scrollWidth,viewport:innerWidth})")
    assert widths["content"] <= widths["viewport"] + 1, f"{label} layout overflow: {widths}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(".local-artifacts/v03-files"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="experience-file-browser-") as location:
        # Explicitly avoid reading deployment/model configuration during this test.
        app = create_app(location, planner=UnconfiguredPlanner())
        ledger = app.extensions["ledger"]
        case, candidate, source, original, replacement, replacement_bytes = seed_native_candidate(ledger)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    base = f"http://127.0.0.1:{server.server_port}"
                    page.goto(base + f"/?case_id={case['id']}")
                    page.wait_for_load_state("networkidle")
                    # Record the rendered DOM before choosing the observed form controls.
                    observed = page.locator("form").evaluate_all("forms=>forms.map(f=>({id:f.id,fields:[...f.elements].map(e=>({tag:e.tagName,name:e.name,id:e.id}))}))")
                    (args.output_dir / "observed-forms.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2), encoding="utf-8")
                    assert any(f["id"] == "file-form" for f in observed)
                    expect(page.locator("#case-title")).to_have_text(case["title"])
                    materials = page.locator("#materials-list > .source-card")
                    file_materials = page.locator('#materials-list > [data-material-type="file"]')
                    expect(file_materials).to_have_count(2)
                    page.get_by_text("添加资料", exact=True).click()
                    page.get_by_role("button", name="记录文字", exact=True).click()
                    page.locator('#source-form [name="title"]').fill("合成测试交接说明")
                    page.locator("#source-text").fill("合成测试：原件与文字保存在同一个案例中。")
                    page.get_by_role("button", name="保存资料版本", exact=True).click()
                    expect(materials).to_have_count(3)
                    expect(materials.filter(has_text="合成测试交接说明")).to_have_attribute("data-material-type", "text")
                    page.locator("#source-parent").select_option(index=1)
                    chosen_source = page.locator("#source-parent").input_value()
                    page.get_by_role("button", name="上传文件", exact=True).click()
                    uploaded = []
                    for index, (filename, mime_type, data) in enumerate(upload_payloads(), 3):
                        page.locator('#file-form [name="file"]').set_input_files({"name": filename, "mimeType": mime_type, "buffer": data})
                        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/api/cases/{case['id']}/files")) as pending_upload:
                            page.get_by_role("button", name="保存文件原件", exact=True).click()
                        response = pending_upload.value
                        assert response.status == 201, response.text()
                        metadata = response.json()
                        assert metadata["parse_status"] == "ready", metadata
                        assert metadata["size"] == len(data)
                        uploaded.append(metadata)
                        expect(file_materials).to_have_count(index)
                        expect(materials).to_have_count(index + 1)
                        card = file_materials.filter(has_text=filename)
                        card.locator("summary").click()
                        expect(card).to_contain_text("本地解析：可读取")
                        with page.expect_download() as pending_download:
                            card.get_by_role("link", name="下载原件 →", exact=True).click()
                        download = pending_download.value
                        assert download.suggested_filename == filename
                        assert Path(download.path()).read_bytes() == data
                        assert ledger.file_bytes(metadata["id"])[1] == data
                        card.get_by_role("button", name="查看提取片段", exact=True).click()
                        expect(card.locator("pre")).to_be_visible()
                    page.get_by_role("button", name="记录文字", exact=True).click()
                    expect(page.locator("#source-parent")).to_have_value(chosen_source)
                    page.get_by_text("添加资料", exact=True).click()
                    assert_no_overflow(page, "desktop files")
                    page.screenshot(path=str(args.output_dir / "files-desktop.png"), full_page=True)

                    page.goto(base + f"/?case_id={case['id']}&candidate_id={candidate['id']}")
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#candidate-heading")).to_have_text("修订 2")
                    expect(page.locator("#candidate-status")).to_have_text("待审核")
                    native_row = page.locator("#evidence-list .evidence-row").filter(has_text="synthetic-native-reference.pdf")
                    expect(native_row).to_contain_text("引用位置或文字尚待人工对照原件核对")
                    native_row.get_by_text("对照原件确认位置与引文", exact=True).click()
                    manual_dom = native_row.locator("details").last.evaluate("e=>e.outerHTML")
                    (args.output_dir / "observed-manual-check.html").write_text(manual_dom, encoding="utf-8")
                    with page.expect_download() as native_download:
                        native_row.get_by_role("link", name="下载原件核对 →", exact=True).click()
                    assert Path(native_download.value.path()).read_bytes() == original
                    native_row.get_by_label("页码", exact=True).fill("1")
                    native_row.get_by_label("核对人", exact=True).fill("测试模拟原件核对人")
                    native_row.get_by_label("核对说明", exact=True).fill("自动化测试模拟核对：第一页包含合成英文引文；原件为固定版本1。")
                    page.screenshot(path=str(args.output_dir / "review-pending-desktop.png"), full_page=True)
                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/api/files/{source['id']}/reference-checks")) as pending_check:
                        native_row.get_by_role("button", name="保存原件核对记录", exact=True).click()
                    assert pending_check.value.status == 201, pending_check.value.text()
                    checked = pending_check.value.json()
                    expect(native_row).to_contain_text("已人工对照原件确认位置与引文")
                    expect(page.locator("#approve")).to_be_disabled()
                    page.locator('#candidate-form [name="actor"]').fill("测试模拟核对后编辑")
                    page.get_by_role("button", name="保存为新修订", exact=True).click()
                    expect(page.locator("#candidate-heading")).to_have_text("修订 3")
                    expect(page.locator("#candidate-status")).to_have_text("待审核")
                    page.locator('#review-form [name="reviewer"]').fill("测试模拟经验审核人")
                    page.locator('#review-form [name="note"]').fill("自动化测试模拟审核：已核对合成原件引用、专家原话与新修订；未进行经验可靠性验证。")
                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/api/candidates/{candidate['id']}/reviews")) as pending_review:
                        page.get_by_role("button", name="确认并入库", exact=True).click()
                    assert pending_review.value.status == 201, pending_review.value.text()
                    expect(page.locator("#candidate-status")).to_have_text("已确认 · 尚无案例验证")
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#candidate-heading")).to_have_text("修订 3")
                    expect(page.locator("#candidate-status")).to_have_text("已确认 · 尚无案例验证")
                    assert_no_overflow(page, "desktop review")
                    page.screenshot(path=str(args.output_dir / "review-confirmed-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert_no_overflow(page, "390px review")
                    page.screenshot(path=str(args.output_dir / "review-confirmed-mobile.png"), full_page=True)
                    assert not errors, errors

                    saved = ledger.get_candidate(candidate["id"])
                    old = ledger.get_candidate(candidate["id"], revision=2)
                    reference = next(e for e in saved["evidence"] if e["source_type"] == "file")
                    assert saved["revision"] == 3 and saved["status"] == "approved"
                    assert saved["validation_status"] == "unvalidated"
                    assert reference["source_id"] == source["id"]
                    assert reference["locator"]["manual_check_id"] == checked["locator"]["manual_check_id"]
                    assert old["evidence"][-1]["locator"]["verification"] == "pending_manual"
                    metadata = ledger.get_file(source["id"])
                    assert metadata["version"] == 1 and metadata["parse_status"] == "not_requested"
                    assert len(metadata["reference_checks"]) == 1
                    assert metadata["reference_checks"][0]["actor"] == "测试模拟原件核对人"
                    assert metadata["reference_checks"][0]["file_version_id"] == source["id"]
                    assert ledger.file_bytes(source["id"])[1] == original
                    assert ledger.file_bytes(replacement["id"])[1] == replacement_bytes
                    assert [f["version"] for f in ledger.list_files(case["id"]) if f["file_id"] == source["file_id"]] == [1, 2]
                    report = {"result": "passed", "synthetic_only": True, "uploaded_formats": [f["filename"] for f in uploaded],
                              "downloads_byte_exact": True, "native_file_parse_status": metadata["parse_status"],
                              "approved_revision": 3, "old_revision_preserved": True, "file_versions_preserved": [1, 2],
                              "manual_checks": 1, "viewport_widths": [1440, 390], "page_errors": errors}
                    (args.output_dir / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    print("File browser smoke passed: PDF/DOCX/XLSX/PNG upload + exact downloads; native citation pending -> human check -> new revision -> simulated approval; original versions intact; desktop/390px without overflow or page errors.")
                except Exception:
                    page.screenshot(path=str(args.output_dir / "failure.png"), full_page=True)
                    (args.output_dir / "failure.html").write_text(page.content(), encoding="utf-8")
                    raise
                finally:
                    context.close()
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    main()
