"use strict";
const $ = selector => document.querySelector(selector);
const experienceId = decodeURIComponent(location.pathname.split("/").pop());
let currentExperience, comparedExperience, busy = false;
const kindLabels = {duplicate:"内容和条件相同", supplement:"补充经验", conflict:"差异与条件待核对"};
const relationStatuses = {proposed:"待人工核对", accepted:"已确认关联", rejected:"关联建议已驳回", resolved:"已记录澄清结果"};
function element(tag,text,className){const value=document.createElement(tag);if(text!=null)value.textContent=text;if(className)value.className=className;return value;}
function notice(text,error=false){$("#message").hidden=false;$("#message").textContent=text;$("#message").className=error?"error":"";}
async function api(path,method="GET",data){const options={method,headers:{Accept:"application/json"}};if(data!==undefined){options.headers["Content-Type"]="application/json";options.body=JSON.stringify(data);}const response=await fetch(path,options);const result=await response.json();if(!response.ok)throw Error(result.error||"请求失败");return result;}
async function task(action){if(busy)return;busy=true;$("#revision-select").disabled=true;document.querySelectorAll("button").forEach(b=>b.disabled=true);try{await action();}catch(e){notice(e.message,true);}finally{busy=false;$("#revision-select").disabled=false;document.querySelectorAll("button").forEach(b=>b.disabled=false);}}
function fields(form){return Object.fromEntries(new FormData(form));}
function revisionLink(id,revision,label){const a=element("a",label);a.href=`/experience/${encodeURIComponent(id)}?revision=${revision}`;return a;}
function quoteText(cite){return cite.quote || (cite.locator?JSON.stringify(cite.locator):"查看原始资料区域");}
async function openRevision(revision){
 const latest=await api(`/api/candidates/${experienceId}`);const selected=revision||latest.active_revision||latest.revision;
 currentExperience=selected===latest.revision?latest:await api(`/api/candidates/${experienceId}?revision=${selected}`);
 const item=currentExperience;$("#experience-title").textContent=item.title;
 const approved=(item.reviews||[]).some(r=>r.revision===item.revision&&r.decision==="approve");
 const reviewStatus = {approved:"已确认", pending:"待审核", rejected:"已驳回"};
 $("#version-state").textContent = `v${item.revision} · ${reviewStatus[item.status]}`;
 const catalogVersion = item.active_revision ? `经验库显示 v${item.active_revision}。` : "经验库暂无可查阅版本。";
 const newer = item.latest_revision > item.revision ? `v${item.latest_revision} ${reviewStatus[item.latest_status]}。` : "";
 $("#version-notice").textContent = `${catalogVersion}${newer}尚未经过案例验证。`;
 const select=$("#revision-select");select.replaceChildren();for(const r of item.revisions||[]){const option=element("option",`v${r.revision}${r.revision===item.active_revision?" · 当前确认版":""}`);option.value=r.revision;option.selected=r.revision===item.revision;select.append(option);}
 $("#edit-link").href=`/?candidate_id=${encodeURIComponent(item.id)}${item.case_id?`&case_id=${encodeURIComponent(item.case_id)}`:""}`;
 const content=$("#experience-content");content.replaceChildren();
 const values=item.structured?item.content:[{key:"statement",text:item.statement},{key:"scope",text:item.scope},{key:"rationale",text:item.rationale},{key:"exceptions",text:item.exceptions}];
 const fallback={statement:"经验表述",scope:"适用条件",rationale:"判断依据",exceptions:"例外与边界"};
 const labels=new Map(Object.entries(fallback));
 if(item.structured) for(const field of item.content_schema) labels.set(field.key,field.label);
 for(const field of values){const article=element("article");article.append(element("h3",labels.get(field.key)||field.key),element("p",field.text));if(field.status)article.append(element("small",{supported:"已有原话依据",unknown:"尚未说明",hypothesis:"待检验假设"}[field.status],"content-status"));content.append(article);}
 const evidence=$("#experience-evidence");evidence.replaceChildren();for(const cite of item.evidence||[]){const block=element("div",null,"evidence-row");block.append(element("strong",`${labels.get(cite.field)||cite.field} · ${cite.source_title}`),element("blockquote",quoteText(cite)));if(cite.locator)block.append(element("small",JSON.stringify(cite.locator)));evidence.append(block);}if(!item.evidence?.length)evidence.append(element("p","本版暂无引文。","empty-note"));
 $("#withdraw-panel").hidden=!item.active_revision;$("#feedback-panel").hidden=!approved;
 await renderRelations();await renderFeedback();$("#comparison").hidden=true;
}
async function renderRelations(){const list=$("#relation-list");list.replaceChildren();const relations=await api(`/api/experience-relations?candidate_id=${experienceId}`);for(const relation of relations){const card=element("article",null,"relation-record");card.append(element("strong",`${kindLabels[relation.kind]} · ${relationStatuses[relation.status]}`));const links=element("div",null,"relation-endpoints");links.append(revisionLink(relation.left_id,relation.left_revision,`第一条 v${relation.left_revision}`),revisionLink(relation.right_id,relation.right_revision,`另一条 v${relation.right_revision}`));if(relation.requires_recheck)card.append(element("p","经验已有新版本，请重新核对这项关联。","hint"));card.append(links,element("p",`比较条件：${relation.conditions}`),element("p",relation.reason),element("small",`提出者：${relation.actor}`));for(const cite of relation.evidence||[])card.append(element("blockquote",`${cite.side==="left"?"第一条":"另一条"}：${quoteText(cite)}`));
 const reviews=relation.reviews||[];for(const review of reviews)card.append(element("p",`${review.reviewer}：${review.note}`,"hint"));
 if(["proposed","accepted"].includes(relation.status)){const form=element("form");const actor=element("input");actor.name="reviewer";actor.required=true;actor.placeholder="核对人";actor.setAttribute("aria-label","关联核对人");const note=element("textarea");note.name="note";note.required=true;note.rows=2;note.placeholder="说明条件差异、分歧或澄清依据";note.setAttribute("aria-label","关联核对说明");const buttons=element("div",null,"button-row");for(const [decision,label] of relation.status==="proposed"?[["accept","确认关联"],["reject","驳回建议"]]:[["resolve","记录澄清结果"]]){const button=element("button",label,"secondary");button.type="submit";button.value=decision;buttons.append(button);}form.append(actor,note,buttons);form.addEventListener("submit",event=>{event.preventDefault();const decision=event.submitter.value;task(async()=>{await api(`/api/experience-relations/${relation.id}/reviews`,"POST",{...fields(form),decision,expected_status:relation.status});await renderRelations();notice("关联审核结果已保存。");});});card.append(form);}list.append(card);}if(!relations.length)list.append(element("p","还没有关联或分歧记录。","empty-note"));}
