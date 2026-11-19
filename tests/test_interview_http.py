"""HTTP UI contracts, with an injected synthetic planner."""

from experience_ledger.web import create_app
from experience_ledger.interviews.planner import UnconfiguredPlanner
from test_interviews import ScriptPlanner, ask, draft, known_context


def test_http_start_answer_review_and_static_resources(tmp_path):
    app = create_app(tmp_path, planner=ScriptPlanner(ask(), draft))
    client = app.test_client()
    assert client.get("/interviews").status_code == 200
    assert client.get("/static/interviews.js").status_code == 200
    assert client.get("/api/interview-config").json["configured"]
    response = client.post("/api/interviews", json=known_context(app.extensions["ledger"], {"goal": "合成 HTTP 访谈", "request_id": "start"}))
    assert response.status_code == 201
    session = response.json
    assert session["status"] == "waiting"
    path = f"/api/interviews/{session['id']}"
    response = client.post(path + "/answers", json={"question_id": session["pending"]["id"], "request_id": "answer", "answer": "合成专家原话", "speaker": "测试模拟专家"})
    assert response.status_code == 201 and response.json["status"] == "review"
    cid = response.json["candidate_ids"][0]
    candidate = client.get(f"/api/candidates/{cid}").json
    assert candidate["structured"]
    edited = client.post(f"/api/candidates/{cid}/content", json={"expected_revision": 1, "title": "合成编辑", "kind": "case_note",
        "content": candidate["content"], "evidence": candidate["evidence"], "actor": "测试编辑"})
    assert edited.status_code == 201 and edited.json["revision"] == 2
    review = client.post(f"/api/candidates/{cid}/reviews", json={"expected_revision": 2, "decision": "approve", "reviewer": "测试模拟审核", "note": "确认保留原话和未知"})
    assert review.status_code == 201
    assert len(client.get("/api/knowledge?q=合成专家").json) == 1
    assert client.get("/api/knowledge?q=supported").json == []


def test_invalid_contracts_and_unconfigured_model(tmp_path):
    client = create_app(tmp_path, planner=UnconfiguredPlanner()).test_client()
    for value in ([], {"goal": "x"}, {"goal": "x", "request_id": "s", "limits": {"max_calls": True}},
                  {"goal": "x", "request_id": "s", "approve": True}):
        result = client.post("/api/interviews", json=value)
        assert result.status_code == 400
    result = client.post("/api/interviews", json={"goal": "合成目标", "request_id": "start"})
    assert result.status_code == 201 and result.json["status"] == "waiting"
    assert result.json["pending"]["origin"] == "initial_text"
    assert result.json["calls"] == 0
    assert client.post("/api/interviews", json={"goal": "x", "request_id": "s"}, headers={"Origin": "https://evil.example"}).status_code == 403
