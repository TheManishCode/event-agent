"""Multi-city trip search (ADR-030): the system orders the stops, picks dates, searches each distinct leg once, and ranks packages."""

import json
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from eventmate.schemas import FlightOption, FlightSearch, FlightSegmentInfo, TripRequest, TripStop
from eventmate.service import EventMate
from eventmate.tests.conftest import TODAY
from eventmate.travel import FlightProvider, TravelError
from eventmate.tripsearch import search_trip
from eventmate.web.app import create_app

START = date(2026, 12, 12)


def opt(frm, to, day, price, minutes=120, stops=0):
    seg = FlightSegmentInfo(
        airline_code="6E", airline_name="IndiGo", flight_number="100", from_code=frm, to_code=to,
        departs=datetime(day.year, day.month, day.day, 9, 0), arrives=datetime(day.year, day.month, day.day, 11, 0), duration_min=minutes,
    )
    return FlightOption(price_inr=price, duration_min=minutes, stops=stops, segments=(seg,), source="tool:fli")


class Fake(FlightProvider):
    """price_fn(origin, dest, day) -> list of (price, minutes) or raises TravelError."""

    def __init__(self, price_fn):
        self.price_fn, self.calls = price_fn, []

    def search_one_way(self, origin, destination, day, adults=1, cabin="economy", max_stops=None, limit=10):
        self.calls.append((origin, destination, day))
        rows = self.price_fn(origin, destination, day)
        return FlightSearch(
            origin=origin, destination=destination, travel_date=day, fetched_at=datetime(2026, 10, 30, 9), source="tool:fli",
            options=tuple(opt(origin, destination, day, p, m) for p, m in rows),
        )


def flat(price=5000, minutes=120):
    return lambda o, d, day: [(price, minutes)]


def req(**kw):
    base = dict(origin="DEL", stops=(TripStop(code="BOM", nights=3), TripStop(code="GOI", nights=2)), earliest_start=START)
    base.update(kw)
    return TripRequest(**base)


# --------------------------------------------------------------------- the request


@pytest.mark.parametrize("kw", [
    {"stops": ()}, {"stops": tuple(TripStop(code=c, nights=1) for c in ("BOM", "GOI", "BLR", "MAA", "HYD"))},
    {"stops": (TripStop(code="BOM", nights=1), TripStop(code="BOM", nights=2))}, {"stops": (TripStop(code="DEL", nights=1),)},
    {"flex_days": 4}, {"adults": 0}, {"max_searches": 61}, {"max_searches": 0}, {"cabin": "warp"}, {"origin": "del"}, {"return_to": "D3L"},
])
def test_request_rejects_bad_input(kw):
    with pytest.raises(ValidationError):
        req(**kw)


def test_stop_nights_are_bounded_and_extras_forbidden():
    for bad in (0, 31):
        with pytest.raises(ValidationError):
            TripStop(code="BOM", nights=bad)
    with pytest.raises(ValidationError):
        TripStop(code="BOM", nights=1, extra=1)


# ---------------------------------------------------------------------- the search


def test_fixed_order_builds_legs_with_dates_from_the_nights():
    fake = Fake(flat())
    r = search_trip(fake, req(fixed_order=True), TODAY)
    assert sorted(fake.calls) == sorted([("DEL", "BOM", date(2026, 12, 12)), ("BOM", "GOI", date(2026, 12, 15)), ("GOI", "DEL", date(2026, 12, 17))])
    p = r.packages[0]
    assert [(l.origin, l.destination, l.date) for l in p.legs] == [("DEL", "BOM", date(2026, 12, 12)), ("BOM", "GOI", date(2026, 12, 15)), ("GOI", "DEL", date(2026, 12, 17))]
    assert p.order == ("BOM", "GOI") and p.total_price_inr == 15000 and p.total_flight_minutes == 360 and r.searches_used == 3


def test_return_to_another_airport_ends_the_trip_there():
    r = search_trip(Fake(flat()), req(fixed_order=True, return_to="BLR"), TODAY)
    assert r.packages[0].legs[-1].destination == "BLR"


def test_the_system_tries_every_order_and_finds_the_cheapest():
    # GOI first is cheap to reach from DEL, and returning from BOM is cheap: so GOI -> BOM wins overall
    def prices(o, d, day):
        return [{("DEL", "GOI"): (3000, 150), ("GOI", "BOM"): (2000, 80), ("BOM", "DEL"): (2500, 130)}.get((o, d), (9000, 130))]

    r = search_trip(Fake(prices), req(), TODAY)
    assert r.orderings_considered == 2
    cheapest = next(p for p in r.packages if p.label == "cheapest")
    assert cheapest.order == ("GOI", "BOM") and cheapest.total_price_inr == 7500


def test_each_distinct_leg_is_searched_once_even_when_orders_share_it():
    fake = Fake(flat())
    stops = tuple(TripStop(code=c, nights=2) for c in ("BOM", "GOI", "BLR"))
    r = search_trip(fake, req(stops=stops), TODAY)
    assert len(fake.calls) == len(set(fake.calls)) == r.searches_used and r.orderings_considered == 6
    assert r.searches_used < 6 * 4  # shared legs are not repeated


def test_too_many_combinations_is_refused_before_any_search_with_a_repair():
    fake = Fake(flat())
    stops = tuple(TripStop(code=c, nights=n) for c, n in (("BOM", 1), ("GOI", 2), ("BLR", 3), ("MAA", 4)))
    with pytest.raises(ValueError, match=r"needs \d+ searches.*fixed_order"):
        search_trip(fake, req(stops=stops, max_searches=20), TODAY)
    assert fake.calls == []


