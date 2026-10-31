"""The do-not-contact list, stop requests and bounces (ADR-049).

A suppression is about an address on a channel. It is added only by a matched stop request, a correlated permanent bounce, or the user; it stops drafts that
were already queued; and only the user lifts it.
"""

import json
from datetime import timedelta

import pytest

from eventmate.messaging.channels import Inbound
from eventmate.messaging.conversations import SendPolicy
from eventmate.schemas import Suppression, VendorCategory as C
from eventmate.service import EventMate
from eventmate.sourcing.suppression import NeedsAcknowledgement, domain_note, normalise_address
from tests.conftest import TODAY
from tests.mailfake import MailWorld
from tests.test_conversations import Clock, FakeChannel
from tests.test_jobs_worker import NoWhatsApp
from eventmate.web.app import create_app

COLLEGE = "Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering."
SRC = "https://sharma-caterers.example/contact"
ADDR = "sales@sharma-caterers.example"


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def chans():
    return {"email": FakeChannel("email"), "whatsapp": FakeChannel("whatsapp", False, False)}


@pytest.fixture
def em(tmp_path, chans, clock):
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="sup", channels=chans, require_symbolic=False)
    e.outreach_store.now = e.conversations.now = e.jobs_clock = e.suppressions.now = clock
    e.conversations.policy = SendPolicy(per_vendor_per_day=20, per_day=200, whatsapp_gap_s=0)
    return e


@pytest.fixture
def pid(em):
    return em.plan(COLLEGE, today=TODAY).plan.plan_id


def business(em, ref="osm:node/1001", name="Sharma Caterers", value=ADDR, by="Asha"):
    biz = em.add_discovered(ref, name, "Pune", C.CATERER, "osm")
    cand = em.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": value, "source_url": SRC, "source_kind": "page_mailto", "flags": ()}])[0]
    em.confirm_contact(biz.vendor_id, cand.candidate_id, by)
    return biz


def request(em, pid, biz, address=ADDR):
    d = em.draft_outreach(pid, C.CATERER, "email", address, "Asha", vendor_id=biz.vendor_id)
    return em.approve_outreach(pid, d.draft_id, d.content_hash)


def says(chans, sender, body, ext, clock, minutes=5, **kw):
    chans["email"].inbox.append(Inbound(channel="email", sender=sender, external_id=ext, received_at=clock() + timedelta(minutes=minutes), body=body, **kw))


# ---- normalising an address

@pytest.mark.parametrize("channel,raw,want", [
    ("email", " Sales@Shop.Example ", "sales@shop.example"),
    ("email", "J.Doe+promo@gmail.com", "jdoe@gmail.com"),
    ("email", "jdoe@googlemail.com", "jdoe@gmail.com"),
    ("email", "a.b+x@outlook.com", "a.b@outlook.com"),  # dots matter at Outlook, the tag does not
    ("email", "A.B+x@Shop.Example", "a.b+x@shop.example"),  # an unknown provider: compared exactly
    ("email", "x+1@icloud.com", "x@icloud.com"),
    ("whatsapp", "+91 98765-43210", "919876543210"),
])
def test_addresses_are_normalised_only_where_the_provider_is_known_to_ignore_the_difference(channel, raw, want):
    assert normalise_address(channel, raw) == want


@pytest.mark.parametrize("channel,raw", [("email", "nope"), ("email", "a@b.example,c@d.example"), ("email", "a@b.example\nBcc: x@y.example"), ("whatsapp", "123"), ("sms", "a@b.example"), ("email", "")])
def test_an_invalid_address_is_refused_not_normalised(channel, raw):
    with pytest.raises(ValueError):
        normalise_address(channel, raw)


def test_the_note_says_what_a_suppression_does_not_cover():
    assert "other addresses at shop.example are not blocked" in domain_note("email", "A@Shop.Example") and "this number only" in domain_note("whatsapp", "919876543210")


# ---- a matched stop request

