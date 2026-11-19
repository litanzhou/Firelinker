"use strict";

const params = new URLSearchParams(location.search);
const query = (params.get("q") || "").trim();
const list = document.querySelector("#knowledge-list");
const reader = document.querySelector("#knowledge-reader");
const message = document.querySelector("#library-message");
const legacyLabels = {statement: "经验内容", scope: "适用条件", rationale: "判断理由", exceptions: "例外与边界"};
const statusLabels = {unknown: "未知", hypothesis: "待检验假设"};
const kindLabels = {conditional: "有条件的经验", perceptual: "感知线索", case_note: "个案记录"};

function make(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function link(label, url) {
  const anchor = make("a", label);
  anchor.href = url;
  return anchor;
}

function marked(text, excerpt = false) {
  const fragment = document.createDocumentFragment();
  const index = query ? text.toLowerCase().indexOf(query.toLowerCase()) : -1;
  if (index < 0) {
    fragment.append(excerpt && text.length > 80 ? text.slice(0, 80) + "…" : text);
    return fragment;
  }
  const start = excerpt ? Math.max(0, index - 22) : 0;
  const end = excerpt ? Math.min(text.length, index + query.length + 45) : text.length;
  fragment.append((start ? "…" : "") + text.slice(start, index),
    make("mark", text.slice(index, index + query.length)),
    text.slice(index + query.length, end) + (end < text.length ? "…" : ""));
  return fragment;
}

function fieldLabel(item, key) {
  return (item.structured && item.content_schema.find(field => field.key === key)?.label)
    || legacyLabels[key] || key;
}

function revisionNotice(item) {
  if (item.latest_revision <= item.revision) return "";
  const status = {pending: "待审核", rejected: "已驳回", approved: "已确认"}[item.latest_status];
  return `v${item.latest_revision} ${status || "另有修订"}`;
}

function resultButton(item) {
  const button = make("button", null, "knowledge-card");
  button.type = "button";
  button.dataset.id = item.id;
  const title = make("strong");
  title.append(marked(item.title));
  const match = query && item.content.find(field => field.text.toLowerCase().includes(query.toLowerCase()));
  const summary = make("span", null, "match-text");
  if (match && match.key !== "statement") summary.append(`${fieldLabel(item, match.key)} · `);
  summary.append(marked(match ? match.text : item.statement, true));
  button.append(make("small", `${item.domain_name} · v${item.revision}`), title, summary);
  const scope = item.content.find(field => field.key === "scope" || field.key === "boundaries");
  if (scope && scope !== match) {
    const label = fieldLabel(item, scope.key);
    const line = make("span", null, "result-scope");
    line.append(`${label} · `);
    if (statusLabels[scope.status]) line.append(`${statusLabels[scope.status]} · `);
    line.append(marked(scope.text || "未填写", true));
    button.append(line);
  }
  button.addEventListener("click", () => selectResult(item));
  return button;
}

function sourcePosition(cite) {
  const at = cite.locator || {};
  const parts = [];
  if (at.page != null) parts.push(`第 ${at.page} 页`);
  if (at.paragraph != null) parts.push(`第 ${at.paragraph + 1} 段`);
  if (at.table != null) parts.push(`表 ${at.table + 1} · 第 ${at.row + 1} 行第 ${at.column + 1} 列`);
  if (at.sheet) parts.push([at.sheet, at.range].filter(Boolean).join(" / "));
  if (at.bbox) parts.push(`区域 ${at.bbox.join(", ")}`);
  if (cite.start != null && cite.end != null) parts.push(`字符 ${cite.start}–${cite.end}`);
  return parts.join(" · ");
}

function citation(cite) {
  const block = make("figure", null, "quotation");
  if (cite.quote) block.append(make("blockquote", cite.quote));
  const caption = make("figcaption", [cite.source_title, sourcePosition(cite)].filter(Boolean).join(" · "));
  if (cite.source_type === "file") {
    caption.append(" · ", link("下载原件", `/api/files/${encodeURIComponent(cite.source_id)}/download`));
  }
  block.append(caption);
  return block;
}

function selectResult(item) {
  for (const button of list.children) button.setAttribute("aria-pressed", String(button.dataset.id === item.id));
  const url = new URL(location.href);
  url.searchParams.set("selected", item.id);
  history.replaceState(null, "", url);
  reader.replaceChildren();
  const detailUrl = `/experience/${encodeURIComponent(item.id)}?revision=${item.revision}`;
  const meta = make("div", null, "reader-meta");
  meta.append(make("span", `${item.domain_name} / ${kindLabels[item.kind] || item.kind}`),
    make("span", `已确认 v${item.revision}`, "confirmed"));
  if (revisionNotice(item)) meta.append(make("span", revisionNotice(item), "pending"));
  const heading = make("h2");
  heading.append(marked(item.title));
  const actions = make("div", null, "reader-actions");
  actions.append(link("版本、关联与使用反馈", detailUrl),
    link("查看原始案例", `/?${new URLSearchParams({case_id: item.case_id, candidate_id: item.id})}`),
    make("span", "尚未经过案例验证"));
  reader.append(meta, heading, actions);
  for (const field of item.content) {
    const section = make("section", null, "reader-field");
    const header = make("h3", fieldLabel(item, field.key));
    if (statusLabels[field.status]) header.append(make("small", statusLabels[field.status]));
    const value = make("p");
    value.append(marked(field.text || "未填写"));
    section.append(header, value);
    const sources = item.evidence.filter(cite => cite.field === field.key);
    if (sources.length) section.append(citation(sources[0]));
    if (sources.length > 1) {
      const more = make("details");
      more.append(make("summary", `其他 ${sources.length - 1} 条出处`));
      for (const cite of sources.slice(1)) more.append(citation(cite));
      section.append(more);
    }
    reader.append(section);
  }
  reader.scrollTop = 0;
}

async function readJson(path) {
  const response = await fetch(path);
  const body = await response.json();
  if (!response.ok) throw Error(body.error || "经验库读取失败");
  return body;
}

async function loadLibrary() {
  const domain = params.get("domain_id") || "";
  document.querySelector('#search-form [name="q"]').value = query;
  const [domains, items] = await Promise.all([
    readJson("/api/domains"), readJson(`/api/knowledge?${new URLSearchParams({q: query, domain_id: domain})}`)
  ]);
  const select = document.querySelector("#search-domain");
  for (const value of domains) select.add(new Option(value.name, value.id));
  if (domain && !domains.some(value => value.id === domain)) select.add(new Option(domain, domain));
  select.value = domain;
  if (!items.length) {
    const filtered = query || domain;
    message.replaceChildren();
    if (!filtered) {
      const illustration = make("img", null, "library-brand-art");
      illustration.src = "/static/firelinker-handoff.png";
      illustration.alt = "";
      illustration.width = illustration.height = 132;
      message.append(illustration);
    }
    message.append(make("strong", filtered ? "没有找到匹配的经验" : "还没有已确认的经验"),
      make("span", filtered ? "试试更短的关键词，或查看其他领域。" : "在案例工作台确认候选经验后，即可入库。"),
      filtered ? link("查看全部经验", "/knowledge") : link("前往案例工作台", "/"));
    return;
  }
  message.hidden = true;
  document.querySelector("#result-count").textContent = `${items.length} 条`;
  list.replaceChildren(...items.map(resultButton));
  document.querySelector("#workspace").hidden = false;
  selectResult(items.find(item => item.id === params.get("selected")) || items[0]);
}

loadLibrary().catch(error => { message.textContent = error.message; });
window.addEventListener("pageshow", event => { if (event.persisted) location.reload(); });
