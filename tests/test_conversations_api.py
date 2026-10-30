"""HTTP API, CLI and background loop for the vendor-conversation agent (ADR-042)."""

import json
from datetime import timedelta

import pytest

from eventmate.messaging.channels import Inbound
from eventmate.schemas import VendorCategory as C
from eventmate.service import EventMate
from tests.conftest import EXAMPLE, TODAY
from tests.test_conversations import T0, FakeChannel
from eventmate.web.app import create_app


@pytest.fixture
def chans():
    return {"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp")}


@pytest.fixture
def em(tmp_path, chans):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="api", channels=chans)
    e.outreach_store.now = e.conversations.now = lambda: T0
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


WA = {"category": "caterer", "channel": "whatsapp", "recipient": "9876543210", "sender_name": "Asha"}


def test_full_loop_over_http_draft_approve_reply_followup(client, pid, chans):
    d = post(client, f"/api/plans/{pid}/outreach", WA).get_json()
    assert d["status"] == "prepared" and len(d["content_hash"]) == 64 and chans["whatsapp"].sent == []
    ok = post(client, f"/api/plans/{pid}/outreach/{d['draft_id']}/approve", {"content_hash": d["content_hash"]})
    assert ok.status_code == 200 and ok.get_json()["status"] == "sent" and len(chans["whatsapp"].sent) == 1
    chans["whatsapp"].inbox = [Inbound(channel="whatsapp", sender="919876543210", external_id="w1", received_at=T0 + timedelta(minutes=3), body="Rs 650 per plate. Available.")]
    assert post(client, "/api/inbox/poll", {}).get_json()["new_messages"] == 1
    box = client.get(f"/api/plans/{pid}/inbox").get_json()
    assert box["unread_total"] == 1 and box["threads"][0]["replies"][0]["analysis"]["amounts"][0]["low_inr"] == 650
    assert {c["channel"] for c in box["channels"]} == {"email", "whatsapp"}
    sug = client.get(f"/api/plans/{pid}/outreach/{d['draft_id']}/suggestions").get_json()["suggestions"]
    assert sug
    f = post(client, f"/api/plans/{pid}/outreach/{d['draft_id']}/followup", {"text": sug[0], "sender_name": "Asha"})
    assert f.status_code == 201 and f.get_json()["status"] == "prepared" and len(chans["whatsapp"].sent) == 1
    mid = box["threads"][0]["replies"][0]["message_id"]
    assert post(client, f"/api/plans/{pid}/inbox/{mid}/read", {}).get_json()["read"] is True


def test_approval_needs_the_current_hash_and_a_csrf_token(client, pid, chans):
    d = post(client, f"/api/plans/{pid}/outreach", WA).get_json()
    bad = post(client, f"/api/plans/{pid}/outreach/{d['draft_id']}/approve", {"content_hash": "0" * 64})
    assert bad.status_code in (400, 409, 422) and chans["whatsapp"].sent == []
    raw = client.post(f"/api/plans/{pid}/outreach/{d['draft_id']}/approve", data=json.dumps({"content_hash": d["content_hash"]}), headers={"Content-Type": "application/json"})
    assert raw.status_code in (400, 403) and chans["whatsapp"].sent == []
    assert post(client, f"/api/plans/{pid}/outreach/{d['draft_id']}/approve", {"content_hash": "short"}).status_code in (400, 422)
    assert post(client, f"/api/plans/{pid}/outreach/{d['draft_id']}/approve", {"content_hash": d["content_hash"], "extra": 1}).status_code == 422


def test_deny_blocks_sending(client, pid, chans):
    d = post(client, f"/api/plans/{pid}/outreach", WA).get_json()
    assert post(client, f"/api/plans/{pid}/outreach/{d['draft_id']}/deny", {}).get_json()["status"] == "denied"
    assert post(client, f"/api/plans/{pid}/outreach/{d['draft_id']}/approve", {"content_hash": d["content_hash"]}).status_code in (400, 409)
    assert chans["whatsapp"].sent == []


