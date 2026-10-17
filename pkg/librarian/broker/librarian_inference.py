"""Host-only, fixed-route LiteLLM chat completion adapter."""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

INFERENCE_URL = "http://127.0.0.1:4000/v1/chat/completions"
MODEL = "glm-5.3-flash"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 30
MAX_RESPONSE_BYTES = 1_048_576


class InferenceError(RuntimeError):
    """A safe, provider-independent inference failure."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_origin(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise InferenceError("inference route rejected") from None
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or port != 4000 or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != "/v1/chat/completions"):
        raise InferenceError("inference route rejected")


def _safe_failure() -> InferenceError:
    return InferenceError("inference request failed")


@dataclass
class LiteLLMInferenceAdapter:
    """Callable adapter for ``InferenceProxy``; the key never enters child RPC."""

    api_key: str = field(repr=False)
    model: str = MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT
    max_response_bytes: int = MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key:
            raise ValueError("invalid inference key")
        if self.model != MODEL:
            raise ValueError("invalid inference model")
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 1_048_576:
            raise ValueError("invalid inference token limit")
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or not 0 < self.timeout <= 300):
            raise ValueError("invalid inference timeout")
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= 16_777_216:
            raise ValueError("invalid inference response limit")
        self._usage: dict[str, int] = {}
        self._receipt: dict[str, object] = {}

    def __call__(self, *, route: str, prompt: str, max_output_tokens: int,
                 timeout: float) -> str:
        self._usage = {}
        self._receipt = {}
        if route != MODEL:
            raise InferenceError("inference route rejected")
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 1_048_576:
            raise ValueError("invalid inference token limit")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 300):
            raise ValueError("invalid inference timeout")
        if not isinstance(prompt, str) or not prompt:
            raise _safe_failure()
        if self.api_key in prompt:
            raise _safe_failure()
        effective_max_tokens = min(self.max_tokens, max_output_tokens)
        effective_timeout = min(self.timeout, timeout)
        try:
            _validate_origin(INFERENCE_URL)
        except InferenceError:
            raise _safe_failure()
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": effective_max_tokens,
            "reasoning_effort": "low",
        }
        request = Request(
            INFERENCE_URL,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        deadline = time.monotonic() + effective_timeout
        for attempt in range(1, 3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _safe_failure()
            self._receipt["attempt_count"] = attempt
            try:
                with opener.open(request, timeout=remaining) as response:
                    if response.status != 200:
                        raise _safe_failure()
                    body = response.read(self.max_response_bytes + 1)
                    if len(body) > self.max_response_bytes:
                        raise _safe_failure()
                document = json.loads(body.decode("utf-8"))
                choice_data = document["choices"][0]
                message = choice_data["message"]
                choice = message["content"]
                finish_reason = choice_data.get("finish_reason")
                usage = document.get("usage", {})
                self._receipt.update({
                    "finish_reason": finish_reason if isinstance(finish_reason, str) else None,
                    "content_type": type(choice).__name__,
                    "content_length": len(choice) if isinstance(choice, str) else None,
                    "usage": self._safe_usage(usage),
                })
                if finish_reason == "length":
                    raise InferenceError("output_budget_exhausted")
                if type(choice) is not str or self.api_key in choice or not isinstance(usage, dict):
                    raise _safe_failure()
                if choice:
                    self._usage = dict(self._receipt["usage"])
                    return choice
                if attempt == 2:
                    raise _safe_failure()
                # Only an otherwise valid empty 200 response is retryable.
                continue
            except InferenceError:
                raise
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, IndexError,
                    UnicodeError, TypeError, json.JSONDecodeError):
                raise _safe_failure() from None

        raise _safe_failure()

    @staticmethod
    def _safe_usage(usage: object) -> dict[str, int]:
        if not isinstance(usage, dict):
            return {}
        return {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if type(usage.get(key)) is int and usage[key] >= 0}

    def usage_receipt(self) -> dict[str, int]:
        """Return provider usage only; prompts and response text are excluded."""
        return dict(self._usage)

    def diagnostic_receipt(self) -> dict[str, object]:
        """Return bounded response metadata without prompts, text, or failures."""
        return dict(self._receipt)


# Descriptive alias for callers that prefer the shorter name.
InferenceAdapter = LiteLLMInferenceAdapter
