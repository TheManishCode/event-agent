"""The all-at-once smoke run (ADR-035). Here it runs against fakes, so the suite itself stays free and offline."""

import json
from datetime import date

import pytest

from eventmate.deals import DealAgent
from eventmate.service import EventMate
from eventmate.smoke import main, run_smoke
from eventmate.tests.test_deals import A, B, C, D, E, PAGES, FakeFetch, FakeSearch
from eventmate.tests.test_discovery import src as osm_src
from eventmate.tests.test_places import src as places_src
from eventmate.tests.test_tripsearch import Fake as FlightFake, flat
from eventmate.tests.test_weather import src as wx_src

TODAY = date(2026, 10, 30)


class Router:
    name, model = "scripted", "m"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens):
        self.calls.append(max_tokens)
        return json.dumps({"action": "weather", "city": "Pune", "date": "2026-11-02"}) if "Request:" in user else "OK"


@pytest.fixture
def em(tmp_path):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="s", flights=FlightFake(flat()), weather=wx_src(), discovery=osm_src(), places=places_src())
    e.deals_factory = lambda on_call: DealAgent(FakeSearch([A, B, C, D, E]), FakeFetch(PAGES), on_call=on_call)
    return e


def by(rows):
    return {r.name: r for r in rows}


def test_every_free_integration_is_exercised_and_the_model_is_not_touched_without_the_flag(em):
    router = Router()
    em.router_clients = lambda: [router]
    rows = by(run_smoke(em, TODAY, llm=False))
    assert {"flights", "trip", "deals", "discover", "weather", "places"} <= set(rows) and all(r.status == "PASS" for r in rows.values() if r.name != "llm")
    assert rows["llm"].status == "SKIP" and "--llm" in rows["llm"].detail and router.calls == []


def test_with_the_flag_the_model_gets_tiny_calls_and_the_run_reports_how_many(em):
    router = Router()
    em.router_clients = lambda: [router]
    rows = by(run_smoke(em, TODAY, llm=True))
    assert rows["llm"].status == "PASS" and rows["explore"].status == "PASS" and 1 <= len(router.calls) <= 3 and max(router.calls) <= 400
    assert "calls" in rows["llm"].detail


def test_unconfigured_services_are_skipped_not_failed_and_failures_are_named(em):
    from eventmate.places import PlacesSource
    from eventmate.weather import OpenWeatherSource

    em.weather, em.places = OpenWeatherSource(api_key=""), PlacesSource(api_key="")
    em.router_clients = lambda: []
    rows = by(run_smoke(em, TODAY, llm=True))
    assert rows["weather"].status == "SKIP" and rows["places"].status == "SKIP" and rows["llm"].status == "SKIP"
    em.flights = FlightFake(lambda o, d, day: (_ for _ in ()).throw(__import__("eventmate.travel", fromlist=["TravelError"]).TravelError("down")))
    rows = by(run_smoke(em, TODAY, llm=False))
    assert rows["flights"].status == "FAIL" and "down" in rows["flights"].detail


def test_a_busy_public_server_is_a_warning_not_a_failure(em):
    import urllib.error

    from eventmate.tests.test_discovery import Transport

    em.discovery = osm_src(Transport(exc=urllib.error.HTTPError("u", 502, "busy", {}, None)))
    assert by(run_smoke(em, TODAY, llm=False))["discover"].status == "WARN"


def test_no_secret_value_can_appear_in_the_report(em):
    rows = run_smoke(em, TODAY, llm=False)
    blob = json.dumps([r.__dict__ for r in rows])
    assert "SECRETKEY" not in blob and "AIza" not in blob


def test_main_exits_nonzero_only_on_failure(em, monkeypatch, capsys):
    monkeypatch.setattr("eventmate.smoke.build_em", lambda: em)
    assert main(["--today", "2026-10-30"]) == 0
    assert "PASS" in capsys.readouterr().out
    em.flights = FlightFake(lambda o, d, day: (_ for _ in ()).throw(__import__("eventmate.travel", fromlist=["TravelError"]).TravelError("down")))
    assert main(["--today", "2026-10-30"]) == 1
