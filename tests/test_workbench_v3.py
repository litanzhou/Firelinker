"""Cross-module HTTP acceptance using synthetic records and injected models."""

from io import BytesIO
from hashlib import sha256

import pytest

from experience_ledger.demo import seed_demo
from experience_ledger.web import create_app
from experience_ledger.interviews.planner import Proposal


def setup_workbench(tmp_path, planner=None):
    app = create_app(tmp_path, planner=planner)
    ledger = app.extensions["ledger"]
    cases = seed_demo(ledger)
    candidates = [ledger.get_candidate(case["candidates"][0]["id"]) for case in cases]
    for candidate in candidates:
        ledger.review_candidate(candidate["id"], 1, "approve", "合成审核人", "合成：核对当前版本")
    return app, app.test_client(), cases, candidates


def citation(ref, side):
    return {"side": side, **{key: ref[key] for key in ("source_type", "source_id", "start", "end", "quote")}}


def test_confirmed_version_feedback_and_withdrawal_http(tmp_path):
    app, client, cases, candidates = setup_workbench(tmp_path)
    candidate = candidates[0]
    path = f"/api/candidates/{candidate['id']}"
    payload = {key: candidate[key] for key in ("title", "kind", "statement", "scope", "rationale", "exceptions")}
    payload.update(title="新草稿不可冒充确认版", expected_revision=1, actor="合成编辑", evidence=candidate["evidence"])
    assert client.post(path+"/revisions", json=payload).status_code == 201
    visible = client.get("/api/knowledge").json
    confirmed = next(c for c in visible if c["id"] == candidate["id"])
    assert confirmed["revision"] == 1 and confirmed["title"] == candidate["title"]
    assert all(r["revision"] == 1 for r in confirmed["revisions"])
    feedback = {"revision": 1, "actor": "合成使用者", "original": "仅参考，没有实际采用，结果还不清楚。",
                "actual_used": False, "conditions": "合成条件", "actions": "仅阅读", "outcome": "未知"}
    saved = client.post(path+"/feedback", json=feedback)
    assert saved.status_code == 201 and saved.json["actual_used"] is False
    assert client.post(path+"/feedback", json=dict(feedback, revision=2)).status_code == 400
    assert client.post(path+"/reviews", json={"expected_revision":2,"decision":"approve","reviewer":"合成审核人","note":"核对新版"}).status_code == 201
    assert client.get(path+"/feedback?revision=2").json == []
    assert client.get(path+"/feedback?revision=1").json[0]["original"] == feedback["original"]
    assert client.get(path).json["validation_status"] == "unvalidated"
    assert client.get(f"/experience/{candidate['id']}?revision=1").status_code == 200
    assert client.post(path+"/withdraw",json={"expected_revision":2,"expected_active_revision":1,"actor":"合成维护者","note":"旧页面撤下"}).status_code == 409
    assert client.post(path+"/withdraw",json={"expected_revision":2,"expected_active_revision":2,"actor":"合成维护者","note":"需要重新审视适用条件"}).status_code == 201
    assert all(c["id"] != candidate["id"] for c in client.get("/api/knowledge").json)
    assert client.get(path+"?revision=1").json["statement"] == candidate["statement"]


def test_relation_proposal_review_and_versions_http(tmp_path):
    app, client, cases, candidates = setup_workbench(tmp_path)
    left, right = candidates
    payload = {"left_id":left["id"],"left_revision":1,"right_id":right["id"],"right_revision":1,
               "kind":"conflict","conditions":"条件尚待核对","reason":"仅保存差异，不裁定正确与否","actor":"合成记录人",
               "evidence":[citation(left["evidence"][0],"left"),citation(right["evidence"][0],"right")]}
    proposed = client.post("/api/experience-relations", json=payload)
    assert proposed.status_code == 201 and proposed.json["status"] == "proposed"
    path=f"/api/experience-relations/{proposed.json['id']}/reviews"
    review={"decision":"accept","reviewer":"合成审核人","note":"确认存在需澄清的差异","expected_status":"proposed"}
    assert client.post(path,json=review).json["status"] == "accepted"
    assert client.post(path,json=review).status_code == 409
    assert client.post(path,json=dict(review,decision="resolve",expected_status="accepted",note="核对后发现目标不同，两条各自保留")).json["status"] == "resolved"
    assert client.get(f"/api/candidates/{left['id']}").json["statement"] == left["statement"]
    assert len(client.get("/api/experience-relations?candidate_id="+left["id"]).json) == 1


