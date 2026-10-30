"""Flight fares and schedules (ADR-028): provider adapter, multi-city built from separate searches, hostile data."""

import json
from datetime import date, datetime

import pytest
from pydantic import ValidationError

from eventmate.schemas import FlightOption, FlightSearch, FlightSegmentInfo
from eventmate.service import EventMate
from eventmate.tests.conftest import TODAY
from eventmate.travel import FlightProvider, FliProvider, TravelError, search_multi_city
from eventmate.web.app import create_app


def seg(frm="DEL", to="BOM", day=date(2026, 12, 12), flight="6107", airline="IndiGo"):
    return FlightSegmentInfo(
        airline_code="6E", airline_name=airline, flight_number=flight, from_code=frm, to_code=to,
        departs=datetime(day.year, day.month, day.day, 9, 30), arrives=datetime(day.year, day.month, day.day, 12, 5), duration_min=155,
    )


def option(price, **kw):
    return FlightOption(price_inr=price, duration_min=155, stops=0, segments=(seg(**kw),), source="tool:fli")


class Fake(FlightProvider):
    """Offline provider: returns canned options per (origin, destination) and records the calls."""

    def __init__(self, table=None, fail=()):
        self.table, self.fail, self.calls = table or {}, set(fail), []

    def search_one_way(self, origin, destination, day, adults=1, cabin="economy", max_stops=None, limit=10):
        self.calls.append((origin, destination, day))
        if (origin, destination) in self.fail:
            raise TravelError(f"no service for {origin}-{destination}")
        opts = tuple(sorted(self.table.get((origin, destination), ()), key=lambda o: (o.price_inr is None, o.price_inr or 0)))[:limit]
        return FlightSearch(origin=origin, destination=destination, travel_date=day, options=opts, fetched_at=datetime(2026, 10, 30, 9), source="tool:fli")


# ------------------------------------------------------------------ schemas


def test_option_and_search_reject_unknown_fields_bad_codes_and_control_characters():
    with pytest.raises(ValidationError):
        FlightOption(price_inr=100, duration_min=10, stops=0, segments=(seg(),), source="tool:fli", extra=1)
    for code in ("del", "DELH", "D1L", "../x"):
        with pytest.raises(ValidationError):
            seg(frm=code)
    with pytest.raises(ValidationError):
        seg(airline="Indi\nGo")
    with pytest.raises(ValidationError):
        FlightOption(price_inr=-1, duration_min=10, stops=0, segments=(seg(),), source="tool:fli")
    with pytest.raises(ValidationError):
        FlightOption(price_inr=100, duration_min=10, stops=0, segments=(), source="tool:fli")


def test_booking_url_must_be_https_google_flights():
    ok = FlightOption(price_inr=1, duration_min=10, stops=0, segments=(seg(),), source="tool:fli", booking_url="https://www.google.com/travel/flights?tfs=abc")
    assert ok.booking_url.startswith("https://www.google.com/")
    for bad in ("http://www.google.com/travel/flights?tfs=x", "https://evil.example/travel/flights", "javascript:alert(1)", "https://www.google.com.evil.example/x"):
        with pytest.raises(ValidationError):
            FlightOption(price_inr=1, duration_min=10, stops=0, segments=(seg(),), source="tool:fli", booking_url=bad)


# --------------------------------------------------------------- multi-city


def legs3():
    return [("DEL", "BOM", date(2026, 12, 12)), ("BOM", "GOI", date(2026, 12, 15)), ("GOI", "DEL", date(2026, 12, 18))]


def table():
    return {("DEL", "BOM"): [option(5000), option(4000)], ("BOM", "GOI"): [option(3000, frm="BOM", to="GOI")], ("GOI", "DEL"): [option(7000, frm="GOI", to="DEL")]}


