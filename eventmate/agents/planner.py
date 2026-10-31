"""Planner Agent: builds a candidate plan (``PlanDraft``).

1. Picks one vendor per requested category from the Vendor Agent's shortlists
   by exhaustive search (the space is tiny, so the optimum is provable and the
   runner-up combinations become honest alternatives).
2. Builds the CP-SAT schedule spec via the Scheduling Agent and solves it.
3. Runs the PDDL symbolic feasibility check (Fast Downward + VAL).

It never decides that a plan is acceptable: the Validator Agent does that.
"""

from __future__ import annotations

from itertools import product
from typing import Dict, List, Optional, Sequence, Tuple

from eventmate.arcade_pulse import pulse as _arcade_pulse
from eventmate.agents.scheduler import SchedulingAgent
from eventmate.cpsat import solve_schedule
from eventmate.logs import RunLogs
from eventmate.schemas import (
    AgentName,
    Alternative,
    BookingStatus,
    BudgetAllocation,
    ConstraintCheck,
    ConstraintViolation,
    DecisionType,
    EventRequest,
    GuestSummary,
    PlanDraft,
    PlanningResult,
    VendorCandidate,
    VendorCategory,
    VendorSelection,
    VendorShortlist,
)
from eventmate.symbolic import SymbolicChecker
from datetime import datetime

BOOKING_TASK = {
    VendorCategory.VENUE: "book_venue",
    VendorCategory.CATERER: "book_caterer",
    VendorCategory.PHOTOGRAPHER: "book_photographer",
}


# How structural a failed constraint is: money and preferences are easy to fix, city and
# booking status are not. Used to show the nearest misses as alternatives.
_FIX_COST = {"budget_cap": 1, "dietary": 2, "venue_setting": 2, "capacity": 3, "date_availability": 4, "booking_status": 8, "city": 9}


def _miss_distance(rejected) -> Tuple[int, str]:
    return (sum(_FIX_COST.get(c.name, 5) for c in rejected.failed), rejected.vendor_id)


