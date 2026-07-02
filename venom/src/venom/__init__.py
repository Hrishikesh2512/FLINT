"""Venom — the Raspberry Pi wearable runtime of FLINT.

This package is the appliance daemon that runs on the Pi. It owns the
device-level concerns (connectivity, audio hardware, brain selection,
health reporting) and deliberately contains no AI compute: the brain is
the laptop when reachable, cloud APIs otherwise.
"""

__version__ = "0.1.0"
