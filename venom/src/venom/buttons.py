"""Physical button handling: headset button + Bluetooth camera-shutter remote.

Three buttons, all seen through Linux evdev as key events:

  • headset inline button  → wake Venom (and its music-duck happens downstream)
  • shutter button 1        → toggle Do-Not-Disturb
  • shutter button 2        → wake Venom (a reliable physical wake button)

The headset button's key code is well-known across firmwares (the play/pause
family). The two shutter codes vary by remote, so they live in config; until
they are set, pressing a shutter button logs its code (`unmapped key code N`)
so it can be identified on the very first press and locked in.

We watch *every* input device that emits key events (the appliance has no real
keyboard) and re-scan periodically, so the listener survives headset/shutter
drops and reconnects without any coordination.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("venom.buttons")

# Headset play/pause family (linux/input-event-codes.h) — any of these from the
# headset's inline button means "wake". KEY_PLAYPAUSE/PLAYCD/PLAY/PAUSECD/STOPCD.
WAKE_CODES = {164, 200, 207, 201, 209}
RESCAN_SECONDS = 10
DEBOUNCE_SECONDS = 0.5        # one press can emit several events — act once


def route_key(code: int, dnd_code: int, wake_code: int) -> str | None:
    """Map a key code to an action, or None if it is not one of ours.

    Kept pure (no I/O) so the routing is unit-testable and the configurable
    shutter codes are matched before falling through to 'unknown'. Both the
    headset play/pause family and the configured shutter wake_code wake her."""
    if code in WAKE_CODES or (wake_code and code == wake_code):
        return "wake"
    if dnd_code and code == dnd_code:
        return "dnd"
    return None


def find_key_devices() -> list:
    """Every input device that emits key events (headset controls, shutter)."""
    try:
        import evdev
    except ImportError:
        return []
    devices = []
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
        except OSError:
            continue
        if evdev.ecodes.EV_KEY in device.capabilities():
            devices.append(device)
        else:
            device.close()
    return devices


async def watch_buttons(*, on_wake=None, on_dnd=None,
                        dnd_code: int = 0, wake_code: int = 0) -> None:
    """Forever: attach to every key device and route presses to callbacks.

    Callbacks are plain callables invoked on the event loop (they must be quick
    and non-blocking — offload real work with asyncio themselves)."""
    try:
        import evdev
    except ImportError:
        log.info("evdev not installed — physical buttons disabled")
        return

    watched: dict[str, asyncio.Task] = {}

    import time

    last_action = 0.0

    async def listen(device) -> None:
        nonlocal last_action
        log.info("buttons attached: %s", device.name)
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY or event.value != 1:
                    continue  # key-down only (press, not release/autorepeat)
                action = route_key(event.code, dnd_code, wake_code)
                if action is None:
                    # Not debounced: this is how an unknown shutter button is
                    # identified — press it once and read the code from here.
                    log.info("button: unmapped key code %d from %s",
                             event.code, device.name)
                    continue
                now = time.monotonic()
                if now - last_action < DEBOUNCE_SECONDS:
                    continue
                last_action = now
                cb = {"wake": on_wake, "dnd": on_dnd}[action]
                if cb is not None:
                    log.info("button (%d): %s", event.code, action)
                    cb()
        except OSError:
            log.info("buttons detached: %s", device.name)
        finally:
            watched.pop(device.path, None)

    while True:
        for device in await asyncio.to_thread(find_key_devices):
            if device.path not in watched:
                watched[device.path] = asyncio.create_task(listen(device))
            else:
                device.close()
        await asyncio.sleep(RESCAN_SECONDS)
