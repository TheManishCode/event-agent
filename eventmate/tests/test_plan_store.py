import json
import os
import stat
import threading
from datetime import datetime

import pytest

from eventmate.agents.guest import GuestAgent, GuestRepository
from eventmate.agents.planner import PlannerAgent
from eventmate.agents.scheduler import SchedulingAgent
from eventmate.agents.validator import ValidatorAgent
from eventmate.logs import RunLogs
from eventmate.plan_store import PlanIntegrityError, PlanStore, assemble_plan
from eventmate.symbolic import SymbolicChecker

ORIGIN = datetime(2026, 10, 30)


@pytest.fixture
def plan(tmp_path, repo, intake):
    logs = RunLogs.create(log_dir=tmp_path / "logs", run_id="store")
    g = GuestAgent(GuestRepository(), logs.decisions).summarize(intake.request)
    d = PlannerAgent(logs, SchedulingAgent(logs.decisions), SymbolicChecker()).plan(intake.request, g, intake.budget_allocation, intake.vendor_shortlists, ORIGIN, plan_id="plan1").draft
    report = ValidatorAgent(repo, logs).validate(d)
    return assemble_plan(d, report, version=1, parent_version=None, reason="initial plan", decision_entry_ids=())


@pytest.fixture
def store(tmp_path):
    return PlanStore(tmp_path / "plans")


def test_save_and_load_roundtrip_is_exact(store, plan):
    path = store.save(plan)
    assert path.name == "plan-plan1-v0001.json"
    assert store.load("plan1") == plan and store.load("plan1", 1) == plan


def test_files_are_read_only_and_never_overwritten(store, plan):
    path = store.save(plan)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o444
    with pytest.raises(FileExistsError):
        store.save(plan)  # same version again
    assert store.load("plan1") == plan


def test_versions_must_be_consecutive_and_link_to_their_parent(store, plan):
    store.save(plan)
    v3 = plan.model_copy(update={"version": 3, "parent_version": 2})
    with pytest.raises(ValueError, match="next version"):
        store.save(v3)
    v2_wrong_parent = plan.model_copy(update={"version": 2, "parent_version": None})
    with pytest.raises(ValueError, match="parent"):
        store.save(v2_wrong_parent)
    v2 = plan.model_copy(update={"version": 2, "parent_version": 1, "change_reason": "second"})
    store.save(v2)
    assert [p.version for p in store.versions("plan1")] == [1, 2]
    assert store.latest("plan1").version == 2
    assert store.load("plan1", 1).change_reason == "initial plan"  # earlier version untouched


def test_first_version_must_be_one_without_parent(store, plan):
    with pytest.raises(ValueError):
        store.save(plan.model_copy(update={"version": 2, "parent_version": 1}))


def test_tampering_is_detected_by_hash(store, plan):
    path = store.save(plan)
    os.chmod(path, 0o644)
    doc = json.loads(path.read_text())
    doc["plan"]["total_cost"] = 1
    path.write_text(json.dumps(doc))
    with pytest.raises(PlanIntegrityError):
        store.load("plan1")


def test_tampered_hash_and_content_together_still_fail_model_validation(store, plan):
    """An attacker who recomputes the hash still cannot forge a failed-validation plan."""
    import hashlib

    path = store.save(plan)
    os.chmod(path, 0o644)
    doc = json.loads(path.read_text())
    doc["plan"]["validation"]["passed"] = False
    canon = json.dumps(doc["plan"], sort_keys=True, separators=(",", ":"))
    doc["sha256"] = hashlib.sha256(canon.encode()).hexdigest()
    path.write_text(json.dumps(doc))
    with pytest.raises(PlanIntegrityError):
        store.load("plan1")


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "x" * 65, "a b", "..", "a\nb", "a.json"])
def test_plan_id_cannot_escape_the_store(store, bad):
    with pytest.raises(ValueError):
        store.load(bad)
    with pytest.raises(ValueError):
        store.versions(bad)


