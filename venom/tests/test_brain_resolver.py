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


def test_hysteresis_tolerates_transient_current_failures():
    # fail_threshold=3: a healthy Gemini shouldn't flap to Groq on brief blips.
    net = FakeNet(GEMINI.host)
    resolver = BrainResolver(ALL, prober=net, fail_threshold=3)
    assert resolve(resolver).brain == GEMINI

    net.reachable = {GROQ.host}  # Gemini blips out, Groq available
    first = resolve(resolver)
    assert first.brain == GEMINI and not first.switched   # 1st miss tolerated
    second = resolve(resolver)
    assert second.brain == GEMINI and not second.switched  # 2nd miss tolerated
    third = resolve(resolver)
    assert third.brain == GROQ and third.switched          # 3rd miss → switch


def test_hysteresis_recovers_when_current_returns_before_threshold():
    net = FakeNet(GEMINI.host)
    resolver = BrainResolver(ALL, prober=net, fail_threshold=3)
    assert resolve(resolver).brain == GEMINI

    net.reachable = set()
    assert resolve(resolver).brain == GEMINI  # tolerated miss, streak now 1
    net.reachable = {GEMINI.host}             # Gemini comes back
    assert resolve(resolver).brain == GEMINI  # streak reset, still no switch

    # A later isolated miss is again tolerated, proving the streak reset.
    net.reachable = set()
    assert resolve(resolver).brain == GEMINI


def test_hysteresis_requires_consecutive_successes_to_promote():
    # success_threshold=2: the laptop must be up twice in a row before it wins.
    net = FakeNet(GEMINI.host)
    resolver = BrainResolver(ALL, prober=net, success_threshold=2)
    assert resolve(resolver).brain == GEMINI

    net.reachable = {GEMINI.host, LAPTOP.host}
    first = resolve(resolver)
    assert first.brain == GEMINI and not first.switched  # 1st laptop success
    second = resolve(resolver)
    assert second.brain == LAPTOP and second.switched    # 2nd → promote


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
