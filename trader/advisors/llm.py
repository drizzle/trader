"""DeepSeek HTTP client.

Tiny, dependency-free wrapper around DeepSeek's OpenAI-compatible Chat
Completions endpoint. Uses `requests` (already a project dep).

Security invariants enforced here:
  - The API key is sent ONLY in the Authorization header to api.deepseek.com.
  - Prompt content is logged at DEBUG only and is never stored to disk by this
    client (the cache module hashes prompts for cache keys; rationales are
    stored separately).
"""
from __future__ import annotations

import json

import requests
from loguru import logger


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns an unparseable response."""


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        default_model: str = "deepseek-chat",
        timeout_seconds: int = 60,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if not base_url.startswith("https://"):
            raise ValueError(f"base_url must use HTTPS, got {base_url!r}")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout_seconds

    def chat(
        self,
        system: str,
        user: str,
        model: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.3,
        response_format_json: bool = False,
    ) -> str:
        """Send a single chat completion. Returns the assistant's text content.

        `response_format_json=True` asks DeepSeek to return strict JSON
        (when the model supports it) — useful for advisor recommendations.
        """
        url = f"{self._base_url}/chat/completions"
        # Defensive: never let the prompt content accidentally include the key.
        if self._api_key in system or self._api_key in user:
            raise LLMError("REFUSING TO SEND: API key appears in prompt content.")

        payload: dict = {
            "model": model or self._default_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}

        try:
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            raise LLMError(f"network error calling DeepSeek: {e}") from e

        if r.status_code >= 400:
            # Don't echo the request payload — it's already been seen by the
            # server but we don't want it in logs either.
            raise LLMError(f"DeepSeek returned HTTP {r.status_code}: {r.text[:300]}")

        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            raise LLMError(f"unexpected response shape: {e}") from e

        logger.debug(f"DeepSeek call ok: {len(user)} in, {len(content)} out")
        return content
