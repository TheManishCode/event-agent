"""Replanning: disruptions, preservation of confirmed bookings, versioned diffs, immutability."""

import hashlib
import os
import stat
from datetime import date

import pytest
from pydantic import ValidationError

from eventmate.schemas import BookingStatus, Disruption, DisruptionKind as K, VendorCategory as C
from eventmate.service import EventMate
from tests.conftest import EXAMPLE, TODAY

LATER = date(2026, 11, 2)  # "today" when the disruption happens


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="replan")


@pytest.fixture
def planned(em):
    out = em.plan(EXAMPLE, today=TODAY)
    assert out.status == "planned", out.violations
    return out.plan


def digest(em, plan_id, v):
    return hashlib.sha256(em.store._path(plan_id, v).read_bytes()).hexdigest()


def sel(plan, cat):
    return plan.selection_for(cat)


def cancel(vid, **kw):
    return Disruption(kind=K.VENDOR_CANCELLATION, vendor_id=vid, **kw)


# -------------------------------------------------------------- disruption model


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(kind=K.VENDOR_CANCELLATION),
        dict(kind=K.BUDGET_CHANGE),
        dict(kind=K.GUEST_COUNT_CHANGE),
        dict(kind=K.DATE_CHANGE),
        dict(kind=K.BUDGET_CHANGE, new_budget=1, vendor_id="x"),
        dict(kind=K.BUDGET_CHANGE, new_budget=0),
        dict(kind=K.GUEST_COUNT_CHANGE, new_guest_count=0),
        dict(kind=K.VENDOR_CANCELLATION, vendor_id="../x"),
    ],
)
def test_invalid_disruptions_cannot_be_constructed(kwargs):
    with pytest.raises(ValidationError):
        Disruption(**kwargs)


# --------------------------------------------------------- cancellation


def test_vendor_cancellation_creates_version_two_with_a_diff(em, planned):
    v1_hash = digest(em, planned.plan_id, 1)
    old = sel(planned, C.VENUE)
    out = em.replan(planned.plan_id, cancel(old.vendor_id), LATER)
    assert out.status == "replanned"
    new = out.plan
    assert (new.version, new.parent_version) == (2, 1)
    assert sel(new, C.VENUE).vendor_id != old.vendor_id
    assert old.vendor_id in new.excluded_vendor_ids
    d = out.diff
    assert any(i.kind == "vendor" and i.key == "venue" and old.name in (i.before or "") for i in d.removed)
    assert any(i.kind == "vendor" and i.key == "venue" for i in d.added)
    assert any(i.kind == "vendor" and i.key == "caterer" for i in d.preserved)
    assert digest(em, planned.plan_id, 1) == v1_hash  # version 1 untouched


def test_cancelled_vendor_stays_excluded_in_later_replans(em, planned):
    first = sel(planned, C.VENUE).vendor_id
    v2 = em.replan(planned.plan_id, cancel(first), LATER).plan
    second = sel(v2, C.VENUE).vendor_id
    out = em.replan(planned.plan_id, cancel(second), LATER)  # both venues now gone
    assert out.status == "failed" and out.plan is None
    v = out.violations[0]
    assert v.constraint == "vendor_coverage" and v.repair
    assert em.store.latest(planned.plan_id).version == 2  # nothing saved by the failed replan


def test_unknown_vendor_cancellation_is_rejected(em, planned):
    out = em.replan(planned.plan_id, cancel("ven-does-not-exist"), LATER)
    assert out.status == "failed" and out.violations[0].constraint == "unknown_vendor"


def test_cancelling_a_confirmed_vendor_voids_that_booking_but_keeps_the_others(em, planned):
    em.confirm(planned.plan_id, C.VENUE)
    v3 = em.confirm(planned.plan_id, C.CATERER).plan
    out = em.replan(planned.plan_id, cancel(sel(v3, C.CATERER).vendor_id), LATER)
    assert out.status == "replanned"
    assert sel(out.plan, C.VENUE).booking_status == BookingStatus.CONFIRMED
    assert sel(out.plan, C.CATERER).booking_status == BookingStatus.AVAILABLE  # replacement is only proposed
    assert sel(out.plan, C.CATERER).vendor_id != sel(v3, C.CATERER).vendor_id


