"""Mutation tests: start from a valid draft, break exactly one thing, expect the Validator to name it."""

from datetime import datetime, timedelta

import pytest

from eventmate.agents.guest import GuestAgent, GuestRepository
from eventmate.agents.planner import PlannerAgent
from eventmate.agents.scheduler import SchedulingAgent
from eventmate.agents.validator import ValidatorAgent
from eventmate.logs import RunLogs
from eventmate.schemas import BookingStatus, ScheduledEntry, VendorCategory as C
from eventmate.symbolic import SymbolicChecker

ORIGIN = datetime(2026, 10, 30)


@pytest.fixture
def logs(tmp_path):
    return RunLogs.create(log_dir=tmp_path, run_id="val")


@pytest.fixture
def draft(logs, intake, repo):
    g = GuestAgent(GuestRepository(), logs.decisions).summarize(intake.request)
    planner = PlannerAgent(logs, SchedulingAgent(logs.decisions), SymbolicChecker())
    return planner.plan(intake.request, g, intake.budget_allocation, intake.vendor_shortlists, ORIGIN, plan_id="pv").draft


@pytest.fixture
def validator(repo, logs):
    return ValidatorAgent(repo, logs)


def failed(report):
    return {c.name for c in report.checks if not c.passed}


def with_selection(draft, category, **changes):
    sels = tuple(s.model_copy(update=changes) if s.category == category else s for s in draft.selections)
    return draft.model_copy(update={"selections": sels})


def with_entry(draft, task_id, start=None, end=None):
    entries = tuple(
        ScheduledEntry.model_construct(task_id=e.task_id, start=e.start if start is None else start, end=e.end if end is None else end) if e.task_id == task_id else e
        for e in draft.solution.entries
    )
    return draft.model_copy(update={"solution": draft.solution.model_copy(update={"entries": entries})})


def test_genuine_draft_passes_every_check(validator, draft):
    r = validator.validate(draft)
    assert r.passed and not r.violations and len(r.checks) > 20
    assert not failed(r)


# ------------------------------------------------------------- vendor facts


def test_tampered_price_is_caught_against_the_database(validator, draft):
    r = validator.validate(with_selection(draft, C.CATERER, cost=1))
    assert "caterer:cost_matches_database" in failed(r)


def test_unknown_vendor_is_caught(validator, draft):
    r = validator.validate(with_selection(draft, C.VENUE, vendor_id="ven-invented"))
    assert "venue:vendor_exists" in failed(r)


def test_vendor_from_another_city_or_date_is_caught(validator, draft):
    r = validator.validate(with_selection(draft, C.VENUE, vendor_id="ven-mum-bay", name="Bay Pavilion", cost=70_000))
    assert "venue:city" in failed(r)
    r = validator.validate(with_selection(draft, C.VENUE, vendor_id="ven-pune-riverside", name="Riverside Hall", cost=60_000))
    assert "venue:date_availability" in failed(r)


def test_venue_too_small_and_wrong_setting_are_caught(validator, draft):
    r = validator.validate(with_selection(draft, C.VENUE, vendor_id="ven-pune-loft", name="Cozy Loft", cost=30_000))
    assert "venue:capacity" in failed(r) and "guest_capacity" in failed(r)
    r = validator.validate(with_selection(draft, C.VENUE, vendor_id="ven-pune-lawns", name="Lakeside Lawns", cost=50_000))
    assert "venue:venue_setting" in failed(r)


def test_caterer_missing_a_required_diet_is_caught(validator, draft):
    r = validator.validate(with_selection(draft, C.CATERER, vendor_id="cat-pune-darbar", name="Mughlai Darbar", cost=96_000))
    assert "caterer:dietary" in failed(r) and "guest_dietary" in failed(r)


def test_cancelled_vendor_is_caught(validator, draft):
    r = validator.validate(with_selection(draft, C.VENUE, vendor_id="ven-pune-mahal", name="Grand Mahal", cost=68_000))
    assert "venue:booking_status" in failed(r)


def test_excluded_vendor_cannot_be_planned(validator, draft):
    sel = next(s for s in draft.selections if s.category == C.VENUE)
    r = validator.validate(draft.model_copy(update={"excluded_vendor_ids": (sel.vendor_id,)}))
    assert "venue:booking_status" in failed(r)