@pytest.mark.parametrize("text", ["Please stop messaging me.", "STOP", "wrong number", "I am not interested, remove my number", "Do not contact me again.", "unsubscribe"])
def test_a_stop_request_in_a_matched_reply_suppresses_the_address(em, pid, chans, clock, text):
    biz = business(em)
    sent = request(em, pid, biz)
    says(chans, ADDR, text, "<r1>", clock, in_reply_to=sent.external_id)
    em.poll_conversations()
    s = em.suppressions.active_for("email", ADDR)
    assert s is not None and s.reason == "stop_request" and s.created_by.startswith("system") and s.source_ref == em.outreach_store.inbound(pid)[0].message_id and s.active
    assert em.unmatched_messages() == []  # a matched message is in the vendor chat, not in the unmatched list


def test_after_a_stop_request_no_draft_can_be_written_or_approved(em, pid, chans, clock):
    biz = business(em)
    sent = request(em, pid, biz)
    pending = em.followup_outreach(pid, sent.draft_id, "Could you share the price?", "Asha")  # prepared before the stop arrived
    says(chans, ADDR, "Please stop messaging me.", "<r1>", clock, in_reply_to=sent.external_id)
    em.poll_conversations()
    assert em.outreach_store.get(pid, pending.draft_id).status == "blocked" and "ask" in em.outreach_store.get(pid, pending.draft_id).error.lower()
    n = len(chans["email"].sent)
    with pytest.raises(ValueError, match="do not contact"):
        em.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id)
    with pytest.raises(ValueError):
        em.approve_outreach(pid, pending.draft_id, pending.content_hash)
    assert len(chans["email"].sent) == n


def test_a_message_that_only_mentions_stopping_adds_nothing(em, pid, chans, clock):
    biz = business(em)
    sent = request(em, pid, biz)
    for i, text in enumerate(["We will not stop the music at 10pm, is that fine?", "Our menu does not include anything to stop hunger", "Thanks, Rs 650 per plate."]):
        says(chans, ADDR, text, f"<m{i}>", clock, minutes=5 + i, in_reply_to=sent.external_id)
    em.poll_conversations()
    assert em.suppressions.active_for("email", ADDR) is None


def test_a_stop_from_an_address_we_never_wrote_to_adds_nothing_and_is_shown_for_review(em, pid, chans, clock):
    biz = business(em)
    request(em, pid, biz)
    says(chans, "stranger@elsewhere.example", "Please stop messaging me and remove my number", "<x1>", clock)  # no thread headers, not an address we wrote to
    says(chans, "stranger2@elsewhere.example", "Win a prize", "<x2>", clock, minutes=6)  # unrelated mail: not even shown
    em.poll_conversations()
    assert em.suppressions.active_for("email", "stranger@elsewhere.example") is None and em.suppressions.active_for("email", ADDR) is None
    shown = em.unmatched_messages()
    assert [(m.sender, m.kind) for m in shown] == [("stranger@elsewhere.example", "stop_like")]
    draft = em.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id)  # the real contact is still reachable
    assert draft.status == "prepared"


def test_a_forged_stop_that_copies_the_thread_headers_from_another_address_adds_nothing_but_is_flagged(em, pid, chans, clock):
    biz = business(em)
    sent = request(em, pid, biz)
    says(chans, "someone@elsewhere.example", "Stop messaging this business please", "<f1>", clock, in_reply_to=sent.external_id)
    em.poll_conversations()
    assert em.suppressions.active_for("email", ADDR) is None and em.suppressions.active_for("email", "someone@elsewhere.example") is None
    assert em.outreach_store.inbound(pid)[0].sender_differs and [m.kind for m in em.unmatched_messages()] == ["stop_like"]


def test_the_same_unmatched_message_is_listed_once(em, pid, chans, clock):
    request(em, pid, business(em))
    says(chans, "stranger@elsewhere.example", "stop", "<x1>", clock)
    for _ in range(3):
        em.poll_conversations()
    assert len(em.unmatched_messages()) == 1


