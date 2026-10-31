"""Behaviour that mutation testing showed nothing pinned (audit): claim digits and boundaries, job field limits, and the job store's guards."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from eventmate.followthrough.claims import amount_is_in_text, extract_claims, stated_validity_days
from eventmate.followthrough.jobs import Job, JobStore, StaleJob, VendorProgress
from eventmate.schemas import AmountMention, ReplyAnalysis
from tests.test_claims import msg

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


# ---- claims

@pytest.mark.parametrize("amount,text,ok", [
    (66000, "price is 66000", True), (66000, "price is 66,000", True), (66000, "price 66k", True), (150000, "1.5 lakh", True), (100000, "1 lac", True), (200000, "2 lacs", True),
    (300000, "3 lakhs", True), (20000000, "2 crore", True), (30000000, "3 crores", True), (10000000, "1 cr", True),
    (66000, "66 guests", False), (66000, "65999.5", False), (66000, "65999.7", True), (66000, "66000.4", True), (66000, "66000.5", False), (66000, "nothing here", False),
])
def test_an_amount_counts_only_when_it_is_written_as_that_amount(amount, text, ok):
    assert amount_is_in_text(amount, text) is ok


@pytest.mark.parametrize("text,days", [
    ("valid for 365 days", 365), ("valid for 366 days", None), ("valid for 1 day", 1), ("valid for 0 days", None), ("valid for 52 weeks", 364), ("valid for 53 weeks", None),
    ("valid for two weeks", 14), ("holds for four days", 4), ("valid for three days", 3), ("valid for one day", 1), ("valid for a week", 7), ("no mention of validity", None),
])
def test_stated_validity_is_read_only_when_written_and_within_a_year(text, days):
    assert stated_validity_days(text) == days


def forged(body, amounts, unit="per_plate"):
    """A message whose analysis claims amounts that the text may not contain: what a bug upstream could produce."""
    m = msg(body)
    return m.model_copy(update={"analysis": ReplyAnalysis(amounts=tuple(AmountMention(low_inr=a, high_inr=a, unit=unit, snippet=f"INR {a} per plate") for a in amounts), says_unavailable=False, says_available=False, questions=(), asks_to_stop=False, checklist_mentioned=())})


def test_a_price_whose_digits_are_not_in_the_reply_is_never_proposed():
    claims, problem = extract_claims(forged("Happy to help with your event.", [650]))
    assert claims == [] and "digits are not clearly in the text" in problem


def test_two_different_amounts_need_a_person_but_the_same_amount_twice_does_not():
    claims, problem = extract_claims(forged("Rs 650 per plate or Rs 700 per plate", [650, 700]))
    assert claims == [] and "several different amounts" in problem
    claims, problem = extract_claims(forged("Rs 650 per plate. Yes, Rs 650 per plate.", [650, 650]))
    assert len(claims) == 1 and claims[0].amount_inr == 650 and problem is None


def test_a_claim_carries_the_stated_validity_and_a_bounded_snippet():
    claims, _ = extract_claims(msg("Rs 650 per plate, valid for 10 days."))
    assert claims[0].stated_valid_days == 10 and len(claims[0].snippet) <= 200 and len(claims[0].claim_id) == 16


# ---- jobs: limits and guards

def job(**kw):
    base = dict(job_id="j1", plan_id="p1", category="caterer", goal="3 quotes", sender_name="Asha", deadline=NOW + timedelta(days=3), vendors={"v1": VendorProgress(contact_channel="email", recipient="a@b.test", draft_id="d1")}, created_at=NOW)
    base.update(kw)
    return Job(**base)


@pytest.mark.parametrize("field,ok,bad", [
    ("min_quotes", (1, 10), (0, 11)), ("reply_window_hours", (1, 168), (0, 169)), ("poll_minutes", (1, 1440), (0, 1441)), ("max_nudges_per_vendor", (0, 5), (-1, 6)),
    ("max_amount_inr", (1,), (0, -5)),
])
def test_job_limits_are_exact(field, ok, bad):
    for v in ok:
        assert getattr(job(**{field: v}), field) == v
    for v in bad:
        with pytest.raises(ValidationError):
            job(**{field: v})


@pytest.mark.parametrize("field,limit", [("goal", 300), ("sender_name", 80)])
def test_text_limits_are_exact(field, limit):
    job(**{field: "x" * limit})
    with pytest.raises(ValidationError):
        job(**{field: "x" * (limit + 1)})
    if field == "sender_name":
        with pytest.raises(ValidationError):
            job(sender_name="")


@pytest.mark.parametrize("field", ["deadline", "created_at", "wake_at"])
def test_a_time_without_a_zone_is_refused_on_every_time_field(field):
    with pytest.raises(ValidationError, match="time zone"):
        job(**{field: datetime(2026, 11, 1, 9, 0)})
    assert job(**{field: datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)})


def test_a_job_refuses_unknown_fields_and_cannot_be_edited_in_place():
    with pytest.raises(ValidationError):
        job(surprise=1)
    with pytest.raises(ValidationError):
        job().status = "done"


def test_saving_an_unknown_job_fails_and_leaves_the_store_unchanged(tmp_path):
    store = JobStore(tmp_path)
    with pytest.raises(ValueError, match="unknown job"):
        store.save(job(), "user", "x", NOW)
    assert store.list() == []


def test_a_stale_writer_loses_and_the_history_records_the_transition(tmp_path):
    store = JobStore(tmp_path)
    j = store.create(job(status="active"), NOW)
    first = store.save(j.model_copy(update={"status": "waiting_vendor"}), "worker", "asked", NOW, {"n": 1})
    with pytest.raises(StaleJob):
        store.save(j.model_copy(update={"status": "done"}), "user", "late", NOW)  # j is version 1, the store is at 2
    assert first.version == j.version + 1 and store.get("j1").status == "waiting_vendor"
    store.save(first, "worker", "noop", NOW)
    ev = store.history("j1")
    assert ev[-2][3] == {"n": 1, "from": "active", "to": "waiting_vendor"} and ev[-1][3] == {}


def test_a_job_that_is_done_cannot_move_again(tmp_path):
    store = JobStore(tmp_path)
    j = store.create(job(status="active"), NOW)
    d = store.save(j.model_copy(update={"status": "done"}), "worker", "done", NOW)
    for to in ("active", "waiting_user", "cancelled"):
        with pytest.raises(ValueError, match="illegal job transition"):
            store.save(d.model_copy(update={"status": to}), "user", "x", NOW)
