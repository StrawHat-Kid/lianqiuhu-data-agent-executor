"""多展厅 Token 隔离测试（仅本地 Mock，不访问生产 RUISI / DeepSeek）。

运行：
    python -m unittest discover -s test -p "test_*.py" -v
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_A = "test-token-a-0123456789abcdef"
TOKEN_B = "test-token-b-0123456789abcdef"
TOKEN_C = "test-token-c-0123456789abcdef"
HALL_A = "exhibition-a"
HALL_B = "exhibition-b"
HALL_C = "exhibition-c"
AGENT_A = "test-agent-a"
AGENT_B = "test-agent-b"
AGENT_C = "test-agent-c"
CALLBACK_TOKEN_A = "callback-token-a"
CALLBACK_TOKEN_B = "callback-token-b"
CALLBACK_TOKEN_C = "callback-token-c"


class CallbackCollector:
    def __init__(self):
        self.requests = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                collector.requests.append({"payload": payload, "headers": dict(self.headers)})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/agent/send"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeDeepSeek:
    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return f"答复-{self.calls}"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MultiHallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="hc-data-agent-test-")
        cls.callback = CallbackCollector()
        config = {
            "server": {"host": "127.0.0.1", "port": free_port()},
            "deepseek": {"api_key": "test-key", "stream": False},
            "knowledge_base": {"dir": os.path.join(cls.temp_dir, "knowledge"), "max_file_chars": 1000, "max_total_chars": 1000},
            "upload": {"dir": os.path.join(cls.temp_dir, "uploads")},
            "callback": {"token": "global-callback-token"},
            "exhibitions": {
                "token_map": {TOKEN_A: HALL_A, TOKEN_B: HALL_B},
                "halls": {
                    HALL_A: {
                        "agent": AGENT_A,
                        "callback_token": CALLBACK_TOKEN_A,
                        "callback_allowlist": [cls.callback.url],
                    },
                    HALL_B: {
                        "agent": AGENT_B,
                        "callback_token": CALLBACK_TOKEN_B,
                        "callback_allowlist": [cls.callback.url],
                    },
                },
            },
        }
        config_path = os.path.join(cls.temp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        os.environ["EXECUTOR_CONFIG"] = config_path
        os.chdir(cls.temp_dir)
        sys.path.insert(0, PROJECT_DIR)

        import main as executor

        cls.executor = executor
        cls.fake_client = FakeDeepSeek()
        executor.client = cls.fake_client

        import uvicorn

        cls.uvicorn_server = uvicorn.Server(uvicorn.Config(executor.app, host="127.0.0.1", port=config["server"]["port"], log_level="warning"))
        cls.thread = threading.Thread(target=cls.uvicorn_server.run, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{config['server']['port']}"

        deadline = time.time() + 15
        while not cls.uvicorn_server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.uvicorn_server.should_exit = True
        cls.thread.join(timeout=10)
        cls.callback.close()
        os.chdir(PROJECT_DIR)

    def setUp(self):
        self.callback.requests.clear()
        self.fake_client.calls = 0

    def post_commands(self, payload, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-HC-Token"] = token
        return requests.post(f"{self.base_url}/api/commands", headers=headers, json=payload, timeout=15)

    def context(self):
        return {"callback": self.callback.url, "reply_to": "user@example.com", "agent": "context-agent"}

    def test_missing_invalid_and_unknown_token_are_rejected(self):
        payload = {"context": self.context(), "commands": [{"action": "AI问答会话重置", "params": {}}]}
        for token in (None, "short", "unknown-token-0123456789"):
            response = self.post_commands(payload, token)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"ok": False, "error": "unauthorized"})
        self.assertEqual(self.callback.requests, [])
        self.assertEqual(self.fake_client.calls, 0)

    def test_a_and_b_tokens_use_their_own_agent_and_callback_token(self):
        for token, agent, callback_token in (
            (TOKEN_A, AGENT_A, CALLBACK_TOKEN_A),
            (TOKEN_B, AGENT_B, CALLBACK_TOKEN_B),
        ):
            self.callback.requests.clear()
            response = self.post_commands(
                {"context": self.context(), "commands": [{"action": "AI问答会话重置", "params": {}}]},
                token,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.callback.requests), 1)
            request = self.callback.requests[0]
            self.assertEqual(request["payload"]["agent"], agent)
            self.assertEqual(request["payload"]["to"], "user@example.com")
            self.assertEqual(request["headers"].get("X-Auth-Token"), callback_token)

    def test_body_exhibition_id_and_context_agent_cannot_override_token(self):
        response = self.post_commands(
            {
                "exhibitionId": HALL_A,
                "context": {**self.context(), "exhibitionId": HALL_A},
                "commands": [{"action": "AI问答会话重置", "params": {}}],
            },
            TOKEN_B,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.callback.requests), 1)
        self.assertEqual(self.callback.requests[0]["payload"]["agent"], AGENT_B)
        self.assertEqual(self.callback.requests[0]["headers"].get("X-Auth-Token"), CALLBACK_TOKEN_B)

    def test_concurrent_a_and_b_requests_keep_their_own_configuration(self):
        results = []

        def call(token):
            response = self.post_commands(
                {"context": self.context(), "commands": [{"action": "AI问答会话重置", "params": {}}]},
                token,
            )
            results.append(response.status_code)

        threads = [threading.Thread(target=call, args=(token,)) for token in (TOKEN_A, TOKEN_B)]
        for item in threads:
            item.start()
        for item in threads:
            item.join()

        self.assertEqual(results, [200, 200])
        self.assertEqual(len(self.callback.requests), 2)
        observed = {(item["payload"]["agent"], item["headers"].get("X-Auth-Token")) for item in self.callback.requests}
        self.assertEqual(observed, {(AGENT_A, CALLBACK_TOKEN_A), (AGENT_B, CALLBACK_TOKEN_B)})

    def test_business_request_still_works_and_health_is_anonymous(self):
        response = self.post_commands(
            {"context": self.context(), "commands": [{"action": "AI智能问答", "params": {"问题": "测试问题"}}]},
            TOKEN_A,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["reply"], "答复-1")
        self.assertEqual(self.fake_client.calls, 1)
        self.assertEqual(len(self.callback.requests), 1)

        health = requests.get(f"{self.base_url}/api/health", timeout=10)
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])

    def test_debug_chat_endpoint_remains_compatible(self):
        response = requests.post(f"{self.base_url}/api/chat", json={"question": "联调问题"}, timeout=15)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.callback.requests, [])

    def test_session_isolation_between_exhibitions(self):
        executor = self.executor
        self.assertEqual(executor._session_id({"reply_to": "u"}, HALL_A), "hall:exhibition-a:user:u")
        self.assertEqual(executor._session_id({"reply_to": "u"}, HALL_B), "hall:exhibition-b:user:u")
        self.assertNotEqual(
            executor._session_id({}, HALL_A),
            executor._session_id({}, HALL_B),
        )
        self.assertEqual(executor._session_id({}), "default")

    def test_context_agent_cannot_override_hall_agent(self):
        response = self.post_commands(
            {
                "context": {**self.context(), "agent": "spoofed-agent"},
                "commands": [{"action": "AI问答会话重置", "params": {}}],
            },
            TOKEN_A,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.callback.requests), 1)
        self.assertEqual(self.callback.requests[0]["payload"]["agent"], AGENT_A)

    def test_callback_allowlist_blocks_cross_hall_target(self):
        executor = self.executor
        original = executor.HALLS[HALL_A]
        try:
            executor.HALLS[HALL_A] = {**original, "callback_allowlist": ["http://not-allowed.example/agent/send"]}
            self.assertTrue(executor.callback_allowed("http://not-allowed.example/agent/send", executor.HALLS[HALL_A]["callback_allowlist"]))
            self.assertFalse(executor.callback_allowed(self.callback.url, executor.HALLS[HALL_A]["callback_allowlist"]))

            response = self.post_commands(
                {"context": self.context(), "commands": [{"action": "AI问答会话重置", "params": {}}]},
                TOKEN_A,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.callback.requests, [])
        finally:
            executor.HALLS[HALL_A] = original

    def test_callback_allowlist_allows_configured_target(self):
        executor = self.executor
        original = executor.HALLS[HALL_A]
        try:
            executor.HALLS[HALL_A] = {**original, "callback_allowlist": [self.callback.url]}
            response = self.post_commands(
                {"context": self.context(), "commands": [{"action": "AI问答会话重置", "params": {}}]},
                TOKEN_A,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.callback.requests), 1)
        finally:
            executor.HALLS[HALL_A] = original

    def test_third_hall_maps_to_its_own_agent_and_requires_complete_configuration(self):
        executor = self.executor
        token_map = executor.parse_token_map({TOKEN_A: HALL_A, TOKEN_B: HALL_B, TOKEN_C: HALL_C})
        self.assertEqual(executor.resolve_exhibition_id(TOKEN_C, token_map), HALL_C)

        allowlist = [self.callback.url]
        original = executor.TOKEN_MAP
        try:
            executor.TOKEN_MAP = token_map
            with self.assertRaisesRegex(ValueError, "missing configuration"):
                executor.parse_halls({
                    HALL_A: {"agent": AGENT_A, "callback_allowlist": allowlist},
                    HALL_B: {"agent": AGENT_B, "callback_allowlist": allowlist},
                })
            halls = executor.parse_halls({
                HALL_A: {"agent": AGENT_A, "callback_allowlist": allowlist},
                HALL_B: {"agent": AGENT_B, "callback_allowlist": allowlist},
                HALL_C: {"agent": AGENT_C, "callback_allowlist": allowlist},
            })
            self.assertEqual(halls[HALL_C]["agent"], AGENT_C)
            self.assertEqual(halls[HALL_C]["callback_allowlist"], allowlist)
        finally:
            executor.TOKEN_MAP = original

    def test_unconfigured_hall_cannot_fall_back_to_default_callback(self):
        executor = self.executor
        original = dict(executor.HALLS)
        try:
            executor.HALLS.pop(HALL_C, None)
            executor.send_callback(self.context(), "body", HALL_C)
            self.assertEqual(self.callback.requests, [])
        finally:
            executor.HALLS.clear()
            executor.HALLS.update(original)

    def test_parse_halls_requires_agent_allowlist_and_complete_mapping(self):
        executor = self.executor
        allowlist = ["http://allowed.example/agent/send"]
        with self.assertRaisesRegex(ValueError, "agent is required"):
            executor.parse_halls({HALL_A: {"agent": "", "callback_allowlist": allowlist}})
        with self.assertRaisesRegex(ValueError, "callback_allowlist is required"):
            executor.parse_halls({HALL_A: {"agent": AGENT_A}})
        with self.assertRaisesRegex(ValueError, "callback_allowlist is required"):
            executor.parse_halls({HALL_A: {"agent": AGENT_A, "callback_allowlist": []}})
        with self.assertRaisesRegex(ValueError, "missing configuration"):
            executor.parse_halls({HALL_A: {"agent": AGENT_A, "callback_allowlist": allowlist}})
        with self.assertRaisesRegex(ValueError, "placeholder value"):
            executor.parse_halls({
                HALL_A: {"agent": "REPLACE_WITH_EXHIBITION_A_AGENT", "callback_allowlist": allowlist}
            })

    def test_callback_allowlist_matches_scheme_host_port_and_path_prefix(self):
        executor = self.executor
        allowed = ["http://allowed.example:9000/agent/send"]
        self.assertTrue(executor.callback_allowed("http://allowed.example:9000/agent/send", allowed))
        self.assertTrue(executor.callback_allowed("http://allowed.example:9000/agent/send/extra", allowed))
        self.assertFalse(executor.callback_allowed("https://allowed.example:9000/agent/send", allowed))
        self.assertFalse(executor.callback_allowed("http://other.example:9000/agent/send", allowed))
        self.assertFalse(executor.callback_allowed("http://allowed.example:9001/agent/send", allowed))
        self.assertFalse(executor.callback_allowed("http://allowed.example:9000/agent/send2", allowed))
        self.assertFalse(executor.callback_allowed("http://allowed.example:9000/other", allowed))
        self.assertFalse(executor.callback_allowed("http://allowed.example:9000/agent/send", []))
        self.assertTrue(executor.callback_allowed("http://allowed.example:80/agent/send", ["http://allowed.example/agent/send"]))

    def test_placeholder_tokens_are_rejected(self):
        executor = self.executor
        with self.assertRaisesRegex(ValueError, "placeholder token"):
            executor.parse_token_map({"REPLACE_WITH_EXHIBITION_A_TOKEN": HALL_A})
        with self.assertRaisesRegex(ValueError, "placeholder token"):
            executor.parse_token_map({"example-token-a-0123456789abcdef": HALL_A})

    def test_startup_rejects_hall_without_agent(self):
        temp_dir = tempfile.mkdtemp(prefix="hc-data-agent-bad-config-")
        config_path = os.path.join(temp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "server": {"host": "127.0.0.1", "port": 0},
                "deepseek": {"api_key": "test-key"},
                "exhibitions": {"token_map": {TOKEN_A: HALL_A}, "halls": {HALL_A: {}}},
            }, handle)

        env = {**os.environ, "EXECUTOR_CONFIG": config_path}
        code = f"import sys; sys.path.insert(0, {PROJECT_DIR!r}); import main"
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=temp_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("agent is required", result.stderr)

    def test_startup_rejects_hall_without_callback_allowlist(self):
        temp_dir = tempfile.mkdtemp(prefix="hc-data-agent-bad-allowlist-")
        config_path = os.path.join(temp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "server": {"host": "127.0.0.1", "port": 0},
                "deepseek": {"api_key": "test-key"},
                "exhibitions": {
                    "token_map": {TOKEN_A: HALL_A},
                    "halls": {HALL_A: {"agent": AGENT_A}},
                },
            }, handle)

        env = {**os.environ, "EXECUTOR_CONFIG": config_path}
        code = f"import sys; sys.path.insert(0, {PROJECT_DIR!r}); import main"
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=temp_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("callback_allowlist is required", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
