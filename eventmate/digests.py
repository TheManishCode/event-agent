"""The job digest: what changed and what needs the owner, always written to the in-app inbox (ADR-047).

A digest is written only when its content changes, so a job that is merely waiting does not fill the inbox. The text names vendors by name, never
by id, and never contains the budget. An optional email copy is handled separately by ``notify.py`` and is off by default.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from eventmate.jsonl import JsonlLog
from eventmate.jobs import Job
from eventmate.schemas import DigestRecord

_NEXT_CHECK = re.compile(r";\s*next check\s+\S+")
_MONEY = re.compile(r"\d{4,}|₹|\b(?:inr|rs|rupees?)\b|lakh", re.I)
ITEM_LABEL = {
    "confirm_claim": "a price to confirm", "vendor_question": "a question from a vendor", "approve_requests": "requests to approve", "approve_nudge": "a follow-up to approve",
    "check_unknown_send": "a message whose delivery is unknown", "unclear_price": "a reply to read", "vendor_unavailable": "a vendor who is not available",
    "no_quotes_possible": "too few vendors left", "different_sender": "a reply from an unexpected address",
}


def _names(text: str, names: Mapping[str, str]) -> str:
    for vid, name in sorted(names.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(vid, name)
    return text


def digest_text(job: Job, names: Optional[Mapping[str, str]] = None) -> str:
    """Plain text for the owner in the app. Free of anything that changes only with the clock (the next-check time), so it changes only when something real does."""
    names = names or {}
    lines = [f"{job.goal} - {job.status.replace('_', ' ')}", _NEXT_CHECK.sub("", job.next_action)]
    for n, i in enumerate(job.open_items(), 1):
        lines.append(f"{n}. {i.text}")
    ok = job.confirmed()
    if ok:
        lines.append("Confirmed: " + "; ".join(f"{c.vendor_id} INR {c.amount_inr:,}{' per plate' if c.basis == 'per_plate' else ' total'}" for c in ok))
    return _names("\n".join(lines), names)


def safe_goal(job: Job) -> str:
    """The goal as the user wrote it, unless it mentions money: a budget typed into a goal must never leave the app."""
    return f"{job.min_quotes} {job.category} quotes" if _MONEY.search(job.goal) else job.goal


def scoped_text(job: Job, names: Mapping[str, str], scope) -> str:
    """The email copy: status and counts always; vendor names, amounts and vendor wording only when the owner ticked them. Never the budget or the cap."""
    open_items = job.open_items()
    lines = [f"EventMate update: {safe_goal(job)}", f"Status: {job.status.replace('_', ' ')}", f"Quotes confirmed: {len(job.confirmed())} of {job.min_quotes}"]
    if open_items:
        kinds = sorted({ITEM_LABEL.get(i.kind, "something to look at") for i in open_items})
        lines.append(f"Waiting for you: {len(open_items)} ({', '.join(kinds)})")
    if scope.include_vendor_names:
        lines.append("Vendors: " + "; ".join(f"{names.get(v, 'a vendor')} ({p.state})" for v, p in job.vendors.items()))
    if scope.include_amounts and job.confirmed():
        lines.append("Confirmed: " + "; ".join(
            (f"{names.get(c.vendor_id, 'a vendor')} " if scope.include_vendor_names else "") + f"INR {c.amount_inr:,}{' per plate' if c.basis == 'per_plate' else ' total'}" for c in job.confirmed()))
    if scope.include_raw_text and open_items:
        lines.append("Details:")
        lines += [f"- {_names(i.text, names)}" for i in open_items]
    lines += ["", "Open EventMate to act on this. Nothing was sent to anyone else."]
    return "\n".join(lines)


class _DigestFile(JsonlLog[DigestRecord]):
    channel = "digest"
    model = DigestRecord


class DigestStore:
    def __init__(self, root: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.root = Path(root) / "digests"
        self.now = now
        self._lock = threading.RLock()

    def _file(self, plan_id: str) -> _DigestFile:
        return _DigestFile(self.root, run_id=plan_id)

    def plan_ids(self) -> List[str]:
        return sorted(p.name[len("digest-"):-len(".jsonl")] for p in self.root.glob("digest-*.jsonl")) if self.root.exists() else []

    def list(self, plan_id: str) -> List[DigestRecord]:
        """Newest first (by when each was first written), latest state of each digest."""
        latest: Dict[str, DigestRecord] = {}
        for d in self._file(plan_id).read():
            latest[d.digest_id] = d  # a dict keeps the order of first appearance, so equal timestamps still sort correctly
        return list(reversed(list(latest.values())))

    def write(self, job: Job, text: str) -> Optional[DigestRecord]:
        """Append a digest if it differs from the job's latest one; otherwise nothing."""
        digest_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._lock:
            mine = [d for d in self.list(job.plan_id) if d.job_id == job.job_id]
            if mine and mine[0].content_hash == digest_hash:
                return None
            rec = DigestRecord(digest_id=uuid.uuid4().hex, job_id=job.job_id, plan_id=job.plan_id, created_at=self.now(), text=text[:4000], content_hash=digest_hash)
            return self._file(job.plan_id)._append(rec, rec.digest_id)

    def _update(self, plan_id: str, digest_id: str, **changes) -> DigestRecord:
        with self._lock:
            cur = next((d for d in self.list(plan_id) if d.digest_id == digest_id), None)
            if cur is None:
                raise ValueError(f"unknown digest {digest_id!r}")
            return self._file(plan_id)._append(cur.model_copy(update=changes), digest_id)

    def mark_read(self, plan_id: str, digest_id: str) -> DigestRecord:
        return self._update(plan_id, digest_id, read=True)

    def mark_emailed(self, plan_id: str, digest_id: str, result: str) -> DigestRecord:
        return self._update(plan_id, digest_id, emailed_at=self.now(), email_result=result)
