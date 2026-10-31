import pytest
from pydantic import ValidationError

from eventmate.agents.budget import BudgetAgent, BudgetPolicy
from eventmate.agents.manager import parse_request
from eventmate.schemas import (
    BudgetAllocation,
    ConstraintCheck,
    RejectedVendor,
    VendorCandidate,
    VendorCategory,
    VendorShortlist,
)
from tests.conftest import EXAMPLE, TODAY


def req(text=EXAMPLE):
    return parse_request(text, today=TODAY, known_cities=("Pune",))


def test_example_allocation(logger):
    a = BudgetAgent(logger=logger).allocate(req())
    amounts = {l.category: l.amount for l in a.lines}
    assert set(amounts) == {"venue", "caterer", "photographer"}
    assert a.contingency == 20_000
    assert sum(amounts.values()) + a.contingency == 200_000
    assert amounts["caterer"] > amounts["venue"] > amounts["photographer"]


@pytest.mark.parametrize("total", [1, 7, 99, 1_001, 77_777, 199_999, 200_000, 9_999_999_999])
def test_allocation_always_sums_exactly(logger, total):
    r = req().model_copy(update={"budget_total": total})
    a = BudgetAgent(logger=logger).allocate(r)
    assert sum(l.amount for l in a.lines) + a.contingency == total
    assert all(l.amount >= 0 for l in a.lines)


def test_weights_renormalise_over_requested_services_only(logger):
    r = parse_request("Plan a 80 guests party in Pune on December 12 under INR 100,000.", today=TODAY)
    a = BudgetAgent(logger=logger).allocate(r)
    assert [l.category for l in a.lines] == ["venue"]
    assert a.lines[0].amount == 90_000 and a.contingency == 10_000


def test_allocation_is_deterministic(logger):
    ag = BudgetAgent(logger=logger)
    assert ag.allocate(req()) == ag.allocate(req())


def test_custom_policy_is_respected(logger):
    pol = BudgetPolicy(weights_bp={"venue": 5000, "caterer": 5000, "photographer": 0}, contingency_bp=0)
    a = BudgetAgent(logger=logger, policy=pol).allocate(req())
    assert a.contingency == 0 and a.cap_for(VendorCategory.PHOTOGRAPHER) == 0
    assert a.cap_for(VendorCategory.VENUE) == 100_000


def test_inconsistent_allocation_cannot_be_constructed():
    with pytest.raises(ValidationError):
        BudgetAllocation(total=100, lines=(), contingency=5)


def test_zero_negative_and_oversized_budgets_are_not_valid_requests():
    r = req()
    for bad in (0, -1, 10**11):
        with pytest.raises(ValidationError):
            type(r)(**{**r.model_dump(), "budget_total": bad})


def test_cap_lookup_for_unrequested_category_raises(logger):
    r = parse_request("Plan a 80 guests party in Pune on December 12 under INR 100,000.", today=TODAY)
    a = BudgetAgent(logger=logger).allocate(r)
    with pytest.raises(KeyError):
        a.cap_for(VendorCategory.CATERER)


def test_allocation_log_has_alternatives_and_source(logger):
    BudgetAgent(logger=logger).allocate(req())
    [e] = logger.read()
    assert len(e.alternatives) >= 1
    assert e.source == "rule:budget_policy"
    assert e.constraints_checked[0].passed


# ------------------------------------------------------------- assessment


def _cand(cid, cat, cost):
    return VendorCandidate(
        vendor_id=cid, name=cid, category=cat, estimated_cost=cost, score=0.5,
        checks=(ConstraintCheck(name="x", passed=True, detail="d"),), source="database:seed_vendors.json",
    )


def _sl(cat, cap, costs=(), rejected_cost=None):
    rej = ()
    if rejected_cost is not None:
        rej = (
            RejectedVendor(
                vendor_id="r", name="r", category=cat, estimated_cost=rejected_cost,
                failed=(ConstraintCheck(name="budget_cap", passed=False, detail="over"),),
            ),
        )
    return VendorShortlist(
        category=cat, budget_cap=cap,
        candidates=tuple(_cand(f"{cat.value}{i}", cat, c) for i, c in enumerate(costs)),
        rejected=rej,
    )


V, C, P = VendorCategory.VENUE, VendorCategory.CATERER, VendorCategory.PHOTOGRAPHER


