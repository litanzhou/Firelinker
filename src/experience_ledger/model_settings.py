"""Workspace model settings and the lifetime of in-flight model connections."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .interviews.planner import ModelCapabilities, OpenAIPlanner, PlannerError, planner_from_env
from .service import LedgerError


class ConnectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["compatible", "openai", "deepseek"]
    base_url: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=300)
    api_key: SecretStr | None = None
    protocol: Literal["chat_completions", "responses"] = "chat_completions"
    input_types: list[str] = Field(default_factory=lambda: ["text"])
    context_chars: int = 60000
    max_output_tokens: int = 4000
    max_attachment_bytes: int = 25 * 1024 * 1024

    @field_validator("base_url", "model")
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("请填写有效的模型名称或 API 地址")
        return value

    @field_validator("base_url")
    @classmethod
    def check_url(cls, value):
        try:
            url = urlsplit(value)
            valid = (url.hostname and url.port != 0 and not url.username and not url.password
                     and not url.query and not url.fragment
                     and (url.scheme == "https" or
                          (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("API 地址须为 HTTPS 或本机 HTTP，且不能包含凭据、查询参数或片段")
        return value.rstrip("/")

    def connection(self):
        key = self.api_key.get_secret_value().strip() if self.api_key else ""
        if self.provider != "compatible" and not key:
            raise LedgerError("请填写 API Key。")
        if len(key) > 8192 or any(ord(char) < 32 for char in key):
            raise LedgerError("API Key 含有无效字符。")
        capabilities = ModelCapabilities(
            input_types=tuple(dict.fromkeys(["text", *self.input_types])),
            context_chars=self.context_chars, max_output_tokens=self.max_output_tokens,
            max_attachment_bytes=self.max_attachment_bytes)
        return OpenAIPlanner(self.model, self.base_url, key, provider=self.provider,
                             protocol=self.protocol, capabilities=capabilities)


@dataclass
class _Connection:
    planner: object
    values: dict
    source: str
    revision: str
    users: int = 0


def _connection(planner, source, revision=None):
    capabilities = getattr(planner, "capabilities", ModelCapabilities())
    if hasattr(planner, "describe"):
        capabilities = planner.describe().get("capabilities") or asdict(ModelCapabilities())
    elif hasattr(capabilities, "__dataclass_fields__"):
        capabilities = asdict(capabilities)
    elif hasattr(capabilities, "model_dump"):
        capabilities = capabilities.model_dump()
    values = {
        "provider": getattr(planner, "provider", "compatible"),
        "base_url": getattr(planner, "base_url", ""),
        "model": planner.model if planner.configured else "",
        "protocol": getattr(planner, "protocol", "chat_completions"),
        "api_key": getattr(planner, "_api_key", ""),
        **(capabilities or asdict(ModelCapabilities())),
    }
    values.pop("structured_actions", None)
    return _Connection(planner, values, source, revision or uuid4().hex)


def _close(planner):
    if hasattr(planner, "close"):
        planner.close()


class ModelSettings:
    def __init__(self, data_dir, planner=None):
        self.path = Path(data_dir) / "model.keys.local.json"
        self._lock = RLock()
        if planner is not None:
            self._current = _connection(planner, "injected")
        elif self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                revision = saved.pop("revision")
                settings = ConnectionSettings.model_validate(saved)
                self._current = _connection(settings.connection(), "local", revision)
            except (ValueError, KeyError, TypeError, OSError, LedgerError, PlannerError):
                raise PlannerError("本机模型配置文件无法读取，请检查 model.keys.local.json。") from None
        else:
            self._current = _connection(planner_from_env(), "environment")

    @property
    def planner(self):
        return self._current.planner

    @contextmanager
    def use(self):
        with self._lock:
            entry = self._current
            entry.users += 1
        try:
            yield entry.planner
        finally:
            with self._lock:
                entry.users -= 1
                if entry is not self._current and not entry.users:
                    _close(entry.planner)

    def override(self, planner):
        """Keep the runtime's existing injected-planner interface available."""
        with self._lock:
            self._swap(_connection(planner, "injected"))

    def describe(self):
        with self._lock:
            entry = self._current
            values = {key: value for key, value in entry.values.items() if key != "api_key"}
            return dict(values, revision=entry.revision, source=entry.source,
                        configured=entry.planner.configured,
                        has_api_key=bool(entry.values["api_key"]))

    def _prepare(self, values):
        values = dict(values)
        revision = values.pop("expected_revision", None)
        if revision != self._current.revision:
            raise LedgerError("模型配置已被另一页面更新，请刷新后再保存或测试。", 409)
        settings = ConnectionSettings.model_validate(values)
        if settings.api_key is None:
            previous = self._current.values
            same_target = (settings.provider == previous["provider"]
                           and settings.base_url == previous["base_url"].rstrip("/"))
            if previous["api_key"] and not same_target:
                raise LedgerError("API 地址或服务商已改变，请重新填写 Key，或勾选清除 Key。")
            settings.api_key = SecretStr(previous["api_key"])
        return settings

    def _swap(self, entry):
        previous = self._current
        self._current = entry
        if not previous.users:
            _close(previous.planner)

    def save(self, values):
        with self._lock:
            planner = self._prepare(values).connection()
            entry = _connection(planner, "local")
            filename = None
            try:
                # Same-directory replace keeps a failed write from replacing
                # the last valid settings. Restrict POSIX file permissions.
                with NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".model.keys.local.",
                                        suffix=".tmp", dir=self.path.parent, delete=False) as target:
                    filename = target.name
                    os.chmod(filename, 0o600)
                    json.dump(dict(entry.values, revision=entry.revision), target, ensure_ascii=False)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(filename, self.path)
            except OSError:
                _close(planner)
                raise LedgerError("模型配置保存失败，请检查工作区目录的写入权限。", 500) from None
            finally:
                if filename and Path(filename).exists():
                    Path(filename).unlink()
            self._swap(entry)
            return self.describe()

    def probe(self, values):
        with self._lock:
            planner = self._prepare(values).connection()
        try:
            return planner.probe()
        finally:
            _close(planner)
