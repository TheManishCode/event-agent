"""Email flow, local first (ADR-047): the real EmailChannel against a fake SMTP server and a fake IMAP mailbox. No network, no credentials.

brief -> plan -> job -> approved requests -> vendor replies (plain, Hinglish, ambiguous) -> claims -> user confirms -> digest -> the plan is
unchanged until the user presses the existing re-price action.
"""

from datetime import datetime, timedelta, timezone

import pytest

from eventmate.messaging.conversations import SendPolicy
from eventmate.schemas import Disruption, DisruptionKind, VendorCategory as C
from eventmate.service import EventMate
from tests.conftest import TODAY
from tests.mailfake import MailWorld, disconnected
from tests.test_conversations import Clock
from tests.test_jobs_worker import NoWhatsApp
from eventmate.followthrough.worker import digest

T0 = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)  # 14:30 IST: inside the sending window
COLLEGE = "Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering."
ADDR = {"cat-pune-annapurna": "a@annapurna.test", "cat-pune-rasoi": "r@rasoi.test", "cat-pune-spice": "s@spice.test"}
VENDORS = {vid: ("email", to) for vid, to in ADDR.items()}


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def world(clock):
    return MailWorld(clock)


@pytest.fixture
def em(tmp_path, world, clock):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="mail", channels={"email": world.channel(), "whatsapp": NoWhatsApp()}, require_symbolic=False)
    e.outreach_store.now = e.conversations.now = e.jobs_clock = clock
    e.conversations.policy = SendPolicy(per_vendor_per_day=10, per_day=100, whatsapp_gap_s=0)
    return e


@pytest.fixture
def pid(em):
    out = em.plan(COLLEGE, today=TODAY)
    assert out.plan is not None
    return out.plan.plan_id


def start(em, pid, clock, **kw):
    args = dict(goal="2 caterer quotes", deadline=clock() + timedelta(days=7), sender_name="Asha", min_quotes=2)
    args.update(kw)
    return em.jobs.start_job(pid, C.CATERER, VENDORS, **args)


def started(em, pid, clock, **kw):
    job = start(em, pid, clock, **kw)
    em.jobs.approve_requests(job.job_id)
    return job


def tick(em, clock, minutes=20):
    clock.advance(minutes=minutes)
    return em.jobs.tick()


def nudges(world):
    return [m for m in world.outbox if "ust checking" in m.get_content()]


# ---- the whole flow

def test_brief_to_confirmed_quote_to_a_manual_reprice(em, world, pid, clock):
    v1 = em.store.latest(pid)
    job = start(em, pid, clock)
    assert world.outbox == [] and em.jobs.store.get(job.job_id).status == "waiting_user"  # drafts only: nothing is sent before approval
    em.jobs.approve_requests(job.job_id)
    assert sorted(m["To"] for m in world.outbox) == sorted(ADDR.values())
    for m in world.outbox:  # real MIME, built by the real channel
        body = m.get_content()
        assert m["From"] == "agent@fake.test" and m["Message-ID"].startswith("<") and "Quote request" in m["Subject"]
        assert "250,000" not in body and "250000" not in body and "udget" not in body  # the budget never reaches a vendor
    world.reply("r@rasoi.test", "Hello,\nTotal INR 78,000 for the event. Valid for 10 days.\n\nOn Fri, 30 Oct 2026, Asha wrote:\n> Hello Royal Rasoi,\n> Rs 999 per plate please")
    world.reply("a@annapurna.test", "Sir hamara rate Rs 650 per plate hai, taxes alag se.")
    world.reply("s@spice.test", "Price is Rs 40,000.")  # no unit: ambiguous
    tick(em, clock)
    j = em.jobs.store.get(job.job_id)
    by = {c.vendor_id: c for c in j.claims}
    assert j.status == "waiting_user" and set(by) == {"cat-pune-rasoi", "cat-pune-annapurna"}
    assert (by["cat-pune-rasoi"].amount_inr, by["cat-pune-rasoi"].basis, by["cat-pune-rasoi"].stated_valid_days) == (78_000, "total", 10)
    assert (by["cat-pune-annapurna"].amount_inr, by["cat-pune-annapurna"].basis) == (650, "per_plate")
    assert any(i.kind == "unclear_price" and "cat-pune-spice" in i.text for i in j.open_items())  # the ambiguous one goes to the user
    assert all(em.observations.quotes(v) == [] for v in ADDR)  # a reply is not a quote
    for c in j.claims:
        em.jobs.confirm_claim(job.job_id, c.claim_id, valid_days=10)
    final = em.jobs.store.get(job.job_id)
    assert final.status == "waiting_user" or final.status == "active"
    em.jobs.tick()
    final = em.jobs.store.get(job.job_id)
    assert final.status == "done" and len(final.confirmed()) == 2
    q = em.observations.quotes("cat-pune-rasoi")[0]
    assert q.amount_inr == 78_000 and q.entered_by == "user_confirmed" and q.evidence_ref and "10 days" in q.note
    assert "Confirmed" in digest(final)
    # nothing about the plan has changed: a confirmed quote is not a plan change
    assert em.store.latest(pid) == v1 and len(em.store.versions(pid)) == 1 and em.store.latest(pid).total_cost == v1.total_cost
    stale = [x for x in em.quote_status(pid) if x.price_stale]
    assert [x.vendor_id for x in stale] == ["cat-pune-rasoi"] and stale[0].plan_cost > stale[0].current_price == 78_000
    # the user presses the existing Re-price action (the same replan the UI button sends)
    out = em.replan(pid, Disruption(kind=DisruptionKind.BUDGET_CHANGE, new_budget=v1.request.budget_total), today=TODAY)
    assert out.status == "replanned" and out.plan.version == 2 and out.plan.selection_for(C.CATERER).cost == 78_000
    assert out.plan.total_cost < v1.total_cost and out.plan.validation.passed and not [x for x in em.quote_status(pid) if x.price_stale]