# ------------------------------------------------------------ confirmations


def test_confirm_records_a_user_fact_as_a_new_validated_version(em, planned):
    out = em.confirm(planned.plan_id, C.VENUE)
    assert out.plan.version == 2 and sel(out.plan, C.VENUE).booking_status == BookingStatus.CONFIRMED
    assert out.plan.validation.passed
    assert any(i.kind == "vendor" and i.key == "venue" for i in out.diff.changed)
    assert sel(planned, C.VENUE).booking_status == BookingStatus.AVAILABLE  # v1 still says "proposed"


def test_confirm_twice_or_unknown_category_is_an_error(em, planned):
    em.confirm(planned.plan_id, C.VENUE)
    with pytest.raises(ValueError, match="already confirmed"):
        em.confirm(planned.plan_id, C.VENUE)
    only_venue = EventMate(log_dir=em.logs.decisions.log_dir, plan_dir=em.store.root.parent / "p2", run_id="r2")
    p = only_venue.plan("Plan a 80 guests party in Pune on December 12 under INR 100,000.", today=TODAY).plan
    with pytest.raises(ValueError, match="no caterer"):
        only_venue.confirm(p.plan_id, C.CATERER)


# ----------------------------------------------- preservation / conflicts


def test_confirmed_booking_survives_unrelated_replans_at_the_same_time(em, planned):
    v2 = em.confirm(planned.plan_id, C.VENUE).plan
    out = em.replan(planned.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=190_000), LATER)
    assert out.status == "replanned"
    kept = sel(out.plan, C.VENUE)
    assert kept.vendor_id == sel(v2, C.VENUE).vendor_id and kept.booking_status == BookingStatus.CONFIRMED
    old_t = next(i for i in v2.items if i.task_id == "book_venue").start
    assert next(i for i in out.plan.items if i.task_id == "book_venue").start == old_t
    assert any(i.kind == "vendor" and i.key == "venue" and i.note == "confirmed booking kept" for i in out.diff.preserved)


def test_date_change_that_breaks_a_confirmed_booking_needs_a_decision_and_saves_nothing(em, planned):
    em.confirm(planned.plan_id, C.VENUE)
    before = em.store.latest(planned.plan_id).version
    out = em.replan(planned.plan_id, Disruption(kind=K.DATE_CHANGE, new_date=date(2026, 12, 19)), LATER)
    assert out.status == "needs_decision" and out.plan is None and out.diff is None
    [v] = out.violations
    assert v.constraint == "confirmed_booking_conflict"
    assert "date_availability" in v.detail and "override_confirmed" in v.repair
    assert em.store.latest(planned.plan_id).version == before


def test_explicit_override_releases_the_confirmed_booking(em, planned):
    venue = sel(em.confirm(planned.plan_id, C.VENUE).plan, C.VENUE)
    d = Disruption(kind=K.DATE_CHANGE, new_date=date(2026, 12, 19), override_confirmed=(venue.vendor_id,))
    out = em.replan(planned.plan_id, d, LATER)
    # the venue is released, but no caterer is free on the 19th: still not plannable, and says why
    assert out.status == "failed"
    assert any(v.constraint in ("date_availability", "vendor_coverage") for v in out.violations)
    assert all(v.repair for v in out.violations)


def test_override_must_name_a_confirmed_vendor(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=190_000, override_confirmed=("ven-pune-heritage",)), LATER)
    assert out.status == "failed" and out.violations[0].constraint == "invalid_override"


def test_guest_count_change_recomputes_a_confirmed_caterers_cost(em, planned):
    em.confirm(planned.plan_id, C.CATERER)
    out = em.replan(planned.plan_id, Disruption(kind=K.GUEST_COUNT_CHANGE, new_guest_count=130), LATER)
    assert out.status == "replanned"
    cat = sel(out.plan, C.CATERER)
    assert cat.booking_status == BookingStatus.CONFIRMED
    per_plate = {"cat-pune-rasoi": 550, "cat-pune-annapurna": 650}[cat.vendor_id]
    assert cat.cost == per_plate * 130 and out.plan.request.guest_count == 130
    assert any(i.key == "guest_count" for i in out.diff.changed)


