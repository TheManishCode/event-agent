"""Budget ledger: user-entered quotes and payments against the latest plan. Nothing is ever processed."""

import json

import pytest

from eventmate.schemas import VendorCategory as C
from eventmate.service import EventMate
from tests.conftest import EXAMPLE, TODAY
from eventmate.web.app import create_app


@pytest.fixture
def em(tmp_path):
    return EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id="ledger")


@pytest.fixture
def pid(em):
    return em.plan(EXAMPLE, today=TODAY).plan.plan_id


def line(summary, category):
    return next(l for l in summary.lines if l.category == category)


def test_empty_ledger_mirrors_the_plan_with_nothing_paid(em, pid):
    s = em.ledger_summary(pid)
    plan = em.store.latest(pid)
    assert s.planned_total == plan.total_cost and s.paid_total == 0 and s.quoted_total == 0
    assert s.committed_total == plan.total_cost and s.pending_total == plan.total_cost
    assert s.budget_total == plan.request.budget_total and s.headroom == s.budget_total - s.committed_total
    v = line(s, C.VENUE)
    assert v.quoted is None and v.committed == v.planned and v.variance == 0 and v.pending == v.planned


def test_quote_replaces_the_planned_figure_and_shows_variance(em, pid):
    planned = line(em.ledger_summary(pid), C.CATERER).planned
    em.ledger_add(pid, "quote", C.CATERER, planned + 5_000, "final menu quote")
    em.ledger_add(pid, "quote", C.CATERER, planned + 2_000, "revised quote")  # latest quote wins, history is kept
    s = em.ledger_summary(pid)
    c = line(s, C.CATERER)
    assert c.quoted == planned + 2_000 and c.committed == planned + 2_000 and c.variance == 2_000
    assert s.committed_total == s.planned_total + 2_000
    assert len(em.ledger_entries(pid)) == 2


def test_payments_and_refunds_move_paid_and_pending(em, pid):
    base = line(em.ledger_summary(pid), C.VENUE).planned
    em.ledger_add(pid, "payment", C.VENUE, 10_000, "advance")
    em.ledger_add(pid, "payment", C.VENUE, 5_000, "second instalment")
    em.ledger_add(pid, "refund", C.VENUE, 2_000, "partial refund")
    v = line(em.ledger_summary(pid), C.VENUE)
    assert v.paid == 13_000 and v.pending == base - 13_000 and v.overpaid == 0


def test_overpaying_is_flagged_not_hidden(em, pid):
    base = line(em.ledger_summary(pid), C.PHOTOGRAPHER).planned
    em.ledger_add(pid, "payment", C.PHOTOGRAPHER, base + 700, "paid extra for album")
    v = line(em.ledger_summary(pid), C.PHOTOGRAPHER)
    assert v.pending == 0 and v.overpaid == 700


def test_summary_follows_the_latest_plan_version(em, pid):
    from eventmate.schemas import Disruption, DisruptionKind as K

    em.ledger_add(pid, "payment", C.VENUE, 1_000, "advance")
    out = em.replan(pid, Disruption(kind=K.BUDGET_CHANGE, new_budget=190_000), TODAY)
    assert out.status == "replanned"
    s = em.ledger_summary(pid)
    assert s.plan_version == 2 and s.budget_total == 190_000 and line(s, C.VENUE).paid == 1_000


# ------------------------------------------------------------------ adversarial


@pytest.mark.parametrize("amount", [0, -5, 10**11])
def test_amounts_must_be_positive_whole_rupees_within_bounds(em, pid, amount):
    with pytest.raises(ValueError):
        em.ledger_add(pid, "payment", C.VENUE, amount, "x")


def test_refund_cannot_exceed_what_was_paid(em, pid):
    em.ledger_add(pid, "payment", C.VENUE, 1_000, "advance")
    with pytest.raises(ValueError, match="refund"):
        em.ledger_add(pid, "refund", C.VENUE, 1_001, "too much")
    assert len(em.ledger_entries(pid)) == 1  # the rejected entry was not written


