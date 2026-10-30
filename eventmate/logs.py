"""The four JSONL log channels required by the spec, bundled per run.

- decisions: material decisions (see ``DecisionLogger``)
- tool_calls: every solver / repository call the agents make
- llm_traces: LLM calls; stores hashes by default, never raw text unless enabled
- validation_failures: every hard-constraint failure with its repair
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from eventmate.decision_logger import DecisionLogger
from eventmate.jsonl import JsonlLog

Scalar = Union[str, int, float, bool, None]


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_id: str
    timestamp: datetime
    run_id: str
    request_id: str = Field(max_length=64)


class ToolCallEntry(_Entry):
    tool: str = Field(min_length=1, max_length=80)
    args: Dict[str, Scalar]
    ok: bool
    result: str = Field(max_length=2000)
    duration_ms: float = Field(ge=0)


class LLMTraceEntry(_Entry):
    prompt_id: str = Field(min_length=1, max_length=80)
    prompt_version: int = Field(ge=1)
    model: str = Field(min_length=1, max_length=80)
    input_sha256: str = Field(min_length=64, max_length=64)
    output_sha256: str = Field(min_length=64, max_length=64)
    input_chars: int = Field(ge=0)
    output_chars: int = Field(ge=0)
    accepted: bool
    rejection_reason: Optional[str] = Field(default=None, max_length=500)
    duration_ms: float = Field(ge=0)
    input_text: Optional[str] = None
    output_text: Optional[str] = None


class ValidationFailureEntry(_Entry):
    plan_id: str = Field(max_length=64)
    constraint: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=1000)
    repair: str = Field(min_length=1, max_length=1000)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ToolCallLog(JsonlLog[ToolCallEntry]):
    channel = "tool_calls"
    model = ToolCallEntry

    def log(self, *, request_id: str, **fields) -> ToolCallEntry:
        eid = uuid.uuid4().hex
        entry = ToolCallEntry(entry_id=eid, timestamp=_now(), run_id=self.run_id, request_id=request_id, **fields)
        return self._append(entry, eid)


class LLMTraceLog(JsonlLog[LLMTraceEntry]):
    channel = "llm_traces"
    model = LLMTraceEntry

    def __init__(self, *args, store_text: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.store_text = store_text

    def log(self, *, request_id: str, input_text: str, output_text: str, **fields) -> LLMTraceEntry:
        eid = uuid.uuid4().hex
        entry = LLMTraceEntry(
            entry_id=eid,
            timestamp=_now(),
            run_id=self.run_id,
            request_id=request_id,
            input_sha256=_sha(input_text),
            output_sha256=_sha(output_text),
            input_chars=len(input_text),
            output_chars=len(output_text),
            input_text=input_text if self.store_text else None,
            output_text=output_text if self.store_text else None,
            **fields,
        )
        return self._append(entry, eid)


class ValidationFailureLog(JsonlLog[ValidationFailureEntry]):
    channel = "validation_failures"
    model = ValidationFailureEntry

    def log(self, *, request_id: str, **fields) -> ValidationFailureEntry:
        eid = uuid.uuid4().hex
        entry = ValidationFailureEntry(entry_id=eid, timestamp=_now(), run_id=self.run_id, request_id=request_id, **fields)
        return self._append(entry, eid)


class RunLogs:
    def __init__(self, decisions: DecisionLogger, tool_calls: ToolCallLog, llm_traces: LLMTraceLog, validation_failures: ValidationFailureLog):
        self.decisions = decisions
        self.tool_calls = tool_calls
        self.llm_traces = llm_traces
        self.validation_failures = validation_failures

    @classmethod
    def create(cls, log_dir: Optional[Path] = None, run_id: Optional[str] = None, store_llm_text: bool = False) -> "RunLogs":
        run_id = run_id or uuid.uuid4().hex
        return cls(
            DecisionLogger(log_dir, run_id),
            ToolCallLog(log_dir, run_id),
            LLMTraceLog(log_dir, run_id, store_text=store_llm_text),
            ValidationFailureLog(log_dir, run_id),
        )
