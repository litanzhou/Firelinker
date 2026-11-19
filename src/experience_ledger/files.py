"""Immutable originals and optional, versioned local document representations.

Parsers are replaceable and imported only when requested. Model attachments are
resolved from trusted stored IDs, never model-supplied paths or remote URLs.
"""

from hashlib import sha256
from importlib.metadata import version as package_version
from io import BytesIO
import json
import math
from pathlib import Path
from posixpath import normpath
import re
from urllib.parse import unquote
from xml.etree import ElementTree
from zipfile import ZipFile, BadZipFile


MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_PARSE_CHARS = 2_000_000
MAX_ANCHORS = 20_000
MAX_PPTX_XML_BYTES = 8 * 1024 * 1024
MIME_TYPES = {
    ".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword", ".xls": "application/vnd.ms-excel",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}


def _error(message, status=400):
    from .service import LedgerError
    return LedgerError(message, status)


def _safe_name(filename):
    if not isinstance(filename, str):
        raise _error("文件名必须是字符串")
    filename = re.split(r"[/\\]", filename)[-1]
    if not filename or len(filename) > 255 or any(ord(c) < 32 for c in filename):
        raise _error("文件名无效")
    if Path(filename).suffix.lower() not in MIME_TYPES:
        raise _error("支持 TXT、Markdown、PDF、DOCX、XLSX、PPTX、PNG、JPEG、WEBP；旧 DOC、XLS、PPT 可保存原件")
    return filename


def _check_archive(data, required):
    with ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > 100 * 1024 * 1024:
            raise _error("压缩文档展开后超过本地解析限制")
        if required not in archive.namelist():
            raise _error("文件内容与扩展名不符")


