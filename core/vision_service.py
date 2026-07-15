"""Shared text-vision helpers for FLINT actions.

This module centralizes the non-live vision paths: screen debugging, locating
UI elements, and any future text response over a captured frame. The separate
Gemini Live audio session in actions.screen_processor stays independent for
now, but it can reuse this capture policy later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from core.capture_engine import Frame, get_engine


@dataclass(frozen=True)
class VisionCapture:
    frame: Frame
    width: int | None = None
    height: int | None = None
    source: str = "screen"


class _Gateway(Protocol):
    def vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str = "image/png",
        **kwargs,
    ):
        ...


def _screen_size() -> tuple[int | None, int | None]:
    try:
        import pyautogui

        w, h = pyautogui.size()
        return int(w), int(h)
    except Exception:
        return None, None


class VisionService:
    def __init__(self, gateway: _Gateway | None = None, capture_engine=None):
        self._gateway = gateway
        self._capture_engine = capture_engine

    @property
    def gateway(self) -> _Gateway:
        if self._gateway is not None:
            return self._gateway
        from core.llm import get_gateway

        return get_gateway()

    @property
    def capture_engine(self):
        if self._capture_engine is not None:
            return self._capture_engine
        return get_engine()

    def capture_screen(self, *, force: bool = False) -> VisionCapture:
        frame = self.capture_engine.capture_screen(force=force)
        w, h = _screen_size()
        return VisionCapture(frame=frame, width=w, height=h)

    def ask(
        self,
        prompt: str,
        capture: VisionCapture | None = None,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> str:
        capture = capture or self.capture_screen()
        response = self.gateway.vision(
            prompt,
            capture.frame.b64,
            capture.frame.mime,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.text.strip()

    def locate_on_screen(self, description: str) -> tuple[int, int] | None:
        capture = self.capture_screen()
        w, h = capture.width, capture.height
        bounds = f"{w},{h}" if w and h else "the full screen bounds"
        size = f" of a {w}x{h} pixel screen" if w and h else ""

        prompt = (
            f"This is a downscaled screenshot{size}. "
            f"Locate the UI element: '{description}'. "
            "Reply ONLY with its center in FULL-SCREEN coordinates "
            f"(0,0 top-left, {bounds} bottom-right) as: x,y - or NOT_FOUND"
        )
        text = self.ask(prompt, capture)
        return parse_screen_coordinates(text, width=w, height=h)

    def debug_screen(
        self,
        user_question: str,
        *,
        related_file_content: str = "",
    ) -> str:
        context = ""
        if related_file_content:
            context = (
                "\n\nAdditionally, here is the related file content:\n"
                f"```\n{related_file_content[:4000]}\n```"
            )

        prompt = f"""You are an expert programmer and debugger analyzing a screenshot.

User's question: {user_question}{context}

Please:
1. Identify any errors, exceptions, or problems visible on the screen
2. Explain what is causing the problem in simple terms
3. Provide a concrete fix or solution
4. If there's code visible, show the corrected version

Be specific and actionable. If you see an error message, quote it exactly."""

        return self.ask(prompt, max_tokens=4096)


def parse_screen_coordinates(
    text: str,
    *,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int] | None:
    if not text or "NOT_FOUND" in text.upper():
        return None
    match = re.search(r"(-?\d+)\s*,\s*(-?\d+)", text)
    if not match:
        return None
    x, y = int(match.group(1)), int(match.group(2))
    if x < 0 or y < 0:
        return None
    if width is not None and x > width:
        return None
    if height is not None and y > height:
        return None
    return x, y


_service: VisionService | None = None


def get_vision_service() -> VisionService:
    global _service
    if _service is None:
        _service = VisionService()
    return _service


def reset_vision_service() -> None:
    global _service
    _service = None
