"""Deterministic stand-in OpenAI-compatible HTTP server for Phase 6C tests.

Listens on loopback only. Scenarios are selected by a keyword in the user
message content of the chat-completions JSON body — no outbound network.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def _chat_response(content: str, model: str = "fake-model") -> dict:
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeOpenAI/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"not found"}}')
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"not json")
            return
        messages = payload.get("messages") or []
        prompt = ""
        if messages and isinstance(messages[0], dict):
            prompt = str(messages[0].get("content") or "")

        auth = self.headers.get("Authorization", "")
        if "MISSING_AUTH" in prompt:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "invalid api key"}}).encode())
            return
        if not auth.startswith("Bearer "):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "missing bearer token"}}).encode())
            return

        if "RATE_LIMIT" in prompt:
            self.send_response(429)
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "rate limit exceeded"}}).encode())
            return
        if "SERVER_ERROR" in prompt:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "internal server error"}}).encode())
            return
        if "MALFORMED_BODY" in prompt:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{not valid completion json")
            return
        if "SLOW" in prompt:
            time.sleep(3)
            body = json.dumps(_chat_response("slow but done")).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
            return
        if "SECRET_LEAK" in prompt:
            body = json.dumps(_chat_response("token sk-testsecret1234567890 leaked")).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
            return

        body = json.dumps(_chat_response(f"answer for: {prompt}")).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


class FakeOpenAiServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self.host = host
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="fake-openai", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)


def provider_document(base_url: str, name: str = "local", api_key_env: str = "TEST_OPENAI_API_KEY", **overrides) -> dict:
    entry = {
        "kind": "openai-compatible",
        "base_url": base_url,
        "api_key_env": api_key_env,
        "default_model": "fake-model",
        "request_timeout_seconds": 2,
        "allow_insecure_http": True,
        "capabilities": {"chat_completions": True},
    }
    entry.update(overrides)
    return {"providers": {name: entry}}


if __name__ == "__main__":
    server = FakeOpenAiServer()
    server.start()
    print(server.base_url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
