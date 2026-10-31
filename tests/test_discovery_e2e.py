"""Discovered vendor -> source-backed contact -> reviewed quote job, end to end (ADR-049).

Fake search, fake page fetcher and the real EmailChannel against a fake SMTP server and fake IMAP mailbox. No live send, no live fetch.
"""

from datetime import timedelta

import pytest

from eventmate.integrations.places import PlacesSource
from eventmate.messaging.conversations import SendPolicy
from eventmate.schemas import Disruption, DisruptionKind, VendorCategory as C
from eventmate.service import EventMate
from tests.conftest import TODAY
from tests.mailfake import MailWorld
from tests.test_conversations import Clock
from tests.test_jobs_worker import NoWhatsApp
from tests.test_places import KEY, Net, place
from tests.test_shortlist import FakeFetcher

COLLEGE = "Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering."
SITE_A, SITE_B = "https://sharma-caterers.example/", "https://kapoor-food.example/"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(clock):
    return MailWorld(clock)


@pytest.fixture
def em(tmp_path, world, clock):
    net = Net([
        place("Sharma Caterers") | {"id": "ChIJsharma000001", "websiteUri": SITE_A},
        place("Kapoor Food Services", uri="https://maps.google.com/?cid=2") | {"id": "ChIJkapoor000002", "websiteUri": SITE_B},
    ])
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="de2e", require_symbolic=False, places=PlacesSource(api_key=KEY, transport=net),
                  channels={"email": world.channel(), "whatsapp": NoWhatsApp()})
    e.outreach_store.now = e.conversations.now = e.jobs_clock = e.suppressions.now = clock
    e.conversations.policy = SendPolicy(per_vendor_per_day=20, per_day=200, whatsapp_gap_s=0)
    e.contact_fetcher = lambda: FakeFetcher({
        SITE_A: "<html><body><a href='/contact'>Contact</a><p>Welcome. hello@sharma-caterers.example</p></body></html>",
        SITE_A + "contact": "<html><body><a href='mailto:orders@sharma-caterers.example'>Orders</a></body></html>",
        SITE_B: "<html><body><p>Reach us: kapoor.food@gmail.com</p></body></html>",
    })
    return e


@pytest.fixture
def pid(em):
    return em.plan(COLLEGE, today=TODAY).plan.plan_id


def shortlisted(em, pid):
    em.search_vendors(pid, C.CATERER, "places")
    return {i.business.name: i for i in em.shortlist_view(pid)}


def candidate(em, vid, prefix):
    em.find_business_contacts(vid)
    return next(c for c in em.shortlist.candidates(vid) if c.value.startswith(prefix))


def tick(em, clock, minutes=20):
    clock.advance(minutes=minutes)
    return em.jobs.tick()


def test_the_whole_flow_from_search_to_a_confirmed_quote_and_a_manual_reprice(em, world, pid, clock):
    v1 = em.store.latest(pid)
    items = shortlisted(em, pid)
    assert set(items) == {"Sharma Caterers", "Kapoor Food Services"} and all(i.business.origin == "discovered" and i.contact is None for i in items.values())
    sharma, kapoor = items["Sharma Caterers"].business.vendor_id, items["Kapoor Food Services"].business.vendor_id
    assert world.outbox == [] and all(em.repo.get(C.CATERER, v) is None for v in (sharma, kapoor))

    cand = candidate(em, sharma, "orders")
    assert cand.source_kind == "page_mailto" and cand.source_url == SITE_A + "contact" and cand.status == "found" and em.outreach_store.contacts() == {}
    with pytest.raises(ValueError, match="no saved contact"):  # nothing confirmed yet: no job
        em.jobs.start_job(pid, C.CATERER, [sharma], "1 caterer quote", clock() + timedelta(days=5), "Asha", min_quotes=1)

    # the user reviews the address and corrects it: the human edit replaces the extracted one and is recorded
    contact = em.confirm_contact(sharma, cand.candidate_id, "Asha", edited_value="events@sharma-caterers.example")
    assert contact.edited_by_user and contact.email == "events@sharma-caterers.example" and contact.source_url == SITE_A + "contact" and contact.confirmed_by == "Asha"
    assert next(i for i in em.shortlist_view(pid) if i.business.vendor_id == sharma).contact is not None and next(i for i in em.shortlist_view(pid) if i.business.vendor_id == kapoor).contact is None

    with pytest.raises(ValueError, match="no saved contact"):  # the second business was never confirmed
        em.jobs.start_job(pid, C.CATERER, [sharma, kapoor], "2 caterer quotes", clock() + timedelta(days=5), "Asha", min_quotes=2)
    job = em.jobs.start_job(pid, C.CATERER, [sharma], "1 caterer quote", clock() + timedelta(days=5), "Asha", min_quotes=1)
    draft = em.outreach(pid)[0]
    assert world.outbox == [] and draft.status == "prepared" and draft.recipient == "events@sharma-caterers.example" and draft.vendor_origin == "discovered"
    assert "250,000" not in draft.body and "udget" not in draft.body

    em.jobs.approve_requests(job.job_id)  # the exact-text approval is unchanged
    assert [m["To"] for m in world.outbox] == ["events@sharma-caterers.example"]
    world.reply("events@sharma-caterers.example", "Hello,\nTotal INR 80,000 for the event. Valid for 10 days.\n\nOn Fri Asha wrote:\n> budget?")
    tick(em, clock)
    j = em.jobs.store.get(job.job_id)
    assert j.claims[0].amount_inr == 80_000 and em.observations.quotes(sharma) == []  # a reply is not a quote
    em.jobs.confirm_claim(job.job_id, j.claims[0].claim_id, valid_days=10)
    q = em.observations.quotes(sharma)[0]
    assert q.vendor_id == sharma and q.amount_inr == 80_000 and q.entered_by == "user_confirmed" and q.evidence_ref
    em.jobs.tick()
    assert em.jobs.store.get(job.job_id).status == "done"
    text = em.digest_store.list(pid)[0].text
    assert "Sharma Caterers" in text and "ext_" not in text and "250,000" not in text

    assert em.store.latest(pid) == v1 and len(em.store.versions(pid)) == 1  # nothing about the plan changed
    out = em.replan(pid, Disruption(kind=DisruptionKind.BUDGET_CHANGE, new_budget=v1.request.budget_total), today=TODAY)  # the existing Re-price action
    assert out.status == "replanned" and out.plan.total_cost == v1.total_cost  # a discovered business is not a plan vendor, so its quote cannot reprice it
    assert [s.vendor_id for s in out.plan.selections] == [s.vendor_id for s in v1.selections]
    assert em.repo.get(C.CATERER, sharma) is None  # still not a vendor record


