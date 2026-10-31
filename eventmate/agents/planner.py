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
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

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


class _Opt(NamedTuple):
    """One vendor option for a category: a shortlisted candidate, or the user's confirmed booking (candidate is None)."""

    category: VendorCategory
    vendor_id: str
    name: str
    cost: int
    score: float
    source: str
    status: BookingStatus
    candidate: Optional[VendorCandidate]


def _total(combo) -> int:
    return sum(o.cost for o in combo)


def _rank(combo) -> tuple:
    """Highest combined score first, then cheapest, then vendor ids (so ties are deterministic)."""
    return (-round(sum(o.score for o in combo), 6), _total(combo), [o.vendor_id for o in combo])


def _options_for(category, by_cat, forced) -> List[_Opt]:
    if category in forced:
        f = forced[category]
        return [_Opt(category, f.vendor_id, f.name, f.cost, f.score, f.source, f.booking_status, None)]
    return [_Opt(category, x.vendor_id, x.name, x.estimated_cost, x.score, x.source, BookingStatus.AVAILABLE, x) for x in by_cat[category].candidates]


def _affordable(request, allocation, combos):
    """(pool, warnings, violations): combinations that fit the budget with the reserve, else without it (with a warning), else a violation."""
    strict = [c for c in combos if _total(c) <= request.budget_total - allocation.contingency]
    if strict:
        return strict, [], None
    loose = [c for c in combos if _total(c) <= request.budget_total]
    if not loose:
        cheapest = min(_total(c) for c in combos)
        return None, [], [
            ConstraintViolation(
                constraint="total_budget",
                detail=f"the cheapest eligible combination costs INR {cheapest:,}; budget is INR {request.budget_total:,}",
                repair=f"raise the budget to at least INR {cheapest:,}, drop a service, or reduce the guest count",
            )
        ]
    warning = (
        f"contingency reserve of INR {allocation.contingency:,} is not fully available: cheapest combination "
        f"INR {min(_total(c) for c in loose):,} of budget INR {request.budget_total:,}"
    )
    return loose, [warning], None


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
        options = [_options_for(c, by_cat, forced) for c in categories]
        combos = list(product(*options))
        pool, warnings, violations = _affordable(request, allocation, combos)
        if violations:
            return violations
        ranked = sorted(pool, key=_rank)
        best, runners = ranked[0], ranked[1:3]
        cheapest = min(combos, key=lambda c: (_total(c), _rank(c)))
        selections = [self._selection(request, c, best, cheapest, options[idx], idx, ranked, by_cat.get(c)) for idx, c in enumerate(categories)]
        self._log_selection(request, categories, selections, best, runners, cheapest, ranked, len(combos), len(pool))
        return tuple(selections), warnings

    @staticmethod
    def _selection(request, category, best, cheapest, opts, idx, ranked, shortlist) -> VendorSelection:
        """One chosen vendor with its honest alternatives: the other candidates and the nearest rejections."""
        o = best[idx]
        alts = [
            Alternative(
                option=x.name,
                outcome=f"INR {x.cost:,}, score {x.score}",
                reason_not_chosen="the best affordable combination is higher-scoring overall"
                if any(x.vendor_id == r[idx].vendor_id for r in ranked)
                else "its combinations exceed the budget",
            )
            for x in [x for x in opts if x.vendor_id != o.vendor_id][:3]
        ]
        if o.candidate is not None:
            for rej in sorted(shortlist.rejected, key=_miss_distance)[:2]:  # near misses first
                alts.append(Alternative(option=rej.name, outcome="rejected", reason_not_chosen="; ".join(f"{k.name}: {k.detail}" for k in rej.failed)))
        confirmed = o.status == BookingStatus.CONFIRMED
        justification = (
            f"{o.name} is a user-confirmed booking and is kept."
            if confirmed
            else f"{o.name} (INR {o.cost:,}, score {o.score}) is part of the highest-scoring combination "
            f"(INR {_total(best):,} total) that fits the budget."
        )
        if cheapest[idx].vendor_id != o.vendor_id and not confirmed:
            justification += f" The cheapest combination used {cheapest[idx].name} (INR {cheapest[idx].cost:,}) but scored lower."
        return VendorSelection(
            category=category,
            vendor_id=o.vendor_id,
            name=o.name,
            cost=o.cost,
            booking_status=o.status,
            score=o.score,
            justification=justification,
            alternatives=tuple(alts),
            no_alternative_reason=None if alts else ("user-confirmed booking: the user already committed to this vendor" if confirmed else f"only {o.name} exists in the database for {request.city}"),
            source=o.source,
        )

    def _log_selection(self, request, categories, selections, best, runners, cheapest, ranked, n_combos, n_pool) -> None:
        self.logs.decisions.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.RECOMMENDATION,
            summary="Selected " + ", ".join(f"{s.category.value}: {s.name}" for s in selections) + f" for INR {_total(best):,}",
            alternatives=tuple(
                Alternative(
                    option=" + ".join(o.name for o in r),
                    outcome=f"INR {_total(r):,}, score {round(sum(o.score for o in r), 4)}",
                    reason_not_chosen="lower combined score than the selected combination",
                )
                for r in runners
            )
            + (
                (Alternative(option=" + ".join(o.name for o in cheapest), outcome=f"INR {_total(cheapest):,}", reason_not_chosen="cheapest, but lower combined score"),)
                if cheapest != best and cheapest not in runners
                else ()
            ),
            no_alternative_reason=None if len(ranked) > 1 or cheapest != best else "only one affordable combination exists",
            constraints_checked=(
                ConstraintCheck(name="total_budget", passed=True, detail=f"INR {_total(best):,} <= INR {request.budget_total:,}"),
                ConstraintCheck(name="one_vendor_per_category", passed=True, detail=", ".join(c.value for c in categories)),
            ),
            justification=f"Exhaustive search over {n_combos} combination(s); {n_pool} affordable. Highest combined score wins, then lowest cost.",
            confidence=0.9 if len(ranked) > 1 else 0.6,
            source="rule:vendor_selection",
        )

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
