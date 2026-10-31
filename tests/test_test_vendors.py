"""Test vendors (ADR-049): honest, labelled, and only ever an address the user owns. A discovered business can never be one, and one can never pass as discovered."""

import json
from datetime import timedelta

import pytest

from eventmate.messaging.conversations import SendPolicy
from eventmate.schemas import VendorCategory as C
from eventmate.service import EventMate
from tests.conftest import TODAY
from tests.test_conversations import Clock, FakeChannel
from tests.test_discovery import Transport as OsmTransport, src as osm_src
from tests.test_shortlist import OSM
from eventmate.web.app import create_app

COLLEGE = "Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering."
LABEL = "Test Caterer (TEST VENDOR, address owned by Asha)"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def chans():
    return {"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp", False, False)}


@pytest.fixture
def em(tmp_path, chans, clock):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="tv", channels=chans, require_symbolic=False, discovery=osm_src(OsmTransport(payload=OSM)))
    e.outreach_store.now = e.conversations.now = e.jobs_clock = clock
    e.conversations.policy = SendPolicy(per_vendor_per_day=20, per_day=200, whatsapp_gap_s=0)
    return e


@pytest.fixture
def pid(em):
    return em.plan(COLLEGE, today=TODAY).plan.plan_id


def make(em, pid=None, **kw):
    args = dict(name="Test Caterer", city="Pune", category=C.CATERER, email="owner@personal.test", owns_address=True, confirmed_by="Asha")
    args.update(kw)
    return em.add_test_vendor(plan_id=pid, **args)


# ---- ownership must be confirmed

@pytest.mark.parametrize("kw", [dict(owns_address=False), dict(owns_address=None), dict(owns_address="yes"), dict(owns_address=1), dict(confirmed_by=""), dict(confirmed_by="  ")])
def test_a_test_vendor_needs_the_users_explicit_statement_that_they_own_the_address(em, pid, kw):
    with pytest.raises(ValueError, match="own"):
        make(em, pid, **kw)
    assert em.externals.all() == [] and em.outreach_store.contacts() == {}


@pytest.mark.parametrize("bad", ["nobody", "a@b.example,c@d.example", "a@b.example\nBcc: x@y.example", "<a@b.example>", ""])
def test_a_test_vendor_needs_one_valid_address(em, pid, bad):
    with pytest.raises(ValueError):
        make(em, pid, email=bad)
    assert em.externals.all() == []


def test_a_test_vendor_cannot_use_a_vendors_address_that_the_app_already_wrote_to(em, pid, chans):
    em.set_vendor_contact("cat-pune-rasoi", "r@rasoi.test", None, "email")
    d = em.draft_outreach(pid, C.CATERER, "email", "r@rasoi.test", "Asha", vendor_id="cat-pune-rasoi")
    em.approve_outreach(pid, d.draft_id, d.content_hash)
    with pytest.raises(ValueError, match="own"):
        make(em, pid, email="r@rasoi.test", owns_address=False)  # ownership is a statement, and it is required whichever address it is


# ---- what a test vendor is

def test_a_test_vendor_is_an_external_record_marked_test_with_the_owner_named(em, pid):
    biz, contact = make(em, pid)
    assert biz.is_test and biz.origin == "test" and biz.source == "test" and biz.owner_confirmed_by == "Asha" and biz.external_id.startswith("test:")
    assert contact.origin == "test" and contact.confirmed_by == "Asha" and contact.email == "owner@personal.test" and contact.source_url is None
    assert em.repo.get(C.CATERER, biz.vendor_id) is None and biz.vendor_id.startswith("ext_")


def test_the_same_name_and_address_is_the_same_test_vendor_and_a_different_address_is_another(em, pid):
    a, _ = make(em, pid)
    b, _ = make(em, pid)
    c, _ = make(em, pid, email="owner2@personal.test")
    assert a.vendor_id == b.vendor_id != c.vendor_id and len(em.externals.all()) == 2


