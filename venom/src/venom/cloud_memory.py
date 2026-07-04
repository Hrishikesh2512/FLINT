"""Cloud backup for Venom's long-term memory — survive re-flash / device loss.

Venom's memory lives at ``/var/lib/venom/memory.json`` on the pendrive, so a
re-flash or a lost Pi takes it with it. This module mirrors that file to a
Supabase table so a freshly provisioned device restores it on first boot.

Identity + privacy from a single passphrase (baked in at flash time):

    memory_id      = sha256(fixed_salt + passphrase)   — opaque row key
    encryption key = PBKDF2(passphrase, random per-row salt)  — Fernet

The cloud row holds only ``{id, salt, payload, updated_at}`` where ``payload``
is ciphertext. The Supabase anon key is public and extractable from a device,
so nothing personal is ever stored in the clear: a row leak reveals only an
opaque id and an encrypted blob. Restore requires the passphrase.

No ``supabase`` package (it drags in httpx/gotrue/postgrest/realtime — too much
for a 2 GB Pi); we talk to PostgREST directly with ``requests`` + one Fernet.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

log = logging.getLogger("venom.cloud_memory")

# Fixed salt for the row-id hash only. It just decorrelates the id from the raw
# passphrase; the id is meant to be opaque, not secret-strength.
_ID_SALT = b"venom-memory-id-v1"
_KDF_ITERS = 200_000
MEMORY_CATEGORIES = ("identity", "preferences", "projects",
                     "relationships", "wishes", "notes")


class CloudMemory:
    """Encrypted upsert/fetch of the memory blob against a Supabase table."""

    def __init__(self, url: str, anon_key: str, passphrase: str,
                 table: str = "venom_memory", timeout: float = 10.0):
        self.url = (url or "").rstrip("/")
        self.anon_key = (anon_key or "").strip()
        self.passphrase = (passphrase or "").strip()
        self.table = table or "venom_memory"
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url and self.anon_key and self.passphrase
                    and "YOUR-" not in self.url and "YOUR-" not in self.anon_key)

    @property
    def memory_id(self) -> str:
        return hashlib.sha256(_ID_SALT + self.passphrase.encode()).hexdigest()

    def _fernet(self, salt: bytes) -> Fernet:
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                         iterations=_KDF_ITERS)
        return Fernet(base64.urlsafe_b64encode(kdf.derive(self.passphrase.encode())))

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"apikey": self.anon_key,
             "Authorization": f"Bearer {self.anon_key}",
             "Content-Type": "application/json"}
        if extra:
            h.update(extra)
        return h

    def backup(self, memory_json: str) -> bool:
        """Encrypt and upsert the memory blob. Best-effort: returns success."""
        if not self.configured:
            return False
        salt = os.urandom(16)
        try:
            token = self._fernet(salt).encrypt(memory_json.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("memory backup: encrypt failed: %s", exc)
            return False
        row = {"id": self.memory_id,
               "salt": base64.b64encode(salt).decode(),
               "payload": token.decode(),
               "updated_at": _now_iso()}
        try:
            resp = requests.post(
                f"{self.url}/rest/v1/{self.table}",
                headers=self._headers(
                    {"Prefer": "resolution=merge-duplicates,return=minimal"}),
                json=row, timeout=self.timeout)
            resp.raise_for_status()
            log.info("memory backed up to cloud (%d bytes)", len(memory_json))
            return True
        except requests.RequestException as exc:
            log.warning("memory backup failed: %s", exc)
            return False

    def restore(self) -> str | None:
        """Fetch and decrypt this device's memory blob, or None if absent."""
        if not self.configured:
            return None
        try:
            resp = requests.get(
                f"{self.url}/rest/v1/{self.table}",
                headers=self._headers(),
                params={"id": f"eq.{self.memory_id}",
                        "select": "salt,payload", "limit": 1},
                timeout=self.timeout)
            resp.raise_for_status()
            rows = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("memory restore fetch failed: %s", exc)
            return None
        if not rows:
            log.info("no cloud memory backup found for this passphrase")
            return None
        try:
            salt = base64.b64decode(rows[0]["salt"])
            plain = self._fernet(salt).decrypt(rows[0]["payload"].encode())
            return plain.decode("utf-8")
        except (InvalidToken, KeyError, ValueError, TypeError) as exc:
            log.warning("memory restore decrypt failed (wrong passphrase?): %s", exc)
            return None


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _is_empty(memory: dict) -> bool:
    return not any(memory.get(cat) for cat in MEMORY_CATEGORIES)


def restore_if_empty(cloud: CloudMemory, memory_path: Path) -> bool:
    """On a fresh device (empty local memory), pull the cloud backup down.
    Never overwrites a device that already has memory — local is truth."""
    if not cloud.configured:
        return False
    path = Path(memory_path)
    try:
        local = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(local, dict) and not _is_empty(local):
            return False  # device already has memory; don't clobber
    except (OSError, json.JSONDecodeError):
        pass  # missing/corrupt local memory → safe to restore
    remote = cloud.restore()
    if not remote:
        return False
    try:
        parsed = json.loads(remote)
        if not isinstance(parsed, dict):
            return False
    except json.JSONDecodeError:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    log.info("restored memory from cloud backup")
    return True


class MemoryBackupWorker:
    """Debounced background uploader. `trigger()` (wired to MemoryStore's
    on_change) wakes a single worker that coalesces a burst of edits into one
    upload of the latest file, so the voice loop never blocks on the network."""

    def __init__(self, cloud: CloudMemory, memory_path: Path,
                 debounce: float = 3.0):
        self.cloud = cloud
        self.path = Path(memory_path)
        self.debounce = debounce
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.cloud.configured or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="venom-mem-backup")
        self._thread.start()

    def trigger(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            if self._stop.is_set():
                break
            self._stop.wait(self.debounce)  # let a burst of edits settle
            self._wake.clear()
            try:
                data = self.path.read_text(encoding="utf-8")
            except OSError:
                continue
            self.cloud.backup(data)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
