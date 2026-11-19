"""Explicit provider configuration; no discovery of local keys or config files."""

import base64
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from openai import APIStatusError, APITimeoutError, APIConnectionError, OpenAIError
from pydantic import ValidationError

from .contracts import InterviewAction

PROMPT = (Path(__file__).parent.parent / "prompts" / "interviewer.zh.md").read_text(encoding="utf-8")
PROMPT_VERSION = "interviewer.zh.v3:" + sha256(PROMPT.encode()).hexdigest()[:12]


class PlannerError(Exception):
    """Safe user-facing failure; provider response bodies must not be logged."""


@dataclass
class Proposal:
    action: dict
    model: str
    tokens: int = 0


@dataclass(frozen=True)
class ModelCapabilities:
    """Deployers declare capabilities; a declaration is not a successful probe."""
    structured_actions: bool = True
    input_types: tuple[str, ...] = ("text",)
    context_chars: int = 60000
    max_output_tokens: int = 4000
    max_attachment_bytes: int = 25 * 1024 * 1024

    def __post_init__(self):
        known = {"text", "txt", "md", "pdf", "doc", "docx", "xls", "xlsx", "png", "jpg", "jpeg", "webp"}
        if type(self.structured_actions) is not bool or not self.structured_actions:
            raise PlannerError("访谈规划器需要结构化动作能力；请选择支持该契约的模型，不会自动替换型号")
        if not isinstance(self.input_types, tuple) or not self.input_types or any(t not in known for t in self.input_types):
            raise PlannerError("模型输入类型无效，请显式声明 text 或所需文件扩展名")
        if type(self.context_chars) is not int or not 1000 <= self.context_chars <= 2_000_000:
            raise PlannerError("模型上下文字符预算须为1000至2000000之间的整数")
        if type(self.max_output_tokens) is not int or not 100 <= self.max_output_tokens <= 32000:
            raise PlannerError("模型输出 token 预算无效")
        if type(self.max_attachment_bytes) is not int or not 1 <= self.max_attachment_bytes <= 50 * 1024 * 1024:
            raise PlannerError("模型附件字节预算无效")


class Planner(Protocol):
    model: str
    configured: bool
    def propose(self, context: dict, feedback: str = "") -> Proposal: ...


class UnconfiguredPlanner:
    model = "未配置"
    configured = False
    capabilities = ModelCapabilities()
    last_probe = None

    def propose(self, context, feedback=""):
        raise PlannerError("尚未配置访谈模型。请在“模型设置”中保存连接配置；目标和回答已保存。")

    def probe(self):
        return {"ok": False, "status": "not_configured", "model": self.model, "tested": [], "error": "请显式配置模型型号与连接"}

    def describe(self):
        return {"configured": False, "model": self.model, "protocol": None,
                "capabilities": asdict(self.capabilities), "probe": self.last_probe}