def test_a_plus_tagged_reply_from_the_same_gmail_mailbox_still_counts_as_the_address_we_wrote_to(em, pid, chans, clock):
    biz = business(em, ref="osm:node/2002", value="owner@gmail.com")
    sent = request(em, pid, biz, "owner@gmail.com")
    says(chans, "O.wner+shop@gmail.com", "stop", "<g1>", clock, in_reply_to=sent.external_id)
    em.poll_conversations()
    assert em.outreach_store.inbound(pid)[0].sender_differs is False and em.suppressions.active_for("email", "owner@gmail.com") is not None


# ---- it covers every job, plan, record and alias, and stops queued drafts

def test_one_suppression_covers_every_business_record_plan_and_alias_of_the_same_address(em, pid, clock):
    a = business(em, ref="osm:node/1")
    b = business(em, ref="osm:node/2", name="Another Listing Of The Same Business")
    em.set_vendor_contact("cat-pune-rasoi", ADDR, None, "email")
    other_plan = em.plan(COLLEGE.replace("150", "90"), today=TODAY).plan.plan_id
    ready = [em.draft_outreach(p, C.CATERER, "email", ADDR, "Asha", vendor_id=v) for p, v in ((pid, a.vendor_id), (other_plan, b.vendor_id), (pid, "cat-pune-rasoi"))]
    em.suppress_address("email", "SALES@Sharma-Caterers.example", "Asha", "asked on the phone")
    assert [em.outreach_store.get(d.plan_id, d.draft_id).status for d in ready] == ["blocked"] * 3
    for p, v in ((pid, a.vendor_id), (other_plan, b.vendor_id), (pid, "cat-pune-rasoi")):
        with pytest.raises(ValueError, match="do not contact"):
            em.draft_outreach(p, C.CATERER, "email", ADDR, "Asha", vendor_id=v)


def test_a_draft_that_was_already_queued_and_approved_is_blocked_not_sent(em, pid, chans, clock):
    biz = business(em)
    d = em.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id)
    em.outreach_store.transition(pid, d.draft_id, ("prepared",), status="approved", decided_at=clock())  # queued, waiting for the next dispatch
    em.suppress_address("email", ADDR, "Asha", "asked on the phone")
    assert em.outreach_store.get(pid, d.draft_id).status == "blocked" and "blocked" in em.outreach_store.get(pid, d.draft_id).status
    em.conversations.dispatch()
    assert chans["email"].sent == [] and em.outreach_store.get(pid, d.draft_id).status == "blocked" and em.outreach_store.get(pid, d.draft_id).sent_at is None


def test_the_check_at_send_time_stands_on_its_own_even_if_nothing_blocked_the_queue(em, pid, chans, clock):
    biz = business(em)
    d = em.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id)
    em.outreach_store.transition(pid, d.draft_id, ("prepared",), status="approved", decided_at=clock())
    em.suppressions.add("email", ADDR, "user", "added straight to the list", "Asha")  # no apply_block: only the dispatch-time check can stop it
    em.conversations.dispatch()
    assert chans["email"].sent == [] and em.outreach_store.get(pid, d.draft_id).status == "blocked"


def test_a_plus_tagged_gmail_variant_is_blocked_but_an_unknown_provider_variant_is_not(em, pid, clock):
    em.suppress_address("email", "owner@gmail.com", "Asha")
    em.suppress_address("email", "desk@shop.example", "Asha")
    biz = em.add_discovered("osm:node/3", "Some Caterer", "Pune", C.CATERER, "osm")
    for addr in ("o.w.n.e.r+promo@gmail.com", "owner@googlemail.com"):
        assert em.is_suppressed("email", addr) is not None
    assert em.is_suppressed("email", "desk+bookings@shop.example") is None and em.is_suppressed("email", "other@shop.example") is None  # exact match elsewhere
    assert biz  # (the domain-level display says what is not covered)
    assert "other addresses at shop.example are not blocked" in dict((s.value, n) for s, n in em.suppression_list())["desk@shop.example"]


