"""The LLM gateway — one door to every model FLINT can think with.

Callers say *what* they want (chat, JSON); the gateway decides *where* it
runs: providers are tried in the order given (laptop/preferred first), each
provider's models in its own order. Rate-limited (provider, model) pairs sit
out a cooldown window instead of being hammered.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence

from flint_core.llm.base import (
    ChatMessage,
    LLMResponse,
    Provider,
    ProviderError,
    RateLimitedError,
    strip_code_fences,
)
from flint_core.llm.routing import Router, classify

log = logging.getLogger("flint.llm")


class AllProvidersFailedError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        summary = "; ".join(errors[:5]) or "no providers configured"
        super().__init__(f"all LLM providers failed: {summary}")


class LLMGateway:
    def __init__(
        self,
        providers: Sequence[Provider],
        rate_limit_cooldown: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        router: Router | None = None,
    ):
        if not providers:
            raise ValueError("LLMGateway needs at least one provider")
        self._providers = list(providers)
        self._cooldown = rate_limit_cooldown
        self._clock = clock
        self._limited_until: dict[tuple[str, str], float] = {}
        # Optional: orders (provider, model) by what the request is *for*.
        # Without one, behaviour is exactly as before — configured order,
        # first thing that answers.
        self._router = router

    # ── public API ───────────────────────────────────────────────────────────
    def chat(
        self,
        prompt: str | None = None,
        *,
        messages: Sequence[ChatMessage] | None = None,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        json_mode: bool = False,
        task: str | None = None,
    ) -> LLMResponse:
        """`task` (see flint_core.llm.routing.Task) picks the model by what the
        work *is* — "chat", "reasoning", "code", "bulk". Pass "auto" to have it
        guessed from the prompt. Omit it and nothing changes: configured order,
        first provider that answers.
        """
        if (prompt is None) == (messages is None):
            raise ValueError("pass exactly one of prompt= or messages=")
        turns: list[ChatMessage] = []
        if system:
            turns.append(ChatMessage("system", system))
        turns.extend([ChatMessage("user", prompt)] if prompt is not None else messages)

        errors: list[str] = []
        for provider, candidate_model in self._attempt_order(turns, model, task):
            if self._on_cooldown(provider.name, candidate_model):
                continue
            try:
                text = provider.complete(
                    turns,
                    candidate_model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    json_mode=json_mode,
                )
                return LLMResponse(text=text, provider=provider.name, model=candidate_model)
            except RateLimitedError as exc:
                self._mark_limited(provider.name, candidate_model)
                errors.append(str(exc))
            except ProviderError as exc:
                log.warning("%s", exc)
                errors.append(str(exc))
        raise AllProvidersFailedError(errors)

    def chat_json(
        self,
        prompt: str,
        *,
        system: str = "Return ONLY valid JSON. No markdown, no explanation.",
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.2,
        task: str | None = None,
    ) -> dict | list:
        response = self.chat(
            prompt,
            system=system,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=True,
            task=task,
        )
        cleaned = strip_code_fences(response.text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{response.provider}/{response.model} returned unparseable JSON: {exc}\n"
                f"First 200 chars: {response.text[:200]}"
            ) from exc

    def vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str = "image/png",
        *,
        system: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Ask about an image; routed to the first vision-capable provider/model."""
        errors: list[str] = []
        for provider in self._providers:
            models = getattr(provider, "vision_models", ())
            for candidate_model in models:
                if self._on_cooldown(provider.name, candidate_model):
                    continue
                try:
                    text = provider.complete_vision(
                        prompt,
                        image_b64,
                        mime,
                        candidate_model,
                        system=system,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    return LLMResponse(text=text, provider=provider.name, model=candidate_model)
                except RateLimitedError as exc:
                    self._mark_limited(provider.name, candidate_model)
                    errors.append(str(exc))
                except ProviderError as exc:
                    log.warning("%s", exc)
                    errors.append(str(exc))
        raise AllProvidersFailedError(errors or ["no vision-capable provider configured"])

    # ── internals ────────────────────────────────────────────────────────────
    def _attempt_order(self, turns: Sequence[ChatMessage], requested: str | None,
                       task: str | None) -> list[tuple[Provider, str]]:
        """Every (provider, model) worth trying, best first.

        Routed pairs come first when a task is given; everything else follows
        in the configured order. Nothing is ever dropped — routing decides
        what to try *first*, never what to rule out, so a routed model being
        down or rate-limited still falls through to whatever is up.
        """
        fallback = [(p, m) for p in self._providers
                    for m in self._model_order(p, requested)]
        if task is None or self._router is None:
            return fallback

        if task == "auto":
            text = "\n".join(t.content for t in turns if t.role == "user")
            task = classify(text)
            log.debug("routing: classified as %s", task)

        by_name = {p.name: p for p in self._providers}
        preferred: list[tuple[Provider, str]] = []
        for spec in self._router.candidates(task):
            provider = by_name.get(spec.provider)
            if provider is not None:
                preferred.append((provider, spec.model))
        if preferred:
            log.debug("routing: %s -> %s/%s", task,
                      preferred[0][0].name, preferred[0][1])
        seen = set()
        ordered = []
        for pair in preferred + fallback:
            key = (pair[0].name, pair[1])
            if key not in seen:
                seen.add(key)
                ordered.append(pair)
        return ordered

    def _model_order(self, provider: Provider, requested: str | None) -> list[str]:
        models = list(provider.models)
        if requested and requested in models:
            models.remove(requested)
            models.insert(0, requested)
        return models

    def _on_cooldown(self, provider: str, model: str) -> bool:
        until = self._limited_until.get((provider, model))
        if until is None:
            return False
        if self._clock() >= until:
            del self._limited_until[(provider, model)]
            return False
        return True

    def _mark_limited(self, provider: str, model: str) -> None:
        self._limited_until[(provider, model)] = self._clock() + self._cooldown
        log.warning("%s/%s rate limited — cooling down %.0fs", provider, model, self._cooldown)
