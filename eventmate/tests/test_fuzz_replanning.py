"""Stateful fuzz: random confirm/disruption sequences must never crash or break an invariant."""

import hashlib
import random
from datetime import date, timedelta

import pytest

from eventmate.schemas import BookingStatus, Disruption, DisruptionKind as K
from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY

SEEDS = range(8)
STEPS = 10


def random_disruption(rnd, plan, today):
    kind = rnd.choice(list(K))
    over = tuple(s.vendor_id for s in plan.selections if s.booking_status == BookingStatus.CONFIRMED and rnd.random() < 0.3)
    if kind == K.VENDOR_CANCELLATION:
        pool = [s.vendor_id for s in plan.selections] + ["ven-mum-bay", "cat-pune-saffron", "nonexistent-id"]
        return Disruption(kind=kind, vendor_id=rnd.choice(pool), override_confirmed=over)
    if kind == K.BUDGET_CHANGE:
        return Disruption(kind=kind, new_budget=rnd.choice([50_000, 100_000, 140_000, 150_000, 200_000, 400_000, 10_000_000]), override_confirmed=over)
    if kind == K.GUEST_COUNT_CHANGE:
        return Disruption(kind=kind, new_guest_count=rnd.choice([1, 40, 90, 100, 120, 140, 145, 180, 500]), override_confirmed=over)
    return Disruption(kind=kind, new_date=today + timedelta(days=rnd.choice([-5, 0, 1, 7, 14, 20, 43, 50, 49, 60, 400, 4000])), override_confirmed=over)


@pytest.mark.parametrize("seed", SEEDS)
def test_random_histories_keep_every_invariant(seed, tmp_path):
    rnd = random.Random(seed)
    em = EventMate(log_dir=tmp_path / "logs", plan_dir=tmp_path / "plans", run_id=f"fuzz{seed}")
    plan = em.plan(EXAMPLE, today=TODAY).plan
    pid, today = plan.plan_id, date(2026, 11, 2)
    hashes = {1: hashlib.sha256(em.store._path(pid, 1).read_bytes()).hexdigest()}

    def record():
        for n in range(1, em.store.latest(pid).version + 1):
            hashes.setdefault(n, hashlib.sha256(em.store._path(pid, n).read_bytes()).hexdigest())

    for step in range(STEPS):
        record()
        latest = em.store.latest(pid)
        today += timedelta(days=rnd.choice([0, 0, 1, 3]))
        if rnd.random() < 0.3:
            unconfirmed = [s for s in latest.selections if s.booking_status != BookingStatus.CONFIRMED]
            if unconfirmed:
                out = em.confirm(pid, rnd.choice(unconfirmed).category)
                assert out.status in ("replanned", "failed"), out
            continue
        d = random_disruption(rnd, latest, today)
        out = em.replan(pid, d, today)
        after = em.store.latest(pid)

        if out.status == "replanned":
            assert after.version == latest.version + 1 and out.plan == after and out.diff is not None
            released = set(d.override_confirmed) | ({d.vendor_id} if d.vendor_id else set())
            for s in latest.selections:  # confirmed bookings are never silently lost
                if s.booking_status == BookingStatus.CONFIRMED and s.vendor_id not in released:
                    kept = after.selection_for(s.category)
                    assert kept and kept.vendor_id == s.vendor_id and kept.booking_status == BookingStatus.CONFIRMED, (seed, step, d)
            assert after.validation.passed and after.total_cost <= after.request.budget_total
        else:
            assert after.version == latest.version, "a failed replan must not write anything"
            assert out.violations and all(v.constraint and v.repair for v in out.violations), (seed, step, d)

    record()
    versions = em.store.versions(pid)
    assert [v.version for v in versions] == list(range(1, len(versions) + 1))
    for v in versions:  # history is immutable and every version is still valid against the database
        assert hashlib.sha256(em.store._path(pid, v.version).read_bytes()).hexdigest() == hashes[v.version]
        assert em.revalidate(pid, v.version).passed, (seed, v.version)
