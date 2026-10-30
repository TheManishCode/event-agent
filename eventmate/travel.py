"""Flight fares and schedules from fli (Google Flights), behind a small provider interface (ADR-028).

Facts here come only from the tool and are tagged ``tool:fli``. Fares are indicative: nothing is booked or paid. Google serves
multi-city results through a gated RPC, which fli refuses (``SearchUnsupportedError``, upstream issue #223), so multi-city is
composed from separate one-way searches and labelled as such.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence, Tuple

from eventmate.schemas import (
    MAX_FLIGHT_OPTIONS,
    MAX_MULTI_CITY_LEGS,
    FlightOption,
    FlightSearch,
    FlightSegmentInfo,
    MultiCityResult,
)

SOURCE = "tool:fli"
IATA = re.compile(r"[A-Z]{3}")
CABINS = ("economy", "premium_economy", "business", "first")
MAX_ADULTS = 9
MAX_DAYS_AHEAD = 365  # Google Flights sells about this far ahead


def check_date(day: date, today: date) -> None:
    """Refuse dates in the past or so far ahead that date arithmetic or the provider would misbehave."""
    if day < today:
        raise ValueError(f"{day.isoformat()} is in the past")
    if day > today + timedelta(days=MAX_DAYS_AHEAD):
        raise ValueError(f"{day.isoformat()} is too far ahead; searches cover the next {MAX_DAYS_AHEAD} days")


MULTI_CITY_NOTE = (
    "Built from separate one-way searches, one per leg: this is not a multi-city fare, and booking the legs together can price "
    "differently. Google does not serve multi-city results to fli."
)


class TravelError(Exception):
    """The flight tool failed. The message is safe to show; internals stay in ``__cause__``."""


class FlightProvider:
    def search_one_way(
        self, origin: str, destination: str, day: date, adults: int = 1, cabin: str = "economy", max_stops: Optional[int] = None, limit: int = 10
    ) -> FlightSearch:
        raise NotImplementedError


def check_search(origin: str, destination: str, adults: int, cabin: str, limit: int) -> None:
    for code in (origin, destination):
        if not isinstance(code, str) or not IATA.fullmatch(code):
            raise ValueError(f"airport code must be three capital letters, got {code!r}")
    if origin == destination:
        raise ValueError("origin and destination are the same airport")
    if not 1 <= adults <= MAX_ADULTS:
        raise ValueError(f"adults must be 1-{MAX_ADULTS}")
    if cabin not in CABINS:
        raise ValueError(f"cabin must be one of {', '.join(CABINS)}")
    if not 1 <= limit <= MAX_FLIGHT_OPTIONS:
        raise ValueError(f"limit must be 1-{MAX_FLIGHT_OPTIONS}")


class FliProvider(FlightProvider):
    """Wraps ``fli.search.SearchFlights``. fli is an optional dependency and is imported on first use."""

    def __init__(self, search_factory: Optional[Callable[[], object]] = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._factory, self._now = search_factory, now

    def search_one_way(self, origin, destination, day, adults=1, cabin="economy", max_stops=None, limit=10) -> FlightSearch:
        check_search(origin, destination, adults, cabin, limit)
        if max_stops is not None and not 0 <= max_stops <= 2:
            raise ValueError("max_stops must be 0, 1 or 2")
        try:
            from fli.models import Airport, FlightSearchFilters, FlightSegment, MaxStops, PassengerInfo, SeatType, SortBy
            from fli.search import SearchFlights
            from fli.search.exceptions import SearchClientError
        except ImportError as exc:
            raise TravelError("flight search is not installed: pip install the fli package (see eventmate/README.md)") from exc
        try:
            frm, to = Airport[origin], Airport[destination]
        except KeyError as exc:
            raise ValueError(f"unknown airport code {exc.args[0]!r}") from exc
        stops = {None: MaxStops.ANY, 0: MaxStops.NON_STOP, 1: MaxStops.ONE_STOP_OR_FEWER, 2: MaxStops.TWO_OR_FEWER_STOPS}[max_stops]
        filters = FlightSearchFilters(
            passenger_info=PassengerInfo(adults=adults),
            flight_segments=[FlightSegment(departure_airport=[[frm, 0]], arrival_airport=[[to, 0]], travel_date=day.isoformat())],
            seat_type=SeatType[cabin.upper()],
            stops=stops,
            sort_by=SortBy.CHEAPEST,
        )
        search = self._factory() if self._factory else SearchFlights()
        try:
            results = search.search(filters, currency="INR") or []
        except SearchClientError as exc:
            raise TravelError(f"Google Flights could not be searched ({type(exc).__name__}); try again shortly") from exc
        except Exception as exc:  # fli or the network misbehaved; never leak internals to the caller
            raise TravelError("the flight search failed unexpectedly") from exc
        warnings: List[str] = []
        options: List[FlightOption] = []
        for r in results:
            try:
                options.append(self._option(search, r, cabin, warnings))
            except Exception:
                warnings.append("one result could not be read and was skipped")
        options.sort(key=lambda o: (o.price_inr is None, o.price_inr or 0, o.duration_min))
        warnings = list(dict.fromkeys(warnings))
        return FlightSearch(
            origin=origin, destination=destination, travel_date=day, options=tuple(options[:limit]), fetched_at=self._now(), source=SOURCE, warnings=tuple(warnings)
        )

    @staticmethod
    def _option(search, r, cabin: str, warnings: List[str]) -> FlightOption:
        price = None
        if r.price is not None:
            if (r.currency or "").upper() == "INR":
                price = int(round(r.price))
            else:
                warnings.append(f"a fare was published in {r.currency or 'an unknown currency'}, not INR; it is shown without a price")
        segments = tuple(
            FlightSegmentInfo(
                airline_code=leg.airline.name.lstrip("_"), airline_name=str(leg.airline.value), flight_number=str(leg.flight_number),
                from_code=leg.departure_airport.name, to_code=leg.arrival_airport.name,
                departs=leg.departure_datetime, arrives=leg.arrival_datetime, duration_min=leg.duration,
            )
            for leg in r.legs
        )
        try:
            from fli.models import SeatType

            url = search.build_flight_booking_url(r, currency="INR", seat_type=SeatType[cabin.upper()])
        except Exception:
            url = None
        try:
            return FlightOption(price_inr=price, duration_min=r.duration, stops=r.stops, segments=segments, booking_url=url, source=SOURCE)
        except ValueError:
            return FlightOption(price_inr=price, duration_min=r.duration, stops=r.stops, segments=segments, source=SOURCE)  # unusable link is dropped


def search_multi_city(
    provider: FlightProvider, legs: Sequence[Tuple[str, str, date]], adults: int = 1, cabin: str = "economy", max_stops: Optional[int] = None, limit: int = 5
) -> MultiCityResult:
    """One search per leg, in order. A failed leg is reported on that leg and removes the total; it is never guessed."""
    legs = list(legs)
    if len(legs) < 2:
        raise ValueError("multi-city needs at least 2 legs")
    if len(legs) > MAX_MULTI_CITY_LEGS:
        raise ValueError(f"multi-city allows at most {MAX_MULTI_CITY_LEGS} legs")
    for i, (o, d, day) in enumerate(legs):
        check_search(o, d, adults, cabin, limit)
        if i and day < legs[i - 1][2]:
            raise ValueError(f"leg {i + 1} departs on {day.isoformat()}, not before leg {i} ({legs[i - 1][2].isoformat()})")
    results: List[FlightSearch] = []
    for o, d, day in legs:
        try:
            results.append(provider.search_one_way(o, d, day, adults=adults, cabin=cabin, max_stops=max_stops, limit=limit))
        except TravelError as exc:
            results.append(FlightSearch(origin=o, destination=d, travel_date=day, fetched_at=datetime.now(timezone.utc), source=SOURCE, error=str(exc)[:300]))
    cheapest: List[Optional[int]] = []
    for r in results:
        prices = [x.price_inr for x in r.options if x.price_inr is not None]
        cheapest.append(min(prices) if prices else None)
    incomplete = tuple(i for i, c in enumerate(cheapest) if c is None)
    return MultiCityResult(legs=tuple(results), total_cheapest_inr=None if incomplete else sum(cheapest), incomplete_legs=incomplete, note=MULTI_CITY_NOTE)
