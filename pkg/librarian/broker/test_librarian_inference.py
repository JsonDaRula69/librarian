import json
import threading
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from broker import librarian_inference as module


class FixtureHandler(BaseHTTPRequestHandler):
    response = {"choices": [{"message": {"role": "assistant", "content": "fixture answer"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    status = 200
    delay = 0
    location = None
    seen = []
    fixture_responses: list[dict[str, Any]] | None = None

    def do_POST(self):
        if self.delay:
            import time
            time.sleep(self.delay)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).seen.append((self.path, self.headers, json.loads(body)))
        self.send_response(self.status)
        if self.location:
            self.send_header("Location", self.location)
        self.send_header("Content-Type", "application/json")
        response = (type(self).fixture_responses.pop(0)
                    if type(self).fixture_responses is not None else self.response)
        encoded = json.dumps(response).encode()
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


@pytest.fixture
def fixture_url(monkeypatch):
    FixtureHandler.seen = []
    FixtureHandler.fixture_responses = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    monkeypatch.setattr(module, "_validate_origin", lambda _url: None)
    monkeypatch.setattr(module, "INFERENCE_URL", url)
    yield url
    server.shutdown()
    thread.join()


def _response(content, *, finish_reason=None, usage=None):
    choice = {"message": {"role": "assistant", "content": content}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return {"choices": [choice], "usage": usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def test_posts_fixed_chat_completion_shape_and_returns_text(fixture_url):
    adapter = module.LiteLLMInferenceAdapter("redacted-key", max_tokens=77, timeout=2)
    assert adapter(route=module.MODEL, prompt="hello", max_output_tokens=999, timeout=99) == "fixture answer"
    payload = FixtureHandler.seen[-1][2]
    assert payload == {
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 77,
        "reasoning_effort": "low",
    }
    assert FixtureHandler.seen[-1][1]["Authorization"] == "Bearer redacted-key"
    receipt = adapter.usage_receipt()
    assert receipt == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert "hello" not in repr(receipt)


def test_timeout_error_is_sanitized(fixture_url):
    FixtureHandler.delay = 0.2
    try:
        adapter = module.LiteLLMInferenceAdapter("secret-key", timeout=0.01)
        with pytest.raises(module.InferenceError) as exc:
            adapter(route=module.MODEL, prompt="prompt", max_output_tokens=1, timeout=1)
        assert "secret-key" not in str(exc.value)
        assert "prompt" not in str(exc.value)
    finally:
        FixtureHandler.delay = 0


def test_redirect_is_rejected_without_following(fixture_url):
    FixtureHandler.status = 302
    FixtureHandler.location = "http://127.0.0.1:9/escaped"
    try:
        adapter = module.LiteLLMInferenceAdapter("secret")
        with pytest.raises(module.InferenceError):
            adapter(route=module.MODEL, prompt="x", max_output_tokens=1, timeout=1)
        assert len(FixtureHandler.seen) == 1
    finally:
        FixtureHandler.status = 200
        FixtureHandler.location = None


def test_malformed_response_is_rejected_and_secret_is_not_exposed(fixture_url):
    FixtureHandler.response = {"error": "secret-key", "choices": []}
    try:
        adapter = module.LiteLLMInferenceAdapter("secret-key")
        with pytest.raises(module.InferenceError) as exc:
            adapter(route=module.MODEL, prompt="private prompt", max_output_tokens=1, timeout=1)
        assert "secret-key" not in str(exc.value)
        assert "private prompt" not in str(exc.value)
    finally:
        FixtureHandler.response = {"choices": [{"message": {"role": "assistant", "content": "fixture answer"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}


def test_endpoint_must_be_loopback(monkeypatch):
    monkeypatch.setattr(module, "INFERENCE_URL", "http://localhost:4000/v1/chat/completions")
    with pytest.raises(module.InferenceError):
        module.LiteLLMInferenceAdapter("secret")(route="r", prompt="x", max_output_tokens=1, timeout=1)


def test_api_key_is_hidden_from_repr():
    adapter = module.LiteLLMInferenceAdapter("secret-key")
    assert "secret-key" not in repr(adapter)


def test_call_route_and_limits_are_trusted_and_bounded(fixture_url):
    adapter = module.LiteLLMInferenceAdapter("secret", max_tokens=77, timeout=2)
    with pytest.raises(module.InferenceError):
        adapter(route="wrong-route", prompt="hello", max_output_tokens=10, timeout=1)
    assert FixtureHandler.seen == []
    assert adapter(route=module.MODEL, prompt="hello", max_output_tokens=10, timeout=1) == "fixture answer"
    payload = FixtureHandler.seen[-1][2]
    assert payload["max_tokens"] == 10


@pytest.mark.parametrize("value", [True, False, 0, -1, 1_048_577])
def test_call_rejects_invalid_token_limits(fixture_url, value):
    adapter = module.LiteLLMInferenceAdapter("secret")
    with pytest.raises(ValueError):
        adapter(route=module.MODEL, prompt="hello", max_output_tokens=value, timeout=1)


@pytest.mark.parametrize("value", [True, False, 0, -1, float("inf"), float("nan"), "1"])
def test_call_rejects_invalid_timeouts(fixture_url, value):
    adapter = module.LiteLLMInferenceAdapter("secret")
    with pytest.raises(ValueError):
        adapter(route=module.MODEL, prompt="hello", max_output_tokens=1, timeout=value)


def test_constructor_rejects_nonfinite_or_boolean_timeout():
    for value in (True, False, float("inf"), float("nan"), 0, -1):
        with pytest.raises(ValueError):
            module.LiteLLMInferenceAdapter("secret", timeout=value)


def test_api_key_in_prompt_is_rejected_without_leaking_it(fixture_url):
    adapter = module.LiteLLMInferenceAdapter("secret-key")
    with pytest.raises(module.InferenceError) as exc:
        adapter(route=module.MODEL, prompt="contains secret-key", max_output_tokens=1, timeout=1)
    assert str(exc.value) == "inference request failed"
    assert FixtureHandler.seen == []


def test_usage_is_cleared_before_failed_call(fixture_url):
    adapter = module.LiteLLMInferenceAdapter("secret")
    assert adapter(route=module.MODEL, prompt="hello", max_output_tokens=1, timeout=1)
    FixtureHandler.status = 500
    try:
        with pytest.raises(module.InferenceError):
            adapter(route=module.MODEL, prompt="hello", max_output_tokens=1, timeout=1)
        assert adapter.usage_receipt() == {}
    finally:
        FixtureHandler.status = 200


def test_malformed_url_is_safely_rejected(monkeypatch):
    monkeypatch.setattr(module, "INFERENCE_URL", "http://[malformed")
    with pytest.raises(module.InferenceError) as exc:
        module.LiteLLMInferenceAdapter("secret")(route=module.MODEL, prompt="x", max_output_tokens=1, timeout=1)
    assert str(exc.value) == "inference request failed"


def test_empty_success_is_retried_once_then_returns_valid_content(fixture_url):
    FixtureHandler.fixture_responses = [_response(""), _response("valid")]
    adapter = module.LiteLLMInferenceAdapter("secret")
    assert adapter(route=module.MODEL, prompt="hello", max_output_tokens=1, timeout=1) == "valid"
    assert len(FixtureHandler.seen) == 2
    receipt = adapter.diagnostic_receipt()
    assert receipt["attempt_count"] == 2
    assert receipt["content_type"] == "str"
    assert receipt["content_length"] == 5
    assert "hello" not in repr(receipt)
    assert "valid" not in repr(receipt)


def test_empty_success_is_bounded_to_one_retry(fixture_url):
    FixtureHandler.fixture_responses = [_response(""), _response("")]
    adapter = module.LiteLLMInferenceAdapter("secret")
    with pytest.raises(module.InferenceError):
        adapter(route=module.MODEL, prompt="private", max_output_tokens=1, timeout=1)
    assert len(FixtureHandler.seen) == 2


def test_length_finish_reason_is_explicit_and_not_fabricated(fixture_url):
    FixtureHandler.fixture_responses = [_response("partial", finish_reason="length")]
    adapter = module.LiteLLMInferenceAdapter("secret")
    with pytest.raises(module.InferenceError, match="^output_budget_exhausted$"):
        adapter(route=module.MODEL, prompt="hello", max_output_tokens=1, timeout=1)
    assert len(FixtureHandler.seen) == 1
    assert adapter.diagnostic_receipt()["finish_reason"] == "length"


def test_empty_retry_obeys_total_deadline(fixture_url):
    FixtureHandler.fixture_responses = [_response(""), _response("valid")]
    FixtureHandler.delay = 0.08
    try:
        adapter = module.LiteLLMInferenceAdapter("secret")
        with pytest.raises(module.InferenceError):
            adapter(route=module.MODEL, prompt="hello", max_output_tokens=1, timeout=0.1)
    finally:
        FixtureHandler.delay = 0


def test_secret_in_empty_response_is_never_retried(fixture_url):
    FixtureHandler.fixture_responses = [_response("secret"), _response("valid")]
    adapter = module.LiteLLMInferenceAdapter("secret")
    with pytest.raises(module.InferenceError):
        adapter(route=module.MODEL, prompt="hello", max_output_tokens=1, timeout=1)
    assert len(FixtureHandler.seen) == 1
    assert "secret" not in repr(adapter.diagnostic_receipt())
