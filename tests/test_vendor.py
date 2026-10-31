import json
from datetime import date

import pytest
from pydantic import ValidationError

from eventmate.agents.budget import BudgetAgent
from eventmate.agents.manager import parse_request
from eventmate.agents.vendor import DATA_DIR, DataIntegrityError, VendorAgent, VendorRepository, evaluate
from eventmate.schemas import (
    BookingStatus,
    DecisionType,
    Vendor,
    VendorCategory,
)
from tests.conftest import EXAMPLE, TODAY


@pytest.fixture
def request_():
    return parse_request(EXAMPLE, today=TODAY, known_cities=("Pune",))


@pytest.fixture
def shortlists(repo, logger, request_):
    alloc = BudgetAgent(logger=logger).allocate(request_)
    return VendorAgent(repo, logger).shortlist(request_, alloc)


def ids(s):
    return [c.vendor_id for c in s.candidates]


def rejected(s):
    return {r.vendor_id: {c.name for c in r.failed} for r in s.rejected}


def test_venue_shortlist_filters(shortlists):
    s = shortlists[VendorCategory.VENUE]
    assert set(ids(s)) == {"ven-pune-heritage", "ven-pune-orchid"}
    rej = rejected(s)
    assert rej["ven-pune-skyline"] == {"budget_cap"}
    assert rej["ven-pune-lawns"] == {"venue_setting"}
    assert rej["ven-pune-loft"] == {"capacity"}
    assert rej["ven-pune-riverside"] == {"date_availability"}
    assert rej["ven-pune-mahal"] == {"booking_status"}
    assert rej["ven-mum-bay"] == {"city"}


def test_caterer_shortlist_filters(shortlists):
    s = shortlists[VendorCategory.CATERER]
    assert set(ids(s)) == {"cat-pune-annapurna", "cat-pune-rasoi"}
    rej = rejected(s)
    assert rej["cat-pune-spice"] == {"budget_cap"}
    assert rej["cat-pune-darbar"] == {"dietary", "budget_cap"}  # every reason is reported
    assert rej["cat-pune-tiffins"] == {"capacity"}
    assert rej["cat-pune-saffron"] == {"date_availability"}
    assert rej["cat-mum-marine"] == {"city"}


def test_photographer_shortlist_filters(shortlists):
    s = shortlists[VendorCategory.PHOTOGRAPHER]
    assert set(ids(s)) == {"pho-pune-candid", "pho-pune-lens"}
    rej = rejected(s)
    assert rej["pho-pune-cinema"] == {"budget_cap"}
    assert rej["pho-pune-moments"] == {"date_availability"}
    assert rej["pho-mum-shutter"] == {"city"}


def test_every_candidate_passes_every_check_and_is_within_cap(shortlists):
    for s in shortlists.values():
        for c in s.candidates:
            assert all(k.passed for k in c.checks)
            assert c.estimated_cost <= s.budget_cap


def test_caterer_cost_is_per_plate_times_guests(shortlists):
    top = {c.vendor_id: c for c in shortlists[VendorCategory.CATERER].candidates}
    assert top["cat-pune-annapurna"].estimated_cost == 650 * 120


def test_ranking_is_deterministic_and_best_first(repo, logger, request_):
    alloc = BudgetAgent(logger=logger).allocate(request_)
    a = VendorAgent(repo, logger).shortlist(request_, alloc)
    b = VendorAgent(repo, logger).shortlist(request_, alloc)
    assert a == b
    for s in a.values():
        scores = [c.score for c in s.candidates]
        assert scores == sorted(scores, reverse=True)


def test_rejection_lists_all_reasons_not_just_first(repo, logger, request_):
    cat = next(p for p in repo.providers(VendorCategory.CATERER) if p.id == "cat-mum-marine")
    checks = evaluate(cat, request_, VendorCategory.CATERER, cap=1)
    assert {c.name for c in checks if not c.passed} == {"city", "budget_cap"}


def test_city_match_is_case_and_space_insensitive(repo, logger, request_):
    req = request_.model_copy(update={"city": "  PUNE "})
    s = VendorAgent(repo, logger).shortlist_category(req, VendorCategory.VENUE, 72_000)
    assert ids(s)


def test_max_candidates_limits_and_logs_the_overflow(repo, logger, request_):
    s = VendorAgent(repo, logger, max_candidates=1).shortlist_category(request_, VendorCategory.VENUE, 72_000)
    assert len(s.candidates) == 1
    entry = logger.read()[-1]
    assert any("shortlist size" in a.reason_not_chosen for a in entry.alternatives)


