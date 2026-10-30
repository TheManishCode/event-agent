"""Saved deal leads: bookmarks of advertised prices, per plan, in an append-only JSONL file (ADR-038). A lead never prices anything."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from eventmate.jsonl import JsonlLog
from eventmate.schemas import DealLead


class _LeadFile(JsonlLog[DealLead]):
    channel = "lead"
    model = DealLead


class LeadStore:
    def __init__(self, root: Path):
        self.root = Path(root) / "leads"

    def _file(self, plan_id: str) -> _LeadFile:
        return _LeadFile(self.root, run_id=plan_id)  # the run_id pattern is the plan-id pattern, so the path is safe

    def list(self, plan_id: str) -> List[DealLead]:
        return self._file(plan_id).read()

    def add(self, plan_id: str, **fields) -> DealLead:
        log = self._file(plan_id)
        lead = DealLead(lead_id=uuid.uuid4().hex, plan_id=plan_id, saved_at=datetime.now(timezone.utc), **fields)
        for old in log.read():
            if (old.url, old.low_inr, old.high_inr, old.unit) == (lead.url, lead.low_inr, lead.high_inr, lead.unit):
                return old  # already saved
        return log._append(lead, lead.lead_id)
