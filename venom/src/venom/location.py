"""Approximate location from network geolocation — no GPS, no API key.

Venom has no GPS, but it's always on some network, so its public IP places it
in a city. That's coarse (city-level, and it moves only when the egress IP
changes — e.g. home Wi-Fi vs phone hotspot), but it's enough to ground "where
am I", local weather, and "what's nearby" without any hardware.

Two keyless providers with failover; results cached with a TTL so we never
hammer them and session start never blocks on a slow lookup.
"""

from __future__ import annotations

import logging
import time
from threading import Lock

import requests

log = logging.getLogger("venom.location")

# ipinfo.io is HTTPS and keyless (rate-limited); ip-api.com is the fallback.
_IPINFO = "https://ipinfo.io/json"
_IPAPI = "http://ip-api.com/json/"
DEFAULT_TTL = 1800  # 30 min — city rarely changes faster than this


def _parse_ipinfo(data: dict) -> dict | None:
    if not data.get("city"):
        return None
    lat = lon = None
    if isinstance(data.get("loc"), str) and "," in data["loc"]:
        try:
            lat, lon = (float(x) for x in data["loc"].split(",", 1))
        except ValueError:
            pass
    return {"city": data.get("city"), "region": data.get("region"),
            "country": data.get("country"), "lat": lat, "lon": lon,
            "timezone": data.get("timezone"), "source": "ipinfo"}


def _parse_ipapi(data: dict) -> dict | None:
    if data.get("status") != "success" or not data.get("city"):
        return None
    return {"city": data.get("city"), "region": data.get("regionName"),
            "country": data.get("country"), "lat": data.get("lat"),
            "lon": data.get("lon"), "timezone": data.get("timezone"),
            "source": "ip-api"}


class LocationProvider:
    """Cached, best-effort current location. Thread-safe."""

    def __init__(self, ttl: float = DEFAULT_TTL, get=requests.get,
                 timeout: float = 4.0):
        self._ttl = ttl
        self._get = get
        self._timeout = timeout
        self._cache: dict | None = None
        self._fetched_at = 0.0
        self._lock = Lock()

    def _fetch(self) -> dict | None:
        for url, parse in ((_IPINFO, _parse_ipinfo), (_IPAPI, _parse_ipapi)):
            try:
                resp = self._get(url, timeout=self._timeout)
                loc = parse(resp.json())
                if loc:
                    return loc
            except (requests.RequestException, ValueError) as exc:
                log.debug("location lookup via %s failed: %s", url, exc)
        return None

    def get(self, force: bool = False) -> dict | None:
        """Fresh-enough cached location, refreshing past the TTL. On a failed
        refresh, falls back to the last known value (stale is better than none)."""
        now = time.time()
        with self._lock:
            fresh = self._cache and (now - self._fetched_at) < self._ttl
            if fresh and not force:
                return self._cache
        loc = self._fetch()
        with self._lock:
            if loc:
                self._cache, self._fetched_at = loc, now
            return self._cache

    def cached(self) -> dict | None:
        """The current cache without ever hitting the network (may be None)."""
        with self._lock:
            return self._cache

    def warm(self) -> None:
        """Prefetch in the background so the first read is instant."""
        from threading import Thread
        Thread(target=self.get, daemon=True, name="venom-geoip").start()

    @staticmethod
    def _format(loc: dict | None) -> str:
        if not loc:
            return ""
        parts = [loc.get("city"), loc.get("region"), loc.get("country")]
        return ", ".join(p for p in parts if p)

    def describe(self) -> str:
        """'City, Region, Country' — fetches if needed (may block briefly)."""
        return self._format(self.get())

    def describe_cached(self) -> str:
        """Non-blocking: describe only what's already been fetched."""
        return self._format(self.cached())
