"""Shortlists and contact candidates (ADR-049). Append-only; the latest record of a candidate wins.

A shortlist entry says "this business is worth a look for this plan and category". A contact candidate is an address software found, with the page or
listing it came from. Nothing here writes to the vendor database, and nothing here sends: an address is used only after the user confirms it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from eventmate.jsonl import InterProcessLock, JsonlLog
from eventmate.schemas import ContactCandidate, ShortlistEntry, VendorCategory


class _EntryFile(JsonlLog[ShortlistEntry]):
    channel = "shortlist"
    model = ShortlistEntry


class _CandidateFile(JsonlLog[ContactCandidate]):
    channel = "candidate"
    model = ContactCandidate


MAX_CANDIDATES_PER_BUSINESS = 5


class ShortlistStore:
    def __init__(self, root: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.root = Path(root) / "shortlist"
        self.now = now
        self._lock = InterProcessLock(self.root / ".lock")

    def _entries(self, plan_id: str) -> _EntryFile:
        return _EntryFile(self.root, run_id=plan_id)

    def _cands(self) -> _CandidateFile:
        return _CandidateFile(self.root, run_id="candidates")

    # ------------------------------------------------------------ entries

    def add_entry(self, plan_id: str, category: VendorCategory, vendor_id: str) -> ShortlistEntry:
        with self._lock():
            for e in self._entries(plan_id).read():
                if (e.category, e.vendor_id) == (category, vendor_id):
                    return e
            return self._entries(plan_id)._append(ShortlistEntry(plan_id=plan_id, category=category, vendor_id=vendor_id, added_at=self.now()), vendor_id)

    def entries(self, plan_id: str, category: Optional[VendorCategory] = None) -> List[ShortlistEntry]:
        return [e for e in self._entries(plan_id).read() if category is None or e.category == category]

    # --------------------------------------------------------- candidates

    def candidates(self, vendor_id: Optional[str] = None) -> List[ContactCandidate]:
        latest: Dict[str, ContactCandidate] = {}
        for c in self._cands().read():
            latest[c.candidate_id] = c
        return [c for c in latest.values() if vendor_id is None or c.vendor_id == vendor_id]

    def get_candidate(self, candidate_id: str) -> Optional[ContactCandidate]:
        return next((c for c in self.candidates() if c.candidate_id == candidate_id), None)

    def add_candidates(self, vendor_id: str, found: Sequence[dict]) -> List[ContactCandidate]:
        """Store what an extractor found, once per address. An address the user already rejected is not offered again."""
        added: List[ContactCandidate] = []
        with self._lock():
            have = self.candidates(vendor_id)
            seen = {(c.channel, c.value.casefold()) for c in have}
            room = MAX_CANDIDATES_PER_BUSINESS - len([c for c in have if c.status != "rejected"])
            for f in found:
                key = (f["channel"], f["value"].casefold())
                if key in seen or room <= 0:
                    continue
                cand = ContactCandidate(candidate_id=uuid.uuid4().hex, vendor_id=vendor_id, found_at=self.now(), **f)
                self._cands()._append(cand, cand.candidate_id)
                seen.add(key)
                added.append(cand)
                room -= 1
        return added

    def update(self, candidate_id: str, **changes) -> ContactCandidate:
        with self._lock():
            cur = self.get_candidate(candidate_id)
            if cur is None:
                raise ValueError(f"unknown contact candidate {candidate_id!r}")
            new = ContactCandidate.model_validate({**cur.model_dump(), **changes})
            return self._cands()._append(new, candidate_id)
