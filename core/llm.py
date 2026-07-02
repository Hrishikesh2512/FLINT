"""App-side access to the flint-core LLM gateway.

Every module that needs a model goes through get_gateway() — one provider
chain, one failover policy, one place keys are read. Replaces the old
trio of google.generativeai / google-genai one-offs / or_client.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from flint_core.config import build_gateway, load_settings
from flint_core.llm import LLMGateway


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


LEGACY_KEYS_PATH = _base_dir() / "config" / "api_keys.json"

_gateway: LLMGateway | None = None
_lock = threading.Lock()


def get_gateway() -> LLMGateway:
    global _gateway
    with _lock:
        if _gateway is None:
            settings = load_settings(legacy_json=LEGACY_KEYS_PATH)
            _gateway = build_gateway(settings)
        return _gateway


def reset_gateway() -> None:
    """Drop the cached gateway (after the user changes API keys in setup)."""
    global _gateway
    with _lock:
        _gateway = None


class GatewayModel:
    """Drop-in for the legacy ``google.generativeai.GenerativeModel`` surface.

    Supports the two call shapes the v1 actions use:
        model.generate_content("prompt")                       — text
        model.generate_content([prompt, pil_image, ...])       — multimodal
    and returns the gateway's LLMResponse, which has the same ``.text``
    attribute call sites already read. Lets each action module migrate off
    the deprecated SDK without rewriting every call site; the modules
    themselves get rewritten as plugins in a later phase.
    """

    def __init__(self, model: str | None = None, system: str = "",
                 max_tokens: int = 8192, temperature: float = 0.4):
        self._model = model
        self._system = system
        self._max_tokens = max_tokens
        self._temperature = temperature

    def generate_content(self, content):
        gateway = get_gateway()
        if isinstance(content, str):
            return gateway.chat(
                content, system=self._system, model=self._model,
                max_tokens=self._max_tokens, temperature=self._temperature,
            )

        texts: list[str] = []
        image_b64: str | None = None
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif hasattr(part, "save"):  # PIL.Image.Image
                import base64
                import io

                buffer = io.BytesIO()
                part.save(buffer, format="PNG")
                image_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
            else:
                raise TypeError(f"unsupported content part: {type(part).__name__}")

        prompt = "\n".join(texts)
        if image_b64 is None:
            return gateway.chat(
                prompt, system=self._system, model=self._model,
                max_tokens=self._max_tokens, temperature=self._temperature,
            )
        return gateway.vision(
            prompt, image_b64, "image/png", system=self._system,
            max_tokens=min(self._max_tokens, 4096), temperature=self._temperature,
        )