async function renderFeedback(){const list=$("#feedback-list");list.replaceChildren();for(const item of await api(`/api/candidates/${experienceId}/feedback?revision=${currentExperience.revision}`)){const card=element("article",null,"feedback-record");card.append(element("strong",`${item.actor} · v${item.revision}`),element("small",item.actual_used===true?" · 实际采用":item.actual_used===false?" · 未实际采用":" · 采用情况未知"),element("p",item.original),element("p",`条件：${item.conditions||"尚未说明"}`,"hint"),element("p",`做法：${item.actions||"尚未说明"}`,"hint"),element("p",`结果：${item.outcome||"尚未说明"}`,"hint"));list.append(card);}if(!list.children.length)list.append(element("p",`v${currentExperience.revision} 尚无使用反馈。`,"empty-note"));}
function evidenceChoices(target, candidate, side) {
 const list = $(target);
 list.replaceChildren(element("h3", `${candidate.title} · v${candidate.revision}`));
 for (const field of candidate.content) {
   if (field.key === "statement") continue;
   const schemaField = candidate.content_schema.find(entry => entry.key === field.key);
   const fieldName = candidate.structured ? schemaField?.label : {scope:"适用条件", rationale:"判断理由", exceptions:"例外与边界"}[field.key];
   const status = {unknown:" · 未知", hypothesis:" · 假设"}[field.status] || "";
   list.append(element("small", (fieldName || field.key) + status), element("p", field.text || "未填写"));
 }
 for (const [index, cite] of candidate.evidence.entries()) {
   const label = element("label", null, "citation-choice");
   const input = element("input");
   input.type = "checkbox";
   input.dataset.side = side;
   input.value = index;
   label.append(input, element("span", quoteText(cite)));
   list.append(label);
 }
}
$("#revision-select").addEventListener("change",event=>task(()=>openRevision(Number(event.target.value))));
$("#related-search").addEventListener("submit",event=>{event.preventDefault();task(async()=>{const list=$("#related-results");list.replaceChildren();const results=await api(`/api/knowledge?q=${encodeURIComponent(fields(event.target).q)}`);for(const candidate of results){if(candidate.id===experienceId)continue;const button=element("button",`${candidate.title} · v${candidate.revision}`,"related-choice");button.type="button";button.addEventListener("click",()=>{comparedExperience=candidate;$("#comparison").hidden=false;$("#comparison-title").textContent=`与“${candidate.title}”v${candidate.revision} 比较`;evidenceChoices("#left-evidence",currentExperience,"left");evidenceChoices("#right-evidence",candidate,"right");});list.append(button);}if(!list.children.length)list.append(element("p","没有找到其他已确认经验。可更换关键词。","empty-note"));});});
$("#relation-form").addEventListener("submit",event=>{event.preventDefault();task(async()=>{const evidence=[];for(const input of document.querySelectorAll(".citation-choice input:checked")){const side=input.dataset.side;const cite=(side==="left"?currentExperience:comparedExperience).evidence[Number(input.value)];const value={side};for(const key of ["source_type","source_id","start","end","quote","locator"])if(cite[key]!==undefined)value[key]=cite[key];evidence.push(value);}await api("/api/experience-relations","POST",{...fields(event.target),left_id:experienceId,left_revision:currentExperience.revision,right_id:comparedExperience.id,right_revision:comparedExperience.revision,evidence});await renderRelations();notice("已保存关联建议，等待人工核对。");});});
$("#ai-compare").addEventListener("click",()=>task(async()=>{const key=`experience-ledger.compare.${experienceId}:${currentExperience.revision}:${comparedExperience.id}:${comparedExperience.revision}`;let requestId;try{requestId=sessionStorage.getItem(key);}catch(_){}requestId=requestId||crypto.randomUUID();try{sessionStorage.setItem(key,requestId);}catch(_){}await api("/api/experience-relations/suggest","POST",{left_id:experienceId,left_revision:currentExperience.revision,right_id:comparedExperience.id,right_revision:comparedExperience.revision,request_id:requestId});try{sessionStorage.removeItem(key);}catch(_){}await renderRelations();notice("模型的关联建议已保存，待核对。");}));
$("#feedback-form").addEventListener("submit",event=>{event.preventDefault();task(async()=>{const value=fields(event.target);value.actual_used=value.actual_used==="unknown"?null:value.actual_used==="true";await api(`/api/candidates/${experienceId}/feedback`,"POST",{...value,revision:currentExperience.revision});event.target.reset();await renderFeedback();notice(`已保存 v${currentExperience.revision} 的反馈。`);});});
$("#withdraw-form").addEventListener("submit",event=>{event.preventDefault();task(async()=>{await api(`/api/candidates/${experienceId}/withdraw`,"POST",{...fields(event.target),expected_revision:currentExperience.latest_revision||currentExperience.revision,expected_active_revision:currentExperience.active_revision});await openRevision(currentExperience.revision);notice("已撤下默认确认版，历史继续保留。");});});
task(()=>openRevision(Number(new URLSearchParams(location.search).get("revision"))||null));
