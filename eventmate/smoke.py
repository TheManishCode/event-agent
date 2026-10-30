"""Run every integration once, live, and say what works (ADR-035).

    python -m eventmate.smoke            free sources only: flights, trip, deals, venue discovery, weather and places (if keyed)
    python -m eventmate.smoke --llm      also pings each model provider with a few tokens and routes one sentence (spends a few credits)

Nothing is booked or paid. Services without a key are SKIPPED, not failed. A busy public server is a WARN. No key or secret value is printed.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, List, Optional

from eventmate.discovery import DiscoveryError
from eventmate.env import load_env
from eventmate.explore import route
from eventmate.llm import METER
from eventmate.places import PlacesNotConfigured
from eventmate.schemas import DealQuery, TripRequest, TripStop
from eventmate.service import EventMate
from eventmate.travel import TravelError
from eventmate.weather import WeatherNotConfigured
from eventmate.websearch import SearchError


@dataclass
class Row:
    name: str
    status: str  # PASS, WARN, SKIP, FAIL
    detail: str
    seconds: float = 0.0


def _check(name: str, fn: Callable[[], str], warn_on: tuple = (), skip_on: tuple = ()) -> Row:
    t0 = time.monotonic()
    try:
        detail = fn()
        return Row(name, "PASS", detail, time.monotonic() - t0)
    except skip_on as exc:
        return Row(name, "SKIP", str(exc)[:160], time.monotonic() - t0)
    except warn_on as exc:
        return Row(name, "WARN", str(exc)[:160], time.monotonic() - t0)
    except Exception as exc:  # a smoke test reports every failure by name instead of stopping
        return Row(name, "FAIL", f"{type(exc).__name__}: {str(exc)[:160]}", time.monotonic() - t0)


def run_smoke(em: EventMate, today: date, llm: bool = False) -> List[Row]:
    soon, later = today + timedelta(days=2), today + timedelta(days=45)

    def flights() -> str:
        r = em.search_flights("DEL", "BOM", later, limit=3)
        if not any(o.price_inr for o in r.options):
            raise TravelError("no priced flights came back")
        return f"{len(r.options)} options, cheapest INR {min(o.price_inr for o in r.options if o.price_inr):,}"

    def trip() -> str:
        req = TripRequest(origin="DEL", stops=(TripStop(code="BOM", nights=2), TripStop(code="GOI", nights=2)), earliest_start=later)
        r = em.plan_trip(req, today)
        if not r.packages:
            raise TravelError("no complete itinerary: " + "; ".join(r.warnings)[:100])
        p = r.packages[0]
        return f"{r.searches_used} searches, cheapest {' > '.join(p.order)} INR {p.total_price_inr:,}"

    def deals() -> str:
        r = em.find_deals(DealQuery(kind="caterer", city="Pune", guests=100))
        if not r.offers:
            raise SearchError(f"no offers (read {r.pages_read} pages; skipped {len(r.skipped)})")
        return f"{len(r.offers)} offers from {r.market.n_domains} sites via {r.provider}, median INR {r.market.median_low_inr:,} per plate"

    def discover() -> str:
        r = em.discover_venues("Pune", "venue", 5)
        return f"{len(r.candidates)} places"

    def weather() -> str:
        w = em.weather_forecast("Pune", soon)
        return f"{w.resolved_place}: {w.temp_min} to {w.temp_max} C, rain up to {round((w.max_rain_chance or 0) * 100)}%" if w.slots else (w.note or "no slots")

    def places() -> str:
        r = em.find_places("venue", "Pune")
        return f"{len(r.places)} places, best {r.places[0].name} ({r.places[0].rating})" if r.places else "no places"

    rows = [
        _check("flights", flights),
        _check("trip", trip),
        _check("deals", deals),
        _check("discover", discover, warn_on=(DiscoveryError,)),
        _check("weather", weather, skip_on=(WeatherNotConfigured,)),
        _check("places", places, skip_on=(PlacesNotConfigured,)),
    ]
    if not llm:
        rows.append(Row("llm", "SKIP", "not run: it spends credits; add --llm to ping each provider and route one sentence"))
        return rows
    clients = em.router_clients()
    if not clients:
        rows.append(Row("llm", "SKIP", "no model key (NVIDIA_API_KEY or OPENROUTER_API_KEY)"))
        return rows
    before = METER.status()["calls"]

    def ping() -> str:
        parts = []
        for c in clients:
            t0 = time.monotonic()
            out = c.complete("Reply with the single word OK.", "ping", 64)
            parts.append(f"{c.name} ok in {time.monotonic() - t0:.1f}s ({len(out)} chars)")
        return "; ".join(parts)

    rows.append(_check("llm", ping))
    rows.append(_check("explore", lambda: _explore(em, today, clients)))
    spent = METER.status()["calls"] - before
    rows[-2].detail += f"; {spent} metered model calls this run" if spent else "; calls not metered (custom client)"
    return rows


def _explore(em: EventMate, today: date, clients) -> str:
    r = route(em, "weather in Pune on " + (today + timedelta(days=2)).isoformat(), today, clients)
    return f"routed to {r.action} by {r.model}"


def build_em() -> EventMate:
    load_env()
    from eventmate.llm import client_from_env

    return EventMate(run_id="smoke", llm=client_from_env())


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m eventmate.smoke", description=__doc__.split("\n\n")[0])
    ap.add_argument("--llm", action="store_true", help="also ping the model providers (spends a few credits)")
    ap.add_argument("--today", type=date.fromisoformat, default=None)
    a = ap.parse_args(argv)
    rows = run_smoke(build_em(), a.today or date.today(), llm=a.llm)
    for r in rows:
        print(f"{r.status:5} {r.name:9} {r.seconds:5.1f}s  {r.detail}")
    counts = {s: sum(1 for r in rows if r.status == s) for s in ("PASS", "WARN", "SKIP", "FAIL")}
    print("  ".join(f"{k} {v}" for k, v in counts.items()))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
