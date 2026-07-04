"""Network geolocation: parsing, caching, failover, and tool wiring."""

from venom.config import VenomConfig
from venom.location import LocationProvider, _parse_ipapi, _parse_ipinfo
from venom.tools_pi import TimerBoard, build_pi_registry


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


IPINFO_OK = {"ip": "1.2.3.4", "city": "Pune", "region": "Maharashtra",
             "country": "IN", "loc": "18.52,73.85", "timezone": "Asia/Kolkata"}
IPAPI_OK = {"status": "success", "city": "Mumbai", "regionName": "MH",
            "country": "India", "lat": 19.0, "lon": 72.8}


def test_parse_ipinfo():
    loc = _parse_ipinfo(IPINFO_OK)
    assert loc["city"] == "Pune" and loc["lat"] == 18.52 and loc["source"] == "ipinfo"


def test_parse_ipapi_requires_success():
    assert _parse_ipapi({"status": "fail"}) is None
    assert _parse_ipapi(IPAPI_OK)["city"] == "Mumbai"


def test_get_caches_within_ttl():
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _Resp(IPINFO_OK)

    prov = LocationProvider(ttl=1000, get=fake_get)
    assert prov.get()["city"] == "Pune"
    assert prov.get()["city"] == "Pune"      # served from cache
    assert len(calls) == 1                   # only one network call


def test_failover_to_ipapi():
    def fake_get(url, timeout=None):
        if "ipinfo" in url:
            raise __import__("requests").RequestException("down")
        return _Resp(IPAPI_OK)

    prov = LocationProvider(get=fake_get)
    assert prov.get()["city"] == "Mumbai"


def test_failure_returns_none_then_stale_fallback():
    state = {"fail": True}

    def fake_get(url, timeout=None):
        if state["fail"]:
            raise __import__("requests").RequestException("down")
        return _Resp(IPINFO_OK)

    prov = LocationProvider(ttl=0, get=fake_get)
    assert prov.get() is None                # both providers down
    state["fail"] = False
    assert prov.get()["city"] == "Pune"      # recovers
    state["fail"] = True
    assert prov.get()["city"] == "Pune"      # stale cache beats nothing


def test_describe_variants():
    prov = LocationProvider(get=lambda url, timeout=None: _Resp(IPINFO_OK))
    assert prov.describe_cached() == ""       # nothing fetched yet
    assert prov.describe() == "Pune, Maharashtra, IN"
    assert prov.describe_cached() == "Pune, Maharashtra, IN"  # now warmed


# ── tool wiring ───────────────────────────────────────────────────────────────
def _reg_with_location(monkeypatch, loc_payload=IPINFO_OK):
    prov = LocationProvider(get=lambda url, timeout=None: _Resp(loc_payload))
    reg = build_pi_registry(VenomConfig(), memory=_DummyMem(),
                            timers=TimerBoard(), location=prov)
    return reg


class _DummyMem:
    def load(self):
        return {}

    def render_for_prompt(self):
        return ""


def test_where_am_i_tool(monkeypatch):
    reg = _reg_with_location(monkeypatch)
    assert "where_am_i" in reg
    assert "Pune" in reg.dispatch("where_am_i", {})


def test_weather_defaults_to_current_location(monkeypatch):
    import venom.tools_pi as tp
    captured = {}

    def fake_weather(city, **kw):
        captured["city"] = city
        return f"weather in {city}"

    monkeypatch.setattr(tp, "fetch_weather", fake_weather)
    reg = _reg_with_location(monkeypatch)
    out = reg.dispatch("weather_report", {})   # no city → use location
    assert captured["city"] == "Pune"
    assert "Pune" in out


def test_where_am_i_absent_without_provider():
    reg = build_pi_registry(VenomConfig(), memory=_DummyMem(), timers=TimerBoard())
    assert "where_am_i" not in reg
