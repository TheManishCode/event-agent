import json

import pytest

from eventmate.agents.guest import GuestAgent, GuestDataError, GuestRepository
from eventmate.agents.manager import parse_request
from eventmate.schemas import DecisionType, DietaryRequirement as D
from eventmate.agents.vendor import DATA_DIR
from tests.conftest import EXAMPLE, TODAY


@pytest.fixture
def req():
    return parse_request(EXAMPLE, today=TODAY, known_cities=("Pune",))


def test_without_a_guest_list_headcount_is_the_user_stated_one(logger, req):
    s = GuestAgent(GuestRepository(), logger).summarize(req)
    assert s.planning_headcount == 120
    assert (s.confirmed, s.declined, s.pending) == (0, 0, 120)
    assert s.required_diets == (D.VEGETARIAN,)
    assert all(c.passed for c in s.checks)
    assert s.source == "user_input"


def test_rsvp_counts_use_party_size(logger, req):
    s = GuestAgent(GuestRepository(), logger).summarize(req, guest_list_id="demo-engagement-120")
    assert s.confirmed == 3 + 2 + 1 + 5 + 2
    assert s.declined == 4
    assert s.pending == 4 + (120 - 13 - 4 - 4)  # 4 listed pending + 99 unlisted guests
    assert s.confirmed + s.declined + s.pending == 120
    assert s.source == "database:seed_guests.json"


def test_guest_diets_become_required_diets_but_declined_guests_do_not(logger, req):
    s = GuestAgent(GuestRepository(), logger).summarize(req, guest_list_id="demo-engagement-120")
    assert set(s.required_diets) == {D.VEGETARIAN, D.JAIN}
    assert s.dietary_counts[D.JAIN.value] == 2  # one confirmed + one pending guest


def test_confirmed_guests_over_headcount_is_a_violation_with_repair(logger, req):
    s = GuestAgent(GuestRepository(), logger).summarize(req, guest_list_id="demo-overbooked")
    bad = [c for c in s.checks if not c.passed]
    assert [c.name for c in bad] == ["rsvp_within_headcount"]
    assert "raise the guest count" in bad[0].detail


def test_unknown_guest_list_fails_closed(logger, req):
    with pytest.raises(GuestDataError):
        GuestAgent(GuestRepository(), logger).summarize(req, guest_list_id="nope")


def test_guest_list_id_is_validated_not_path_joined(logger, req):
    for bad in ["../x", "a/b", "", "x" * 100]:
        with pytest.raises(GuestDataError):
            GuestAgent(GuestRepository(), logger).summarize(req, guest_list_id=bad)


def test_capacity_checks_against_a_chosen_venue(logger, req):
    ag = GuestAgent(GuestRepository(), logger)
    s = ag.summarize(req)
    assert ag.capacity_check(s, venue_capacity=150).passed
    low = ag.capacity_check(s, venue_capacity=100)
    assert not low.passed and "120" in low.detail


def test_dietary_check_against_caterer_options(logger, req):
    ag = GuestAgent(GuestRepository(), logger)
    s = ag.summarize(req, guest_list_id="demo-engagement-120")
    assert ag.dietary_check(s, (D.VEGETARIAN, D.JAIN)).passed
    miss = ag.dietary_check(s, (D.VEGETARIAN, D.NON_VEGETARIAN))
    assert not miss.passed and "jain" in miss.detail


def test_summary_decision_is_logged_with_source_and_confidence(logger, req):
    GuestAgent(GuestRepository(), logger).summarize(req, guest_list_id="demo-engagement-120")
    [e] = logger.read()
    assert e.agent.value == "guest" and e.decision_type == DecisionType.ASSESSMENT
    assert e.source == "database:seed_guests.json" and e.constraints_checked


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["records"][0]["guests"][0].update(party_size=0),
        lambda d: d["records"][0]["guests"][0].update(rsvp="maybe"),
        lambda d: d["records"][0]["guests"][0].update(extra="x"),
        lambda d: d["records"][0]["guests"].append(dict(d["records"][0]["guests"][0])),  # duplicate guest id
        lambda d: d["records"].append(dict(d["records"][0])),  # duplicate list id
        lambda d: d["records"][0].update(source="trust me"),
        lambda d: d.update(schema_version=9),
    ],
)
def test_malformed_guest_data_fails_closed(tmp_path, mutate):
    doc = json.loads((DATA_DIR / "seed_guests.json").read_text())
    mutate(doc)
    p = tmp_path / "g.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(GuestDataError):
        GuestRepository(path=p)