def test_followup_and_ids_are_validated(client, pid):
    assert post(client, f"/api/plans/{pid}/outreach/{'f' * 32}/followup", {"text": "hi", "sender_name": "A"}).status_code in (400, 404)
    assert post(client, f"/api/plans/{pid}/outreach/..%2Fx/followup", {"text": "hi", "sender_name": "A"}).status_code in (400, 404)
    assert post(client, f"/api/plans/{pid}/outreach/{'f' * 32}/followup", {"text": "x" * 1001, "sender_name": "A"}).status_code in (400, 422)
    assert client.get("/api/plans/nope/inbox").status_code in (400, 404)
    assert post(client, f"/api/plans/{pid}/inbox/{'f' * 32}/read", {}).status_code in (400, 404)


def test_contacts_and_request_all_over_http(client, em, pid):
    chosen = em.store.latest(pid).selection_for(C.CATERER).vendor_id
    assert post(client, "/api/contacts", {"vendor_id": chosen, "whatsapp": "+91 98765 43210", "preferred": "whatsapp"}).status_code == 201
    assert post(client, "/api/contacts", {"vendor_id": chosen, "preferred": "email"}).status_code in (400, 422)
    assert post(client, "/api/contacts", {"vendor_id": "unknown-1", "email": "a@b.in", "preferred": "email"}).status_code in (400, 422)
    assert client.get("/api/contacts").get_json()[0]["whatsapp"] == "919876543210"
    r = post(client, f"/api/plans/{pid}/outreach/request-all", {"sender_name": "Asha"}).get_json()
    assert len(r["drafts"]) == 1 and r["drafts"][0]["status"] == "prepared" and r["skipped"]


def test_poll_without_channels_does_nothing_harmful(tmp_path):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="none", channels={"email": FakeChannel("email", False, False), "whatsapp": FakeChannel("whatsapp", False, False)})
    c = create_app(e, today_fn=lambda: TODAY, poll_seconds=0).test_client()
    assert post(c, "/api/inbox/poll", {}).get_json() == {"new_messages": 0, "ignored": 0, "sent": 0, "waiting": [], "errors": []}


def test_background_round_sends_approved_drafts_reads_replies_and_survives_errors(tmp_path, chans, capsys):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="loop", channels=chans)
    e.outreach_store.now = e.conversations.now = lambda: T0
    p = e.plan(EXAMPLE, today=TODAY).plan.plan_id
    app = create_app(e, today_fn=lambda: TODAY, poll_seconds=3600)
    app.config["POLL_STOP"].set()  # stop the thread; drive one round by hand
    d = e.draft_outreach(p, C.CATERER, "email", "sales@annapurna.in", "Asha")
    e.outreach_store.transition(p, d.draft_id, ("prepared",), status="approved", decided_at=T0)  # approved, but not yet sent (for example the app was busy)
    assert chans["email"].sent == []
    chans["email"].inbox = [Inbound(channel="email", sender="sales@annapurna.in", external_id="<z1>", received_at=T0 + timedelta(minutes=1), body="Rs 600 per plate")]
    app.config["POLL_ROUND"]()
    assert len(chans["email"].sent) == 1 and e.outreach(p)[0].status == "sent"
    app.config["POLL_ROUND"]()
    assert len(e.outreach_store.inbound(p)) == 1  # the reply was read, and only once
    e.conversations.poll = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    app.config["POLL_ROUND"]()  # must not raise
    assert "RuntimeError" in capsys.readouterr().err


def test_the_loop_is_off_when_no_channel_is_configured_and_not_forced(tmp_path, monkeypatch):
    monkeypatch.delenv("EVENTMATE_POLL_SECONDS", raising=False)
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="off", channels={"email": FakeChannel("email", False, False), "whatsapp": FakeChannel("whatsapp", False, False)})
    assert "POLL_STOP" not in create_app(e, today_fn=lambda: TODAY).config


def test_cli_shows_approves_and_polls(em, pid, chans, tmp_path, capsys):
    from eventmate.__main__ import main

    d = em.draft_outreach(pid, C.CATERER, "email", "sales@annapurna.in", "Asha")
    base = ["--plan-dir", str(tmp_path / "p"), "--log-dir", str(tmp_path / "l")]
    # the CLI builds its own EventMate with channels from the environment: none configured, so approval is refused cleanly
    assert main(base + ["inbox", "approve", pid, d.draft_id, "--hash", d.content_hash]) != 0
    assert main(base + ["inbox", "show", pid]) == 0
    out = capsys.readouterr().out
    assert "waiting for your approval" in out and d.draft_id in out and d.content_hash[:12] in out
    assert main(base + ["inbox", "poll"]) == 0
    assert main(base + ["inbox", "approve", pid]) == 2
