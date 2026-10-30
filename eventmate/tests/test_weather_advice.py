"""Weather advice (ADR-039): code compares the forecast with the chosen venue and suggests a backup. Advice never changes a plan."""

from datetime import date

import pytest

from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY, csrf_get
from eventmate.tests.test_weather import Transport, slot, ts, TZ, src as wx_src
from eventmate.weather_advice import advise
from eventmate.web.app import create_app


def day(rain=0.0, tmax=28.0, tmin=20.0, slots=True):
    from eventmate.weather import OpenWeatherSource

    fc = {"city": {"timezone": TZ}, "list": [slot(ts(2026, 11, 2, 3), tmin, rain), slot(ts(2026, 11, 2, 9), tmax, rain, "light rain" if rain else "clear sky")] if slots else []}
    return OpenWeatherSource._day("Pune", date(2026, 11, 2), "Pune, Maharashtra, IN", fc)


def titles(items):
    return [i.title for i in items]


def test_outdoor_with_rain_possible_asks_for_a_covered_backup_and_says_how_likely():
    items = advise(day(rain=0.5), "outdoor")
    assert items[0].level == "warn" and "covered" in items[0].detail.lower() and "50%" in items[0].detail


def test_outdoor_dry_and_mild_needs_nothing_but_says_so():
    items = advise(day(rain=0.0), "outdoor")
    assert [i.level for i in items] == ["ok"]


def test_indoor_rain_is_only_an_arrival_note_and_only_when_likely():
    assert [i.level for i in advise(day(rain=0.5), "indoor")] == ["ok"]
    assert advise(day(rain=0.8), "indoor")[0].level == "info" and "arrival" in advise(day(rain=0.8), "indoor")[0].detail.lower()


def test_heat_outdoors_is_flagged_and_cold_is_not_invented():
    assert any("heat" in t.lower() for t in titles(advise(day(tmax=37), "outdoor")))
    assert not any("heat" in t.lower() for t in titles(advise(day(tmax=37), "indoor")))
    assert not any("cold" in t.lower() for t in titles(advise(day(tmax=15, tmin=8), "outdoor")))


def test_no_forecast_means_no_advice_claims():
    items = advise(day(slots=False), "outdoor")
    assert [i.level for i in items] == ["info"] and "forecast" in items[0].detail.lower()


def test_unknown_venue_setting_is_treated_cautiously():
    assert advise(day(rain=0.5), None)[0].level == "warn"


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="wa", weather=wx_src(Transport(forecast={"city": {"timezone": TZ}, "list": [slot(ts(2026, 12, 12, 3), 20, 0.6, "rain")]})))


def test_service_uses_the_selected_venue_setting_and_never_changes_the_plan(em):
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    before = em.store.latest(pid).model_dump_json()
    res = em.weather_advice(pid)
    setting = em.repo.get(__import__("eventmate.schemas", fromlist=["VendorCategory"]).VendorCategory.VENUE, em.store.latest(pid).selection_for(__import__("eventmate.schemas", fromlist=["VendorCategory"]).VendorCategory.VENUE).vendor_id).setting
    assert res.venue_setting == setting and res.items and em.store.latest(pid).model_dump_json() == before and em.store.latest(pid).version == 1


def test_api_needs_csrf_and_rejects_bad_ids(em):
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    assert c.get(f"/api/plans/{pid}/weather-advice").status_code == 403
    r = csrf_get(c, f"/api/plans/{pid}/weather-advice")
    assert r.status_code == 200 and r.get_json()["items"] and r.get_json()["forecast"]["source"] == "tool:openweathermap"
    assert csrf_get(c, "/api/plans/..%2Fx/weather-advice").status_code in (400, 404)
