"""Durable jobs: the goal, its state, and what happens next (ADR-044; design: docs/JOBS_DESIGN.md).

A Job is "get N comparable quotes from these vendors by a deadline". The state machine owns the goal; no model does.
State lives in SQLite so a worker and the web process can both use it, and a crash loses nothing.
Every legal transition is listed in ``TRANSITIONS`` and every change is appended to the job's history.

Automatic nudges use the standing permission of ADR-043, so one kill switch and one set of limits cover everything. The job file is private (0600).
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

JobStatus = Literal["active", "waiting_vendor", "waiting_user", "blocked", "done", "expired", "cancelled"]
TERMINAL = ("done", "expired", "cancelled")
VendorState = Literal["drafted", "asked", "replied", "unavailable", "unresponsive"]
_ID = r"^[A-Za-z0-9_-]{1,64}$"

TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "active": ("waiting_vendor", "waiting_user", "blocked", "done", "expired", "cancelled"),
    "waiting_vendor": ("active", "waiting_user", "blocked", "done", "expired", "cancelled"),
    "waiting_user": ("active", "waiting_vendor", "blocked", "done", "expired", "cancelled"),
    "blocked": ("active", "waiting_vendor", "waiting_user", "expired", "cancelled"),
    "done": (), "expired": (), "cancelled": (),
}


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VendorProgress(_M):
    contact_channel: Literal["email", "whatsapp"]
    recipient: str
    draft_id: str
    state: VendorState = "drafted"
    asked_at: Optional[datetime] = None
    nudges: int = 0
    pending_nudge_draft_id: str = ""


class QuoteClaim(_M):
    """A price the vendor wrote, tied to the message it came from. It is a proposal until the user confirms it."""

    claim_id: str
    message_id: str
    vendor_id: str
    snippet: str = Field(max_length=200)
    amount_inr: int = Field(gt=0)
    basis: Literal["total", "per_plate"]
    status: Literal["proposed", "confirmed", "rejected", "superseded"] = "proposed"
    extractor: Literal["rule:reply_analysis"] = "rule:reply_analysis"
    stated_valid_days: Optional[int] = Field(default=None, ge=1, le=365)  # only when the vendor wrote it ("valid for 10 days"); never a default


class UserItem(_M):
    """Something only the user can decide. The job waits until every open item is resolved."""

    item_id: str
    kind: Literal["confirm_claim", "vendor_question", "approve_requests", "approve_nudge", "check_unknown_send", "unclear_price", "vendor_unavailable", "no_quotes_possible"]
    vendor_id: str = ""
    text: str = Field(max_length=600)
    ref: str = ""  # claim_id, message_id or draft_id
    resolved: bool = False


class Job(_M):
    job_id: str = Field(pattern=_ID)
    plan_id: str = Field(pattern=_ID)
    category: str
    goal: str = Field(max_length=300)
    sender_name: str = Field(min_length=1, max_length=80)
    min_quotes: int = Field(default=3, ge=1, le=10)
    max_amount_inr: Optional[int] = Field(default=None, gt=0)
    deadline: datetime
    status: JobStatus = "waiting_user"
    next_action: str = ""
    wake_at: Optional[datetime] = None
    reply_window_hours: int = Field(default=24, ge=1, le=168)
    poll_minutes: int = Field(default=15, ge=1, le=1440)
    max_nudges_per_vendor: int = Field(default=2, ge=0, le=5)
    vendors: Dict[str, VendorProgress]
    seen_message_ids: Tuple[str, ...] = ()
    claims: Tuple[QuoteClaim, ...] = ()
    items: Tuple[UserItem, ...] = ()
    version: int = 1
    created_at: datetime

    @field_validator("deadline", "created_at", "wake_at")
    @classmethod
    def _aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and (v.tzinfo is None or v.utcoffset() is None):
            raise ValueError("timestamps must carry a time zone (a naive time could mean the wrong moment)")
        return v

    def open_items(self) -> List[UserItem]:
        return [i for i in self.items if not i.resolved]

    def confirmed(self) -> List[QuoteClaim]:
        return [c for c in self.claims if c.status == "confirmed"]


class StaleJob(Exception):
    """Someone else changed this job first. Reload and decide again."""


class JobStore:
    def __init__(self, root: Path):
        self.path = Path(root) / "jobs.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, status TEXT NOT NULL, wake_at TEXT, version INTEGER NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL)")
        for suffix in ("", "-wal", "-shm"):  # the job file holds vendor contacts and quotes: owner only
            with contextlib.suppress(OSError):
                os.chmod(str(self.path) + suffix, 0o600)

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            yield db
        finally:
            db.close()

    @staticmethod
    def _iso(t: Optional[datetime]) -> Optional[str]:
        return None if t is None else t.astimezone(timezone.utc).isoformat()

    def create(self, job: Job, now: datetime) -> Job:
        with self._lock, self._db() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (job.job_id, job.status, self._iso(job.wake_at), job.version, job.model_dump_json()))
            db.execute("INSERT INTO events (job_id, ts, actor, event, detail) VALUES (?,?,?,?,?)", (job.job_id, self._iso(now), "system", "created", json.dumps({"goal": job.goal})))
        return job

    def get(self, job_id: str) -> Job:
        with self._db() as db:
            row = db.execute("SELECT data FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown job {job_id!r}")
        return Job.model_validate_json(row[0])

    def list(self, plan_id: Optional[str] = None) -> List[Job]:
        with self._db() as db:
            rows = db.execute("SELECT data FROM jobs ORDER BY rowid").fetchall()
        jobs = [Job.model_validate_json(r[0]) for r in rows]
        return [j for j in jobs if plan_id is None or j.plan_id == plan_id]

    def due(self, now: datetime, limit: int = 20) -> List[Job]:
        with self._db() as db:
            rows = db.execute(
                "SELECT data FROM jobs WHERE status NOT IN ('done','expired','cancelled') AND wake_at IS NOT NULL AND wake_at <= ? ORDER BY wake_at LIMIT ?",
                (self._iso(now), limit),
            ).fetchall()
        return [Job.model_validate_json(r[0]) for r in rows]

    def save(self, job: Job, actor: str, event: str, now: datetime, detail: Optional[dict] = None) -> Job:
        """Compare-and-swap on ``version``: only the writer who read the latest version wins. Illegal status changes are refused."""
        with self._lock, self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status, version FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise ValueError(f"unknown job {job.job_id!r}")
            old_status, old_version = row
            if old_version != job.version:
                db.execute("ROLLBACK")
                raise StaleJob(job.job_id)
            if job.status != old_status and job.status not in TRANSITIONS[old_status]:
                db.execute("ROLLBACK")
                raise ValueError(f"illegal job transition {old_status} -> {job.status}")
            new = job.model_copy(update={"version": job.version + 1})
            db.execute("UPDATE jobs SET status=?, wake_at=?, version=?, data=? WHERE job_id=?", (new.status, self._iso(new.wake_at), new.version, new.model_dump_json(), new.job_id))
            body = dict(detail or {})
            if new.status != old_status:
                body["from"], body["to"] = old_status, new.status
            db.execute("INSERT INTO events (job_id, ts, actor, event, detail) VALUES (?,?,?,?,?)", (new.job_id, self._iso(now), actor, event, json.dumps(body, default=str)))
            db.execute("COMMIT")
        return new

    def history(self, job_id: str) -> List[Tuple[str, str, str, dict]]:
        with self._db() as db:
            rows = db.execute("SELECT ts, actor, event, detail FROM events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        return [(ts, a, e, json.loads(d)) for ts, a, e, d in rows]