class PlannerAgent:
    agent_name = AgentName.PLANNER

    def __init__(self, logs: RunLogs, scheduler: SchedulingAgent, symbolic: SymbolicChecker):
        self.logs = logs
        self.scheduler = scheduler
        self.symbolic = symbolic

    def plan(
        self,
        request: EventRequest,
        guests: GuestSummary,
        allocation: BudgetAllocation,
        shortlists: Sequence[VendorShortlist],
        origin: datetime,
        plan_id: str,
        forced: Optional[Dict[VendorCategory, VendorSelection]] = None,
        fixed_starts: Optional[Dict[str, int]] = None,
        now_offset: Optional[int] = None,
        exclude_ids: Sequence[str] = (),
    ) -> PlanningResult:
        forced = forced or {}
        by_cat = {s.category: s for s in shortlists}
        categories = tuple(request.services)

        gaps = [c for c in categories if c not in forced and not by_cat.get(c, VendorShortlist(category=c, budget_cap=0, candidates=(), rejected=())).candidates]
        if gaps:
            vs = [ConstraintViolation(constraint="vendor_coverage", detail=f"no eligible {c.value}", repair=(by_cat[c].violations[0].repair if c in by_cat and by_cat[c].violations else f"add {c.value} options")) for c in gaps]
            self._fail(request, "No eligible vendor for " + ", ".join(c.value for c in gaps), vs)
            return PlanningResult(violations=tuple(vs))

        _arcade_pulse.stage_started("select")
        chosen = self._select(request, allocation, categories, by_cat, forced)
        if isinstance(chosen, list):
            self._fail(request, "No affordable vendor combination", chosen)
            return PlanningResult(violations=tuple(chosen))
        selections, warnings = chosen
        _arcade_pulse.stage_done("select")

        fixed = dict(fixed_starts or {})
        already_done = tuple(sorted(BOOKING_TASK[s.category] for s in selections if s.booking_status == BookingStatus.CONFIRMED))
        _arcade_pulse.stage_started("schedule")
        spec = self.scheduler.build_spec(request, categories, origin, fixed, now_offset)
        solution = solve_schedule(spec, tool_log=self.logs.tool_calls, request_id=request.request_id)
        _arcade_pulse.stage_done("schedule")
        if solution.status not in ("optimal", "feasible"):
            vs = self.scheduler.explain_infeasibility(solution, request, categories, origin, fixed, now_offset) if solution.status == "infeasible" else [
                ConstraintViolation(constraint="schedule_solver", detail=solution.detail, repair="retry; if it persists reduce the number of tasks")
            ]
            self._fail(request, f"Schedule solver returned {solution.status}", vs)
            return PlanningResult(violations=tuple(vs))
        items = self.scheduler.to_items(spec, solution, origin)
        _arcade_pulse.stage_started("symbolic")
        symbolic = self.symbolic.check(spec, already_done, tool_log=self.logs.tool_calls, request_id=request.request_id)
        _arcade_pulse.stage_done("symbolic")
        self.logs.decisions.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.ASSESSMENT,
            summary=f"Schedule {solution.status} with {len(items)} items; symbolic check {symbolic.status}",
            constraints_checked=(
                ConstraintCheck(name="schedule_feasible", passed=True, detail=f"CP-SAT {solution.status}, objective {solution.objective}"),
                ConstraintCheck(name="symbolic_feasibility", passed=symbolic.status == "valid", detail=f"{symbolic.status}: {symbolic.detail}"),
            ),
            justification="Timing rules (lead times, buffers, resource exclusivity) come from EventPolicy; the solver minimises "
            "booking delay and keeps setups just-in-time. PDDL confirms the dependency structure is achievable.",
            confidence=0.95 if symbolic.status == "valid" else 0.7,
            source="tool:cpsat",
        )
        draft = PlanDraft(
            plan_id=plan_id,
            origin=origin,
            request=request,
            guests=guests,
            allocation=allocation,
            selections=selections,
            spec=spec,
            solution=solution,
            symbolic=symbolic,
            items=items,
            already_done=already_done,
            warnings=tuple(warnings),
            excluded_vendor_ids=tuple(sorted(set(exclude_ids))),
        )
        return PlanningResult(draft=draft)

    # ------------------------------------------------------- vendor choice

    def _select(self, request, allocation, categories, by_cat, forced):
        options: List[List[Tuple[VendorCategory, str, str, int, float, str, BookingStatus, Optional[VendorCandidate]]]] = []
        for c in categories:
            if c in forced:
                f = forced[c]
                options.append([(c, f.vendor_id, f.name, f.cost, f.score, f.source, f.booking_status, None)])
            else:
                options.append([(c, x.vendor_id, x.name, x.estimated_cost, x.score, x.source, BookingStatus.AVAILABLE, x) for x in by_cat[c].candidates])
        combos = list(product(*options))
        total = lambda combo: sum(o[3] for o in combo)  # noqa: E731
        rank = lambda combo: (-round(sum(o[4] for o in combo), 6), total(combo), [o[1] for o in combo])  # noqa: E731
        strict = [c for c in combos if total(c) <= request.budget_total - allocation.contingency]
        warnings: List[str] = []
        if strict:
            pool = strict
        else:
            loose = [c for c in combos if total(c) <= request.budget_total]
            if not loose:
                cheapest = min(total(c) for c in combos)
                return [
                    ConstraintViolation(
                        constraint="total_budget",
                        detail=f"the cheapest eligible combination costs INR {cheapest:,}; budget is INR {request.budget_total:,}",
                        repair=f"raise the budget to at least INR {cheapest:,}, drop a service, or reduce the guest count",
                    )
                ]
            pool = loose
            warnings.append(
                f"contingency reserve of INR {allocation.contingency:,} is not fully available: cheapest combination "
                f"INR {min(total(c) for c in loose):,} of budget INR {request.budget_total:,}"
            )
        ranked = sorted(pool, key=rank)
        best, runners = ranked[0], ranked[1:3]
        cheapest = min(combos, key=lambda c: (total(c), rank(c)))

        selections: List[VendorSelection] = []
        for idx, c in enumerate(categories):
            o = best[idx]
            others = [x for x in options[idx] if x[1] != o[1]]
            alts = [
                Alternative(
                    option=x[2],
                    outcome=f"INR {x[3]:,}, score {x[4]}",
                    reason_not_chosen="the best affordable combination is higher-scoring overall"
                    if any(x[1] == r[idx][1] for r in ranked)
                    else "its combinations exceed the budget",
                )
                for x in others[:3]
            ]
            if o[7] is not None:
                for rej in sorted(by_cat[c].rejected, key=_miss_distance)[:2]:  # near misses first
                    alts.append(Alternative(option=rej.name, outcome="rejected", reason_not_chosen="; ".join(f"{k.name}: {k.detail}" for k in rej.failed)))
            confirmed = o[6] == BookingStatus.CONFIRMED
            justification = (
                f"{o[2]} is a user-confirmed booking and is kept."
                if confirmed
                else f"{o[2]} (INR {o[3]:,}, score {o[4]}) is part of the highest-scoring combination "
                f"(INR {total(best):,} total) that fits the budget."
            )
            if cheapest[idx][1] != o[1] and not confirmed:
                justification += f" The cheapest combination used {cheapest[idx][2]} (INR {cheapest[idx][3]:,}) but scored lower."
            selections.append(
                VendorSelection(
                    category=c,
                    vendor_id=o[1],
                    name=o[2],
                    cost=o[3],
                    booking_status=o[6],
                    score=o[4],
                    justification=justification,
                    alternatives=tuple(alts),
                    no_alternative_reason=None if alts else ("user-confirmed booking: the user already committed to this vendor" if confirmed else f"only {o[2]} exists in the database for {request.city}"),
                    source=o[5],
                )
            )
        self.logs.decisions.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.RECOMMENDATION,
            summary="Selected " + ", ".join(f"{s.category.value}: {s.name}" for s in selections) + f" for INR {total(best):,}",
            alternatives=tuple(
                Alternative(
                    option=" + ".join(o[2] for o in r),
                    outcome=f"INR {total(r):,}, score {round(sum(o[4] for o in r), 4)}",
                    reason_not_chosen="lower combined score than the selected combination",
                )
                for r in runners
            )
            + (
                (Alternative(option=" + ".join(o[2] for o in cheapest), outcome=f"INR {total(cheapest):,}", reason_not_chosen="cheapest, but lower combined score"),)
                if cheapest != best and cheapest not in runners
                else ()
            ),
            no_alternative_reason=None if len(ranked) > 1 or cheapest != best else "only one affordable combination exists",
            constraints_checked=(
                ConstraintCheck(name="total_budget", passed=True, detail=f"INR {total(best):,} <= INR {request.budget_total:,}"),
                ConstraintCheck(name="one_vendor_per_category", passed=True, detail=", ".join(c.value for c in categories)),
            ),
            justification=f"Exhaustive search over {len(combos)} combination(s); {len(pool)} affordable. Highest combined score wins, then lowest cost.",
            confidence=0.9 if len(ranked) > 1 else 0.6,
            source="rule:vendor_selection",
        )
        return tuple(selections), warnings

    def _fail(self, request, summary, violations) -> None:
        self.logs.decisions.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.FAILURE,
            summary=summary,
            constraints_checked=tuple(ConstraintCheck(name=v.constraint, passed=False, detail=v.detail) for v in violations),
            justification="; ".join(f"{v.constraint}: {v.detail}. Repair: {v.repair}" for v in violations),
            confidence=1.0,
            source="rule:planner",
        )
