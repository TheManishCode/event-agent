"""Budget Reality Check: is this budget realistic for these guests, city, date and services?

A pure function over database records. The floor is the cheapest *eligible* vendor per
requested service (caps lifted), so it is a lower bound, not a quote; the output says so.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from eventmate.agents.budget import BudgetPolicy
from eventmate.agents.vendor import Provider, VendorRepository, evaluate
from eventmate.schemas import CategoryFloor, EventRequest, RealityCheck, VendorCategory

NO_CAP = 10**12


def cheapest_eligible(repo: VendorRepository, request: EventRequest) -> Tuple[Dict[VendorCategory, Provider], Optional[VendorCategory]]:
    """Cheapest eligible provider per service (category caps lifted); second value names a service with none."""
    found: Dict[VendorCategory, Provider] = {}
    for category in request.services:
        eligible = [
            p for p in repo.providers(category)
            if all(c.passed for c in evaluate(p, request, category, NO_CAP, price=repo.price(p, request.guest_count), quote_expired=repo.quote_expired(p)))
        ]
        if not eligible:
            return found, category
        found[category] = min(eligible, key=lambda p: (repo.price(p, request.guest_count), p.id))
    return found, None


def _minimum_budget(floor: int, contingency_bp: int) -> int:
    budget = -(-floor * 10_000 // (10_000 - contingency_bp))
    while budget - budget * contingency_bp // 10_000 < floor:
        budget += 1
    while budget > 1 and (budget - 1) - (budget - 1) * contingency_bp // 10_000 >= floor:
        budget -= 1
    return max(1, budget)


def reality_check(request: EventRequest, repo: VendorRepository, policy: BudgetPolicy) -> RealityCheck:
    budget = request.budget_total
    found, missing = cheapest_eligible(repo, request)
    basis = (
        "Estimate, not a quote: the cheapest eligible vendor per requested service in the database "
        f"(development seed data) for {request.guest_count} guests in {request.city} on {request.event_date.isoformat()}."
    )
    if missing is not None:
        return RealityCheck(
            verdict="unknown",
            budget=budget,
            message=f"Cannot judge the budget: no eligible {missing.value} exists for these requirements, so no price floor can be computed.",
            basis=basis,
        )
    rows = tuple(
        CategoryFloor(category=c, vendor_id=p.id, vendor_name=p.name, cost=repo.price(p, request.guest_count)) for c, p in found.items()
    )
    floor = sum(r.cost for r in rows)
    pool = budget - budget * policy.contingency_bp // 10_000
    minimum = _minimum_budget(floor, policy.contingency_bp)
    if floor > budget:
        verdict, msg = "unrealistic", f"Not realistic: even the cheapest eligible vendors cost INR {floor:,}, above your INR {budget:,}."
    elif floor > pool:
        verdict, msg = "contingency_breach", f"Very tight: the cheapest set (INR {floor:,}) fits only by spending the contingency reserve."
    elif floor * 10_000 > pool * policy.comfortable_bp:
        verdict, msg = "tight", f"Tight: the cheapest set (INR {floor:,}) uses {floor * 100 // pool}% of the budget after contingency."
    else:
        verdict, msg = "comfortable", f"Realistic: the cheapest set (INR {floor:,}) uses {floor * 100 // pool}% of the budget after contingency."
    if verdict in ("unrealistic", "contingency_breach", "tight"):
        msg += f" A budget of INR {minimum:,} keeps the contingency reserve intact."
    return RealityCheck(verdict=verdict, budget=budget, floor=floor, per_category=rows, suggested_minimum_budget=minimum, message=msg, basis=basis)
