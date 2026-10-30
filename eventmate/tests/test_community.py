"""Community events (ADR-046): profiles as data, an honest split between hard checks, conditional items and suggestions.

Class A are computed from user input and the database and fail closed with a repair. Class B are conditional requirements that only the user can mark.
Class C are suggestions. A plan is never called safe or compliant.
"""

import json

import pytest
from pydantic import ValidationError

from eventmate.agents.budget import BudgetPolicy, compute_allocation
from eventmate.agents.manager import parse_request
from eventmate.agents.scheduler import EventPolicy
from eventmate.community import ProfileError, load_profiles, match_subtype, parse_profiles
from eventmate.schemas import CommunityDetails, EventRequest, EventType, VendorCategory as C
from eventmate.tests.conftest import EXAMPLE, TODAY

COLLEGE = "Plan a 150-person college fest in Pune on December 12 under INR 250,000. Venue and catering."


def profiles_doc():
    return json.loads(json.dumps(json.load(open(load_profiles.__globals__["PROFILES_PATH"]))))


# ---- profiles are data

def test_the_shipped_profiles_load_with_five_subtypes_and_an_other_default():
    p = load_profiles()
    assert set(p.profiles) == {"other", "college_fest", "neighbourhood_gathering", "fundraiser", "cultural_festival_local"} and p.default == "other"
    for prof in p.profiles.values():
        assert prof.keywords and set(prof.weights) == {"venue", "caterer", "photographer"} and prof.roles and 30 <= prof.minutes <= 1440
    assert p.reviewed is False and {d.item_id for d in p.items} >= {"venue_permission", "food_licence", "allergen_info", "weather_backup"}


@pytest.mark.parametrize("mutate,why", [
    (lambda d: d.__setitem__("schema_version", 2), "schema"),
    (lambda d: d.__setitem__("default_subtype", "nope"), "default"),
    (lambda d: d["profiles"]["other"]["budget_weights_bp"].__setitem__("venue", -1), "weights"),
    (lambda d: d["profiles"]["other"]["budget_weights_bp"].pop("venue"), "weights"),
    (lambda d: d["profiles"]["other"].__setitem__("event_window", {"start_hour": 30, "minutes": 60}), "window"),
    (lambda d: d["profiles"]["other"].__setitem__("keywords", []), "keywords"),
    (lambda d: d["profiles"]["fundraiser"]["keywords"].append("college fest"), "keyword"),
    (lambda d: d["profiles"]["other"].__setitem__("surprise", 1), "unknown fields"),
    (lambda d: d["conditional_items"][0].__setitem__("triggers", ["not_an_answer"]), "triggers"),
    (lambda d: d["conditional_items"][0].__setitem__("title", "This is legally mandatory"), "legal"),
    (lambda d: d["conditional_items"][0].__setitem__("check_with", "a safe and compliant authority"), "safe"),
    (lambda d: d["profiles"]["other"].__setitem__("always_items", ["no_such_item"]), "always_items"),
    (lambda d: d["conditional_items"].append(dict(d["conditional_items"][0])), "duplicate"),
])
def test_a_malformed_profile_file_aborts_loading(mutate, why):
    doc = profiles_doc()
    mutate(doc)
    with pytest.raises(ProfileError):
        parse_profiles(doc)


def test_the_shipped_data_never_claims_safety_compliance_or_a_legal_rule():
    text = json.dumps(profiles_doc()).lower()
    for word in ("safe ", "compliant", "compliance", "legal", "mandatory", "required by law"):
        assert word not in text, word


@pytest.mark.parametrize("text,sub", [
    ("our college fest next month", "college_fest"), ("the RWA annual event", "neighbourhood_gathering"), ("a society fest for residents", "neighbourhood_gathering"),
    ("a temple/church/mosque event", "cultural_festival_local"), ("charity fundraiser dinner", "fundraiser"), ("a community event at the hall", "other"),
    ("annual campus fest", "college_fest"), ("block party for the street", "neighbourhood_gathering"),
])
def test_phrases_map_to_a_subtype(text, sub):
    assert match_subtype(text)[0] == sub