# ---- replies

def test_a_reply_that_arrives_twice_is_stored_and_claimed_once(em, world, pid, clock):
    job = started(em, pid, clock, min_quotes=1)
    world.reply("r@rasoi.test", "Total INR 66,000 for the event.", message_id="<same@vendor.test>")
    world.reply("r@rasoi.test", "Total INR 66,000 for the event.", message_id="<same@vendor.test>")
    tick(em, clock)
    tick(em, clock)
    assert len(em.outreach_store.inbound(pid)) == 1 and len(em.jobs.store.get(job.job_id).claims) == 1


def test_a_stranger_who_copies_the_thread_headers_is_flagged_and_gets_no_claim(em, world, pid, clock):
    job = started(em, pid, clock, min_quotes=1)
    world.reply("r@rasoi.test", "Total INR 1 for the event.", sender="someone@elsewhere.test")  # right thread headers, wrong address
    tick(em, clock)
    stored = em.outreach_store.inbound(pid)
    j = em.jobs.store.get(job.job_id)
    assert len(stored) == 1 and stored[0].sender_differs and j.claims == ()
    assert any(i.kind == "different_sender" and "someone@elsewhere.test" in i.text for i in j.open_items())


def test_a_stranger_without_the_thread_headers_is_ignored_entirely(em, world, pid, clock):
    job = started(em, pid, clock, min_quotes=1)
    world.reply("r@rasoi.test", "Win a prize, send your account number", sender="spam@evil.test", thread=False)
    tick(em, clock)
    assert em.outreach_store.inbound(pid) == [] and em.jobs.store.get(job.job_id).claims == ()


def test_a_different_sender_never_gets_an_automatic_answer(em, world, pid, clock):
    em.grant_autoreply(pid, "Asha", hours=72)
    started(em, pid, clock)
    before = len(world.outbox)
    world.reply("r@rasoi.test", "Hello, we can do it.", sender="someone@elsewhere.test")
    tick(em, clock)
    assert len(world.outbox) == before


def test_quoted_history_is_cut_and_never_makes_a_claim(em, world, pid, clock):
    job = started(em, pid, clock, min_quotes=1)
    world.reply("r@rasoi.test", "Rs 650 per plate.\n\nOn Fri, 30 Oct 2026, Asha wrote:\n> Is Rs 999 per plate fine?\n> Total INR 500,000?")
    tick(em, clock)
    stored = em.outreach_store.inbound(pid)[0]
    assert stored.body == "Rs 650 per plate." and "999" not in stored.body
    assert [(c.amount_inr, c.basis) for c in em.jobs.store.get(job.job_id).claims] == [(650, "per_plate")]


def test_replies_from_robots_and_lists_are_skipped(em, world, pid, clock):
    job = started(em, pid, clock, min_quotes=1)
    world.reply("r@rasoi.test", "Out of office until Monday. Rs 1 per plate.", extra={"Auto-Submitted": "auto-replied"})
    tick(em, clock)
    assert em.outreach_store.inbound(pid) == [] and em.jobs.store.get(job.job_id).claims == ()


# ---- sending

def test_an_unknown_send_outcome_blocks_the_job_is_never_resent_and_the_digest_says_so(em, world, pid, clock):
    world.send_error = disconnected()
    job = start(em, pid, clock)
    em.jobs.approve_requests(job.job_id)
    world.send_error = None
    tick(em, clock)
    tick(em, clock)
    j = em.jobs.store.get(job.job_id)
    assert world.outbox == [] and j.status == "blocked"  # never resent: the message may have gone out
    assert "check the vendor chat" in digest(j).lower()
    assert {d.status for d in em.outreach(pid)} == {"outcome_unknown"}


def test_a_login_failure_is_a_clear_failure_without_leaking_the_password(em, world, pid, clock):
    import smtplib

    world.login_error = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    job = start(em, pid, clock)
    em.jobs.approve_requests(job.job_id)
    drafts = em.outreach(pid)
    assert world.outbox == [] and {d.status for d in drafts} == {"failed"} and all("rejected the login" in d.error for d in drafts)
    blob = " ".join(d.error for d in drafts) + digest(em.jobs.store.get(job.job_id)) + " ".join(str(x) for x in em.jobs.tick().values())
    assert "not-a-real-password" not in blob


