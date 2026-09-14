"""Minimal OpenAI-compatible JSON generation backend."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .backend import EXPECTED_KEYS
from .structured_output import parse_and_validate


def resolve_api_base(explicit: str | None) -> str | None:
    """Resolve a vLLM/OpenAI-compatible endpoint without hard-coding servers."""
    value = explicit or os.environ.get("AIOPS_LLM_API_BASE")
    return value.rstrip("/") if value else None


@dataclass(frozen=True, slots=True)
class ApiConfig:
    base_url: str
    model: str
    api_key_env: str = "AIOPS_LLM_API_KEY"
    timeout: float = 60.0
    retries: int = 2
    retry_backoff: float = 1.0


class ApiBackend:
    """Call a Chat Completions endpoint without adding an SDK dependency."""

    def __init__(self, config: ApiConfig, prompt_dir: Path):
        self.config = config
        self.prompt_dir = prompt_dir

    def _endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _render_prompt(self, name: str, payload: dict[str, Any]) -> str:
        template = (self.prompt_dir / f"{name}.txt").read_text(encoding="utf-8")
        return template + "\n\nINPUT_JSON:\n" + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )

    @staticmethod
    def _content(response: Any) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("API response is missing choices[0].message.content") from exc
        if not isinstance(content, str):
            raise ValueError("API response content must be a string")
        return content

    def generate_json(
        self,
        *,
        role: str,
        prompt_name: str,
        payload: dict[str, Any],
        validator: Callable[[Any], dict[str, Any]],
        max_new_tokens: int,
    ) -> dict[str, Any]:
        api_key = os.environ.get(self.config.api_key_env, "")
        if not api_key:
            raise RuntimeError(f"API key environment variable is not set: {self.config.api_key_env}")
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": [
                    {"role": "user", "content": self._render_prompt(prompt_name, payload)}
                ],
                "temperature": 0,
                "max_tokens": max_new_tokens,
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        last_error = "unknown error"
        for attempt in range(self.config.retries + 1):
            request = Request(
                self._endpoint(),
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urlopen(request, timeout=self.config.timeout) as response:
                    envelope = json.loads(response.read().decode("utf-8"))
                return parse_and_validate(
                    self._content(envelope),
                    expected_key=EXPECTED_KEYS.get(prompt_name),
                    validator=validator,
                )
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                # Deliberately retain only the exception type and bounded message;
                # request headers and the API key never enter diagnostics.
                last_error = f"{type(exc).__name__}: {' '.join(str(exc).splitlines())[:300]}"
                if attempt < self.config.retries and self.config.retry_backoff > 0:
                    time.sleep(self.config.retry_backoff * (attempt + 1))
        raise ValueError(
            f"{role}/{prompt_name} API generation failed after "
            f"{self.config.retries + 1} attempts: {last_error}"
        )
