"""Cloud memory backup/restore — encryption round-trip, restore gating,
and the debounced backup worker. Supabase HTTP is stubbed; no network."""

import json
import threading

import pytest

from venom.cloud_memory import (CloudMemory, MemoryBackupWorker,
                                restore_if_empty)

URL = "https://proj.supabase.co"
KEY = "anon-key-123"
PASS = "chai-samosa-42"

EMPTY = {"identity": {}, "preferences": {}, "projects": {},
         "relationships": {}, "wishes": {}, "notes": {}}
SAMPLE = json.dumps({**EMPTY, "identity": {"name": {"value": "Tushar",
                                                    "updated": "2026-07-05"}}})


class _Resp:
    def __init__(self, data=None, status=200):
        self._data = data if data is not None else []
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._data


class _FakeSupabase:
    """Captures the last upserted row and serves it back on GET by id."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def post(self, url, headers=None, json=None, timeout=None):
        self.rows[json["id"]] = json
        return _Resp(status=201)

    def get(self, url, headers=None, params=None, timeout=None):
        wanted = params["id"].removeprefix("eq.")
        row = self.rows.get(wanted)
        return _Resp([row] if row else [])


@pytest.fixture
def fake_http(monkeypatch):
    fake = _FakeSupabase()
    import venom.cloud_memory as cm
    monkeypatch.setattr(cm.requests, "post", fake.post)
    monkeypatch.setattr(cm.requests, "get", fake.get)
    return fake


def test_memory_id_deterministic_and_opaque():
    a = CloudMemory(URL, KEY, PASS)
    b = CloudMemory(URL, KEY, PASS)
    c = CloudMemory(URL, KEY, "different")
    assert a.memory_id == b.memory_id
    assert a.memory_id != c.memory_id
    assert PASS not in a.memory_id  # not reversible to the passphrase


def test_not_configured_noops():
    cloud = CloudMemory(URL, KEY, "")  # no passphrase
    assert cloud.configured is False
    assert cloud.backup(SAMPLE) is False
    assert cloud.restore() is None


def test_backup_encrypts(fake_http):
    cloud = CloudMemory(URL, KEY, PASS)
    assert cloud.backup(SAMPLE) is True
    row = fake_http.rows[cloud.memory_id]
    assert "Tushar" not in row["payload"]  # ciphertext, not plaintext
    assert row["id"] == cloud.memory_id
    assert row["salt"]


def test_backup_restore_round_trip(fake_http):
    cloud = CloudMemory(URL, KEY, PASS)
    cloud.backup(SAMPLE)
    restored = CloudMemory(URL, KEY, PASS).restore()
    assert json.loads(restored) == json.loads(SAMPLE)


def test_restore_wrong_passphrase(fake_http):
    CloudMemory(URL, KEY, PASS).backup(SAMPLE)
    # Different passphrase → different id → no row found → None (never leaks).
    assert CloudMemory(URL, KEY, "wrong-phrase").restore() is None


def test_restore_corrupt_payload_returns_none(fake_http):
    cloud = CloudMemory(URL, KEY, PASS)
    cloud.backup(SAMPLE)
    fake_http.rows[cloud.memory_id]["payload"] = "not-a-valid-token"
    assert cloud.restore() is None


def test_restore_if_empty_writes_when_empty(fake_http, tmp_path):
    CloudMemory(URL, KEY, PASS).backup(SAMPLE)
    mem = tmp_path / "memory.json"  # missing local file → fresh device
    assert restore_if_empty(CloudMemory(URL, KEY, PASS), mem) is True
    assert json.loads(mem.read_text())["identity"]["name"]["value"] == "Tushar"


def test_restore_if_empty_skips_when_local_has_data(fake_http, tmp_path):
    CloudMemory(URL, KEY, PASS).backup(SAMPLE)
    mem = tmp_path / "memory.json"
    local = {**EMPTY, "identity": {"name": {"value": "LOCAL", "updated": "2026-07-05"}}}
    mem.write_text(json.dumps(local), encoding="utf-8")
    assert restore_if_empty(CloudMemory(URL, KEY, PASS), mem) is False
    assert json.loads(mem.read_text())["identity"]["name"]["value"] == "LOCAL"


def test_restore_if_empty_noop_when_unconfigured(tmp_path):
    mem = tmp_path / "memory.json"
    assert restore_if_empty(CloudMemory(URL, KEY, ""), mem) is False


def test_backup_worker_debounces_and_uploads(fake_http, tmp_path):
    mem = tmp_path / "memory.json"
    mem.write_text(SAMPLE, encoding="utf-8")
    cloud = CloudMemory(URL, KEY, PASS)

    done = threading.Event()
    orig = cloud.backup

    def wrapped(data):
        result = orig(data)
        done.set()
        return result

    cloud.backup = wrapped
    worker = MemoryBackupWorker(cloud, mem, debounce=0.01)
    worker.start()
    worker.trigger()
    assert done.wait(timeout=5), "worker never performed the backup"
    worker.stop()
    # The upload carried the current file contents.
    assert cloud.memory_id in fake_http.rows