class OpenAIPlanner:
    configured = True

    def __init__(self, model, base_url, api_key, *, timeout=45, provider="compatible", protocol="chat_completions", capabilities=None):
        if not isinstance(model, str) or not model.strip():
            raise PlannerError("必须由用户显式选择模型型号")
        if protocol not in ("chat_completions", "responses"):
            raise PlannerError("模型协议应为 chat_completions 或 responses")
        if provider == "deepseek" and protocol != "chat_completions":
            raise PlannerError("当前 DeepSeek 适配器仅实现文本 Chat Completions；其原生文件协议尚未接入")
        self.model = model
        self.provider = provider
        self.protocol = protocol
        self.base_url = base_url
        self._api_key = api_key or ""
        self.capabilities = capabilities if capabilities is not None else ModelCapabilities()
        if not isinstance(self.capabilities, ModelCapabilities):
            raise PlannerError("capabilities 必须为 ModelCapabilities")
        self.last_probe = None
        self._file_loader = None
        try:
            url = urlsplit(base_url)
            valid = (url.hostname and url.port != 0 and not url.username and not url.password
                     and not url.query and not url.fragment
                     and (url.scheme == "https" or
                          (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1"))))
        except ValueError:
            valid = False
        if not valid:
            raise PlannerError("模型地址须为 HTTPS 或本机 HTTP，且不得包含凭据、查询参数或片段")
        from openai import OpenAI
        self._client = OpenAI(base_url=base_url, api_key=api_key or "local-no-key", timeout=timeout, max_retries=0,
                              admin_api_key="", organization="", project="", webhook_secret="")
        # The SDK otherwise inherits OPENAI_CUSTOM_HEADERS even with an explicit
        # key and endpoint. Do not forward another application's account headers.
        self._client._custom_headers = {}
        self._client.organization = self._client.project = None

    def bind_file_loader(self, loader):
        self._file_loader = loader

    def describe(self):
        return {"configured": self.configured, "model": self.model, "protocol": self.protocol,
                "capabilities": asdict(self.capabilities), "probe": self.last_probe}

    def _attachments(self, context):
        # The store creates these references only after checking the interview's
        # linked cases. Never discover attachment instructions inside raw answers.
        last_tool = context.get("last_tool_result", context.get("last_tool", context.get("tool_result", {})))
        if not isinstance(last_tool, dict):
            return []
        results = last_tool.get("results", [])
        if isinstance(results, dict):
            results = [results]
        if last_tool.get("attachment"):
            results = [last_tool, *results]
        refs = [r["attachment"] for r in results if isinstance(r, dict) and isinstance(r.get("attachment"), dict)]
        if not refs:
            return []
        if self.protocol != "responses":
            raise PlannerError("当前文本 Chat Completions 适配器不发送原生附件；请使用本地解析，或显式配置 responses 协议与文件能力")
        if self._file_loader is None:
            raise PlannerError("尚未绑定受控文件读取器，不能发送原生附件")
        parts, total, seen = [], 0, set()
        for ref in refs:
            source_id = ref.get("source_id")
            if not isinstance(source_id, str) or not source_id:
                raise PlannerError("原生附件缺少固定来源 ID")
            if source_id in seen:
                continue
            seen.add(source_id)
            stored = self._file_loader(source_id)
            extension = Path(stored["filename"]).suffix.lower().lstrip(".")
            aliases = {"jpg": "jpeg"}
            declared = {aliases.get(t, t) for t in self.capabilities.input_types}
            if aliases.get(extension, extension) not in declared:
                raise PlannerError("用户尚未声明所选模型支持 " + extension.upper() + " 原生输入；原件已保留，不会自行更换模型")
            data = stored["data"]
            total += len(data)
            if total > self.capabilities.max_attachment_bytes:
                raise PlannerError("本次原生附件超过已配置的字节预算，请缩小输入范围")
            encoded = "data:" + stored["mime_type"] + ";base64," + base64.b64encode(data).decode("ascii")
            # Pair the payload with its immutable local identity for citations.
            parts.append({"type": "input_text", "text": "以下附件的固定 source_id=" + source_id + "；引用需核对原件位置，不可仅凭模型输出认定正确。"})
            if stored["mime_type"].startswith("image/"):
                parts.append({"type": "input_image", "image_url": encoded, "detail": "auto"})
            else:
                parts.append({"type": "input_file", "filename": stored["filename"], "file_data": encoded})
        return parts

    def propose(self, context, feedback=""):
        serialized = json.dumps(context, ensure_ascii=False)
        if len(serialized) + len(feedback) + len(PROMPT) > self.capabilities.context_chars:
            raise PlannerError("本次上下文超过所选模型配置的字符预算；原始记录已保存")
        attachments = self._attachments(context)
        messages = [{"role": "system", "content": PROMPT},
                    {"role": "user", "content": serialized}]
        if feedback:
            messages.append({"role": "user", "content": "程序校验反馈：" + feedback})
        try:
            schema = InterviewAction.model_json_schema()
            if self.protocol == "responses":
                inputs = [{"role": "user", "content": [{"type": "input_text", "text": serialized}, *attachments]}]
                if feedback:
                    inputs.append({"role": "user", "content": "程序校验反馈：" + feedback})
                response = self._client.responses.create(model=self.model, instructions=PROMPT, input=inputs,
                    max_output_tokens=self.capabilities.max_output_tokens, store=False,
                    tools=[{"type": "function", "name": "propose_interview_action", "description": "提出一个可校验的访谈动作",
                            "parameters": schema, "strict": False}],
                    tool_choice={"type": "function", "name": "propose_interview_action"}, parallel_tool_calls=False)
            else:
                response = self._client.chat.completions.create(
                    model=self.model, messages=messages,
                    **({"max_completion_tokens": self.capabilities.max_output_tokens} if self.provider == "openai" else {"max_tokens": self.capabilities.max_output_tokens}),
                    tools=[{"type": "function", "function": {"name": "propose_interview_action",
                           "description": "提出一个可校验的访谈动作", "parameters": schema}}],
                    tool_choice={"type": "function", "function": {"name": "propose_interview_action"}},
                )
        except APIStatusError as exc:
            descriptions = {401: "模型身份验证失败", 403: "模型访问被拒绝", 404: "模型或接口路径不存在", 429: "模型限流或额度不足"}
            raise PlannerError(descriptions.get(exc.status_code, f"模型服务返回 HTTP {exc.status_code}") + "；原始记录已保存。") from None
        except APITimeoutError:
            raise PlannerError("模型请求超时；原始记录已保存，可稍后重试。") from None
        except APIConnectionError:
            raise PlannerError("无法连接模型服务；请检查网络和模型地址。原始记录已保存。") from None
        except OpenAIError:
            raise PlannerError("模型 SDK 调用失败；请检查配置，原始记录已保存。") from None
        if self.protocol == "responses":
            if response.status != "completed":
                raise PlannerError("模型未完成动作调用（可能拒绝或输出被截断）；请重试。")
            calls = [o for o in response.output if o.type == "function_call"]
            if len(calls) != 1 or calls[0].name != "propose_interview_action":
                raise PlannerError("模型必须返回一个指定动作调用")
            arguments = calls[0].arguments
        else:
            if not response.choices or response.choices[0].finish_reason != "tool_calls":
                raise PlannerError("模型未返回完整的动作调用（可能拒绝或输出被截断）；请重试。")
            calls = response.choices[0].message.tool_calls or []
            if len(calls) != 1 or calls[0].function.name != "propose_interview_action":
                raise PlannerError("模型必须返回一个指定动作调用")
            arguments = calls[0].function.arguments
        try:
            action = json.loads(arguments)
        except (TypeError, ValueError):
            raise PlannerError("模型动作不是有效 JSON，请重试") from None
        usage = response.usage.total_tokens if response.usage else 0
        return Proposal(action=action, model=response.model or self.model, tokens=usage)

    def probe(self):
        """A real request using synthetic content, without changing interviews."""
        try:
            proposal = self.propose({"goal": "合成连接测试：请只返回 pause 动作，reason 说明测试完成。", "scope": "仅测试结构化动作接口，不进行访谈。", "gaps": []})
            InterviewAction.model_validate(proposal.action)
            result = {"ok": True, "status": "passed", "model": proposal.model, "protocol": self.protocol,
                      "tokens": proposal.tokens, "tested": ["connection", "structured_actions"],
                      "untested": ["native_file_understanding", "interview_quality"]}
        except (PlannerError, ValidationError) as exc:
            result = {"ok": False, "status": "failed", "model": self.model, "protocol": self.protocol,
                      "tested": [], "error": str(exc) if isinstance(exc, PlannerError) else "连接已返回，但动作未通过结构校验"}
        self.last_probe = result
        return result

    def close(self):
        self._client.close()


def planner_from_env():
    # Only read the deployment variables documented by this project. Never load
    # .env automatically, scan key files or fall back to another service's key.
    provider = os.environ.get("EXPERIENCE_LLM_PROVIDER", "")
    if not provider:
        return UnconfiguredPlanner()
    if provider not in ("deepseek", "openai", "compatible"):
        raise PlannerError("EXPERIENCE_LLM_PROVIDER 应为 deepseek/openai/compatible")
    urls = {"deepseek": "https://api.deepseek.com", "openai": "https://api.openai.com/v1", "compatible": ""}
    model = os.environ.get("EXPERIENCE_LLM_MODEL", "").strip()
    url = os.environ.get("EXPERIENCE_LLM_BASE_URL") or urls[provider]
    key = os.environ.get("EXPERIENCE_LLM_API_KEY", "")
    if not model or not url or (provider != "compatible" and not key):
        return UnconfiguredPlanner()
    try:
        capabilities = ModelCapabilities(
            structured_actions=os.environ.get("EXPERIENCE_LLM_STRUCTURED_ACTIONS", "true").lower() == "true",
            input_types=tuple(dict.fromkeys(["text", *[x.strip().lower().lstrip(".") for x in os.environ.get("EXPERIENCE_LLM_INPUT_TYPES", "text").split(",") if x.strip()]])),
            context_chars=int(os.environ.get("EXPERIENCE_LLM_CONTEXT_CHARS", "60000")),
            max_output_tokens=int(os.environ.get("EXPERIENCE_LLM_MAX_OUTPUT_TOKENS", "4000")),
            max_attachment_bytes=int(os.environ.get("EXPERIENCE_LLM_MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024))))
    except ValueError as exc:
        if isinstance(exc, PlannerError):
            raise
        raise PlannerError("模型能力预算配置须为有效整数") from None
    return OpenAIPlanner(model, url, key, provider=provider,
                         protocol=os.environ.get("EXPERIENCE_LLM_PROTOCOL", "chat_completions"), capabilities=capabilities)