def test_logs_have_alternatives_constraints_source_and_confidence(logger, shortlists):
    entries = [e for e in logger.read() if e.agent.value == "vendor"]
    assert len(entries) == 3
    for e in entries:
        assert e.decision_type == DecisionType.RECOMMENDATION
        assert e.alternatives and e.constraints_checked and e.justification
        assert e.source.startswith("database:")
        assert 0 < e.confidence <= 1


def test_empty_shortlist_reports_violation_and_repair_and_logs_failure(repo, logger, request_):
    s = VendorAgent(repo, logger).shortlist_category(request_, VendorCategory.VENUE, 50_000)
    assert not s.candidates
    [v] = s.violations
    assert v.constraint == "budget_cap" and "55,000" in v.repair  # cheapest otherwise-eligible venue
    assert logger.read()[-1].decision_type == DecisionType.FAILURE


def test_date_repair_suggests_real_available_dates(repo, logger, request_):
    req = request_.model_copy(update={"event_date": date(2026, 12, 13)})
    s = VendorAgent(repo, logger).shortlist_category(req, VendorCategory.VENUE, 72_000)
    [v] = s.violations
    assert v.constraint == "date_availability"
    assert "2026-12-12" in v.repair  # only dates present in the DB may be suggested


def test_unknown_city_yields_no_candidates_not_invented_ones(repo, logger, request_):
    req = request_.model_copy(update={"city": "Atlantis"})
    for cat in VendorCategory:
        s = VendorAgent(repo, logger).shortlist_category(req, cat, 10**9)
        assert not s.candidates and s.violations


@pytest.mark.parametrize("status", [BookingStatus.HELD, BookingStatus.CONFIRMED, BookingStatus.CANCELLED])
def test_non_available_status_is_never_shortlisted(tmp_path, logger, request_, status):
    src = json.loads((DATA_DIR / "seed_venues.json").read_text())
    for r in src["records"]:
        r["booking_status"] = status.value
    p = tmp_path / "v.json"
    p.write_text(json.dumps(src))
    repo = VendorRepository(venues_path=p)
    s = VendorAgent(repo, logger).shortlist_category(request_, VendorCategory.VENUE, 10**9)
    assert not s.candidates


def test_repository_is_read_only_view(repo):
    with pytest.raises(ValidationError):
        repo.providers(VendorCategory.VENUE)[0].name = "x"
    assert isinstance(repo.providers(VendorCategory.VENUE), tuple)


def test_seed_data_is_valid_and_ids_unique(repo):
    assert "Pune" in repo.cities()
    all_ids = [p.id for c in VendorCategory for p in repo.providers(c)]
    assert len(all_ids) == len(set(all_ids))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["records"][0].update(capacity=-5),
        lambda d: d["records"][0].update(unexpected="x"),
        lambda d: d["records"][0].update(source="trust me"),
        lambda d: d["records"][0].update(id="../x"),
        lambda d: d["records"][0].update(available_dates=["not-a-date"]),
        lambda d: d["records"][0].update(rating=9),
        lambda d: d["records"].append(dict(d["records"][0])),  # duplicate id
        lambda d: d.update(schema_version=2),
        lambda d: d.update(records="nope"),
    ],
)
def test_malformed_seed_data_fails_closed(tmp_path, mutate):
    doc = json.loads((DATA_DIR / "seed_venues.json").read_text())
    mutate(doc)
    p = tmp_path / "v.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(DataIntegrityError):
        VendorRepository(venues_path=p)


def test_non_json_and_missing_files_fail_closed(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{oops")
    with pytest.raises(DataIntegrityError):
        VendorRepository(venues_path=p)
    with pytest.raises(DataIntegrityError):
        VendorRepository(venues_path=tmp_path / "missing.json")


def test_vendor_model_enforces_pricing_by_category():
    base = dict(id="x", name="n", city="Pune", rating=4, source="database:x.json")
    with pytest.raises(ValueError):
        Vendor(category="caterer", **base)  # no pricing
    with pytest.raises(ValueError):
        Vendor(category="photographer", price_per_plate=5, flat_fee=1, **base)
    with pytest.raises(ValueError):
        Vendor(category="caterer", price_per_plate=5, min_guests=10, max_guests=5, **base)


def test_excluded_vendors_are_rejected_with_a_clear_reason(repo, logger, request_):
    s = VendorAgent(repo, logger).shortlist_category(request_, VendorCategory.VENUE, 72_000, exclude_ids={"ven-pune-orchid"})
    assert "ven-pune-orchid" not in ids(s)
    rej = next(r for r in s.rejected if r.vendor_id == "ven-pune-orchid")
    assert "cancelled or excluded" in rej.failed[0].detail