def test_assess_within_budget(logger):
    ag = BudgetAgent(logger=logger)
    r = req()
    a = ag.allocate(r)
    res = ag.assess(r, a, {V: _sl(V, 70_000, [55_000]), C: _sl(C, 80_000, [66_000]), P: _sl(P, 20_000, [18_000])})
    assert res.status == "within_budget"
    assert res.cheapest_total == 139_000 and res.headroom == 61_000
    assert not res.violations


def test_assess_flags_overspend_with_violation_and_repair(logger):
    ag = BudgetAgent(logger=logger)
    r = req()
    a = ag.allocate(r)
    res = ag.assess(r, a, {V: _sl(V, 70_000, [90_000]), C: _sl(C, 80_000, [90_000]), P: _sl(P, 20_000, [30_000])})
    assert res.status == "overspend"
    [v] = res.violations
    assert v.constraint == "total_budget" and "210,000" in v.repair
    assert logger.read()[-1].decision_type.value == "failure"


def test_assess_flags_contingency_breach_as_warning(logger):
    ag = BudgetAgent(logger=logger)
    r = req()
    a = ag.allocate(r)
    res = ag.assess(r, a, {V: _sl(V, 70_000, [70_000]), C: _sl(C, 80_000, [80_000]), P: _sl(P, 20_000, [40_000])})
    assert res.status == "contingency_breach" and not res.violations


def test_assess_suggests_rebalance_when_only_the_cap_blocks(logger):
    ag = BudgetAgent(logger=logger)
    r = req()
    a = ag.allocate(r)
    res = ag.assess(r, a, {V: _sl(V, 70_000, [], rejected_cost=75_000), C: _sl(C, 80_000, [66_000]), P: _sl(P, 20_000, [18_000])})
    assert res.status == "incomplete"
    [v] = res.violations
    assert v.constraint == "category_cap" and "75,000" in v.repair


def test_assess_reports_total_budget_when_rebalance_cannot_help(logger):
    ag = BudgetAgent(logger=logger)
    r = req()
    a = ag.allocate(r)
    res = ag.assess(r, a, {V: _sl(V, 70_000, [], rejected_cost=150_000), C: _sl(C, 80_000, [90_000]), P: _sl(P, 20_000, [18_000])})
    [v] = res.violations
    assert v.constraint == "total_budget"


@pytest.mark.parametrize(
    "weights",
    [
        {"venue": 0, "caterer": 0, "photographer": 0},
        {"venue": 1},
        {"venue": -5, "caterer": 1, "photographer": 1},
        {"venue": 1, "caterer": 1, "photographer": 1, "florist": 1},
    ],
)
def test_invalid_policy_is_rejected_at_construction(weights):
    with pytest.raises(ValidationError):
        BudgetPolicy(weights_bp=weights)


def test_requested_services_with_zero_total_weight_fail_clearly(logger):
    pol = BudgetPolicy(weights_bp={"venue": 0, "caterer": 1, "photographer": 1})
    r = parse_request("Plan a 80 guests party in Pune on December 12 under INR 100,000.", today=TODAY)
    with pytest.raises(ValueError, match="weight"):
        BudgetAgent(logger=logger, policy=pol).allocate(r)


def test_incomplete_assessment_log_names_the_real_constraint_and_a_repair(logger):
    ag = BudgetAgent(logger=logger)
    r = req()
    a = ag.allocate(r)
    from eventmate.schemas import ConstraintViolation

    empty_v = VendorShortlist(
        category=V, budget_cap=70_000, candidates=(), rejected=(),
        violations=(ConstraintViolation(constraint="city", detail="none in city", repair="choose a different city"),),
    )
    res = ag.assess(r, a, {V: empty_v, C: _sl(C, 80_000, [66_000]), P: _sl(P, 20_000, [18_000])})
    assert res.status == "incomplete"
    e = logger.read()[-1]
    assert e.constraints_checked[0].name == "vendor_coverage" and not e.constraints_checked[0].passed
    assert "Repair: choose a different city" in e.justification


def test_request_cap_weights_override_the_policy_and_sum_exactly(logger):
    from eventmate.schemas import CapWeight

    r = req().with_changes(
        cap_weights=(
            CapWeight(category=VendorCategory.VENUE, weight=60),
            CapWeight(category=VendorCategory.CATERER, weight=30),
            CapWeight(category=VendorCategory.PHOTOGRAPHER, weight=10),
        )
    )
    a = BudgetAgent(logger=logger).allocate(r)
    amounts = {l.category: l.amount for l in a.lines}
    assert amounts["venue"] == 108_000 and amounts["caterer"] == 54_000 and amounts["photographer"] == 18_000
    assert sum(amounts.values()) + a.contingency == 200_000
