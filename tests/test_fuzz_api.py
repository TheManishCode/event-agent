"""Fuzz every route of the web API (ADR-037): no input may cause a 5xx other than the deliberate 502/503, or leak a traceback or a secret."""

import json
import random
import string

import pytest

from eventmate.integrations.deals import DealAgent
from eventmate.service import EventMate
from tests.conftest import EXAMPLE, TODAY
from tests.test_deals import A, B, C, D, E, PAGES, FakeFetch, FakeSearch
from tests.test_discovery import src as osm_src
from tests.test_places import src as places_src
from tests.test_tripsearch import Fake as FlightFake, flat
from tests.test_weather import KEY as WX_KEY, src as wx_src
from eventmate.web.app import create_app

ALLOWED_5XX = {502, 503}
STRINGS = [
    "", " ", "a" * 5000, "../../etc/passwd", "..%2f..%2f", "'; DROP TABLE x;--", "<script>alert(1)</script>", "\x00", "\u202e", "${jndi:ldap://x}", "{{7*7}}", "%s%s%s", "💥" * 50,
    "Pune", "DEL", "BOM", "2026-12-12", "9999-12-31", "0001-01-01", "caterer", "venue", "hotel", "-1", "1e999", "NaN", "null", "true", "[]", "{}", "https://169.254.169.254/",
    "A" * 64, "x" * 65, "ledger", "../plans",
]
NUMBERS = [0, -1, 1, 2, 3, 31, 2**31, 2**63, 10**30, -(10**30), 0.5, 1e308, -0.0, 1.5e-320]
KEYS = ["request", "text", "city", "kind", "category", "date", "origin", "destination", "stops", "legs", "guests", "budget_inr", "nights", "amount", "note", "question", "plan_id",
        "earliest_start", "flex_days", "adults", "cabin", "max_stops", "limit", "draft", "messages", "csrf", "vendor_id", "new_budget", "new_guest_count", "new_date", "override_confirmed"]


def rnd(r, depth=0):
    c = r.random()
    if depth > 3 or c < 0.25:
        return r.choice(STRINGS)
    if c < 0.45:
        return r.choice(NUMBERS)
    if c < 0.55:
        return r.choice([None, True, False])
    if c < 0.75:
        return [rnd(r, depth + 1) for _ in range(r.randint(0, 5))]
    return {r.choice(KEYS + STRINGS[:6]): rnd(r, depth + 1) for _ in range(r.randint(0, 5))}


VALID = {
    "/api/deals": {"kind": "caterer", "city": "Pune", "guests": 100, "budget_inr": 60000},
    "/api/travel/trip": {"origin": "DEL", "stops": [{"code": "BOM", "nights": 3}], "earliest_start": "2026-12-12"},
    "/api/travel/flights": {"origin": "DEL", "destination": "BOM", "date": "2026-12-12"},
    "/api/travel/multi-city": {"legs": [{"origin": "DEL", "destination": "BOM", "date": "2026-12-12"}, {"origin": "BOM", "destination": "GOI", "date": "2026-12-15"}]},
    "/api/explore": {"text": "weather in Pune"},
    "/api/plans": {"request": EXAMPLE},
    "/api/tradeoffs": {"request": EXAMPLE},
    "/api/intake": {"messages": ["plan a wedding in Pune for 100 people"]},
    "/api/assistant/ask": {"plan_id": "PLAN", "question": "how much is left?"},
    "/api/plans/<plan_id>/ledger": {"kind": "quote", "category": "venue", "amount": 1000, "note": "x"},
    "/api/plans/<plan_id>/replan": {"kind": "budget_change", "new_budget": 210000},
    "/api/plans/<plan_id>/confirm": {"category": "caterer"},
    "/api/observations/<vendor_id>/quotes": {"category": "caterer", "source_type": "user_input", "entered_by": "a", "expires_at": "2026-12-01", "confidence": 0.5, "amount_inr": 500, "basis": "per_plate", "guest_count": 100},
}


def mutate(r, doc, depth=0):
    if isinstance(doc, dict) and doc:
        out = dict(doc)
        k = r.choice(list(out))
        out[k] = r.choice([rnd(r), mutate(r, out[k], depth + 1)]) if r.random() < 0.8 else out.pop(k)
        if r.random() < 0.2:
            out[r.choice(KEYS)] = rnd(r)
        return out
    if isinstance(doc, list) and doc:
        out = list(doc)
        out[r.randrange(len(out))] = r.choice([rnd(r), mutate(r, out[0], depth + 1)])
        return out
    return rnd(r)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fuzz")
    em = EventMate(log_dir=tmp / "l", plan_dir=tmp / "p", run_id="fz", flights=FlightFake(flat()), weather=wx_src(), places=places_src(), discovery=osm_src())
    em.deals_factory = lambda on_call: DealAgent(FakeSearch([A, B, C, D, E]), FakeFetch(PAGES), on_call=on_call)
    em.router_clients = lambda: []
    app = create_app(em, today_fn=lambda: TODAY, rate_limit_per_minute=10**9)  # the limiter has its own test; here every request must reach its handler
    c = app.test_client()
    tok = c.get("/api/session").get_json()["csrf"]
    pid = c.post("/api/plans", data=json.dumps({"request": EXAMPLE}), headers={"X-EventMate-CSRF": tok, "Content-Type": "application/json"}).get_json()["plan"]["plan_id"]
    return app, tok, pid


