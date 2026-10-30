"""Adversarial regressions found by deliberately breaking each integration (ADR-037). Every case here crashed or misbehaved before its fix."""

import http.client
import http.server
import json
import os
import threading
import time
from datetime import date, timedelta

import pytest

from eventmate.integrations.deals import DealAgent
from eventmate.integrations.discovery import DiscoveryError, OverpassSource
from eventmate.env import load_env
from eventmate.integrations.explore import _parse, _validate
from eventmate.llm import LLMError, OpenAICompatClient, UsageMeter
from eventmate.integrations.places import PlacesError, PlacesSource
from eventmate.schemas import DealQuery, TripRequest, TripStop
from eventmate.service import EventMate
from tests.conftest import TODAY
from tests.test_deals import A, B, PAGES, FakeFetch, FakeSearch
from tests.test_tripsearch import Fake as FlightFake, flat
from eventmate.integrations.tripsearch import search_trip
from eventmate.integrations.weather import OpenWeatherSource, WeatherError
from eventmate.web.app import create_app
from eventmate.integrations.web_access import SafeFetcher, check_url, pinned_get
from eventmate.integrations.websearch import BraveSearch, DuckDuckGoHtml, SearchError

DEEP_ARRAY = b"[" * 300_000 + b"]" * 300_000
DEEP_OBJECT = b'{"a":' * 150_000 + b"1" + b"}" * 150_000
KEY = "SECRETKEY1234567890abcdef"


# ----------------------------------------------- 1. deeply nested JSON from a hostile server


@pytest.mark.parametrize("body", [DEEP_ARRAY, DEEP_OBJECT])
def test_every_json_consumer_survives_absurd_nesting(body):
    t = lambda *a, **k: body
    with pytest.raises(LLMError):
        OpenAICompatClient("n", "https://x.test/v1", "k" * 12, "m", transport=t, meter=UsageMeter(5)).complete("s", "u", 50)
    with pytest.raises(WeatherError):
        OpenWeatherSource("k" * 12, transport=lambda u, tmo: body).forecast("Pune", date(2026, 11, 2))
    with pytest.raises(PlacesError):
        PlacesSource("k" * 30, transport=t).search("venue", "Pune")
    with pytest.raises(DiscoveryError):
        OverpassSource(transport=t).venues("Pune")
    with pytest.raises(SearchError):
        BraveSearch("k" * 12, transport=lambda u, h: body).search("x")


def test_the_model_router_survives_nesting_inside_its_size_limit():
    for text in ('{"action": ' + '{"a":' * 700 + "1" + "}" * 700 + "}", "[" * 3000 + '{"action":"none"}', '{"action":"none","x":' + "[" * 1500 + "]" * 1500 + "}"):
        try:
            _parse(text)
        except ValueError:
            pass  # a clean refusal is the point; RecursionError would fail the test


def test_a_deeply_nested_request_body_is_a_client_error_not_a_crash(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="h")
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    tok = c.get("/api/session").get_json()["csrf"]
    for body in (b"[" * 100_000, b'{"a":' * 50_000):
        r = c.post("/api/deals", data=body, headers={"X-EventMate-CSRF": tok, "Content-Type": "application/json"})
        assert r.status_code in (400, 413, 415, 422), r.status_code


# ------------------------------------------- 2. network exceptions that are not OSError


@pytest.mark.parametrize("exc", [http.client.IncompleteRead(b"x"), http.client.BadStatusLine("garbage"), http.client.RemoteDisconnected("gone"), http.client.LineTooLong("line")])
def test_http_client_errors_are_clean_failures_everywhere(exc):
    def boom(*a, **k):
        raise exc

    with pytest.raises(LLMError):
        OpenAICompatClient("n", "https://x.test/v1", "k" * 12, "m", transport=boom, meter=UsageMeter(5)).complete("s", "u", 50)
    with pytest.raises(WeatherError):
        OpenWeatherSource("k" * 12, transport=boom).forecast("Pune", date(2026, 11, 2))
    with pytest.raises(PlacesError):
        PlacesSource("k" * 30, transport=boom).search("venue", "Pune")
    with pytest.raises(DiscoveryError):
        OverpassSource(transport=boom).venues("Pune")
    with pytest.raises(SearchError):
        BraveSearch("k" * 12, transport=boom).search("x")
    with pytest.raises(SearchError):
        DuckDuckGoHtml(transport=boom).search("x")


# ------------------------------------------------ 3. one bad search result must not sink the run


@pytest.mark.parametrize("bad", ["https://[bad/x", "http://[::1", "https://exa mple.com/x", "https://a.com:99999/x", "https://\u202egood.com/x", "https://x.com/\x00"])
def test_a_hostile_result_url_is_skipped_and_the_good_pages_still_rank(bad):
    pages = {**PAGES, bad: PAGES[A]}
    r = DealAgent(FakeSearch([bad, A, B]), FakeFetch(pages)).run(DealQuery(kind="caterer", city="Pune", guests=100))
    assert r.offers and all(o.url in (A, B) for o in r.offers)