def test_an_extracted_address_alone_never_starts_a_job_or_a_send(em, world, pid, clock):
    items = shortlisted(em, pid)
    vid = items["Kapoor Food Services"].business.vendor_id
    cand = candidate(em, vid, "kapoor.food")
    assert "generic mailbox, confirm it is the business's" in cand.flags
    with pytest.raises(ValueError):
        em.jobs.start_job(pid, C.CATERER, [vid], "x", clock() + timedelta(days=5), "Asha", min_quotes=1)
    with pytest.raises(ValueError, match="confirmed"):
        em.draft_outreach(pid, C.CATERER, "email", "kapoor.food@gmail.com", "Asha", vendor_id=vid)
    em.conversations.dispatch()
    em.jobs.tick()
    assert world.outbox == []


def test_a_job_started_from_a_confirmed_address_that_is_then_suppressed_never_sends(em, world, pid, clock):
    vid = shortlisted(em, pid)["Sharma Caterers"].business.vendor_id
    em.confirm_contact(vid, candidate(em, vid, "orders").candidate_id, "Asha")
    job = em.jobs.start_job(pid, C.CATERER, [vid], "1 caterer quote", clock() + timedelta(days=5), "Asha", min_quotes=1)
    em.suppress_address("email", "orders@sharma-caterers.example", "Asha", "asked us not to write")  # between drafting and approving
    em.jobs.approve_requests(job.job_id)  # approving does not send a blocked draft
    assert world.outbox == [] and em.outreach(pid)[0].status == "blocked"
    tick(em, clock)
    j = em.jobs.store.get(job.job_id)
    assert any(i.kind == "do_not_contact" for i in j.open_items()) and "blocked, do not contact" in " ".join(i.text for i in j.open_items())


def test_the_kill_switch_still_stops_automatic_nudges_to_a_discovered_business(em, world, pid, clock):
    em.grant_autoreply(pid, "Asha", hours=168)
    vid = shortlisted(em, pid)["Sharma Caterers"].business.vendor_id
    em.confirm_contact(vid, candidate(em, vid, "orders").candidate_id, "Asha")
    job = em.jobs.start_job(pid, C.CATERER, [vid], "1 caterer quote", clock() + timedelta(days=7), "Asha", min_quotes=1)
    em.jobs.approve_requests(job.job_id)
    em.jobs.tick()
    em.kill_all_autoreplies()
    clock.advance(hours=25)
    em.jobs.tick()
    assert len(world.outbox) == 1 and any(i.kind == "approve_nudge" and "kill switch" in i.text for i in em.jobs.store.get(job.job_id).open_items())


def test_a_stop_reply_from_a_discovered_business_ends_contact_on_every_job(em, world, pid, clock):
    vid = shortlisted(em, pid)["Sharma Caterers"].business.vendor_id
    em.confirm_contact(vid, candidate(em, vid, "orders").candidate_id, "Asha")
    job = em.jobs.start_job(pid, C.CATERER, [vid], "1 caterer quote", clock() + timedelta(days=7), "Asha", min_quotes=1)
    em.jobs.approve_requests(job.job_id)
    world.reply("orders@sharma-caterers.example", "Please stop messaging us.")
    tick(em, clock)
    assert em.is_suppressed("email", "orders@sharma-caterers.example").reason == "stop_request"
    other_plan = em.plan(COLLEGE.replace("150", "90"), today=TODAY).plan.plan_id
    with pytest.raises(ValueError, match="do not contact"):
        em.jobs.start_job(other_plan, C.CATERER, [vid], "another", clock() + timedelta(days=7), "Asha", min_quotes=1)
    assert len(world.outbox) == 1