def test_unknown_plan_and_version_raise_clearly(store, plan):
    with pytest.raises(FileNotFoundError):
        store.load("missing")
    store.save(plan)
    with pytest.raises(FileNotFoundError):
        store.load("plan1", 9)


def test_symlinked_plan_file_is_refused(store, plan, tmp_path):
    path = store.save(plan)
    target = tmp_path / "elsewhere.json"
    target.write_text(path.read_text())
    path.unlink()
    os.symlink(target, path)
    with pytest.raises(PlanIntegrityError):
        store.load("plan1")


def test_oversized_file_is_refused(store, plan):
    path = store.save(plan)
    os.chmod(path, 0o644)
    path.write_text(" " * 6_000_000)
    with pytest.raises(PlanIntegrityError):
        store.load("plan1")


def test_concurrent_writers_of_the_same_version_only_one_wins(store, plan):
    results = []

    def work():
        try:
            store.save(plan)
            results.append("ok")
        except FileExistsError:
            results.append("exists")

    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1 and results.count("exists") == 7


def test_list_plans_and_no_temp_files_left_behind(store, plan):
    store.save(plan)
    assert store.list_plans() == ["plan1"]
    assert [p.name for p in store.root.iterdir()] == ["plan-plan1-v0001.json"]


def test_assemble_refuses_a_failed_report(plan):
    from eventmate.schemas import ConstraintCheck, ValidationReport

    bad = ValidationReport(passed=False, checks=(ConstraintCheck(name="x", passed=False, detail="d"),))
    with pytest.raises(ValueError):
        assemble_plan(_draft_from(plan), bad, version=1, parent_version=None, reason="r", decision_entry_ids=())


def _draft_from(plan):
    from eventmate.schemas import PlanDraft

    raise_ = None
    return PlanDraft.model_construct(
        plan_id=plan.plan_id, origin=plan.origin, request=plan.request, guests=plan.guests, allocation=plan.allocation,
        selections=plan.selections, spec=raise_, solution=raise_, symbolic=plan.symbolic, items=plan.items, already_done=(), warnings=(), excluded_vendor_ids=(),
    )


# ------------------------------------------------------------ signing (HMAC)

KEY = b"0123456789abcdef0123456789abcdef"


def test_signed_store_roundtrips_and_rejects_a_forged_hash(tmp_path, plan):
    import hashlib

    store = PlanStore(tmp_path / "signed", signing_key=KEY)
    path = store.save(plan)
    assert store.load("plan1") == plan
    os.chmod(path, 0o644)
    doc = json.loads(path.read_text())
    assert "hmac" in doc
    doc["plan"]["total_cost"] = doc["plan"]["total_cost"]  # unchanged content
    doc["plan"]["change_reason"] = "edited by an attacker"
    canon = json.dumps(doc["plan"], sort_keys=True, separators=(",", ":"))
    doc["sha256"] = hashlib.sha256(canon.encode()).hexdigest()  # attacker can recompute the plain hash...
    path.write_text(json.dumps(doc))
    with pytest.raises(PlanIntegrityError, match="signature"):  # ...but not the HMAC
        store.load("plan1")


def test_signed_store_refuses_unsigned_files(tmp_path, plan):
    plain = PlanStore(tmp_path / "shared")
    plain.save(plan)
    with pytest.raises(PlanIntegrityError, match="signature"):
        PlanStore(tmp_path / "shared", signing_key=KEY).load("plan1")


def test_signing_key_must_be_long_enough(tmp_path):
    with pytest.raises(ValueError, match="at least 16"):
        PlanStore(tmp_path / "x", signing_key=b"short")


def test_signing_key_is_read_from_the_environment(tmp_path, plan, monkeypatch):
    monkeypatch.setenv("EVENTMATE_SIGNING_KEY", KEY.decode())
    s = PlanStore(tmp_path / "env")
    path = s.save(plan)
    assert "hmac" in json.loads(path.read_text())
    assert KEY.decode() not in path.read_text()  # the key itself never lands on disk
