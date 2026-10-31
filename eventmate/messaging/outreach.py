"""Quote-request proposals: EventMate drafts, the user approves, the agent sends and follows (ADR-041, ADR-042).

A proposal is bound to a content hash. Approval is for exactly that text and recipient; any change needs a new proposal.
The text is built only from the plan's validated facts and a fixed checklist, never from a model, and never contains the
budget. A user's own follow-up words are cleaned and bounded but not rewritten.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import threading
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

from eventmate.jsonl import JsonlLog
from eventmate.schemas import (
    InboundMessage, Mandate, OutreachDraft, PlanVersion, VendorCategory, VendorContact, _EMAIL,
)

_SERVICE = {VendorCategory.VENUE: "a venue", VendorCategory.CATERER: "catering", VendorCategory.PHOTOGRAPHER: "photography"}
_PHONE_CHARS = re.compile(r"^\+?[0-9 ()./-]+$")
PROPOSAL_TTL = timedelta(hours=24)
MAX_FOLLOWUP_CHARS = 1000
MAX_FOLLOWUPS_PER_THREAD = 5
_utcnow = lambda: datetime.now(timezone.utc)  # noqa: E731


class _OutreachFile(JsonlLog[OutreachDraft]):
    channel = "outreach"
    model = OutreachDraft


class _InboundFile(JsonlLog[InboundMessage]):
    channel = "inbound"
    model = InboundMessage


class _MandateFile(JsonlLog[Mandate]):
    channel = "mandate"
    model = Mandate


class _ContactFile(JsonlLog[VendorContact]):
    channel = "contact"
    model = VendorContact


def normalise_phone(raw: str) -> str:
    """Digits with the country code. A bare 10-digit Indian mobile number (starts 6-9) gets 91; anything else needs + or 00."""
    raw = raw.strip()
    if not raw or not _PHONE_CHARS.fullmatch(raw):
        raise ValueError("phone number may only contain digits, spaces, dashes, brackets and a leading +")
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif re.fullmatch(r"[6-9][0-9]{9}", digits):
        digits = "91" + digits
    else:
        raise ValueError("start the number with + and the country code (a bare number must be a 10-digit Indian mobile)")
    if not re.fullmatch(r"[1-9][0-9]{7,14}", digits):
        raise ValueError("phone number must be 8-15 digits with a country code that does not start with 0")
    return digits


def _clean_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("sender name must be 1-80 characters without control characters")
    return name


def clean_followup(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not 1 <= len(text) <= MAX_FOLLOWUP_CHARS:
        raise ValueError(f"a follow-up must be 1-{MAX_FOLLOWUP_CHARS} characters")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise ValueError("control characters are not allowed")
    return text


_EVENT_NOUN = {"community": "community event", "other": "event"}


def _event_phrase(event_type: str) -> str:
    """"a wedding", "an engagement party", "a community event": the right article and a noun a person would say."""
    noun = _EVENT_NOUN.get(event_type, event_type.replace("_", " "))
    return ("an " if noun[0] in "aeiou" else "a ") + noun


def _message(plan: PlanVersion, category: VendorCategory, vendor_name: str, sender: str, checklist: Tuple[str, ...]) -> Tuple[str, str]:
    r = plan.request
    event = _event_phrase(r.event_type.value)
    subject = f"Quote request: {_SERVICE[category].replace('a venue', 'venue')} for {event} on {r.event_date.isoformat()}"
    lines = [
        f"Hello {vendor_name},",
        "",
        f"I am organising {event} in {r.city} on {r.event_date.isoformat()} for about {r.guest_count} guests and would like a quote for {_SERVICE[category]}.",
    ]
    if category == VendorCategory.CATERER and r.dietary:
        lines.append("Dietary needs: " + ", ".join(d.value.replace("_", " ") for d in r.dietary) + ".")
    lines += ["", "Please reply with:", "- whether you are available on that date", "- the total price and what it covers"]
    if checklist:
        lines.append("- for each of these, whether it is included or extra: " + ", ".join(checklist))
    lines += ["- any other charges (taxes, overtime, travel)", "- how long the quote is valid", "", f"Thank you,\n{sender}"]
    return subject, "\n".join(lines)


def _links(channel: str, recipient: str, subject: str, body: str) -> str:
    if channel == "email":
        return f"mailto:{recipient}?subject={quote(subject, safe='')}&body={quote(body, safe='')}"
    return f"https://wa.me/{recipient}?text={quote(body, safe='')}"


def _check_recipient(channel: str, recipient: str) -> str:
    if channel == "email":
        recipient = recipient.strip()
        if not _EMAIL.fullmatch(recipient):
            raise ValueError("not a valid email address (one address, no names or extra parameters)")
        return recipient
    if channel == "whatsapp":
        return normalise_phone(recipient)
    raise ValueError("channel must be email or whatsapp")


class OutreachStore:
    def __init__(self, root: Path, checklists: Dict[str, Tuple[str, ...]], now: Callable[[], datetime] = _utcnow):
        self.base = Path(root)
        self.root = self.base / "outreach"
        self.checklists = checklists
        self.now = now
        self._lock = threading.RLock()
        self._depth = 0

    @contextlib.contextmanager
    def _locked(self):
        """Mutual exclusion for read-modify-write steps: an in-process lock plus an exclusive file lock, so the web server and a separate
        worker process cannot both claim the same draft or store the same reply. Re-entrant within one thread."""
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.base / ".outreach.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # ------------------------------------------------------------ files

    def _file(self, plan_id: str) -> _OutreachFile:
        return _OutreachFile(self.root, run_id=plan_id)

    def _inbound(self, plan_id: str) -> _InboundFile:
        return _InboundFile(self.base / "inbound", run_id=plan_id)

    def _contacts(self) -> _ContactFile:
        return _ContactFile(self.base / "contacts", run_id="all")

    def plan_ids(self) -> List[str]:
        if not self.root.exists():
            return []
        return sorted(p.name[len("outreach-"):-len(".jsonl")] for p in self.root.glob("outreach-*.jsonl"))

    # ------------------------------------------------------------ drafts

    def list(self, plan_id: str) -> List[OutreachDraft]:
        """Latest state of every draft, in creation order. Earlier lines stay in the file."""
        latest: Dict[str, OutreachDraft] = {}
        for d in self._file(plan_id).read():
            latest[d.draft_id] = d
        return list(latest.values())

    def get(self, plan_id: str, draft_id: str) -> Optional[OutreachDraft]:
        return next((d for d in self.list(plan_id) if d.draft_id == draft_id), None)

    def transition(self, plan_id: str, draft_id: str, allowed: Iterable[str], **changes) -> OutreachDraft:
        """Compare-and-set: the draft moves only if it is still in one of the `allowed` states. The old line stays in the file."""
        with self._locked():
            cur = self.get(plan_id, draft_id)
            if cur is None:
                raise ValueError(f"unknown draft {draft_id!r}")
            if cur.status not in set(allowed):
                raise ValueError(f"the draft is {cur.status}, not one of {sorted(set(allowed))}")
            new = OutreachDraft.model_validate({**cur.model_dump(), **changes})
            return self._file(plan_id)._append(new, draft_id)

    def _new(self, plan_id: str, **fields) -> OutreachDraft:
        now = self.now()
        draft = OutreachDraft(draft_id=uuid.uuid4().hex, plan_id=plan_id, created_at=now, expires_at=now + PROPOSAL_TTL, **fields)
        return self._file(plan_id)._append(draft, draft.draft_id)

    def draft(self, plan: PlanVersion, category: VendorCategory, channel: str, recipient: str, sender_name: str, vendor: Optional[Tuple[str, str]] = None) -> OutreachDraft:
        """``vendor`` = (vendor_id, name) asks a vendor other than the plan's pick, to compare quotes (jobs). The caller has checked that it exists."""
        selection = plan.selection_for(category)
        if selection is None:
            raise ValueError(f"{category.value} is not in this plan")
        vendor_id, vendor_name = vendor if vendor is not None else (selection.vendor_id, selection.name)
        sender = _clean_name(sender_name)
        recipient = _check_recipient(channel, recipient)
        subject, body = _message(plan, category, vendor_name, sender, self.checklists.get(category.value, ()))
        subject = subject if channel == "email" else ""
        return self._new(
            plan.plan_id, category=category, vendor_id=vendor_id, vendor_name=vendor_name, channel=channel, recipient=recipient,
            subject=subject, body=body, link=_links(channel, recipient, subject, body),
        )

    def followup(self, plan_id: str, parent_id: str, text: str, sender_name: str, automated: bool = False, action_key: str = "") -> OutreachDraft:
        with self._locked():
            return self._followup_locked(plan_id, parent_id, text, sender_name, automated, action_key)

    def _followup_locked(self, plan_id: str, parent_id: str, text: str, sender_name: str, automated: bool, action_key: str) -> OutreachDraft:
        parent = self.get(plan_id, parent_id)
        if parent is None or parent.kind != "request":
            raise ValueError("unknown request to follow up")
        if parent.status != "sent":
            raise ValueError("a follow-up needs a request that has been sent")
        if sum(1 for d in self.list(plan_id) if d.parent_id == parent_id) >= MAX_FOLLOWUPS_PER_THREAD:
            raise ValueError(f"at most {MAX_FOLLOWUPS_PER_THREAD} follow-ups per request; ask the vendor on a call instead")
        sender = _clean_name(sender_name)
        if action_key and any(d.action_key == action_key and d.status not in ("denied", "expired") for d in self.list(plan_id)):
            raise ValueError("another action already claimed this response")  # two systems may see the same reply or silence; only one may answer it
        body = f"{clean_followup(text)}\n\nThank you,\n{sender}"
        if automated:  # the vendor is always told when no person wrote the message
            body += f"\n(Sent by an automated assistant on behalf of {sender}.)"
        subject = parent.subject if parent.subject.lower().startswith("re:") or not parent.subject else f"Re: {parent.subject}"
        return self._new(
            plan_id, category=parent.category, vendor_id=parent.vendor_id, vendor_name=parent.vendor_name, channel=parent.channel, recipient=parent.recipient,
            subject=subject, body=body, link=_links(parent.channel, parent.recipient, subject, body), kind="followup", parent_id=parent_id, action_key=action_key,
        )

    def mark_sent(self, plan_id: str, draft_id: str) -> OutreachDraft:
        """The user says they sent it themselves (the manual fallback). EventMate cannot know; the label says so."""
        current = self.get(plan_id, draft_id)
        if current is None:
            raise ValueError(f"unknown draft {draft_id!r}")
        if current.status == "marked_sent":
            return current
        return self.transition(plan_id, draft_id, ("prepared",), status="marked_sent", marked_at=self.now())

    # ------------------------------------------------------------ replies

    def inbound(self, plan_id: str) -> List[InboundMessage]:
        latest: Dict[str, InboundMessage] = {}
        for m in self._inbound(plan_id).read():
            latest[m.message_id] = m
        return sorted(latest.values(), key=lambda m: m.received_at)  # stable: equal timestamps keep arrival order

    def add_inbound(self, msg: InboundMessage) -> Optional[InboundMessage]:
        """Store a reply once. The same message (channel and external id) seen twice is ignored."""
        with self._locked():
            if any((m.channel, m.external_id) == (msg.channel, msg.external_id) for m in self.inbound(msg.plan_id)):
                return None
            return self._inbound(msg.plan_id)._append(msg, msg.message_id)

    def mark_read(self, plan_id: str, message_id: str) -> InboundMessage:
        with self._locked():
            cur = next((m for m in self.inbound(plan_id) if m.message_id == message_id), None)
            if cur is None:
                raise ValueError(f"unknown message {message_id!r}")
            if cur.read:
                return cur
            return self._inbound(plan_id)._append(cur.model_copy(update={"read": True}), message_id)

    def mark_auto_handled(self, plan_id: str, message_id: str) -> None:
        with self._locked():
            cur = next((m for m in self.inbound(plan_id) if m.message_id == message_id), None)
            if cur is not None and not cur.auto_handled:
                self._inbound(plan_id)._append(cur.model_copy(update={"auto_handled": True}), message_id)

    # ------------------------------------------------------------ standing permission (ADR-043)

    def _mandates(self, plan_id: str) -> _MandateFile:
        return _MandateFile(self.base / "mandates", run_id=plan_id)

    def mandate(self, plan_id: str) -> Optional[Mandate]:
        items = self._mandates(plan_id).read()
        return items[-1] if items else None

    def set_mandate(self, m: Mandate) -> Mandate:
        return self._mandates(m.plan_id)._append(m, m.plan_id)

    def revoke_mandate(self, plan_id: str) -> Optional[Mandate]:
        with self._locked():
            cur = self.mandate(plan_id)
            if cur is None or cur.revoked_at is not None:
                return cur
            return self._mandates(plan_id)._append(Mandate.model_validate({**cur.model_dump(), "revoked_at": self.now()}), plan_id)

    @property
    def _kill_file(self) -> Path:
        return self.base / "mandates" / "KILL"

    @property
    def killed(self) -> bool:
        return self._kill_file.exists()

    def set_killed(self, on: bool) -> None:
        """The global kill switch: a file, so it survives restarts and can be checked from anywhere. While it exists nothing automatic is sent."""
        self._kill_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if on:
            self._kill_file.touch(mode=0o600)
        else:
            self._kill_file.unlink(missing_ok=True)

    # ------------------------------------------------------------ contacts

    def contacts(self) -> Dict[str, VendorContact]:
        latest: Dict[str, VendorContact] = {}
        for c in self._contacts().read():
            latest[c.vendor_id] = c
        return latest

    def set_contact(self, vendor_id: str, email: Optional[str], whatsapp: Optional[str], preferred: str) -> VendorContact:
        email = _check_recipient("email", email) if email else None
        whatsapp = normalise_phone(whatsapp) if whatsapp else None
        contact = VendorContact(vendor_id=vendor_id, email=email, whatsapp=whatsapp, preferred=preferred, updated_at=self.now())
        return self._contacts()._append(contact, vendor_id)
