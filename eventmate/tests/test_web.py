import json
import pytest

from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY
from eventmate.web.app import create_app


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="web")


@pytest.fixture
def app(em):
    return create_app(em, today_fn=lambda: TODAY)


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client):
    return client.get("/api/session").get_json()["csrf"]


def post(client, url, body, token=None, **headers):
    h = {"X-EventMate-CSRF": token or csrf(client), "Content-Type": "application/json", **headers}
    return client.post(url, data=json.dumps(body), headers=h)


@pytest.fixture
def plan_id(client):
    r = post(client, "/api/plans", {"request": EXAMPLE})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["plan"]["plan_id"]


# --------------------------------------------------------------- functionality


def test_index_and_assets_are_served(client):
    for url, ctype in (("/", "text/html"), ("/static/app.js", "javascript"), ("/static/style.css", "text/css")):
        r = client.get(url)
        assert r.status_code == 200 and ctype in r.content_type


def test_create_and_fetch_a_plan(client, plan_id):
    r = client.get(f"/api/plans/{plan_id}")
    plan = r.get_json()
    assert r.status_code == 200 and plan["version"] == 1 and plan["validation"]["passed"] is True
    assert client.get("/api/plans").get_json()[0]["plan_id"] == plan_id


def test_unplannable_request_returns_422_with_repairs_and_no_plan(client):
    r = post(client, "/api/plans", {"request": EXAMPLE.replace("INR 200,000", "INR 60,000")})
    body = r.get_json()
    assert r.status_code == 422 and body["plan"] is None and body["violations"] and all(v["repair"] for v in body["violations"])


def test_parse_errors_are_422_and_list_the_problems(client):
    r = post(client, "/api/plans", {"request": "Plan a party."})
    assert r.status_code == 422 and len(r.get_json()["problems"]) == 4


def test_confirm_replan_diff_versions_decisions_explanation(client, plan_id):
    r = post(client, f"/api/plans/{plan_id}/confirm", {"category": "venue"})
    assert r.status_code == 200 and r.get_json()["plan"]["version"] == 2
    r = post(client, f"/api/plans/{plan_id}/replan", {"kind": "budget_change", "new_budget": 190000})
    out = r.get_json()
    assert r.status_code == 200 and out["status"] == "replanned" and out["plan"]["version"] == 3
    assert out["diff"]["preserved"] and out["diff"]["changed"]
    vs = client.get(f"/api/plans/{plan_id}/versions").get_json()
    assert [v["version"] for v in vs] == [1, 2, 3] and vs[2]["parent_version"] == 2
    d = client.get(f"/api/plans/{plan_id}/diff?old=1&new=3").get_json()
    assert d["new_version"] == 3
    md = client.get(f"/api/plans/{plan_id}/explanation?version=3").get_json()["markdown"]
    assert "Changes from version 2 to 3" in md
    dec = client.get(f"/api/plans/{plan_id}/decisions?version=3").get_json()
    assert dec and all(e["justification"] for e in dec)
    assert client.get(f"/api/plans/{plan_id}/validate").get_json()["passed"] is True


def test_needs_decision_is_409_and_saves_nothing(client, plan_id):
    post(client, f"/api/plans/{plan_id}/confirm", {"category": "venue"})
    r = post(client, f"/api/plans/{plan_id}/replan", {"kind": "date_change", "new_date": "2026-12-19"})
    assert r.status_code == 409 and r.get_json()["status"] == "needs_decision"
    assert len(client.get(f"/api/plans/{plan_id}/versions").get_json()) == 2


def test_unknown_plan_is_404_and_bad_ids_are_rejected(client):
    assert client.get("/api/plans/nope").status_code == 404
    for bad in ("..%2Fx", "a%20b", "x" * 80):
        assert client.get(f"/api/plans/{bad}").status_code in (400, 404)


