"""CP-SAT scheduling backend (OR-Tools).

The model design follows the CP-SAT formalizer in jiayizx/async_planning (MIT):
each task is a fixed-duration interval, dependencies are precedence with a
minimum lag, shared resources are NoOverlap (capacity 1) or Cumulative
(capacity > 1), and the objective is a weighted time preference. Differences:
the spec is the typed ``ScheduleSpec`` (no robots / cooking actions), and every
bound constraint carries a name via an assumption literal so an infeasible
result reports *which* constraints conflict instead of a bare "infeasible".

Deterministic by construction: one worker, fixed seed.
"""

from __future__ import annotations

import time
from typing import Dict, Optional, Set

from ortools.sat.python import cp_model

from eventmate.logs import ToolCallLog
from eventmate.schemas import ScheduledEntry, ScheduleSolution, ScheduleSpec

MAX_TIMEOUT_S = 120.0


def solve_schedule(
    spec: ScheduleSpec,
    timeout_s: float = 10.0,
    tool_log: Optional[ToolCallLog] = None,
    request_id: str = "unknown",
) -> ScheduleSolution:
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise ValueError(f"timeout_s must be in (0, {MAX_TIMEOUT_S}]")
    started = time.perf_counter()
    solution = _solve(spec, timeout_s)
    if tool_log is not None:
        tool_log.log(
            request_id=request_id,
            tool="cpsat.solve_schedule",
            args={"tasks": len(spec.tasks), "dependencies": len(spec.dependencies), "horizon": spec.horizon, "timeout_s": timeout_s},
            ok=solution.status in ("optimal", "feasible"),
            result=f"{solution.status}" + (f"; core={list(solution.infeasibility_core)[:8]}" if solution.infeasibility_core else ""),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    return solution


def _solve(spec: ScheduleSpec, timeout_s: float, enforce: Optional[Set[str]] = None) -> ScheduleSolution:
    model = cp_model.CpModel()
    horizon = spec.horizon
    starts, ends, intervals = {}, {}, {}
    assumptions: Dict[str, cp_model.IntVar] = {}

    def guard(label: str):
        lit = model.NewBoolVar(label)
        if enforce is not None and label not in enforce:
            model.Add(lit == 0)  # relaxed: this named constraint is switched off
        else:
            assumptions[label] = lit
        return lit

    for t in spec.tasks:
        starts[t.id] = model.NewIntVar(0, horizon, f"s_{t.id}")
        ends[t.id] = model.NewIntVar(0, horizon, f"e_{t.id}")
        intervals[t.id] = model.NewIntervalVar(starts[t.id], t.duration, ends[t.id], f"i_{t.id}")
        if t.earliest_start > 0:
            model.Add(starts[t.id] >= t.earliest_start).OnlyEnforceIf(guard(f"earliest:{t.id}"))
        if t.fixed_start is not None:
            model.Add(starts[t.id] == t.fixed_start).OnlyEnforceIf(guard(f"fixed:{t.id}"))
        if t.deadline_end is not None:
            model.Add(ends[t.id] <= t.deadline_end).OnlyEnforceIf(guard(f"deadline:{t.id}"))

    for d in spec.dependencies:
        model.Add(starts[d.after] >= ends[d.before] + d.min_lag).OnlyEnforceIf(guard(f"dep:{d.before}->{d.after}"))

    for resource, capacity in spec.resources.items():
        users = [intervals[t.id] for t in spec.tasks if t.resource == resource]
        if len(users) < 2:
            continue
        if capacity <= 1:
            model.AddNoOverlap(users)
        else:
            model.AddCumulative(users, [1] * len(users), capacity)

    terms = []
    for t in spec.tasks:
        if t.prefer == "early":
            terms.append(t.weight * ends[t.id])
        elif t.prefer == "late":
            terms.append(t.weight * (starts[spec.anchor_task] - ends[t.id]))
    model.Minimize(sum(terms) if terms else 0)
    model.AddAssumptions(list(assumptions.values()))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(timeout_s)
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 1
    code = solver.Solve(model)

    if code in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        entries = tuple(
            ScheduledEntry(task_id=t.id, start=solver.Value(starts[t.id]), end=solver.Value(ends[t.id])) for t in spec.tasks
        )
        return ScheduleSolution(
            status="optimal" if code == cp_model.OPTIMAL else "feasible",
            entries=entries,
            objective=int(solver.ObjectiveValue()) if terms else 0,
        )
    if code == cp_model.INFEASIBLE:
        by_index = {lit.Index(): label for label, lit in assumptions.items()}
        core = {by_index[i] for i in solver.SufficientAssumptionsForInfeasibility() if i in by_index}
        if enforce is None:
            core = _minimise_core(spec, timeout_s, core)
        return ScheduleSolution(status="infeasible", infeasibility_core=tuple(sorted(core)), detail="no schedule satisfies every constraint")
    return ScheduleSolution(status="error", detail=f"solver stopped with {solver.StatusName(code)}")


def _minimise_core(spec: ScheduleSpec, timeout_s: float, core: Set[str]) -> Set[str]:
    """Deletion-based shrink: drop each named constraint that is not needed for infeasibility.

    CP-SAT's core is sufficient but not minimal; a minimal one names the real conflict.
    """
    current = set(core)
    for label in sorted(core):
        trial = current - {label}
        if _solve(spec, min(timeout_s, 5.0), enforce=trial).status == "infeasible":
            current = trial
    return current
