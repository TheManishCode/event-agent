"""Budget Reality Check: an honest, labelled estimate from database records, before any planning."""

import pytest
from pydantic import ValidationError

from eventmate.agents.budget import BudgetPolicy
from eventmate.agents.manager import parse_request
from eventmate.reality import reality_check
from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY


def check(budget, repo, policy=None, text=EXAMPLE):
    r = parse_request(text.replace("200,000", f"{budget:,}"), today=TODAY, known_cities=repo.cities())
    return reality_check(r, repo, policy or BudgetPolicy())


@pytest.mark.parametrize(
    "budget,verdict",
    [(200_000, "comfortable"), (155_000, "tight"), (150_000, "contingency_breach"), (60_000, "unrealistic")],
)
def test_verdict_bands(repo, budget, verdict):
    c = check(budget, repo)
    assert c.verdict == verdict and c.floor == 139_000


def test_floor_is_the_global_cheapest_eligible_vendor_per_service(repo):
    c = check(200_000, repo)
    assert {(p.category.value, p.cost) for p in c.per_category} == {("venue", 55_000), ("caterer", 66_000), ("photographer", 18_000)}
    assert sum(p.cost for p in c.per_category) == c.floor


def test_suggested_minimum_is_exact(repo):
    c = check(60_000, repo)
    m = c.suggested_minimum_budget
    pol = BudgetPolicy()
    assert m - m * pol.contingency_bp // 10_000 >= c.floor
    assert (m - 1) - (m - 1) * pol.contingency_bp // 10_000 < c.floor


def test_zero_contingency_policy(repo):
    c = check(139_000, repo, BudgetPolicy(contingency_bp=0))
    assert c.verdict == "tight" and c.suggested_minimum_budget == 139_000


def test_every_estimate_is_labelled_with_its_basis(repo):
    c = check(200_000, repo)
    assert c.source.startswith("tool:") and "estimate" in c.basis.lower() and "database" in c.basis.lower()


def test_unknown_when_a_service_has_no_eligible_vendor(repo):
    c = check(200_000, repo, text=EXAMPLE.replace("120-person", "900-person"))
    assert c.verdict == "unknown" and c.floor is None and c.suggested_minimum_budget is None
    assert "venue" in c.message


def test_tiny_and_huge_budgets_do_not_crash(repo):
    assert check(1, repo).verdict == "unrealistic"
    assert check(9_999_999_999, repo).verdict == "comfortable"


def test_comfortable_threshold_is_validated_policy_data():
    with pytest.raises(ValidationError):
        BudgetPolicy(comfortable_bp=0)
    with pytest.raises(ValidationError):
        BudgetPolicy(comfortable_bp=10_001)


def test_intake_and_api_carry_the_check(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="rc")
    intake = em.manager.run_intake(EXAMPLE, today=TODAY)
    assert intake.reality.verdict == "comfortable"
    from eventmate.web.app import create_app

    client = create_app(em, today_fn=lambda: TODAY).test_client()
    tok = client.get("/api/session").get_json()["csrf"]
    import json

    h = {"X-EventMate-CSRF": tok, "Content-Type": "application/json"}
    ok = client.post("/api/plans", data=json.dumps({"request": EXAMPLE}), headers=h)
    bad = client.post("/api/plans", data=json.dumps({"request": EXAMPLE.replace("200,000", "60,000")}), headers=h)
    assert ok.status_code == 201 and ok.get_json()["reality"]["verdict"] == "comfortable"
    assert bad.status_code == 422 and bad.get_json()["reality"]["verdict"] == "unrealistic"