def test_confirmation_without_provenance_is_caught(validator, draft):
    r = validator.validate(with_selection(draft, C.VENUE, booking_status=BookingStatus.CONFIRMED))
    assert "venue:confirmation_provenance" in failed(r)


def test_missing_or_extra_service_is_caught(validator, draft):
    r = validator.validate(draft.model_copy(update={"selections": draft.selections[:2]}))
    assert "services_match_request" in failed(r)


# ------------------------------------------------------------------ budget


def test_over_budget_is_caught(validator, draft):
    cheap = draft.request.with_changes(budget_total=100_000)
    r = validator.validate(draft.model_copy(update={"request": cheap}))
    assert "budget_total" in failed(r)
    assert any(v.constraint == "budget_total" and "raise the budget" in v.repair for v in r.violations)


def test_contingency_breach_is_a_warning_not_a_failure(validator, draft):
    r = validator.validate(draft.model_copy(update={"request": draft.request.with_changes(budget_total=150_000)}))
    assert "budget_total" not in failed(r)
    assert any("contingency" in w for w in r.warnings)


# --------------------------------------------------------------- schedule


def test_dependency_violation_is_caught(validator, draft):
    ev = next(e for e in draft.solution.entries if e.task_id == "book_caterer")
    r = validator.validate(with_entry(draft, "book_caterer", start=0, end=60))  # before book_venue and before work hours
    assert "schedule_dependencies" in failed(r) or "schedule_bounds" in failed(r)
    assert ev is not None


def test_resource_overlap_is_caught(validator, draft):
    e = next(e for e in draft.solution.entries if e.task_id == "book_venue")
    r = validator.validate(with_entry(draft, "book_caterer", start=e.start, end=e.start + 60))
    assert "schedule_resources" in failed(r)


def test_missing_task_entry_is_caught(validator, draft):
    sol = draft.solution.model_copy(update={"entries": tuple(e for e in draft.solution.entries if e.task_id != "teardown")})
    assert "schedule_complete" in failed(validator.validate(draft.model_copy(update={"solution": sol})))


def test_wrong_duration_is_caught(validator, draft):
    e = next(e for e in draft.solution.entries if e.task_id == "event")
    r = validator.validate(with_entry(draft, "event", end=e.end - 30))
    assert "schedule_durations" in failed(r)


def test_event_not_on_event_date_is_caught(validator, draft):
    e = next(e for e in draft.solution.entries if e.task_id == "event")
    r = validator.validate(with_entry(draft, "event", start=e.start + 1440, end=e.end + 1440))
    assert failed(r) & {"event_time", "schedule_bounds"}


def test_late_invitations_violate_lead_time_even_if_spec_was_tampered(validator, draft):
    spec_tasks = tuple(t.model_copy(update={"deadline_end": None}) if t.id == "send_invitations" else t for t in draft.spec.tasks)
    d = draft.model_copy(update={"spec": draft.spec.model_copy(update={"tasks": spec_tasks})})
    day0 = (draft.request.event_date - ORIGIN.date()).days * 1440
    d = with_entry(d, "send_invitations", start=day0 - 3 * 1440, end=day0 - 3 * 1440 + 120)
    assert "invitation_lead_time" in failed(validator.validate(d))


def test_setup_without_buffer_is_caught(validator, draft):
    e = next(e for e in draft.solution.entries if e.task_id == "catering_setup")
    r = validator.validate(with_entry(draft, "catering_setup", end=e.end + 20, start=e.start + 20))
    assert failed(r) & {"setup_before_event", "schedule_dependencies"}


def test_requested_reminder_must_be_scheduled(validator, draft):
    sol = draft.solution.model_copy(update={"entries": tuple(e for e in draft.solution.entries if e.task_id != "reminder_vendor_confirmation")})
    items = tuple(i for i in draft.items if i.task_id != "reminder_vendor_confirmation")
    r = validator.validate(draft.model_copy(update={"solution": sol, "items": items}))
    assert "reminders_scheduled" in failed(r)


