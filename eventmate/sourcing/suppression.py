"""The do-not-contact list (ADR-049).

A suppression is about an address on a channel, not about a vendor, a job or a plan: one entry covers every job, every plan and every business record
that resolves to the same address, and it is checked when a message is about to be sent, so a draft that was queued or approved earlier is stopped too.

What may add one: a stop request in a reply that was tied to a message we sent (the thread headers and the address we wrote to), a permanent delivery failure
that names the Message-ID of a message we sent, or the user. None of this is authentication: thread headers, sender addresses and Message-IDs can all be
forged by someone who has received our mail. The effect of a forgery is bounded (it can only stop us writing to an address we already wrote to), it is
shown with its reason, and only the user lifts it. A message from anyone else, or one that only mentions stopping, adds nothing: it goes to the user to
review. Only the user can lift a suppression, and the lift records who and why.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from eventmate.jsonl import InterProcessLock, JsonlLog
from eventmate.schemas import Suppression, UnmatchedMessage, _EMAIL

# Providers known to ignore what follows a "+" in the local part, and (Gmail only) the dots. Everywhere else the address is compared exactly.
PLUS_IGNORED = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "fastmail.com"})
DOTS_IGNORED = frozenset({"gmail.com", "googlemail.com"})
DOMAIN_ALIAS = {"googlemail.com": "gmail.com"}


def normalise_address(channel: str, value: str) -> str:
    """The form two spellings of the same mailbox share, so one suppression covers them. Unknown providers are compared exactly (case-folded)."""
    if channel == "whatsapp":
        digits = "".join(c for c in (value or "") if c.isdigit())
        if not re.fullmatch(r"[1-9][0-9]{7,14}", digits):
            raise ValueError("not a valid WhatsApp number")
        return digits
    value = (value or "").strip()
    if channel != "email" or not _EMAIL.fullmatch(value):
        raise ValueError("not a valid email address")
    local, domain = value.casefold().rsplit("@", 1)
    domain = DOMAIN_ALIAS.get(domain, domain)
    if domain in PLUS_IGNORED:
        local = local.split("+", 1)[0]
    if domain in DOTS_IGNORED:
        local = local.replace(".", "")
    return f"{local}@{domain}"


def domain_note(channel: str, value: str) -> str:
    """What a suppression does not cover, shown next to it: other addresses at the same domain are separate."""
    if channel != "email":
        return "covers this number only"
    domain = value.rsplit("@", 1)[-1].casefold()
    return f"covers this address only; other addresses at {domain} are not blocked"


class NeedsAcknowledgement(ValueError):
    """Re-enabling an address that bounced needs the user to have read why. Carries the reason so a caller can show it."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(
            f"This address could not be delivered to earlier ({reason or 'no reason was given'}). That is the remote mail server's own wording and was not verified. "
            "Check the address, then confirm that you have read the reason."
        )


class _SuppressionFile(JsonlLog[Suppression]):
    channel = "suppression"
    model = Suppression


class _UnmatchedFile(JsonlLog[UnmatchedMessage]):
    channel = "unmatched"
    model = UnmatchedMessage


class SuppressionStore:
    def __init__(self, root: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.root = Path(root) / "suppression"
        self.now = now
        self._lock = InterProcessLock(self.root / ".lock")

    def _file(self) -> _SuppressionFile:
        return _SuppressionFile(self.root, run_id="all")

    def all(self) -> List[Suppression]:
        latest: Dict[str, Suppression] = {}
        for s in self._file().read():
            latest[s.suppression_id] = s
        return list(latest.values())

    def active_for(self, channel: str, value: str) -> Optional[Suppression]:
        try:
            key = normalise_address(channel, value)
        except ValueError:
            return None
        return next((s for s in self.all() if s.active and s.channel == channel and s.value == key), None)

    def add(self, channel: str, value: str, reason: str, source_ref: str, created_by: str, detail: str = "") -> Suppression:
        key = normalise_address(channel, value)
        with self._lock():
            have = self.active_for(channel, key)
            if have is not None:
                return have  # one suppression per address: a second stop request changes nothing
            s = Suppression(suppression_id=uuid.uuid4().hex, channel=channel, value=key, reason=reason, source_ref=source_ref[:400], detail=detail[:300], created_at=self.now(), created_by=created_by[:80])
            return self._file()._append(s, s.suppression_id)

    def lift(self, suppression_id: str, by: str, reason: str, acknowledge_reason: bool = False) -> Suppression:
        """Only a person lifts a suppression, and the record says who and why. A bad address also needs them to have read the failure reason."""
        if not (by or "").strip() or not (reason or "").strip():
            raise ValueError("say who is lifting it and why")
        with self._lock():
            cur = next((s for s in self.all() if s.suppression_id == suppression_id), None)
            if cur is None:
                raise ValueError(f"unknown suppression {suppression_id!r}")
            if not cur.active:
                return cur
            if cur.reason == "bad_address" and acknowledge_reason is not True:
                raise NeedsAcknowledgement(cur.detail)
            new = Suppression.model_validate({**cur.model_dump(), "lifted_at": self.now(), "lifted_by": by.strip()[:80], "lift_reason": reason.strip()[:200]})
            return self._file()._append(new, suppression_id)

    def lift_bad_address(self, channel: str, value: str, by: str, reason: str, acknowledge_reason: bool = False) -> Optional[Suppression]:
        """A bad address is cleared when the user edits or re-confirms the contact, having read why it failed. A stop request or the user's own entry is never cleared this way."""
        cur = self.active_for(channel, value)
        return self.lift(cur.suppression_id, by, reason, acknowledge_reason) if cur is not None and cur.reason == "bad_address" else None


class UnmatchedStore:
    """Mail that looks like a stop request or a bounce but is not tied to anything we sent. It changes nothing; it is shown to the user."""

    def __init__(self, root: Path):
        self.root = Path(root) / "unmatched"
        self._lock = InterProcessLock(self.root / ".lock")

    def _file(self) -> _UnmatchedFile:
        return _UnmatchedFile(self.root, run_id="all")

    def all(self) -> List[UnmatchedMessage]:
        latest: Dict[str, UnmatchedMessage] = {}
        for m in self._file().read():
            latest[m.message_id] = m
        return list(latest.values())

    def add(self, channel: str, sender: str, received_at: datetime, external_id: str, kind: str, body: str) -> Optional[UnmatchedMessage]:
        with self._lock():
            if any((m.channel, m.external_id) == (channel, external_id) for m in self.all()):
                return None
            m = UnmatchedMessage(message_id=uuid.uuid4().hex, channel=channel, sender=sender[:254], received_at=received_at, external_id=external_id[:400], kind=kind, body=body[:1000])
            return self._file()._append(m, m.message_id)

    def get(self, message_id: str) -> Optional[UnmatchedMessage]:
        return next((m for m in self.all() if m.message_id == message_id), None)

    def dismiss(self, message_id: str) -> UnmatchedMessage:
        with self._lock():
            m = self.get(message_id)
            if m is None:
                raise ValueError(f"unknown message {message_id!r}")
            return m if m.reviewed else self._file()._append(m.model_copy(update={"reviewed": True}), message_id)
