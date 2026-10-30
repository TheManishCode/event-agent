"""Budget Agent: allocates the total budget and flags overspend.

Allocation is pure integer arithmetic (basis points, largest-remainder
rounding) so the lines plus contingency always sum exactly to the budget.
Prices come from vendor records via ``cost_for``; this agent never invents one.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import Field, field_validator

from eventmate.decision_logger import DecisionLogger
from eventmate.schemas import (
    AgentName,
    Alternative,
    BudgetAllocation,
    BudgetAssessment,
    BudgetLine,
    ConstraintCheck,
    ConstraintViolation,
    DecisionType,
    EventRequest,
    EventType,
    ShortlistMap,
    VendorCategory,
    _Model,
)


class BudgetPolicy(_Model):
    """Allocation weights in basis points (1 bp = 0.01%). Tunable, not prompt-driven."""

    weights_bp: Dict[str, int] = {"venue": 3500, "caterer": 4000, "photographer": 1200}
    contingency_bp: int = Field(default=1000, ge=0, lt=10_000)
    # "comfortable" = the cheapest eligible set uses at most this share of the budget left after contingency
    comfortable_bp: int = Field(default=8500, gt=0, le=10_000)

    def for_request(self, request: EventRequest) -> "BudgetPolicy":
        """A community event uses its own profile's shares (data, adjustable by the user through cap_weights), not the wedding-style default."""
        if request.event_type != EventType.COMMUNITY:
            return self
        from eventmate.community import profile_for

        return self.model_copy(update={"weights_bp": dict(profile_for(request).weights)})

    @field_validator("weights_bp")
    @classmethod
    def _weights_valid(cls, v: Dict[str, int]) -> Dict[str, int]:
        expected = {c.value for c in VendorCategory}
        if set(v) != expected:
            raise ValueError(f"weights_bp must define exactly {sorted(expected)}")
        if any(w < 0 for w in v.values()):
            raise ValueError("weights must be non-negative")
        if sum(v.values()) <= 0:
            raise ValueError("at least one weight must be positive")
        return v


