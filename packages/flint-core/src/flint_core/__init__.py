"""flint-core — the shared, platform-free domain layer of FLINT.

Everything in this package must run identically on the Windows app and the
Raspberry Pi (Venom): no GUI, no OS automation, no audio hardware. Platform
runtimes depend on flint-core; never the other way around.
"""

__version__ = "0.1.0"