def test_an_automatic_nudge_is_never_queued_for_a_suppressed_address(em, pid, chans, clock):
    em.grant_autoreply(pid, "Asha", hours=72)
    biz = business(em)
    sent = request(em, pid, biz)
    em.suppress_address("email", ADDR, "Asha")
    d, why = em.conversations.queue_automatic(pid, sent.draft_id, "Could you share the price?", "nudge:x")
    assert d is None and "do not contact" in why


# ---- the user's own actions

def test_the_user_can_add_and_lift_and_the_lift_is_recorded(em, pid):
    s = em.suppress_address("email", "x@shop.example", "Asha", "asked by phone")
    assert s.reason == "user" and s.created_by == "Asha" and s.source_ref == "asked by phone" and s.active
    assert em.suppress_address("email", "X@Shop.example", "Asha").suppression_id == s.suppression_id  # one entry per address
    for who, why in (("", "mistake"), ("Asha", ""), ("  ", " ")):
        with pytest.raises(ValueError):
            em.lift_suppression(s.suppression_id, who, why)
    assert em.is_suppressed("email", "x@shop.example") is not None
    lifted = em.lift_suppression(s.suppression_id, "Asha", "they emailed to say it is fine now")
    assert not lifted.active and lifted.lifted_by == "Asha" and lifted.lift_reason.startswith("they emailed") and lifted.lifted_at
    assert em.is_suppressed("email", "x@shop.example") is None
    with pytest.raises(ValueError):
        em.lift_suppression("nope", "Asha", "x")
    with pytest.raises(ValueError):
        em.suppress_address("email", "x@shop.example", "", "x")


def test_a_lift_does_not_resurrect_drafts_that_were_blocked_and_a_new_draft_works(em, pid):
    biz = business(em)
    d = em.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id)
    s = em.suppress_address("email", ADDR, "Asha")
    em.lift_suppression(s.suppression_id, "Asha", "mistake")
    assert em.outreach_store.get(pid, d.draft_id).status == "blocked"
    assert em.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id).status == "prepared"


def test_the_user_can_act_on_an_unmatched_message(em, pid, chans, clock):
    request(em, pid, business(em))
    says(chans, "stranger@elsewhere.example", "Stop messaging us", "<x1>", clock)
    em.poll_conversations()
    m = em.unmatched_messages()[0]
    s = em.suppress_unmatched(m.message_id, "Asha")
    assert s.reason == "user" and s.created_by == "Asha" and "unmatched" in s.source_ref and em.is_suppressed("email", "stranger@elsewhere.example")
    assert em.unmatched_messages() == [] and em.unmatched_messages(include_reviewed=True)[0].reviewed
    says(chans, "other@elsewhere.example", "stop", "<x2>", clock, minutes=8)
    em.poll_conversations()
    em.dismiss_unmatched(em.unmatched_messages()[0].message_id)
    assert em.unmatched_messages() == [] and em.is_suppressed("email", "other@elsewhere.example") is None
    with pytest.raises(ValueError):
        em.dismiss_unmatched("nope")


# ---- bounces

@pytest.fixture
def mail(tmp_path, clock):
    w = MailWorld(clock)
    e = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="bounce", channels={"email": w.channel(), "whatsapp": NoWhatsApp()}, require_symbolic=False)
    e.outreach_store.now = e.conversations.now = e.jobs_clock = e.suppressions.now = clock
    e.conversations.policy = SendPolicy(per_vendor_per_day=20, per_day=200, whatsapp_gap_s=0)
    plan = e.plan(COLLEGE, today=TODAY).plan.plan_id
    return e, w, plan


@pytest.mark.parametrize("original", ["headers", "message"])
def test_a_permanent_failure_that_matches_a_message_we_sent_marks_the_address_bad(mail, original):
    e, w, pid = mail
    biz = business(e)
    request(e, pid, biz)
    w.bounce(ADDR, with_original=original)
    e.poll_conversations()
    s = e.suppressions.active_for("email", ADDR)
    assert s is not None and s.reason == "bad_address" and s.created_by.startswith("system") and s.source_ref.startswith("<")
    with pytest.raises(ValueError, match="do not contact"):
        e.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id)
    assert e.unmatched_messages() == []