def test_an_oversized_hostname_cannot_reach_the_report_through_the_real_fetcher():
    host = ".".join(["a" * 60] * 4) + ".example.com"  # 250+ characters
    with pytest.raises(Exception) as e:
        check_url(f"https://{host}/p", lambda h: ["93.184.216.34"])
    assert "invalid" in str(e.value) or "malformed" in str(e.value)
    host2 = ".".join(["a" * 40] * 3) + ".example.com"  # 140 chars: legal in DNS, longer than the report's field before the fix
    pages = {f"https://{host2}/robots.txt": (404, {}, b""), f"https://{host2}/p": (200, {"Content-Type": "text/html"}, b"<p>Veg buffet Rs 500 per plate</p>")}

    def net(url, ip, h, t, m):
        s, hd, b = pages.get(url, (404, {}, b""))
        return s, {k.lower(): v for k, v in hd.items()}, b

    r = DealAgent(FakeSearch([f"https://{host2}/p", A]), SafeFetcher(transport=net, resolver=lambda h: ["93.184.216.34"])).run(DealQuery(kind="caterer", city="Pune"))
    assert any(o.low_inr == 500 for o in r.offers) or r.skipped is not None  # reaches a report either way


def test_one_unbuildable_offer_is_dropped_not_fatal():
    pages = {A: "Veg buffet ₹500 per plate. Veg ₹600 per plate."}
    agent = DealAgent(FakeSearch([A]), FakeFetch(pages))
    real = DealQuery(kind="caterer", city="Pune")
    import eventmate.integrations.deals as d

    orig = d.DealOffer
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return orig(**kw)

    d.DealOffer = flaky
    try:
        r = agent.run(real)
    finally:
        d.DealOffer = orig
    assert len(r.offers) == 1 and any("could not be built" in w for w in r.warnings)


# --------------------------------------------------------------- 4. dates that break arithmetic


def test_trip_dates_far_in_the_future_are_refused_cleanly():
    far = TripRequest(origin="DEL", stops=(TripStop(code="BOM", nights=3),), earliest_start=date(9999, 12, 31))
    fake = FlightFake(flat())
    with pytest.raises(ValueError, match="too far"):
        search_trip(fake, far, TODAY)
    assert fake.calls == []
    edge = TripRequest(origin="DEL", stops=(TripStop(code="BOM", nights=3),), earliest_start=TODAY + timedelta(days=366))
    with pytest.raises(ValueError, match="too far"):
        search_trip(fake, edge, TODAY)
    ok = TripRequest(origin="DEL", stops=(TripStop(code="BOM", nights=3),), earliest_start=TODAY + timedelta(days=300))
    assert search_trip(fake, ok, TODAY).packages


def test_far_future_dates_are_client_errors_on_every_route_and_in_the_router(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="h", flights=FlightFake(flat()))
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    tok = c.get("/api/session").get_json()["csrf"]
    post = lambda url, body: c.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": tok, "Content-Type": "application/json"})
    for far in ("9999-12-31", "2099-01-01", str(TODAY + timedelta(days=400))):
        assert post("/api/travel/trip", {"origin": "DEL", "stops": [{"code": "BOM", "nights": 3}], "earliest_start": far}).status_code in (400, 422)
        assert post("/api/travel/flights", {"origin": "DEL", "destination": "BOM", "date": far}).status_code in (400, 422)
        assert post("/api/travel/multi-city", {"legs": [{"origin": "DEL", "destination": "BOM", "date": far}, {"origin": "BOM", "destination": "GOI", "date": far}]}).status_code in (400, 422)
    with pytest.raises(ValueError):
        _validate({"action": "flights", "origin": "DEL", "destination": "BOM", "date": "9999-12-31"}, TODAY)


# ------------------------------------------------------------------- 5. the .env file


@pytest.mark.parametrize("raw", ["\ufeffOPENWEATHER_API_KEY=abc12345\n", "OPENWEATHER_API_KEY=abc12345\r\n", "\ufeff# note\r\nOPENWEATHER_API_KEY=abc12345\r\n"])
def test_env_files_from_windows_editors_load(tmp_path, raw):
    p = tmp_path / ".env"
    p.write_bytes(raw.encode("utf-8"))
    os.chmod(p, 0o600)
    env = {}
    assert load_env(p, environ=env).loaded == ("OPENWEATHER_API_KEY",) and env["OPENWEATHER_API_KEY"] == "abc12345"


# ----------------------------------------------------------- 6. slow-drip and resource limits


def test_a_server_that_dribbles_bytes_cannot_hold_a_fetch_open_indefinitely():
    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.15)
            except OSError:
                pass

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            pinned_get(f"http://example.test:{srv.server_port}/", "127.0.0.1", {"User-Agent": "t"}, 5, 10_000, total_s=1.0)
    finally:
        srv.shutdown()
    assert time.monotonic() - t0 < 4


