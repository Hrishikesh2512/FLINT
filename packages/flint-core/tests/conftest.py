"""Shared fakes: a scriptable transport so no test touches the network."""

from __future__ import annotations

from typing import Any

import pytest

from flint_core.llm.base import TransportResult


class FakeTransport:
    """Returns queued responses in order; records every request it saw."""

    def __init__(self, *responses: TransportResult):
        self.queue = list(responses)
        self.requests: list[dict[str, Any]] = []

    def __call__(
        self, url: str, headers: dict, payload: dict, timeout: float
    ) -> TransportResult:
        self.requests.append({"url": url, "headers": headers, "payload": payload})
        if not self.queue:
            raise AssertionError(f"unexpected extra request to {url}")
        return self.queue.pop(0)


def gemini_ok(text: str) -> TransportResult:
    return TransportResult(
        200, {"candidates": [{"content": {"parts": [{"text": text}]}}]}
    )


def openai_ok(text: str) -> TransportResult:
    return TransportResult(200, {"choices": [{"message": {"content": text}}]})


def anthropic_ok(text: str) -> TransportResult:
    return TransportResult(200, {"content": [{"type": "text", "text": text}]})


def http(status: int, body: dict | None = None) -> TransportResult:
    return TransportResult(status, body or {})


@pytest.fixture()
def fake_clock():
    class Clock:
        now = 1000.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds

    return Clock()