def test_a_failure_that_names_our_recipient_but_not_a_message_we_sent_goes_to_review_and_blocks_nothing(mail):
    """A crafted notice can name any vendor we wrote to. Only a Message-ID we recorded counts as correlation; the recipient alone does not."""
    e, w, pid = mail
    biz = business(e)
    request(e, pid, biz)
    w.bounce(ADDR, original_message_id="<not-ours@elsewhere>", with_original="headers")
    w.bounce(ADDR, original_message_id="", with_original="none")  # no returned headers at all
    e.poll_conversations()
    assert e.suppressions.all() == [] and e.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id).status == "prepared"
    shown = e.unmatched_messages()
    assert len(shown) == 2 and all(m.kind == "bounce" and "names an address we wrote to but not a message we sent" in m.body for m in shown)


def test_a_failure_that_matches_our_message_id_for_a_differently_spelled_recipient_correlates(mail):
    e, w, pid = mail
    request(e, pid, business(e))
    orig = str(w.last_to(ADDR)["Message-ID"])
    w.bounce("someone-else@forwarded.example", original_message_id=orig)
    e.poll_conversations()
    assert e.suppressions.active_for("email", ADDR).reason == "bad_address"


def test_the_bounce_reason_is_kept_as_the_remote_servers_words_cleaned_and_capped(mail):
    e, w, pid = mail
    request(e, pid, business(e))
    w.bounce(ADDR, status="5.1.1", diagnostic="smtp; 550 5.1.1 The email account that you tried to reach does not exist <script>x</script>" + " x" * 400)
    e.poll_conversations()
    s = e.suppressions.active_for("email", ADDR)
    assert s.detail.startswith("5.1.1: ") and "does not exist" in s.detail and len(s.detail) <= 300 and "\n" not in s.detail and "\x00" not in s.detail


def test_a_forged_but_well_formed_notice_naming_a_real_message_id_is_still_only_as_trustworthy_as_the_id(mail):
    """Honest limit, pinned as a test: whoever received our message knows its id, so a matching id is correlation and not proof the notice is genuine.
    The effect is bounded: it can only stop us writing to an address we already wrote to, it shows its reason, and only the user lifts it."""
    e, w, pid = mail
    biz = business(e)
    request(e, pid, biz)
    w.bounce(ADDR, original_message_id=str(w.last_to(ADDR)["Message-ID"]), sender="forger@elsewhere.example")
    e.poll_conversations()
    s = e.suppressions.active_for("email", ADDR)
    assert s is not None and s.created_by == "system: delivery failure notice" and s.detail
    assert e.suppressions.active_for("email", "other@sharma-caterers.example") is None  # bounded to the one address


# ---- the reason is shown before an address can be re-enabled

def bad(mail, detail_status="5.1.1", diagnostic="smtp; 550 mailbox unavailable"):
    e, w, pid = mail
    biz = business(e)
    request(e, pid, biz)
    w.bounce(ADDR, status=detail_status, diagnostic=diagnostic)
    e.poll_conversations()
    return e, w, pid, biz


def test_lifting_a_bad_address_needs_the_user_to_have_seen_the_reason(mail):
    e, w, pid, biz = bad(mail)
    s = e.suppressions.active_for("email", ADDR)
    with pytest.raises(NeedsAcknowledgement) as exc:
        e.lift_suppression(s.suppression_id, "Asha", "checked with them")
    assert "5.1.1" in str(exc.value) and "mailbox unavailable" in str(exc.value) and "remote mail server" in str(exc.value) and exc.value.reason == s.detail
    assert e.is_suppressed("email", ADDR) is not None
    assert not e.lift_suppression(s.suppression_id, "Asha", "checked with them", acknowledge_reason=True).active


