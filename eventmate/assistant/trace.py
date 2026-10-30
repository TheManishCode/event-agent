"""Observability: one trace per question, one JSONL line, every step named.

A trace answers "why did it say that?": which route, which documents, which tools (with masked arguments), which model,
whether the cache answered, what the output guard decided. Text is PII-masked and truncated before it is written, and the
raw question is stored only as a hash.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from eventmate.assistant.guard import mask_pii
from eventmate.jsonl import JsonlLog
from eventmate.schemas import _Model


class Step(_Model):
    kind: str = Field(max_length=24)  # guard | route | cache | retrieve | llm | tool | policy | output
    name: str = Field(max_length=80)
    ok: bool = True
    detail: str = Field(default="", max_length=400)
    ms: float = Field(default=0, ge=0)


class TraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_id: str
    timestamp: datetime
    run_id: str
    request_id: str = Field(max_length=64)  # = trace id
    plan_id: str
    user_id: str
    roles: Tuple[str, ...]
    question_sha256: str
    question_masked: str = Field(max_length=240)
    answer_masked: str = Field(default="", max_length=600)
    route: str
    cache: str  # miss | hit | coalesced | skipped
    outcome: str  # answered | refused | degraded | error
    steps: Tuple[Step, ...]
    cited: Tuple[str, ...] = ()
    models: Tuple[str, ...] = ()
    prompt_sha256: str
    kb_version: str
    tokens_in_est: int = Field(ge=0)
    tokens_out_est: int = Field(ge=0)
    total_ms: float = Field(ge=0)


class ProposalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_id: str
    timestamp: datetime
    run_id: str
    request_id: str = Field(max_length=64)  # = proposal id
    event: str  # proposed | approved | rejected | stale | failed
    plan_id: str
    tool: str
    actor: str
    content_sha256: str
    detail: str = Field(default="", max_length=400)


class FeedbackEvent(BaseModel):
    """A thumbs up or down on an answer: a score on its trace (the Langfuse "score" idea)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    entry_id: str
    timestamp: datetime
    run_id: str
    request_id: str = Field(max_length=64)  # = trace id
    user_id: str
    value: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=200)


class FeedbackLog(JsonlLog[FeedbackEvent]):
    channel = "assistant_feedback"
    model = FeedbackEvent

    def write(self, **fields) -> FeedbackEvent:
        eid = uuid.uuid4().hex
        return self._append(FeedbackEvent(entry_id=eid, timestamp=datetime.now(timezone.utc), run_id=self.run_id, **fields), eid)


class TraceLog(JsonlLog[TraceEntry]):
    channel = "assistant_traces"
    model = TraceEntry

    def write(self, **fields) -> TraceEntry:
        eid = uuid.uuid4().hex
        return self._append(TraceEntry(entry_id=eid, timestamp=datetime.now(timezone.utc), run_id=self.run_id, **fields), eid)

    def find(self, trace_id: str) -> Optional[TraceEntry]:
        return next((e for e in reversed(self.read()) if e.request_id == trace_id), None)


class ProposalLog(JsonlLog[ProposalEvent]):
    channel = "assistant_approvals"
    model = ProposalEvent

    def write(self, **fields) -> ProposalEvent:
        eid = uuid.uuid4().hex
        return self._append(ProposalEvent(entry_id=eid, timestamp=datetime.now(timezone.utc), run_id=self.run_id, **fields), eid)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokens(text: str) -> int:
    return max(1, len(text) // 4)  # rough estimate, labelled as such wherever it is shown


class TraceBuilder:
    def __init__(self, plan_id: str, user_id: str, roles: Tuple[str, ...], question: str):
        self.id = uuid.uuid4().hex[:16]
        self.plan_id, self.user_id, self.roles, self.question = plan_id, user_id, roles, question
        self.steps: List[Step] = []
        self.models: List[str] = []
        self.t0 = time.perf_counter()
        self.tin = self.tout = 0

    def step(self, kind: str, name: str, ok: bool = True, detail: str = "", started: Optional[float] = None) -> None:
        ms = round((time.perf_counter() - (started if started is not None else self.t0)) * 1000, 2) if started is not None else 0
        self.steps.append(Step(kind=kind, name=name[:80], ok=ok, detail=mask_pii(detail)[:400], ms=ms))

    def llm(self, model: str, prompt: str, reply: str) -> None:
        self.models.append(model[:80])
        self.tin += tokens(prompt)
        self.tout += tokens(reply)
