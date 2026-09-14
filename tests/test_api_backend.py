from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest

from baseline.bian.models.api_backend import ApiBackend, ApiConfig, resolve_api_base
from baseline.bian.models.backend import ModelConfig, model_load_options


PROMPTS = Path(__file__).parents[1] / "baseline/bian/prompts"


def validator(value):
    if not isinstance(value, dict) or not isinstance(value.get("answer"), int):
        raise ValueError("answer must be an integer")
    return value


class _Handler(BaseHTTPRequestHandler):
    attempts = 0
    failures_before_success = 0
    content = '{"answer":42}'
    authorization = None

    def do_POST(self):
        type(self).attempts += 1
        type(self).authorization = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        if self.path != "/v1/chat/completions" or payload.get("model") != "test-model":
            self.send_response(400)
            self.end_headers()
            return
        if type(self).attempts <= type(self).failures_before_success:
            self.send_response(500)
            self.end_headers()
            return
        body = json.dumps({"choices": [{"message": {"content": type(self).content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


class ApiBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        _Handler.attempts = 0
        _Handler.failures_before_success = 0
        _Handler.content = '{"answer":42}'
        _Handler.authorization = None
        os.environ["AIOPS_TEST_API_KEY"] = "test-token"
        self.backend = ApiBackend(
            ApiConfig(
                base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
                model="test-model",
                api_key_env="AIOPS_TEST_API_KEY",
                timeout=2.0,
                retries=1,
                retry_backoff=0.0,
            ),
            PROMPTS,
        )

    def tearDown(self):
        os.environ.pop("AIOPS_TEST_API_KEY", None)

    def test_parses_valid_chat_completion_and_sends_bearer_token(self):
        result = self.backend.generate_json(
            role="test",
            prompt_name="classification",
            payload={"value": 1},
            validator=validator,
            max_new_tokens=32,
        )

        self.assertEqual(result, {"answer": 42})
        self.assertEqual(_Handler.authorization, "Bearer test-token")

    def test_retries_transient_http_500(self):
        _Handler.failures_before_success = 1

        result = self.backend.generate_json(
            role="test",
            prompt_name="classification",
            payload={},
            validator=validator,
            max_new_tokens=32,
        )

        self.assertEqual(result["answer"], 42)
        self.assertEqual(_Handler.attempts, 2)

    def test_invalid_model_json_is_rejected_without_leaking_secret(self):
        _Handler.content = "not-json"

        with self.assertRaises(ValueError) as caught:
            self.backend.generate_json(
                role="test",
                prompt_name="classification",
                payload={},
                validator=validator,
                max_new_tokens=32,
            )

        self.assertNotIn("test-token", str(caught.exception))

    def test_multi_gpu_load_options_use_automatic_device_map(self):
        self.assertEqual(model_load_options(cuda_available=True), {"device_map": "auto"})
        self.assertEqual(model_load_options(cuda_available=False), {})

    def test_api_base_can_come_from_server_environment(self):
        os.environ["AIOPS_LLM_API_BASE"] = "http://server.example/v1/"
        try:
            self.assertEqual(resolve_api_base(None), "http://server.example/v1")
            self.assertEqual(
                resolve_api_base("http://explicit.example/v1/"),
                "http://explicit.example/v1",
            )
        finally:
            os.environ.pop("AIOPS_LLM_API_BASE", None)

    def test_local_model_config_ignores_orchestration_only_fields(self):
        config = ModelConfig.from_mapping(
            {
                "batch_size": 4,
                "max_input_tokens": 4096,
                "llm_candidate_limit": 12,
            }
        )

        self.assertEqual(config.batch_size, 4)
        self.assertEqual(config.max_input_tokens, 4096)


if __name__ == "__main__":
    unittest.main()