def test_discovery_can_never_produce_a_test_vendor_and_a_test_vendor_can_never_pass_as_discovered(em, pid):
    em.search_vendors(pid, C.VENUE, "osm")
    assert em.externals.all() and all(not b.is_test and b.origin == "discovered" and b.source == "osm" for b in em.externals.all())
    with pytest.raises(ValueError):
        em.add_discovered("test:me", "Fake", "Pune", C.CATERER, "test")
    with pytest.raises(ValueError):
        em.add_discovered("test:me", "Fake", "Pune", C.CATERER, "osm")
    tv, _ = make(em, pid)
    assert tv.origin == "test" and em.resolve_vendor(tv.vendor_id).origin == "test"
    assert all(b.origin == "discovered" for b in em.externals.all() if not b.is_test)


# ---- the label is everywhere

def test_the_label_names_the_owner_in_the_shortlist_drafts_the_job_and_the_digest(em, pid, chans, clock):
    biz, _ = make(em, pid)
    assert em._vendor_names()[biz.vendor_id] == LABEL and em.vendor_labels()[biz.vendor_id] == "TEST VENDOR (address owned by Asha)"
    assert em.shortlist_view(pid)[0].business.owner_confirmed_by == "Asha" and em.shortlist_view(pid)[0].business.origin == "test"
    job = em.jobs.start_job(pid, C.CATERER, [biz.vendor_id], "1 caterer quote", clock() + timedelta(days=3), "Asha", min_quotes=1)
    d = em.outreach(pid)[0]
    assert d.vendor_origin == "test" and d.vendor_name == "Test Caterer" and "TEST VENDOR" not in d.body  # the message to the owner reads like a real request; the label is the app's
    em.jobs.approve_requests(job.job_id)
    assert [s[0] for s in chans["email"].sent] == ["owner@personal.test"]
    from eventmate.messaging.channels import Inbound

    chans["email"].inbox = [Inbound(channel="email", sender="owner@personal.test", external_id="<r1>", received_at=clock() + timedelta(minutes=5), body="Total INR 50,000 for the event.", in_reply_to=em.outreach(pid)[0].external_id)]
    clock.advance(minutes=20)
    em.jobs.tick()
    digest = em.digest_store.list(pid)[0].text
    assert LABEL in digest and "ext_" not in digest  # the label, never the internal id


def test_a_discovered_business_has_no_test_label(em, pid):
    em.search_vendors(pid, C.VENUE, "osm")
    assert all("TEST VENDOR" not in n for n in em._vendor_names().values()) and em.vendor_labels() == {}


# ---- API and CLI

@pytest.fixture
def client(em, pid):
    return create_app(em, today_fn=lambda: TODAY, poll_seconds=0).test_client()


def post(client, url, body):
    token = client.get("/api/session").get_json()["csrf"]
    return client.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": token, "Content-Type": "application/json"})


def test_api_creates_a_test_vendor_only_with_the_ownership_flag_and_lists_labels(client, em, pid):
    body = {"name": "Test Caterer", "category": "caterer", "email": "owner@personal.test", "owns_address": True, "by": "Asha"}
    for bad in ({**body, "owns_address": False}, {k: v for k, v in body.items() if k != "owns_address"}, {**body, "by": ""}, {**body, "email": "nope"}, {**body, "category": "yacht"}, {**body, "surprise": 1}):
        assert post(client, f"/api/plans/{pid}/shortlist/test-vendor", bad).status_code in (400, 422), bad
    assert client.get(f"/api/plans/{pid}/shortlist").get_json() == []
    r = post(client, f"/api/plans/{pid}/shortlist/test-vendor", body)
    assert r.status_code == 201 and r.get_json()["business"]["origin"] == "test" and r.get_json()["contact"]["origin"] == "test"
    assert client.get("/api/vendors/labels").get_json() == {r.get_json()["business"]["vendor_id"]: "TEST VENDOR (address owned by Asha)"}
    items = client.get(f"/api/plans/{pid}/shortlist").get_json()
    assert items[0]["business"]["is_test"] is True and items[0]["business"]["owner_confirmed_by"] == "Asha"


def test_cli_needs_the_ownership_flag(em, pid, tmp_path, capsys):
    from eventmate.__main__ import main

    base = ["--plan-dir", str(tmp_path / "p"), "--log-dir", str(tmp_path / "l"), "testvendor", pid, "caterer", "Test Caterer", "owner@personal.test", "--by", "Asha"]
    assert main(base) == 2 and "--i-own-this-address" in capsys.readouterr().err
    assert main(base + ["--i-own-this-address"]) == 0 and "TEST VENDOR" in capsys.readouterr().out
