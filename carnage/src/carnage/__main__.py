"""Run Carnage, or ask it what it thinks it is.

    python -m carnage --once     one health line, then exit
    python -m carnage            run the hub until stopped

`--once` exists for the same reason Venom's does: on a device with no screen
and no keyboard, the first question is always "did it come up, and what did it
find?", and that has to be answerable without starting anything long-lived.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from carnage.config import load_config
from carnage.runtime import Carnage

log = logging.getLogger("carnage")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")


async def _run(carnage: Carnage) -> None:
    await carnage.start()
    try:
        await asyncio.Event().wait()          # until interrupted
    finally:
        await carnage.stop()


def _body_for(config):
    """Which phone body to use — the native one whenever there is one.

    Order matters and the wrong order is expensive. Serving the page does not
    make the browser the body: on Termux the native side is strictly better at
    the same jobs — a real GPS fix rather than one relayed through a tab, and
    an SMS that actually *sends* instead of one the user must tap. Choosing the
    browser because a page happens to be enabled would silently downgrade the
    emergency path on the one device where it works properly.

    So the browser body is the fallback, for when she is served from a machine
    that is not the phone at all: then the tab genuinely is the only phone in
    the picture, and its senses are the only ones there are.
    """
    from carnage.platform import detect

    native = detect()
    if native.available():
        log.info("body: %s (native)", native.name)
        return native
    if config.web.enabled:
        from carnage.browserphone import BrowserPhone

        log.info("body: browser — senses come from whatever page is open")
        return BrowserPhone()
    return native


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="carnage")
    parser.add_argument("--once", action="store_true",
                        help="print one status snapshot and exit")
    parser.add_argument("--config", default=None, help="path to carnage.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--web", action="store_true",
                        help="serve the installable page, whatever the config says")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    config = load_config(args.config)
    if args.web:
        # A flag rather than only a config key: the first thing anyone does is
        # try it once, and editing JSON to do that is a poor welcome.
        from dataclasses import replace

        config = replace(config, web=replace(config.web, enabled=True))
    carnage = Carnage(config, phone=_body_for(config))

    if args.once:
        print(json.dumps({
            "device": config.device,
            "platform": carnage.phone.name,
            "phone_available": carnage.phone.available(),
            "state_dir": str(config.state_dir),
            "tools": len(list(carnage.registry)),
            "capabilities": carnage.capabilities.names(),
            "hub": {"enabled": config.hub.enabled, "port": config.hub.port},
            "web": {"enabled": config.web.enabled, "port": config.web.port},
        }, indent=2))
        return 0

    try:
        asyncio.run(_run(carnage))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