# ---- automatic nudges stay inside the standing permission

def silent(em, world, pid, clock, **grant):
    em.grant_autoreply(pid, "Asha", hours=168, **grant)
    job = started(em, pid, clock, min_quotes=3)
    em.jobs.tick()
    return job


def test_the_kill_switch_stops_the_nudges_and_hands_them_to_the_user(em, world, pid, clock):
    job = silent(em, world, pid, clock)
    em.kill_all_autoreplies()
    clock.advance(hours=25)
    em.jobs.tick()
    assert nudges(world) == []
    assert any("kill switch" in i.text for i in em.jobs.store.get(job.job_id).open_items() if i.kind == "approve_nudge")


def test_outside_the_ist_window_a_nudge_waits_and_then_goes(em, world, pid, clock):
    silent(em, world, pid, clock)
    clock.t = T0.replace(hour=18) + timedelta(days=1)  # 23:30 IST
    em.jobs.tick()
    assert nudges(world) == [] and all(d.status == "approved" for d in em.outreach(pid) if d.kind == "followup")
    clock.t = T0.replace(hour=4) + timedelta(days=2)  # 09:30 IST
    em.jobs.tick()
    assert len(nudges(world)) == 3 and all("automated assistant" in m.get_content() for m in nudges(world))


def test_when_the_cap_is_reached_the_rest_go_to_the_user(em, world, pid, clock):
    job = silent(em, world, pid, clock, max_total=1)
    clock.advance(hours=25)
    em.jobs.tick()
    assert len(nudges(world)) == 1
    items = [i for i in em.jobs.store.get(job.job_id).open_items() if i.kind == "approve_nudge"]
    assert len(items) == 2 and all("limit of 1" in i.text for i in items)


# ---- regression: a reply that the Mandate answered must not also raise a job item

def test_a_reply_the_mandate_already_answered_raises_no_item_for_the_user(em, world, pid, clock):
    em.grant_autoreply(pid, "Asha", hours=72)
    job = started(em, pid, clock)
    before = len(world.outbox)
    world.reply("r@rasoi.test", "Hello, yes we can do it.")  # no price, no question: the Mandate asks for the price
    tick(em, clock)
    j = em.jobs.store.get(job.job_id)
    assert len(world.outbox) == before + 1 and "total price" in world.last_to("r@rasoi.test").get_content()
    assert [i for i in j.open_items() if i.kind in ("vendor_question", "unclear_price") and i.vendor_id == "cat-pune-rasoi"] == []


def test_a_question_is_left_to_the_user_even_when_a_later_reply_arrives_in_the_same_round(em, world, pid, clock):
    em.grant_autoreply(pid, "Asha", hours=72)
    job = started(em, pid, clock)
    before = len(world.outbox)
    world.reply("r@rasoi.test", "Is it for children too? Which date exactly?", minutes=5)
    world.reply("r@rasoi.test", "Ok noted, thanks.", minutes=9)
    tick(em, clock)
    j = em.jobs.store.get(job.job_id)
    assert len(world.outbox) == before  # the Mandate does not answer past a question it was not meant to answer
    questions = [i for i in j.open_items() if i.kind == "vendor_question"]
    assert len(questions) == 1 and "children" in questions[0].text
    tick(em, clock)
    assert len([i for i in em.jobs.store.get(job.job_id).open_items() if i.kind == "vendor_question"]) == 1  # not raised again


def test_without_a_permission_the_worker_still_raises_the_item(em, world, pid, clock):
    job = started(em, pid, clock)
    world.reply("r@rasoi.test", "Hello, yes we can do it.")
    tick(em, clock)
    assert any(i.kind == "unclear_price" and i.vendor_id == "cat-pune-rasoi" for i in em.jobs.store.get(job.job_id).open_items())


def test_a_denied_automatic_answer_frees_the_user_item(em, world, pid, clock):
    em.grant_autoreply(pid, "Asha", hours=72)
    job = started(em, pid, clock)
    clock.t = T0.replace(hour=18)  # 23:30 IST: the automatic answer is queued, not sent
    mid = world.reply("r@rasoi.test", "Hello, yes we can do it.")
    em.jobs.tick()
    queued = [d for d in em.outreach(pid) if d.kind == "followup"]
    assert queued and queued[0].approved_by == "mandate" and queued[0].status == "approved"
    em.kill_all_autoreplies()
    tick(em, clock)  # the next round: the queued answer is denied, and the reply is the user's again
    assert queued and em.outreach_store.get(pid, queued[0].draft_id).status == "denied" and mid
    assert any(i.kind == "unclear_price" and i.vendor_id == "cat-pune-rasoi" for i in em.jobs.store.get(job.job_id).open_items())
