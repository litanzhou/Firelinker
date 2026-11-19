"""Local, same-origin workbench over the public Ledger application interface."""

from pathlib import Path
from io import BytesIO
from dataclasses import asdict, is_dataclass
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from pydantic import ValidationError

from .service import Ledger, LedgerError
from .interviews.planner import PlannerError


def create_app(data_dir=None, domains_dir=None, *, planner=None):
    app = Flask(__name__, static_folder="static")
    app.config.update(MAX_CONTENT_LENGTH=26 * 1024 * 1024, JSON_SORT_KEYS=False)
    ledger = Ledger(Path(data_dir or ".local-data"), domains_dir=domains_dir)
    app.extensions["ledger"] = ledger
    from .interviews.runtime import InterviewRuntime
    interview = InterviewRuntime(ledger, planner=planner)
    app.extensions["interviews"] = interview

    @app.errorhandler(ValidationError)
    def contract_error(error):
        details = [".".join(map(str, e["loc"])) + ": " + e["msg"] for e in error.errors(include_input=False)]
        return jsonify(error="；".join(details)[:2000]), 400

    @app.before_request
    def local_origin_only():
        # Reject DNS rebinding as well as cross-site writes. This is a local
        # single-user application; these checks are not an authentication layer.
        if urlsplit(request.host_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            return jsonify(error="仅允许通过本机地址访问。"), 403
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("Origin")
            if (origin and origin.rstrip("/") != request.host_url.rstrip("/")) or request.headers.get("Sec-Fetch-Site") == "cross-site":
                return jsonify(error="拒绝跨站写入请求。"), 403
        if request.is_json and request.content_length and request.content_length > 2 * 1024 * 1024:
            raise RequestEntityTooLarge()

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(LedgerError)
    def ledger_error(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(PlannerError)
    def planner_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {400: "请求格式错误。", 404: "记录或接口不存在。", 405: "请求方法不支持。", 413: "上传或请求内容过大。", 415: "请提交 JSON 请求。"}
        return jsonify(error=messages.get(error.code, error.name)), error.code

    @app.errorhandler(Exception)
    def internal_error(error):
        app.logger.exception("Unhandled workbench request failure")
        return jsonify(error="处理失败，请检查本地服务日志。"), 500

    def body(required, optional=(), integers=(), arrays=(), booleans=()):
        if not request.is_json:
            raise LedgerError("请提交 JSON 对象。", status=415)
        value = request.get_json()
        if not isinstance(value, dict):
            raise LedgerError("请求内容必须是 JSON 对象。")
        if set(value) - set(required) - set(optional):
            raise LedgerError("请求包含不支持的字段。")
        for key in required:
            if key not in value:
                raise LedgerError(f"缺少字段：{key}")
        for key, item in value.items():
            if key in booleans:
                valid = type(item) is bool or item is None
            elif key in integers:
                valid = isinstance(item, int) and not isinstance(item, bool) and item > 0
            elif key in arrays:
                valid = isinstance(item, list) and all(isinstance(row, dict) for row in item)
            else:
                valid = isinstance(item, str) or (key in optional and item is None)
            if not valid:
                raise LedgerError(f"字段类型错误：{key}")
        return value

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/interviews")
    def interviews_page():
        return app.send_static_file("interviews.html")

    @app.get("/knowledge")
    def knowledge_page():
        return app.send_static_file("knowledge.html")

    @app.get("/settings")
    def settings_page():
        return app.send_static_file("settings.html")

    @app.get("/experience/<candidate_id>")
    def experience_page(candidate_id):
        ledger.get_candidate(candidate_id)
        return app.send_static_file("experience.html")

    def json_object():
        if not request.is_json:
            raise LedgerError("请提交 JSON 对象", 415)
        value = request.get_json()
        if not isinstance(value, dict):
            raise LedgerError("请求内容必须是 JSON 对象")
        return value

    @app.get("/api/interview-config")
    def interview_config():
        with interview.models.use() as planner:
            if hasattr(planner, "describe"):
                return jsonify(planner.describe())
            capabilities = getattr(planner, "capabilities", None)
            if is_dataclass(capabilities):
                capabilities = asdict(capabilities)
            elif hasattr(capabilities, "model_dump"):
                capabilities = capabilities.model_dump()
            return jsonify(configured=planner.configured, model=planner.model,
                           capabilities=capabilities, probe=None)

    @app.get("/api/model-settings")
    def model_settings():
        return jsonify(interview.models.describe())

    @app.put("/api/model-settings")
    def save_model_settings():
        return jsonify(interview.models.save(json_object()))

    @app.post("/api/model-settings/probe")
    def probe_model_settings():
        return jsonify(interview.models.probe(json_object()))

    @app.post("/api/model-probe")
    def model_probe():
        body(())
        with interview.models.use() as planner:
            if not planner.configured:
                raise LedgerError("请先在“模型设置”中保存连接配置。")
            if not hasattr(planner, "probe"):
                raise LedgerError("当前模型适配器没有提供连接测试。")
            return jsonify(planner.probe())

    @app.get("/api/interviews")
    def interviews():
        return jsonify(interview.store.list())

    @app.post("/api/interviews")
    def start_interview():
        return jsonify(interview.start(json_object())), 201

    @app.get("/api/interviews/<sid>")
    def get_interview(sid):
        return jsonify(interview.store.get(sid))

    @app.post("/api/interviews/<sid>/answers")
    def answer_interview(sid):
        return jsonify(interview.answer(sid, json_object())), 201

    @app.post("/api/interviews/<sid>/control")
    def control_interview(sid):
        return jsonify(interview.control(sid, json_object())), 201

    @app.post("/api/interviews/<sid>/supplements")
    def supplement_interview(sid):
        return jsonify(interview.supplement(sid, json_object())), 201

    @app.post("/api/interviews/<sid>/focus")
    def focus_interview(sid):
        return jsonify(interview.set_focus(sid, json_object())), 201

    @app.post("/api/interviews/<sid>/cases")
    def link_interview_case(sid):
        return jsonify(interview.link_case(sid, json_object())), 201

    @app.post("/api/interviews/<sid>/turn-contexts")
    def link_interview_turn(sid):
        return jsonify(interview.link_turn(sid, json_object())), 201

    @app.post("/api/interviews/<sid>/question-feedback")
    def question_feedback(sid):
        return jsonify(interview.quality_feedback(sid, json_object())), 201

    @app.get("/api/domains")
    def domains():
        return jsonify(ledger.domains())

    @app.get("/api/cases")
    def cases():
        return jsonify(ledger.list_cases())

    @app.post("/api/cases")
    def create_case():
        return jsonify(ledger.create_case(**body(("domain_id", "title", "context")))), 201

    @app.get("/api/cases/<case_id>")
    def get_case(case_id):
        return jsonify(ledger.get_case(case_id))

    @app.post("/api/cases/<case_id>/sources")
    def add_source(case_id):
        return jsonify(ledger.add_source(case_id, **body(("title", "text"), ("source_id",)))), 201

    @app.get("/api/cases/<case_id>/files")
    def list_files(case_id):
        return jsonify(ledger.list_files(case_id))

    @app.post("/api/cases/<case_id>/files")
    def upload_file(case_id):
        if set(request.form) - {"file_id", "parse"} or set(request.files) != {"file"}:
            raise LedgerError("请选择一个文件；仅支持 file_id 和 parse 附加字段。")
        upload = request.files["file"]
        if request.form.get("parse", "true") not in {"true", "false"}:
            raise LedgerError("parse 须为 true 或 false。")
        result = ledger.add_file(case_id, upload.filename or "", upload.read(25 * 1024 * 1024 + 1),
                                 file_id=request.form.get("file_id") or None,
                                 parse=request.form.get("parse", "true") == "true")
        return jsonify(result), 201

    @app.get("/api/files/<version_id>")
    def file_metadata(version_id):
        return jsonify(ledger.get_file(version_id))

    @app.get("/api/files/<version_id>/download")
    def download_file(version_id):
        metadata, content = ledger.file_bytes(version_id)
        return send_file(BytesIO(content), mimetype="application/octet-stream", as_attachment=True,
                         download_name=metadata["filename"], max_age=0)

    @app.get("/api/files/<version_id>/preview")
    def preview_file(version_id):
        metadata, content = ledger.file_bytes(version_id)
        if metadata["mime_type"] not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            raise LedgerError("该类型请下载原件查看。")
        return send_file(BytesIO(content), mimetype=metadata["mime_type"], max_age=0)

    @app.post("/api/files/<version_id>/parse")
    def parse_file(version_id):
        body(())
        return jsonify(ledger.parse_file(version_id))

    @app.post("/api/files/<version_id>/reference-checks")
    def confirm_file_reference(version_id):
        value = json_object()
        if set(value) - {"locator", "quote", "actor", "note", "start", "end"}:
            raise LedgerError("文件引文确认包含不支持的字段。")
        if not isinstance(value.get("locator"), dict) or any(not isinstance(value.get(k), str) for k in ("quote", "actor", "note")):
            raise LedgerError("请提交引用位置、引文、核对人及核对说明。")
        for key in ("start", "end"):
            if key in value and value[key] is not None and (type(value[key]) is not int or value[key] < 0):
                raise LedgerError("引文字符位置无效。")
        return jsonify(ledger.confirm_file_reference(version_id, **value)), 201

    @app.post("/api/cases/<case_id>/decisions")
    def add_decision(case_id):
        return jsonify(ledger.add_decision(case_id, **body(("title", "description")))), 201

    @app.post("/api/cases/<case_id>/turns")
    def add_turn(case_id):
        return jsonify(ledger.add_turn(case_id, **body(("gap_id", "answer", "speaker"), ("question",)))), 201

    @app.post("/api/cases/<case_id>/candidates")
    def draft_candidate(case_id):
        return jsonify(ledger.draft_candidate(case_id, **body(("decision_id",)))), 201

    @app.get("/api/candidates/<candidate_id>")
    def get_candidate(candidate_id):
        revision = request.args.get("revision")
        if revision is not None and (not revision.isascii() or not revision.isdecimal() or int(revision) < 1):
            raise LedgerError("revision 必须是正整数。")
        return jsonify(ledger.get_candidate(candidate_id, revision=int(revision) if revision else None))

    @app.post("/api/candidates/<candidate_id>/revisions")
    def revise_candidate(candidate_id):
        fields = ("expected_revision", "title", "kind", "statement", "scope", "rationale", "exceptions", "evidence", "actor")
        return jsonify(ledger.revise_candidate(candidate_id, **body(fields, ("contexts",), integers=("expected_revision",), arrays=("evidence", "contexts")))), 201

    @app.post("/api/candidates/<candidate_id>/content")
    def revise_content(candidate_id):
        fields = ("expected_revision", "title", "kind", "content", "evidence", "actor")
        return jsonify(ledger.revise_content(candidate_id, **body(fields, ("contexts",), integers=("expected_revision",), arrays=("content", "evidence", "contexts")))), 201

    @app.post("/api/candidates/<candidate_id>/reviews")
    def review_candidate(candidate_id):
        fields = ("expected_revision", "decision", "reviewer", "note")
        return jsonify(ledger.review_candidate(candidate_id, **body(fields, integers=("expected_revision",)))), 201

    @app.post("/api/candidates/<candidate_id>/withdraw")
    def withdraw_candidate(candidate_id):
        return jsonify(ledger.withdraw_candidate(candidate_id, **body(
            ("expected_revision", "expected_active_revision", "actor", "note"), integers=("expected_revision", "expected_active_revision")))), 201

    @app.get("/api/candidates/<candidate_id>/feedback")
    def list_feedback(candidate_id):
        revision = request.args.get("revision")
        if revision is not None and (not revision.isascii() or not revision.isdecimal() or int(revision) < 1):
            raise LedgerError("revision 必须是正整数。")
        return jsonify(ledger.list_feedback(candidate_id, revision=int(revision) if revision else None))

    @app.post("/api/candidates/<candidate_id>/feedback")
    def add_feedback(candidate_id):
        fields = ("revision", "actor", "original", "actual_used", "conditions", "actions", "outcome")
        return jsonify(ledger.add_feedback(candidate_id, **body(fields, ("case_id",),
                       integers=("revision",), booleans=("actual_used",)))), 201

    @app.get("/api/experience-relations")
    def list_relations():
        return jsonify(ledger.list_relations(request.args.get("candidate_id"), request.args.get("status")))

    @app.post("/api/experience-relations")
    def propose_relation():
        fields = ("left_id", "left_revision", "right_id", "right_revision", "kind", "conditions", "reason", "evidence", "actor")
        return jsonify(ledger.propose_relation(**body(fields, integers=("left_revision", "right_revision"), arrays=("evidence",)))), 201

    @app.post("/api/experience-relations/suggest")
    def suggest_relation():
        from .comparison import compare_experiences
        fields = ("left_id", "left_revision", "right_id", "right_revision", "request_id")
        selected = body(fields, integers=("left_revision", "right_revision"))
        request_id = selected.pop("request_id")
        with interview.models.use() as planner:
            return jsonify(compare_experiences(ledger, planner, selected, request_id)), 201

    @app.post("/api/experience-relations/<relation_id>/reviews")
    def review_relation(relation_id):
        return jsonify(ledger.review_relation(relation_id, **body(
            ("decision", "reviewer", "note"), ("expected_status",)))), 201

    @app.get("/api/knowledge")
    def knowledge():
        return jsonify(ledger.search_knowledge(request.args.get("q", ""), request.args.get("domain_id") or None))

    return app
