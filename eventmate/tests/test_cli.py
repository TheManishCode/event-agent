import json

import pytest

from eventmate.__main__ import main
from eventmate.tests.conftest import EXAMPLE


@pytest.fixture
def run(tmp_path, capsys):
    base = ["--today", "2026-10-30", "--log-dir", str(tmp_path / "logs"), "--plan-dir", str(tmp_path / "plans")]

    def go(*args, today=None):
        pre = list(base)
        if today:
            pre[1] = today
        code = main(pre + list(args))
        cap = capsys.readouterr()
        return code, cap.out, cap.err

    return go


def plan_id(err):
    return err.split("plan id: ")[1].split(" ")[0]


def test_full_lifecycle_through_the_cli(run):
    code, out, err = run("plan", EXAMPLE)
    assert code == 0 and "Why this plan is valid" in out and "Alternatives considered" in out
    pid = plan_id(err)

    assert run("confirm", pid, "venue")[0] == 0
    code, out, err = run("replan", pid, "--budget", "190000", today="2026-11-02")
    assert code == 0 and "Changes from version 2 to 3" in out and "confirmed booking kept" in out

    code, out, _ = run("diff", pid, "1", "3")
    assert code == 0 and json.loads(out)["new_version"] == 3
    code, out, _ = run("validate", pid)
    assert code == 0 and "PASSED" in out
    code, out, _ = run("list")
    assert pid in out and "v3" in out
    code, out, _ = run("show", pid, "--version", "1", "--json")
    assert json.loads(out)["version"] == 1


def test_conflict_exits_3_and_explains_the_override(run):
    _, _, err = run("plan", EXAMPLE)
    pid = plan_id(err)
    run("confirm", pid, "venue")
    code, out, err = run("replan", pid, "--date", "2026-12-19", today="2026-11-02")
    assert code == 3 and "needs decision" in err and "override_confirmed" in err and out == ""
    code, out, _ = run("list")
    assert "v2" in out  # nothing new was saved


def test_unplannable_request_exits_3_with_repair(run):
    code, out, err = run("plan", EXAMPLE.replace("INR 200,000", "INR 60,000"))
    assert code == 3 and "repair:" in err and out == ""


def test_bad_input_exits_2_without_a_traceback(run):
    for args in (("plan", "Plan a party."), ("show", "nope"), ("confirm", "nope", "venue"), ("show", "../x")):
        code, out, err = run(*args)
        assert code == 2 and "error" in err and "Traceback" not in err


def test_legacy_form_still_prints_the_intake_json(run):
    code, out, _ = run(EXAMPLE)
    assert code == 0 and json.loads(out)["final_plan"] is None


def test_guest_list_flag(run):
    code, out, err = run("plan", EXAMPLE, "--guest-list", "demo-engagement-120")
    assert code == 0 and "jain" in out.lower()


def test_filesystem_errors_are_reported_not_tracebacks(run, monkeypatch):
    import eventmate.plan_store as ps

    def boom(self, plan):
        raise PermissionError("disk is read-only")

    monkeypatch.setattr(ps.PlanStore, "save", boom)
    code, out, err = run("plan", EXAMPLE)
    assert code == 2 and "disk is read-only" in err and "Traceback" not in err


def test_tradeoffs_command_lists_options_and_exits_zero(run):
    code, out, _ = run("tradeoffs", EXAMPLE.replace("200,000", "150,000"))
    assert code == 0 and "Raise the budget" in out and "preference impact" in out


def test_tradeoffs_command_on_a_feasible_request(run):
    code, out, _ = run("tradeoffs", EXAMPLE)
    assert code == 0 and "No trade-off needed" in out


def test_tradeoffs_command_apply_creates_a_plan(run):
    code, out, err = run("tradeoffs", EXAMPLE.replace("200,000", "150,000"), "--apply", "1")
    assert code == 0 and "plan id:" in err
    assert run("tradeoffs", EXAMPLE, "--apply", "1")[0] == 2  # nothing to apply: bad input


def test_plan_prints_the_labelled_budget_reality_check_on_stderr(run):
    code, out, err = run("plan", EXAMPLE)
    assert code == 0 and "Budget reality check: Realistic" in err and "Estimate, not a quote" in err
    assert "Budget reality check" not in out  # stdout stays the plan itself (pipeable)


def test_failed_plan_still_prints_the_reality_check_with_a_minimum_budget(run):
    code, out, err = run("plan", EXAMPLE.replace("200,000", "90,000"))
    assert code == 3 and "Budget reality check: Not realistic" in err and "A budget of INR" in err


def test_ledger_command_records_user_reported_money_and_prints_the_table(run):
    _, _, err = run("plan", EXAMPLE)
    pid = plan_id(err)
    code, out, err = run("ledger", pid, "--payment", "venue", "10000", "--note", "advance")
    assert code == 0 and "recorded payment of INR 10,000" in out
    code, out, _ = run("ledger", pid)
    assert code == 0 and "venue" in out and "paid" in out.lower() and "10,000" in out
    code, out, err = run("ledger", pid, "--refund", "venue", "99999")
    assert code == 2 and "exceeds" in err
    code, out, _ = run("ledger", pid, "--json")
    assert json.loads(out)["summary"]["paid_total"] == 10_000


