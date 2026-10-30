from datetime import datetime

import pytest

from eventmate.agents.manager import parse_request
from eventmate.agents.scheduler import SchedulingAgent
from eventmate.schemas import VendorCategory as C
from eventmate.symbolic import SymbolicChecker, build_pddl, order_respects_dependencies
from tests.conftest import EXAMPLE, TODAY

ORIGIN = datetime(2026, 10, 30)


@pytest.fixture
def spec(logger):
    r = parse_request(EXAMPLE, today=TODAY, known_cities=("Pune",))
    return SchedulingAgent(logger).build_spec(r, (C.VENUE, C.CATERER, C.PHOTOGRAPHER), ORIGIN)


@pytest.fixture(scope="module")
def checker():
    return SymbolicChecker()


needs_solvers = pytest.mark.skipif(not SymbolicChecker().available, reason="Fast Downward / VAL binaries not built")


def test_pddl_has_one_action_per_task_with_dependency_preconditions(spec):
    domain, problem, actions = build_pddl(spec)
    assert len(actions) == len(spec.tasks)
    assert "(:action do_event" in domain
    # event needs all three setups done
    event_block = domain.split("(:action do_event")[1].split("(:action")[0]
    for s in ("venue_setup", "catering_setup", "photography_prep"):
        assert f"(done_{s})" in event_block
    assert "(:goal (and" in problem and "(done_teardown)" in problem


def test_already_done_tasks_are_initial_facts_not_actions(spec):
    domain, problem, actions = build_pddl(spec, already_done=("book_venue",))
    assert "(done_book_venue)" in problem.split("(:init")[1].split("(:goal")[0]
    assert "do_book_venue" not in domain and "do_book_venue" not in actions


def test_unknown_already_done_task_is_rejected(spec):
    with pytest.raises(ValueError):
        build_pddl(spec, already_done=("nonsense",))


def test_pddl_never_contains_untrusted_text(spec):
    domain, problem, _ = build_pddl(spec)
    assert all(ch in "abcdefghijklmnopqrstuvwxyz0123456789_-() :\n?;.,'\"" or ch.isspace() for ch in domain + problem)


def test_order_checker_catches_violations(spec):
    _, _, actions = build_pddl(spec)
    good = ["do_" + t.id for t in spec.tasks]  # task order in the spec is dependency-compatible?
    # build an order from the checker's own view: a reversed order must fail
    assert not order_respects_dependencies(spec, list(reversed(good)))
    assert not order_respects_dependencies(spec, ["do_event"])


@needs_solvers
def test_planner_and_validator_accept_the_real_event_graph(spec, checker, logs_tool):
    res = checker.check(spec, tool_log=logs_tool, request_id="r1")
    assert res.status == "valid", res.detail
    assert order_respects_dependencies(spec, list(res.plan_actions))
    assert res.plan_actions[-1] == "do_teardown"
    assert logs_tool.read()[-1].tool == "pddl.check"


@needs_solvers
def test_confirmed_bookings_shorten_the_symbolic_plan(spec, checker):
    full = checker.check(spec)
    part = checker.check(spec, already_done=("book_venue", "book_caterer"))
    assert part.status == "valid" and len(part.plan_actions) == len(full.plan_actions) - 2


@needs_solvers
def test_val_rejects_a_plan_with_wrong_order(spec, checker):
    domain, problem, actions = build_pddl(spec)
    bad = list(reversed(actions))
    ok, detail = checker.validate_plan(domain, problem, bad)
    assert not ok


@needs_solvers
def test_unsolvable_problem_is_no_plan_not_a_crash(checker):
    domain = "(define (domain t) (:requirements :strips) (:predicates (a) (b)) (:action x :parameters () :precondition (a) :effect (b)))"
    problem = "(define (problem p) (:domain t) (:init) (:goal (b)))"
    status, actions, detail = checker.plan_pddl(domain, problem)
    assert status == "no_plan" and actions == ()


def test_missing_binaries_report_unavailable_not_valid(spec, tmp_path):
    ch = SymbolicChecker(fd_path=tmp_path / "nope.py", val_path=tmp_path / "nope")
    assert not ch.available
    res = ch.check(spec)
    assert res.status == "unavailable" and "not found" in res.detail


@needs_solvers
def test_planner_timeout_is_reported_as_error(spec):
    ch = SymbolicChecker(timeout_s=0.001)
    assert ch.check(spec).status == "error"


def test_timeout_bounds_are_enforced():
    for bad in (0, -1, 10_000):
        with pytest.raises(ValueError):
            SymbolicChecker(timeout_s=bad)
