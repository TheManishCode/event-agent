from datetime import datetime
from itertools import product

import pytest

from eventmate.agents.guest import GuestAgent, GuestRepository
from eventmate.agents.planner import PlannerAgent
from eventmate.agents.scheduler import SchedulingAgent
from eventmate.logs import RunLogs
from eventmate.schemas import BookingStatus, VendorCategory as C, VendorSelection
from eventmate.symbolic import SymbolicChecker

ORIGIN = datetime(2026, 10, 30)


@pytest.fixture
def logs(tmp_path):
    return RunLogs.create(log_dir=tmp_path, run_id="planner")


@pytest.fixture
def planner(logs):
    return PlannerAgent(logs, SchedulingAgent(logs.decisions), SymbolicChecker())


@pytest.fixture
def guests(logs, intake):
    return GuestAgent(GuestRepository(), logs.decisions).summarize(intake.request)


def run(planner, intake, guests, **kw):
    return planner.plan(intake.request, guests, intake.budget_allocation, intake.vendor_shortlists, ORIGIN, plan_id="p1", **kw)


def test_selects_the_best_scoring_affordable_combination(planner, intake, guests):
    res = run(planner, intake, guests)
    d = res.draft
    assert d is not None and not res.violations
    chosen = {s.category: s.vendor_id for s in d.selections}
    # brute-force oracle over the shortlists
    options = [[(c.score, c.estimated_cost, c.vendor_id) for c in sl.candidates] for sl in intake.vendor_shortlists]
    best = max(
        (combo for combo in product(*options) if sum(x[1] for x in combo) <= 200_000 - intake.budget_allocation.contingency),
        key=lambda combo: (round(sum(x[0] for x in combo), 6), -sum(x[1] for x in combo)),
    )
    assert set(chosen.values()) == {x[2] for x in best}


def test_every_selection_has_cost_from_the_shortlist_and_a_real_alternative(planner, intake, guests):
    d = run(planner, intake, guests).draft
    costs = {c.vendor_id: c.estimated_cost for sl in intake.vendor_shortlists for c in sl.candidates}
    for s in d.selections:
        assert s.cost == costs[s.vendor_id]
        assert s.alternatives and s.justification and s.source.startswith("database:")
    assert sum(s.cost for s in d.selections) <= 200_000


def test_draft_contains_schedule_and_symbolic_result(planner, intake, guests):
    d = run(planner, intake, guests).draft
    assert d.solution.status == "optimal" and len(d.items) == len(d.spec.tasks)
    assert d.symbolic.status == "valid"
    assert {i.task_id for i in d.items} >= {"event", "book_venue", "send_invitations"}


def test_planning_logs_decisions_and_tool_calls(planner, logs, intake, guests):
    run(planner, intake, guests)
    kinds = [e.decision_type.value for e in logs.decisions.read() if e.agent.value == "planner"]
    assert "recommendation" in kinds and "assessment" in kinds
    assert {e.tool for e in logs.tool_calls.read()} >= {"cpsat.solve_schedule", "pddl.check"}


def test_budget_too_small_for_any_combination_reports_total_budget_with_repair(planner, intake, guests):
    tiny = intake.request.with_changes(budget_total=100_000)
    alloc = intake.budget_allocation
    res = planner.plan(tiny, guests, alloc, intake.vendor_shortlists, ORIGIN, plan_id="p2")
    assert res.draft is None
    [v] = res.violations
    assert v.constraint == "total_budget" and "139,000" in v.repair


def test_contingency_breach_is_allowed_with_a_warning_when_total_still_fits(planner, intake, guests):
    req = intake.request.with_changes(budget_total=150_000)  # 139k fits the total but not total - 10% reserve
    res = planner.plan(req, guests, intake.budget_allocation, intake.vendor_shortlists, ORIGIN, plan_id="p3")
    assert res.draft is not None
    assert any("contingency" in w for w in res.draft.warnings)


def test_forced_confirmed_selection_is_kept(planner, intake, guests):
    cand = intake.vendor_shortlists[0].candidates[-1]  # not the top venue
    forced = VendorSelection(
        category=C.VENUE, vendor_id=cand.vendor_id, name=cand.name, cost=cand.estimated_cost,
        booking_status=BookingStatus.CONFIRMED, score=cand.score, justification="confirmed by user",
        no_alternative_reason="confirmed booking", source=cand.source,
    )
    res = run(planner, intake, guests, forced={C.VENUE: forced}, fixed_starts={"book_venue": 600})
    d = res.draft
    assert next(s for s in d.selections if s.category == C.VENUE).booking_status == BookingStatus.CONFIRMED
    assert next(s for s in d.selections if s.category == C.VENUE).vendor_id == cand.vendor_id
    assert "book_venue" in d.already_done
    assert next(i for i in d.items if i.task_id == "book_venue").start == ORIGIN.replace(hour=10)


def test_event_too_soon_returns_violation_not_a_plan(planner, intake, guests):
    near = intake.request.with_changes(event_date=datetime(2026, 11, 5).date())
    res = planner.plan(near, guests, intake.budget_allocation, intake.vendor_shortlists, ORIGIN, plan_id="p4")
    assert res.draft is None and res.violations[0].constraint == "invitation_lead_time"


def test_empty_shortlist_is_a_violation_not_a_crash(planner, intake, guests):
    from eventmate.schemas import VendorShortlist

    empty = VendorShortlist(category=C.VENUE, budget_cap=1, candidates=(), rejected=())
    res = planner.plan(intake.request, guests, intake.budget_allocation, (empty,) + intake.vendor_shortlists[1:], ORIGIN, plan_id="p5")
    assert res.draft is None and res.violations[0].constraint == "vendor_coverage"


def test_selection_is_deterministic(planner, intake, guests):
    a = run(planner, intake, guests).draft
    b = run(planner, intake, guests).draft
    assert [s.vendor_id for s in a.selections] == [s.vendor_id for s in b.selections]
    assert a.items == b.items


def test_rejected_alternatives_are_near_misses_not_irrelevant_records(planner, intake, guests):
    d = run(planner, intake, guests).draft
    venue = next(s for s in d.selections if s.category == C.VENUE)
    names = [a.option for a in venue.alternatives]
    assert "Skyline Banquet" in names  # fails only the budget cap
    assert "Bay Pavilion" not in names  # wrong city: not a real alternative
