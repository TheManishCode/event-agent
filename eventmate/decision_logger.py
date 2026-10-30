"""Append-only JSONL decision log (``decisions-<run_id>.jsonl``)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from eventmate.jsonl import DEFAULT_LOG_DIR, RUN_ID_PATTERN, JsonlLog

__all__ = ["DecisionLogger", "DEFAULT_LOG_DIR", "RUN_ID_PATTERN"]  # the last two stay importable from here
from eventmate.schemas import DecisionLogEntry


class DecisionLogger(JsonlLog[DecisionLogEntry]):
    channel = "decisions"
    model = DecisionLogEntry

    def log(self, **fields) -> DecisionLogEntry:
        """Validate, then append one entry. Raises before writing if invalid."""
        entry = DecisionLogEntry(
            entry_id=uuid.uuid4().hex,
            timestamp=datetime.now(timezone.utc),
            run_id=self.run_id,
            **fields,
        )
        return self._append(entry, entry.entry_id)
