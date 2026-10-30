"""模型服务切换回归测试（不调用真实外部 API）。

覆盖：
- 默认 provider / base_url / model 与本机 OpenCode 一致；
- LLM_* 环境变量优先于 config.json；
- 旧 deepseek 段只继承 system_prompt，不再提供连接参数；
- OpenAI-compatible /chat/completions 请求结构、SSE UTF-8 解析；
- API Key 缺失、HTTP 错误时的清晰异常且不回显密钥。

运行：python -m unittest discover -s test -p "test_*.py" -v
"""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llm_client import LlmClient, LlmConfigError, LlmError
from llm_config import resolve_llm_config


class MockLlmServer:
    def __init__(self, mode="json", status=200):
        self.requests = []
        self.mode = mode
        self.status = status
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                collector.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if collector.status >= 400:
                    self.send_response(collector.status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": "mock failure"}}).encode("utf-8"))
                    return
                if collector.mode == "stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for text in ("你好", "，园区"):
                        chunk = {"choices": [{"delta": {"content": text}}]}
                        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                payload = {"choices": [{"message": {"role": "assistant", "content": "mock answer"}}]}
                self.wfile.write(json.dumps(payload).encode("utf-8"))

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LlmConfigTest(unittest.TestCase):
    def test_defaults_match_opencode_provider(self):
        config = resolve_llm_config({}, {})
        self.assertEqual(config["provider"], "ruisi")
        self.assertEqual(config["base_url"], "https://llm.rsagent.net/v1")
        self.assertEqual(config["model"], "dh-dev")
        self.assertEqual(config["timeout_seconds"], 60)
        self.assertEqual(config["api_key_source"], "missing")

    def test_env_overrides_config_and_strips_trailing_slash(self):
        config = resolve_llm_config(
            {"llm": {"provider": "cfg", "base_url": "https://cfg.example/v1/", "model": "cfg-model", "api_key": "cfg-key"}},
            {
                "LLM_PROVIDER": "env-provider",
                "LLM_BASE_URL": "https://env.example/v1/",
                "LLM_MODEL": "env-model",
                "LLM_API_KEY": "env-key",
                "LLM_TIMEOUT_SECONDS": "90",
                "LLM_STREAM": "false",
            },
        )
        self.assertEqual(config["provider"], "env-provider")
        self.assertEqual(config["base_url"], "https://env.example/v1")
        self.assertEqual(config["model"], "env-model")
        self.assertEqual(config["timeout_seconds"], 90)
        self.assertFalse(config["stream"])
        self.assertEqual(config["api_key_source"], "env")

    def test_legacy_deepseek_section_only_inherits_business_fields(self):
        config = resolve_llm_config(
            {
                "deepseek": {
                    "base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V4-Flash",
                    "api_key": "legacy-key",
                    "system_prompt": "legacy prompt",
                    "max_tokens": 4096,
                }
            },
            {},
        )
        self.assertEqual(config["base_url"], "https://llm.rsagent.net/v1")
        self.assertEqual(config["model"], "dh-dev")
        self.assertEqual(config["api_key"], "")
        self.assertEqual(config["system_prompt"], "legacy prompt")
        self.assertEqual(config["max_tokens"], 4096)
        self.assertTrue(config["legacy_section_ignored"])

    def test_invalid_timeout_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_llm_config({}, {"LLM_TIMEOUT_SECONDS": "0"})


class LlmClientTest(unittest.TestCase):
    def test_missing_api_key_returns_config_error_without_leaking(self):
        client = LlmClient(resolve_llm_config({}, {}))
        self.assertFalse(client.configured)
        with self.assertRaises(LlmConfigError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("LLM_API_KEY", str(ctx.exception))

    def test_json_mode_uses_openai_compatible_endpoint(self):
        server = MockLlmServer(mode="json")
        try:
            config = resolve_llm_config({}, {"LLM_BASE_URL": server.base_url, "LLM_API_KEY": "test-key", "LLM_STREAM": "false"})
            client = LlmClient(config)
            answer = client.chat([{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
            self.assertEqual(answer, "mock answer")
            request = server.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(request["headers"].get("Authorization"), "Bearer test-key")
            self.assertEqual(request["body"]["model"], "dh-dev")
            self.assertEqual(request["body"]["stream"], False)
            self.assertNotIn("thinking", request["body"])
        finally:
            server.close()

    def test_stream_mode_decodes_utf8_sse(self):
        server = MockLlmServer(mode="stream")
        try:
            config = resolve_llm_config({}, {"LLM_BASE_URL": server.base_url, "LLM_API_KEY": "test-key", "LLM_STREAM": "true"})
            client = LlmClient(config)
            answer = client.chat([{"role": "user", "content": "u"}])
            self.assertEqual(answer, "你好，园区")
            self.assertEqual(server.requests[0]["body"]["stream"], True)
        finally:
            server.close()

    def test_deepseek_thinking_mode_is_explicit(self):
        server = MockLlmServer(mode="json")
        try:
            config = resolve_llm_config(
                {},
                {
                    "LLM_BASE_URL": server.base_url,
                    "LLM_API_KEY": "test-key",
                    "LLM_STREAM": "false",
                    "LLM_THINKING_MODE": "deepseek",
                },
            )
            LlmClient(config).chat([{"role": "user", "content": "u"}])
            self.assertEqual(server.requests[0]["body"]["thinking"], {"type": "enabled"})
        finally:
            server.close()

    def test_http_error_is_reported_without_key(self):
        server = MockLlmServer(mode="json", status=400)
        try:
            config = resolve_llm_config({}, {"LLM_BASE_URL": server.base_url, "LLM_API_KEY": "secret-key-value", "LLM_STREAM": "false"})
            client = LlmClient(config)
            with self.assertRaises(LlmError) as ctx:
                client.chat([{"role": "user", "content": "u"}])
            message = str(ctx.exception)
            self.assertIn("HTTP 400", message)
            self.assertNotIn("secret-key-value", message)
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
