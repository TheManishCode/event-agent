"""The optional email copy of a job digest, to the owner only (ADR-047).

Off by default. Turning it on is two steps by the owner: propose an exact address and what the email may contain, read the summary, then confirm
with the code that binds the confirmation to exactly that. Changing the address or the scope puts it back to pending. It is a separate
owner-notification path but it passes the same gates as dispatch: the kill switch, the stop file, the 09:00-20:00 IST window, caps and logging.
It takes no recipient argument and refuses any vendor address, so it cannot be used to reach anyone else. It never includes the budget.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from eventmate.messaging.channels import Channel, ChannelError, OutcomeUnknown
from eventmate.messaging.conversations import IST
from eventmate.messaging.digests import DigestStore, safe_goal, scoped_text
from eventmate.followthrough.jobs import Job
from eventmate.jsonl import JsonlLog
from eventmate.schemas import NotifyLogEntry, NotifyProposal, NotifyScope, NotifySetting, NotifyStatus, _EMAIL

WINDOW = (9, 20)  # IST hours; the same window as automatic vendor messages


class NotifyError(ValueError):
    """A notification setting or confirmation was refused."""


@dataclass(frozen=True)
class NotifyLimits:
    per_day: int = 4
    min_gap_minutes: int = 30


class _SettingFile(JsonlLog[NotifySetting]):
    channel = "notifysetting"
    model = NotifySetting


class _LogFile(JsonlLog[NotifyLogEntry]):
    channel = "notifylog"
    model = NotifyLogEntry


def _digest_of(destination: str) -> str:
    return hashlib.sha256(destination.casefold().encode()).hexdigest()[:16]


class OwnerNotifier:
    def __init__(
        self, root: Path, digests: DigestStore, channel: Callable[[], Channel], jobs: Callable[[str], Iterable[Job]], names: Callable[[], Mapping[str, str]],
        vendor_addresses: Callable[[], set], own_addresses: Callable[[], set], killed: Callable[[], bool],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc), limits: NotifyLimits = NotifyLimits(),
    ):
        self.root = Path(root) / "notify"
        self.digests, self._channel, self._jobs, self._names = digests, channel, jobs, names
        self._vendor_addresses, self._own_addresses, self._killed = vendor_addresses, own_addresses, killed
        self.now, self.limits = now, limits
        self._lock = threading.RLock()

    # ------------------------------------------------------------ settings

    def _settings(self) -> _SettingFile:
        return _SettingFile(self.root, run_id="owner")

    def _log(self) -> "_LogFile":
        return _LogFile(self.root, run_id="owner")

    def _latest(self) -> Optional[NotifySetting]:
        items = self._settings().read()
        return items[-1] if items else None

    def status(self) -> NotifyStatus:
        cur = self._latest()
        sent = self._sent_since(self.now() - timedelta(days=1))
        base = dict(sent_last_day=len(sent), per_day=self.limits.per_day, min_gap_minutes=self.limits.min_gap_minutes)
        if cur is None or cur.status == "disabled":
            return NotifyStatus(state="off", **base)
        return NotifyStatus(state="on" if cur.status == "active" else "pending", destination=cur.destination, **cur.scope.model_dump(), **base)

    def propose(self, destination: str, include_vendor_names: bool = False, include_amounts: bool = False, include_raw_text: bool = False) -> NotifyProposal:
        destination = (destination or "").strip()
        if not _EMAIL.fullmatch(destination):
            raise NotifyError("give one plain email address (no names, lists or extra parameters)")
        low = destination.casefold()
        if low in {a.casefold() for a in self._vendor_addresses()}:
            raise NotifyError("that address belongs to a vendor you contact; updates only go to you")
        if low in {a.casefold() for a in self._own_addresses()}:
            raise NotifyError("that is the mailbox EventMate sends from; use your own address")
        scope = NotifyScope(include_vendor_names=include_vendor_names, include_amounts=include_amounts, include_raw_text=include_raw_text)
        at = self.now()
        code = hashlib.sha256(f"{low}|{scope.model_dump_json()}|{at.isoformat()}|{secrets.token_hex(8)}".encode()).hexdigest()
        with self._lock:
            self._settings()._append(NotifySetting(destination=destination, scope=scope, status="pending", code=code, proposed_at=at), code[:16])
        parts = ["the status of each job and how many quotes are confirmed"]
        if include_vendor_names:
            parts.append("vendor names")
        if include_amounts:
            parts.append("confirmed amounts")
        if include_raw_text:
            parts.append("the vendors' own wording")
        left_out = [w for w, on in (("vendor names", include_vendor_names), ("confirmed amounts", include_amounts), ("vendor message text", include_raw_text)) if not on]
        summary = (
            f"EventMate will email job updates to {destination} and to no one else. Each email will contain: {', '.join(parts)}. "
            + (f"It will not include: {', '.join(left_out)}, " if left_out else "It will not include: ")
            + "or your budget. Emails go out only between 09:00 and 20:00 IST, at most "
            + f"{self.limits.per_day} a day and {self.limits.min_gap_minutes} minutes apart, and stop at once when the kill switch is on."
        )
        return NotifyProposal(destination=destination, scope=scope, code=code, summary=summary)

    def confirm(self, code: str) -> NotifyStatus:
        with self._lock:
            cur = self._latest()
            if cur is None or cur.status != "pending" or not isinstance(code, str) or not secrets.compare_digest(code, cur.code):
                raise NotifyError("that confirmation does not match the latest proposal; propose again and confirm the new one")
            self._settings()._append(cur.model_copy(update={"status": "active", "confirmed_at": self.now()}), cur.code[:16])
        return self.status()

    def disable(self) -> NotifyStatus:
        with self._lock:
            cur = self._latest()
            if cur is not None and cur.status != "disabled":
                self._settings()._append(cur.model_copy(update={"status": "disabled"}), cur.code[:16])
        return self.status()

    # ------------------------------------------------------------ sending

    def _sent_since(self, since: datetime):
        return [e for e in self._log().read() if e.result == "sent" and e.at >= since]

    def flush(self) -> int:
        """Email the owner the newest digest of each job that has not been emailed, if every gate allows it. At most one email per call."""
        cur = self._latest()
        if cur is None or cur.status != "active" or self._killed():
            return 0
        stop = os.environ.get("EVENTMATE_WORKER_STOP", "")
        if stop and Path(stop).exists():
            return 0
        now = self.now()
        if not WINDOW[0] <= now.astimezone(IST).hour < WINDOW[1]:
            return 0
        recent = self._sent_since(now - timedelta(days=1))
        if len(recent) >= self.limits.per_day or (recent and now - max(e.at for e in recent) < timedelta(minutes=self.limits.min_gap_minutes)):
            return 0
        channel = self._channel()
        if not channel.status().can_send:
            return 0
        names = self._names()
        for plan_id in self.digests.plan_ids():
            newest = {}
            for d in self.digests.list(plan_id):  # newest first: keep the first per job
                newest.setdefault(d.job_id, d)
            for d in newest.values():
                if d.emailed_at is not None:
                    continue
                job = next((j for j in self._jobs(plan_id) if j.job_id == d.job_id), None)
                if job is None:
                    continue
                return self._send(cur, channel, d, scoped_text(job, names, cur.scope), safe_goal(job), now)
        return 0

    def _send(self, cur: NotifySetting, channel: Channel, d, body: str, goal: str, now: datetime) -> int:
        log = self._log()
        result = "sent"
        try:
            channel.send(cur.destination, f"EventMate update: {goal}"[:200], body, None)  # the only recipient is the confirmed owner address
        except OutcomeUnknown:
            result = "unknown"  # it may have gone out: recorded as emailed, never resent
        except ChannelError:
            result = "failed"  # nothing went out: left for a later round
        log._append(NotifyLogEntry(at=now, result=result, destination_hash=_digest_of(cur.destination), digest_id=d.digest_id, job_id=d.job_id, scope=cur.scope.model_dump()), d.digest_id)
        if result != "failed":
            self.digests.mark_emailed(d.plan_id, d.digest_id, result)
        return 1 if result == "sent" else 0