class _PresentationXmlTree(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise _error("PPTX 的 XML 不允许包含文档类型声明")


def _presentation_xml(archive, name):
    try:
        part = archive.getinfo(name)
    except KeyError:
        raise _error("PPTX 缺少演示文稿或幻灯片部件") from None
    if part.file_size > MAX_PPTX_XML_BYTES:
        raise _error("PPTX 的 XML 部件超过本地解析限制")
    try:
        return ElementTree.fromstring(archive.read(part), parser=ElementTree.XMLParser(target=_PresentationXmlTree()))
    except ElementTree.ParseError:
        raise _error("PPTX 的演示文稿或幻灯片 XML 无法解析") from None


def _pptx_anchors(data):
    # Slides follow the presentation's relationship order, not ZIP entry names.
    part_name = "ppt/presentation.xml"
    _check_archive(data, part_name)
    with ZipFile(BytesIO(data)) as archive:
        if len(set(archive.namelist())) != len(archive.infolist()):
            raise _error("PPTX 存在重名的文档部件")
        presentation = _presentation_xml(archive, part_name)
        namespaces = (
            ("http://schemas.openxmlformats.org/presentationml/2006/main",
             "http://schemas.openxmlformats.org/drawingml/2006/main",
             "http://schemas.openxmlformats.org/officeDocument/2006/relationships"),
            ("http://purl.oclc.org/ooxml/presentationml/main",
             "http://purl.oclc.org/ooxml/drawingml/main",
             "http://purl.oclc.org/ooxml/officeDocument/relationships"),
        )
        for presentation_ns, drawing_ns, relationship_ns in namespaces:
            p, a, r = ("{" + ns + "}" for ns in (presentation_ns, drawing_ns, relationship_ns))
            if presentation.tag == p + "presentation":
                break
        else:
            raise _error("PPTX 的演示文稿格式无效")

        slide_ids = presentation.findall(f"{p}sldIdLst/{p}sldId")
        if not slide_ids:
            raise _error("PPTX 没有可读取的幻灯片")
        if len(slide_ids) > MAX_ANCHORS:
            raise _error("PPTX 的幻灯片数超过本地解析限制")
        relationships = _presentation_xml(archive, "ppt/_rels/presentation.xml.rels")
        slide_type = relationship_ns + "/slide"
        targets = {}
        for relation in relationships:
            if relation.get("Type") != slide_type or relation.get("TargetMode", "Internal") != "Internal":
                continue
            relation_id = relation.get("Id")
            if not relation_id or relation_id in targets:
                raise _error("PPTX 的幻灯片关系无效")
            targets[relation_id] = relation.get("Target", "")
        anchors, visited = [], set()
        chars = 0
        for slide_number, slide_id in enumerate(slide_ids, 1):
            target = unquote(targets.get(slide_id.get(r + "id"), ""))
            path = normpath(target.lstrip("/") if target.startswith("/") else "ppt/" + target)
            if not path.startswith("ppt/") or not path.endswith(".xml") or path in visited:
                raise _error("PPTX 的幻灯片关系无效")
            visited.add(path)
            slide = _presentation_xml(archive, path)
            if slide.tag != p + "sld":
                raise _error("PPTX 的幻灯片格式无效")
            paragraphs = ["".join((element.text or "") if element.tag == a + "t" else "\n"
                                  for element in paragraph.iter() if element.tag in (a + "t", a + "br"))
                          for paragraph in slide.iter(a + "p")]
            text = "\n".join(line for line in paragraphs if line)
            chars += len(text)
            if chars > MAX_PARSE_CHARS:
                raise _error("PPTX 提取内容超过本地解析限制，原件仍保留")
            anchors.append({"locator": {"kind": "slide", "slide": slide_number}, "text": text})
    return anchors


def _extract(filename, data):
    """Return (parser identity, stable anchors, explicit extraction limitations)."""
    extension = Path(filename).suffix.lower()
    anchors, warnings = [], []
    if extension in (".doc", ".xls", ".ppt"):
        legacy = extension[1:].upper()
        return "unsupported", [], [f"已保存旧版 {legacy} 原件。本地解析不支持该格式，可以另存为 {legacy}X 或配置支持此格式的模型。"]
    if extension in (".txt", ".md"):
        anchors = [{"locator": {"kind": "text"}, "text": data.decode("utf-8-sig")}]
        parser = "utf8:v1"
    elif extension == ".pdf":
        from pypdf import PdfReader
        parser = "pypdf:" + package_version("pypdf")
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise _error("加密 PDF 不能本地解析，请先解密后重新上传")
        if len(reader.pages) > MAX_ANCHORS:
            raise _error("PDF 页数超过本地解析限制")
        for page_number, page in enumerate(reader.pages, 1):
            anchors.append({"locator": {"kind": "pdf", "page": page_number},
                            "text": page.extract_text() or "", "width": float(page.mediabox.width),
                            "height": float(page.mediabox.height), "coordinate_system": "pdf-mediabox-bottom-left-points"})
        warnings.append("本地 PDF 提取不含 OCR，图片和排版含义需对照原件核对。")
    elif extension == ".docx":
        _check_archive(data, "word/document.xml")
        from docx import Document
        parser = "python-docx:" + package_version("python-docx")
        document = Document(BytesIO(data))
        for index, paragraph in enumerate(document.paragraphs):
            anchors.append({"locator": {"kind": "docx", "paragraph": index}, "text": paragraph.text})
        for table_index, table in enumerate(document.tables):
            for row_index, row in enumerate(table.rows):
                for column_index, cell in enumerate(row.cells):
                    anchors.append({"locator": {"kind": "docx", "table": table_index, "row": row_index,
                                                "column": column_index}, "text": cell.text})
        warnings.append("DOCX 以固定原件的段落和表格单元格定位；本地提取不含嵌入图片、页眉页脚或分页。")
    elif extension == ".xlsx":
        _check_archive(data, "xl/workbook.xml")
        from openpyxl import load_workbook
        parser = "openpyxl:" + package_version("openpyxl")
        book = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in book.worksheets:
                if (sheet.max_row or 0) * (sheet.max_column or 0) > 1_000_000:
                    raise _error("工作表使用区域超过本地解析限制")
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.value is not None:
                            anchors.append({"locator": {"kind": "xlsx", "sheet": sheet.title, "range": cell.coordinate},
                                            "text": str(cell.value), "formula": cell.data_type == "f"})
                            if len(anchors) > MAX_ANCHORS:
                                raise _error("非空单元格数量超过本地解析限制")
        finally:
            book.close()
        warnings.append("单元格保留值或公式原文；本地解析不计算公式，不提取图表或嵌入图片。")
    elif extension == ".pptx":
        parser, anchors = "presentationml:v1", _pptx_anchors(data)
        warnings.append("按幻灯片顺序提取正文和表格文字。备注、版式、图形和嵌入图片请对照原件。")
    else:
        from PIL import Image
        parser = "Pillow:" + package_version("Pillow")
        with Image.open(BytesIO(data)) as picture:
            expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[extension]
            if picture.format != expected:
                raise _error("图片内容与扩展名不符")
            width, height = picture.size
            if width * height > 40_000_000:
                raise _error("图片像素数超过本地解析限制")
            picture.verify()
        anchors = [{"locator": {"kind": "image"}, "text": "", "width": width, "height": height,
                    "coordinate_system": "image-top-left-pixels"}]
        warnings.append("本地只读取图片尺寸；图像内容和区域含义需由所选视觉模型或人工核对，未执行 OCR。")
    if len(anchors) > MAX_ANCHORS or sum(len(a.get("text", "")) for a in anchors) > MAX_PARSE_CHARS:
        raise _error("提取内容超过本地解析限制，原件仍保留")
    return parser, anchors, warnings


class FileMixin:
    def _file(self, db, version_id, parse_id=None):
        record = self._one(db, "file_versions", version_id)
        if parse_id is not None:
            parsed = db.execute("SELECT * FROM file_parses WHERE id=? AND file_version_id=?", (parse_id, version_id)).fetchone()
            if parsed is None:
                raise _error("文件解析版本不存在或不属于该原件")
        else:
            parsed = db.execute("SELECT * FROM file_parses WHERE file_version_id=? ORDER BY rowid DESC LIMIT 1", (version_id,)).fetchone()
        record.update(parse_id=parsed["id"] if parsed else None, parse_status=parsed["status"] if parsed else "not_requested",
                      parser=parsed["parser"] if parsed else "", parse_error=parsed["error"] if parsed else "",
                      anchors=json.loads(parsed["anchors_json"]) if parsed else [],
                      warnings=json.loads(parsed["warnings_json"]) if parsed else [])
        record["anchor_count"] = len(record["anchors"])
        return record

    def list_file_metadata(self, db, case_id):
        self._case(db, case_id)
        result = []
        for row in db.execute("SELECT id FROM file_versions WHERE case_id=? ORDER BY created_at,version", (case_id,)):
            record = self._file(db, row[0])
            record.pop("anchors")
            result.append(record)
        return result

    def list_files(self, case_id):
        with self._connection() as db:
            return self.list_file_metadata(db, case_id)

    def get_file(self, version_id):
        with self._connection() as db:
            record = self._file(db, version_id)
            record["reference_checks"] = []
            for row in db.execute("SELECT * FROM file_reference_checks WHERE file_version_id=? ORDER BY rowid", (version_id,)):
                check = dict(row)
                check["locator"] = json.loads(check.pop("locator_json"))
                record["reference_checks"].append(check)
            return record

    def file_bytes(self, version_id):
        record = self.get_file(version_id)
        try:
            data = (self.blobs / (record["sha256"] + ".bin")).read_bytes()
        except OSError:
            raise _error("文件原件缺失，请恢复备份", 409) from None
        if len(data) != record["size"] or sha256(data).hexdigest() != record["sha256"]:
            raise _error("文件原件完整性校验失败", 409)
        return record, data

    def add_file(self, case_id, filename, data, *, file_id=None, parse=True):
        from .service import _id, _now
        filename = _safe_name(filename)
        if not isinstance(data, bytes) or not data:
            raise _error("文件内容必须是非空字节")
        if len(data) > MAX_FILE_BYTES:
            raise _error("文件超过 25 MiB", 413)
        if type(parse) is not bool:
            raise _error("parse 必须是布尔值")
        if file_id is not None and (not isinstance(file_id, str) or not 1 <= len(file_id) <= 128):
            raise _error("逻辑文件 ID 无效")
        digest = sha256(data).hexdigest()
        record_id = _id()
        with self._connection(write=True) as db:
            self._case(db, case_id)
            version = 1
            if file_id is not None:
                prior = db.execute("SELECT * FROM file_versions WHERE file_id=? ORDER BY version DESC", (file_id,)).fetchall()
                if not prior:
                    raise _error("需要更新的逻辑文件不存在", 404)
                if any(row["case_id"] != case_id for row in prior):
                    raise _error("文件不属于当前案例")
                for row in prior:
                    if row["sha256"] == digest:
                        try:
                            original = (self.blobs / (digest + ".bin")).read_bytes()
                        except OSError:
                            raise _error("文件原件缺失，请恢复备份", 409) from None
                        if original != data:
                            raise _error("已有原件完整性校验失败", 409)
                        return self._file(db, row["id"])
                version = prior[0]["version"] + 1
            else:
                file_id = _id()
            path = self.blobs / (digest + ".bin")
            if path.exists():
                if path.read_bytes() != data:
                    raise _error("已有原件完整性校验失败", 409)
            else:
                temporary = self.blobs / (_id() + ".tmp")
                try:
                    temporary.write_bytes(data)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            db.execute("INSERT INTO file_versions VALUES(?,?,?,?,?,?,?,?,?)",
                       (record_id, file_id, case_id, version, filename, MIME_TYPES[Path(filename).suffix.lower()], digest, len(data), _now()))
            self._event(db, case_id, "file.added", {"id": record_id, "file_id": file_id, "version": version, "sha256": digest})
        return self.parse_file(record_id) if parse else self.get_file(record_id)

    def parse_file(self, version_id, *, extractor=None):
        """An alternate callable may return (parser_id, anchors, warnings).

        This is a Python extension point, never an HTTP-supplied code path.
        Each attempt creates a new immutable representation for pinned citations.
        """
        from .service import _id, _now, LedgerError
        record, data = self.file_bytes(version_id)
        parser, anchors, warnings, error, status = "", [], [], "", "ready"
        try:
            parser, anchors, warnings = (extractor or _extract)(record["filename"], data)
            if not isinstance(parser, str) or not parser or not isinstance(anchors, list) or not isinstance(warnings, list):
                raise _error("解析器返回结构无效，原件已保留")
            if len(anchors) > MAX_ANCHORS or sum(len(a.get("text", "")) for a in anchors) > MAX_PARSE_CHARS:
                raise _error("解析器输出超过本地解析限制，原件已保留")
            if parser == "unsupported":
                status, error = "unsupported", warnings[0]
        except ImportError:
            status, error = "unavailable", '未安装该格式的本地解析依赖，请安装 experience-ledger[files]，或使用支持的原生输入。'
        except (LedgerError, UnicodeError, BadZipFile) as exc:
            status, error = "failed", str(exc) if isinstance(exc, LedgerError) else "文件内容无法解析或编码不是 UTF-8"
        except Exception as exc:
            status, error = "failed", "本地解析失败（" + type(exc).__name__ + "），原件已保留；可更换解析器或使用支持的原生输入。"
        with self._connection(write=True) as db:
            self._one(db, "file_versions", version_id)
            db.execute("INSERT INTO file_parses VALUES(?,?,?,?,?,?,?,?)", (_id(), version_id, parser, status, error,
                       json.dumps(anchors, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False), _now()))
            self._event(db, record["case_id"], "file.parsed", {"id": version_id, "status": status, "parser": parser})
            return self._file(db, version_id)

    def _select_file_anchors(self, record, locator):
        if record["parse_status"] != "ready":
            raise _error("文件尚无可用本地解析；可解析原件或使用已配置的原生文件输入")
        if locator is None:
            return record["anchors"]
        if not isinstance(locator, dict):
            raise _error("文件位置必须是对象")
        target = {k: v for k, v in locator.items() if k not in ("parse_id", "bbox", "verification", "manual_check_id")}
        if not target or any(k not in ("kind", "page", "slide", "paragraph", "table", "row", "column", "sheet", "range") for k in target):
            raise _error("文件位置包含无效字段")
        for key in ("page", "slide", "paragraph", "table", "row", "column"):
            if key in target and (type(target[key]) is not int or target[key] < (1 if key in ("page", "slide") else 0)):
                raise _error("文件位置序号无效")
        if "sheet" in target and (not isinstance(target["sheet"], str) or not target["sheet"]):
            raise _error("工作表名称无效")
        if target.get("kind") == "xlsx" and isinstance(target.get("range"), str):
            target["range"] = target["range"].replace("$", "").upper()
        anchors = [a for a in record["anchors"] if a["locator"] == target]
        if not anchors and target.get("kind") == "xlsx" and isinstance(target.get("range"), str):
            from openpyxl.utils.cell import range_boundaries, coordinate_to_tuple
            try:
                min_col, min_row, max_col, max_row = range_boundaries(target["range"])
                if None in (min_col, min_row, max_col, max_row) or min_row < 1 or min_col < 1 or max_row > 1048576 or max_col > 16384:
                    raise ValueError()
                if min_row > max_row or min_col > max_col or (max_row-min_row+1)*(max_col-min_col+1) > MAX_ANCHORS:
                    raise ValueError()
            except (ValueError, TypeError):
                raise _error("工作表单元格范围无效") from None
            for anchor in record["anchors"]:
                position = anchor["locator"]
                if position.get("kind") == "xlsx" and position.get("sheet") == target.get("sheet"):
                    row, col = coordinate_to_tuple(position["range"])
                    if min_row <= row <= max_row and min_col <= col <= max_col:
                        anchors.append(anchor)
        if not anchors:
            raise _error("文件位置不存在，需核对固定原件与解析版本")
        return anchors

    def read_file(self, version_id, *, locator=None, offset=0, limit=4000, native=False):
        parse_id = locator.get("parse_id") if isinstance(locator, dict) else None
        with self._connection() as db:
            record = self._file(db, version_id, parse_id)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20000 or type(native) is not bool:
            raise _error("文件读取参数无效")
        result = {k: record[k] for k in ("file_id", "case_id", "version", "filename", "mime_type", "parse_id", "parse_status", "warnings")}
        result.update(source_id=version_id, source_type="file", anchors=[])
        if record["parse_status"] == "ready":
            anchors = self._select_file_anchors(record, locator)
            remaining, skipped = limit, offset
            for anchor in anchors:
                text = anchor.get("text", "")
                if skipped >= len(text) and text:
                    skipped -= len(text)
                    continue
                item = dict(anchor)
                item["locator"] = dict(anchor["locator"], parse_id=record["parse_id"])
                item.update(start=skipped, text=text[skipped:skipped+remaining], total_length=len(text))
                result["anchors"].append(item)
                remaining -= len(item["text"])
                skipped = 0
                if remaining <= 0 or len(result["anchors"]) >= 100:
                    result["truncated"] = True
                    break
        elif not native:
            raise _error(record["parse_error"] or "文件尚未解析；请选择本地解析或已配置的原生输入")
        if native:
            # Bytes are loaded immediately before the model call, not stored in
            # tool audit JSON, checkpoints, browser responses or prompt context.
            result["attachment"] = {"source_id": version_id, **{k: record[k] for k in ("filename", "mime_type", "sha256", "size")}}
        return result

    def _file_search(self, db, case_ids, terms, limit=4):
        """Search derived text inside the already-authorized case set."""
        matches = []
        for case_id in case_ids:
            for row in db.execute("""SELECT f.id FROM file_versions f WHERE f.case_id=?
                AND f.version=(SELECT MAX(v.version) FROM file_versions v WHERE v.file_id=f.file_id)
                ORDER BY f.created_at DESC""", (case_id,)):
                record = self._file(db, row[0])
                if record["parse_status"] != "ready":
                    continue
                for anchor in record["anchors"]:
                    text = anchor.get("text", "")
                    score = sum(text.casefold().count(term) + record["filename"].casefold().count(term) for term in terms)
                    if not score:
                        continue
                    positions = [text.casefold().find(term) for term in terms if term in text.casefold()]
                    start = max(0, min(positions, default=0) - 200)
                    matches.append((score, {"source_id": record["id"], "source_type": "file", "title": record["filename"],
                        "filename": record["filename"], "version": record["version"], "case_id": case_id,
                        "parse_id": record["parse_id"], "locator": dict(anchor["locator"], parse_id=record["parse_id"]),
                        "start": start, "text": text[start:start+3000], "total_length": len(text)}))
        return sorted(matches, key=lambda pair: pair[0], reverse=True)[:limit]

    def file_attachment(self, version_id):
        record, data = self.file_bytes(version_id)
        return {"filename": record["filename"], "mime_type": record["mime_type"], "data": data}

    def _file_reference_text(self, db, item):
        locator = item.get("locator") or {}
        if not locator.get("parse_id"):
            return ""  # Never substitute a later parse for a native-only quote.
        record = self._file(db, item["source_id"], locator.get("parse_id"))
        if record["parse_status"] != "ready":
            return ""  # A model-proposed quote is not an extracted original.
        anchors = self._select_file_anchors(record, locator)
        return "\n".join(a.get("text", "") for a in anchors)

    def _basic_file_reference(self, record, item):
        locator = item.get("locator")
        if not isinstance(locator, dict):
            raise _error("文件引用必须提供页码、幻灯片、段落、单元格或图片区域位置")
        # Verification claims belong to the store, not the caller or model.
        base = {k: v for k, v in locator.items() if k not in ("verification", "manual_check_id")}
        allowed = {"kind", "parse_id", "page", "slide", "paragraph", "table", "row", "column", "sheet", "range", "bbox"}
        if set(base) - allowed:
            raise _error("文件位置包含无效字段")
        if "parse_id" in base and (not isinstance(base["parse_id"], str) or not base["parse_id"]):
            raise _error("文件解析版本 ID 无效")
        extension = Path(record["filename"]).suffix.lower()
        expected = "text" if extension in (".txt", ".md") else "pdf" if extension == ".pdf" else "docx" if extension in (".doc", ".docx") else "xlsx" if extension in (".xls", ".xlsx") else "image"
        if extension in (".ppt", ".pptx"):
            expected = "slide"
        if base.get("kind") != expected:
            raise _error("引用位置类型与原件格式不符")
        position_keys = set(base) - {"kind", "parse_id", "bbox"}
        if expected == "pdf":
            if position_keys != {"page"} or type(base["page"]) is not int or base["page"] < 1:
                raise _error("PDF 页码须为正整数")
        elif expected == "slide":
            if position_keys != {"slide"} or type(base["slide"]) is not int or base["slide"] < 1:
                raise _error("幻灯片序号须为正整数")
        elif expected == "docx":
            if position_keys not in ({"paragraph"}, {"table", "row", "column"}) or any(type(base[k]) is not int or base[k] < 0 for k in position_keys):
                raise _error("Word 引用须指定非负段落序号或表格/行/列序号")
        elif expected == "xlsx":
            if position_keys != {"sheet", "range"} or not isinstance(base["sheet"], str) or not base["sheet"]:
                raise _error("Excel 引用须指定工作表和单元格范围")
            cell_range = base["range"]
            if isinstance(cell_range, str):
                cell_range = cell_range.replace("$", "").upper()
                base["range"] = cell_range
            if not isinstance(cell_range, str) or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}(?::[A-Z]{1,3}[1-9][0-9]{0,6})?", cell_range):
                raise _error("单元格范围格式无效")
            endpoints = cell_range.split(":")
            positions = []
            for cell in endpoints:
                match = re.fullmatch(r"([A-Z]+)([0-9]+)", cell)
                column = 0
                for letter in match[1]:
                    column = column * 26 + ord(letter) - 64
                row = int(match[2])
                if column > 16384 or row > 1048576:
                    raise _error("单元格范围超出 Excel 边界")
                positions.append((row, column))
            if len(positions) == 2 and (positions[0][0] > positions[1][0] or positions[0][1] > positions[1][1]):
                raise _error("单元格范围次序无效")
        elif position_keys:
            raise _error("该格式的引用位置字段无效")
        bbox = base.get("bbox")
        if bbox is not None:
            if expected not in ("image", "pdf") or not isinstance(bbox, list) or len(bbox) != 4 or any(type(n) not in (int, float) or not math.isfinite(n) for n in bbox):
                raise _error("引用区域须为有限数值 [x,y,width,height]")
            if bbox[0] < 0 or bbox[1] < 0 or bbox[2] <= 0 or bbox[3] <= 0:
                raise _error("引用区域位置或大小无效")
        if expected == "image" and bbox is None:
            raise _error("图片引用必须指定具体区域")
        quote = item.get("quote", "")
        if not isinstance(quote, str) or len(quote) > 10000:
            raise _error("文件引文须为不超过10000字符的字符串")
        start, end = item.get("start"), item.get("end")
        if start is not None or end is not None:
            if type(start) is not int or start < 0 or (end is not None and (type(end) is not int or end != start + len(quote))):
                raise _error("文件引文字符区间无效")
            if not quote:
                raise _error("无文字引文的区域引用不使用字符区间")
            end = start + len(quote) if end is None else end
        if not quote and (expected not in ("pdf", "image") or bbox is None):
            raise _error("无逐字引文时必须指定页面或图片的具体区域，供人工核对")
        return base, quote, start, end

    def _validate_file_reference(self, db, item, allowed_case_ids):
        locator = item.get("locator")
        if not isinstance(locator, dict):
            raise _error("文件引用必须提供页码、幻灯片、段落、单元格或图片区域位置")
        record = self._file(db, item.get("source_id"), locator.get("parse_id"))
        if record["case_id"] not in allowed_case_ids:
            raise _error("文件不属于本次经验明确关联的案例")
        self.file_bytes(record["id"])
        base, quote, start, end = self._basic_file_reference(record, item)
        check_id = locator.get("manual_check_id")
        manual = None
        if check_id is not None:
            if not isinstance(check_id, str):
                raise _error("人工核对记录 ID 无效")
            manual = db.execute("SELECT * FROM file_reference_checks WHERE id=?", (check_id,)).fetchone()
            if manual is None or manual["file_version_id"] != record["id"] or json.loads(manual["locator_json"]) != base or manual["quote"] != quote or manual["start"] != start or manual["end"] != end:
                raise _error("人工核对记录与该原件版本、位置或引文不匹配")
        anchors = None
        if record["parse_status"] == "ready":
            anchors = self._select_file_anchors(record, base)
            bbox = base.get("bbox")
            if bbox is not None:
                x, y, width, height = bbox
                if len(anchors) != 1 or x+width > anchors[0]["width"] or y+height > anchors[0]["height"]:
                    raise _error("引用区域超出原件页面或图片边界")
        verification = "human_checked" if manual else "pending_manual"
        if anchors is not None and not manual:
            text = "\n".join(a.get("text", "") for a in anchors)
            if quote and text:
                if start is None:
                    if text.count(quote) != 1:
                        raise _error("文件引文无法唯一定位，请提供锚点内的 start")
                    start = text.find(quote)
                end = start + len(quote) if end is None else end
                if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(text) or end-start != len(quote) or text[start:end] != quote:
                    raise _error("文件引文与固定解析锚点不一致")
                verification = "position_checked"
            elif not quote:
                verification = "position_checked"
            # Scanned PDF/image text has not been checked by a text extractor.
            base["parse_id"] = record["parse_id"]
        verified_locator = dict(base, verification=verification)
        if manual:
            verified_locator["manual_check_id"] = manual["id"]
        result = {"source_type": "file", "source_id": record["id"], "start": start, "end": end,
                  "quote": quote, "locator": verified_locator}
        if "field" in item:
            result["field"] = item["field"]
        return result

    def confirm_file_reference(self, version_id, locator, quote, actor, note, *, start=None, end=None):
        """Record a person's explicit check; it never edits existing revisions."""
        from .service import _id, _now, _text
        _text(actor, "核对人", limit=200)
        _text(note, "核对说明", limit=2000)
        if not isinstance(locator, dict):
            raise _error("文件位置必须是对象")
        without_claim = {k: v for k, v in locator.items() if k not in ("verification", "manual_check_id")}
        with self._connection(write=True) as db:
            record = self._file(db, version_id, without_claim.get("parse_id"))
            normalized = self._validate_file_reference(db, {"source_id": version_id, "locator": without_claim,
                         "quote": quote, "start": start, "end": end}, {record["case_id"]})
            base = {k: v for k, v in normalized["locator"].items() if k != "verification"}
            check_id = _id()
            db.execute("INSERT INTO file_reference_checks VALUES(?,?,?,?,?,?,?,?,?)", (check_id, version_id,
                       json.dumps(base, ensure_ascii=False, sort_keys=True), normalized["quote"], normalized["start"],
                       normalized["end"], actor, note, _now()))
            self._event(db, record["case_id"], "file.reference_checked", {"id": check_id, "file_version_id": version_id, "actor": actor})
            normalized["locator"] = dict(base, verification="human_checked", manual_check_id=check_id)
            return normalized