def test_items_must_match_solution(validator, draft):
    items = tuple(i.model_copy(update={"start": i.start + timedelta(hours=1), "end": i.end + timedelta(hours=1)}) if i.task_id == "event" else i for i in draft.items)
    assert "items_match_solution" in failed(validator.validate(draft.model_copy(update={"items": items})))


# ---------------------------------------------------------------- symbolic


def test_failed_symbolic_check_fails_the_plan(validator, draft):
    bad = draft.symbolic.model_copy(update={"status": "invalid"})
    assert "symbolic_feasibility" in failed(validator.validate(draft.model_copy(update={"symbolic": bad})))


def test_unavailable_symbolic_fails_only_when_required(repo, logs, draft):
    un = draft.model_copy(update={"symbolic": draft.symbolic.model_copy(update={"status": "unavailable", "detail": "x"})})
    assert "symbolic_feasibility" in failed(ValidatorAgent(repo, logs, require_symbolic=True).validate(un))
    r = ValidatorAgent(repo, logs, require_symbolic=False).validate(un)
    assert r.passed and any("symbolic" in w for w in r.warnings)


# ----------------------------------------------------------------- logging


def test_failures_are_logged_with_constraint_and_repair(validator, logs, draft):
    validator.validate(with_selection(draft, C.CATERER, cost=1))
    rows = logs.validation_failures.read()
    assert rows and all(r.constraint and r.repair and r.plan_id == "pv" for r in rows)
    assert logs.decisions.read()[-1].decision_type.value == "failure"


def test_every_failure_has_a_violation_with_a_repair(validator, draft):
    r = validator.validate(with_selection(draft, C.CATERER, cost=1))
    assert {v.constraint for v in r.violations} == failed(r)
    assert all(v.repair for v in r.violations)


def test_validator_is_pure_with_respect_to_the_draft(validator, draft):
    before = draft.model_dump_json()
    validator.validate(draft)
    assert draft.model_dump_json() == before


# ---------------------------------------- the draft's own spec must not be trusted


def with_spec(draft, **changes):
    return draft.model_copy(update={"spec": draft.spec.model_copy(update=changes)})


def test_tampered_resource_capacity_cannot_legalise_overlaps(validator, draft):
    e = next(e for e in draft.solution.entries if e.task_id == "book_venue")
    d = with_entry(draft, "book_caterer", start=e.start, end=e.start + 60)  # overlaps book_venue on the organizer
    d = with_spec(d, resources={**d.spec.resources, "organizer": 5})
    assert "spec_matches_policy" in failed(validator.validate(d))


def test_shrunken_task_duration_in_the_spec_is_caught(validator, draft):
    tasks = tuple(t.model_copy(update={"duration": 5}) if t.id == "catering_setup" else t for t in draft.spec.tasks)
    sol = draft.solution
    entries = tuple(ScheduledEntry.model_construct(task_id=e.task_id, start=e.start, end=e.start + 5) if e.task_id == "catering_setup" else e for e in sol.entries)
    d = with_spec(draft, tasks=tasks).model_copy(update={"solution": sol.model_copy(update={"entries": entries})})
    assert "spec_matches_policy" in failed(validator.validate(d))


def test_reassigning_a_task_to_another_resource_is_caught(validator, draft):
    tasks = tuple(t.model_copy(update={"resource": None}) if t.id == "event" else t for t in draft.spec.tasks)
    assert "spec_matches_policy" in failed(validator.validate(with_spec(draft, tasks=tasks)))


def test_allocation_must_match_the_request_budget(validator, draft):
    bad = draft.allocation.model_copy(update={"total": draft.allocation.total + 1})
    assert "allocation_matches_request" in failed(validator.validate(draft.model_copy(update={"allocation": bad})))


def test_tampered_allocation_caps_are_caught(validator, draft):
    lines = tuple(l.model_copy(update={"amount": l.amount + 1}) if l.category == "venue" else l.model_copy(update={"amount": l.amount - 1}) if l.category == "caterer" else l for l in draft.allocation.lines)
    bad = draft.model_copy(update={"allocation": draft.allocation.model_copy(update={"lines": lines})})
    assert "allocation_matches_request" in failed(validator.validate(bad))