def test_nothing_else_matches_a_community_subtype():
    for text in ("a wedding in Pune", "a quiet dinner", "corporate offsite", "just a party"):
        assert match_subtype(text) is None


# ---- parsing

def test_a_college_fest_brief_becomes_a_community_event_and_asks_the_user_to_check():
    r = parse_request(COLLEGE, today=TODAY)
    assert r.event_type == EventType.COMMUNITY and r.community.subtype == "college_fest"
    assert any("college fest" in a and "check" in a for a in r.assumptions) and r.parse_confidence < 1.0


def test_an_explicit_community_event_uses_the_default_subtype_and_says_so():
    r = parse_request("Plan a 80-person community event in Pune on December 12 under INR 150,000. Venue.", today=TODAY)
    assert r.event_type == EventType.COMMUNITY and r.community.subtype == "other"
    assert any("subtype" in a for a in r.assumptions)


@pytest.mark.parametrize("text,typ", [
    ("Plan a 120-person wedding in Pune on December 12 under INR 500,000. Venue.", EventType.WEDDING),
    ("Plan a temple wedding for 120 people in Pune on December 12 under INR 500,000. Venue.", EventType.WEDDING),
    ("Plan a 60-person birthday in Pune on December 12 under INR 90,000. Venue.", EventType.BIRTHDAY),
    ("Plan a 120-person engagement party in Pune on December 12 under INR 200,000. Venue.", EventType.ENGAGEMENT_PARTY),
    ("Plan a company offsite for 60 people in Pune on December 12 under INR 150,000. Venue.", EventType.CORPORATE),
    ("Plan a conference for 200 people in Pune on December 12 under INR 300,000. Venue.", EventType.CONFERENCE),
    ("Plan a 50-person gathering in Pune on December 12 under INR 100,000. Venue.", EventType.OTHER),
])
def test_other_event_types_are_parsed_exactly_as_before(text, typ):
    r = parse_request(text, today=TODAY)
    assert r.event_type == typ and r.community is None


def test_community_details_only_belong_to_a_community_event():
    base = parse_request(EXAMPLE, today=TODAY).model_dump()
    with pytest.raises(ValidationError, match="community event"):
        EventRequest.model_validate({**base, "community": {"subtype": "other"}})


def test_the_expected_headcount_must_lie_inside_the_attendance_range():
    base = parse_request(COLLEGE, today=TODAY).model_dump()
    for bad in ({"attendance_low": 200}, {"attendance_high": 100}, {"attendance_low": 200, "attendance_high": 100}):
        with pytest.raises(ValidationError):
            EventRequest.model_validate({**base, "community": {**base["community"], **bad}})
    ok = EventRequest.model_validate({**base, "community": {**base["community"], "attendance_low": 100, "attendance_high": 220}})
    assert ok.planning_attendance == 220 and parse_request(COLLEGE, today=TODAY).planning_attendance == 150


def test_the_community_details_model_rejects_inconsistent_input():
    ok = dict(volunteers=("Asha", "Ravi"))
    CommunityDetails(**ok, setup_owner="Asha")
    for bad in (
        dict(setup_owner="Zed"), dict(volunteers=("Asha", "Asha")), dict(shifts=({"volunteer": "Zed", "role": "x", "phase": "event"},)),
        dict(slots=({"slot_id": "a", "role": "x", "phase": "event"}, {"slot_id": "a", "role": "y", "phase": "event"})),
        dict(slots=({"slot_id": "a", "role": "x", "phase": "event", "start_min": 30, "end_min": 10},)),
        dict(program=({"activity_id": "a", "title": "t", "minutes": 30, "resource": "stage", "after": "ghost"},)),
        dict(program=({"activity_id": "a", "title": "t", "minutes": 30, "resource": "stage", "after": "a"},)),
        dict(volunteers=("bad\nname",)), dict(subtype="Bad Subtype"), dict(access_needs=("step_free_access", "step_free_access")), dict(access_needs=("flying_carpet",)),
        dict(funding=({"label": "x", "status": "maybe", "amount_inr": 5},)), dict(funding=({"label": "x", "status": "received", "amount_inr": 0},)), dict(surprise=1),
    ):
        with pytest.raises(ValidationError):
            CommunityDetails(**{**ok, **bad} if "volunteers" not in bad else bad)


