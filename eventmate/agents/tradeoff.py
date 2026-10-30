"""Trade-off Negotiator: ranked, verified ways out of a plan that cannot be built.

Proposes, never applies. Every option is a changed ``EventRequest`` priced from
database records and then verified by a full dry run of the real pipeline
(``verify``), so anything shown is something the Validator accepts. Harm
weights are data in ``TradeoffPolicy``, not hidden constants (ADR-003/015).

Moves: rebalance category caps, drop an optional service, shift the date to
one the database has availability for, raise the budget by the minimum that
works (binary search over the real feasibility check).
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import combinations
from typing import Callable, Dict, List, Optional, Tuple

from pydantic import Field

from eventmate.agents.budget import BudgetAgent, BudgetPolicy, compute_allocation
from eventmate.agents.manager import MAX_EVENT_AHEAD_DAYS
from eventmate.agents.vendor import VendorAgent, VendorRepository
from eventmate.decision_logger import DecisionLogger
from eventmate.pricing.reality import cheapest_eligible
from eventmate.schemas import (
    MAX_BUDGET_INR,
    AgentName,
    Alternative,
    CapWeight,
    ConstraintCheck,
    ConstraintViolation,
    DecisionType,
    EventRequest,
    TradeoffKind,
    TradeoffOption,
    TradeoffReport,
    VendorCategory,
    _Model,
)


class TradeoffPolicy(_Model):
    """How much each sacrifice hurts (0 = nothing, 1 = defeats the purpose). Tunable data."""

    drop_harm: Dict[str, float] = {"caterer": 0.8, "photographer": 0.3}
    date_harm_per_day: float = Field(default=0.02, ge=0.0, le=1.0)
    date_harm_max: float = Field(default=0.6, ge=0.0, le=1.0)
    rebalance_base_harm: float = Field(default=0.05, ge=0.0, le=1.0)
    rebalance_harm_max: float = Field(default=0.5, ge=0.0, le=1.0)
    max_options: int = Field(default=4, ge=1, le=10)
    max_date_candidates: int = Field(default=12, ge=1, le=60)


class _Silent:
    """Decision-logger stand-in for pricing probes: many what-ifs, no log noise."""

    entry_ids: List[str] = []

    def log(self, **_fields) -> None:
        return None


class _Candidate:
    __slots__ = ("kind", "request", "harm", "extra", "title", "detail", "total")

    def __init__(self, kind, request, harm, extra, title, detail, total):
        self.kind, self.request, self.harm, self.extra = kind, request, harm, extra
        self.title, self.detail, self.total = title, detail, total

    def utility(self, budget: int) -> float:
        return round((1.0 - self.harm) - self.extra / budget, 6)


class TradeoffAgent:
    agent_name = AgentName.BUDGET  # budget trade-offs; no separate agent identity in the log schema

    def __init__(
        self,
        repo: VendorRepository,
        logger: DecisionLogger,
        verify: Callable[[EventRequest], bool],
        budget_policy: Optional[BudgetPolicy] = None,
        policy: Optional[TradeoffPolicy] = None,
    ):
        self.repo, self.logger, self.verify = repo, logger, verify
        self.budget_policy = budget_policy or BudgetPolicy()
        self.policy = policy or TradeoffPolicy()

    # ------------------------------------------------------------- public

    def propose(self, request: EventRequest, today: date) -> TradeoffReport:
        if self.verify(request):
            report = TradeoffReport(status="not_needed", request=request)
            self._log(request, report, [])
            return report
        found = self._candidates(request, today)
        ranked = sorted(found, key=lambda c: (-c.utility(request.budget_total), c.extra, c.kind.value, c.title))
        options: List[TradeoffOption] = []
        rejected: List[_Candidate] = []
        for cand in ranked:
            if len(options) >= self.policy.max_options:
                break
            if not self.verify(cand.request):
                rejected.append(cand)
                continue
            options.append(self._option(request, cand, len(options) + 1))
        if options:
            report = TradeoffReport(status="options", request=request, options=tuple(options))
        else:
            report = TradeoffReport(
                status="no_option",
                request=request,
                violations=(
                    ConstraintViolation(
                        constraint="no_tradeoff",
                        detail="no cap rebalance, dropped service, date shift or budget raise produces a plan the Validator accepts",
                        repair="change the city, guest count or dietary needs, or add vendors for this city and date",
                    ),
                ),
            )
        self._log(request, report, rejected)
        return report

    # --------------------------------------------------------- candidates

    def _candidates(self, request: EventRequest, today: date) -> List[_Candidate]:
        out: List[_Candidate] = []
        out += self._rebalance(request)
        out += self._drops(request)
        out += self._dates(request, today)
        out += self._raise_budget(request)
        return out

    def _quick_ok(self, request: EventRequest) -> bool:
        """Cheap pre-filter: every service has an eligible vendor under the caps and the total fits."""
        silent = _Silent()
        budget = BudgetAgent(silent, self.budget_policy)
        vendor = VendorAgent(self.repo, silent)
        allocation = budget.allocate(request)
        shortlists = vendor.shortlist(request, allocation)
        return budget.assess(request, allocation, shortlists).status in ("within_budget", "contingency_breach")

    def _cheapest_by_category(self, request: EventRequest) -> Optional[Dict[VendorCategory, int]]:
        """Cheapest eligible price per service with the caps lifted, or None if a service has no eligible vendor."""
        found, missing = cheapest_eligible(self.repo, request)
        return None if missing is not None else {c: self.repo.price(p, request.guest_count) for c, p in found.items()}

    def _rebalanced(self, request: EventRequest) -> Optional[Tuple[EventRequest, float, str, int]]:
        """(request with cap_weights, harm, description, cheapest total) if re-splitting the caps makes it fit."""
        cheapest = self._cheapest_by_category(request)
        if cheapest is None:
            return None
        default = compute_allocation(request.with_changes(cap_weights=()), self.budget_policy)
        pool = default.total - default.contingency
        need = sum(cheapest.values())
        if need > pool:
            return None  # caps always sum to the pool; no split can fit these prices
        policy_caps = {l.category: l.amount for l in default.lines}
        surplus = pool - need
        wsum = sum(policy_caps.values()) or 1
        # cheapest price per category, plus the surplus spread in the policy's own proportions
        caps = {c: cheapest[c] + surplus * policy_caps[c.value] // wsum for c in request.services}
        caps[request.services[0]] += pool - sum(caps.values())  # rounding remainder, keeps caps == pool
        changed = request.with_changes(cap_weights=tuple(CapWeight(category=c, weight=caps[c]) for c in request.services))
        if not self._quick_ok(changed):
            return None
        taken = sum(max(0, policy_caps[c.value] - caps[c]) for c in request.services)
        harm = min(self.policy.rebalance_harm_max, self.policy.rebalance_base_harm + taken / request.budget_total)
        moves = ", ".join(f"{c.value} INR {policy_caps[c.value]:,} -> INR {caps[c]:,}" for c in request.services if policy_caps[c.value] != caps[c])
        return changed, round(harm, 4), moves, need

    def _rebalance(self, request: EventRequest) -> List[_Candidate]:
        found = self._rebalanced(request)
        if found is None:
            return []
        changed, harm, moves, need = found
        return [
            _Candidate(
                TradeoffKind.REBALANCE_CAPS,
                changed,
                harm,
                0,
                "Rebalance category budgets",
                f"Same budget, same services. Category caps change: {moves}. Cheapest eligible vendors in every category fit INR {need:,}.",
                need,
            )
        ]

    def _drops(self, request: EventRequest) -> List[_Candidate]:
        optional = [c for c in request.services if c != VendorCategory.VENUE]
        out: List[_Candidate] = []
        for n in range(1, len(optional) + 1):
            for dropped in combinations(optional, n):
                services = tuple(c for c in request.services if c not in dropped)
                changes: Dict[str, object] = {"services": services, "cap_weights": ()}
                changed = request.with_changes(**changes)
                if not self._quick_ok(changed):
                    continue
                harm = min(1.0, sum(self.policy.drop_harm.get(c.value, 0.5) for c in dropped))
                names = " and ".join(c.value for c in dropped)
                cheapest = self._cheapest_by_category(changed) or {}
                out.append(
                    _Candidate(
                        TradeoffKind.DROP_SERVICE,
                        changed,
                        round(harm, 4),
                        0,
                        f"Drop the {names}",
                        f"Plan without the {names}; you would arrange it yourself. Remaining services: {', '.join(c.value for c in services)}.",
                        sum(cheapest.values()),
                    )
                )
        return out

    def _dates(self, request: EventRequest, today: date) -> List[_Candidate]:
        horizon = today + timedelta(days=MAX_EVENT_AHEAD_DAYS)
        dates = {
            d
            for c in request.services
            for p in self.repo.providers(c)
            for d in p.available_dates
            if today < d <= horizon and d != request.event_date
        }
        out: List[_Candidate] = []
        for d in sorted(dates, key=lambda d: (abs((d - request.event_date).days), d)):
            if len(out) >= self.policy.max_date_candidates:
                break
            changed = request.with_changes(event_date=d)
            if not self._quick_ok(changed):
                continue
            days = abs((d - request.event_date).days)
            harm = min(self.policy.date_harm_max, days * self.policy.date_harm_per_day)
            cheapest = self._cheapest_by_category(changed) or {}
            out.append(
                _Candidate(
                    TradeoffKind.SHIFT_DATE,
                    changed,
                    round(harm, 4),
                    0,
                    f"Move the event to {d.isoformat()}",
                    f"{days} day(s) {'later' if d > request.event_date else 'earlier'} than {request.event_date.isoformat()}; "
                    "every service has an eligible vendor on that date.",
                    sum(cheapest.values()),
                )
            )
        return out

    def _fits(self, request: EventRequest) -> Optional[EventRequest]:
        """The request as-is, or with re-split caps, whichever the real checks accept."""
        if self._quick_ok(request):
            return request
        found = self._rebalanced(request)
        return found[0] if found else None

    def _raise_budget(self, request: EventRequest) -> List[_Candidate]:
        if self._fits(request) is not None:
            return []  # caps alone are the blocker; the rebalance option already covers it at no extra spend
        if self._fits(request.with_changes(budget_total=MAX_BUDGET_INR)) is None:
            return []  # money is not what blocks this request
        lo, hi = request.budget_total, MAX_BUDGET_INR  # lo infeasible, hi feasible
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self._fits(request.with_changes(budget_total=mid)) is not None:
                hi = mid
            else:
                lo = mid
        changed = self._fits(request.with_changes(budget_total=hi))
        extra = hi - request.budget_total
        rebalanced = bool(changed.cap_weights)
        harm = self._rebalanced(request.with_changes(budget_total=hi))[1] if rebalanced else 0.0
        cheapest = self._cheapest_by_category(changed) or {}
        return [
            _Candidate(
                TradeoffKind.RAISE_BUDGET,
                changed,
                harm,
                extra,
                f"Raise the budget to INR {hi:,}",
                f"The smallest budget that works is INR {hi:,}, INR {extra:,} more than you set"
                + (", with category caps re-split to fit the cheapest eligible vendors." if rebalanced else "."),
                sum(cheapest.values()),
            )
        ]

    # ------------------------------------------------------------ output

    def _option(self, original: EventRequest, cand: _Candidate, n: int) -> TradeoffOption:
        stem = original.request_id[:55]
        return TradeoffOption(
            option_id=f"{stem}-o{n}",
            kind=cand.kind,
            title=cand.title,
            detail=cand.detail,
            request=cand.request.with_changes(request_id=f"{stem}-o{n}"),
            preference_harm=cand.harm,
            extra_spend=cand.extra,
            utility=cand.utility(original.budget_total),
            estimated_total=cand.total,
        )

    def _log(self, request: EventRequest, report: TradeoffReport, rejected: List[_Candidate]) -> None:
        if report.status == "not_needed":
            summary, decision = "No trade-off needed: the request already plans", DecisionType.ASSESSMENT
        elif report.status == "options":
            summary, decision = f"Proposed {len(report.options)} verified trade-off option(s)", DecisionType.RECOMMENDATION
        else:
            summary, decision = "No verified trade-off exists", DecisionType.FAILURE
        alternatives = tuple(
            Alternative(option=c.title, outcome="failed the full dry run", reason_not_chosen="the Validator or planner rejected the resulting plan")
            for c in rejected[:5]
        ) + tuple(
            Alternative(option=o.title, outcome=f"harm {o.preference_harm}, extra INR {o.extra_spend:,}", reason_not_chosen="lower utility than the first option")
            for o in report.options[1:]
        )
        self.logger.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=decision,
            summary=summary,
            alternatives=alternatives,
            no_alternative_reason=None if alternatives or decision != DecisionType.RECOMMENDATION else "only one verified option exists",
            constraints_checked=(
                ConstraintCheck(name="dry_run_verified", passed=report.status != "no_option", detail=f"{len(report.options)} option(s) built a valid plan; {len(rejected)} candidate(s) failed"),
            ),
            justification="Options are ranked by utility = (1 - preference_harm) - extra_spend / budget. Harm weights come from "
            "TradeoffPolicy; prices come from database records; nothing was applied.",
            confidence=1.0,
            source="tool:tradeoff",
        )
