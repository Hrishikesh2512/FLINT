from types import SimpleNamespace

from core.capture_engine import Frame
from core.vision_service import VisionService, parse_screen_coordinates


class FakeGateway:
    def __init__(self, text="ok"):
        self.text = text
        self.calls = []

    def vision(self, prompt, image_b64, mime="image/png", **kwargs):
        self.calls.append(
            {
                "prompt": prompt,
                "image_b64": image_b64,
                "mime": mime,
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(text=self.text)


class FakeCaptureEngine:
    def __init__(self):
        self.calls = []
        self.frame = Frame(b"jpeg-bytes", mime="image/jpeg")

    def capture_screen(self, force=False):
        self.calls.append({"force": force})
        return self.frame


def test_parse_screen_coordinates_accepts_plain_pair():
    assert parse_screen_coordinates("120, 240", width=500, height=500) == (120, 240)


def test_parse_screen_coordinates_rejects_not_found_and_out_of_bounds():
    assert parse_screen_coordinates("NOT_FOUND", width=500, height=500) is None
    assert parse_screen_coordinates("-1, 10", width=500, height=500) is None
    assert parse_screen_coordinates("700, 10", width=500, height=500) is None
    assert parse_screen_coordinates("10, 700", width=500, height=500) is None


def test_locate_on_screen_uses_capture_and_parses_response(monkeypatch):
    gateway = FakeGateway("42, 84")
    capture = FakeCaptureEngine()
    monkeypatch.setattr("core.vision_service._screen_size", lambda: (1920, 1080))

    service = VisionService(gateway=gateway, capture_engine=capture)

    assert service.locate_on_screen("submit button") == (42, 84)
    assert capture.calls == [{"force": False}]
    call = gateway.calls[0]
    assert call["image_b64"] == capture.frame.b64
    assert call["mime"] == "image/jpeg"
    assert "submit button" in call["prompt"]
    assert "1920x1080" in call["prompt"]


def test_debug_screen_includes_related_file_and_requests_more_tokens():
    gateway = FakeGateway("analysis")
    capture = FakeCaptureEngine()
    service = VisionService(gateway=gateway, capture_engine=capture)

    result = service.debug_screen("why failed?", related_file_content="print('x')")

    assert result == "analysis"
    call = gateway.calls[0]
    assert "why failed?" in call["prompt"]
    assert "print('x')" in call["prompt"]
    assert call["kwargs"]["max_tokens"] == 4096