def test_quotes_command_reads_recorded_quotes_and_fails_for_unknown(run, tmp_path):
    from datetime import date, datetime, timezone

    from eventmate.service import EventMate
    from eventmate.schemas import VendorCategory as C

    em = EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="q")
    em.observe_quote(
        "cat-pune-annapurna", C.CATERER, source_type="user_input", entered_by="asha", observed_at=datetime.now(timezone.utc),
        expires_at=date(2026, 12, 1), confidence=0.7, amount_inr=600, basis="per_plate", guest_count=100,
    )
    code, out, _ = run("quotes", "show", "cat-pune-annapurna")
    assert code == 0 and "INR 60,000" in out and "valid" in out
    code, out, _ = run("quotes", "compare", "caterer", "--json")
    assert code == 0 and json.loads(out)[0]["vendor_id"] == "cat-pune-annapurna"
    code, out, err = run("quotes", "show", "cat-pune-rasoi")
    assert code == 2 and "no quote recorded" in err
    code, out, _ = run("quotes", "compare", "venue")
    assert code == 0 and "no quotes" in out


def _fake_flights(monkeypatch, fail=()):
    from eventmate.tests.test_travel import Fake, table
    from eventmate.travel import FliProvider

    fake = Fake(table(), fail=fail)
    monkeypatch.setattr(FliProvider, "search_one_way", lambda self, *a, **k: fake.search_one_way(*a, **k))
    return fake


def test_flights_command_prints_fares_and_the_no_booking_notice(run, monkeypatch):
    _fake_flights(monkeypatch)
    code, out, _ = run("flights", "DEL", "BOM", "2026-12-12")
    assert code == 0 and "INR 4,000" in out and "IndiGo 6107" in out and "does not book" in out
    code, out, _ = run("flights", "DEL", "BOM", "2026-12-12", "--json")
    assert code == 0 and json.loads(out)["options"][0]["price_inr"] == 4000


def test_multicity_command_totals_legs_and_labels_the_method(run, monkeypatch):
    _fake_flights(monkeypatch)
    code, out, _ = run("multicity", "DEL,BOM,2026-12-12", "BOM,GOI,2026-12-15", "GOI,DEL,2026-12-18")
    assert code == 0 and "INR 14,000" in out and "not a multi-city fare" in out
    _fake_flights(monkeypatch, fail={("BOM", "GOI")})
    code, out, _ = run("multicity", "DEL,BOM,2026-12-12", "BOM,GOI,2026-12-15")
    assert code == 0 and "unavailable" in out and "failed:" in out


def test_travel_commands_reject_bad_input_with_exit_2(run, monkeypatch):
    _fake_flights(monkeypatch)
    for args in (("flights", "del", "BOM", "2026-12-12"), ("flights", "DEL", "DEL", "2026-12-12"), ("multicity", "DEL,BOM"), ("multicity", "DEL,BOM,2026-12-12")):
        code, _, err = run(*args)
        assert code == 2 and "error" in err or "ORIGIN" in err, args


def test_discover_command_lists_candidates_with_attribution_and_rejects_hostile_cities(run, monkeypatch):
    from eventmate.discovery import OverpassSource
    from eventmate.tests.test_discovery import Transport

    real = OverpassSource.__init__
    monkeypatch.setattr(OverpassSource, "__init__", lambda self, *a, **k: real(self, transport=Transport()))
    code, out, _ = run("discover", "Pune")
    assert code == 0 and "Orchid Hall [node/11]" in out and "OpenStreetMap contributors" in out and "capacity 300 (tagged, unverified)" in out
    code, _, err = run("discover", 'Pune"];out;')
    assert code == 2 and "error" in err


def test_weather_command_prints_the_forecast_or_a_clear_not_configured_error(run, monkeypatch):
    from eventmate.tests.test_weather import Transport
    from eventmate.weather import OpenWeatherSource

    monkeypatch.delenv("OPENWEATHER_API_KEY", raising=False)
    code, _, err = run("weather", "Pune", "2026-11-02")
    assert code == 2 and "OPENWEATHER_API_KEY" in err
    real = OpenWeatherSource.__init__
    monkeypatch.setattr(OpenWeatherSource, "__init__", lambda self, *a, **k: real(self, api_key="K" * 12, transport=Transport()))
    code, out, _ = run("weather", "Pune", "2026-11-02")
    assert code == 0 and "light rain" in out and "OpenWeather" in out and "K" * 12 not in out
    code, out, _ = run("weather", "Pune", "2026-12-12")
    assert code == 0 and "beyond the forecast horizon" in out
    code, _, err = run("weather", 'Pune"', "2026-11-02")
    assert code == 2


def test_deals_command_lists_ranked_offers_with_flags_and_the_not_a_quote_notice(run, monkeypatch):
    from eventmate.deals import DealAgent
    from eventmate.tests.test_deals import PAGES, A, B, C, D, E, FakeFetch, FakeSearch
    from eventmate.service import EventMate

    monkeypatch.setattr(EventMate, "__init__", (lambda orig: lambda self, *a, **k: (orig(self, *a, **k), setattr(self, "deals_factory", lambda on_call: DealAgent(FakeSearch([A, B, C, D, E]), FakeFetch(PAGES), on_call=on_call)))[0])(EventMate.__init__))
    code, out, err = run("deals", "caterer", "Pune", "--guests", "100", "--budget", "50000")
    assert code == 0 and "#1" in out and "[fits budget]" in out and "unusually low" in out and "not a quote" in out
    assert "blocked by robots.txt" in err
    code, _, err = run("deals", "caterer", 'Pune"; x')
    assert code == 2 and "error" in err
