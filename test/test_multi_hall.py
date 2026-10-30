"""多展厅 Token 鉴权与按展厅回程配置测试（全部使用 Mock RUISI / DeepSeek）。

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
HALL_A = "HC-A7K9P2"
HALL_B = "HC-B4M8Q1"
HALL_C = "HC-C6N3R5"
AGENT_A = "test-agent-a"
AGENT_B = "test-agent-b"
AGENT_C = "test-agent-c"
INGRESS_TOKEN_A = "ingress-token-a"
INGRESS_TOKEN_B = "ingress-token-b"
INGRESS_TOKEN_C = "ingress-token-c"
HALL_ENV_NAMES = {
    "HC_TOKEN_MAP",
    "AGENT",
    "INGRESS_HOST",
    "INGRESS_PORT",
    "INGRESS_TOKEN",
}
for hall in (HALL_A, HALL_B, HALL_C):
    prefix = "HC_HALL_" + "".join(ch if ch.isalnum() else "_" for ch in hall.upper())
    HALL_ENV_NAMES.update({f"{prefix}_{suffix}" for suffix in ("AGENT", "INGRESS_HOST", "INGRESS_PORT", "INGRESS_TOKEN", "TO")})


class CallbackCollector:
    def __init__(self):
        self.requests = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                collector.requests.append(
                    {"path": self.path, "payload": payload, "headers": dict(self.headers)}
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.host = "127.0.0.1"
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/agent/send"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeDeepSeek:
    def __init__(self):
        self.calls = 0
        self.messages = []

    def chat(self, messages):
        self.calls += 1
        self.messages.append(messages)
        return f"answer-{self.calls}"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def hall_env(collectors, overrides=None):
    env = {
        "HC_TOKEN_MAP": f"{TOKEN_A}:{HALL_A},{TOKEN_B}:{HALL_B},{TOKEN_C}:{HALL_C}",
        f"HC_HALL_HC_A7K9P2_AGENT": AGENT_A,
        f"HC_HALL_HC_A7K9P2_INGRESS_HOST": collectors[HALL_A].host,
        f"HC_HALL_HC_A7K9P2_INGRESS_PORT": str(collectors[HALL_A].port),
        f"HC_HALL_HC_A7K9P2_INGRESS_TOKEN": INGRESS_TOKEN_A,
        f"HC_HALL_HC_B4M8Q1_AGENT": AGENT_B,
        f"HC_HALL_HC_B4M8Q1_INGRESS_HOST": collectors[HALL_B].host,
        f"HC_HALL_HC_B4M8Q1_INGRESS_PORT": str(collectors[HALL_B].port),
        f"HC_HALL_HC_B4M8Q1_INGRESS_TOKEN": INGRESS_TOKEN_B,
        f"HC_HALL_HC_C6N3R5_AGENT": AGENT_C,
        f"HC_HALL_HC_C6N3R5_INGRESS_HOST": collectors[HALL_C].host,
        f"HC_HALL_HC_C6N3R5_INGRESS_PORT": str(collectors[HALL_C].port),
        f"HC_HALL_HC_C6N3R5_INGRESS_TOKEN": INGRESS_TOKEN_C,
    }
    if overrides:
        env.update(overrides)
    return env


class MultiHallTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="hc-data-agent-test-")
        cls.callbacks = {hall: CallbackCollector() for hall in (HALL_A, HALL_B, HALL_C)}
        config = {
            "server": {"host": "127.0.0.1", "port": free_port()},
            "deepseek": {"api_key": "test-key", "stream": False},
            "knowledge_base": {
                "dir": os.path.join(cls.temp_dir, "knowledge"),
                "max_file_chars": 1000,
                "max_total_chars": 1000,
            },
            "upload": {"dir": os.path.join(cls.temp_dir, "uploads")},
        }
        config_path = os.path.join(cls.temp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        for name in HALL_ENV_NAMES:
            os.environ.pop(name, None)
        os.environ["EXECUTOR_CONFIG"] = config_path
        os.environ.update(hall_env(cls.callbacks))
        os.chdir(cls.temp_dir)
        sys.path.insert(0, PROJECT_DIR)

        import main as executor

        cls.executor = executor
        cls.fake_client = FakeDeepSeek()
        executor.client = cls.fake_client

        import uvicorn

        cls.uvicorn_server = uvicorn.Server(
            uvicorn.Config(
                executor.app,
                host="127.0.0.1",
                port=config["server"]["port"],
                log_level="warning",
            )
        )
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
        for collector in cls.callbacks.values():
            collector.close()
        os.chdir(PROJECT_DIR)

    def setUp(self):
        for collector in self.callbacks.values():
            collector.requests.clear()
        self.fake_client.calls = 0

    def post_commands(self, payload, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-HC-Token"] = token
        return requests.post(
            f"{self.base_url}/api/commands", headers=headers, json=payload, timeout=15
        )

    def context(self, hall=HALL_A, agent="context-agent"):
        return {
            "callback": self.callbacks[hall].url,
            "reply_to": "user@example.com",
            "agent": agent,
            "groupchat": False,
        }

    def reset_payload(self, hall=HALL_A):
        return {"context": self.context(hall), "commands": [{"action": "AI问答会话重置", "params": {}}]}

    def test_missing_invalid_and_unknown_token_are_rejected(self):
        payload = self.reset_payload()
        for token in (None, "short", "unknown-token-0123456789"):
            response = self.post_commands(payload, token)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"ok": False, "error": "unauthorized"})
        self.assertEqual(sum(len(item.requests) for item in self.callbacks.values()), 0)
        self.assertEqual(self.fake_client.calls, 0)

    def test_a_b_c_tokens_use_their_own_agent_ingress_port_and_token(self):
        cases = (
            (TOKEN_A, HALL_A, AGENT_A, INGRESS_TOKEN_A),
            (TOKEN_B, HALL_B, AGENT_B, INGRESS_TOKEN_B),
            (TOKEN_C, HALL_C, AGENT_C, INGRESS_TOKEN_C),
        )
        for token, hall, agent, ingress_token in cases:
            for collector in self.callbacks.values():
                collector.requests.clear()
            response = self.post_commands(self.reset_payload(hall), token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.callbacks[hall].requests), 1)
            request = self.callbacks[hall].requests[0]
            self.assertEqual(request["path"], "/agent/send")
            self.assertEqual(request["payload"]["agent"], agent)
            self.assertEqual(request["payload"]["to"], "user@example.com")
            self.assertEqual(request["headers"].get("X-Auth-Token"), ingress_token)
            for other in self.callbacks:
                if other != hall:
                    self.assertEqual(self.callbacks[other].requests, [])

    def test_body_exhibition_id_and_context_agent_cannot_override_token(self):
        response = self.post_commands(
            {
                "exhibitionId": HALL_A,
                "context": {**self.context(HALL_B), "exhibitionId": HALL_A, "agent": "spoofed-agent"},
                "commands": [{"action": "AI问答会话重置", "params": {}}],
            },
            TOKEN_B,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.callbacks[HALL_A].requests, [])
        self.assertEqual(len(self.callbacks[HALL_B].requests), 1)
        self.assertEqual(self.callbacks[HALL_B].requests[0]["payload"]["agent"], AGENT_B)
        self.assertEqual(
            self.callbacks[HALL_B].requests[0]["headers"].get("X-Auth-Token"), INGRESS_TOKEN_B
        )

    def test_context_callback_cannot_point_at_another_hall(self):
        response = self.post_commands(
            {
                "context": {**self.context(HALL_A), "callback": self.callbacks[HALL_B].url},
                "commands": [{"action": "AI问答会话重置", "params": {}}],
            },
            TOKEN_A,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.callbacks[HALL_A].requests, [])
        self.assertEqual(self.callbacks[HALL_B].requests, [])

    def test_concurrent_a_b_c_requests_keep_their_own_configuration_and_sessions(self):
        results = []

        def call(token, hall):
            response = self.post_commands(self.reset_payload(hall), token)
            results.append(response.status_code)

        threads = [
            threading.Thread(target=call, args=(TOKEN_A, HALL_A)),
            threading.Thread(target=call, args=(TOKEN_B, HALL_B)),
            threading.Thread(target=call, args=(TOKEN_C, HALL_C)),
        ]
        for item in threads:
            item.start()
        for item in threads:
            item.join()

        self.assertEqual(results, [200, 200, 200])
        observed = {
            hall: {(item["payload"]["agent"], item["headers"].get("X-Auth-Token")) for item in collector.requests}
            for hall, collector in self.callbacks.items()
        }
        self.assertEqual(observed[HALL_A], {(AGENT_A, INGRESS_TOKEN_A)})
        self.assertEqual(observed[HALL_B], {(AGENT_B, INGRESS_TOKEN_B)})
        self.assertEqual(observed[HALL_C], {(AGENT_C, INGRESS_TOKEN_C)})

    def test_business_request_still_works_and_health_is_anonymous(self):
        response = self.post_commands(
            {"context": self.context(HALL_A), "commands": [{"action": "AI智能问答", "params": {"问题": "园区有多少建筑"}}]},
            TOKEN_A,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["reply"], "answer-1")
        self.assertEqual(self.fake_client.calls, 1)
        self.assertEqual(len(self.callbacks[HALL_A].requests), 1)
        self.assertEqual(self.callbacks[HALL_A].requests[0]["payload"]["body"], "answer-1")

        health = requests.get(f"{self.base_url}/api/health", timeout=10)
        self.assertEqual(health.status_code, 200)
        self.assertTrue(health.json()["ok"])
        self.assertEqual(health.json()["provider"], self.executor.LLM_CFG["provider"])
        self.assertEqual(health.json()["model"], "dh-dev")
        self.assertNotIn("api_key", health.json())

    def test_knowledge_base_is_loaded_and_injected_into_llm_messages(self):
        kb_dir = self.executor.KB_CFG["dir"]
        os.makedirs(kb_dir, exist_ok=True)
        kb_path = os.path.join(kb_dir, "01_权威指标事实表.csv")
        with open(kb_path, "w", encoding="utf-8") as handle:
            handle.write("指标,数值\n园区楼栋数,12\n")
        try:
            self.assertIn("园区楼栋数,12", self.executor.load_knowledge_base())

            self.fake_client.messages.clear()
            response = self.post_commands(
                {"context": self.context(HALL_A), "commands": [{"action": "AI智能问答", "params": {"问题": "园区有多少栋楼"}}]},
                TOKEN_A,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["reply"], "answer-1")
            messages = self.fake_client.messages[-1]
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[0]["content"], self.executor.LLM_CFG["system_prompt"])
            self.assertIn("园区楼栋数,12", messages[1]["content"])
            self.assertEqual(response.json()["results"][0]["knowledge_base_chars"], len(self.executor.load_knowledge_base()))
        finally:
            os.remove(kb_path)

    def test_debug_chat_endpoint_remains_compatible(self):
        response = requests.post(f"{self.base_url}/api/chat", json={"question": "园区有多少建筑"}, timeout=15)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(sum(len(item.requests) for item in self.callbacks.values()), 0)

    def test_session_isolation_between_exhibitions(self):
        executor = self.executor
        self.assertEqual(executor._session_id({"reply_to": "u"}, HALL_A), f"hall:{HALL_A}:user:u")
        self.assertEqual(executor._session_id({"reply_to": "u"}, HALL_B), f"hall:{HALL_B}:user:u")
        self.assertNotEqual(executor._session_id({}, HALL_A), executor._session_id({}, HALL_B))
        self.assertEqual(executor._session_id({}), "default")

    def test_to_fallback_is_used_only_when_reply_to_is_missing(self):
        executor = self.executor
        original = executor.HALLS[HALL_A]
        try:
            executor.HALLS[HALL_A] = {**original, "to": "fixed@example.net"}
            executor.send_callback({"callback": self.callbacks[HALL_A].url}, "body", HALL_A)
            self.assertEqual(len(self.callbacks[HALL_A].requests), 1)
            self.assertEqual(self.callbacks[HALL_A].requests[0]["payload"]["to"], "fixed@example.net")

            self.callbacks[HALL_A].requests.clear()
            executor.send_callback(
                {"callback": self.callbacks[HALL_A].url, "reply_to": "user@example.com"}, "body", HALL_A
            )
            self.assertEqual(self.callbacks[HALL_A].requests[0]["payload"]["to"], "user@example.com")
        finally:
            executor.HALLS[HALL_A] = original

    def test_unconfigured_hall_cannot_fall_back_to_another_hall(self):
        executor = self.executor
        original = dict(executor.HALLS)
        try:
            executor.HALLS.pop(HALL_C, None)
            executor.send_callback(self.context(HALL_C), "body", HALL_C)
            self.assertEqual(self.callbacks[HALL_C].requests, [])
        finally:
            executor.HALLS.clear()
            executor.HALLS.update(original)

    def test_callback_matches_hall_requires_scheme_host_port_and_fixed_path(self):
        executor = self.executor
        hall = executor.HALLS[HALL_A]
        allowed = self.callbacks[HALL_A].url
        self.assertTrue(executor.callback_matches_hall(allowed, hall))
        self.assertFalse(executor.callback_matches_hall(allowed.replace("http://", "https://"), hall))
        self.assertFalse(executor.callback_matches_hall(allowed.replace("127.0.0.1", "other.example"), hall))
        self.assertFalse(executor.callback_matches_hall(allowed + "/extra", hall))
        self.assertFalse(executor.callback_matches_hall(allowed.replace("/agent/send", "/other"), hall))
        self.assertFalse(executor.callback_matches_hall("", hall))
        self.assertFalse(executor.callback_matches_hall(allowed, {}))

    def test_parse_halls_requires_complete_per_hall_configuration(self):
        executor = self.executor
        collectors = self.callbacks
        base = hall_env(collectors)
        for missing in (
            f"HC_HALL_HC_C6N3R5_AGENT",
            f"HC_HALL_HC_C6N3R5_INGRESS_HOST",
            f"HC_HALL_HC_C6N3R5_INGRESS_PORT",
            f"HC_HALL_HC_C6N3R5_INGRESS_TOKEN",
        ):
            with self.assertRaisesRegex(ValueError, missing):
                executor.parse_halls({**base, missing: ""}, executor.TOKEN_MAP)

        with self.assertRaisesRegex(ValueError, "INGRESS_PORT"):
            executor.parse_halls(
                {**base, "HC_HALL_HC_C6N3R5_INGRESS_PORT": "70000"}, executor.TOKEN_MAP
            )
        with self.assertRaisesRegex(ValueError, "without protocol or path"):
            executor.parse_halls(
                {**base, "HC_HALL_HC_C6N3R5_INGRESS_HOST": "http://127.0.0.1"}, executor.TOKEN_MAP
            )
        with self.assertRaisesRegex(ValueError, "placeholder"):
            executor.parse_halls(
                {**base, "HC_HALL_HC_C6N3R5_AGENT": "REPLACE_WITH_EXHIBITION_AGENT"},
                executor.TOKEN_MAP,
            )

    def test_multi_hall_does_not_fall_back_to_legacy_global_ingress(self):
        executor = self.executor
        env = {
            "HC_TOKEN_MAP": f"{TOKEN_A}:{HALL_A},{TOKEN_B}:{HALL_B}",
            "AGENT": "legacy-agent",
            "INGRESS_HOST": "127.0.0.1",
            "INGRESS_PORT": "29876",
            "INGRESS_TOKEN": "legacy-token",
        }
        token_map = executor.parse_token_map(env["HC_TOKEN_MAP"], label="HC_TOKEN_MAP")
        with self.assertRaisesRegex(ValueError, "HC_HALL_HC_A7K9P2_AGENT"):
            executor.parse_halls(env, token_map)

    def test_single_hall_legacy_ingress_fallback_still_works(self):
        executor = self.executor
        env = {
            "HC_TOKEN_MAP": f"{TOKEN_A}:{HALL_A}",
            "AGENT": "legacy-agent",
            "INGRESS_HOST": "127.0.0.1",
            "INGRESS_PORT": "29876",
            "INGRESS_TOKEN": "legacy-token",
        }
        token_map = executor.parse_token_map(env["HC_TOKEN_MAP"], label="HC_TOKEN_MAP")
        halls = executor.parse_halls(env, token_map)
        self.assertEqual(halls[HALL_A]["agent"], "legacy-agent")
        self.assertEqual(halls[HALL_A]["ingress_port"], 29876)
        self.assertEqual(halls[HALL_A]["ingress_url"], "http://127.0.0.1:29876/agent/send")

    def test_shared_agent_and_ingress_target_are_rejected(self):
        executor = self.executor
        base = hall_env(self.callbacks)
        with self.assertRaisesRegex(ValueError, "AGENT is shared"):
            executor.parse_halls(
                {**base, "HC_HALL_HC_B4M8Q1_AGENT": AGENT_A}, executor.TOKEN_MAP
            )
        with self.assertRaisesRegex(ValueError, "ingress target .* is shared"):
            executor.parse_halls(
                {
                    **base,
                    "HC_HALL_HC_B4M8Q1_INGRESS_HOST": self.callbacks[HALL_A].host,
                    "HC_HALL_HC_B4M8Q1_INGRESS_PORT": str(self.callbacks[HALL_A].port),
                },
                executor.TOKEN_MAP,
            )

    def test_placeholder_tokens_are_rejected(self):
        executor = self.executor
        with self.assertRaisesRegex(ValueError, "placeholder token"):
            executor.parse_token_map({"REPLACE_WITH_EXHIBITION_A_TOKEN": HALL_A}, label="HC_TOKEN_MAP")
        with self.assertRaisesRegex(ValueError, "placeholder token"):
            executor.parse_token_map({"example-token-a-0123456789abcdef": HALL_A}, label="HC_TOKEN_MAP")

    def test_startup_rejects_missing_token_map(self):
        temp_dir = tempfile.mkdtemp(prefix="hc-data-agent-no-token-map-")
        config_path = os.path.join(temp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"server": {"host": "127.0.0.1", "port": 0}, "deepseek": {"api_key": "k"}}, handle)

        env = {key: value for key, value in os.environ.items() if key not in HALL_ENV_NAMES}
        env["EXECUTOR_CONFIG"] = config_path
        code = f"import sys; sys.path.insert(0, {PROJECT_DIR!r}); import main"
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=temp_dir, env=env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HC_TOKEN_MAP is required", result.stderr)

    def test_startup_rejects_missing_hall_configuration(self):
        temp_dir = tempfile.mkdtemp(prefix="hc-data-agent-bad-hall-")
        config_path = os.path.join(temp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"server": {"host": "127.0.0.1", "port": 0}, "deepseek": {"api_key": "k"}}, handle)

        env = {key: value for key, value in os.environ.items() if key not in HALL_ENV_NAMES}
        env.update(hall_env(self.callbacks, {"HC_HALL_HC_C6N3R5_INGRESS_TOKEN": ""}))
        env["EXECUTOR_CONFIG"] = config_path
        code = f"import sys; sys.path.insert(0, {PROJECT_DIR!r}); import main"
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=temp_dir, env=env,
            capture_output=True, text=True, timeout=60,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HC_HALL_HC_C6N3R5_INGRESS_TOKEN", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