def test_invalid_bodies_are_422_not_500(client, plan_id):
    for url, body in (
        ("/api/plans", {}),
        ("/api/plans", {"request": 5}),
        ("/api/plans", {"request": "x" * 5000}),
        ("/api/plans", {"request": EXAMPLE, "unexpected": 1}),
        (f"/api/plans/{plan_id}/confirm", {"category": "dj"}),
        (f"/api/plans/{plan_id}/replan", {"kind": "budget_change"}),
        (f"/api/plans/{plan_id}/replan", {"kind": "budget_change", "new_budget": -5}),
    ):
        assert post(client, url, body).status_code == 422, (url, body)


# ------------------------------------------------------------------ security


def test_post_without_csrf_token_is_rejected(client):
    r = client.post("/api/plans", data=json.dumps({"request": EXAMPLE}), headers={"Content-Type": "application/json"})
    assert r.status_code == 403
    assert post(client, "/api/plans", {"request": EXAMPLE}, token="wrong").status_code == 403


def test_cross_origin_post_is_rejected_even_with_a_token(client):
    r = post(client, "/api/plans", {"request": EXAMPLE}, Origin="https://evil.example")
    assert r.status_code == 403


def test_same_origin_post_is_accepted(client):
    r = post(client, "/api/plans", {"request": EXAMPLE}, Origin="http://localhost")
    assert r.status_code == 201


