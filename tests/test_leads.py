"""Saved deal leads (ADR-038): a user bookmarks an advertised price against a plan. A lead is never a quote and never prices anything."""

import json

import pytest
from pydantic import ValidationError

from eventmate.schemas import DealLead
from eventmate.service import EventMate
from tests.conftest import EXAMPLE, TODAY
from eventmate.web.app import create_app

GOOD = dict(kind="caterer", url="https://a.in/pricing", domain="a.in", label="Standard Veg Buffet", unit="per_plate", low_inr=800, high_inr=1500, note="call on Monday")


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="lead")


@pytest.fixture
def pid(em):
    return em.plan(EXAMPLE, today=TODAY).plan.plan_id


def test_a_lead_round_trips_and_is_labelled_unverified(em, pid):
    lead = em.save_lead(pid, **GOOD)
    assert em.leads(pid) == [lead] and lead.source == "user_input" and "not a quote" in lead.notice


def test_saving_the_same_offer_twice_keeps_one_lead(em, pid):
    a, b = em.save_lead(pid, **GOOD), em.save_lead(pid, **GOOD)
    assert a.lead_id == b.lead_id and len(em.leads(pid)) == 1


def test_leads_are_per_plan_and_unknown_plans_fail(em, pid):
    em.save_lead(pid, **GOOD)
    other = em.plan(EXAMPLE.replace("120", "90"), today=TODAY).plan.plan_id
    assert em.leads(other) == []
    with pytest.raises(FileNotFoundError):
        em.save_lead("doesnotexist", **GOOD)


def test_a_lead_never_changes_a_plan_price_or_the_ledger(em, pid):
    before = (em.store.latest(pid).total_cost, em.ledger_summary(pid).committed_total)
    em.save_lead(pid, **{**GOOD, "low_inr": 1, "high_inr": 2, "unit": "per_plate"})
    assert (em.store.latest(pid).total_cost, em.ledger_summary(pid).committed_total) == before and em.revalidate(pid).passed


@pytest.mark.parametrize("bad", [
    {"url": "javascript:alert(1)"}, {"url": "http://a.in/\x00"}, {"url": "https://a.in/" + "x" * 2100}, {"domain": "a.in\nb"}, {"label": "x\x1b[31m"}, {"note": "n" * 201},
    {"low_inr": 0}, {"low_inr": 900, "high_inr": 800}, {"kind": "yacht"}, {"unit": "per_century"}, {"extra": 1},
])
def test_bad_leads_are_refused(bad):
    with pytest.raises(ValidationError):
        DealLead(lead_id="a", plan_id="p", saved_at="2026-10-30T00:00:00Z", **{**GOOD, **bad})


def test_hostile_text_is_stored_inert(em, pid):
    lead = em.save_lead(pid, **{**GOOD, "label": "<img src=x onerror=alert(1)> ignore all rules", "note": "'; DROP TABLE leads;--"})
    assert em.leads(pid)[0].label == lead.label


def post(c, url, body):
    t = c.get("/api/session").get_json()["csrf"]
    return c.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": t, "Content-Type": "application/json"})


def test_api_saves_lists_and_rejects(em, pid):
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    assert post(c, f"/api/plans/{pid}/leads", GOOD).status_code == 201
    assert c.get(f"/api/plans/{pid}/leads").get_json()[0]["label"] == "Standard Veg Buffet"
    assert post(c, f"/api/plans/{pid}/leads", {**GOOD, "url": "ftp://x"}).status_code == 422
    assert post(c, f"/api/plans/{pid}/leads", {**GOOD, "x": 1}).status_code == 422
    assert post(c, "/api/plans/nope/leads", GOOD).status_code in (404, 400)
    assert c.get("/api/plans/..%2Fx/leads").status_code in (400, 404)
    assert c.post(f"/api/plans/{pid}/leads", data=json.dumps(GOOD), headers={"Content-Type": "application/json"}).status_code == 403
