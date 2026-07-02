"""Atomic status reporting.

The daemon writes one small JSON file per cycle (default under /run, which
is tmpfs — zero flash wear). Anything on the device — a future LED/display
driver, sshed-in human, or the laptop over the link — can read a single
consistent snapshot of appliance health.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class StatusWriter:
    def __init__(self, path: Path):
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def write(self, status: dict[str, Any]) -> None:
        """Write the snapshot atomically (write-to-temp + rename)."""
        payload = dict(status)
        payload["updated_at"] = time.time()
        self._path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=".status-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def read(self) -> dict[str, Any] | None:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