def test_category_must_be_part_of_the_plan(em):
    text = "Plan a 80 guests party in Pune on December 12 2026 under INR 150,000. Vegetarian catering, indoor venue."
    out = em.plan(text, today=TODAY)
    assert out.plan is not None and out.plan.selection_for(C.PHOTOGRAPHER) is None
    with pytest.raises(ValueError, match="not part of this plan"):
        em.ledger_add(out.plan.plan_id, "payment", C.PHOTOGRAPHER, 100, "x")


def test_unknown_kind_unknown_plan_and_hostile_ids(em, pid):
    with pytest.raises(ValueError):
        em.ledger_add(pid, "wire_transfer", C.VENUE, 100, "x")
    with pytest.raises(FileNotFoundError):
        em.ledger_summary("nope")
    with pytest.raises(ValueError):
        em.ledger_summary("../../etc/passwd")


def test_note_is_bounded_and_ledger_file_is_private_and_append_only(em, pid, tmp_path):
    with pytest.raises(ValueError):
        em.ledger_add(pid, "payment", C.VENUE, 100, "x" * 201)
    em.ledger_add(pid, "payment", C.VENUE, 100, "first")
    em.ledger_add(pid, "payment", C.VENUE, 100, "second")
    path = next((tmp_path / "plans").rglob(f"ledger-{pid}.jsonl"))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert [json.loads(l)["note"] for l in path.read_text().splitlines()] == ["first", "second"]


# ------------------------------------------------------------------------- API


@pytest.fixture
def client(em):
    return create_app(em, today_fn=lambda: TODAY).test_client()


def _post(client, url, body):
    tok = client.get("/api/session").get_json()["csrf"]
    return client.post(url, data=json.dumps(body), headers={"X-EventMate-CSRF": tok, "Content-Type": "application/json"})


def test_api_add_and_read_the_ledger(client, pid):
    r = _post(client, f"/api/plans/{pid}/ledger", {"kind": "payment", "category": "venue", "amount": 12_000, "note": "advance"})
    assert r.status_code == 201 and r.get_json()["paid_total"] == 12_000
    g = client.get(f"/api/plans/{pid}/ledger")
    assert g.status_code == 200 and g.get_json()["summary"]["paid_total"] == 12_000 and len(g.get_json()["entries"]) == 1


def test_api_rejects_bad_ledger_input_with_4xx(client, pid):
    assert _post(client, f"/api/plans/{pid}/ledger", {"kind": "payment", "category": "venue", "amount": -1, "note": ""}).status_code == 422
    assert _post(client, f"/api/plans/{pid}/ledger", {"kind": "payment", "category": "venue", "amount": 5, "note": "", "extra": 1}).status_code == 422
    assert client.get("/api/plans/doesnotexist/ledger").status_code == 404


def test_money_paid_to_a_category_no_longer_in_the_plan_is_still_reported(em):
    """Simulates a replan that dropped the photographer after a payment was recorded."""
    from datetime import datetime, timezone

    from eventmate.schemas import LedgerEntry

    text = "Plan a 80 guests party in Pune on December 12 2026 under INR 150,000. Vegetarian catering, indoor venue."
    plan = em.plan(text, today=TODAY).plan
    assert plan.selection_for(C.PHOTOGRAPHER) is None
    for kind, amount in (("payment", 4_000), ("payment", 1_000), ("refund", 500)):
        e = LedgerEntry(entry_id=kind + str(amount), timestamp=datetime.now(timezone.utc), plan_id=plan.plan_id, kind=kind, category=C.PHOTOGRAPHER, amount=amount)
        em.ledger._file(plan.plan_id)._append(e, e.entry_id)
    s = em.ledger_summary(plan.plan_id)
    assert s.orphaned_paid == {"photographer": 4_500} and s.paid_total == 0
