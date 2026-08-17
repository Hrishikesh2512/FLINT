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
    phone = None
    if config.web.enabled:
        # A page is the body. Detecting Termux underneath would be wrong: the
        # senses are coming from the browser, and two bodies claiming the same
        # phone would disagree about where he is.
        from carnage.browserphone import BrowserPhone

        phone = BrowserPhone()
    carnage = Carnage(config, phone=phone)

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
