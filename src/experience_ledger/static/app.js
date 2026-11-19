"use strict";

const $ = (selector, base = document) => base.querySelector(selector);
const $$ = (selector, base = document) => [...base.querySelectorAll(selector)];
const state = { domains: [], cases: [], current: null, decision: null, gap: null, candidate: null, evidence: [], contexts: [], referenceCases: [], referenceSources: new Map(), contextCase: null, dirty: false };
const gapNames = { action: "做出的选择", scope: "适用条件", rationale: "判断理由", exceptions: "例外与边界", alternatives: "替代方案" };
const fieldNames = { statement: "经验内容", scope: "适用条件", rationale: "判断理由", exceptions: "例外与边界" };
const statusNames = { pending: "待审核", approved: "已确认 · 尚无案例验证", rejected: "已驳回" };

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function empty(container, text) { container.replaceChildren(node("p", text, "empty-note")); }
function message(text, error = false) {
  const target = $("#message");
  target.textContent = text; target.className = error ? "error" : ""; target.hidden = false;
}
async function api(path, method = "GET", data) {
  const options = { method, headers: { Accept: "application/json" } };
  if (data !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(data); }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `请求失败（${response.status}）`);
  return result;
}
async function run(action, owner) {
  const buttons = owner ? (owner.tagName === "BUTTON" ? [owner] : $$("button", owner)) : [];
  if (buttons.some(button => button.dataset.busy)) return;
  buttons.forEach(button => { button.dataset.busy = "1"; button.disabled = true; });
  try { await action(); } catch (error) { message(error.message || "操作失败，请重试。", true); }
  finally { buttons.forEach(button => { delete button.dataset.busy; button.disabled = false; }); updateReviewButtons(); }
}
function data(form) { return Object.fromEntries(new FormData(form)); }
function stamp(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}
function pickOption(select, value, label) { const option = node("option", label); option.value = value; select.append(option); }
function cleanEvidence(item) {
  return Object.fromEntries(["field", "source_type", "source_id", "start", "end", "quote", "locator"].filter(key=>item[key]!==undefined).map(key => [key, item[key]]));
}
function selectedCaseId() { try { return localStorage.getItem("experience-ledger.case"); } catch (_) { return null; } }
function rememberCase(id) { try { localStorage.setItem("experience-ledger.case", id); } catch (_) { /* Selection persistence is optional. */ } }
function allowLeave() { return !state.dirty || window.confirm("当前候选经验有未保存的修改。离开并放弃这些修改？"); }
function setTab(name) {
  $$(".step").forEach(button => { const active = button.dataset.tab === name; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); });
  $$(".tab-panel").forEach(panel => { panel.hidden = panel.id !== `tab-${name}`; });
}
function renderCaseList() {
  const list = $("#case-list"); list.replaceChildren();
  if (!state.cases.length) return empty(list, "暂无案例。");
  for (const item of state.cases) {
    const button = node("button", null, "case-card"); button.classList.toggle("selected", item.id === state.current?.id);
    button.append(node("strong", item.title), node("small", state.domains.find(domain => domain.id === item.domain_id)?.name || item.domain_id));
    button.addEventListener("click", () => { if (allowLeave()) run(() => openCase(item.id), button); }); list.append(button);
  }
}
async function refreshCases() { state.cases = await api("/api/cases"); renderCaseList(); }
async function openCase(id, keepSelection = false) {
  const current = await api(`/api/cases/${encodeURIComponent(id)}`);
  if (state.current?.id !== id) {
    $("#add-material").open = false;
    $("#source-form").reset();
    $("#file-form").reset();
    $("#materials-list").replaceChildren();
    setMaterialMethod("file-form");
  }
  state.current = current; rememberCase(id);
  if (!keepSelection) { state.decision = null; state.gap = null; state.candidate = null; state.dirty = false; }
  $("#empty-case").hidden = true; $("#case-detail").hidden = false; $("#new-case-details").open = false;
  $("#case-title").textContent = current.title; $("#case-context").textContent = current.context;
  const domain = current.domain || state.domains.find(item => item.id === current.domain_id);
  $("#case-domain-label").textContent = domain?.name || current.domain_id;
  renderCaseList(); await renderMaterials();
  if (state.current !== current) return;
  renderDecisions(); renderCandidates(); renderEvents();
  if (keepSelection && state.decision) openDecision(state.decision.id, state.gap?.id);
  else if (current.decisions.length) openDecision(current.decisions[0].id);
  else $("#interview-detail").hidden = true;
  if (keepSelection && state.candidate) { if (!state.dirty) await openCandidate(state.candidate.id); }
  else $("#candidate-detail").hidden = true;
}
function locatorLabel(locator) {
  return locator.kind === "slide" ? `第 ${locator.slide} 张幻灯片` : JSON.stringify(locator);
}
function fileMaterialCard(file) {
  const card = node("details", null, "source-card");
  card.dataset.materialType = "file";
  const heading = node("summary");
  heading.append(node("span", file.filename), node("span", `原件 v${file.version}`, "pill"));
  const download = node("a", "下载原件 →", "text-button");
  download.href = `/api/files/${file.id}/download`;
  const parseStatus = {ready: "可读取", not_requested: "未请求", unsupported: "当前不支持",
    unavailable: "缺少可选解析依赖", failed: "解析失败"}[file.parse_status] || file.parse_status;
  const parseDetails = file.parse_error ? ` · ${file.parse_error}` : "";
  card.append(heading, node("p", `${file.size} 字节 · SHA-256 ${file.sha256}`, "source-meta"),
    download, node("p", `本地解析：${parseStatus}${parseDetails}`, "hint"));
  const parse = node("button", "提取或重新提取可定位片段", "secondary");
  parse.type = "button";
  parse.addEventListener("click", () => run(async () => {
    await api(`/api/files/${file.id}/parse`, "POST", {});
    if (state.current.id === file.case_id) await renderMaterials();
  }, parse));
  card.append(parse);
  if (file.parse_status === "ready") {
    const preview = node("button", "查看提取片段", "text-button");
    preview.type = "button";
    preview.addEventListener("click", () => run(async () => {
      const metadata = await api(`/api/files/${file.id}`);
      const text = metadata.anchors.map(anchor => `${locatorLabel(anchor.locator)}\n${anchor.text || "此处没有可提取的文字，请查看原件。"}`).join("\n\n");
      card.append(node("pre", text || "此文件没有可提取的文字，请查看原件。"));
      preview.remove();
    }, preview));
    card.append(preview);
  }
  return card;
}
function textMaterialCard(source) {
  const card = node("details", null, "source-card");
  card.dataset.materialType = "text";
  const heading = node("summary");
  heading.append(node("span", source.title), node("span", `文字 v${source.version}`, "pill"));
  card.append(heading, node("div", `${stamp(source.created_at)} · SHA-256 ${source.sha256}`, "source-meta"), node("pre", source.text));
  return card;
}
async function renderMaterials() {
  const current = state.current;
  const files = await api(`/api/cases/${encodeURIComponent(current.id)}/files`);
  if (state.current !== current) return;
  const list = $("#materials-list");
  const items = [
    ...files.map(file => ({createdAt: file.created_at, card: fileMaterialCard(file)})),
    ...current.sources.map(source => ({createdAt: source.created_at, card: textMaterialCard(source)})),
  ];
  items.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  list.replaceChildren(...items.map(item => item.card));
  if (!items.length) empty(list, "暂无资料。可以上传文件，也可以记录一段文字。");
  $("#case-counts").textContent = `${items.length} 个资料版本 · ${current.turns.length} 轮记录`;

  const fileParent = $("#file-parent");
  const sourceParent = $("#source-parent");
  const selectedFile = fileParent.value;
  const selectedSource = sourceParent.value;
  fileParent.replaceChildren();
  sourceParent.replaceChildren();
  pickOption(fileParent, "", "作为新文件");
  pickOption(sourceParent, "", "作为新资料");
  const logicalFiles = new Map(files.map(file => [file.file_id, file.filename]));
  const logicalSources = new Map(current.sources.map(source => [source.source_id, source.title]));
  for (const [id, name] of logicalFiles) pickOption(fileParent, id, `${name} · 增加版本`);
  for (const [id, title] of logicalSources) pickOption(sourceParent, id, `${title} · 增加版本`);
  if (logicalFiles.has(selectedFile)) fileParent.value = selectedFile;
  if (logicalSources.has(selectedSource)) sourceParent.value = selectedSource;
}
function renderDecisions() {
  const list = $("#decision-list"); list.replaceChildren();
  for (const item of state.current.decisions) {
    const button = node("button", null, "decision-button"); button.dataset.id = item.id;
    const gaps = state.current.gaps.filter(gap => gap.decision_id === item.id);
    button.append(node("strong", item.title), node("small", `${gaps.filter(gap => gap.status === "answered").length} / ${gaps.length} 项已有回答`));
    button.addEventListener("click", () => openDecision(item.id)); list.append(button);
  }
  if (!state.current.decisions.length) empty(list, "先添加一个需要解释的判断，例如一次选择、调整或诊断。");
}
function openDecision(id, gapId) {
  state.decision = state.current.decisions.find(item => item.id === id);
  if (!state.decision) { $("#interview-detail").hidden = true; return; }
  $("#interview-detail").hidden = false; $("#decision-title").textContent = state.decision.title; $("#decision-description").textContent = state.decision.description;
  $$(".decision-button").forEach(button => button.classList.toggle("selected", button.dataset.id === id));
  const gaps = state.current.gaps.filter(gap => gap.decision_id === id);
  const list = $("#gap-list"); list.replaceChildren();
  for (const gap of gaps) {
    const button = node("button", null, "gap-button"); button.dataset.id = gap.id;
    button.append(node("span", gapNames[gap.kind] || gap.kind), node("small", gap.status === "answered" ? "已记录回答 · 可继续追问" : "尚未回答"));
    button.addEventListener("click", () => openGap(gap.id)); list.append(button);
  }
  const selected = gaps.find(gap => gap.id === gapId) || gaps.find(gap => gap.status === "open") || gaps[0];
  if (selected) openGap(selected.id);
  const history = $("#turn-list"); history.replaceChildren();
  const turns = state.current.turns.filter(turn => turn.decision_id === id);
  turns.forEach((turn, index) => {
    const card = node("article", null, "record-card");
    card.append(node("div", `第 ${index + 1} 轮 · ${turn.speaker} · ${gapNames[turn.kind] || turn.kind} · ${stamp(turn.created_at)}`, "record-meta"), node("p", turn.question, "question"), node("p", turn.answer, "answer")); history.append(card);
  });
  if (!turns.length) empty(history, "暂无问答记录。");
}
function openGap(id) {
  state.gap = state.current.gaps.find(gap => gap.id === id);
  const form = $("#turn-form"); form.elements.gap_id.value = id; form.elements.question.value = state.gap.question; form.elements.answer.value = "";
  $$(".gap-button").forEach(button => button.classList.toggle("selected", button.dataset.id === id));
}
function renderCandidates() {
  const list = $("#candidate-list"); list.replaceChildren();
  for (const item of state.current.candidates) {
    const button = node("button", null, "candidate-button"); button.dataset.id = item.id;
    button.append(node("strong", item.title), node("small", `修订 ${item.revision} · ${statusNames[item.status] || item.status}`));
    button.addEventListener("click", () => { if (allowLeave()) run(() => openCandidate(item.id), button); }); list.append(button);
  }
  if (!state.current.candidates.length) empty(list, "记录访谈后，在“判断与访谈”中整理候选经验。");
}
async function openCandidate(id) {
  $("#candidate-detail").hidden = true;
  state.candidate = await api(`/api/candidates/${encodeURIComponent(id)}`);
  state.evidence = state.candidate.evidence.map(item => ({ ...item })); state.dirty = false;
  $("#candidate-heading").textContent = `修订 ${state.candidate.revision}`;
  $("#candidate-status").textContent = statusNames[state.candidate.status] || state.candidate.status;
  $("#candidate-version-note").textContent = state.candidate.active_revision
    ? `经验库显示 v${state.candidate.active_revision}。` : "经验库暂无已发布版本。";
  $("#experience-detail-link").href=`/experience/${encodeURIComponent(id)}`;
  state.contexts=(state.candidate.contexts||[{case_id:state.current.id,decision_id:state.candidate.decision_id}]).map(v=>({case_id:v.case_id,decision_id:v.decision_id||null}));
  await refreshReferenceCases();renderContexts();const caseSelect=$("#context-case");caseSelect.replaceChildren();for(const item of state.cases)pickOption(caseSelect,item.id,item.title);await loadContextCase();
  const form = $("#candidate-form");
  for (const key of ["title", "kind", "statement", "scope", "rationale", "exceptions"]) form.elements[key].value = state.candidate[key] || "";
  const contentBox = $("#dynamic-content"); contentBox.replaceChildren(); contentBox.hidden = !state.candidate.structured;
  for (const key of ["statement", "scope", "rationale", "exceptions"]) form.elements[key].closest("label").hidden = state.candidate.structured;
  const fieldSelect = $("#evidence-field"); fieldSelect.replaceChildren();
  const schema = state.candidate.content_schema;
  for (const field of schema) {
    fieldNames[field.key] = field.label; pickOption(fieldSelect, field.key, field.label);
    if (state.candidate.structured) {
      const value = state.candidate.content.find(v => v.key === field.key) || { text: "", status: "unknown" };
      const row = node("div", null, "content-editor"); row.dataset.key = field.key;
      const label = node("label", field.label); const text = node("textarea"); text.name = `content.${field.key}.text`; text.rows = 3; text.value = value.text; label.append(text);
      const statusLabel = node("label", "内容性质"); const select = node("select"); select.name = `content.${field.key}.status`;
      for (const [status, title] of Object.entries({ supported: "已有原文依据", unknown: "尚未说明 / 未知", hypothesis: "待检验假设" })) pickOption(select, status, title);
      select.value = value.status; statusLabel.append(select); row.append(label, statusLabel); contentBox.append(row);
    }
  }
  form.elements.actor.value = "";
  $$(".candidate-button").forEach(button => button.classList.toggle("selected", button.dataset.id === id));
  renderEvidence(); await renderEvidenceSources(); renderHistory(); updateReviewButtons();
  $("#candidate-detail").hidden = false;
}
async function refreshReferenceCases(){state.referenceCases=await Promise.all([...new Set(state.contexts.map(v=>v.case_id))].map(id=>id===state.current.id?state.current:api(`/api/cases/${id}`)));}
function renderContexts(){const list=$("#revision-contexts");list.replaceChildren();for(const [index,context] of state.contexts.entries()){const record=node("div",null,"evidence-row");const item=state.referenceCases.find(c=>c.id===context.case_id);const label=`${item?.title||context.case_id} · ${context.decision_id?item?.decisions.find(d=>d.id===context.decision_id)?.title||context.decision_id:"整个案例"}`;const remove=node("button","移除","text-button");remove.type="button";remove.addEventListener("click",()=>run(async()=>{state.contexts.splice(index,1);state.dirty=true;await refreshReferenceCases();renderContexts();await renderEvidenceSources();updateReviewButtons();},remove));record.append(node("span",label),document.createTextNode(" "),remove);list.append(record);}}
async function loadContextCase(){const id=$("#context-case").value;if(!id)return;state.contextCase=await api(`/api/cases/${id}`);const select=$("#context-decision");select.replaceChildren();pickOption(select,"","整个案例");for(const item of state.contextCase.decisions)pickOption(select,item.id,item.title);}
function evidenceSource(item) {
  if(item.source_type==="file")return {title:item.source_title||"文件原件",text:item.source_text||"未提取到原文，请下载原件核对引文。"};
  if(item.source_type==="feedback")return {title:item.source_title||"使用反馈",text:item.source_text||item.quote||""};
  if (item.source_type === "document") {
    const source = state.referenceCases.flatMap(c=>c.sources).find(source => source.id === item.source_id);
    return { title: source ? `${source.title} · 版本 ${source.version}` : item.source_title, text: source?.text ?? item.source_text };
  }
  const allTurns=state.referenceCases.flatMap(c=>c.turns);const index = allTurns.findIndex(turn => turn.id === item.source_id);
  const turn = allTurns[index];
  return { title: turn ? `访谈记录 ${index + 1} · ${turn.speaker} · ${gapNames[turn.kind] || turn.kind}` : item.source_title, text: turn?.answer ?? item.source_text };
}
function renderEvidence() {
  const list = $("#evidence-list"); list.replaceChildren();
  state.evidence.forEach((item, index) => {
    const source = evidenceSource(item); const row = node("article", null, "evidence-row");
    const header = node("header"); const remove = node("button", "移除", "text-button"); remove.type = "button";
    remove.addEventListener("click", () => { state.evidence.splice(index, 1); state.dirty = true; renderEvidence(); updateReviewButtons(); });
    const slide = item.locator?.kind === "slide" ? ` · ${locatorLabel(item.locator)}` : "";
    header.append(node("span", `${fieldNames[item.field]} ← ${source.title || item.source_id}${slide}`), remove);
    const detail = node("details"); detail.append(node("summary", item.start==null?"查看原件位置与核对信息":`查看来源原文 · 字符 ${item.start}–${item.end}`), node("pre", source.text || ""));
    row.append(header, node("blockquote", item.quote), detail);
    if(item.source_type==="file"){
      row.append(node("p",{pending_manual:"引用位置或文字尚待人工对照原件核对",position_checked:"位置已核对；表述含义仍需人工审核",human_checked:"已人工对照原件确认位置与引文"}[item.locator?.verification]||"请核对原件位置与引文","hint"));
      if(item.locator?.verification==="pending_manual"){
        const check=node("details");check.append(node("summary","对照原件确认位置与引文"));const download=node("a","下载原件核对 →","text-button");download.href=`/api/files/${item.source_id}/download`;check.append(download);
        const locationInputs={};for(const [key,label] of Object.entries({page:"页码",slide:"幻灯片页码",paragraph:"段落序号（从0开始）",sheet:"工作表",range:"单元格范围",bbox:"区域：左、上、宽、高"})){if(item.locator[key]===undefined)continue;const wrapper=node("label",label);const input=node("input");input.value=Array.isArray(item.locator[key])?item.locator[key].join(","):item.locator[key];wrapper.append(input);locationInputs[key]=input;check.append(wrapper);}
        const quoteLabel=node("label","对照原件核对或修正引文");const checkedQuote=node("textarea");checkedQuote.rows=3;checkedQuote.value=item.quote||"";quoteLabel.append(checkedQuote);check.append(quoteLabel);
        const actorLabel=node("label","核对人");const actor=node("input");actorLabel.append(actor);const noteLabel=node("label","核对说明");const note=node("textarea");note.rows=2;note.placeholder="说明已核对原件中的位置与文字；若不一致请先修正引用";noteLabel.append(note);const confirm=node("button","保存原件核对记录","secondary");confirm.type="button";confirm.addEventListener("click",()=>run(async()=>{if(!actor.value.trim()||!note.value.trim())throw Error("请填写核对人及说明。");const locator={...item.locator};for(const [key,input]of Object.entries(locationInputs))locator[key]=key==="bbox"?input.value.split(",").map(Number):["page","slide","paragraph"].includes(key)?Number(input.value):input.value;const quoteChanged=checkedQuote.value!==(item.quote||"");const result=await api(`/api/files/${item.source_id}/reference-checks`,"POST",{locator,quote:checkedQuote.value,start:quoteChanged?null:item.start??null,end:quoteChanged?null:item.end??null,actor:actor.value,note:note.value});state.evidence[index]={...item,...result,field:item.field};state.dirty=true;renderEvidence();updateReviewButtons();message("原件核对已记录，请保存为新修订后审核。");},confirm));check.append(actorLabel,noteLabel,confirm);row.append(check);
      }
    }
    if (item.hypothesis) row.append(node("p", `本轮包含系统假设：${item.hypothesis}，请核对经验归属。`, "hint"));
    list.append(row);
  });
  if (!state.evidence.length) empty(list, "暂无引文。审核前，请为有具体内容的字段补充出处。");
}
async function renderEvidenceSources() {
  const select = $("#evidence-source"); select.replaceChildren();
  state.referenceSources=new Map();
  for(const record of state.referenceCases){
    record.sources.forEach(source=>{const key=`document:${source.id}`;state.referenceSources.set(key,{source_type:"document",source_id:source.id,text:source.text});pickOption(select,key,`${record.title} · ${source.title} v${source.version}`);});
    record.turns.forEach(turn=>{const key=`turn:${turn.id}`;state.referenceSources.set(key,{source_type:"turn",source_id:turn.id,text:turn.answer});pickOption(select,key,`${record.title} · ${turn.speaker}访谈原话`);});
    for(const file of await api(`/api/cases/${record.id}/files`)){if(file.parse_status!=="ready")continue;const metadata=await api(`/api/files/${file.id}`);for(const [index,anchor] of metadata.anchors.entries()){const key=`file:${file.id}:${index}`;state.referenceSources.set(key,{source_type:"file",source_id:file.id,text:anchor.text,width:anchor.width,height:anchor.height,locator:{...anchor.locator,parse_id:metadata.parse_id}});pickOption(select,key,`${file.filename} v${file.version} · ${locatorLabel(anchor.locator)}`);}}
  }
  for(const feedback of state.candidate.feedback||[]){const key=`feedback:${feedback.id}`;state.referenceSources.set(key,{source_type:"feedback",source_id:feedback.id,text:feedback.original});pickOption(select,key,`v${feedback.revision}反馈 · ${feedback.actor}`);}
  previewEvidenceSource();
}
function previewEvidenceSource() {
  const source=state.referenceSources.get($("#evidence-source").value);$("#evidence-source-preview").textContent = source?.text || "此来源没有提取文本；图片区域请在原件上核对。";
  const visual=source?.locator&&["image","pdf"].includes(source.locator.kind);$("#evidence-region-label").hidden=!visual;$("#evidence-region").value=visual&&source.width&&source.height?`0,0,${source.width},${source.height}`:"";
  $("#evidence-image").hidden=source?.locator?.kind!=="image";if(source?.locator?.kind==="image")$("#evidence-image").src=`/api/files/${source.source_id}/preview`;
  $("#evidence-quote").value = "";
}
function renderHistory() {
  const list = $("#revision-history"); list.replaceChildren();
  for (const revision of state.candidate.revisions || []) {
    const card = node("details", null, "record-card");
    const actor = revision.actor?.startsWith("system:") ? "依据访谈原文整理" : revision.actor || "依据访谈整理";
    card.append(node("summary", `修订 ${revision.revision} · ${actor} · ${stamp(revision.created_at)}`));
    if (revision.structured) {
      for (const value of revision.content) card.append(node("p", `${revision.content_schema.find(f => f.key === value.key)?.label || value.key}：${value.text}（${value.status === "unknown" ? "未知" : value.status === "hypothesis" ? "假设" : "有原文依据"}）`));
    } else for (const key of ["statement", "scope", "rationale", "exceptions"]) card.append(node("p", `${fieldNames[key]}：${revision[key] || "（未填写）"}`));
    for (const citation of revision.evidence || []) {
      const source = evidenceSource(citation);
      const evidence = node("details", null, "evidence-row");
      evidence.append(node("summary", `${fieldNames[citation.field]}的来源 · ${source.title || citation.source_id}`), node("blockquote", citation.quote), node("pre", source.text || ""));
      card.append(evidence);
    }
    list.append(card);
  }
  for (const review of state.candidate.reviews || []) {
    const card = node("article", null, "record-card");
    card.append(node("strong", `修订 ${review.revision} · ${review.decision === "approve" ? "确认" : "驳回"} · ${review.reviewer}`), node("p", review.note), node("small", stamp(review.created_at))); list.append(card);
  }
}
function updateReviewButtons() {
  const disabled = !state.candidate || state.candidate.status !== "pending" || state.dirty;
  for (const button of [$("#approve"), $("#reject")]) if (!button.dataset.busy) button.disabled = disabled;
  $("#review-state").textContent = state.dirty ? "请先保存当前修改，再审核新修订。" : state.candidate?.status === "approved" ? "此版本已通过审核。" : state.candidate?.status === "rejected" ? "此修订已驳回。修改并保存新修订后可再次审核。" : "填写审核人和审核意见后提交。";
}
function renderEvents() {
  const list = $("#event-list"); list.replaceChildren();
  const names = { "case.created": "创建案例", "source.added": "加入资料版本", "decision.created": "添加判断", "turn.recorded": "保存访谈", "candidate.drafted": "整理候选经验", "candidate.revised": "修订候选经验", "candidate.reviewed": "审核候选经验" };
  for (const event of state.current.events || []) {
    const type = event.type || event.event_type || event.kind;
    list.append(node("div", `${stamp(event.created_at)} · ${names[type] || type || "记录变更"} · ${event.payload?.actor || event.payload?.reviewer || ""}`, "event-row"));
  }
}

