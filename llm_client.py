"""OpenAI-compatible 对话客户端（不绑定具体厂商）。

默认对接本机 OpenCode 实际使用的服务（provider / model / base_url 由 llm_config 解析）。
- 只依赖标准 `/chat/completions` 与 SSE `text/event-stream`；
- `thinking` / `reasoning_effort` 等厂商专有参数仅在 `thinking_mode=deepseek` 时发送；
- 日志与异常不回显 API Key。

SSE 解码说明：服务端返回 `text/event-stream` 且不带 charset 时，requests 会按
Latin-1 猜测编码，直接把 UTF-8 字节解成乱码。这里取原始字节再按 UTF-8 解码。
"""
import json
import logging
import time

import requests

log = logging.getLogger("llm")


class LlmError(RuntimeError):
    pass


class LlmConfigError(LlmError):
    pass


class LlmClient:
    def __init__(self, config: dict):
        self.provider = (config.get("provider") or "openai-compatible").strip()
        self.base_url = (config.get("base_url") or "").rstrip("/")
        self.api_key = config.get("api_key") or ""
        self.model = config.get("model") or ""
        self.thinking_mode = (config.get("thinking_mode") or "none").lower()
        self.reasoning_effort = config.get("reasoning_effort") or "medium"
        self.max_tokens = int(config.get("max_tokens", 8192))
        self.stream = bool(config.get("stream", True))
        self.timeout = int(config.get("timeout_seconds", 60))
        self.max_retries = int(config.get("max_retries", 2))
        self.retry_delay = float(config.get("retry_delay_seconds", 2))
        self._session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def describe(self) -> dict:
        """可安全写日志/暴露给健康检查的元数据，不含密钥。"""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_state": "configured" if self.api_key else "missing",
            "stream": self.stream,
            "timeout_seconds": self.timeout,
            "thinking_mode": self.thinking_mode,
        }

    def chat(self, messages):
        """发送一轮对话，返回模型回复文本。"""
        if not self.configured:
            raise LlmConfigError(
                "LLM 未配置：请设置 LLM_API_KEY（或 config.json 的 llm.api_key），"
                "并确认 LLM_BASE_URL / LLM_MODEL"
            )
        if self.stream:
            return self._chat_stream(messages)
        return self._chat_once(messages)

    def _payload(self, messages):
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": self.stream,
            "max_tokens": self.max_tokens,
        }
        if self.thinking_mode == "deepseek":
            payload["thinking"] = {"type": "enabled"}
            if self.reasoning_effort:
                payload["reasoning_effort"] = self.reasoning_effort
        return payload

    def _headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.stream else "application/json",
        }

    def _error_message(self, resp):
        try:
            data = resp.json()
            err = data.get("error") or data
            text = json.dumps(err, ensure_ascii=False)
        except Exception:
            text = resp.text or ""
        # 不把响应头或密钥写进异常信息。
        return text[:500]

    def _endpoint(self):
        return f"{self.base_url}/chat/completions"

    def _retryable_error(self, attempt, exc, mode):
        if attempt < self.max_retries:
            log.warning("%s API %s请求异常（第 %s 次重试）: %s",
                        self.provider, mode, attempt + 1, exc)
            time.sleep(self.retry_delay * (attempt + 1))
            return True
        return False

    def _chat_once(self, messages):
        url = self._endpoint()
        payload = self._payload(messages)
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(url, headers=self._headers(), json=payload, timeout=self.timeout)
                if resp.status_code >= 400:
                    raise LlmError(
                        f"{self.provider} API 请求 HTTP {resp.status_code}: {self._error_message(resp)}"
                    )
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    raise LlmError(
                        f"{self.provider} API 响应缺少 choices: "
                        f"{json.dumps(data, ensure_ascii=False)[:500]}"
                    )
                return (choices[0].get("message") or {}).get("content") or ""
            except requests.exceptions.RequestException as e:
                last_err = e
                if self._retryable_error(attempt, e, "普通"):
                    continue
        raise LlmError(f"{self.provider} API 请求失败（已重试）: {last_err}")

    def _chat_stream(self, messages):
        url = self._endpoint()
        payload = self._payload(messages)
        last_err = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.post(
                    url, headers=self._headers(), json=payload, stream=True, timeout=self.timeout
                )
                if resp.status_code >= 400:
                    raise LlmError(
                        f"{self.provider} API 请求 HTTP {resp.status_code}: {self._error_message(resp)}"
                    )
                parts = []
                for raw_line in resp.iter_lines(decode_unicode=False):
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", errors="replace")
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for ch in chunk.get("choices", []) or []:
                        delta = ch.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            parts.append(content)
                return "".join(parts)
            except LlmError:
                raise
            except requests.exceptions.RequestException as e:
                last_err = e
                if self._retryable_error(attempt, e, "流式"):
                    continue
        raise LlmError(f"{self.provider} API 流式请求失败（已重试）: {last_err}")