# ---- planning basis: window and weights come from the profile

def test_the_event_window_comes_from_the_profile_and_falls_back_to_a_default():
    pol = EventPolicy()
    college = parse_request(COLLEGE, today=TODAY)
    assert pol.event_window(college) == (12, 480)
    assert pol.event_window(parse_request(EXAMPLE, today=TODAY)) == (18, 240)  # unchanged for other types
    other = college.with_changes(community=CommunityDetails(subtype="fundraiser"))
    assert pol.event_window(other) == (18, 240)


def test_community_budget_weights_are_their_own_profile_not_the_wedding_split():
    college = parse_request(COLLEGE, today=TODAY)
    wedding_like = college.with_changes(event_type=EventType.OTHER, community=None)
    a, w = compute_allocation(college, BudgetPolicy()), compute_allocation(wedding_like, BudgetPolicy())
    share = lambda al: {l.category: l.amount for l in al.lines}  # noqa: E731
    assert share(a) != share(w) and a.total == w.total == a.contingency + sum(l.amount for l in a.lines)
    prof = load_profiles().get("college_fest").weights
    pool = college.budget_total - a.contingency
    assert share(a)["venue"] == pool * prof["venue"] // (prof["venue"] + prof["caterer"])


def test_a_users_cap_weights_still_override_the_profile():
    college = parse_request(COLLEGE, today=TODAY)
    from eventmate.schemas import CapWeight

    custom = college.with_changes(cap_weights=(CapWeight(category=C.VENUE, weight=1), CapWeight(category=C.CATERER, weight=1)))
    lines = {l.category: l.amount for l in compute_allocation(custom, BudgetPolicy()).lines}
    assert abs(lines["venue"] - lines["caterer"]) <= 1


def test_editing_the_profile_file_changes_behaviour_with_no_code_edit(tmp_path, monkeypatch):
    doc = profiles_doc()
    doc["profiles"]["science_fair"] = {
        "label": "Science fair", "keywords": ["science fair"], "budget_weights_bp": {"venue": 1000, "caterer": 9000, "photographer": 100},
        "event_window": {"start_hour": 9, "minutes": 360}, "suggested_roles": [{"role": "judge liaison", "note": "Looks after the judges."}], "always_items": [],
    }
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(doc))
    text = "Plan a 100-person science fair in Pune on December 12 under INR 200,000. Venue and catering."
    assert parse_request(text, today=TODAY).event_type == EventType.OTHER  # unknown with the shipped file
    monkeypatch.setenv("EVENTMATE_COMMUNITY_PROFILES", str(path))
    r = parse_request(text, today=TODAY)
    assert r.event_type == EventType.COMMUNITY and r.community.subtype == "science_fair" and EventPolicy().event_window(r) == (9, 360)
    lines = {l.category: l.amount for l in compute_allocation(r, BudgetPolicy()).lines}
    assert lines["caterer"] > 5 * lines["venue"]
    doc["profiles"]["science_fair"]["event_window"]["start_hour"] = 14
    path.write_text(json.dumps(doc))
    assert EventPolicy().event_window(parse_request(text, today=TODAY)) == (14, 360)  # edited again, picked up without a restart


def test_an_unknown_subtype_is_refused_with_the_known_ones_listed():
    with pytest.raises(ValueError, match="known:"):
        load_profiles().get("moon_base")
