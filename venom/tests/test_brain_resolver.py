import asyncio

from venom.config import BrainCandidate
from venom.monitors.brain import BrainResolver

LAPTOP = BrainCandidate("laptop", "192.168.1.50", 8765, priority=0)
GEMINI = BrainCandidate("gemini", "gemini.example", 443, priority=10)
GROQ = BrainCandidate("groq", "groq.example", 443, priority=11)

ALL = (GEMINI, LAPTOP, GROQ)  # deliberately unsorted


class FakeNet:
    """Prober whose reachability set can be mutated between resolves."""

    def __init__(self, *reachable_hosts: str):
        self.reachable = set(reachable_hosts)
        self.probed: list[str] = []

    async def __call__(self, host: str, port: int, timeout: float) -> bool:
        self.probed.append(host)
        return host in self.reachable


def resolve(resolver: BrainResolver):
    return asyncio.run(resolver.resolve())


def test_laptop_wins_when_reachable():
    net = FakeNet(LAPTOP.host, GEMINI.host)
    resolver = BrainResolver(ALL, prober=net)
    result = resolve(resolver)
    assert result.brain == LAPTOP
    assert result.switched  # None -> laptop counts as a switch


def test_falls_back_to_cloud_in_priority_order():
    net = FakeNet(GROQ.host)
    resolver = BrainResolver(ALL, prober=net)
    result = resolve(resolver)
    assert result.brain == GROQ


def test_offline_when_nothing_reachable():
    resolver = BrainResolver(ALL, prober=FakeNet())
    result = resolve(resolver)
    assert result.brain is None
    assert not result.online
    assert not result.switched  # was already None


def test_sticky_brain_is_kept_while_healthy():
    net = FakeNet(GEMINI.host)
    resolver = BrainResolver(ALL, prober=net)
    assert resolve(resolver).brain == GEMINI
    second = resolve(resolver)
    assert second.brain == GEMINI
    assert not second.switched


def test_laptop_takes_over_when_it_comes_online():
    net = FakeNet(GEMINI.host)
    resolver = BrainResolver(ALL, prober=net)
    assert resolve(resolver).brain == GEMINI

    net.reachable.add(LAPTOP.host)
    result = resolve(resolver)
    assert result.brain == LAPTOP
    assert result.switched


def test_switches_to_fallback_when_current_dies():
    net = FakeNet(LAPTOP.host)
    resolver = BrainResolver(ALL, prober=net)
    assert resolve(resolver).brain == LAPTOP

    net.reachable = {GROQ.host}
    result = resolve(resolver)
    assert result.brain == GROQ
    assert result.switched


def test_goes_offline_and_recovers():
    net = FakeNet(LAPTOP.host)
    resolver = BrainResolver(ALL, prober=net)
    assert resolve(resolver).brain == LAPTOP

    net.reachable = set()
    down = resolve(resolver)
    assert down.brain is None and down.switched

    net.reachable = {LAPTOP.host}
    back = resolve(resolver)
    assert back.brain == LAPTOP and back.switched