def test_guest_count_below_a_confirmed_caterers_minimum_needs_a_decision(em, planned):
    cat = sel(planned, C.CATERER)
    if cat.vendor_id != "cat-pune-rasoi":
        pytest.skip("selection changed")  # Royal Rasoi serves 100-500 guests
    em.confirm(planned.plan_id, C.CATERER)
    out = em.replan(planned.plan_id, Disruption(kind=K.GUEST_COUNT_CHANGE, new_guest_count=90), LATER)
    assert out.status == "needs_decision" and "capacity" in out.violations[0].detail


def test_guest_count_too_large_for_unconfirmed_venue_selects_a_bigger_one(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.GUEST_COUNT_CHANGE, new_guest_count=145), LATER)
    assert out.status == "replanned"
    venue_id = sel(out.plan, C.VENUE).vendor_id
    assert em.repo.get(C.VENUE, venue_id).capacity >= 145


# ------------------------------------------------------------------ budget


def test_budget_cut_below_the_cheapest_plan_fails_with_a_repair(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=100_000), LATER)
    assert out.status == "failed"
    assert out.violations[0].constraint in ("total_budget", "vendor_coverage") and out.violations[0].repair


def test_budget_cut_with_confirmed_bookings_asks_for_a_decision(em, planned):
    em.confirm(planned.plan_id, C.VENUE)
    em.confirm(planned.plan_id, C.CATERER)
    out = em.replan(planned.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=100_000), LATER)
    assert out.status == "needs_decision"
    assert any("override_confirmed" in v.repair for v in out.violations)


def test_budget_increase_can_change_recommendations_and_is_diffed(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=400_000), LATER)
    assert out.status == "replanned"
    assert any(i.key == "budget_total" for i in out.diff.changed)
    assert out.plan.request.budget_total == 400_000 and out.plan.total_cost <= 400_000


# -------------------------------------------------------------------- date


def test_date_in_the_past_is_rejected(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.DATE_CHANGE, new_date=date(2026, 10, 1)), LATER)
    assert out.status == "failed" and out.violations[0].constraint == "event_date_in_past"


def test_date_change_without_vendors_free_reports_real_available_dates(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.DATE_CHANGE, new_date=date(2026, 12, 13)), LATER)
    assert out.status == "failed"
    assert any("2026-12-12" in v.repair for v in out.violations)  # only dates that exist in the database


def test_date_change_too_close_for_invitations_is_explained(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.DATE_CHANGE, new_date=date(2026, 11, 5)), LATER)
    assert out.status == "failed" and out.violations


# --------------------------------------------------------------- invariants


def test_every_replan_diff_has_all_four_sections_and_is_consistent(em, planned):
    out = em.replan(planned.plan_id, cancel(sel(planned, C.VENUE).vendor_id), LATER)
    d = out.diff
    assert (d.parent_version, d.new_version) == (1, 2)
    assert d.added and d.removed and d.changed and d.preserved
    keys = lambda items: {(i.kind, i.key) for i in items}  # noqa: E731
    assert not (keys(d.preserved) & keys(d.changed))


def test_old_versions_are_immutable_read_only_and_never_overwritten(em, planned):
    pid = planned.plan_id
    h1 = digest(em, pid, 1)
    em.confirm(pid, C.VENUE)
    em.replan(pid, Disruption(kind=K.BUDGET_CHANGE, new_budget=190_000), LATER)
    em.replan(pid, Disruption(kind=K.GUEST_COUNT_CHANGE, new_guest_count=110), LATER)
    assert [p.version for p in em.store.versions(pid)] == [1, 2, 3, 4]
    assert digest(em, pid, 1) == h1
    for v in (1, 2, 3, 4):
        assert stat.S_IMODE(os.stat(em.store._path(pid, v)).st_mode) == 0o444
    assert [p.parent_version for p in em.store.versions(pid)] == [None, 1, 2, 3]