def test_re_confirming_a_bad_address_needs_the_reason_to_be_acknowledged_but_a_different_address_does_not(mail):
    e, w, pid, biz = bad(mail)
    fresh = e.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": "billing@sharma-caterers.example", "source_url": SRC, "source_kind": "page_text", "flags": ()}])[0]
    with pytest.raises(NeedsAcknowledgement, match="5.1.1"):
        e.confirm_contact(biz.vendor_id, fresh.candidate_id, "Asha", edited_value=ADDR)  # typing the bounced address back
    assert e.is_suppressed("email", ADDR) is not None and e.shortlist.get_candidate(fresh.candidate_id).status == "found"
    assert e.confirm_contact(biz.vendor_id, fresh.candidate_id, "Asha", edited_value=ADDR, acknowledge_bounce=True).email == ADDR
    assert e.is_suppressed("email", ADDR) is None
    other = e.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": "x3@sharma-caterers.example", "source_url": SRC, "source_kind": "page_text", "flags": ()}])[0]
    assert e.confirm_contact(biz.vendor_id, other.candidate_id, "Asha").email == "x3@sharma-caterers.example"  # nothing to acknowledge


def test_a_stop_request_or_a_users_own_entry_needs_no_acknowledgement_to_lift(em):
    s = em.suppress_address("email", "x@shop.example", "Asha", "asked by phone")
    assert not em.lift_suppression(s.suppression_id, "Asha", "fine now").active


def test_the_api_returns_the_reason_with_a_conflict_and_the_list_shows_it(mail):
    e, w, pid, biz = bad(mail, diagnostic="smtp; 550 no such user")
    c = create_app(e, today_fn=lambda: TODAY, poll_seconds=0).test_client()
    listed = c.get("/api/suppressions").get_json()[0]
    assert listed["reason"] == "bad_address" and "no such user" in listed["detail"] and listed["detail"].startswith("5.1.1")
    r = post(c, f"/api/suppressions/{listed['suppression_id']}/lift", {"by": "Asha", "reason": "checked"})
    assert r.status_code == 409 and r.get_json()["needs_acknowledgement"] is True and "no such user" in r.get_json()["reason"]
    assert post(c, f"/api/suppressions/{listed['suppression_id']}/lift", {"by": "Asha", "reason": "checked", "acknowledge_reason": True}).status_code == 200


def test_a_temporary_failure_changes_nothing(mail):
    e, w, pid = mail
    request(e, pid, business(e))
    w.bounce(ADDR, permanent=False)
    e.poll_conversations()
    assert e.suppressions.all() == [] and e.unmatched_messages() == []


def test_a_failure_that_matches_nothing_we_sent_is_shown_and_changes_nothing(mail):
    e, w, pid = mail
    request(e, pid, business(e))
    w.bounce("someone@unrelated.example", original_message_id="<ours-not@elsewhere>")
    e.poll_conversations()
    assert e.suppressions.all() == [] and [(m.kind, m.sender) for m in e.unmatched_messages()] == [("bounce", "mailer-daemon@googlemail.com")]


def test_a_plain_message_that_only_looks_like_a_bounce_is_not_one(mail):
    e, w, pid = mail
    request(e, pid, business(e))
    w.inbox.append(f"From: MAILER-DAEMON@x.example\nTo: agent@fake.test\nSubject: Undelivered Mail Returned to Sender\nDate: Fri, 30 Oct 2026 10:00:00 +0000\nMessage-ID: <fake@x>\n\nFailed recipient: {ADDR}\n".encode())
    e.poll_conversations()
    assert e.suppressions.all() == []


def test_a_repeated_poll_does_not_repeat_the_effects(mail):
    e, w, pid = mail
    request(e, pid, business(e))
    w.bounce(ADDR)
    w.bounce("x@unrelated.example", original_message_id="<n@e>")
    for _ in range(3):
        e.poll_conversations()
    assert len(e.suppressions.all()) == 1 and len(e.unmatched_messages()) == 1