$("#new-case").addEventListener("click", () => { $("#new-case-details").open = true; $("#case-form").elements.title.focus(); });
$("#case-domain").addEventListener("change", () => { $("#domain-description").textContent = state.domains.find(domain => domain.id === $("#case-domain").value)?.description || ""; });
$("#case-form").addEventListener("submit", event => {
  event.preventDefault(); if (!allowLeave()) return;
  run(async () => { const created = await api("/api/cases", "POST", data(event.target)); event.target.reset(); await refreshCases(); await openCase(created.id); setTab("materials"); message("案例已创建。"); }, event.target);
});
function setMaterialMethod(formId) {
  for (const button of $$("#material-methods button")) {
    const controls = button.getAttribute("aria-controls");
    button.setAttribute("aria-pressed", String(controls === formId));
    document.getElementById(controls).hidden = controls !== formId;
  }
}
for (const button of $$("#material-methods button")) {
  button.addEventListener("click", () => setMaterialMethod(button.getAttribute("aria-controls")));
}
$("#source-form").addEventListener("submit", event => {
  event.preventDefault();
  const caseId = state.current.id;
  run(async () => {
    const values = data(event.target);
    if (!values.source_id) delete values.source_id;
    await api(`/api/cases/${caseId}/sources`, "POST", values);
    if (state.current.id === caseId) {
      event.target.reset();
      await openCase(caseId, true);
    }
    message("文字资料已保存。");
  }, event.target);
});
$("#file-form").addEventListener("submit", event => {
  event.preventDefault();
  const caseId = state.current.id;
  run(async () => {
    const values = new FormData(event.target);
    if (!values.get("file_id")) values.delete("file_id");
    const response = await fetch(`/api/cases/${caseId}/files`, {method: "POST", body: values});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "上传失败");
    if (state.current.id === caseId) {
      event.target.reset();
      await renderMaterials();
    }
    message(result.parse_status === "ready" ? "文件原件与提取片段已保存。" : "文件原件已保存。可在资料列表查看本地解析状态。");
  }, event.target);
});
$("#context-case").addEventListener("change",()=>run(loadContextCase));
$("#context-form").addEventListener("submit",event=>{event.preventDefault();run(async()=>{const value=data(event.target);value.decision_id=value.decision_id||null;if(!state.contexts.some(c=>c.case_id===value.case_id&&c.decision_id===value.decision_id))state.contexts.push(value);state.dirty=true;await refreshReferenceCases();renderContexts();await renderEvidenceSources();updateReviewButtons();},event.target);});
$("#decision-form").addEventListener("submit", event => {
  event.preventDefault(); run(async () => { const created = await api(`/api/cases/${state.current.id}/decisions`, "POST", data(event.target)); event.target.reset(); event.target.parentElement.open = false; await openCase(state.current.id, true); openDecision(created.id); message("判断已保存。"); }, event.target);
});
$("#turn-form").addEventListener("submit", event => {
  event.preventDefault(); run(async () => { await api(`/api/cases/${state.current.id}/turns`, "POST", data(event.target)); await openCase(state.current.id, true); message("本轮问答已保存。"); }, event.target);
});
$("#draft-candidate").addEventListener("click", event => { if (!allowLeave()) return; run(async () => {
  const candidate = await api(`/api/cases/${state.current.id}/candidates`, "POST", { decision_id: state.decision.id });
  await openCase(state.current.id, true); await openCandidate(candidate.id); setTab("review"); message("候选经验已保存，待审核。");
}, event.target); });
$("#candidate-form").addEventListener("input", event => { if (event.target.name && event.target.name !== "actor") { state.dirty = true; updateReviewButtons(); } });
$("#candidate-form").addEventListener("submit", event => {
  event.preventDefault(); run(async () => {
    const values = data(event.target); if (!values.actor.trim()) throw new Error("请填写修订人。");
    values.expected_revision = state.candidate.revision; values.evidence = state.evidence.map(cleanEvidence);values.contexts=state.contexts;
    let payload = values; let suffix = "revisions";
    if (state.candidate.structured) {
      suffix = "content";
      payload = { title: values.title, kind: values.kind, actor: values.actor, expected_revision: values.expected_revision, evidence: values.evidence,contexts:values.contexts,
        content: $$(".content-editor", $("#dynamic-content")).map(row => ({ key: row.dataset.key, text: $("textarea", row).value, status: $("select", row).value })) };
    }
    await api(`/api/candidates/${state.candidate.id}/${suffix}`, "POST", payload); state.dirty = false;
    await openCase(state.current.id, true); message("新修订已保存，等待人工审核。");
  }, event.target);
});
$("#evidence-source").addEventListener("change", previewEvidenceSource);
$("#add-evidence").addEventListener("click", event => run(async () => {
  const source = state.referenceSources.get($("#evidence-source").value);const quote = $("#evidence-quote").value;if(!source)throw Error("请选择来源。");const {source_type,source_id}=source;
  if(source.locator&&["image","pdf"].includes(source.locator.kind)&&!quote.trim()){
    const bbox=$("#evidence-region").value.split(",").map(Number);if(bbox.length!==4||bbox.some(n=>!Number.isFinite(n)))throw Error("请填写有效引用区域。");
    state.evidence.push({field:$("#evidence-field").value,source_type,source_id,start:null,end:null,quote:"",locator:{...source.locator,bbox}});state.dirty=true;renderEvidence();updateReviewButtons();message("区域引用已加入；请核对图片含义后保存新修订。");return;
  }
  if (!quote.trim() || !source.text) throw new Error("请选择来源并填写逐字引文。");
  const index = source.text.indexOf(quote); if (index < 0) throw new Error("引文与来源原文不一致，请从原文复制。");
  const start = Array.from(source.text.slice(0, index)).length;
  state.evidence.push({ field: $("#evidence-field").value, source_type, source_id, start, end: start + Array.from(quote).length, quote,...(source.locator?{locator:source.locator}:{}) });
  state.dirty = true; renderEvidence(); updateReviewButtons(); $("#evidence-quote").value = ""; message("引文已加入本次编辑，请保存为新修订。");
}, event.target));
$("#review-form").addEventListener("submit", event => {
  event.preventDefault(); const decision = event.submitter?.value;
  run(async () => {
    if (!decision || state.dirty) throw new Error("请先保存候选经验，再选择审核决定。");
    const values = data(event.target); values.expected_revision = state.candidate.revision; values.decision = decision;
    await api(`/api/candidates/${state.candidate.id}/reviews`, "POST", values); event.target.reset(); await openCase(state.current.id, true);
    message(decision === "approve" ? "此版本已入库。" : "此版本已驳回。");
  }, event.target);
});
$$(".step").forEach(button => button.addEventListener("click", () => setTab(button.dataset.tab)));
window.addEventListener("beforeunload", event => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });

run(async () => {
  state.domains = await api("/api/domains");
  for (const domain of state.domains) pickOption($("#case-domain"), domain.id, domain.name);
  $("#case-domain").dispatchEvent(new Event("change")); await refreshCases();
  const params = new URLSearchParams(location.search);
  const remembered = params.get("case_id") || selectedCaseId(); const selected = state.cases.find(item => item.id === remembered) || state.cases[0];
  if (selected) { await openCase(selected.id); if (params.get("candidate_id")) { await openCandidate(params.get("candidate_id")); setTab("review"); } }
});