def compute_allocation(request: EventRequest, policy: BudgetPolicy) -> BudgetAllocation:
    """Pure allocation: request ``cap_weights`` win over the policy split. Lines + contingency == total."""
    policy = policy.for_request(request)
    total = request.budget_total
    contingency = total * policy.contingency_bp // 10_000
    pool = total - contingency
    cats = [c.value for c in request.services]
    chosen = {w.category.value: w.weight for w in request.cap_weights}
    weights = [chosen[c] if chosen else policy.weights_bp[c] for c in cats]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError("requested services have zero total allocation weight")
    amounts = [pool * w // weight_sum for w in weights]
    leftover = pool - sum(amounts)
    # largest remainder: hand the stray rupees to the biggest fractional parts
    order = sorted(range(len(cats)), key=lambda i: (-(pool * weights[i] % weight_sum), i))
    for i in order[:leftover]:
        amounts[i] += 1
    lines = tuple(BudgetLine(category=c, amount=a, share=round(a / total, 4)) for c, a in zip(cats, amounts))
    return BudgetAllocation(total=total, lines=lines, contingency=contingency)


class BudgetAgent:
    agent_name = AgentName.BUDGET

    def __init__(self, logger: DecisionLogger, policy: Optional[BudgetPolicy] = None):
        self.logger = logger
        self.policy = policy or BudgetPolicy()

    def allocate(self, request: EventRequest) -> BudgetAllocation:
        allocation = compute_allocation(request, self.policy)
        self._log_allocation(request, allocation)
        return allocation

    def assess(self, request: EventRequest, allocation: BudgetAllocation, shortlists: ShortlistMap) -> BudgetAssessment:
        """Check the cheapest eligible vendor set against the total budget."""
        empty = [c for c, s in shortlists.items() if not s.candidates]
        if empty:
            violations = self._rebalance_violations(request, allocation, shortlists, empty)
            assessment = BudgetAssessment(status="incomplete", violations=tuple(violations))
        else:
            cheapest = sum(min(c.estimated_cost for c in s.candidates) for s in shortlists.values())
            headroom = allocation.total - cheapest
            if cheapest > allocation.total:
                status = "overspend"
                violations = [
                    ConstraintViolation(
                        constraint="total_budget",
                        detail=f"cheapest eligible set costs INR {cheapest:,} > budget INR {allocation.total:,}",
                        repair=f"raise the budget to at least INR {cheapest:,} or drop optional services",
                    )
                ]
            elif cheapest > allocation.total - allocation.contingency:
                status = "contingency_breach"
                violations = []
            else:
                status, violations = "within_budget", []
            assessment = BudgetAssessment(
                status=status, cheapest_total=cheapest, headroom=headroom, violations=tuple(violations)
            )
        self._log_assessment(request, allocation, assessment, shortlists)
        return assessment

    # ----------------------------------------------------------- helpers

    def _rebalance_violations(self, request, allocation, shortlists, empty) -> List[ConstraintViolation]:
        """If an empty category is blocked only by its cap, say whether moving money fixes it."""
        usable = allocation.total - allocation.contingency
        out: List[ConstraintViolation] = []
        for cat in empty:
            budget_only = [
                r for r in shortlists[cat].rejected if {c.name for c in r.failed} == {"budget_cap"}
            ]
            if not budget_only:
                continue  # blocked by something money cannot fix; the vendor shortlist already explains it
            needed = min(r.estimated_cost for r in budget_only)
            others = sum(
                min(c.estimated_cost for c in s.candidates)
                for k, s in shortlists.items()
                if k != cat and s.candidates
            )
            other_empty = any(k != cat for k in empty)
            if not other_empty and others + needed <= usable:
                out.append(
                    ConstraintViolation(
                        constraint="category_cap",
                        detail=f"{cat.value} needs INR {needed:,} but its cap is INR "
                        f"{shortlists[cat].budget_cap:,}; the other categories leave room",
                        repair=f"rebalance: raise the {cat.value} cap to INR {needed:,} and lower other caps",
                    )
                )
            else:
                out.append(
                    ConstraintViolation(
                        constraint="total_budget",
                        detail=f"cheapest eligible {cat.value} (INR {needed:,}) does not fit with the "
                        f"other categories in INR {usable:,}",
                        repair=f"raise the budget or relax {cat.value} requirements",
                    )
                )
        return out

    def _log_allocation(self, request: EventRequest, allocation: BudgetAllocation) -> None:
        n = len(allocation.lines)
        equal = allocation.total * (10_000 - self.policy.contingency_bp) // 10_000 // n
        self.logger.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.ALLOCATION,
            summary="Allocated budget "
            + ", ".join(f"{l.category} INR {l.amount:,}" for l in allocation.lines)
            + f", contingency INR {allocation.contingency:,}",
            alternatives=(
                Alternative(
                    option="equal split across requested services",
                    outcome=f"INR {equal:,} each",
                    reason_not_chosen="ignores that catering scales with guest count and photography is a small flat fee",
                ),
                Alternative(
                    option="no contingency reserve",
                    outcome=f"INR {allocation.contingency:,} extra spread across lines",
                    reason_not_chosen="leaves no buffer for price drift or last-minute changes",
                ),
            ),
            constraints_checked=(
                ConstraintCheck(
                    name="sum_equals_total",
                    passed=True,
                    detail=f"lines + contingency = INR {allocation.total:,}",
                ),
            ),
            justification="Weights come from BudgetPolicy (basis points) renormalised over the requested services; "
            "remainders use largest-remainder rounding so the total is exact.",
            confidence=0.8,
            source="rule:budget_policy",
        )

    def _log_assessment(self, request, allocation, assessment: BudgetAssessment, shortlists) -> None:
        if assessment.status == "incomplete":
            blocked = [c for c, sl in shortlists.items() if not sl.candidates]
            repairs = [v for c in blocked for v in shortlists[c].violations] + list(assessment.violations)
            checks = (
                ConstraintCheck(
                    name="vendor_coverage",
                    passed=False,
                    detail="no eligible vendor for: " + ", ".join(c.value for c in blocked) + "; total cannot be assessed",
                ),
            )
            justification = "; ".join(f"{v.constraint}: {v.detail}. Repair: {v.repair}" for v in repairs)
        else:
            checks = (
                ConstraintCheck(
                    name="total_budget",
                    passed=assessment.status != "overspend",
                    detail=f"cheapest eligible total INR {assessment.cheapest_total:,} vs budget INR {allocation.total:,}",
                ),
            )
            justification = (
                "; ".join(f"{v.constraint}: {v.detail}. Repair: {v.repair}" for v in assessment.violations)
                or "Cheapest eligible vendor combination compared with the total budget and contingency reserve."
            )
        self.logger.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.FAILURE if assessment.status in ("overspend", "incomplete") else DecisionType.ASSESSMENT,
            summary=f"Budget assessment: {assessment.status}",
            constraints_checked=checks,
            justification=justification,
            confidence=1.0,
            source="rule:budget_assessment",
        )
