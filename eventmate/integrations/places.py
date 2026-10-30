"""Ratings, review counts, price level and (optionally) reviews from the Google Places API (New) (ADR-033).

The key is sent in a header and never appears in a result, an error or a log. Reviews are requested only on demand because they bill at a
higher tier. Places are ranked by a confidence-weighted rating so a few perfect reviews do not beat hundreds of good ones.
Field names follow Google's published Places API (New) reference and have not yet been run against a live key.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from eventmate.integrations.discovery import clean_city
from eventmate.netutil import NETWORK_ERRORS, loads, read_capped
from eventmate.schemas import PlaceInfo, PlaceReview, PlaceSearch

URL = "https://places.googleapis.com/v1/places:searchText"
BASE_FIELDS = "places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.priceLevel,places.googleMapsUri"
MAX_REPLY_BYTES = 2_000_000
TIMEOUT_S = 20
CACHE_TTL_S = 600
CACHE_MAX = 64
PRIOR_REVIEWS = 20  # weight of the prior: a place needs about this many reviews before its own rating counts for half
PRIOR_MEAN = 4.0
_KEY_OK = re.compile(r"[A-Za-z0-9_-]{20,80}")
_PRICE = {"PRICE_LEVEL_INEXPENSIVE": 1, "PRICE_LEVEL_MODERATE": 2, "PRICE_LEVEL_EXPENSIVE": 3, "PRICE_LEVEL_VERY_EXPENSIVE": 4}
_TAGS = re.compile(r"<[^>]+>")
QUERIES = {"caterer": "wedding caterers", "venue": "banquet halls and wedding venues", "photographer": "wedding photographers", "hotel": "hotels"}


class PlacesError(Exception):
    """The places lookup failed. The message is safe to show and never contains the key."""


class PlacesNotConfigured(PlacesError):
    pass


def _post(url: str, headers: dict, data: bytes, timeout: float) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as resp:
        return read_capped(resp, MAX_REPLY_BYTES + 1)


def _text(v, cap: int) -> str:
    v = _TAGS.sub("", v if isinstance(v, str) else "")
    return " ".join("".join(c for c in v if c.isprintable() or c == " ").split())[:cap]


def weighted(rating: Optional[float], count: Optional[int]) -> Optional[float]:
    if rating is None or not count:
        return None
    return round((count / (count + PRIOR_REVIEWS)) * rating + (PRIOR_REVIEWS / (count + PRIOR_REVIEWS)) * PRIOR_MEAN, 2)


class PlacesSource:
    def __init__(self, api_key: Optional[str] = None, transport: Callable[[str, dict, bytes, float], bytes] = _post, clock: Callable[[], float] = time.monotonic):
        self._key, self._transport, self._clock = api_key, transport, clock
        self._cache: Dict[Tuple[str, str, bool], Tuple[float, PlaceSearch]] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "PlacesSource(key=<hidden>)"

    def configured(self) -> bool:
        return bool((self._key if self._key is not None else os.environ.get("GOOGLE_PLACES_API_KEY", "")).strip())

    def _api_key(self) -> str:
        key = (self._key if self._key is not None else os.environ.get("GOOGLE_PLACES_API_KEY", "")).strip()
        if not key:
            raise PlacesNotConfigured("places are not configured: add GOOGLE_PLACES_API_KEY to the .env file")
        if not _KEY_OK.fullmatch(key):
            raise PlacesError("GOOGLE_PLACES_API_KEY has an unexpected format")
        return key

    def search(self, kind: str, city: str, reviews: bool = False, limit: int = 10) -> PlaceSearch:
        if kind not in QUERIES:
            raise ValueError(f"kind must be one of {', '.join(QUERIES)}")
        city = clean_city(city)
        if not 1 <= limit <= 20:
            raise ValueError("limit must be 1-20")
        key = self._api_key()
        ck = (kind, city.casefold(), reviews)
        with self._lock:
            hit = self._cache.get(ck)
        if hit and self._clock() - hit[0] < CACHE_TTL_S:
            return hit[1]
        fields = BASE_FIELDS + (",places.reviews" if reviews else "")
        body = {"textQuery": f"{QUERIES[kind]} in {city}", "maxResultCount": limit, "languageCode": "en", "regionCode": "IN"}
        headers = {"X-Goog-Api-Key": key, "X-Goog-FieldMask": fields, "Content-Type": "application/json"}
        try:
            raw = self._transport(URL, headers, json.dumps(body).encode(), TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            msg = {
                400: "Google rejected the request or the API key; check GOOGLE_PLACES_API_KEY",
                403: "Google refused the request; enable the Places API (New) for this key and check its restrictions",
                429: "the Google Places quota is used up; try again later",
            }.get(exc.code, f"the Places request failed (HTTP {exc.code})")
            raise PlacesError(msg) from None
        except NETWORK_ERRORS:
            raise PlacesError("Google Places could not be reached; try again shortly") from None
        if len(raw) > MAX_REPLY_BYTES:
            raise PlacesError("the Places reply was too large and was discarded")
        try:
            items = loads(raw)
            items = items.get("places", []) if isinstance(items, dict) else None
        except ValueError:
            items = None
        if not isinstance(items, list):
            raise PlacesError("Google Places returned something unreadable")
        places: List[PlaceInfo] = []
        skipped = 0
        for it in items:
            try:
                places.append(self._place(it, reviews))
            except (ValueError, TypeError, AttributeError, KeyError):
                skipped += 1
        places.sort(key=lambda p: (p.weighted_rating is None, -(p.weighted_rating or 0), p.name))
        result = PlaceSearch(
            kind=kind, city=city, places=tuple(places), fetched_at=datetime.now(timezone.utc),
            warnings=(f"{skipped} place(s) could not be read and were skipped",) if skipped else (),
        )
        with self._lock:
            if len(self._cache) >= CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))
            self._cache[ck] = (self._clock(), result)
        return result

    @staticmethod
    def _place(it: dict, reviews: bool) -> PlaceInfo:
        name = _text(it["displayName"]["text"], 120)
        if not name:
            raise ValueError("no name")
        rating = it.get("rating") if isinstance(it.get("rating"), (int, float)) and not isinstance(it.get("rating"), bool) and 0 <= it["rating"] <= 5 else None
        count = it.get("userRatingCount") if isinstance(it.get("userRatingCount"), int) and it["userRatingCount"] >= 0 else None
        uri = it.get("googleMapsUri")
        revs = []
        for r in (it.get("reviews") or [])[:5] if reviews else []:
            text = _text(((r.get("text") or r.get("originalText") or {}).get("text")), 300)
            stars = r.get("rating")
            if text:
                revs.append(PlaceReview(rating=stars if isinstance(stars, int) and 1 <= stars <= 5 else None, text=text, when=_text(r.get("relativePublishTimeDescription"), 60) or None))
        fields = dict(
            name=name, address=_text(it.get("formattedAddress"), 200) or None, rating=rating, rating_count=count, weighted_rating=weighted(rating, count),
            price_level=_PRICE.get(it.get("priceLevel")), reviews=tuple(revs),
        )
        try:
            return PlaceInfo(maps_uri=uri if isinstance(uri, str) else None, **fields)
        except ValueError:
            return PlaceInfo(**fields)  # a link that is not Google Maps is dropped, the place is kept
