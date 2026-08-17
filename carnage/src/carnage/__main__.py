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
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    config = load_config(args.config)
    carnage = Carnage(config)

    if args.once:
        print(json.dumps({
            "device": config.device,
            "platform": carnage.phone.name,
            "phone_available": carnage.phone.available(),
            "state_dir": str(config.state_dir),
            "tools": len(list(carnage.registry)),
            "capabilities": carnage.capabilities.names(),
            "hub": {"enabled": config.hub.enabled, "port": config.hub.port},
        }, indent=2))
        return 0

    try:
        asyncio.run(_run(carnage))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