def test_non_json_content_type_is_rejected(client):
    r = client.post("/api/plans", data="request=x", headers={"X-EventMate-CSRF": csrf(client), "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_dns_rebinding_host_is_rejected(client):
    assert client.get("/api/plans", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "attacker.com:5000"}).status_code == 400


def test_security_headers_on_every_response(client):
    for url in ("/", "/api/plans", "/static/app.js"):
        h = client.get(url).headers
        csp = h["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "frame-ancestors 'none'" in csp
        assert h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
    assert client.get("/api/plans").headers["Cache-Control"] == "no-store"


def test_oversized_body_is_413(client):
    r = client.post("/api/plans", data="x" * 200_000, headers={"X-EventMate-CSRF": csrf(client), "Content-Type": "application/json"})
    assert r.status_code == 413


def test_static_route_cannot_traverse_or_serve_other_files(client):
    for bad in ("../app.py", "..%2Fapp.py", "app.py", "%2e%2e/app.py", "index.html/../../x"):
        assert client.get(f"/static/{bad}").status_code == 404


def test_api_token_is_enforced_when_configured(em):
    app = create_app(em, api_token="s3cret", today_fn=lambda: TODAY)
    c = app.test_client()
    assert c.get("/api/plans").status_code == 401
    assert c.get("/api/plans", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/plans", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/").status_code == 200  # the shell loads; data does not


def test_rate_limit_on_writes(em):
    app = create_app(em, rate_limit_per_minute=5, today_fn=lambda: TODAY)
    c = app.test_client()
    tok = c.get("/api/session").get_json()["csrf"]
    codes = [c.post("/api/plans", data=json.dumps({"request": "bad"}), headers={"X-EventMate-CSRF": tok, "Content-Type": "application/json"}).status_code for _ in range(8)]
    assert 429 in codes and codes.index(429) == 5


def test_internal_errors_do_not_leak_details(em):
    app = create_app(em, today_fn=lambda: TODAY)
    em.plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret path /home/x/.env"))
    c = app.test_client()
    r = post(c, "/api/plans", {"request": EXAMPLE})
    assert r.status_code == 500 and "secret" not in r.get_data(as_text=True) and "Traceback" not in r.get_data(as_text=True)


def test_tampered_stored_plan_is_reported_without_internals(client, plan_id, em):
    import os

    path = em.store._path(plan_id, 1)
    os.chmod(path, 0o644)
    path.write_text(path.read_text().replace('"total_cost": 139000', '"total_cost": 1'))
    r = client.get(f"/api/plans/{plan_id}")
    assert r.status_code == 500 and r.get_json()["error"] == "stored plan failed its integrity check"


def test_responses_never_echo_the_raw_request_text_or_log_paths(client, plan_id):
    body = client.get(f"/api/plans/{plan_id}").get_data(as_text=True)
    assert "/home/" not in body and "logs/" not in body


def test_server_refuses_remote_bind_without_token(monkeypatch, capsys):
    from eventmate.web.__main__ import main

    monkeypatch.delenv("EVENTMATE_WEB_TOKEN", raising=False)
    assert main(["--host", "0.0.0.0"]) == 2
    assert main(["--host", "0.0.0.0", "--allow-remote"]) == 2
    assert "refusing" in capsys.readouterr().err


def test_parallel_requests_do_not_cross_decision_logs(app, em):
    import threading

    results, lock = [], threading.Lock()

    def work():
        c = app.test_client()
        tok = c.get("/api/session").get_json()["csrf"]
        r = c.post("/api/plans", data=json.dumps({"request": EXAMPLE}), headers={"X-EventMate-CSRF": tok, "Content-Type": "application/json"})
        with lock:
            results.append((r.status_code, r.get_json()["plan"]["plan_id"]))

    ts = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(code == 201 for code, _ in results) and len({pid for _, pid in results}) == 6
    for _, pid in results:
        plan = em.store.load(pid)
        own = {e.request_id for e in em.decisions_for(plan)}
        assert own == {plan.request.request_id}  # no other request's decisions leaked in


def test_unknown_guest_list_is_a_422_with_a_reason_not_a_500(client):
    r = post(client, "/api/plans", {"request": EXAMPLE, "guest_list_id": "no-such-list"})
    assert r.status_code == 422 and "guest list" in r.get_json()["error"]


def test_corrupt_vendor_data_is_a_generic_500_without_details(tmp_path):
    from eventmate.agents.vendor import DataIntegrityError

    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p")
    em.plan = lambda *a, **k: (_ for _ in ()).throw(DataIntegrityError("/secret/path/seed.json line 3"))
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    r = post(c, "/api/plans", {"request": EXAMPLE})
    assert r.status_code == 500 and "/secret" not in r.get_data(as_text=True)


# ------------------------------------------------------------------ trade-offs


def tight(budget=150_000):
    return EXAMPLE.replace("200,000", f"{budget:,}")


def test_tradeoffs_endpoint_proposes_verified_options_and_stores_nothing(client, em, tmp_path):
    r = post(client, "/api/tradeoffs", {"request": tight()})
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "options" and data["options"] and all(o["verified"] for o in data["options"])
    assert list((tmp_path / "plans").glob("**/*.json")) == []


def test_apply_tradeoff_creates_a_plan_whose_reason_names_the_choice(client):
    opts = post(client, "/api/tradeoffs", {"request": tight()}).get_json()["options"]
    r = post(client, "/api/tradeoffs/apply", {"option": opts[0]})
    assert r.status_code == 201, r.get_json()
    plan = r.get_json()["plan"]
    assert plan["change_reason"].startswith("trade-off chosen by the user")


def test_apply_rejects_a_forged_option(client):
    opts = post(client, "/api/tradeoffs", {"request": tight()}).get_json()["options"]
    forged = {**opts[0], "request": {**opts[0]["request"], "guest_count": 0}}
    assert post(client, "/api/tradeoffs/apply", {"option": forged}).status_code == 422
    extra = {**opts[0], "approved": True}
    assert post(client, "/api/tradeoffs/apply", {"option": extra}).status_code == 422


def test_apply_still_refuses_an_option_the_validator_rejects(client):
    opts = post(client, "/api/tradeoffs", {"request": tight()}).get_json()["options"]
    bad = {**opts[0], "request": {**opts[0]["request"], "budget_total": 1000, "cap_weights": []}}
    r = post(client, "/api/tradeoffs/apply", {"option": bad})
    assert r.status_code == 422 and r.get_json()["violations"]


def test_tradeoffs_need_csrf_and_valid_body(client):
    assert client.post("/api/tradeoffs", data=json.dumps({"request": tight()}), headers={"Content-Type": "application/json"}).status_code == 403
    assert post(client, "/api/tradeoffs", {"request": ""}).status_code == 422
    assert post(client, "/api/tradeoffs", {"request": tight(), "x": 1}).status_code == 422
