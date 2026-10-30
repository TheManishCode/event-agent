"""Weather forecast from OpenWeather (free 5-day / 3-hour forecast API) (ADR-029).

The key comes from OPENWEATHER_API_KEY and is only ever placed in the request URL: it is never logged and never appears in a result
or an error message. Beyond the forecast horizon (about five days) the answer says so; nothing is estimated.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from eventmate.discovery import clean_city
from eventmate.netutil import NETWORK_ERRORS, loads, read_capped
from eventmate.schemas import RAIN_POSSIBLE_AT, WeatherDay, WeatherSlot

BASE = "https://api.openweathermap.org"
MAX_REPLY_BYTES = 2_000_000
TIMEOUT_S = 15
CACHE_TTL_S = 600
CACHE_MAX = 64
USER_AGENT = "EventMate/0.1"
_KEY_OK = re.compile(r"[A-Za-z0-9_-]{8,128}")


class WeatherError(Exception):
    """The weather lookup failed. The message is safe to show and never contains the key."""


class WeatherNotConfigured(WeatherError):
    pass


def _urllib_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # the URL is always built from BASE
        return read_capped(resp, MAX_REPLY_BYTES + 1)


class OpenWeatherSource:
    def __init__(self, api_key: Optional[str] = None, transport: Callable[[str, float], bytes] = _urllib_get, clock: Callable[[], float] = time.monotonic):
        self._key, self._transport, self._clock = api_key, transport, clock
        self._cache: Dict[str, Tuple[float, str, dict]] = {}
        self._lock = threading.Lock()  # guards the cache only, never held during a request

    def __repr__(self) -> str:
        return "OpenWeatherSource(key=<hidden>)"

    def configured(self) -> bool:
        """True if a key is present. Says nothing about whether OpenWeather will accept it."""
        return bool((self._key if self._key is not None else os.environ.get("OPENWEATHER_API_KEY", "")).strip())

    def _api_key(self) -> str:
        key = (self._key if self._key is not None else os.environ.get("OPENWEATHER_API_KEY", "")).strip()
        if not key:
            raise WeatherNotConfigured("weather is not configured: add OPENWEATHER_API_KEY to the .env file (see eventmate/README.md)")
        if not _KEY_OK.fullmatch(key):
            raise WeatherError("OPENWEATHER_API_KEY has an unexpected format")
        return key

    def _get(self, path: str, params: dict, key: str):
        url = f"{BASE}{path}?" + urllib.parse.urlencode({**params, "appid": key})
        try:
            raw = self._transport(url, TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 401:
                raise WeatherError("OpenWeather rejected the API key (a new key can take a couple of hours to activate)") from None
            if code == 429:
                raise WeatherError("OpenWeather rate limit reached; try again later") from None
            if code == 404:
                raise WeatherError("that place was not found") from None
            raise WeatherError(f"the weather request failed (HTTP {code})") from None
        except NETWORK_ERRORS:
            raise WeatherError("the weather service could not be reached; try again shortly") from None
        if len(raw) > MAX_REPLY_BYTES:
            raise WeatherError("the weather reply was too large and was discarded")
        try:
            return loads(raw)
        except ValueError:
            raise WeatherError("the weather service returned something unreadable") from None

    def forecast(self, city: str, day: date) -> WeatherDay:
        city = clean_city(city)  # validates before any request
        key = self._api_key()
        ck = city.casefold()
        with self._lock:
            hit = self._cache.get(ck)
        if hit and self._clock() - hit[0] < CACHE_TTL_S:
            place, doc = hit[1], hit[2]
        else:
            geo = self._get("/geo/1.0/direct", {"q": city, "limit": 1}, key)
            if not isinstance(geo, list) or not geo or not isinstance(geo[0], dict):
                raise WeatherError("city not found")
            g = geo[0]
            try:
                lat, lon = float(g["lat"]), float(g["lon"])
            except (KeyError, TypeError, ValueError):
                raise WeatherError("the weather service returned an unreadable place") from None
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise WeatherError("the weather service returned an unreadable place")
            place = ", ".join(str(g[k]) for k in ("name", "state", "country") if isinstance(g.get(k), str) and g[k].strip())[:200] or city
            doc = self._get("/data/2.5/forecast", {"lat": lat, "lon": lon, "units": "metric"}, key)
            if not isinstance(doc, dict) or not isinstance(doc.get("list"), list):
                raise WeatherError("the weather service returned an unexpected shape")
            with self._lock:
                if len(self._cache) >= CACHE_MAX:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[ck] = (self._clock(), place, doc)
        return self._day(city, day, place, doc)

    @staticmethod
    def _day(city: str, day: date, place: str, doc: dict) -> WeatherDay:
        tz = doc.get("city", {}).get("timezone", 0) if isinstance(doc.get("city"), dict) else 0
        tz = tz if isinstance(tz, int) and abs(tz) <= 14 * 3600 else 0
        slots: List[WeatherSlot] = []
        local_days: List[date] = []
        skipped = 0
        for item in doc["list"]:
            try:
                local = datetime.fromtimestamp(int(item["dt"]), timezone.utc) + timedelta(seconds=tz)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                skipped += 1
                continue
            local_days.append(local.date())
            if local.date() != day:
                continue
            try:
                main = item["main"]
                w = item.get("weather") or [{}]
                desc = w[0].get("description") if isinstance(w[0], dict) else None
                base = dict(
                    local_time=local.strftime("%Y-%m-%dT%H:%M:%S"), temp_c=float(main["temp"]), feels_like_c=float(main.get("feels_like", main["temp"])),
                    humidity_pct=main.get("humidity") if isinstance(main.get("humidity"), int) else None, rain_chance=float(item.get("pop", 0.0)),
                    wind_ms=float(item["wind"]["speed"]) if isinstance(item.get("wind"), dict) and "speed" in item["wind"] else None,
                )
                try:
                    slots.append(WeatherSlot(description=desc if isinstance(desc, str) and desc else None, **base))
                except ValueError:
                    slots.append(WeatherSlot(**base))  # a bad description costs only the description
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                skipped += 1
        warnings = (f"{skipped} forecast entr{'y' if skipped == 1 else 'ies'} could not be read and were skipped",) if skipped else ()
        common = dict(city=city, travel_date=day, resolved_place=place, warnings=warnings, fetched_at=datetime.now(timezone.utc))
        if not slots:
            if local_days and day > max(local_days):
                note = f"{day.isoformat()} is beyond the forecast horizon (the forecast ends {max(local_days).isoformat()}); check again within five days of the date"
            elif local_days and day < min(local_days):
                note = f"{day.isoformat()} is before the forecast starts ({min(local_days).isoformat()})"
            else:
                note = "no forecast entries were returned for that day"
            return WeatherDay(note=note, **common)
        temps = [s.temp_c for s in slots]
        rain = max(s.rain_chance for s in slots)
        conditions = Counter(s.description for s in slots if s.description).most_common(1)
        return WeatherDay(
            slots=tuple(slots), temp_min=round(min(temps), 1), temp_max=round(max(temps), 1), max_rain_chance=round(rain, 2),
            rain_possible=rain >= RAIN_POSSIBLE_AT, conditions=conditions[0][0] if conditions else None, **common,
        )
