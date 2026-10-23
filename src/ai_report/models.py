"""Explicitly configured Chat-Completions-compatible transport; no discovery calls."""

from dataclasses import dataclass, field
import hashlib
import os
import time
from urllib.parse import urlsplit

import requests


class ModelCallError(RuntimeError):
    def __init__(self, message: str, *, http_status: int | None = None, business_code: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.business_code = business_code


@dataclass(frozen=True)
class ModelConfig:
    model: str
    base_url: str
    api_key: str = field(repr=False)
    label: str = ""
    provider: str = "custom"

    @property
    def identity(self) -> str:
        return hashlib.sha256(f"{self.model}|{self.base_url}".encode()).hexdigest()

    @property
    def display_name(self) -> str:
        return self.label or self.model


def _validated_config(fields: dict) -> tuple[ModelConfig | None, str | None]:
    required = ("model", "base_url", "api_key")
    values = {key: str(fields.get(key) or "").strip() for key in required}
    if not any(values.values()):
        return None, None
    if not all(values.values()):
        return None, "LLM配置不完整：需要模型名称、API基础地址及API Key；当前使用本地规则模板。"
    base = values["base_url"].rstrip("/")
    try:
        parts = urlsplit(base)
    except ValueError:
        return None, "LLM服务地址格式无效，当前使用本地规则模板。"
    if any(character.isspace() for character in base):
        return None, "LLM服务地址包含空白字符，当前使用本地规则模板。"
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        return None, "LLM服务地址无效：需显式HTTP(S) API基址，不允许内嵌凭证、查询参数或片段。"
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None, "远程LLM服务需使用HTTPS；HTTP仅允许本机服务。"
    if any(character in values["model"] for character in "\r\n"):
        return None, "模型名称包含无效字符。"
    return ModelConfig(values["model"], base, values["api_key"],
                       str(fields.get("label") or values["model"]).strip(),
                       str(fields.get("provider") or "custom").strip()), None


def configured_models(environ=None, secrets=None, runtime=None) -> tuple[list[ModelConfig], list[str]]:
    """Load explicit environment/Secrets and optional session-owned profiles."""
    env = os.environ if environ is None else environ
    secrets = secrets or {}
    environment = {
        "model": env.get("RESEARCH_LLM_MODEL") or secrets.get("RESEARCH_LLM_MODEL"),
        "base_url": env.get("RESEARCH_LLM_BASE_URL") or secrets.get("RESEARCH_LLM_BASE_URL"),
        "api_key": env.get("RESEARCH_LLM_API_KEY") or secrets.get("RESEARCH_LLM_API_KEY"),
        "label": "环境变量 / Streamlit Secrets",
        "provider": "environment",
    }
    candidates = [environment]
    if runtime:
        candidates.extend(runtime if isinstance(runtime, (list, tuple)) else [runtime])
    models, errors, identities = [], [], set()
    for candidate in candidates:
        config, error = _validated_config(candidate)
        if error:
            errors.append(error)
        elif config is not None and config.identity not in identities:
            models.append(config)
            identities.add(config.identity)
    return models, errors


_ERROR_GUIDANCE = {
    "1113": "账户余额或资源包不可用，请检查智谱控制台。",
    "1302": "账户达到请求频率或并发限制，请降低频率后再试。",
    "1305": "模型当前访问量过大，请稍后再试。",
    "1308": "账户达到本周期使用上限，请等待额度重置。",
    "1311": "当前套餐没有该模型权限，请核对模型和Key类型。",
    "1315": "API Key产品类型不匹配，请改用模型标准API Key。",
}


def _http_error(response, api_key: str) -> ModelCallError:
    code = None
    message = ""
    try:
        if len(response.content) > 20_000:
            raise ValueError("error body too large")
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            code = str(error.get("code") or body.get("code") or "").strip() or None
            message = str(error.get("message") or body.get("message") or "").strip()
    except (ValueError, TypeError, AttributeError):
        pass
    if api_key and message:
        message = message.replace(api_key, "[已隐藏]")
    message = " ".join(message.replace("\r", " ").replace("\n", " ").split())[:300]
    guidance = _ERROR_GUIDANCE.get(code or "")
    details = f" · 业务码 {code}" if code else ""
    if guidance:
        details += f" · {guidance}"
    elif message:
        details += f" · {message}"
    return ModelCallError(f"模型API返回HTTP {response.status_code}{details}，本次回退规则报告。",
                          http_status=response.status_code, business_code=code)


def call_model(config: ModelConfig, messages: list[dict]) -> str:
    """Exactly one POST, no retry/probe/redirect. Errors never echo keys or bodies."""
    try:
        response = requests.post(config.base_url + "/chat/completions",
                                 headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
                                 json={"model": config.model, "messages": messages,
                                       "response_format": {"type": "json_object"}, "max_tokens": 4096},
                                 timeout=(5, 25), allow_redirects=False)
        try:
            if not 200 <= response.status_code < 300:
                raise _http_error(response, config.api_key)
            if len(response.content) > 200_000:
                raise ModelCallError("模型API响应超过大小上限。")
            envelope = response.json()
        finally:
            response.close()
    except requests.Timeout as exc:
        raise ModelCallError("模型API连接或读取超时，本次回退规则报告。") from exc
    except requests.RequestException as exc:
        raise ModelCallError("模型API网络请求失败，本次回退规则报告；未记录密钥或响应正文。") from exc
    except (ValueError, TypeError) as exc:
        raise ModelCallError("模型API未返回有效JSON响应。") from exc
    try:
        choice = envelope["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
            raise ModelCallError("模型拒绝或输出未完整结束，本次回退规则报告。")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ModelCallError("模型返回空内容。")
        return content
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ModelCallError("模型API响应结构不兼容。") from exc


def call_model_with_overload_retry(config: ModelConfig, messages: list[dict], *,
                                   delays: tuple[float, ...] = (2.0, 5.0), sleeper=time.sleep) -> str:
    """Retry only provider-overload 1305; every other failure returns immediately."""
    for attempt in range(len(delays) + 1):
        try:
            return call_model(config, messages)
        except ModelCallError as exc:
            if exc.business_code != "1305" or attempt == len(delays):
                raise
            sleeper(delays[attempt])
    raise AssertionError("unreachable")
