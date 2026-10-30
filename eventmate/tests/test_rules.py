"""F7 v1: versioned, reviewable checklists keyed by dietary requirement. Advisory only; never a hard constraint."""

import json

import pytest

from eventmate.rules import RulePackError, load_packs, advisories_for
from eventmate.schemas import DietaryRequirement as D
from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY
from eventmate.web.app import create_app


def test_shipped_packs_load_and_cover_every_diet_that_needs_a_checklist():
    packs = load_packs()
    assert {p.diet for p in packs} >= {D.JAIN, D.HALAL, D.VEGAN, D.GLUTEN_FREE}
    for p in packs:
        assert p.version >= 1 and p.items and p.source
        assert p.reviewed is False or p.reviewed_by  # a claim of review must name who


def test_every_pack_says_it_is_unreviewed_until_someone_signs_off():
    assert all(not p.reviewed for p in load_packs())  # none has been reviewed yet; flip this when that is true


def test_advisories_follow_the_request_diets_and_carry_provenance():
    em_req = pytest.importorskip("eventmate.agents.manager").parse_request(
        "Plan a 80 guests party in Pune on December 12 2026 under INR 150,000. Jain and vegan catering, indoor venue.", today=TODAY
    )
    items = advisories_for(em_req)
    assert {a.diet for a in items} == {D.JAIN, D.VEGAN}
    assert all(a.pack_version >= 1 and a.advisory is True and a.reviewed is False and a.source for a in items)


def test_no_diet_means_no_advisories_and_unknown_content_is_not_invented():
    from eventmate.agents.manager import parse_request

    r = parse_request("Plan a 80 guests party in Pune on December 12 2026 under INR 150,000. Indoor venue.", today=TODAY)
    assert advisories_for(r) == []


# --------------------------------------------------------------- strict loading


def _write(tmp_path, doc):
    p = tmp_path / "packs.json"
    p.write_text(json.dumps(doc))
    return p


GOOD = {"schema_version": 1, "packs": [{"id": "x", "diet": "jain", "version": 1, "source": "s", "reviewed": False, "items": [{"title": "t", "detail": "d"}]}]}


def test_loader_accepts_a_minimal_valid_file(tmp_path):
    assert len(load_packs(_write(tmp_path, GOOD))) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["packs"][0].update(extra=1),  # unknown field
        lambda d: d["packs"][0].update(diet="paleo"),  # unknown diet
        lambda d: d["packs"][0].update(items=[]),  # empty pack
        lambda d: d["packs"][0].update(reviewed=True),  # claims review without a reviewer
        lambda d: d["packs"][0]["items"][0].update(detail="<img src=x onerror=1>" * 100),  # oversized / markup-heavy text
        lambda d: d["packs"].append(dict(d["packs"][0])),  # duplicate id
        lambda d: d.update(schema_version=2),
    ],
)
def test_loader_fails_closed_on_malformed_packs(tmp_path, mutate):
    doc = json.loads(json.dumps(GOOD))
    mutate(doc)
    with pytest.raises(RulePackError):
        load_packs(_write(tmp_path, doc))


def test_loader_rejects_oversized_and_non_json_files(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(" " * 600_000)
    with pytest.raises(RulePackError):
        load_packs(big)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(RulePackError):
        load_packs(bad)


# ------------------------------------------------------------------ API and UI


def test_api_returns_advisories_for_a_plan(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p")
    plan = em.plan(EXAMPLE.replace("Vegetarian", "Jain"), today=TODAY).plan
    c = create_app(em, today_fn=lambda: TODAY).test_client()
    r = c.get(f"/api/plans/{plan.plan_id}/advisories")
    assert r.status_code == 200
    body = r.get_json()
    assert body and all(a["advisory"] and not a["reviewed"] for a in body) and {a["diet"] for a in body} == {"jain"}
    assert c.get("/api/plans/nope/advisories").status_code == 404