def test_ai_can_only_propose_the_selected_pair(tmp_path):
    class ComparisonModel:
        configured=True
        model="synthetic-comparison-model"
        replace_id=False
        def propose(self,context,feedback=""):
            assert all("revisions" not in context[side] and "feedback_history" not in context[side] for side in ("left", "right"))
            assert all("source_text" not in ref for side in ("left", "right") for ref in context[side]["evidence"])
            left,right=context["left"],context["right"]
            return Proposal({"action":"propose_relation","reason":"两份原话的目标不同，建议核对条件。",
                "left_id":"invented" if self.replace_id else left["id"],"left_revision":left["revision"],
                "right_id":right["id"],"right_revision":right["revision"],"relation_kind":"conflict",
                "conditions":"原始目标不同，不据此判定谁正确",
                "relation_evidence":[citation(left["evidence"][0],"left"),citation(right["evidence"][0],"right")]}, self.model, 12)
    model=ComparisonModel()
    app,client,cases,candidates=setup_workbench(tmp_path,model)
    left,right=candidates
    payload={"left_id":left["id"],"left_revision":1,"right_id":right["id"],"right_revision":1,"request_id":"compare-1"}
    proposed=client.post("/api/experience-relations/suggest",json=payload)
    assert proposed.status_code == 201 and proposed.json["status"] == "proposed"
    assert "AI" in proposed.json["actor"]
    assert client.post("/api/experience-relations/suggest",json=payload).json["id"] == proposed.json["id"]
    model.replace_id=True
    rejected=client.post("/api/experience-relations/suggest",json=dict(payload,request_id="compare-2"))
    assert rejected.status_code == 400
    assert len(client.get("/api/experience-relations").json) == 1


def test_original_upload_versions_download_and_configuration(tmp_path):
    app,client,cases,candidates=setup_workbench(tmp_path)
    case_id=cases[0]["id"]
    original="合成原件\r\n原始换行与字节保留".encode("utf-8")
    uploaded=client.post(f"/api/cases/{case_id}/files", data={"file":(BytesIO(original),"synthetic.txt")})
    assert uploaded.status_code == 201
    metadata=uploaded.json
    assert metadata["sha256"] == sha256(original).hexdigest()
    assert client.get(f"/api/files/{metadata['id']}/download").data == original
    assert client.get(f"/api/files/{metadata['id']}/preview").status_code == 400
    newer=client.post(f"/api/cases/{case_id}/files",data={"file_id":metadata["file_id"],"file":(BytesIO(b'new contents'),"synthetic.txt")})
    assert newer.status_code == 201 and newer.json["version"] == 2
    assert client.get(f"/api/files/{metadata['id']}/download").data == original
    assert client.post(f"/api/cases/{case_id}/files",data={"file":(BytesIO(original),"synthetic.txt")},headers={"Origin":"https://evil.example"}).status_code == 403
    config=client.get("/api/interview-config")
    assert config.status_code == 200 and config.json["configured"] is False
    assert client.post("/api/model-probe",json={}).status_code == 400
    assert client.get("/static/experience.js").status_code == 200
    assert client.get(f"/api/candidates/{candidates[0]['id']}/feedback?revision=abc").status_code == 400


def test_supplement_without_case_and_human_focus_http(tmp_path):
    app=create_app(tmp_path)
    client=app.test_client()
    session=client.post("/api/interviews",json={"goal":"合成目标","request_id":"start","start_only":True}).json
    assert session["case_id"] is None
    path=f"/api/interviews/{session['id']}"
    supplement={"request_id":"raw-1","speaker":"合成专家","answer":"当时凭手感判断，无法解释。"}
    saved=client.post(path+"/supplements",json=supplement)
    assert saved.status_code == 201 and len(saved.json["turns"]) == 1
    assert client.post(path+"/supplements",json=supplement).json["turns"] == saved.json["turns"]
    case=app.extensions["ledger"].create_case("general","合成案例","合成背景")
    linked=client.post(path+"/cases",json={"expected_revision":saved.json["revision"],"case_id":case["id"]})
    assert linked.status_code == 201
    focused=client.post(path+"/focus",json={"expected_revision":linked.json["revision"],"case_id":case["id"]})
    assert focused.status_code == 201 and focused.json["case_id"] == case["id"]
    turn=saved.json["turns"][0]
    associated=client.post(path+"/turn-contexts",json={"expected_revision":focused.json["revision"],"turn_id":turn["id"],"start":0,"end":len(turn["answer"]),"case_id":case["id"],"actor":"合成记录人"})
    assert associated.status_code == 201
    assert associated.json["turns"][0]["answer"] == supplement["answer"]
