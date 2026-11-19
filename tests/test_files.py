"""Original bytes, stable local anchors and honest extraction failures."""

from io import BytesIO
import json

import pytest

from experience_ledger import files
from experience_ledger.service import Ledger, LedgerError


@pytest.fixture
def ledger_case(tmp_path):
    ledger = Ledger(tmp_path)
    case = ledger.create_case("general", "合成文件案例", "仅测试")
    return ledger, case["id"]


def validate(ledger, case_id, file, locator, quote="", **kwargs):
    with ledger._connection() as db:
        return ledger._validate_file_reference(db, {"field": "statement", "source_type": "file", "source_id": file["id"],
                                                    "locator": locator, "quote": quote, **kwargs}, {case_id})


def test_original_bytes_versions_and_pinned_parses(ledger_case, monkeypatch):
    ledger, case_id = ledger_case
    original = b"\xef\xbb\xbf" + "合成原文\n第二行".encode()
    first = ledger.add_file(case_id, "../original.txt", original)
    assert first["filename"] == "original.txt"
    assert ledger.file_bytes(first["id"])[1] == original
    assert first["anchors"][0]["text"] == "合成原文\n第二行"
    reference = validate(ledger, case_id, first, {"kind": "text"}, "合成原文")
    assert reference["locator"]["parse_id"] == first["parse_id"]
    monkeypatch.setattr(files, "_extract", lambda filename, data: ("synthetic-parser:v2", [{"locator": {"kind": "text"}, "text": "全新解析结果"}], []))
    reparsed = ledger.parse_file(first["id"])
    assert reparsed["parse_id"] != first["parse_id"]
    with ledger._connection() as db:
        assert ledger._file_reference_text(db, reference) == "合成原文\n第二行"
    assert validate(ledger, case_id, first, reference["locator"], "合成原文") == reference
    second = ledger.add_file(case_id, "original.txt", b"version two", file_id=first["file_id"])
    assert second["version"] == 2 and second["file_id"] == first["file_id"]
    assert ledger.file_bytes(first["id"])[1] == original
    assert ledger.add_file(case_id, "renamed.txt", b"version two", file_id=first["file_id"])["id"] == second["id"]
    assert len(ledger.list_files(case_id)) == 2
    assert "anchors" not in ledger.list_files(case_id)[0]
    assert "path" not in json.dumps(first)


def test_tampered_original_and_other_case_reference_are_rejected(ledger_case):
    ledger, case_id = ledger_case
    file = ledger.add_file(case_id, "example.md", b"synthetic")
    other = ledger.create_case("general", "另一个案例", "")
    with pytest.raises(LedgerError, match="关联的案例"):
        validate(ledger, other["id"], file, {"kind": "text"}, "synthetic")
    (ledger.blobs / (file["sha256"] + ".bin")).write_bytes(b"tampered")
    with pytest.raises(LedgerError, match="完整性"):
        ledger.file_attachment(file["id"])


def test_failed_optional_and_legacy_parsing_preserve_download(ledger_case, monkeypatch):
    ledger, case_id = ledger_case
    legacy = ledger.add_file(case_id, "old.doc", b"synthetic legacy original")
    assert legacy["parse_status"] == "unsupported" and "不支持" in legacy["parse_error"]
    broken = ledger.add_file(case_id, "broken.txt", b"\xff\xfe")
    assert broken["parse_status"] == "failed"
    assert ledger.file_bytes(broken["id"])[1] == b"\xff\xfe"
    def missing_parser(filename, data):
        raise ImportError("synthetic missing parser")
    monkeypatch.setattr(files, "_extract", missing_parser)
    missing = ledger.add_file(case_id, "optional.pdf", b"synthetic")
    assert missing["parse_status"] == "unavailable" and "[files]" in missing["parse_error"]
    assert ledger.read_file(missing["id"], native=True)["attachment"]["source_id"] == missing["id"]
    assert "data" not in ledger.read_file(missing["id"], native=True)["attachment"]
    with pytest.raises(LedgerError, match="解析依赖"):
        ledger.read_file(missing["id"])


def test_docx_paragraph_and_table_quotes(ledger_case):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("合成第一段")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = "合成表格"
    buffer = BytesIO()
    document.save(buffer)
    ledger, case_id = ledger_case
    file = ledger.add_file(case_id, "sample.docx", buffer.getvalue())
    assert file["parse_status"] == "ready"
    assert validate(ledger, case_id, file, {"kind": "docx", "paragraph": 0}, "第一段")["start"] == 2
    assert validate(ledger, case_id, file, {"kind": "docx", "table": 0, "row": 0, "column": 0}, "表格")["quote"] == "表格"
    with pytest.raises(LedgerError, match="位置不存在"):
        validate(ledger, case_id, file, {"kind": "docx", "paragraph": 3}, "合成")


def test_spreadsheet_cells_formulas_and_ranges(ledger_case):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active.title = "合成表"
    book.active["A1"] = "工况"
    book.active["B1"] = 80
    book.active["B2"] = "=B1+1"
    buffer = BytesIO()
    book.save(buffer)
    ledger, case_id = ledger_case
    file = ledger.add_file(case_id, "sample.xlsx", buffer.getvalue())
    assert file["parse_status"] == "ready"
    reference = validate(ledger, case_id, file, {"kind": "xlsx", "sheet": "合成表", "range": "A1:B2"}, "工况\n80\n=B1+1")
    assert reference["quote"].endswith("=B1+1")
    assert any(a.get("formula") for a in file["anchors"])
    with pytest.raises(LedgerError):
        validate(ledger, case_id, file, {"kind": "xlsx", "sheet": "其他表", "range": "A1"}, "工况")
    with pytest.raises(LedgerError):
        validate(ledger, case_id, file, {"kind": "xlsx", "sheet": "合成表", "range": "B2"}, "81")


