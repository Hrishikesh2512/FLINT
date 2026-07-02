import asyncio
import json

from venom.config import BrainCandidate, VenomConfig
from venom.status import StatusWriter
from venom.supervisor import Supervisor


def test_status_write_read_round_trip(tmp_path):
    writer = StatusWriter(tmp_path / "nested" / "status.json")
    writer.write({"online": True, "brain": "laptop"})
    data = writer.read()
    assert data["online"] is True
    assert data["brain"] == "laptop"
    assert data["updated_at"] > 0


def test_status_write_is_valid_json_on_disk(tmp_path):
    writer = StatusWriter(tmp_path / "status.json")
    writer.write({"n": 1})
    raw = (tmp_path / "status.json").read_text(encoding="utf-8")
    assert json.loads(raw)["n"] == 1
    # no leftover temp files from the atomic write
    assert list(tmp_path.glob(".status-*")) == []


def test_read_missing_or_corrupt(tmp_path):
    writer = StatusWriter(tmp_path / "status.json")
    assert writer.read() is None
    (tmp_path / "status.json").write_text("{broken", encoding="utf-8")
    assert writer.read() is None


def _test_config(tmp_path, **overrides) -> VenomConfig:
    defaults = dict(
        poll_interval=0.05,
        probe_timeout=0.1,
        status_path=tmp_path / "status.json",
        # 127.0.0.1:9 (discard) is reliably closed — probes fail fast.
        internet_host="127.0.0.1",
        internet_port=9,
        brains=(BrainCandidate("nowhere", "127.0.0.1", 9, priority=0),),
    )
    defaults.update(overrides)
    return VenomConfig(**defaults)


def test_supervisor_cycle_offline(tmp_path):
    supervisor = Supervisor(_test_config(tmp_path))
    snapshot = asyncio.run(supervisor.cycle())
    assert snapshot["online"] is False
    assert snapshot["brain"] is None
    assert supervisor.status.read()["online"] is False


def test_supervisor_cycle_finds_local_brain(tmp_path):
    async def scenario():
        server = await asyncio.start_server(
            lambda r, w: w.close(), "127.0.0.1", 0
        )
        port = server.sockets[0].getsockname()[1]
        config = _test_config(
            tmp_path,
            brains=(BrainCandidate("laptop", "127.0.0.1", port, priority=0),),
        )
        supervisor = Supervisor(config)
        try:
            return await supervisor.cycle()
        finally:
            server.close()
            await server.wait_closed()

    snapshot = asyncio.run(scenario())
    assert snapshot["online"] is True
    assert snapshot["brain"] == "laptop"


def test_supervisor_run_stops_on_request(tmp_path):
    supervisor = Supervisor(_test_config(tmp_path))

    async def scenario():
        task = asyncio.create_task(supervisor.run())
        await asyncio.sleep(0.15)  # let at least one cycle happen
        supervisor.request_stop()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    assert supervisor.status.read() is not None
