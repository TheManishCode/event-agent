from datetime import date, datetime, timedelta

import pytest

from eventmate.agents.manager import parse_request
from eventmate.agents.scheduler import EventPolicy, SchedulingAgent
from eventmate.cpsat import solve_schedule
from eventmate.schemas import VendorCategory as C
from eventmate.tests.conftest import EXAMPLE, TODAY

ORIGIN = datetime(2026, 10, 30)
ALL = (C.VENUE, C.CATERER, C.PHOTOGRAPHER)


@pytest.fixture
def req():
    return parse_request(EXAMPLE, today=TODAY, known_cities=("Pune",))


@pytest.fixture
def agent(logger):
    return SchedulingAgent(logger)


def solved(agent, req, **kw):
    spec = agent.build_spec(req, ALL, ORIGIN, **kw)
    return spec, solve_schedule(spec)


def test_spec_has_tasks_durations_dependencies_and_resources(agent, req):
    spec = agent.build_spec(req, ALL, ORIGIN)
    ids = {t.id for t in spec.tasks}
    assert ids == {
        "book_venue", "book_caterer", "book_photographer", "reminder_invitation", "send_invitations",
        "reminder_vendor_confirmation", "venue_setup", "catering_setup", "photography_prep", "event", "teardown",
    }
    assert {"venue", "caterer", "photographer", "organizer"} <= set(spec.resources)
    assert {(d.before, d.after) for d in spec.dependencies} >= {("book_venue", "book_caterer"), ("venue_setup", "event"), ("event", "teardown")}
    assert all(t.duration >= 1 for t in spec.tasks)


def test_only_requested_services_and_reminders_produce_tasks(agent):
    r = parse_request("Plan a 80 guests party in Pune on December 12 under INR 100,000.", today=TODAY)
    spec = agent.build_spec(r, (C.VENUE,), ORIGIN)
    ids = {t.id for t in spec.tasks}
    assert not ids & {"book_caterer", "catering_setup", "book_photographer", "send_invitations", "reminder_invitation"}
    assert {"book_venue", "venue_setup", "event", "teardown"} <= ids


def test_example_schedule_is_feasible_and_meets_every_rule(agent, req):
    spec, sol = solved(agent, req)
    assert sol.status == "optimal"
    s = {e.task_id: e for e in sol.entries}
    p = EventPolicy()
    day0 = (req.event_date - ORIGIN.date()).days * 1440
    assert s["event"].start == day0 + 18 * 60  # engagement party starts 18:00
    assert s["send_invitations"].end <= day0 - p.invitation_lead_days * 1440
    assert s["reminder_invitation"].end + 1440 <= s["send_invitations"].start
    assert day0 - 7 * 1440 <= s["reminder_vendor_confirmation"].start and s["reminder_vendor_confirmation"].end <= day0 - 2 * 1440
    for setup in ("venue_setup", "catering_setup", "photography_prep"):
        assert s[setup].end + p.setup_buffer_minutes <= s["event"].start
    assert s["teardown"].start >= s["event"].end
    assert all(e.start >= 9 * 60 for e in sol.entries)  # nothing before the working day starts


def test_setups_are_just_in_time_not_at_dawn(agent, req):
    spec, sol = solved(agent, req)
    s = {e.task_id: e for e in sol.entries}
    assert s["catering_setup"].end == s["event"].start - EventPolicy().setup_buffer_minutes


def test_booking_tasks_are_sequenced_on_the_single_organizer(agent, req):
    spec, sol = solved(agent, req)
    s = {e.task_id: e for e in sol.entries}
    org = sorted((s[t.id].start, s[t.id].end) for t in spec.tasks if t.resource == "organizer")
    assert all(org[i][1] <= org[i + 1][0] for i in range(len(org) - 1))


def test_confirmed_bookings_become_fixed_starts(agent, req):
    spec = agent.build_spec(req, ALL, ORIGIN, fixed_starts={"book_venue": 600})
    t = next(t for t in spec.tasks if t.id == "book_venue")
    assert t.fixed_start == 600
    assert next(e for e in solve_schedule(spec).entries if e.task_id == "book_venue").start == 600


def test_unknown_fixed_task_is_rejected(agent, req):
    with pytest.raises(ValueError):
        agent.build_spec(req, ALL, ORIGIN, fixed_starts={"nonsense": 5})


def test_event_too_soon_is_infeasible_and_explained_with_a_solver_derived_repair(agent):
    r = parse_request("Plan a 80 guests party in Pune on 5 November under INR 100,000 with a photographer and reminders for invitations.", today=TODAY)
    spec, sol = solved(agent, r)
    assert sol.status == "infeasible"
    [v] = agent.explain_infeasibility(sol, r, (C.VENUE, C.PHOTOGRAPHER), ORIGIN)
    assert v.constraint == "invitation_lead_time"
    # the suggested date really is feasible, and the day before is not
    repair_date = date.fromisoformat(v.repair.split("on or after ")[1][:10])
    ok = agent.build_spec(r.with_changes(event_date=repair_date), (C.VENUE, C.PHOTOGRAPHER), ORIGIN)
    assert solve_schedule(ok).status in ("optimal", "feasible")
    bad = agent.build_spec(r.with_changes(event_date=repair_date - timedelta(days=1)), (C.VENUE, C.PHOTOGRAPHER), ORIGIN)
    assert solve_schedule(bad).status == "infeasible"


def test_now_offset_moves_all_unfixed_pre_event_tasks(agent, req):
    spec = agent.build_spec(req, ALL, ORIGIN, now_offset=10 * 1440 + 540)
    assert all(t.earliest_start >= 10 * 1440 + 540 for t in spec.tasks if t.group == "pre_event")


def test_items_have_wall_clock_times(agent, req):
    spec, sol = solved(agent, req)
    items = agent.to_items(spec, sol, ORIGIN)
    ev = next(i for i in items if i.task_id == "event")
    assert ev.start == datetime(2026, 12, 12, 18, 0) and ev.end == datetime(2026, 12, 12, 22, 0)
    assert [i.start for i in items] == sorted(i.start for i in items)


def test_conference_uses_its_own_event_defaults(agent):
    r = parse_request("Plan a 80 guests conference in Pune on December 12 under INR 100,000.", today=TODAY)
    spec = agent.build_spec(r, (C.VENUE,), ORIGIN)
    ev = next(t for t in spec.tasks if t.id == "event")
    assert ev.duration == 480 and ev.fixed_start % 1440 == 9 * 60


def test_policy_rejects_nonsense():
    for kw in (dict(invitation_lead_days=-1), dict(booking_minutes=0), dict(setup_buffer_minutes=-5)):
        with pytest.raises(Exception):
            EventPolicy(**kw)


def test_dependencies_into_a_fixed_booking_are_dropped_but_not_out_of_it(agent, req):
    spec = agent.build_spec(req, ALL, ORIGIN, fixed_starts={"book_caterer": 615}, now_offset=3 * 1440)
    pairs = {(d.before, d.after) for d in spec.dependencies}
    assert ("book_venue", "book_caterer") not in pairs  # caterer was booked before the venue was confirmed
    assert ("book_caterer", "catering_setup") in pairs
    assert solve_schedule(spec).status in ("optimal", "feasible")