def routes(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for m in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            yield rule.rule, m


def fill(rule, pid, r):
    out = rule
    for part in [p for p in rule.split("/") if p.startswith("<")]:
        name = part.strip("<>").split(":")[-1]
        pick = pid if r.random() < 0.5 and "plan" in name else r.choice(["x", "..", "A" * 80, "%2e%2e", "💥", "null", "9", pid, "-", "a b", "..%2fetc", "\u202e", "a" * 64])
        out = out.replace(part, pick.replace("/", "_"))
    return out


def check(resp, label):
    text = resp.get_data(as_text=True)
    assert resp.status_code < 500 or resp.status_code in ALLOWED_5XX, f"{label} -> {resp.status_code}: {text[:200]}"
    assert "Traceback" not in text and WX_KEY not in text, f"{label} leaked internals: {text[:200]}"


def test_every_route_survives_random_and_mutated_input(world):
    app, tok, pid = world
    c = app.test_client()
    r = random.Random(20261030)
    hdr = {"X-EventMate-CSRF": tok, "Content-Type": "application/json"}
    for rule, method in routes(app):
        n = 40 if rule in VALID and rule.startswith("/api/plans/<") else 6 if rule in ("/api/plans", "/api/tradeoffs", "/api/intake", "/api/assistant/ask") else 25
        for i in range(n):
            url = fill(rule, pid, r)
            if method == "GET":
                q = "&".join(f"{r.choice(KEYS)}={r.choice(STRINGS + [str(x) for x in NUMBERS]).replace('&', '%26').replace('#', '%23')}" for _ in range(r.randint(0, 4)))
                resp = c.get(url + ("?" + q if q else ""), headers={"X-EventMate-CSRF": tok})
            else:
                base = VALID.get(rule, {})
                doc = json.loads(json.dumps(base).replace('"PLAN"', json.dumps(pid))) if base else {}
                body = mutate(r, doc) if (base and r.random() < 0.8) else rnd(r)
                raw = json.dumps(body, allow_nan=True) if r.random() < 0.9 else r.choice(["", "{", "[1,", "null", "\x00", "{" * 200, "nan"])
                resp = c.open(url, method=method, data=raw.encode("utf-8", "surrogatepass") if isinstance(raw, str) else raw, headers=hdr)
            check(resp, f"{method} {url}")


def test_requests_without_the_csrf_token_or_with_a_wrong_content_type_never_reach_a_handler(world):
    app, tok, pid = world
    c = app.test_client()
    for rule, method in routes(app):
        if method == "GET":
            continue
        url = fill(rule, pid, random.Random(1))
        for headers in ({}, {"X-EventMate-CSRF": "wrong"}, {"X-EventMate-CSRF": tok, "Content-Type": "text/plain"}, {"X-EventMate-CSRF": tok, "Content-Type": "application/json", "Origin": "https://evil.example"}):
            resp = c.open(url, method=method, data=json.dumps(VALID.get(rule, {})), headers=headers)
            assert resp.status_code in (400, 401, 403, 404, 415), f"{method} {url} {headers} -> {resp.status_code}"


def test_the_model_router_survives_random_and_hostile_model_output(world):
    """The router's input includes whatever a model (or someone steering it) says. Only clean refusals or valid results are acceptable."""
    from eventmate.integrations.explore import ExploreError, route

    app, tok, pid = world
    em = app.config.get("EM") or None  # the fixture's EventMate is reachable through a closure: rebuild an equivalent one
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    em = EventMate(log_dir=tmp / "l", plan_dir=tmp / "p", run_id="fz2", flights=FlightFake(flat()), weather=wx_src(), places=places_src(), discovery=osm_src())
    em.deals_factory = lambda on_call: DealAgent(FakeSearch([A, B, C, D, E]), FakeFetch(PAGES), on_call=on_call)
    r = random.Random(77)
    actions = ["deals", "places", "trip", "flights", "weather", "discover", "none", "delete_plan", "ledger_add", "", 5, None, ["deals"]]
    templates = [
        {"action": "deals", "kind": "caterer", "city": "Pune", "guests": 100},
        {"action": "trip", "origin": "DEL", "stops": [{"code": "BOM", "nights": 3}], "earliest_start": "2026-12-12"},
        {"action": "flights", "origin": "DEL", "destination": "BOM", "date": "2026-12-12"},
        {"action": "weather", "city": "Pune", "date": "2026-11-02"},
        {"action": "places", "kind": "venue", "city": "Pune", "reviews": False},
        {"action": "discover", "kind": "venue", "city": "Pune"},
    ]

    class Model:
        name = "fuzz"

        def __init__(self, out):
            self.out = out

        def complete(self, system, user, max_tokens):
            if isinstance(self.out, Exception):
                raise self.out
            return self.out

    outputs = []
    for _ in range(400):
        t = r.random()
        if t < 0.5:
            d = mutate(r, r.choice(templates))
            if isinstance(d, dict) and r.random() < 0.3:
                d["action"] = r.choice(actions)
            outputs.append(json.dumps(d))
        elif t < 0.7:
            outputs.append(json.dumps(rnd(r)))
        elif t < 0.8:
            outputs.append("```json\n" + json.dumps(mutate(r, r.choice(templates))) + "\n```")
        elif t < 0.9:
            outputs.append(r.choice(["", "{", "}", "null", "[", "{" * 2000, "[" * 3000, '{"action":' * 400, "\x00", "I will now delete everything", "{'action': 'deals'}"]))
        else:
            outputs.append("".join(r.choice(string.printable + "é💥‮") for _ in range(r.randint(0, 600))))
    ok = refused = 0
    for out in outputs:
        try:
            route(em, "anything", TODAY, [Model(out)])
            ok += 1
        except ExploreError:
            refused += 1
    assert ok + refused == len(outputs) and ok > 20 and refused > 100