@pytest.mark.parametrize("extension,format", [("png", "PNG"), ("jpg", "JPEG"), ("webp", "WEBP")])
def test_visual_region_is_locatable_without_fabricated_quote(ledger_case, extension, format):
    image = pytest.importorskip("PIL.Image")
    buffer = BytesIO()
    image.new("RGB", (100, 60), "white").save(buffer, format=format)
    ledger, case_id = ledger_case
    file = ledger.add_file(case_id, "sample." + extension, buffer.getvalue())
    assert file["parse_status"] == "ready"
    reference = validate(ledger, case_id, file, {"kind": "image", "bbox": [10, 10, 20, 20]})
    assert reference["start"] is None and reference["end"] is None and reference["quote"] == ""
    for bbox in ([90, 10, 20, 20], [-1, 0, 1, 1], [0, 0, float("nan"), 1], [True, 0, 1, 1]):
        with pytest.raises(LedgerError):
            validate(ledger, case_id, file, {"kind": "image", "bbox": bbox})


def test_pdf_page_region_and_invalid_page(ledger_case):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=300)
    buffer = BytesIO()
    writer.write(buffer)
    ledger, case_id = ledger_case
    file = ledger.add_file(case_id, "blank.pdf", buffer.getvalue())
    assert file["parse_status"] == "ready" and file["anchors"][0]["locator"]["page"] == 1
    validate(ledger, case_id, file, {"kind": "pdf", "page": 1, "bbox": [0, 0, 200, 300]})
    for page in (0, 2, True, "1"):
        with pytest.raises(LedgerError):
            validate(ledger, case_id, file, {"kind": "pdf", "page": page, "bbox": [0, 0, 1, 1]})


def test_text_offsets_and_upload_limits(ledger_case, monkeypatch):
    ledger, case_id = ledger_case
    file = ledger.add_file(case_id, "sample.txt", b"one two")
    for quote, offsets in (("missing", {}), ("one two", {"start": 0, "end": 999}), ("one", {"start": True, "end": 4})):
        with pytest.raises(LedgerError):
            validate(ledger, case_id, file, {"kind": "text"}, quote, **offsets)
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 4)
    with pytest.raises(LedgerError, match="25 MiB"):
        ledger.add_file(case_id, "large.txt", b"12345")
    with pytest.raises(LedgerError, match="支持"):
        ledger.add_file(case_id, "program.exe", b"data")


def test_native_only_quotes_need_a_bound_human_check(ledger_case):
    ledger, case_id = ledger_case
    file = ledger.add_file(case_id, "native.pdf", b"%PDF-synthetic-original", parse=False)
    pending = validate(ledger, case_id, file, {"kind": "pdf", "page": 2, "verification": "human_checked"}, "模型提出的引文")
    assert pending["locator"]["verification"] == "pending_manual"
    with ledger._connection() as db:
        assert ledger._file_reference_text(db, pending) == ""
    with pytest.raises(LedgerError, match="不匹配"):
        validate(ledger, case_id, file, {**pending["locator"], "manual_check_id": "forged"}, pending["quote"])
    checked = ledger.confirm_file_reference(file["id"], pending["locator"], pending["quote"], "合成审核人", "测试中模拟对照原件")
    assert checked["locator"]["verification"] == "human_checked"
    assert validate(ledger, case_id, file, checked["locator"], checked["quote"])["locator"] == checked["locator"]
    for locator, quote in (({**checked["locator"], "page": 3}, checked["quote"]), (checked["locator"], "另一句引文")):
        with pytest.raises(LedgerError, match="不匹配"):
            validate(ledger, case_id, file, locator, quote)
    second = ledger.add_file(case_id, "native.pdf", b"%PDF-new-version", file_id=file["file_id"], parse=False)
    with pytest.raises(LedgerError, match="不匹配"):
        validate(ledger, case_id, second, checked["locator"], checked["quote"])


def test_scanned_pdf_quote_remains_pending_even_when_page_is_locatable(ledger_case):
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=300)
    buffer = BytesIO()
    writer.write(buffer)
    ledger, case_id = ledger_case
    file = ledger.add_file(case_id, "scan.pdf", buffer.getvalue())
    pending = validate(ledger, case_id, file, {"kind": "pdf", "page": 1}, "视觉模型提出的文字")
    assert pending["locator"]["verification"] == "pending_manual"
    checked = ledger.confirm_file_reference(file["id"], pending["locator"], pending["quote"], "模拟核对人", "确认图像中的文字")
    assert validate(ledger, case_id, file, checked["locator"], checked["quote"])["locator"]["verification"] == "human_checked"


def test_file_search_uses_authorized_cases_and_current_file_version(ledger_case):
    ledger, case_id = ledger_case
    old = ledger.add_file(case_id, "sample.txt", b"old-only-keyword")
    current = ledger.add_file(case_id, "sample.txt", b"current-keyword", file_id=old["file_id"])
    other_case = ledger.create_case("general", "外部案例", "")
    ledger.add_file(other_case["id"], "other.txt", b"private-keyword")
    with ledger._connection() as db:
        assert not ledger._file_search(db, {case_id}, ["old-only-keyword"])
        assert not ledger._file_search(db, {case_id}, ["private-keyword"])
        matches = ledger._file_search(db, {case_id}, ["current-keyword"])
    assert len(matches) == 1
    result = matches[0][1]
    assert result["source_id"] == current["id"] and result["locator"]["parse_id"] == current["parse_id"]
