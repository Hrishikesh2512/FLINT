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


# Hint tiers tried in order. Both Bluetooth and USB headsets flow through
# PipeWire, whose ALSA plugin shows up as a "pipewire" (or "pulse"-compat)
# device; picking it lets PipeWire resample to whatever the hardware wants
# (many cheap USB DACs reject our 16/24 kHz rates on a direct hw open). We
# pin the right node as PipeWire's default separately (see audio/routing.py).
# A raw "usb"/"headset" device is the fallback on a PipeWire-less box.
_TIERS_BLUETOOTH = (("pipewire",), ("pulse",), ("default",))
_TIERS_USB = (("pipewire",), ("pulse",), ("usb",), ("headset",))


def pick_devices(devices: list[dict], bluetooth: bool = False) -> DevicePick:
    """Choose input/output devices from a sounddevice.query_devices() table."""
    tiers = _TIERS_BLUETOOTH if bluetooth else _TIERS_USB

    def find(kind: str) -> tuple[int | None, str]:
        key = f"max_{kind}_channels"
        candidates = [
            (index, dev) for index, dev in enumerate(devices) if dev.get(key, 0) > 0
        ]
        for tier in tiers:
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


def current_devices(bluetooth: bool = False) -> DevicePick:
    import sounddevice as sd

    table = [dict(d) for d in sd.query_devices()]
    return pick_devices(table, bluetooth=bluetooth)
