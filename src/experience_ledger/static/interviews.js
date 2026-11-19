"use strict";
const $ = selector => document.querySelector(selector);
const sessionState = { current: null, busy: false, startId: crypto.randomUUID(), answerId: crypto.randomUUID() };
let statusPoll = null;
const statuses = { ready: "待继续", running: "正在处理", waiting: "等待回答", paused: "已暂停", review: "待审核经验", closed: "已结束", error: "需要处理" };
const gapStatuses = { open: "待说明", covered: "已有依据 · 待核对", deferred: "已暂缓", conflict: "存在矛盾", not_applicable: "本次不适用" };
function el(tag, text, className) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; }
async function api(path, payload) {
 const response = await fetch(path, payload === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
 const result = await response.json(); if (!response.ok) throw new Error(result.error || "请求失败"); return result;
}
function notification(text, error=false) { const target=$("#message"); target.textContent=text; target.className=error?"error":""; target.hidden=false; }
async function task(work) {
 if(sessionState.busy)return; sessionState.busy=true;
 document.querySelectorAll("button").forEach(button=>button.disabled=!["pause-interview","close-interview"].includes(button.id));
 try { await work(); } catch(error) { notification(error.message, true); }
 finally { sessionState.busy=false; document.querySelectorAll("button").forEach(button=>button.disabled=false); controls(); }
}
async function listSessions() {
 const list=$("#session-list"); list.replaceChildren();
 for(const item of await api("/api/interviews")) { const button=el("button",null,"case-card" ); button.classList.toggle("selected",item.id===sessionState.current?.id); button.append(el("strong",item.goal),el("small",statuses[item.status])); button.addEventListener("click",()=>task(()=>open(item.id))); list.append(button); }
 if(!list.children.length) list.append(el("p","暂无访谈","empty-note"));
}
async function open(id) {
 sessionState.current=await api(`/api/interviews/${encodeURIComponent(id)}`); sessionState.answerId=crypto.randomUUID();
 try { localStorage.setItem("experience-ledger.interview",id); } catch(_) {}
 render(); await listSessions();
}
function controls() {
 clearTimeout(statusPoll);
 const item=sessionState.current; if(!item)return;
 $("#continue-interview").hidden=!["paused","error","ready","review","running"].includes(item.status);
 $("#pause-interview").hidden=["closed","paused"].includes(item.status);
 $("#close-interview").disabled=item.status==="closed";
 $("#draft-interview").hidden=!item.turns.length || item.status==="closed";
 $("#current-question").hidden=item.status!=="waiting";
 if(item.status==="running" && !sessionState.busy) statusPoll=setTimeout(()=>task(()=>open(item.id)),2000);
}
function render() {
 const item=sessionState.current;
 const detail=$("#session-detail");
 if(detail.dataset.session !== item.id) {
   detail.querySelectorAll("form").forEach(form => form.reset());
   detail.dataset.session=item.id;
 }
 $("#welcome").hidden=true; detail.hidden=false;
 $("#session-goal").textContent=item.goal; $("#session-domain").textContent=item.domain.name;
 $("#session-status").textContent=statuses[item.status]; $("#session-notice").textContent=item.notice;
 $("#case-link").href=item.case_id?`/?case_id=${encodeURIComponent(item.case_id)}`:"/";
 renderFocus(item);$("#supplement-box").hidden=item.status==="closed";
 const question=item.pending;
 const initialText=question?.origin==="initial_text";
 $("#question-label").textContent=initialText?"先提供一段内容":"当前问题";
 $("#answer-label").textContent=initialText?"原始内容":"你的回答";
 $('#answer-interview [name="answer"]').placeholder=initialText?"可以写一段经历、当前做法，或粘贴要整理的原文。记不清的地方可以注明。":"说说当时怎么做的。记不清的地方可以注明。";
 $("#skip-question").hidden=initialText;
 $("#question-feedback").hidden=initialText;
 if(question) { $("#question-text").textContent=question.question; $("#question-reason").textContent=question.reason; $("#question-expected").textContent=`希望补充：${question.expected_information}`; $("#question-hypothesis").textContent=question.hypothesis?`本轮包含系统假设：${question.hypothesis}`:"";$("#question-warnings").textContent=(question.quality_warnings||[]).map(v=>typeof v==="string"?v:JSON.stringify(v)).join("；"); }
 const turns=$("#conversation"); turns.replaceChildren();
 const correction=$("#correction-of");correction.replaceChildren();const initial=el("option","补充新内容");initial.value="";correction.append(initial);
 for(const turn of item.turns) { const record=el("article",null,"record-card"); record.append(el("small",`第 ${turn.interview_seq||turn.seq} 轮 · ${turn.speaker}${turn.correction_of?" · 追加纠正":""}`),el("p",turn.question,"turn-question"),el("p",turn.answer,"turn-answer")); if(turn.hypothesis)record.append(el("p",`系统假设：${turn.hypothesis}`,"hint"));record.append(turnAssociation(turn,item)); turns.append(record);const option=el("option",`纠正第${turn.interview_seq||turn.seq}轮 · ${turn.speaker}`);option.value=turn.id;correction.append(option); }
 if(!item.turns.length)turns.append(el("p","暂无问答记录。","empty-note"));
 const gaps=$("#session-gaps"); gaps.replaceChildren();
 for(const gap of item.gaps) { const record=el("article",null,"gap-record"); record.append(el("strong",gap.label),el("small",`${gapStatuses[gap.status]}${gap.required?" · 重要缺口":""}`),el("p",gap.reason)); for(const cite of gap.evidence){const detail=el("details"); detail.append(el("summary","原文依据"),el("blockquote",cite.quote));record.append(detail);} gaps.append(record); }
 const actions=$("#action-history"); actions.replaceChildren();
 for(const action of item.actions) { const detail=el("details",null,"record-card"); detail.append(el("summary",`${action.action?.action || "模型调用"} · ${action.model}`),el("p",action.error || action.action?.reason || "调用中断，尚无结果"),el("pre",JSON.stringify({outcome:action.outcome,usage:action.usage,prompt:action.prompt_version},null,2)));actions.append(detail); }
 $("#review-ready").hidden=!item.candidate_ids.length; const links=$("#candidate-links");links.replaceChildren();
 for(const id of item.candidate_ids) {const link=el("a","查看候选经验与证据 →","secondary");link.href=`/experience/${encodeURIComponent(id)}`;links.append(link);}
 controls();
}
function option(select,value,label){const item=el("option",label);item.value=value||"";select.append(item);}
function decisionOptions(caseSelect,decisionSelect,item){decisionSelect.replaceChildren();option(decisionSelect,"","先关注整个案例");const current=item.cases?.find(c=>c.id===caseSelect.value);for(const decision of current?.decisions||[])option(decisionSelect,decision.id,decision.title);}
function renderFocus(item) {
 const current=item.cases?.find(c=>c.id===item.case_id);
 const decision=current?.decisions.find(d=>d.id===item.decision_id);
 $("#focus-heading").textContent=current?`当前关注：${current.title}${decision?` / ${decision.title}`:""}`:"尚未选择案例";
 const cases=$("#focus-case"); cases.replaceChildren(); option(cases,"","暂不指定案例");
 for(const value of item.cases||[])option(cases,value.id,value.title);
 cases.value=item.case_id||"";decisionOptions(cases,$("#focus-decision"),item);$("#focus-decision").value=item.decision_id||"";
 const list=$("#focus-proposals");list.replaceChildren();
 for(const proposal of item.focus_proposals||[]) {
  if(proposal.status!=="pending")continue;
  const card=el("article",null,"focus-proposal");card.append(el("strong","建议接下来讨论"),el("p",proposal.summary||proposal.reason));
  for(const gap of Array.isArray(proposal.remaining)?proposal.remaining:[])card.append(el("p",`尚未解决：${gap}`,"hint"));
  const buttons=el("div",null,"button-row");
  for(const [choice,label]of [["accept","切换话题"],["reject","继续当前话题"]]) {
   const button=el("button",label,"secondary");button.type="button";
   button.addEventListener("click",()=>task(()=>mutate("focus",{proposal_id:proposal.id,decision:choice,note:choice==="accept"?"专家选择切换关注点":"专家选择继续当前，暂不转向建议重点"})));
   buttons.append(button);
  }
  card.append(buttons);list.append(card);
 }
 for(const topic of item.deferred_topics||[])list.append(el("p",`后续话题：${topic.title||topic.description||topic.reason}`,"hint"));
 document.querySelectorAll(".focus-panel > details").forEach(details=>details.hidden=item.status==="closed");
}
async function mutate(path,payload){const current=sessionState.current;sessionState.current=await api(`/api/interviews/${current.id}/${path}`,{expected_revision:current.revision,...payload});render();await listSessions();}
function turnAssociation(turn,item){const details=el("details",null,"turn-association");details.append(el("summary","关联这段原话到案例与判断"));const form=el("form");const quoteLabel=el("label","原话片段");const quote=el("textarea");quote.required=true;quote.rows=3;quote.value=turn.answer;quoteLabel.append(quote);const caseLabel=el("label","案例");const caseSelect=el("select");caseSelect.required=true;caseSelect.name="case_id";caseSelect.setAttribute("aria-label","关联案例");for(const record of item.cases||[])option(caseSelect,record.id,record.title);caseLabel.append(caseSelect);const decisionLabel=el("label","判断");const decisions=el("select");decisions.name="decision_id";decisions.setAttribute("aria-label","关联判断");decisionOptions(caseSelect,decisions,item);decisionLabel.append(decisions);caseSelect.addEventListener("change",()=>decisionOptions(caseSelect,decisions,item));const actorLabel=el("label","关联人");const actor=el("input");actor.required=true;actor.name="actor";actor.setAttribute("aria-label","关联人");actorLabel.append(actor);const button=el("button","保存片段关联","secondary");button.type="submit";form.append(quoteLabel,caseLabel,decisionLabel,actorLabel,button);form.addEventListener("submit",event=>{event.preventDefault();task(async()=>{const index=turn.answer.indexOf(quote.value);if(index<0||!quote.value)throw Error("请从原话中选择片段。");if(turn.answer.indexOf(quote.value,index+1)>=0)throw Error("该片段重复出现，请多选择一些上下文以确定位置。");const start=Array.from(turn.answer.slice(0,index)).length;await mutate("turn-contexts",{turn_id:turn.id,start,end:start+Array.from(quote.value).length,case_id:caseSelect.value,decision_id:decisions.value||null,actor:actor.value});notification("片段关联已保存，原话仍只保留一份。");});});details.append(form);const linked=(item.turn_contexts||[]).filter(c=>c.turn_id===turn.id);for(const context of linked){const record=item.cases.find(c=>c.id===context.case_id);details.append(el("p",`${record?.title||context.case_id} · 字符${context.start}–${context.end}`,"hint"));}return details;}
async function control(command) {
 const current=sessionState.current;
 const result=await api(`/api/interviews/${current.id}/control`,{command,expected_revision:current.revision,note:""});
 sessionState.current=result;sessionState.answerId=crypto.randomUUID();render();await listSessions();
}
$("#new-interview").addEventListener("click", () => {
 clearTimeout(statusPoll);
 sessionState.current = null;
 $("#session-list .selected")?.classList.remove("selected");
 $("#session-detail").hidden = true;
 $("#welcome").hidden = false;
 $("#message").hidden = true;
 $('#start-interview [name="goal"]').focus();
});
$("#start-interview").addEventListener("submit",event=>{event.preventDefault();task(async()=>{
 const values=Object.fromEntries(new FormData(event.target));if(!values.case_id)delete values.case_id;values.request_id=sessionState.startId;values.start_only=true;
 const result=await api("/api/interviews",values);sessionState.startId=crypto.randomUUID();
 $("#start-interview").elements.goal.value="";$("#start-case").value="";
 await open(result.id);
 if(result.status==="ready")await control("continue");
 if(sessionState.current.status==="error")notification(sessionState.current.notice,true);
});});
$("#answer-interview").addEventListener("submit",event=>{event.preventDefault();task(async()=>{
 const values=Object.fromEntries(new FormData(event.target));values.question_id=sessionState.current.pending.id;values.request_id=sessionState.answerId;
 const result=await api(`/api/interviews/${sessionState.current.id}/answers`,values);
 // An error response can follow a successful save. Preserve the same request ID
 // until the operator refreshes, so a network retry cannot duplicate the answer.
 event.target.elements.answer.value="";sessionState.current=result;sessionState.answerId=crypto.randomUUID();render();await listSessions();
 notification(result.status==="error"?result.notice:"回答已保存。",result.status==="error");
});});
$("#continue-interview").addEventListener("click",()=>task(()=>control("continue")));
$("#draft-interview").addEventListener("click",()=>task(()=>control("draft")));
async function stopWhileBusy(command) {
 if(!sessionState.busy)return task(()=>control(command));
 try { const current=await api(`/api/interviews/${sessionState.current.id}`);sessionState.current=current;await control(command);notification(command==="close"?"已结束；正在返回的模型结果不会继续执行。":"已暂停，已有记录保留。"); }
 catch(error){notification(error.message,true);}
}
$("#pause-interview").addEventListener("click",()=>stopWhileBusy("pause"));
$("#close-interview").addEventListener("click",()=>stopWhileBusy("close"));
$("#skip-question").addEventListener("click",()=>task(()=>control("skip")));
$("#focus-case").addEventListener("change",()=>decisionOptions($("#focus-case"),$("#focus-decision"),sessionState.current));
$("#focus-form").addEventListener("submit",event=>{event.preventDefault();task(async()=>{const values=Object.fromEntries(new FormData(event.target));values.case_id=values.case_id||null;values.decision_id=values.decision_id||null;await mutate("focus",values);notification("讨论主题已更新。");});});
$("#link-case-form").addEventListener("submit",event=>{event.preventDefault();task(async()=>{await mutate("cases",Object.fromEntries(new FormData(event.target)));notification("案例已关联，可以选择当前关注点。");});});
$("#supplement-form").addEventListener("submit",event=>{event.preventDefault();task(async()=>{const values=Object.fromEntries(new FormData(event.target));values.correction_of=values.correction_of||null;values.request_id=sessionState.supplementId||(sessionState.supplementId=crypto.randomUUID());sessionState.current=await api(`/api/interviews/${sessionState.current.id}/supplements`,values);sessionState.supplementId=crypto.randomUUID();event.target.reset();render();await listSessions();notification("原话已保存。");});});
$("#question-feedback-form").addEventListener("submit",event=>{event.preventDefault();task(async()=>{await mutate("question-feedback",{...Object.fromEntries(new FormData(event.target)),question_id:sessionState.current.pending.id});event.target.reset();notification("纠正意见已保存。");});});
$("#probe-model").addEventListener("click",()=>task(async()=>{
 const result=await api("/api/model-probe",{});
 notification(result.ok ? "连接成功，模型已返回访谈指令。" : result.error || "连接测试失败。", !result.ok);
}));
async function loadModelConfig() {
 const config=await api("/api/interview-config");$("#model-label").textContent=config.configured?`当前模型：${config.model}（已配置）`:"模型尚未配置";
 $("#start-model-note").hidden=config.configured;
 $("#model-help").textContent=config.configured?"测试连接可检查模型是否能返回访谈指令。":"可先记录访谈主题和原话。在“模型设置”中配置连接后，可启用自动提问。";
}
window.addEventListener("focus", () => loadModelConfig().catch(error => notification(error.message, true)));
task(async()=>{
 await loadModelConfig();
 for(const item of await api("/api/cases")){option($("#start-case"),item.id,item.title);option($("#link-case-select"),item.id,item.title);}
 const domains=await api("/api/domains"); for(const domain of domains){const option=el("option",domain.name);option.value=domain.id;$("#domain-select").append(option);} if(domains.some(d=>d.id==="general"))$("#domain-select").value="general";
 const sessions=await api("/api/interviews");let remembered;try{remembered=localStorage.getItem("experience-ledger.interview");}catch(_){}
 const selected=sessions.find(s=>s.id===remembered)||sessions[0];if(selected)await open(selected.id);else await listSessions();
});
