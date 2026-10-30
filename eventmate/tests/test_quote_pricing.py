"""F9 part 2: recorded quotes drive prices everywhere; an expired quote fails closed (ADR-026)."""

from datetime import date, datetime, timezone

import pytest

from eventmate.schemas import VendorCategory as C
from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY

RASOI = "cat-pune-rasoi"  # the caterer the example plan picks at INR 550/plate


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="qp")


def rec(em, vendor=RASOI, category=C.CATERER, **kw):
    base = dict(source_type="user_input", entered_by="asha", observed_at=datetime(2026, 10, 30, tzinfo=timezone.utc),
                expires_at=date(2026, 11, 20), confidence=0.8, amount_inr=500, basis="per_plate", guest_count=100)
    base.update(kw)
    return em.observe_quote(vendor, category, **base)


def caterer(plan):
    return next(s for s in plan.selections if s.category == C.CATERER)


def test_no_quote_means_seed_price(em):
    assert caterer(em.plan(EXAMPLE, today=TODAY).plan).cost == 66_000


def test_valid_per_plate_quote_reprices_the_plan_for_the_current_guest_count(em):
    rec(em, hidden_costs=({"label": "GST", "amount_inr": 3_000},))
    out = em.plan(EXAMPLE, today=TODAY)
    assert out.status == "planned" and caterer(out.plan).vendor_id == RASOI
    assert caterer(out.plan).cost == 500 * 120 + 3_000  # per plate x 120 guests + known hidden cost


def test_total_quote_is_used_as_is(em):
    rec(em, basis="total", guest_count=None, amount_inr=70_000)
    sl = em.manager.vendor.shortlist_category(em.manager.parse(EXAMPLE, TODAY), C.CATERER, 10**9)
    assert next(c for c in sl.candidates + tuple() if c.vendor_id == RASOI).estimated_cost == 70_000


def test_expired_quote_fails_closed_and_the_vendor_is_not_chosen(em):
    rec(em, expires_at=date(2026, 10, 30))
    out = em.plan(EXAMPLE, today=TODAY)
    assert caterer(out.plan).cost == 500 * 120  # still valid on its last day
    rec(em, vendor="cat-pune-spice", expires_at=date(2026, 10, 30))
    out = em.plan(EXAMPLE, today=date(2026, 11, 2))
    assert out.status == "planned"
    assert all(s.vendor_id not in (RASOI, "cat-pune-spice") for s in out.plan.selections if s.category == C.CATERER)


def test_expired_quote_is_named_in_the_rejection_with_a_requote_repair(em):
    rec(em, expires_at=date(2026, 10, 30))
    em.repo.clock = lambda: date(2026, 11, 2)
    sl = em.manager.vendor.shortlist_category(em.manager.parse(EXAMPLE, TODAY), C.CATERER, 10**9)
    bad = next(r for r in sl.rejected if r.vendor_id == RASOI)
    assert any(c.name == "quote_valid" and "re-quote needed" in c.detail for c in bad.failed)


def test_public_listing_never_sets_a_price(em):
    rec(em, source_type="public_listing", amount_inr=100)
    assert caterer(em.plan(EXAMPLE, today=TODAY).plan).cost == 66_000


def test_latest_quote_wins(em):
    rec(em, amount_inr=520)
    rec(em, amount_inr=480)
    assert caterer(em.plan(EXAMPLE, today=TODAY).plan).cost == 480 * 120


def test_validator_rejects_a_plan_priced_off_the_quote_it_ignores(em):
    out = em.plan(EXAMPLE, today=TODAY)
    pid = out.plan.plan_id
    rec(em, amount_inr=400)  # a cheaper quote arrives after the plan was made
    report = em.revalidate(pid)
    assert not report.passed and any("cost_matches_database" in c.name and not c.passed for c in report.checks)


def test_per_plate_quote_for_a_venue_is_refused(em):
    venue = next(p.id for p in em.repo.providers(C.VENUE))
    with pytest.raises(ValueError, match="per_plate"):
        rec(em, vendor=venue, category=C.VENUE)


def test_reality_check_uses_quotes(em):
    rec(em, vendor="cat-pune-annapurna", amount_inr=100)  # cheapest eligible caterer drops
    em.repo.clock = lambda: TODAY
    from eventmate.reality import reality_check
    req = em.manager.parse(EXAMPLE, TODAY)
    rc = reality_check(req, em.repo, em.manager.budget.policy)
    floor = next(f for f in rc.per_category if f.category == C.CATERER)
    assert floor.vendor_id == "cat-pune-annapurna" and floor.cost == 12_000


# ---------------------------------------------------- F9 part 3: status, vendor-confirmation label, re-price repair


def plan_id(em):
    return em.plan(EXAMPLE, today=TODAY).plan.plan_id


def status(em, pid, category=C.CATERER):
    return next(s for s in em.quote_status(pid, TODAY) if s.category == category)


def test_status_without_quotes_is_neutral(em):
    s = status(em, plan_id(em))
    assert s.report is None and s.confirmed_by is None and not s.price_stale and s.current_price == s.plan_cost


def test_new_quote_marks_the_plan_price_stale_and_replan_reprices_it(em):
    pid = plan_id(em)
    rec(em, amount_inr=400)
    s = status(em, pid)
    assert s.price_stale and s.current_price == 400 * 120 and s.plan_cost == 66_000
    from eventmate.schemas import Disruption, DisruptionKind
    out = em.replan(pid, Disruption(kind=DisruptionKind.BUDGET_CHANGE, new_budget=200_000), TODAY)
    assert out.status == "replanned", out
    assert not status(em, pid).price_stale and em.revalidate(pid).passed


def test_confirmed_by_you_when_only_the_user_said_so(em):
    pid = plan_id(em)
    em.confirm(pid, C.CATERER)
    assert status(em, pid).confirmed_by == "you"


def test_confirmed_by_vendor_needs_a_vendor_confirmation_with_evidence(em):
    pid = plan_id(em)
    em.confirm(pid, C.CATERER)
    rec(em, source_type="vendor_quote", evidence_ref="email: quote")  # a quote is not a confirmation
    assert status(em, pid).confirmed_by == "you"
    rec(em, source_type="vendor_confirmation", evidence_ref="whatsapp msg 4411")
    assert status(em, pid).confirmed_by == "vendor"


def test_a_vendor_confirmation_does_not_confirm_an_unconfirmed_selection(em):
    pid = plan_id(em)
    rec(em, source_type="vendor_confirmation", evidence_ref="whatsapp msg 4411")
    assert status(em, pid).confirmed_by is None  # the plan still says proposed; only the user confirms


def test_api_exposes_status_and_rejects_hostile_plan_ids(em):
    from eventmate.web.app import create_app
    pid = plan_id(em)
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    rec(em, amount_inr=400)
    got = c.get(f"/api/plans/{pid}/quotes").get_json()
    row = next(r for r in got if r["category"] == "caterer")
    assert row["price_stale"] is True and row["report"]["effective_total"] == 40_000 and row["current_price"] == 48_000  # report = as quoted (100 plates); current = this plan (120)
    assert c.get("/api/plans/..%2Fx/quotes").status_code in (400, 404)
    assert c.get("/api/plans/doesnotexist/quotes").status_code in (400, 404)