def test_re_confirming_or_editing_the_contact_clears_a_bad_address_but_never_a_stop_request(mail):
    e, w, pid = mail
    biz = business(e)
    request(e, pid, biz)
    w.bounce(ADDR)
    e.poll_conversations()
    assert e.suppressions.active_for("email", ADDR).reason == "bad_address"
    cand = e.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": ADDR, "source_url": SRC, "source_kind": "page_text", "flags": ()}])
    assert cand == []  # the same address is not offered twice; the user re-confirms by editing the confirmation of a fresh candidate
    fresh = e.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": "billing@sharma-caterers.example", "source_url": SRC, "source_kind": "page_text", "flags": ()}])[0]
    e.confirm_contact(biz.vendor_id, fresh.candidate_id, "Asha", edited_value=ADDR, acknowledge_bounce=True)  # the user types the same address back, having seen why it bounced
    lifted = [s for s in e.suppressions.all() if s.reason == "bad_address"][0]
    assert not lifted.active and lifted.lifted_by == "Asha" and "re-confirmed" in lifted.lift_reason and e.is_suppressed("email", ADDR) is None
    e.suppress_address("email", ADDR, "Asha", "stop by phone")
    last = e.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": "x2@sharma-caterers.example", "source_url": SRC, "source_kind": "page_text", "flags": ()}])[0]
    e.confirm_contact(biz.vendor_id, last.candidate_id, "Asha", edited_value=ADDR)
    assert e.is_suppressed("email", ADDR).reason == "user"  # confirming does not undo a stop


def test_a_bounce_never_blocks_a_different_address_at_the_same_domain(mail):
    e, w, pid = mail
    request(e, pid, business(e))
    w.bounce(ADDR)
    e.poll_conversations()
    assert e.is_suppressed("email", "other@sharma-caterers.example") is None


# ---- the model

def test_a_suppression_record_is_validated():
    from datetime import datetime, timezone

    base = dict(suppression_id="s1", channel="email", value="a@b.example", reason="user", created_at=datetime.now(timezone.utc), created_by="Asha")
    Suppression(**base)
    for bad in ({"reason": "because"}, {"channel": "sms"}, {"lifted_at": datetime.now(timezone.utc)}, {"lifted_by": "Asha"}, {"lifted_at": datetime.now(timezone.utc), "lifted_by": "Asha"}, {"surprise": 1}):
        with pytest.raises(Exception):
            Suppression(**{**base, **bad})


# ---- API

@pytest.fixture
def client(em, pid):
    return create_app(em, today_fn=lambda: TODAY, poll_seconds=0).test_client()


def post(client, url, body):
    token = client.get("/api/session").get_json()["csrf"]
    return client.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": token, "Content-Type": "application/json"})


def test_api_suppression_lifecycle(client, em):
    r = post(client, "/api/suppressions", {"channel": "email", "value": "x@shop.example", "by": "Asha", "note": "asked by phone"})
    assert r.status_code == 201 and r.get_json()["reason"] == "user"
    listed = client.get("/api/suppressions").get_json()
    assert listed[0]["value"] == "x@shop.example" and listed[0]["active"] is True and "other addresses at shop.example are not blocked" in listed[0]["note"]
    sid = listed[0]["suppression_id"]
    assert post(client, f"/api/suppressions/{sid}/lift", {"by": "Asha"}).status_code in (400, 422)
    assert post(client, f"/api/suppressions/{sid}/lift", {"by": "", "reason": "x"}).status_code in (400, 422)
    ok = post(client, f"/api/suppressions/{sid}/lift", {"by": "Asha", "reason": "fine now"})
    assert ok.status_code == 200 and ok.get_json()["lifted_by"] == "Asha"
    assert client.get("/api/suppressions").get_json()[0]["active"] is False


def test_api_unmatched_review(client, em, pid, chans, clock):
    request(em, pid, business(em))
    says(chans, "stranger@elsewhere.example", "stop", "<x1>", clock)
    em.poll_conversations()
    got = client.get("/api/unmatched").get_json()
    assert got[0]["kind"] == "stop_like" and got[0]["sender"] == "stranger@elsewhere.example"
    mid = got[0]["message_id"]
    assert post(client, f"/api/unmatched/{mid}/suppress", {"by": "Asha"}).status_code == 201
    assert client.get("/api/unmatched").get_json() == [] and em.is_suppressed("email", "stranger@elsewhere.example")


