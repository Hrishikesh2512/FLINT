"""USB headset detection via ALSA's /proc/asound/cards.

No external dependencies: the kernel exposes registered sound cards as a
small text file. A USB headset shows up with "USB-Audio" in its
description line, e.g.:

     0 [Headphones     ]: bcm2835_headphonbcm2835 Headphones - bcm2835 Headphones
     1 [Device         ]: USB-Audio - USB PnP Sound Device
                          C-Media Electronics Inc. USB PnP Sound Device at usb-...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ASOUND_CARDS = Path("/proc/asound/cards")

_CARD_RE = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(.+)$")


@dataclass(frozen=True)
class AudioCard:
    index: int
    id: str
    description: str

    @property
    def is_usb(self) -> bool:
        return "usb" in self.description.lower()


def parse_asound_cards(text: str) -> list[AudioCard]:
    """Parse the /proc/asound/cards format into AudioCard entries."""
    cards: list[AudioCard] = []
    for line in text.splitlines():
        match = _CARD_RE.match(line)
        if match:
            cards.append(
                AudioCard(
                    index=int(match.group(1)),
                    id=match.group(2),
                    description=match.group(3).strip(),
                )
            )
    return cards


def list_audio_cards(cards_file: Path = ASOUND_CARDS) -> list[AudioCard] | None:
    """All registered sound cards, or None where ALSA doesn't exist (dev boxes)."""
    try:
        text = cards_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return parse_asound_cards(text)


def find_usb_audio(cards_file: Path = ASOUND_CARDS) -> AudioCard | None:
    """The first USB audio device (the wearable's headset), if present."""
    cards = list_audio_cards(cards_file)
    if not cards:
        return None
    for card in cards:
        if card.is_usb:
            return card
    return None
