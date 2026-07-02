"""Audio device auto-selection — the headset must Just Work, headless.

Policy: prefer a USB device (the wearable's headset), else the system
default. Selection logic is pure (testable with fake device tables);
only query_devices() touches sounddevice.
"""

from __future__ import annotations

from dataclasses import dataclass

MIC_SAMPLE_RATE = 16000      # what Gemini Live expects inbound
SPEAKER_SAMPLE_RATE = 24000  # what Gemini Live produces outbound
CHANNELS = 1
MIC_BLOCK = 1024             # frames per mic callback (64 ms @ 16 kHz)


@dataclass(frozen=True)
class DevicePick:
    input_index: int | None   # None = library default
    output_index: int | None
    input_name: str
    output_name: str


# Tried in order: a USB device always beats the Pi's built-in
# "bcm2835 Headphones" jack, which would otherwise match "headphone".
_HINT_TIERS = (("usb",), ("headset",))


def pick_devices(devices: list[dict]) -> DevicePick:
    """Choose input/output devices from a sounddevice.query_devices() table."""

    def find(kind: str) -> tuple[int | None, str]:
        key = f"max_{kind}_channels"
        candidates = [
            (index, dev) for index, dev in enumerate(devices) if dev.get(key, 0) > 0
        ]
        for tier in _HINT_TIERS:
            for index, dev in candidates:
                name = str(dev.get("name", "")).lower()
                if any(hint in name for hint in tier):
                    return index, str(dev.get("name", ""))
        if candidates:
            return None, "(system default)"
        return None, "(none found)"

    in_index, in_name = find("input")
    out_index, out_name = find("output")
    return DevicePick(in_index, out_index, in_name, out_name)


def current_devices() -> DevicePick:
    import sounddevice as sd

    table = [dict(d) for d in sd.query_devices()]
    return pick_devices(table)