def test_multi_city_is_separate_one_way_searches_and_says_so():
    fake = Fake(table())
    r = search_multi_city(fake, legs3())
    assert [c[:2] for c in fake.calls] == [("DEL", "BOM"), ("BOM", "GOI"), ("GOI", "DEL")]
    assert r.method == "separate_one_way_searches" and "not a multi-city fare" in r.note
    assert r.total_cheapest_inr == 4000 + 3000 + 7000 and r.incomplete_legs == ()
    assert [len(l.options) for l in r.legs] == [2, 1, 1]


def test_multi_city_with_a_failing_or_priceless_leg_has_no_total_and_names_the_leg():
    r = search_multi_city(Fake(table(), fail={("BOM", "GOI")}), legs3())
    assert r.total_cheapest_inr is None and r.incomplete_legs == (1,)
    assert "no service for BOM-GOI" in r.legs[1].error
    t = table()
    t[("BOM", "GOI")] = [FlightOption(price_inr=None, duration_min=60, stops=0, segments=(seg(frm="BOM", to="GOI"),), source="tool:fli")]
    r = search_multi_city(Fake(t), legs3())
    assert r.total_cheapest_inr is None and r.incomplete_legs == (1,)


@pytest.mark.parametrize("legs, msg", [
    ([("DEL", "BOM", date(2026, 12, 12))], "at least 2 legs"),
    ([("DEL", "BOM", date(2026, 12, 12))] * 7, "at most 6 legs"),
    ([("DEL", "BOM", date(2026, 12, 15)), ("BOM", "GOI", date(2026, 12, 12))], "not before"),
    ([("DEL", "DEL", date(2026, 12, 12)), ("DEL", "BOM", date(2026, 12, 13))], "same airport"),
])
def test_multi_city_validates_legs_before_any_search(legs, msg):
    fake = Fake(table())
    with pytest.raises(ValueError, match=msg):
        search_multi_city(fake, legs)
    assert fake.calls == []


# ------------------------------------------------------------ fli adapter

fli = pytest.importorskip("fli")


def _fli_result(price=4321.0, currency="INR", airline="IndiGo", url=None):
    from fli.models import Airline, Airport, FlightLeg, FlightResult

    leg = FlightLeg(
        airline=Airline._6E, flight_number="6107", departure_airport=Airport.DEL, arrival_airport=Airport.BOM,
        departure_datetime=datetime(2026, 12, 12, 9, 30), arrival_datetime=datetime(2026, 12, 12, 12, 5), duration=155,
    )
    return FlightResult(legs=[leg], price=price, currency=currency, duration=155, stops=0)


class FakeSearch:
    def __init__(self, results=None, exc=None):
        self.results, self.exc, self.kwargs = results, exc, None

    def build_flight_booking_url(self, flight, **kw):
        return "https://www.google.com/travel/flights/booking?tfs=abc"

    def search(self, filters, top_n=5, currency=None, language=None, country=None):
        self.kwargs = dict(currency=currency, filters=filters)
        if self.exc:
            raise self.exc
        return self.results


def adapter(search):
    return FliProvider(search_factory=lambda: search, now=lambda: datetime(2026, 10, 30, 9))


def test_adapter_maps_results_requests_inr_and_rounds_to_whole_rupees():
    s = FakeSearch([_fli_result(4321.4)])
    out = adapter(s).search_one_way("DEL", "BOM", date(2026, 12, 12))
    assert s.kwargs["currency"] == "INR"
    assert out.source == "tool:fli" and out.options[0].price_inr == 4321 and out.options[0].segments[0].from_code == "DEL"
    assert out.options[0].segments[0].flight_number == "6107"
    assert out.options[0].booking_url == "https://www.google.com/travel/flights/booking?tfs=abc"  # a link to Google's page; nothing is booked


def test_adapter_refuses_a_price_in_another_currency_instead_of_mislabelling_it():
    out = adapter(FakeSearch([_fli_result(71.0, currency="USD")])).search_one_way("DEL", "BOM", date(2026, 12, 12))
    assert out.options[0].price_inr is None  # never shown as rupees
    assert any("USD" in w for w in out.warnings)


