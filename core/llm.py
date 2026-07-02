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