def test_api_rejects_bad_input_and_needs_csrf(client):
    for url, body in (("/api/suppressions", {"channel": "sms", "value": "x@shop.example", "by": "A"}), ("/api/suppressions", {"channel": "email", "value": "nope", "by": "A"}),
                      ("/api/suppressions", {"channel": "email", "value": "x@shop.example"}), ("/api/suppressions", {"channel": "email", "value": "x@shop.example", "by": "A", "surprise": 1}),
                      ("/api/suppressions/nope/lift", {"by": "A", "reason": "x"}), ("/api/unmatched/nope/dismiss", {}), ("/api/unmatched/nope/suppress", {"by": "A"})):
        assert post(client, url, body).status_code in (400, 404, 422), (url, body)
    raw = client.post("/api/suppressions", data=json.dumps({"channel": "email", "value": "x@shop.example", "by": "A"}), headers={"Content-Type": "application/json"})
    assert raw.status_code in (400, 403)


def test_blocking_survives_a_draft_that_another_process_just_moved_on(em, pid, monkeypatch):
    biz = business(em)
    a = em.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id)
    b = em.draft_outreach(pid, C.CATERER, "email", ADDR, "Asha", vendor_id=biz.vendor_id)
    real = em.outreach_store.transition
    calls = []

    def flaky(plan_id, draft_id, allowed, **kw):
        calls.append(draft_id)
        if draft_id == a.draft_id:
            raise ValueError("the draft is sending, not one of ['approved', 'prepared']")  # claimed by a dispatcher a moment earlier
        return real(plan_id, draft_id, allowed, **kw)

    monkeypatch.setattr(em.outreach_store, "transition", flaky)
    em.suppress_address("email", ADDR, "Asha")  # must not raise
    monkeypatch.undo()
    assert em.outreach_store.get(pid, b.draft_id).status == "blocked" and a.draft_id in calls


def test_the_batch_request_skips_a_suppressed_address(em, pid):
    chosen = em.store.latest(pid).selection_for(C.CATERER).vendor_id
    em.set_vendor_contact(chosen, "r@rasoi.test", None, "email")
    em.suppress_address("email", "r@rasoi.test", "Asha")
    made, skipped = em.request_all_quotes(pid, "Asha")
    assert made == [] and any("do not contact" in s for s in skipped)


# ---- audit: two spellings of one mailbox share one suppression; two different mailboxes never do

@pytest.mark.parametrize("a,b,same", [
    ("Sales@Shop.Example", "sales@shop.example", True), ("a.b@gmail.com", "ab@gmail.com", True), ("a+promo@gmail.com", "a@gmail.com", True), ("a.b+x@googlemail.com", "ab@gmail.com", True),
    ("a+x@outlook.com", "a@outlook.com", True), ("a.b@outlook.com", "ab@outlook.com", False), ("a+x@shop.example", "a@shop.example", False), ("a.b@shop.example", "ab@shop.example", False),
    ("a@gmail.com", "b@gmail.com", False), ("a@gmail.com", "a@outlook.com", False), ("a+x+y@gmail.com", "a@gmail.com", True),
])
def test_the_same_mailbox_matches_and_different_ones_do_not(a, b, same):
    assert (normalise_address("email", a) == normalise_address("email", b)) is same


@pytest.mark.parametrize("number,ok", [("+91 98765 43210", True), ("12345678", True), ("123456789012345", True), ("1234567", False), ("1234567890123456", False), ("0123456789", False), ("", False), ("abc", False)])
def test_a_whatsapp_number_is_8_to_15_digits_not_starting_with_zero(number, ok):
    if ok:
        assert normalise_address("whatsapp", number).isdigit()
    else:
        with pytest.raises(ValueError):
            normalise_address("whatsapp", number)


@pytest.mark.parametrize("bad", ["", "no-at-sign", "a@b", "a b@c.test", None, "a@b.test\nBcc: x@y.test"])
def test_a_malformed_email_is_refused_by_normalisation(bad):
    with pytest.raises(ValueError):
        normalise_address("email", bad)
