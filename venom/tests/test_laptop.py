"""Venom → FLINT laptop-task client — scripted fake socket, no server.
The wire protocol is FLINT's core/ws_listener.py: hello → (auth) →
command → ack → reply."""

import json

from venom.config import LaptopConfig, VenomConfig, load_config
from venom.laptop import run_laptop_task


class FakeWS:
    """Replays scripted server messages; records what the client sends."""

    def __init__(self, script):
        self.script = list(script)
        self.sent: list[dict] = []
        self.closed = False

    def recv(self, timeout=None):
        if not self.script:
            raise TimeoutError("no more scripted messages")
        return json.dumps(self.script.pop(0))

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed = True


def run(script, token="", timeout=45.0, ticks=None):
    config = LaptopConfig(host="exodus.local", token=token, timeout=timeout)
    ws = FakeWS(script)
    clock = iter(ticks or range(1000))
    out = run_laptop_task(config, "open spotify",
                          connect_fn=lambda _uri: ws,
                          clock=lambda: next(clock))
    return out, ws


def test_tokenless_flow_returns_flints_reply():
    out, ws = run([
        {"type": "hello", "name": "FLINT", "auth_required": False},
        {"type": "ack", "text": "open spotify"},
        {"type": "log", "line": "opening...", "level": "info"},
        {"type": "reply", "text": "Spotify khol diya, boss."},
    ])
    assert out == "Spotify khol diya, boss."
    assert {"type": "command", "text": "open spotify"} in ws.sent
    assert ws.closed


def test_auth_flow_sends_token_before_command():
    out, ws = run([
        {"type": "hello", "name": "FLINT", "auth_required": True},
        {"type": "auth_ok"},
        {"type": "reply", "text": "done"},
    ], token="s3cret")
    assert out == "done"
    assert ws.sent[0] == {"type": "auth", "token": "s3cret"}
    assert ws.sent[1] == {"type": "command", "text": "open spotify"}


def test_auth_required_but_no_token_configured():
    out, _ = run([{"type": "hello", "auth_required": True}], token="")
    assert "token" in out


def test_auth_rejected():
    out, _ = run([
        {"type": "hello", "auth_required": True},
        {"type": "auth_failed"},
    ], token="wrong")
    assert "rejected" in out


def test_timeout_after_ack_says_still_working():
    # Command accepted, but no reply within the window.
    out, _ = run([
        {"type": "hello", "auth_required": False},
        {"type": "ack", "text": "open spotify"},
    ], timeout=5.0, ticks=[0, 1, 2, 3, 10, 11, 12])
    assert "still working" in out


def test_unreachable_laptop_degrades_gracefully():
    config = LaptopConfig(host="exodus.local")

    def refuse(_uri):
        raise OSError("connection refused")

    out = run_laptop_task(config, "open spotify", connect_fn=refuse)
    assert "couldn't reach FLINT" in out


def test_flint_error_is_spoken():
    out, _ = run([
        {"type": "hello", "auth_required": False},
        {"type": "error", "message": "empty command"},
    ])
    assert "problem" in out and "empty command" in out


def test_long_replies_are_trimmed_for_speech():
    out, _ = run([
        {"type": "hello", "auth_required": False},
        {"type": "reply", "text": "x" * 2000},
    ])
    assert len(out) < 700


# ── config + registry wiring ──────────────────────────────────────────────────
def test_laptop_config_parsing(tmp_path):
    assert VenomConfig().laptop.ready is False  # off until a host is set

    path = tmp_path / "venom.toml"
    path.write_text('[laptop]\nhost = "exodus.local"\ntoken = "t"\n')
    config = load_config(path)
    assert config.laptop.ready is True
    assert config.laptop.host == "exodus.local"
    assert config.laptop.port == 8765
    assert config.laptop.token == "t"


def test_laptop_task_registered_only_when_configured(tmp_path):
    from flint_core.memory import MemoryStore
    from venom.tools_pi import TimerBoard, build_pi_registry

    base = dict(gemini_api_key="k", memory_path=tmp_path / "m.json")
    off = build_pi_registry(VenomConfig(**base),
                            MemoryStore(base["memory_path"]), TimerBoard())
    assert "laptop_task" not in off.names()

    on = build_pi_registry(
        VenomConfig(**base, laptop=LaptopConfig(host="exodus.local")),
        MemoryStore(base["memory_path"]), TimerBoard())
    assert "laptop_task" in on.names()
