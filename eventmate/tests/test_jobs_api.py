"""HTTP API and CLI for follow-through jobs (ADR-044)."""

import json
from datetime import timedelta

import pytest

from eventmate.channels import Inbound
from eventmate.conversations import SendPolicy
from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY
from eventmate.tests.test_jobs_worker import Clock, FakeChannel, NoWhatsApp, VENDORS
from eventmate.web.app import create_app


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def chan():
    return FakeChannel()


@pytest.fixture
def em(tmp_path, chan, clock):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="japi", channels={"email": chan, "whatsapp": NoWhatsApp()}, require_symbolic=False)
    e.outreach_store.now = e.conversations.now = e.jobs_clock = clock
    e.conversations.policy = SendPolicy(per_vendor_per_day=10, per_day=100, whatsapp_gap_s=0)
    return e


@pytest.fixture
def pid(em):
    return em.plan(EXAMPLE, today=TODAY).plan.plan_id


@pytest.fixture
def client(em):
    return create_app(em, today_fn=lambda: TODAY, poll_seconds=0).test_client()


def post(client, url, body):
    token = client.get("/api/session").get_json()["csrf"]
    return client.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": token, "Content-Type": "application/json"})


def with_contacts(client):
    for vid, (_, to) in VENDORS.items():
        assert post(client, "/api/contacts", {"vendor_id": vid, "email": to, "preferred": "email"}).status_code == 201


JOB = {"category": "caterer", "vendor_ids": list(VENDORS), "goal": "3 caterer quotes", "sender_name": "Asha", "deadline_days": 7, "min_quotes": 3}


def test_job_flow_over_http_from_start_to_a_recorded_quote(client, em, pid, chan, clock):
    with_contacts(client)
    r = post(client, f"/api/plans/{pid}/jobs", JOB)
    assert r.status_code == 201, r.get_json()
    job = r.get_json()
    assert job["status"] == "waiting_user" and chan.sent == [] and set(job["vendors"]) == set(VENDORS)
    assert post(client, f"/api/jobs/{job['job_id']}/approve-requests", {}).get_json()["status"] == "active" and len(chan.sent) == 3
    chan.inbox = [Inbound(channel="email", sender="r@rasoi.test", external_id="<r1>", received_at=clock() + timedelta(minutes=5), body="Total INR 66,000 for the event.")]
    clock.advance(minutes=20)
    assert "ran" in post(client, "/api/jobs/tick", {}).get_json()
    listed = client.get(f"/api/plans/{pid}/jobs").get_json()[0]
    assert listed["claims"][0]["amount_inr"] == 66_000 and "confirm" in listed["digest"].lower() and em.observations.quotes("cat-pune-rasoi") == []
    claim = listed["claims"][0]["claim_id"]
    assert post(client, f"/api/jobs/{job['job_id']}/claims/{claim}/confirm", {}).status_code in (400, 422)  # validity is required
    assert post(client, f"/api/jobs/{job['job_id']}/claims/{claim}/confirm", {"valid_days": 0}).status_code in (400, 422)
    assert post(client, f"/api/jobs/{job['job_id']}/claims/{claim}/confirm", {"valid_days": 14}).status_code == 200
    q = em.observations.quotes("cat-pune-rasoi")[0]
    assert q.amount_inr == 66_000 and q.entered_by == "user_confirmed"


def test_reject_resolve_nudge_cancel_and_unknown_ids(client, pid, chan, clock):
    with_contacts(client)
    job = post(client, f"/api/plans/{pid}/jobs", JOB).get_json()
    jid = job["job_id"]
    item = job["items"][0]["item_id"]
    assert post(client, f"/api/jobs/{jid}/items/{item}/resolve", {"note": "done by phone"}).status_code == 200
    assert post(client, f"/api/jobs/{jid}/items/{'f' * 12}/resolve", {}).status_code == 400
    assert post(client, f"/api/jobs/{jid}/claims/nope/reject", {}).status_code == 200  # rejecting nothing is harmless
    assert post(client, f"/api/jobs/{jid}/items/{item}/approve-nudge", {}).status_code == 400
    assert post(client, f"/api/jobs/{'f' * 16}/cancel", {}).status_code == 400
    assert post(client, "/api/jobs/..%2Fx/cancel", {}).status_code in (400, 404)
    assert post(client, f"/api/jobs/{jid}/cancel", {}).get_json()["status"] == "cancelled"


def test_bad_job_bodies_are_refused_and_csrf_is_required(client, pid):
    with_contacts(client)
    for bad in ({**JOB, "vendor_ids": []}, {**JOB, "min_quotes": 0}, {**JOB, "deadline_days": 0}, {**JOB, "deadline_days": 61}, {**JOB, "sender_name": ""}, {**JOB, "extra": 1}, {**JOB, "max_amount_inr": -5},
                {**JOB, "category": "yacht"}, {**JOB, "vendor_ids": ["x"] * 11}, {**JOB, "min_quotes": 4, "vendor_ids": list(VENDORS)}, {**JOB, "vendor_ids": ["cat-pune-darbar"] * 3}):
        assert post(client, f"/api/plans/{pid}/jobs", bad).status_code in (400, 422), bad
    raw = client.post(f"/api/plans/{pid}/jobs", data=json.dumps(JOB), headers={"Content-Type": "application/json"})
    assert raw.status_code in (400, 403)
    assert client.get("/api/plans/nope/jobs").status_code in (400, 404)


def test_listing_jobs_creates_no_database(client, em, pid):
    assert client.get(f"/api/plans/{pid}/jobs").get_json() == [] and not (em.store.root / "jobs.db").exists()


def test_cli_jobs_start_show_approve_confirm_needs_validity(em, pid, tmp_path, capsys, chan):
    from eventmate.__main__ import main

    for vid, (_, to) in VENDORS.items():
        em.set_vendor_contact(vid, to, None, "email")
    base = ["--plan-dir", str(tmp_path / "p"), "--log-dir", str(tmp_path / "l")]
    assert main(base + ["jobs", "start", pid, "caterer", "--vendors", ",".join(VENDORS), "--name", "Asha"]) == 0
    assert "nothing sent" in capsys.readouterr().out
    assert main(base + ["jobs", "show", pid]) == 0 and "approve" in capsys.readouterr().out.lower()
    assert main(base + ["jobs", "start", pid, "caterer"]) == 2
    assert main(base + ["jobs", "confirm", "somejob", "someclaim"]) == 2 and "--valid-days" in capsys.readouterr().err
    assert main(base + ["jobs", "tick"]) == 0
