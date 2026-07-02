"""Typed configuration for the Venom daemon.

Read from a TOML file (default /etc/venom/venom.toml, override with
VENOM_CONFIG or --config). Every field has a working default so the daemon
boots on a freshly provisioned Pi with no config file at all.

Example venom.toml:

    [venom]
    poll_interval = 10.0
    status_path = "/run/venom/status.json"

    [internet]
    host = "1.1.1.1"
    port = 53

    [[brain]]
    name = "laptop"
    host = "192.168.1.50"
    port = 8765
    priority = 0

    [[brain]]
    name = "gemini"
    host = "generativelanguage.googleapis.com"
    port = 443
    priority = 10
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/venom/venom.toml")


@dataclass(frozen=True)
class BrainCandidate:
    """A place the wearable can send its audio/requests to."""

    name: str
    host: str
    port: int
    priority: int = 100  # lower wins

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("brain candidate needs a name")
        if not self.host:
            raise ValueError(f"brain candidate {self.name!r} needs a host")
        if not (0 < self.port < 65536):
            raise ValueError(f"brain candidate {self.name!r} has invalid port {self.port}")


# Cloud fallbacks that exist even with an empty config file: if the laptop
# is not configured or not reachable, any of these being reachable means
# "online, cloud brain available".
DEFAULT_CLOUD_CANDIDATES: tuple[BrainCandidate, ...] = (
    BrainCandidate("gemini", "generativelanguage.googleapis.com", 443, priority=10),
    BrainCandidate("groq", "api.groq.com", 443, priority=11),
    BrainCandidate("openai", "api.openai.com", 443, priority=12),
    BrainCandidate("anthropic", "api.anthropic.com", 443, priority=13),
    BrainCandidate("openrouter", "openrouter.ai", 443, priority=14),
)


@dataclass(frozen=True)
class VenomConfig:
    poll_interval: float = 10.0
    probe_timeout: float = 3.0
    status_path: Path = Path("/run/venom/status.json")
    internet_host: str = "1.1.1.1"
    internet_port: int = 53
    brains: tuple[BrainCandidate, ...] = field(default=DEFAULT_CLOUD_CANDIDATES)

    def __post_init__(self) -> None:
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if self.probe_timeout <= 0:
            raise ValueError("probe_timeout must be positive")
        if not self.brains:
            raise ValueError("at least one brain candidate is required")


def _parse_brains(raw: list[dict]) -> tuple[BrainCandidate, ...]:
    brains = [
        BrainCandidate(
            name=str(entry.get("name", "")),
            host=str(entry.get("host", "")),
            port=int(entry.get("port", 0)),
            priority=int(entry.get("priority", 100)),
        )
        for entry in raw
    ]
    return tuple(sorted(brains, key=lambda b: b.priority))


def load_config(path: Path | None = None) -> VenomConfig:
    """Load config from TOML; missing file or missing keys fall back to defaults."""
    if path is None:
        path = Path(os.environ.get("VENOM_CONFIG", str(DEFAULT_CONFIG_PATH)))

    if not path.exists():
        return VenomConfig()

    with open(path, "rb") as fh:
        data = tomllib.load(fh)

    venom = data.get("venom", {})
    internet = data.get("internet", {})
    raw_brains = data.get("brain", [])

    brains = _parse_brains(raw_brains) if raw_brains else DEFAULT_CLOUD_CANDIDATES

    return VenomConfig(
        poll_interval=float(venom.get("poll_interval", 10.0)),
        probe_timeout=float(venom.get("probe_timeout", 3.0)),
        status_path=Path(venom.get("status_path", "/run/venom/status.json")),
        internet_host=str(internet.get("host", "1.1.1.1")),
        internet_port=int(internet.get("port", 53)),
        brains=brains,
    )
