import pytest
from pydantic import ValidationError

from eventmate.cpsat import solve_schedule
from eventmate.schemas import ScheduleDependency as Dep, ScheduleSpec, ScheduleTask as T


def spec(tasks, deps=(), resources=None, horizon=10_000, anchor=None):
    return ScheduleSpec(tasks=tuple(tasks), dependencies=tuple(deps), resources=resources or {}, horizon=horizon, anchor_task=anchor)


def at(sol):
    return {e.task_id: (e.start, e.end) for e in sol.entries}


def test_dependency_with_lag_is_respected_and_asap_for_early_tasks():
    sol = solve_schedule(spec([T(id="a", name="a", duration=10, prefer="early"), T(id="b", name="b", duration=5, prefer="early")], [Dep(before="a", after="b", min_lag=20)]))
    assert sol.status == "optimal"
    s = at(sol)
    assert s["a"] == (0, 10) and s["b"][0] == 30


def test_unary_resource_prevents_overlap():
    tasks = [T(id=f"t{i}", name="t", duration=10, resource="crew", prefer="early") for i in range(3)]
    sol = solve_schedule(spec(tasks, resources={"crew": 1}))
    spans = sorted(at(sol).values())
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(2))


def test_capacity_two_allows_two_in_parallel_but_not_three():
    tasks = [T(id=f"t{i}", name="t", duration=10, resource="r", prefer="early") for i in range(3)]
    sol = solve_schedule(spec(tasks, resources={"r": 2}))
    starts = sorted(s for s, _ in at(sol).values())
    assert starts == [0, 0, 10]


def test_fixed_start_and_earliest_start_are_honoured():
    sol = solve_schedule(spec([T(id="a", name="a", duration=10, fixed_start=500), T(id="b", name="b", duration=10, earliest_start=300, prefer="early")]))
    assert at(sol)["a"] == (500, 510) and at(sol)["b"][0] == 300


def test_late_preference_pulls_task_toward_the_anchor():
    tasks = [T(id="setup", name="s", duration=30, prefer="late"), T(id="event", name="e", duration=60, fixed_start=1000)]
    sol = solve_schedule(spec(tasks, [Dep(before="setup", after="event", min_lag=15)], anchor="event"))
    assert at(sol)["setup"][1] == 985  # ends exactly at the lag boundary


def test_infeasible_deadline_reports_the_conflicting_constraints():
    tasks = [T(id="a", name="a", duration=100, earliest_start=0), T(id="b", name="b", duration=100, deadline_end=150)]
    sol = solve_schedule(spec(tasks, [Dep(before="a", after="b")]))
    assert sol.status == "infeasible"
    assert set(sol.infeasibility_core) == {"deadline:b", "dep:a->b"}  # minimal: nothing extra blamed


def test_infeasible_due_to_fixed_start_conflict_names_both_fixed_tasks():
    tasks = [T(id="a", name="a", duration=100, resource="r", fixed_start=0), T(id="b", name="b", duration=100, resource="r", fixed_start=50)]
    sol = solve_schedule(spec(tasks, resources={"r": 1}))
    assert sol.status == "infeasible"
    assert {"fixed:a", "fixed:b"} <= set(sol.infeasibility_core)


def test_solver_is_deterministic():
    tasks = [T(id=f"t{i}", name="t", duration=7 + i, resource="r", prefer="early") for i in range(6)]
    runs = [solve_schedule(spec(tasks, resources={"r": 1})) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


def test_every_task_is_scheduled_inside_the_horizon():
    tasks = [T(id=f"t{i}", name="t", duration=50, resource="r", prefer="early") for i in range(5)]
    sol = solve_schedule(spec(tasks, resources={"r": 1}, horizon=300))
    assert len(sol.entries) == 5 and all(0 <= e.start and e.end <= 300 for e in sol.entries)


def test_horizon_too_small_is_infeasible_not_a_crash():
    tasks = [T(id=f"t{i}", name="t", duration=50, resource="r") for i in range(5)]
    assert solve_schedule(spec(tasks, resources={"r": 1}, horizon=200)).status == "infeasible"


def test_timeout_is_clamped_and_validated():
    t = [T(id="a", name="a", duration=1)]
    assert solve_schedule(spec(t), timeout_s=0.001).status in ("optimal", "feasible", "error")
    with pytest.raises(ValueError):
        solve_schedule(spec(t), timeout_s=0)
    with pytest.raises(ValueError):
        solve_schedule(spec(t), timeout_s=10_000)


@pytest.mark.parametrize(
    "build",
    [
        lambda: spec([T(id="a", name="a", duration=1), T(id="a", name="a", duration=1)]),  # duplicate id
        lambda: spec([T(id="a", name="a", duration=1)], [Dep(before="a", after="zzz")]),  # unknown task
        lambda: spec([T(id="a", name="a", duration=1)], [Dep(before="a", after="a")]),  # self dependency
        lambda: spec([T(id="a", name="a", duration=1, resource="ghost")]),  # undeclared resource
        lambda: spec([T(id="a", name="a", duration=1), T(id="b", name="b", duration=1)], [Dep(before="a", after="b"), Dep(before="b", after="a")]),  # cycle
        lambda: spec([T(id="a", name="a", duration=1, prefer="late")]),  # late needs an anchor
        lambda: spec([T(id="a", name="a", duration=50)], horizon=10),  # cannot fit
        lambda: spec([]),
    ],
)
def test_invalid_specs_are_rejected_at_construction(build):
    with pytest.raises(ValidationError):
        build()


def test_task_models_reject_bad_values():
    for kw in (dict(duration=0), dict(duration=-5), dict(id="Bad Id"), dict(weight=0), dict(earliest_start=-1)):
        with pytest.raises(ValidationError):
            T(**{**dict(id="a", name="a", duration=1), **kw})


def test_core_is_minimal_even_when_many_unrelated_constraints_exist():
    tasks = [T(id=f"x{i}", name="x", duration=10, earliest_start=5, deadline_end=10_000) for i in range(6)]
    tasks += [T(id="a", name="a", duration=100), T(id="b", name="b", duration=100, deadline_end=150)]
    sol = solve_schedule(spec(tasks, [Dep(before="a", after="b")]))
    assert set(sol.infeasibility_core) == {"deadline:b", "dep:a->b"}