def test_adapter_no_results_is_an_empty_search_not_an_error():
    assert adapter(FakeSearch(None)).search_one_way("DEL", "BOM", date(2026, 12, 12)).options == ()


def test_adapter_maps_failures_to_travel_error_and_never_leaks_internals():
    from fli.search.exceptions import SearchRejectedError, SearchTimeoutError

    for exc in (SearchTimeoutError("timed out"), SearchRejectedError("blocked"), RuntimeError("boom")):
        with pytest.raises(TravelError):
            adapter(FakeSearch(exc=exc)).search_one_way("DEL", "BOM", date(2026, 12, 12))


def test_adapter_validates_input_before_calling_out():
    s = FakeSearch([_fli_result()])
    for args in (("del", "BOM"), ("DEL", "B0M"), ("DEL", "DEL"), ("QQQ", "BOM"), ("DEL; rm -rf /", "BOM")):
        with pytest.raises(ValueError):
            adapter(s).search_one_way(*args, date(2026, 12, 12))
    assert s.kwargs is None
    with pytest.raises(ValueError):
        adapter(s).search_one_way("DEL", "BOM", date(2026, 12, 12), adults=0)
    with pytest.raises(ValueError):
        adapter(s).search_one_way("DEL", "BOM", date(2026, 12, 12), cabin="warp")


def test_missing_fli_fails_closed_with_an_install_hint(monkeypatch):
    import builtins

    real = builtins.__import__

    def deny(name, *a, **k):
        if name == "fli" or name.startswith("fli."):
            raise ImportError("no fli")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    with pytest.raises(TravelError, match="not installed"):
        FliProvider().search_one_way("DEL", "BOM", date(2026, 12, 12))


# ------------------------------------------------------------- service + API


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="travel", flights=Fake(table()))


@pytest.fixture
def client(em):
    return create_app(em, today_fn=lambda: TODAY).test_client()


def post(client, url, body):
    token = client.get("/api/session").get_json()["csrf"]
    return client.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": token, "Content-Type": "application/json"})


def test_api_flights_and_multi_city(client):
    r = post(client, "/api/travel/flights", {"origin": "DEL", "destination": "BOM", "date": "2026-12-12"})
    assert r.status_code == 200 and r.get_json()["options"][0]["price_inr"] == 4000
    r = post(client, "/api/travel/multi-city", {"legs": [{"origin": o, "destination": d, "date": t.isoformat()} for o, d, t in legs3()]})
    body = r.get_json()
    assert r.status_code == 200 and body["total_cheapest_inr"] == 14000 and body["method"] == "separate_one_way_searches"


def test_api_rejects_hostile_and_past_input(client):
    assert post(client, "/api/travel/flights", {"origin": "DEL;", "destination": "BOM", "date": "2026-12-12"}).status_code == 422
    assert post(client, "/api/travel/flights", {"origin": "DEL", "destination": "BOM", "date": "2026-12-12", "x": 1}).status_code == 422
    assert post(client, "/api/travel/flights", {"origin": "DEL", "destination": "BOM", "date": "2020-01-01"}).status_code in (400, 422)
    assert post(client, "/api/travel/multi-city", {"legs": [{"origin": "DEL", "destination": "BOM", "date": "2026-12-12"}]}).status_code == 422


def test_api_provider_failure_is_a_clean_502_without_internals(em):
    em.flights = Fake(fail={("DEL", "BOM")})
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    r = post(c, "/api/travel/flights", {"origin": "DEL", "destination": "BOM", "date": "2026-12-12"})
    assert r.status_code == 502 and "Traceback" not in r.get_data(as_text=True)


def test_api_post_requires_csrf(client):
    r = client.post("/api/travel/flights", data=json.dumps({"origin": "DEL", "destination": "BOM", "date": "2026-12-12"}), headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 403)
