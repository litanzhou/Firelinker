"use strict";

const form = document.querySelector("#connection-form");
const fields = document.querySelector("#connection-fields");
const message = document.querySelector("#settings-message");
const key = document.querySelector("#api-key");
const clearKey = document.querySelector("#clear-key");
const defaults = {openai: "https://api.openai.com/v1", deepseek: "https://api.deepseek.com", compatible: ""};
let saved;

async function api(path, method = "GET", value) {
  const options = {method, headers: {Accept: "application/json"}};
  if (value !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(value);
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || "模型配置读取失败。");
  return data;
}

function notice(text, error = false) {
  message.textContent = text;
  message.classList.toggle("error", error);
  message.hidden = false;
}

function keyHint() {
  const sameTarget = saved && form.elements.provider.value === saved.provider
    && form.elements.base_url.value.trim().replace(/\/+$/, "") === saved.base_url.replace(/\/+$/, "");
  const keepKey = saved?.has_api_key && sameTarget;
  key.disabled = clearKey.checked;
  key.required = !clearKey.checked && !keepKey && form.elements.provider.value !== "compatible";
  key.placeholder = keepKey ? "已设置，留空保留" : "填写 API Key";
  document.querySelector("#key-hint").textContent = keepKey
    ? "留空保留当前 Key，填写后替换。"
    : saved?.has_api_key ? "地址或服务商已改变，请重新填写 Key，或勾选清除 Key。" : "填写服务商提供的 Key。无需认证的自定义接口可留空。";
}

function render(values) {
  saved = values;
  for (const name of ["provider", "base_url", "model", "protocol", "context_chars", "max_output_tokens"]) {
    form.elements[name].value = values[name];
  }
  form.elements.input_types.value = values.input_types.join(",");
  form.elements.attachment_mb.value = values.max_attachment_bytes / (1024 * 1024);
  key.value = "";
  clearKey.checked = false;
  document.querySelector("#clear-key-label").hidden = !values.has_api_key;
  const source = values.source === "local" ? "本机已保存" : "启动配置";
  document.querySelector("#connection-state").textContent = values.configured ? source : "尚未配置";
  keyHint();
}

function payload() {
  return {
    expected_revision: saved.revision,
    provider: form.elements.provider.value,
    base_url: form.elements.base_url.value.trim(),
    model: form.elements.model.value.trim(),
    api_key: clearKey.checked ? "" : key.value.trim() || null,
    protocol: form.elements.protocol.value,
    input_types: form.elements.input_types.value.split(",").map(value => value.trim().toLowerCase()).filter(Boolean),
    context_chars: Number(form.elements.context_chars.value),
    max_output_tokens: Number(form.elements.max_output_tokens.value),
    max_attachment_bytes: Math.round(Number(form.elements.attachment_mb.value) * 1024 * 1024)
  };
}

async function apply(testOnly) {
  if (!form.reportValidity()) return;
  const values = payload();
  fields.disabled = true;
  notice(testOnly ? "正在测试连接…" : "正在保存…");
  try {
    if (testOnly) {
      const result = await api("/api/model-settings/probe", "POST", values);
      notice(result.ok ? "连接成功，模型已返回访谈指令。当前填写内容尚未保存。" : result.error || "连接测试失败。", !result.ok);
    } else {
      render(await api("/api/model-settings", "PUT", values));
      notice("配置已保存。新请求将使用此模型。");
    }
  } catch (error) {
    notice(error.message, true);
  } finally {
    fields.disabled = false;
    keyHint();
  }
}

form.addEventListener("submit", event => { event.preventDefault(); apply(false); });
document.querySelector("#test-connection").addEventListener("click", () => apply(true));
form.elements.provider.addEventListener("change", () => {
  const url = form.elements.base_url;
  if (!url.value || Object.values(defaults).includes(url.value)) url.value = defaults[form.elements.provider.value];
  keyHint();
});
form.elements.base_url.addEventListener("input", keyHint);
clearKey.addEventListener("change", keyHint);
api("/api/model-settings").then(values => { render(values); fields.disabled = false; })
  .catch(error => notice(error.message, true));
