"""The outreach store's compare-and-set must hold across processes, not only threads (ADR-044): the web server and the worker can run side by side."""

import multiprocessing as mp
from datetime import datetime, timezone

import pytest

from eventmate.outreach import OutreachStore
from eventmate.schemas import VendorCategory as C
from eventmate.service import EventMate
from eventmate.tests.conftest import EXAMPLE, TODAY


def _slow_read(fn):
    """Each child pauses right after its read, so without a real cross-process lock every process would see the old state and win."""
    import time

    def wrapper(*a, **k):
        out = fn(*a, **k)
        time.sleep(0.15)
        return out

    return wrapper


def _claim(root, plan_id, draft_id, q):
    OutreachStore.get = _slow_read(OutreachStore.get)
    store = OutreachStore(root, {})
    try:
        store.transition(plan_id, draft_id, ("approved",), status="sending", decided_at=datetime.now(timezone.utc))
        q.put("won")
    except ValueError:
        q.put("lost")


def _store_reply(root, plan_id, msg_json, q):
    from eventmate.schemas import InboundMessage

    msg = InboundMessage.model_validate_json(msg_json)
    OutreachStore.inbound = _slow_read(OutreachStore.inbound)
    q.put("stored" if OutreachStore(root, {}).add_inbound(msg) is not None else "dup")


@pytest.fixture
def setup(tmp_path):
    em = EventMate(log_dir=tmp_path / "l", plan_dir=tmp_path / "p", run_id="mp")
    pid = em.plan(EXAMPLE, today=TODAY).plan.plan_id
    d = em.draft_outreach(pid, C.CATERER, "email", "sales@annapurna.in", "Asha")
    em.outreach_store.transition(pid, d.draft_id, ("prepared",), status="approved", decided_at=datetime.now(timezone.utc))
    return em, pid, d


def test_exactly_one_process_wins_the_send_claim(setup):
    em, pid, d = setup
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=_claim, args=(em.outreach_store.base, pid, d.draft_id, q)) for _ in range(12)]
    [p.start() for p in procs]
    [p.join(30) for p in procs]
    results = sorted(q.get(timeout=5) for _ in procs)
    assert results.count("won") == 1 and results.count("lost") == 11
    assert em.outreach_store.get(pid, d.draft_id).status == "sending"


def test_the_same_reply_seen_by_many_processes_is_stored_once(setup):
    from eventmate.schemas import InboundMessage

    em, pid, d = setup
    msgs = [InboundMessage(message_id=f"m{i}", plan_id=pid, draft_id=d.draft_id, vendor_id=d.vendor_id, vendor_name=d.vendor_name, category=C.CATERER, channel="email",
                           sender="sales@annapurna.in", received_at=datetime.now(timezone.utc), external_id="<same@v>", body="Rs 600") for i in range(10)]
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=_store_reply, args=(em.outreach_store.base, pid, m.model_dump_json(), q)) for m in msgs]
    [p.start() for p in procs]
    [p.join(30) for p in procs]
    results = [q.get(timeout=5) for _ in procs]
    assert results.count("stored") == 1 and len(em.outreach_store.inbound(pid)) == 1


def test_the_lock_is_reentrant_inside_one_process(setup):
    em, pid, d = setup
    with em.outreach_store._locked():
        with em.outreach_store._locked():
            em.outreach_store.transition(pid, d.draft_id, ("approved",), status="denied")
    assert em.outreach_store.get(pid, d.draft_id).status == "denied"
