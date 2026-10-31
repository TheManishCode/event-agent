"""Venue discovery from OpenStreetMap through the Overpass API (ADR-028).

Results are candidates, not facts about price or availability, and are never written into the vendor database. The only user
text that reaches a query is the city name, and it is checked against a strict allow-list first.
"""

from __future__ import annotations

import os
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from eventmate.netutil import NETWORK_ERRORS, loads, read_capped
from eventmate.schemas import DiscoveryResult, VenueCandidate

DEFAULT_URL = "https://overpass-api.de/api/interpreter"
MAX_REPLY_BYTES = 2_000_000
TIMEOUT_S = 30
CACHE_TTL_S = 3600
CACHE_MAX = 64
MAX_LIMIT = 100
USER_AGENT = "EventMate/0.1 (venue discovery; contact via project owner)"

TAGS = {
    "venue": '["amenity"~"^(events_venue|conference_centre|community_centre)$"]',
    "hotel": '["tourism"="hotel"]',
    "restaurant": '["amenity"="restaurant"]',
}
_CITY_PUNCT = set(" .'\u2019-")


class DiscoveryError(Exception):
    """The place search failed. The message is safe to show."""


def clean_city(city: str) -> str:
    if not isinstance(city, str):
        raise ValueError("city must be text")
    city = " ".join(city.split()) if all(unicodedata.category(c) != "Cc" for c in city) else ""
    if not city or len(city) > 60:
        raise ValueError("city must be 1-60 characters")
    if not all(unicodedata.category(c)[0] in "LMN" or c in _CITY_PUNCT for c in city):
        raise ValueError("city may contain only letters, digits, spaces and . ' -")
    return city


def build_query(city: str, kind: str, limit: int) -> str:
    city = clean_city(city)
    if kind not in TAGS:
        raise ValueError(f"kind must be one of {', '.join(TAGS)}")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be 1-{MAX_LIMIT}")
    return (
        f'[out:json][timeout:25];\narea["boundary"="administrative"]["name"="{city}"]->.a;\n'
        f'nwr(area.a){TAGS[kind]}["name"];\nout center {limit};'
    )


def _urllib_transport(url: str, query: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": query}).encode(), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # url scheme is checked in OverpassSource.__init__
        return read_capped(resp, MAX_REPLY_BYTES + 1)


def _opt(model, **fields):
    """Build a candidate, dropping only the optional fields that fail validation rather than trusting them."""
    for key in ("street", "city", "website", "capacity_tagged", "email"):
        if fields.get(key) is None:
            continue
        try:
            model(**{**fields, **{k: None for k in ("street", "city", "website", "capacity_tagged", "email") if k != key}})
        except ValueError:
            fields[key] = None
    return model(**fields)


class OverpassSource:
    def __init__(self, url: Optional[str] = None, transport: Callable[[str, str, float], bytes] = _urllib_transport, clock: Callable[[], float] = time.monotonic):
        url = url or os.environ.get("EVENTMATE_OVERPASS_URL") or DEFAULT_URL
        if not url.startswith("https://"):
            raise ValueError("the Overpass endpoint must be https")
        self.url, self._transport, self._clock = url, transport, clock
        self._cache: Dict[Tuple[str, str, int], Tuple[float, DiscoveryResult]] = {}
        self._lock = threading.Lock()  # guards the cache only, never held during a request

    def venues(self, city: str, kind: str = "venue", limit: int = 20) -> DiscoveryResult:
        query = build_query(city, kind, limit)  # validates everything before any request
        city = clean_city(city)
        key = (city.casefold(), kind, limit)
        with self._lock:
            hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < CACHE_TTL_S:
            return hit[1]
        try:
            raw = self._transport(self.url, query, TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503, 504):
                raise DiscoveryError(f"the public place-search server answered HTTP {exc.code} (busy or down); try again in a minute, or set EVENTMATE_OVERPASS_URL to another Overpass server") from exc
            raise DiscoveryError(f"the place search was refused (HTTP {exc.code})") from exc
        except NETWORK_ERRORS as exc:
            raise DiscoveryError("the place search service could not be reached; try again shortly") from exc
        if len(raw) > MAX_REPLY_BYTES:
            raise DiscoveryError("the place search reply was too large and was discarded")
        try:
            doc = loads(raw)
        except ValueError as exc:
            raise DiscoveryError("the place search returned something unreadable") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("elements"), list):
            raise DiscoveryError("the place search returned an unexpected shape")
        remark = doc.get("remark")
        if isinstance(remark, str) and ("error" in remark.lower() or "timed out" in remark.lower()):
            raise DiscoveryError(f"the place search was incomplete: {remark[:120]}")
        candidates, warnings = self._parse(doc["elements"], kind)
        result = DiscoveryResult(city=city, kind=kind, candidates=tuple(candidates[:limit]), fetched_at=datetime.now(timezone.utc), warnings=tuple(warnings))
        with self._lock:
            if len(self._cache) >= CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (self._clock(), result)
        return result

    @staticmethod
    def _parse(elements: List[object], kind: str) -> Tuple[List[VenueCandidate], List[str]]:
        seen, out = set(), []
        unnamed = bad = 0
        for el in elements:
            if not isinstance(el, dict) or not isinstance(el.get("tags"), dict):
                bad += 1
                continue
            tags = el["tags"]
            ref = f"{el.get('type')}/{el.get('id')}"
            if ref in seen:
                continue
            seen.add(ref)
            name = tags.get("name")
            if not isinstance(name, str) or not name.strip():
                unnamed += 1
                continue
            at = el if "lat" in el else el.get("center") or {}
            cap = tags.get("capacity")
            try:
                out.append(_opt(
                    VenueCandidate, osm_ref=ref, name=name.strip()[:120], kind=kind, lat=at["lat"], lon=at["lon"],
                    street=tags.get("addr:street"), city=tags.get("addr:city"), website=tags.get("website") or tags.get("contact:website"),
                    email=tags.get("contact:email") or tags.get("email"),
                    capacity_tagged=int(cap) if isinstance(cap, str) and cap.isdigit() else None,
                ))
            except (KeyError, TypeError, ValueError):
                bad += 1
        warnings = []
        if unnamed:
            warnings.append(f"{unnamed} unnamed element(s) skipped")
        if bad:
            warnings.append(f"{bad} element(s) could not be read and were skipped")
        return out, warnings