def test_a_flexible_start_date_can_be_cheaper_and_the_insight_says_by_how_much():
    def prices(o, d, day):
        return [(4000 if day.day in (12, 15, 17) else 6000, 120)]  # the legs of a Dec 12 start are cheap

    r = search_trip(Fake(prices), req(fixed_order=True, flex_days=2), TODAY)
    cheapest = next(p for p in r.packages if p.label == "cheapest")
    assert cheapest.start_date == date(2026, 12, 12) and cheapest.total_price_inr == 3 * 4000
    r2 = search_trip(Fake(lambda o, d, day: [(6000 if day <= date(2026, 12, 13) else 4000, 120)]), req(fixed_order=True, flex_days=3), TODAY)
    assert any("later" in i and "saves INR" in i for i in r2.insights)


def test_labels_are_cheapest_fastest_and_best_value_and_they_can_differ():
    def prices(o, d, day):
        return [(3000, 400), (6000, 100)]  # a slow cheap flight and a fast expensive one on every leg

    r = search_trip(Fake(prices), req(fixed_order=True, value_per_hour_inr=1000), TODAY)
    by = {p.label: p for p in r.packages}
    assert by["cheapest"].total_price_inr == 9000 and by["cheapest"].total_flight_minutes == 1200
    assert by["fastest"].total_price_inr == 18000 and by["fastest"].total_flight_minutes == 300
    assert by["best_value"].total_price_inr == 18000  # 1000/hour makes the fast one worth it
    r0 = search_trip(Fake(prices), req(fixed_order=True, value_per_hour_inr=0), TODAY)
    assert {p.label: p for p in r0.packages}["best_value"].total_price_inr == 9000


def test_a_failing_leg_drops_only_the_orders_that_need_it():
    def prices(o, d, day):
        if (o, d) == ("BOM", "GOI"):
            raise TravelError("no service for BOM-GOI")
        return [(5000, 120)]

    r = search_trip(Fake(prices), req(), TODAY)
    assert {p.order for p in r.packages} == {("GOI", "BOM")} and r.dropped_orders >= 1
    assert any("BOM-GOI" in w or "BOM to GOI" in w for w in r.warnings)


def test_if_nothing_is_priced_the_result_is_empty_and_explains_why():
    r = search_trip(Fake(lambda o, d, day: []), req(fixed_order=True), TODAY)
    assert r.packages == () and r.warnings and "no complete itinerary" in r.warnings[-1]


def test_unpriced_options_are_never_used_to_build_a_total():
    class NoPrice(Fake):
        def search_one_way(self, o, d, day, **k):
            s = super().search_one_way(o, d, day, **k)
            bad = FlightOption(price_inr=None, duration_min=60, stops=0, segments=(opt(o, d, day, 1).segments[0],), source="tool:fli")
            return s.model_copy(update={"options": (bad,)})

    assert search_trip(NoPrice(flat()), req(fixed_order=True), TODAY).packages == ()


def test_past_start_dates_are_refused():
    fake = Fake(flat())
    with pytest.raises(ValueError, match="past"):
        search_trip(fake, req(earliest_start=date(2026, 10, 1)), TODAY)
    assert fake.calls == []


def test_the_deadline_returns_partial_results_and_says_so():
    import time

    def slow(o, d, day):
        time.sleep(0.3)
        return [(5000, 120)]

    r = search_trip(Fake(slow), req(fixed_order=True), TODAY, deadline_s=0.05, workers=1)
    assert any("time limit" in w for w in r.warnings)


def test_result_is_labelled_indicative_and_not_a_booking():
    r = search_trip(Fake(flat()), req(fixed_order=True), TODAY)
    assert r.source == "tool:fli" and "not a multi-city fare" in r.note and "does not book" in r.notice


# ------------------------------------------------------------- service, API, trace


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="trip", flights=Fake(flat()))


def test_service_records_every_search_in_the_tool_call_log(em):
    em.plan_trip(req(fixed_order=True), TODAY)
    entries = em.logs.tool_calls.read()
    assert len(entries) == 3 and all(e.tool == "flights.search_one_way" and e.ok for e in entries)
    assert "appid" not in json.dumps([e.model_dump(mode="json") for e in entries])


BODY = {"origin": "DEL", "stops": [{"code": "BOM", "nights": 3}, {"code": "GOI", "nights": 2}], "earliest_start": "2026-12-12"}


def post(c, url, body):
    t = c.get("/api/session").get_json()["csrf"]
    return c.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": t, "Content-Type": "application/json"})


def test_api_returns_packages_and_rejects_bad_requests(em):
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    r = post(c, "/api/travel/trip", BODY)
    j = r.get_json()
    assert r.status_code == 200 and {p["label"] for p in j["packages"]} >= {"cheapest", "fastest", "best_value"}
    assert post(c, "/api/travel/trip", {**BODY, "stops": []}).status_code == 422
    assert post(c, "/api/travel/trip", {**BODY, "extra": 1}).status_code == 422
    assert post(c, "/api/travel/trip", {**BODY, "earliest_start": "2020-01-01"}).status_code in (400, 422)
    big = {**BODY, "stops": [{"code": c_, "nights": n} for c_, n in (("BOM", 1), ("GOI", 2), ("BLR", 3), ("MAA", 4))], "max_searches": 20}
    r = post(c, "/api/travel/trip", big)
    assert r.status_code == 400 and "fixed_order" in r.get_json()["error"]
