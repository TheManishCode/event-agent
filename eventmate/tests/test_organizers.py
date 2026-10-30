"""F5 (part): the number of organizers is the capacity of the organizer resource (ADR-004, ADR-020)."""

import json

import pytest

from eventmate.agents.manager import RequestParseError, parse_request
from eventmate.schemas import EventRequest
from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="org")


def parse(text):
    return parse_request(text, today=TODAY, known_cities=("Pune", "Mumbai"))


def test_default_is_one_organizer_and_old_requests_still_load():
    r = parse(EXAMPLE)
    assert r.organizers == 1
    doc = json.loads(r.model_dump_json())
    del doc["organizers"]  # a request stored before this field existed
    assert EventRequest.model_validate(doc).organizers == 1


@pytest.mark.parametrize("phrase,n", [("with 2 organizers", 2), ("we have 3 coordinators", 3), ("2 planners", 2), ("1 organiser", 1)])
def test_organizer_count_is_parsed(phrase, n):
    assert parse(EXAMPLE + f" Team: {phrase}.").organizers == n


@pytest.mark.parametrize("bad", ["0 organizers", "11 organizers", "2 organizers and 3 organizers"])
def test_unreasonable_or_conflicting_organizer_counts_are_rejected(bad):
    with pytest.raises(RequestParseError):
        parse(EXAMPLE + f" Team: {bad}.")


def test_two_organizers_can_book_in_parallel_and_plan_still_validates(em):
    solo = em.plan(EXAMPLE, today=TODAY)
    duo = em.plan(EXAMPLE + " Team: 2 organizers.", today=TODAY)
    assert solo.plan and duo.plan and duo.plan.request.organizers == 2
    assert duo.plan.spec.resources["organizer"] == 2 and solo.plan.spec.resources["organizer"] == 1
    assert duo.plan.validation.passed
    entries = {e.task_id: e for e in duo.plan.solution.entries}
    bookings = [entries[t] for t in ("book_venue", "book_caterer", "book_photographer") if t in entries]
    overlaps = any(a.start < b.end and b.start < a.end for i, a in enumerate(bookings) for b in bookings[i + 1 :])
    solo_entries = {e.task_id: e for e in solo.plan.solution.entries}
    solo_b = [solo_entries[t] for t in ("book_venue", "book_caterer", "book_photographer") if t in solo_entries]
    assert not any(a.start < b.end and b.start < a.end for i, a in enumerate(solo_b) for b in solo_b[i + 1 :])  # one organizer never overlaps
    assert overlaps or duo.plan.solution.objective <= solo.plan.solution.objective  # never worse, parallel when it helps


def test_validator_rejects_organizer_capacity_that_disagrees_with_the_request(em):
    from eventmate.plan_store import draft_from_plan

    plan = em.plan(EXAMPLE, today=TODAY).plan  # request says 1 organizer
    d = draft_from_plan(plan)
    tampered = d.model_copy(update={"spec": d.spec.model_copy(update={"resources": {**d.spec.resources, "organizer": 5}})})
    report = em.validator.validate(tampered)
    assert any(c.name == "spec_matches_policy" and not c.passed for c in report.checks)


def test_replan_keeps_the_organizer_count(em):
    from eventmate.schemas import Disruption, DisruptionKind as K

    plan = em.plan(EXAMPLE + " Team: 2 organizers.", today=TODAY).plan
    out = em.replan(plan.plan_id, Disruption(kind=K.BUDGET_CHANGE, new_budget=190_000), TODAY)
    assert out.status == "replanned" and out.plan.request.organizers == 2 and out.plan.spec.resources["organizer"] == 2
