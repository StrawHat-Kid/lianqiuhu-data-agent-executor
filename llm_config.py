"""纯问数执行器的模型服务配置解析。

模型服务不再写死 DeepSeek：
- 默认使用本机 OpenCode 实际生效的 OpenAI-compatible 服务（provider / model / base_url）；
- 连接参数（provider、base_url、model、api_key、timeout）只从 `llm` 配置段和 `LLM_*` 环境变量读取；
- 旧 `deepseek` 配置段仅保留业务相关的 `system_prompt` 等字段，连接参数不再沿用，
  避免继续消耗 DeepSeek / SiliconFlow 余额。

任何密钥都不写日志、不写入仓库。
"""
from copy import deepcopy
import os


DEFAULT_PROVIDER = "ruisi"
DEFAULT_BASE_URL = "https://llm.rsagent.net/v1"
DEFAULT_MODEL = "dh-dev"

DEFAULT_SYSTEM_PROMPT = (
    "你是一个严谨的问数助手。用户每次提问时，请把随包知识库全部文件内容视为唯一事实依据，"
    "回答必须严格依据知识库中的事实数据与口径，不允许编造，不允许改变数值、单位、时间口径和筛选条件。"
    "如果知识库没有提供相关事实，请直接说明无法回答。"
)

DEFAULT_LLM_CONFIG = {
    "provider": DEFAULT_PROVIDER,
    "base_url": DEFAULT_BASE_URL,
    "model": DEFAULT_MODEL,
    "api_key": "",
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "max_tokens": 8192,
    "stream": True,
    "timeout_seconds": 60,
    "max_retries": 2,
    "retry_delay_seconds": 2,
    # "none"：不发送厂商专有参数（OpenAI-compatible 通用）；"deepseek"：额外发送 thinking/reasoning_effort。
    "thinking_mode": "none",
}

# 旧 deepseek 段允许继承的字段：只保留与模型厂商无关的业务/请求参数。
LEGACY_INHERITED_FIELDS = (
    "system_prompt",
    "max_tokens",
    "stream",
    "max_retries",
    "retry_delay_seconds",
)

ENV_MAP = {
    "provider": "LLM_PROVIDER",
    "base_url": "LLM_BASE_URL",
    "model": "LLM_MODEL",
    "api_key": "LLM_API_KEY",
    "timeout_seconds": "LLM_TIMEOUT_SECONDS",
    "max_tokens": "LLM_MAX_TOKENS",
    "stream": "LLM_STREAM",
    "thinking_mode": "LLM_THINKING_MODE",
}

INT_FIELDS = ("timeout_seconds", "max_tokens", "max_retries")
FLOAT_FIELDS = ("retry_delay_seconds",)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return bool(value)


def _as_int(value, name):
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    return number


def _non_empty(value):
    return isinstance(value, str) and value.strip() != ""


def resolve_llm_config(config, env=None):
    """合并默认值、config.json 的 llm/deepseek 段与 LLM_* 环境变量。"""
    env = os.environ if env is None else env
    config = config or {}

    resolved = deepcopy(DEFAULT_LLM_CONFIG)
    legacy = config.get("deepseek") if isinstance(config.get("deepseek"), dict) else {}
    section = config.get("llm") if isinstance(config.get("llm"), dict) else {}

    for name in LEGACY_INHERITED_FIELDS:
        if name in legacy and legacy[name] is not None:
            resolved[name] = legacy[name]
    for name in LEGACY_INHERITED_FIELDS:
        if name in section and section[name] is not None:
            resolved[name] = section[name]

    # 连接参数只认新 llm 段，避免旧配置继续指向 DeepSeek/SiliconFlow。
    for name in ("provider", "base_url", "model", "api_key"):
        if _non_empty(section.get(name)):
            resolved[name] = section[name].strip()

    for field, env_name in ENV_MAP.items():
        value = env.get(env_name)
        if _non_empty(value):
            resolved[field] = value.strip()

    resolved["api_key_source"] = (
        "env" if _non_empty(env.get(ENV_MAP["api_key"]))
        else "config" if _non_empty(section.get("api_key"))
        else "missing"
    )
    resolved["legacy_section_ignored"] = bool(legacy)

    resolved["base_url"] = str(resolved.get("base_url", "")).rstrip("/")
    resolved["provider"] = str(resolved.get("provider") or DEFAULT_PROVIDER)
    resolved["model"] = str(resolved.get("model") or DEFAULT_MODEL)

    for name in INT_FIELDS:
        resolved[name] = _as_int(resolved.get(name), name)
        if resolved[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    for name in FLOAT_FIELDS:
        resolved[name] = float(resolved.get(name))

    resolved["stream"] = _as_bool(resolved.get("stream", True))
    thinking_mode = str(resolved.get("thinking_mode") or "none").strip().lower()
    resolved["thinking_mode"] = thinking_mode if thinking_mode in ("none", "deepseek") else "none"
    return resolved
