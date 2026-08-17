"""Where Carnage keeps its things, and who it thinks it is.

Deliberately thinner than `venom/config.py`, and for a reason rather than out
of laziness. Venom's config describes hardware that was decided at flash time
— which headset, which wake word, which camera. A phone's equivalents are
either discovered at runtime (see `platform.detect`) or belong to the host app
rather than to Python. What is left is small: an identity, somewhere to put
files, and how to reach the other devices.

`device` is the one field that must be got right. It is the name the sync
layer attributes every change to, so two installs sharing a device id would
each treat the other's edits as their own and quietly eat each other's
watermarks. It defaults to the hostname and is worth setting explicitly.
"""

from __future__ import annotations

import json
import logging
import os
import platform as _platform
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("carnage.config")

DEFAULT_DEVICE = "carnage"
DEFAULT_PORT = 8790


def _default_state() -> Path:
    """Somewhere writable on Android, Termux, and a dev laptop alike."""
    for candidate in (os.environ.get("CARNAGE_STATE"),
                      os.environ.get("ANDROID_DATA_DIR"),
                      # Termux's own home, which is app-private storage.
                      os.environ.get("PREFIX") and
                      f"{os.environ['PREFIX']}/var/lib/carnage"):
        if candidate:
            return Path(candidate)
    return Path.home() / ".carnage"


@dataclass(frozen=True)
class HubConfig:
    """Carnage is the hub, so this is what it *listens* on."""

    enabled: bool = True
    host: str = "0.0.0.0"       # noqa: S104 — a hub nobody can reach is not one
    port: int = DEFAULT_PORT
    #: Shared secret every leaf must present. Empty means no check, which is
    #: only ever right on a loopback-only bind.
    token: str = ""
    #: Device ids allowed to sync. Empty means any device with the token.
    peers: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebConfig:
    """The page the phone installs — see `carnage/web.py`.

    Bound to loopback by default and meant to be published by `tailscale
    serve`, which terminates TLS and reaches it over the tailnet. That is not
    a nicety: browsers refuse geolocation, the microphone and installability on
    a plain-HTTP origin that is not localhost, so a LAN address would give a
    page with none of the three things that make it worth having.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8791
    #: Defaults to the hub token, so there is one secret for the device.
    token: str = ""


@dataclass(frozen=True)
class CarnageConfig:
    device: str = DEFAULT_DEVICE
    user_name: str = ""
    gemini_api_key: str = ""
    state_dir: Path = field(default_factory=_default_state)
    #: Where written documents land. Empty switches the skill off entirely
    #: rather than guessing at a folder on someone's phone.
    documents_dir: str = ""
    hub: HubConfig = field(default_factory=HubConfig)
    web: WebConfig = field(default_factory=WebConfig)
    #: Repos and deploy targets, matching `flint_core.skills.Workspace`. Same
    #: allowlist discipline as Venom: nothing until it is named.
    repos: tuple[tuple[str, str], ...] = ()
    deploy_targets: tuple[dict, ...] = ()
    #: Her other bodies: [{name, body, can: [...]}]. What goes in `can` is
    #: read out in the prompt, so it is written the way she would say it —
    #: "send a text", not "sms_send".
    devices: tuple[dict, ...] = ()

    # ── the Workspace protocol, so flint_core.skills.dev works unchanged ────
    def repo_path(self, name: str) -> str:
        wanted = (name or "").strip().lower()
        for repo_name, path in self.repos:
            if repo_name.lower() == wanted:
                return path
        return ""

    @property
    def repo_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.repos)

    @property
    def default_repo(self) -> str:
        return self.repos[0][1] if self.repos else ""

    @property
    def voice_ready(self) -> bool:
        return bool(self.gemini_api_key)


def load_config(path: Path | None = None) -> CarnageConfig:
    """Read config from JSON, falling back to sane defaults for every field.

    JSON rather than TOML because this has to parse under whatever Python the
    host app embeds, and `json` is the one parser guaranteed to be there.
    A missing or broken file yields defaults and a warning: a phone that will
    not start because of a stray comma is a phone with no assistant on it.
    """
    path = Path(path) if path else _default_state() / "carnage.json"
    raw: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError) as exc:
            log.warning("config at %s unreadable (%s) — using defaults",
                        path, exc)

    web_raw = raw.get("web") if isinstance(raw.get("web"), dict) else {}
    web = WebConfig(
        enabled=bool(web_raw.get("enabled", False)),
        host=str(web_raw.get("host", WebConfig.host)),
        port=int(web_raw.get("port", WebConfig.port) or WebConfig.port),
        token=str(web_raw.get("token", "") or ""))

    hub_raw = raw.get("hub") if isinstance(raw.get("hub"), dict) else {}
    hub = HubConfig(
        enabled=bool(hub_raw.get("enabled", True)),
        host=str(hub_raw.get("host", HubConfig.host)),
        port=int(hub_raw.get("port", DEFAULT_PORT) or DEFAULT_PORT),
        token=str(hub_raw.get("token", "") or ""),
        peers=tuple(str(p) for p in (hub_raw.get("peers") or ())))

    state = raw.get("state_dir")
    return CarnageConfig(
        device=str(raw.get("device") or _platform.node() or DEFAULT_DEVICE),
        user_name=str(raw.get("user_name", "") or ""),
        gemini_api_key=str(raw.get("gemini_api_key", "") or
                           os.environ.get("GEMINI_API_KEY", "")),
        state_dir=Path(state) if state else _default_state(),
        documents_dir=str(raw.get("documents_dir", "") or ""),
        hub=hub,
        web=web,
        repos=tuple((str(name), str(p))
                    for name, p in (raw.get("repos") or ())),
        deploy_targets=tuple(d for d in (raw.get("deploy_targets") or ())
                             if isinstance(d, dict)),
        devices=tuple(d for d in (raw.get("devices") or ())
                      if isinstance(d, dict) and d.get("name")))