def test_every_replanned_version_revalidates_against_the_database(em, planned):
    pid = planned.plan_id
    em.confirm(pid, C.VENUE)
    em.replan(pid, Disruption(kind=K.GUEST_COUNT_CHANGE, new_guest_count=110), LATER)
    for v in (1, 2, 3):
        assert em.revalidate(pid, v).passed


def test_guest_list_is_remembered_across_replans(em):
    out = em.plan(EXAMPLE, today=TODAY, guest_list_id="demo-engagement-120")
    assert out.status == "planned"
    new = em.replan(out.plan.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=210_000), LATER).plan
    assert new.guests.guest_list_id == "demo-engagement-120" and new.guests.confirmed == 13


def test_replan_decisions_are_logged_with_alternatives_or_failures(em, planned):
    em.replan(planned.plan_id, cancel(sel(planned, C.VENUE).vendor_id), LATER)
    em.replan(planned.plan_id, Disruption(kind=K.DATE_CHANGE, new_date=date(2026, 10, 1)), LATER)
    entries = [e for e in em.logs.decisions.read() if e.agent.value == "replanner"]
    assert any(e.decision_type.value == "recommendation" and e.alternatives for e in entries)
    assert any(e.decision_type.value == "failure" and "Repair" in e.justification or "nothing" in e.justification for e in entries)


# ------------------------------------------------------ time-awareness (bug hunt)


def test_replan_after_the_event_date_says_the_event_has_passed(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=190_000), date(2026, 12, 20))
    assert out.status == "failed"
    assert out.violations[0].constraint == "event_in_past" and "2026-12-12" in out.violations[0].detail
    assert "date change" in out.violations[0].repair


def test_repair_date_suggested_during_a_replan_really_is_schedulable_from_today(em, planned):
    today = date(2026, 12, 1)  # 11 days before the event: invitations (14-day rule) can no longer make it
    out = em.replan(planned.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=190_000), today)
    assert out.status == "failed"
    v = next(v for v in out.violations if v.constraint == "invitation_lead_time")
    suggested = date.fromisoformat(v.repair.split("on or after ")[1][:10])
    assert suggested > date(2026, 12, 12) and suggested >= today  # not the date that just failed, nor the past
    # and it is genuinely feasible when the replan is attempted from the same "today"
    import eventmate.agents.scheduler as sch

    agent = sch.SchedulingAgent(em.logs.decisions)
    spec_ok = agent.build_spec(planned.request.with_changes(event_date=suggested), tuple(planned.request.services), planned.origin, now_offset=(today - planned.origin.date()).days * 1440 + 540)
    spec_bad = agent.build_spec(planned.request.with_changes(event_date=suggested - __import__("datetime").timedelta(days=1)), tuple(planned.request.services), planned.origin, now_offset=(today - planned.origin.date()).days * 1440 + 540)
    from eventmate.cpsat import solve_schedule

    assert solve_schedule(spec_ok).status in ("optimal", "feasible")
    assert solve_schedule(spec_bad).status == "infeasible"


def test_date_change_far_into_the_future_is_rejected_with_a_clear_reason(em, planned):
    out = em.replan(planned.plan_id, Disruption(kind=K.DATE_CHANGE, new_date=date(2090, 1, 1)), LATER)
    assert out.status == "failed" and out.violations[0].constraint == "event_date_too_far"


def test_concurrent_modification_is_a_clear_failure_not_a_crash(em, planned, monkeypatch):
    def lost_race(plan):
        raise FileExistsError("version already written by another process")

    monkeypatch.setattr(em.store, "save", lost_race)
    out = em.replan(planned.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=190_000), LATER)
    assert out.status == "failed" and out.violations[0].constraint == "concurrent_modification"
    assert "retry" in out.violations[0].repair
    out = em.confirm(planned.plan_id, C.VENUE)
    assert out.status == "failed" and out.violations[0].constraint == "concurrent_modification"
