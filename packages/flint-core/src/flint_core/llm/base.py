"""Provider-agnostic LLM primitives.

A Provider turns (messages, model) into text over one vendor's REST API.
All providers share one injected Transport so tests never touch the network
and timeouts/retries behave uniformly.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

Role = str  # "system" | "user" | "assistant"


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant"):
            raise ValueError(f"invalid role: {self.role!r}")


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str


class ProviderError(Exception):
    """The provider failed for this request (HTTP error, bad payload, empty reply)."""


class RateLimitedError(ProviderError):
    """HTTP 429 — the gateway puts this (provider, model) on cooldown."""


@dataclass(frozen=True)
class TransportResult:
    status: int
    body: dict[str, Any]


# (url, headers, json_payload, timeout_seconds) -> TransportResult
Transport = Callable[[str, dict[str, str], dict[str, Any], float], TransportResult]


class Provider(Protocol):
    name: str
    models: tuple[str, ...]

    def complete(
        self,
        messages: Sequence[ChatMessage],
        model: str,
        *,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str: ...


def split_system(messages: Sequence[ChatMessage]) -> tuple[str, list[ChatMessage]]:
    """Separate system text (many APIs take it out-of-band) from the turn list."""
    system_parts = [m.content for m in messages if m.role == "system"]
    rest = [m for m in messages if m.role != "system"]
    return "\n\n".join(system_parts), rest


def strip_code_fences(text: str) -> str:
    """Remove markdown code fences that models wrap around JSON/code output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        if first_newline != -1:
            cleaned = cleaned[first_newline + 1 :]
    if cleaned.endswith("```"):
        cleaned = cleaned[: -3]
    return cleaned.strip()
