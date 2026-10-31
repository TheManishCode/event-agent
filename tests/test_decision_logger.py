import json
import os
import stat
import threading

import pytest
from pydantic import ValidationError

from eventmate.decision_logger import DecisionLogger
from eventmate.schemas import (
    AgentName,
    Alternative,
    ConstraintCheck,
    DecisionType,
)


def _log(logger, **kw):
    base = dict(
        request_id="r1",
        agent=AgentName.MANAGER,
        decision_type=DecisionType.PARSE,
        summary="s",
        justification="j",
        confidence=0.9,
        source="user_input",
    )
    base.update(kw)
    return logger.log(**base)


def test_writes_one_json_line_per_entry(logger):
    _log(logger)
    _log(logger, summary="second")
    lines = logger.path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["summary"] == "second"


def test_roundtrip_preserves_alternatives_and_constraints(logger):
    _log(
        logger,
        decision_type=DecisionType.RECOMMENDATION,
        alternatives=(Alternative(option="a", outcome="o", reason_not_chosen="r"),),
        constraints_checked=(ConstraintCheck(name="c", passed=True, detail="d"),),
    )
    [entry] = logger.read()
    assert entry.alternatives[0].option == "a"
    assert entry.constraints_checked[0].name == "c"
    assert entry.timestamp.tzinfo is not None


def test_recommendation_requires_alternative_or_reason(logger):
    with pytest.raises(ValidationError):
        _log(logger, decision_type=DecisionType.RECOMMENDATION)
    _log(logger, decision_type=DecisionType.RECOMMENDATION, no_alternative_reason="only one vendor in DB")


@pytest.mark.parametrize("confidence", [-0.1, 1.1, float("nan")])
def test_confidence_bounds(logger, confidence):
    with pytest.raises(ValidationError):
        _log(logger, confidence=confidence)


@pytest.mark.parametrize("source", ["", "made up", "database:../../etc/passwd x", "gpt", "user_input\n", "database:seed_vendors.json\n"])
def test_source_must_be_accountable(logger, source):
    with pytest.raises(ValidationError):
        _log(logger, source=source)


def test_failed_validation_writes_nothing(logger):
    with pytest.raises(ValidationError):
        _log(logger, confidence=5)
    assert not logger.path.exists()


@pytest.mark.parametrize("run_id", ["../evil", "a/b", "", "x" * 65, "a b", "..", "a\nb"])
def test_run_id_cannot_escape_log_dir(tmp_path, run_id):
    with pytest.raises(ValueError):
        DecisionLogger(log_dir=tmp_path, run_id=run_id)


def test_file_is_owner_only(logger):
    _log(logger)
    assert stat.S_IMODE(os.stat(logger.path).st_mode) == 0o600


def test_refuses_symlinked_log_file(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep")
    lg = DecisionLogger(log_dir=tmp_path, run_id="sym")
    os.symlink(target, lg.path)
    with pytest.raises(OSError):
        _log(lg)
    assert target.read_text() == "keep"


def test_concurrent_writes_do_not_interleave(logger):
    def work(i):
        for j in range(25):
            _log(logger, summary=f"{i}-{j}")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(logger.read()) == 200


def test_newlines_in_text_stay_on_one_line(logger):
    _log(logger, summary="a\nb\r\nc", justification="x\ny")
    assert len(logger.path.read_text().splitlines()) == 1
    assert logger.read()[0].summary == "a\nb\r\nc"


def test_corrupt_line_is_reported_not_skipped(logger):
    _log(logger)
    with open(logger.path, "a") as fh:
        fh.write("{not json\n")
    with pytest.raises(ValueError):
        logger.read()
