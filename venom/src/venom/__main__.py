"""Entry point: `venom` (console script) or `python -m venom`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from venom.config import load_config
from venom.supervisor import Supervisor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="venom", description="Venom appliance daemon")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to venom.toml (default: /etc/venom/venom.toml)")
    parser.add_argument("--once", action="store_true",
                        help="run a single monitoring cycle, print it, and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    # journald captures stderr and adds its own timestamps — keep lines bare.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s: %(message)s",
    )

    config = load_config(args.config)
    supervisor = Supervisor(config)

    if args.once:
        snapshot = asyncio.run(supervisor.cycle())
        json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0 if snapshot["online"] else 1

    try:
        asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
