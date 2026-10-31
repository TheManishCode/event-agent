"""External businesses (ADR-049): a discovered business has its own identity, is never a seed vendor, and is contactable only through a confirmed address."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from eventmate.agents.vendor import VendorRepository
from eventmate.messaging.conversations import SendPolicy
from eventmate.schemas import ExternalBusiness, VendorCategory as C, VendorRef, external_vendor_id
from eventmate.service import EventMate
from tests.conftest import TODAY
from tests.test_conversations import Clock, FakeChannel

COLLEGE = "Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering."
SRC = "https://rasoi-pune.example/contact"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def chans():
    return {"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp", False, False)}


@pytest.fixture
def em(tmp_path, chans, clock):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="ext", channels=chans, require_symbolic=False)
    e.outreach_store.now = e.conversations.now = e.jobs_clock = clock
    e.conversations.policy = SendPolicy(per_vendor_per_day=10, per_day=100, whatsapp_gap_s=0)
    return e


@pytest.fixture
def pid(em):
    return em.plan(COLLEGE, today=TODAY).plan.plan_id


def discovered(em, ref="osm:node/1001", name="Sharma Caterers", city="Pune", category=C.CATERER, **kw):
    return em.add_discovered(ref, name, city, category, "osm", source_url="https://www.openstreetmap.org/node/1001", website="https://sharma-caterers.example", **kw)


def confirm(em, vendor_id, value="sales@sharma-caterers.example", by="Asha", edited=None, kind="page_mailto"):
    cand = em.shortlist.add_candidates(vendor_id, [{"channel": "email", "value": value, "source_url": SRC, "source_kind": kind, "flags": ()}])[0]
    return em.confirm_contact(vendor_id, cand.candidate_id, by, edited)


# ---- identity

def test_an_external_id_is_path_safe_deterministic_and_never_a_seed_id():
    vid = external_vendor_id("osm:node/123")
    assert vid.startswith("ext_") and vid == external_vendor_id("osm:node/123") and vid != external_vendor_id("osm:node/124") and len(vid) == 28
    repo = VendorRepository()
    assert not any(p.id.startswith("ext_") for c in C for p in repo.providers(c))
    assert VendorRef.of("cat-pune-rasoi").kind == "seed" and VendorRef.of(vid).kind == "external"


@pytest.mark.parametrize("kind,id_", [("seed", "ext_" + "a" * 24), ("external", "cat-pune-rasoi"), ("external", "x"), ("seed", "../x"), ("seed", ""), ("seed", "a" * 65)])
def test_a_vendor_ref_must_be_explicit_and_consistent(kind, id_):
    with pytest.raises(ValidationError):
        VendorRef(kind=kind, id=id_)


@pytest.mark.parametrize("ref", ["osm:node/", "osm:point/1", "gplaces:short", "x:osm:node/1", "osm:node/1\n", "../etc", "", "test:UPPER"])
def test_an_external_reference_must_come_from_a_known_source_and_shape(em, ref):
    with pytest.raises((ValidationError, ValueError)):
        em.add_discovered(ref, "Name", "Pune", C.CATERER, "osm")


def test_a_discovered_business_can_never_be_a_test_vendor_or_the_other_way_round(em):
    for kwargs in (dict(source="test", external_id="osm:node/5"), dict(source="osm", external_id="test:me")):
        with pytest.raises((ValidationError, ValueError)):
            em.add_discovered(kwargs["external_id"], "N", "Pune", C.CATERER, kwargs["source"])
    base = dict(vendor_id=external_vendor_id("osm:node/9"), external_id="osm:node/9", name="N", city="Pune", category=C.CATERER, source="osm", created_at=datetime.now(timezone.utc))
    ExternalBusiness(**base)
    for bad in ({"is_test": True}, {"vendor_id": external_vendor_id("osm:node/10")}, {"source": "test"}, {"website": "javascript:alert(1)"}, {"source_url": "ftp://x"}, {"surprise": 1}, {"rating": 9.0}, {"name": "x\ny"}):
        with pytest.raises(ValidationError):
            ExternalBusiness(**{**base, **bad})


def test_the_same_source_reference_is_the_same_business(em):
    a, b = discovered(em), discovered(em, name="A different name from a later search")
    assert a.vendor_id == b.vendor_id and b.name == "Sharma Caterers" and len(em.externals.all()) == 1


# ---- it is not a vendor record

def test_an_external_business_never_appears_in_the_vendor_repository_and_is_never_promoted(em, pid):
    seeds = {c: [p.id for p in em.repo.providers(c)] for c in C}
    biz = discovered(em)
    confirm(em, biz.vendor_id)
    assert em.repo.get(C.CATERER, biz.vendor_id) is None and all(biz.vendor_id not in ids for ids in ({p.id for p in em.repo.providers(c)} for c in C))
    assert {c: [p.id for p in em.repo.providers(c)] for c in C} == seeds
    assert em.resolve_vendor(biz.vendor_id).origin == "discovered" and em.resolve_vendor("cat-pune-rasoi").origin == "seed"
    assert not any(s.vendor_id == biz.vendor_id for s in em.store.latest(pid).selections)  # the plan's vendors are untouched


def test_a_business_named_like_a_seed_vendor_stays_a_separate_record(em):
    biz = em.add_discovered("osm:node/77", "Royal Rasoi", "Pune", C.CATERER, "osm")
    seed = em.resolve_vendor("cat-pune-rasoi")
    assert seed.name == "Royal Rasoi" == biz.name and seed.ref.kind == "seed" and em.resolve_vendor(biz.vendor_id).ref.kind == "external" and biz.vendor_id != "cat-pune-rasoi"


# ---- drafting and sending

def test_an_external_business_drafts_and_sends_through_the_normal_path(em, pid, chans, clock):
    biz = discovered(em)
    confirm(em, biz.vendor_id)
    d = em.draft_outreach(pid, C.CATERER, "email", "sales@sharma-caterers.example", "Asha", vendor_id=biz.vendor_id)
    assert d.vendor_id == biz.vendor_id and d.vendor_name == "Sharma Caterers" and d.vendor_origin == "discovered" and d.status == "prepared" and "Hello Sharma Caterers" in d.body
    assert chans["email"].sent == []
    done = em.approve_outreach(pid, d.draft_id, d.content_hash)  # the exact-text approval is unchanged
    assert done.status == "sent" and [s[0] for s in chans["email"].sent] == ["sales@sharma-caterers.example"]


def test_an_external_business_cannot_be_written_to_without_a_confirmed_address(em, pid):
    biz = discovered(em)
    with pytest.raises(ValueError, match="confirmed"):
        em.draft_outreach(pid, C.CATERER, "email", "sales@sharma-caterers.example", "Asha", vendor_id=biz.vendor_id)
    em.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": "sales@sharma-caterers.example", "source_url": SRC, "source_kind": "page_mailto", "flags": ()}])
    with pytest.raises(ValueError, match="confirmed"):  # found, not confirmed
        em.draft_outreach(pid, C.CATERER, "email", "sales@sharma-caterers.example", "Asha", vendor_id=biz.vendor_id)


def test_a_typed_recipient_cannot_stand_in_for_the_confirmed_address(em, pid):
    biz = discovered(em)
    confirm(em, biz.vendor_id)
    with pytest.raises(ValueError, match="confirmed address"):
        em.draft_outreach(pid, C.CATERER, "email", "someone.else@elsewhere.example", "Asha", vendor_id=biz.vendor_id)
    em.draft_outreach(pid, C.CATERER, "email", "SALES@Sharma-Caterers.example", "Asha", vendor_id=biz.vendor_id)  # the same address in another case is the same address


def test_unknown_malformed_and_mismatched_references_fail_closed(em, pid):
    biz = discovered(em, category=C.VENUE, ref="osm:node/2002")
    confirm(em, biz.vendor_id)
    for vid in ("nope-1", "ext_" + "0" * 24, "../x", "ext_zz", "cat-pune-rasoi\n"):
        with pytest.raises(ValueError):
            em.draft_outreach(pid, C.CATERER, "email", "a@b.example", "Asha", vendor_id=vid)
    with pytest.raises(ValueError, match="venue"):  # it is a venue, not a caterer
        em.draft_outreach(pid, C.CATERER, "email", "sales@sharma-caterers.example", "Asha", vendor_id=biz.vendor_id)
    with pytest.raises(ValueError):
        em.set_vendor_contact(biz.vendor_id, "a@b.example", None, "email")  # an external contact is saved only by confirming a found address
    with pytest.raises(ValueError):
        em.set_vendor_contact("ext_" + "0" * 24, "a@b.example", None, "email")


def test_seed_vendor_flows_are_unchanged(em, pid, chans):
    em.set_vendor_contact("cat-pune-rasoi", "r@rasoi.test", None, "email")
    d = em.draft_outreach(pid, C.CATERER, "email", "r@rasoi.test", "Asha", vendor_id="cat-pune-rasoi")
    assert d.vendor_origin == "seed" and em.approve_outreach(pid, d.draft_id, d.content_hash).status == "sent"
    with pytest.raises(ValueError, match="unknown caterer"):
        em.draft_outreach(pid, C.CATERER, "email", "r@rasoi.test", "Asha", vendor_id="cat-nowhere")


def test_the_same_email_on_an_external_and_a_seed_vendor_stays_two_records(em, pid, chans):
    biz = discovered(em)
    confirm(em, biz.vendor_id, value="shared@example.test")
    em.set_vendor_contact("cat-pune-rasoi", "shared@example.test", None, "email")
    contacts = em.outreach_store.contacts()
    assert contacts[biz.vendor_id].email == contacts["cat-pune-rasoi"].email == "shared@example.test"
    assert contacts[biz.vendor_id].origin == "discovered" and contacts["cat-pune-rasoi"].origin == "seed"
    a = em.draft_outreach(pid, C.CATERER, "email", "shared@example.test", "Asha", vendor_id=biz.vendor_id)
    b = em.draft_outreach(pid, C.CATERER, "email", "shared@example.test", "Asha", vendor_id="cat-pune-rasoi")
    assert a.vendor_id != b.vendor_id and a.vendor_name == "Sharma Caterers" and b.vendor_name == "Royal Rasoi"


# ---- replies, quotes and scores key on the reference

def test_a_reply_a_quote_and_a_score_belong_to_the_external_reference(em, pid, chans, clock):
    from eventmate.messaging.channels import Inbound

    biz = discovered(em)
    confirm(em, biz.vendor_id)
    d = em.draft_outreach(pid, C.CATERER, "email", "sales@sharma-caterers.example", "Asha", vendor_id=biz.vendor_id)
    d = em.approve_outreach(pid, d.draft_id, d.content_hash)
    chans["email"].inbox = [Inbound(channel="email", sender="sales@sharma-caterers.example", external_id="<r1>", received_at=clock() + timedelta(minutes=5), body="Total INR 80,000 for the event.")]
    em.poll_conversations()
    msg = em.outreach_store.inbound(pid)[0]
    assert msg.vendor_id == biz.vendor_id and msg.vendor_origin == "discovered" and msg.vendor_name == "Sharma Caterers"
    q = em.observe_quote(biz.vendor_id, C.CATERER, source_type="user_input", entered_by="asha", observed_at=clock(), expires_at=(clock() + timedelta(days=9)).date(), confidence=0.7, amount_inr=80_000, basis="total")
    assert q.vendor_id == biz.vendor_id and em.observations.quotes("cat-pune-rasoi") == []
    with pytest.raises(ValueError, match="unknown vendor"):
        em.observe_quote("ext_" + "1" * 24, C.CATERER, source_type="user_input", entered_by="a", observed_at=clock(), expires_at=(clock() + timedelta(days=9)).date(), confidence=0.7, amount_inr=1, basis="total")
    with pytest.raises(ValueError, match="is a caterer"):
        em.observe_quote(biz.vendor_id, C.VENUE, source_type="user_input", entered_by="a", observed_at=clock(), expires_at=(clock() + timedelta(days=9)).date(), confidence=0.7, amount_inr=1, basis="total")


def test_a_quote_from_a_discovered_business_does_not_change_the_plan_price(em, pid, clock):
    biz = discovered(em)
    before = em.store.latest(pid)
    em.observe_quote(biz.vendor_id, C.CATERER, source_type="user_input", entered_by="asha", observed_at=clock(), expires_at=(clock() + timedelta(days=9)).date(), confidence=0.7, amount_inr=1000, basis="total")
    assert em.store.latest(pid) == before and not [x for x in em.quote_status(pid) if x.price_stale]


def test_names_used_in_digests_label_origin(em):
    biz = discovered(em)
    names = em._vendor_names()
    assert names[biz.vendor_id] == "Sharma Caterers" and names["cat-pune-rasoi"] == "Royal Rasoi"
    tv, _ = em.add_test_vendor("Test Caterer", "Pune", C.CATERER, "owner@personal.test", owns_address=True, confirmed_by="Asha")
    assert em._vendor_names()[tv.vendor_id] == "Test Caterer (TEST VENDOR, address owned by Asha)"


def test_the_vendor_score_ranks_external_businesses_by_their_own_reference_and_the_directory_says_where_each_came_from(em, clock):
    ids = []
    for n, amount in enumerate((500, 600, 700)):
        b = em.add_discovered(f"osm:node/{3000 + n}", f"Caterer {n}", "Pune", C.CATERER, "osm")
        ids.append(b.vendor_id)
        em.observe_quote(b.vendor_id, C.CATERER, source_type="user_input", entered_by="asha", observed_at=clock(), expires_at=(clock() + timedelta(days=9)).date(),
                         confidence=0.7, amount_inr=amount, basis="per_plate", guest_count=100, inclusions=("service staff",))
    board = em.vendor_scores(C.CATERER, TODAY)
    assert [s.vendor_id for s in board.scores][0] == ids[0] and set(s.vendor_id for s in board.scores) == set(ids)
    assert any(c.name == "price" and c.score is not None for c in board.scores[0].components)  # three comparable quotes: price is scored
    tv, _ = em.add_test_vendor("Test Caterer", "Pune", C.CATERER, "owner@personal.test", owns_address=True, confirmed_by="Asha")
    d = em.vendor_directory()
    assert d["cat-pune-rasoi"]["origin"] == "seed" and d[ids[0]] == {"name": "Caterer 0", "origin": "discovered", "label": None}
    assert d[tv.vendor_id]["origin"] == "test" and d[tv.vendor_id]["label"] == "TEST VENDOR (address owned by Asha)"


# ---- audit: the entry points that must refuse, each by name

def test_a_contact_cannot_be_typed_in_for_a_discovered_business(em, pid):
    biz = discovered(em)
    with pytest.raises(ValueError, match="only by confirming an address found"):
        em.set_vendor_contact(biz.vendor_id, "owner@rasoi-pune.example", None, "email")
    assert em.outreach_store.contacts().get(biz.vendor_id) is None
    with pytest.raises(ValueError, match="unknown vendor"):
        em.set_vendor_contact("cat-nowhere", "a@b.test", None, "email")


@pytest.mark.parametrize("source", ["", "web", "OSM", "google", "gplaces ", "osm; drop"])
def test_a_discovered_business_comes_only_from_a_known_source(em, source):
    with pytest.raises(ValueError, match="OpenStreetMap or Google Places"):
        em.add_discovered("osm:node/1001", "Sharma Caterers", "Pune", C.CATERER, source)
    assert em.add_discovered("osm:node/1001", "Sharma Caterers", "Pune", C.CATERER, "osm").source == "osm"


def test_a_test_vendor_has_no_website_to_set_or_read_and_an_unknown_business_is_refused(em):
    test, _contact = em.add_test_vendor("Test Caterer", "Pune", C.CATERER, "owner@example.test", True, "Manish")
    for call in (lambda: em.set_business_website(test.vendor_id, "https://x.example/"), lambda: em.find_business_contacts(test.vendor_id)):
        with pytest.raises(ValueError, match="test vendor"):
            call()
    with pytest.raises(ValueError, match="unknown discovered business"):
        em.find_business_contacts("ext_nope")
    with pytest.raises(ValueError, match="unknown discovered business"):
        em.find_business_contacts("cat-pune-annapurna")  # a seed vendor is not a discovered business


def test_an_address_can_be_decided_only_once(em):
    biz = discovered(em)
    em.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": "a@rasoi-pune.example", "source_url": SRC, "source_kind": "page_text", "flags": ()}])
    cand = em.shortlist.candidates(biz.vendor_id)[0]
    em.reject_contact(biz.vendor_id, cand.candidate_id)
    with pytest.raises(ValueError, match="already rejected"):
        em.reject_contact(biz.vendor_id, cand.candidate_id)
    with pytest.raises(ValueError, match="already rejected"):
        em.confirm_contact(biz.vendor_id, cand.candidate_id, "Asha")
    with pytest.raises(ValueError, match="unknown business or contact candidate"):
        em.reject_contact(biz.vendor_id, "nope")
