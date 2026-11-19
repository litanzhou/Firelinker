"""HTTP acceptance checks; all data and review identities here are synthetic."""

import pytest

from experience_ledger.web import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(tmp_path / "web-data")


@pytest.fixture
def client(app):
    return app.test_client()


def post(client, path, **payload):
    response = client.post(path, json=payload)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_http_case_interview_review_and_restart(client, app, tmp_path):
    domain = client.get("/api/domains").get_json()[0]
    case = post(client, "/api/cases", domain_id=domain["id"], title="合成 HTTP 案例", context="仅用于验证记录闭环。")
    base = f"/api/cases/{case['id']}"
    source = post(client, base + "/sources", title="合成手册", text="先确认现场条件，再作出判断。")
    decision = post(client, base + "/decisions", title="先检查连接", description="合成现象：操作没有响应。")
    state = client.get(base).get_json()
    assert state["sources"][0]["id"] == source["id"]
    for gap in state["gaps"]:
        post(client, base + "/turns", gap_id=gap["id"], answer=f"合成回答：{gap['kind']}。", speaker="测试专家")
    candidate = post(client, base + "/candidates", decision_id=decision["id"])
    assert client.get("/api/knowledge").get_json() == []
    item_url = f"/api/candidates/{candidate['id']}"
    approved = post(client, item_url + "/reviews", expected_revision=1, decision="approve", reviewer="测试审核人", note="测试模拟审核，非真实专家背书。")
    assert approved["status"] == "approved"
    knowledge = client.get("/api/knowledge", query_string={"q": "合成回答", "domain_id": domain["id"]}).get_json()
    assert len(knowledge) == 1
    assert knowledge[0]["validation_status"] == "unvalidated"
    loaded = client.get(item_url).get_json()
    assert len(loaded["reviews"]) == 1
    assert all(citation["source_text"] == citation["quote"] for citation in loaded["evidence"])

    revision = {key: loaded[key] for key in ("title", "kind", "statement", "scope", "rationale", "exceptions")}
    revision.update(expected_revision=1, actor="测试编辑人", evidence=[{key: citation[key] for key in ("field", "source_type", "source_id", "start", "end", "quote")} for citation in loaded["evidence"]])
    revision["title"] = "合成案例的新修订"
    revised = post(client, item_url + "/revisions", **revision)
    assert revised["revision"] == 2 and revised["status"] == "pending"
    visible = client.get("/api/knowledge").get_json()
    assert len(visible) == 1 and visible[0]["revision"] == 1
    assert visible[0]["title"] == loaded["title"]
    stale = client.post(item_url + "/reviews", json={"expected_revision": 1, "decision": "approve", "reviewer": "测试人", "note": "旧页面提交"})
    assert stale.status_code == 409
    rejected = post(client, item_url + "/reviews", expected_revision=2, decision="reject", reviewer="测试审核人", note="合成驳回意见")
    assert rejected["status"] == "rejected"
    restarted = create_app(tmp_path / "web-data").test_client()
    history = restarted.get(item_url).get_json()
    assert len(history["revisions"]) == 2 and len(history["reviews"]) == 2
    assert restarted.get(base).get_json()["turns"] == client.get(base).get_json()["turns"]


@pytest.mark.parametrize("headers", [
    {"Origin": "https://attacker.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://localhost:9999"},
])
def test_cross_site_write_rejected_before_mutation(client, headers):
    response = client.post("/api/cases", json={"domain_id": "electronics_repair", "title": "unwanted", "context": "x"}, headers=headers)
    assert response.status_code == 403
    assert client.get("/api/cases").get_json() == []


def test_local_origin_and_rebinding_guard(client):
    response = client.post("/api/cases", json={"domain_id": "electronics_repair", "title": "local", "context": "synthetic"}, headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 201
    assert client.get("/api/cases", headers={"Host": "evil.example"}).status_code == 403


@pytest.mark.parametrize("payload", [[], None, {"title": "missing"}, {"domain_id": "electronics_repair", "title": 123, "context": "x"}, {"domain_id": "electronics_repair", "title": "x", "context": "x", "admin": True}])
def test_invalid_json_shape_is_client_error(client, payload):
    response = client.post("/api/cases", json=payload)
    assert response.status_code in (400, 415)
    assert "error" in response.get_json()
    assert client.get("/api/cases").get_json() == []


def test_error_responses_and_request_limits(client, app, monkeypatch):
    assert client.post("/api/cases", data="{broken", content_type="application/json").status_code == 400
    assert client.post("/api/cases", data="title=bad", content_type="application/x-www-form-urlencoded").status_code == 415
    assert client.get("/api/missing").status_code == 404
    assert client.delete("/api/cases").status_code == 405
    large = client.post("/api/cases", json={"domain_id": "electronics_repair", "title": "x", "context": "a" * (2 * 1024 * 1024)})
    assert large.status_code == 413
    malformed_revision = client.post("/api/candidates/nonexistent/reviews", json={"expected_revision": True, "decision": "approve", "reviewer": "x", "note": "x"})
    assert malformed_revision.status_code == 400

    def fail():
        raise RuntimeError("private internal detail")

    monkeypatch.setattr(app.extensions["ledger"], "list_cases", fail)
    response = client.get("/api/cases")
    assert response.status_code == 500
    assert "private internal detail" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_workbench_assets_and_browser_headers(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "传火" in response.get_data(as_text=True)
    assert "FIRELINKER" in response.get_data(as_text=True)
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
