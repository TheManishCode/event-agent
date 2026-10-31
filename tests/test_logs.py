import os
import stat

import pytest
from pydantic import ValidationError

from eventmate.logs import RunLogs


@pytest.fixture
def logs(tmp_path):
    return RunLogs.create(log_dir=tmp_path, run_id="run1")


def test_run_logs_share_run_id_and_use_separate_files(logs, tmp_path):
    names = {logs.decisions.path.name, logs.tool_calls.path.name, logs.llm_traces.path.name, logs.validation_failures.path.name}
    assert names == {
        "decisions-run1.jsonl",
        "tool_calls-run1.jsonl",
        "llm_traces-run1.jsonl",
        "validation_failures-run1.jsonl",
    }


def test_tool_call_roundtrip_and_permissions(logs):
    logs.tool_calls.log(tool="cpsat.solve", args={"tasks": 11, "timeout_s": 10.0}, ok=True, result="optimal", duration_ms=12.5, request_id="r1")
    [e] = logs.tool_calls.read()
    assert e.tool == "cpsat.solve" and e.args["tasks"] == 11
    assert stat.S_IMODE(os.stat(logs.tool_calls.path).st_mode) == 0o600


def test_tool_call_args_must_be_flat_scalars(logs):
    with pytest.raises(ValidationError):
        logs.tool_calls.log(tool="x", args={"nested": {"a": 1}}, ok=True, result="r", duration_ms=1, request_id="r1")


def test_tool_call_rejects_negative_duration_and_oversized_result(logs):
    with pytest.raises(ValidationError):
        logs.tool_calls.log(tool="x", args={}, ok=True, result="r", duration_ms=-1, request_id="r1")
    with pytest.raises(ValidationError):
        logs.tool_calls.log(tool="x", args={}, ok=True, result="r" * 3000, duration_ms=1, request_id="r1")


def test_llm_trace_stores_hashes_not_text_by_default(logs):
    logs.llm_traces.log(prompt_id="explainer.rewrite", prompt_version=1, model="m", input_text="secret guest list", output_text="out", accepted=True, duration_ms=3, request_id="r1")
    raw = logs.llm_traces.path.read_text()
    assert "secret guest list" not in raw
    [e] = logs.llm_traces.read()
    assert len(e.input_sha256) == 64 and e.input_chars == len("secret guest list")


def test_llm_trace_can_store_text_only_when_explicitly_enabled(tmp_path):
    lg = RunLogs.create(log_dir=tmp_path, run_id="r2", store_llm_text=True)
    lg.llm_traces.log(prompt_id="p", prompt_version=1, model="m", input_text="hello", output_text="world", accepted=False, rejection_reason="guard", duration_ms=1, request_id="r")
    assert "hello" in lg.llm_traces.path.read_text()


def test_validation_failure_requires_constraint_and_repair(logs):
    logs.validation_failures.log(plan_id="p1", constraint="budget_total", detail="over", repair="raise budget", request_id="r1")
    [e] = logs.validation_failures.read()
    assert e.constraint == "budget_total"
    with pytest.raises(ValidationError):
        logs.validation_failures.log(plan_id="p1", constraint="", detail="d", repair="r", request_id="r1")
