"""Voice → laptop: hand whole tasks to FLINT, the desktop assistant.

Venom is the voice on the body; FLINT is the hands on the laptop. This
client speaks FLINT's remote wire protocol (FLINT core/ws_listener.py):

    -> {"type": "hello", "auth_required": bool}
    <- {"type": "auth", "token": ...}          (when required)
    -> {"type": "auth_ok"} | {"type": "auth_failed"}
    <- {"type": "command", "text": "open spotify"}
    -> {"type": "ack", ...} then, when FLINT's model finishes the task,
    -> {"type": "reply", "text": "<what FLINT said>"}

Everything in between (log/state/transcript/job broadcasts) is ignored.
The connection factory is injectable, so the whole exchange is testable
without a server. Returns are natural speech — they're read aloud.
"""

from __future__ import annotations

import json
import logging
import time

from venom.config import LaptopConfig

log = logging.getLogger("venom.laptop")

# Cap what she reads aloud — FLINT can be chatty; the user is on an earphone.
MAX_SPOKEN_CHARS = 600


def _default_connect(uri: str):
    from websockets.sync.client import connect

    return connect(uri, open_timeout=6)


def run_laptop_task(config: LaptopConfig, command: str,
                    connect_fn=None, clock=time.monotonic) -> str:
    """Send one task to FLINT and wait for its spoken-back reply."""
    uri = f"ws://{config.host}:{config.port}"
    try:
        ws = (connect_fn or _default_connect)(uri)
    except Exception as exc:
        log.info("FLINT unreachable at %s: %s", uri, exc)
        return ("I couldn't reach FLINT on the laptop — it's either not "
                "running, or the laptop isn't on this network.")

    deadline = clock() + config.timeout
    sent = False
    try:
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                return ("FLINT accepted the task but is still working on "
                        "it — check the laptop screen.") if sent else (
                        "FLINT didn't respond in time — check the laptop.")
            try:
                raw = ws.recv(timeout=remaining)
            except TimeoutError:
                continue  # loop re-checks the deadline and words the answer
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            mtype = str(msg.get("type", "")).lower()

            if mtype == "hello":
                if msg.get("auth_required"):
                    if not config.token:
                        return ("FLINT wants a token and I don't have one — "
                                "set [laptop] token in my config to match "
                                "FLINT's remote_token.")
                    ws.send(json.dumps({"type": "auth",
                                        "token": config.token}))
                else:
                    ws.send(json.dumps({"type": "command", "text": command}))
                    sent = True
            elif mtype == "auth_ok":
                ws.send(json.dumps({"type": "command", "text": command}))
                sent = True
            elif mtype == "auth_failed":
                return ("FLINT rejected my token — [laptop] token here must "
                        "match FLINT's remote_token.")
            elif mtype == "reply":
                text = str(msg.get("text", "")).strip()
                if len(text) > MAX_SPOKEN_CHARS:
                    text = text[:MAX_SPOKEN_CHARS] + " …"
                log.info("laptop task done: %.80s", text)
                return text or "Done — FLINT finished but said nothing."
            elif mtype == "error":
                return f"FLINT reported a problem: {msg.get('message', '?')}"
            # ack / log / state / transcript / job / pong — progress noise
    except Exception as exc:
        log.warning("laptop task failed mid-flight: %s", exc)
        return ("The connection to FLINT dropped mid-task — it may still "
                "finish on the laptop.")
    finally:
        try:
            ws.close()
        except Exception:
            pass
