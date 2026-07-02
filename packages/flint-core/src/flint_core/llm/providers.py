"""Concrete LLM providers, all speaking plain REST through one Transport.

- GeminiProvider        Google Generative Language API (generateContent)
- OpenAICompatProvider  anything with an OpenAI-style /chat/completions:
                        OpenAI, Groq, OpenRouter, a local llama.cpp server...
- AnthropicProvider     Anthropic /v1/messages
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from flint_core.llm.base import (
    ChatMessage,
    ProviderError,
    RateLimitedError,
    Transport,
    split_system,
)
from flint_core.llm.transport import requests_transport

DEFAULT_TIMEOUT = 60.0


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProviderError(message)


class GeminiProvider:
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: str,
        models: Sequence[str] = ("gemini-2.5-flash", "gemini-2.5-flash-lite"),
        transport: Transport = requests_transport,
        timeout: float = DEFAULT_TIMEOUT,
        thinking_budget: int | None = 0,
    ):
        _require(bool(api_key), "gemini: api key is empty")
        self.name = "gemini"
        self.models = tuple(models)
        self._key = api_key
        self._transport = transport
        self._timeout = timeout
        # Gemini 2.5 "thinking" tokens are billed against maxOutputTokens and
        # can silently truncate small utility replies — off by default here;
        # pass thinking_budget=None to leave the model's default on.
        self._thinking_budget = thinking_budget

    def complete(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        system, turns = split_system(messages)
        role_map = {"user": "user", "assistant": "model"}
        payload: dict[str, Any] = {
            "contents": [
                {"role": role_map[m.role], "parts": [{"text": m.content}]} for m in turns
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        if self._thinking_budget is not None:
            payload["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": self._thinking_budget
            }

        result = self._transport(
            f"{self.BASE_URL}/models/{model}:generateContent",
            {"x-goog-api-key": self._key, "Content-Type": "application/json"},
            payload,
            self._timeout,
        )
        if result.status == 429:
            raise RateLimitedError(f"gemini/{model}: rate limited")
        if result.status != 200:
            raise ProviderError(f"gemini/{model}: HTTP {result.status}: {result.body}")

        candidates = result.body.get("candidates") or []
        _require(bool(candidates), f"gemini/{model}: no candidates in response")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        _require(bool(text), f"gemini/{model}: empty response text")
        return text

    @property
    def vision_models(self) -> tuple[str, ...]:
        return self.models  # Gemini flash models are natively multimodal

    def grounded_search(self, query: str, model: str | None = None,
                        max_tokens: int = 2048) -> str:
        """Answer with Google Search grounding — a real web search, not recall."""
        model = model or self.models[0]
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        result = self._transport(
            f"{self.BASE_URL}/models/{model}:generateContent",
            {"x-goog-api-key": self._key, "Content-Type": "application/json"},
            payload,
            self._timeout,
        )
        if result.status == 429:
            raise RateLimitedError(f"gemini/{model}: rate limited")
        if result.status != 200:
            raise ProviderError(f"gemini/{model}: HTTP {result.status}: {result.body}")
        candidates = result.body.get("candidates") or []
        _require(bool(candidates), f"gemini/{model}: no candidates in response")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        _require(bool(text), f"gemini/{model}: empty response text")
        return text

    def complete_vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str,
        model: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        payload: dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inline_data": {"mime_type": mime, "data": image_b64}},
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        if self._thinking_budget is not None:
            payload["generationConfig"]["thinkingConfig"] = {
                "thinkingBudget": self._thinking_budget
            }

        result = self._transport(
            f"{self.BASE_URL}/models/{model}:generateContent",
            {"x-goog-api-key": self._key, "Content-Type": "application/json"},
            payload,
            self._timeout,
        )
        if result.status == 429:
            raise RateLimitedError(f"gemini/{model}: rate limited")
        if result.status != 200:
            raise ProviderError(f"gemini/{model}: HTTP {result.status}: {result.body}")
        candidates = result.body.get("candidates") or []
        _require(bool(candidates), f"gemini/{model}: no candidates in response")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        _require(bool(text), f"gemini/{model}: empty response text")
        return text


class OpenAICompatProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        models: Sequence[str],
        supports_json_response: bool = True,
        vision_models: Sequence[str] = (),
        extra_headers: dict[str, str] | None = None,
        transport: Transport = requests_transport,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        _require(bool(api_key), f"{name}: api key is empty")
        _require(bool(models), f"{name}: needs at least one model")
        self.name = name
        self.models = tuple(models)
        self.vision_models = tuple(vision_models)
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **(extra_headers or {}),
        }
        self._supports_json = supports_json_response
        self._transport = transport
        self._timeout = timeout

    def complete(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode and self._supports_json:
            payload["response_format"] = {"type": "json_object"}

        result = self._transport(self._url, self._headers, payload, self._timeout)
        if result.status == 429:
            raise RateLimitedError(f"{self.name}/{model}: rate limited")
        if result.status != 200:
            raise ProviderError(f"{self.name}/{model}: HTTP {result.status}: {result.body}")

        choices = result.body.get("choices") or []
        _require(bool(choices), f"{self.name}/{model}: no choices in response")
        text = (choices[0].get("message") or {}).get("content") or ""
        text = text.strip()
        _require(bool(text), f"{self.name}/{model}: empty response text")
        return text

    def complete_vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str,
        model: str,
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        )
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        result = self._transport(self._url, self._headers, payload, self._timeout)
        if result.status == 429:
            raise RateLimitedError(f"{self.name}/{model}: rate limited")
        if result.status != 200:
            raise ProviderError(f"{self.name}/{model}: HTTP {result.status}: {result.body}")
        choices = result.body.get("choices") or []
        _require(bool(choices), f"{self.name}/{model}: no choices in response")
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        _require(bool(text), f"{self.name}/{model}: empty response text")
        return text


class AnthropicProvider:
    BASE_URL = "https://api.anthropic.com/v1/messages"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: str,
        models: Sequence[str] = ("claude-haiku-4-5-20251001",),
        transport: Transport = requests_transport,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        _require(bool(api_key), "anthropic: api key is empty")
        self.name = "anthropic"
        self.models = tuple(models)
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": self.API_VERSION,
            "Content-Type": "application/json",
        }
        self._transport = transport
        self._timeout = timeout

    def complete(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        system, turns = split_system(messages)
        if json_mode:
            system = (system + "\n\nReturn ONLY valid JSON. No markdown, no extra text.").strip()
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": m.role, "content": m.content} for m in turns],
        }
        if system:
            payload["system"] = system

        result = self._transport(self.BASE_URL, self._headers, payload, self._timeout)
        if result.status == 429:
            raise RateLimitedError(f"anthropic/{model}: rate limited")
        if result.status != 200:
            raise ProviderError(f"anthropic/{model}: HTTP {result.status}: {result.body}")

        blocks = result.body.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        _require(bool(text), f"anthropic/{model}: empty response text")
        return text