def test_expensive_routes_refuse_a_pile_up_instead_of_exhausting_the_server(tmp_path):
    gate, started = threading.Event(), threading.Semaphore(0)

    class Slow(FlightFake):
        def search_one_way(self, *a, **k):
            started.release()
            gate.wait(10)
            return super().search_one_way(*a, **k)

    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="h", flights=Slow(flat()))
    app = create_app(em, today_fn=lambda: TODAY)
    body = {"origin": "DEL", "destination": "BOM", "date": str(TODAY + timedelta(days=30))}
    codes = []

    def call():
        c = app.test_client()
        tok = c.get("/api/session").get_json()["csrf"]
        codes.append(c.post("/api/travel/flights", data=json.dumps(body), headers={"X-EventMate-CSRF": tok, "Content-Type": "application/json"}).status_code)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(4):
        assert started.acquire(timeout=10)
    c = app.test_client()
    tok = c.get("/api/session").get_json()["csrf"]
    r = c.post("/api/travel/flights", data=json.dumps(body), headers={"X-EventMate-CSRF": tok, "Content-Type": "application/json"})
    assert r.status_code == 429 and "busy" in r.get_json()["error"]
    assert c.get("/api/session").status_code == 200  # cheap routes stay available
    gate.set()
    for t in threads:
        t.join(10)
    assert codes == [200, 200, 200, 200]


# ------------------------------------------- 7. drive-by requests from another website must not spend your API quota


def test_quota_spending_gets_need_the_csrf_header_so_a_web_page_cannot_drain_your_keys(tmp_path):
    from tests.test_places import src as places_src
    from tests.test_weather import src as wx_src
    from tests.test_discovery import src as osm_src

    net_calls = []

    class Counting(FlightFake):
        def search_one_way(self, *a, **k):
            net_calls.append(a)
            return super().search_one_way(*a, **k)

    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="h", flights=Counting(flat()), weather=wx_src(), places=places_src(), discovery=osm_src())
    pid = em.plan("Plan a 120-person engagement party in Pune on December 12 under INR 200,000. Vegetarian catering, indoor venue, photographer.", today=TODAY).plan.plan_id
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    tok = c.get("/api/session").get_json()["csrf"]
    for url in ("/api/weather?city=Pune&date=2026-11-02", "/api/places?kind=venue&city=Pune", "/api/discover/venues?city=Pune", f"/api/plans/{pid}/weather"):
        assert c.get(url).status_code == 403, url  # what an <img> or a no-cors fetch from another site would send
        assert c.get(url, headers={"X-EventMate-CSRF": "wrong"}).status_code == 403, url
        assert c.get(url, headers={"X-EventMate-CSRF": tok}).status_code == 200, url
    for url in ("/api/weather/status", "/api/places/status", "/api/session", "/api/explore/status"):
        assert c.get(url).status_code == 200, url  # cheap and free: no header needed


# ------------------------------------------------- 8. safety checks must not depend on assert


def test_policy_validation_still_rejects_a_bad_policy_when_python_runs_with_O(tmp_path):
    import subprocess
    import sys

    good = json.loads((__import__("pathlib").Path(__file__).resolve().parents[1] / "eventmate" / "data" / "assistant_policy.json").read_text())
    bad = dict(good, preset_reply="<script>alert(1)</script>", retries=99)
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(bad))
    code = "import sys\nfrom eventmate.assistant.guard import load_policy, PolicyError\ntry:\n    load_policy(sys.argv[1])\nexcept PolicyError:\n    print('refused')\nelse:\n    print('LOADED')"
    for flags in ([], ["-O"], ["-OO"]):
        out = subprocess.run([sys.executable, *flags, "-c", code, str(p)], capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]), env={"PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1])}).stdout.strip()
        assert out == "refused", (flags, out)


def test_no_safety_check_in_the_package_relies_on_assert():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "eventmate"
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assert):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], "asserts vanish under python -O: " + ", ".join(offenders)


def test_modules_that_read_the_open_web_never_import_a_model_or_a_shell():
    """Architecture invariant (ADR-037): web text can never reach a model, and nothing here can run a command."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "eventmate"
    web_facing = [f"integrations/{n}.py" for n in ("web_access", "websearch", "deals", "places", "discovery", "weather", "travel", "tripsearch")] + ["netutil.py"]
    banned_imports = {"eventmate.llm", "eventmate.assistant", "eventmate.planning.intake", "eventmate.integrations.explore", "subprocess", "pickle", "marshal", "shelve"}
    banned_names = {"eval", "exec", "compile", "__import__"}  # bare calls; re.compile is an attribute and is fine
    banned_attrs = {"system", "popen"}
    problems = []
    for name in web_facing:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            problems += [f"{name} imports {m}" for m in mods if any(m == b or m.startswith(b + ".") for b in banned_imports)]
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in banned_names:
                    problems.append(f"{name} calls {node.func.id}()")
                if isinstance(node.func, ast.Attribute) and node.func.attr in banned_attrs:
                    problems.append(f"{name} calls .{node.func.attr}()")
    assert problems == []
