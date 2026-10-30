"""The vendor-conversation agent (ADR-042): it sends approved messages, reads replies and tells the user what came back.

Patterns taken from OpenMuse (MIT, read 2026-10-30; no code copied): a proposal is bound to a content hash, expires, and is approved or
denied once; execution is claimed before it happens so it cannot run twice; an outcome that cannot be known is recorded as unknown and
never retried; a worker loop does the slow part; the user sees an inbox of what needs them. Rules that stay from the project:

* nothing leaves without an approval of exactly that text (the hash), and never past its expiry;
* the agent writes only fixed templates or the user's own words, never a price, a promise or the budget;
* a vendor's reply is untrusted text. The analysis quotes it; it does not record a quote or change a plan (ADR-025);
* limits are code: sends per vendor and per day, a gap between WhatsApp messages, follow-ups per thread.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from eventmate.messaging.channels import Channel, ChannelError, Inbound, KnownThread, OutcomeUnknown, ThreadRef
from eventmate.integrations.deals import extract_mentions
from eventmate.messaging.outreach import OutreachStore
from eventmate.schemas import (
    AmountMention, AutoreplyStatus, ConversationThread, Inbox, InboundMessage, Mandate, OutreachDraft, PlanVersion, PollResult, ReplyAnalysis,
)

LEASE = timedelta(minutes=5)  # a send claimed longer ago than this and never finished is recorded as unknown
LOOKBACK = timedelta(days=30)  # threads older than this are no longer polled
# Hints only. "is booked" alone is left out on purpose: "your slot is booked" is a confirmation, not a refusal.
_UNAVAILABLE = re.compile(
    r"\b(?:not available|unavailable|fully booked|already booked|booked out|sold out|no slots?|not free|not open|"
    r"cannot (?:do|make|cater|accommodate|take)|can'?t (?:do|make|cater|accommodate|take))\b", re.I,
)
_AVAILABLE = re.compile(r"\b(?:available|we can do|we are free|slot is (?:open|free)|date is (?:open|free))\b", re.I)
_STOP = re.compile(
    r"\b(?:stop (?:messaging|contacting|texting|sending|calling|emailing)|(?:don'?t|do not|never) (?:message|contact|text|call|email|write to)|unsubscribe|wrong number|not interested|remove (?:me|my number|my email))\b|^\s*stop[.!\s]*$",
    re.I,
)
_VALIDITY = re.compile(r"\b(?:valid|validity|expires?|expiry|until|till)\b", re.I)
IST = timezone(timedelta(hours=5, minutes=30))  # India has no daylight saving; the sending window is in IST
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


@dataclass(frozen=True)
class SendPolicy:
    per_vendor_per_day: int = 3
    per_day: int = 30
    whatsapp_gap_s: int = 20  # WhatsApp restricts accounts that send in bursts


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def analyze_reply(body: str, checklist: Sequence[str]) -> ReplyAnalysis:
    """Rule-based reading of a reply. It quotes the vendor's words; it never decides a fact."""
    amounts = tuple(
        AmountMention(low_inr=m.low, high_inr=m.high, unit=m.unit, snippet=m.snippet) for m in extract_mentions(body)[:5]
    )
    unavailable = bool(_UNAVAILABLE.search(body))
    questions = tuple(s.strip()[:200] for s in _SENTENCE.split(body) if s.strip().endswith("?"))[:5]
    text = _norm(body)
    return ReplyAnalysis(
        amounts=amounts, says_unavailable=unavailable, says_available=bool(_AVAILABLE.search(body)) and not unavailable, questions=questions, asks_to_stop=bool(_STOP.search(body)),
        checklist_mentioned=tuple(item for item in checklist if _norm(item) in text),
    )


def suggest_followups(thread_replies: Sequence[InboundMessage], checklist: Sequence[str]) -> List[str]:
    """Fixed-template follow-ups for the user to pick from. They ask; they never offer a price or accept one."""
    if not thread_replies:
        return []
    last = thread_replies[-1].analysis
    out: List[str] = []
    if last.says_unavailable:
        return ["Thank you for letting me know. Do you have another date close to this one, or can you suggest someone who is free?"]
    if not last.amounts:
        out.append("Thank you for your reply. Could you share the total price and what it covers?")
    if checklist:
        missing = [c for c in checklist if c not in last.checklist_mentioned]
        if missing:
            out.append("Thank you. Could you also tell me whether each of these is included or extra: " + ", ".join(missing) + "?")
    if not last.says_available:
        out.append("Could you confirm that you are available on the date in my first message?")
    if not _VALIDITY.search(thread_replies[-1].body):
        out.append("Could you tell me how long this quote is valid, and any charges not mentioned so far (taxes, overtime, travel)?")
    return out[:4]


class ConversationService:
    def __init__(
        self, store: OutreachStore, channels: Dict[str, Channel], plan_lookup: Callable[[str], PlanVersion],
        policy: SendPolicy = SendPolicy(), now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.store, self.channels, self.plan_lookup, self.policy, self.now = store, channels, plan_lookup, policy, now
        self._last_whatsapp: Optional[datetime] = None
        self.guards: List[Callable[[OutreachDraft], Optional[str]]] = []  # each may veto an automatic send at dispatch time (for example: its job was cancelled)

    # ------------------------------------------------------------ approval

    def approve(self, plan_id: str, draft_id: str, content_hash: str) -> OutreachDraft:
        """Approve exactly the content the user saw. The send happens now if the limits allow, otherwise on the next dispatch."""
        d = self.store.get(plan_id, draft_id)
        if d is None:
            raise ValueError(f"unknown draft {draft_id!r}")
        if d.content_hash != content_hash:
            raise ValueError("This draft changed. Open its latest version before approving.")
        if d.status in ("approved", "sending", "sent"):
            return d  # approving twice never sends twice
        if d.status != "prepared":
            raise ValueError(f"This draft is {d.status}; write a new one.")
        if d.expires_at is not None and d.expires_at <= self.now():
            self.store.transition(plan_id, draft_id, ("prepared",), status="expired", decided_at=self.now())
            raise ValueError("This approval expired. Write a fresh draft.")
        st = self.channels[d.channel].status()
        if not st.can_send:
            raise ValueError(f"{d.channel} sending is not set up ({st.detail}). Use the link to send it yourself, then mark it sent.")
        self.store.transition(plan_id, draft_id, ("prepared",), status="approved", decided_at=self.now())
        self.dispatch()
        return self.store.get(plan_id, draft_id)

    def deny(self, plan_id: str, draft_id: str) -> OutreachDraft:
        d = self.store.get(plan_id, draft_id)
        if d is not None and d.status == "denied":
            return d
        return self.store.transition(plan_id, draft_id, ("prepared", "approved"), status="denied", decided_at=self.now())

    # ------------------------------------------------------------ sending

    def _sent_since(self, since: datetime) -> List[OutreachDraft]:
        return [d for pid in self.store.plan_ids() for d in self.store.list(pid) if d.status == "sent" and d.sent_at is not None and d.sent_at >= since]

    def dispatch(self) -> Tuple[int, List[str]]:
        """Send what the user approved, within the limits. Returns (sent, reasons something is still waiting)."""
        now, sent, waiting = self.now(), 0, []
        day = now - timedelta(days=1)
        for pid in self.store.plan_ids():
            for d in self.store.list(pid):
                if d.status == "sending" and d.decided_at is not None and now - d.decided_at > LEASE:
                    self.store.transition(pid, d.draft_id, ("sending",), status="outcome_unknown", error="the app stopped while sending; check the vendor chat before trying again")
        for pid in self.store.plan_ids():
            for d in [x for x in self.store.list(pid) if x.status == "approved"]:
                if d.approved_by == "mandate":
                    veto = next((why for why in (g(d) for g in self.guards) if why), None)
                    if veto:
                        self.store.transition(pid, d.draft_id, ("approved",), status="denied", error=veto[:300])
                        continue
                    st = self.autoreply_status(pid)
                    if not st.active:  # revoked, expired or killed after the message was queued: it never goes out
                        self.store.transition(pid, d.draft_id, ("approved",), status="denied", error="the standing permission ended before this was sent")
                        continue
                    local = now.astimezone(IST).hour
                    if not st.mandate.window_start <= local < st.mandate.window_end:
                        waiting.append(f"{d.vendor_name}: automatic messages wait for the sending window ({st.mandate.window_start:02d}:00-{st.mandate.window_end:02d}:00 IST)")
                        continue
                recent = self._sent_since(day)
                if len(recent) >= self.policy.per_day:
                    waiting.append(f"daily limit of {self.policy.per_day} messages reached")
                    return sent, waiting
                if sum(1 for r in recent if r.vendor_id == d.vendor_id) >= self.policy.per_vendor_per_day:
                    waiting.append(f"{d.vendor_name}: {self.policy.per_vendor_per_day} messages in 24 hours is the limit")
                    continue
                if d.channel == "whatsapp" and self._last_whatsapp is not None and (now - self._last_whatsapp).total_seconds() < self.policy.whatsapp_gap_s:
                    waiting.append(f"{d.vendor_name}: WhatsApp messages are spaced {self.policy.whatsapp_gap_s} seconds apart")
                    continue
                try:
                    self.store.transition(pid, d.draft_id, ("approved",), status="sending", decided_at=now)  # the claim: only one dispatcher gets past this
                except ValueError:
                    continue
                sent += self._send_one(pid, d, now)
        return sent, waiting

    def _send_one(self, pid: str, d: OutreachDraft, now: datetime) -> int:
        thread = None
        if d.kind == "followup":
            parent = self.store.get(pid, d.parent_id)
            if parent and parent.external_id:
                thread = ThreadRef(in_reply_to=parent.external_id, references=parent.external_id)
        try:
            ext = self.channels[d.channel].send(d.recipient, d.subject, d.body, thread)
        except OutcomeUnknown as exc:
            self.store.transition(pid, d.draft_id, ("sending",), status="outcome_unknown", error=str(exc)[:300])
            return 0
        except ChannelError as exc:
            self.store.transition(pid, d.draft_id, ("sending",), status="failed", error=str(exc)[:300])
            return 0
        if d.channel == "whatsapp":
            self._last_whatsapp = now
        self.store.transition(pid, d.draft_id, ("sending",), status="sent", sent_at=self.now(), external_id=ext)
        return 1

    # ------------------------------------------------------------ replies

    def _threads(self) -> List[Tuple[str, OutreachDraft]]:
        since = self.now() - LOOKBACK
        return [(pid, d) for pid in self.store.plan_ids() for d in self.store.list(pid) if d.status == "sent" and d.sent_at is not None and d.sent_at >= since]

    def poll(self) -> PollResult:
        """Send what is due, read every channel for replies, let a standing permission answer routine ones, then send again. A failing channel is reported, not fatal."""
        sent, waiting = self.dispatch()
        sent_drafts = self._threads()
        new = ignored = 0
        touched: List[Tuple[str, str]] = []
        errors: List[str] = []
        for name, channel in self.channels.items():
            known = {}
            for pid, d in sent_drafts:
                if d.channel == name:
                    key = (d.vendor_id, d.recipient)
                    if key not in known or d.sent_at > known[key][1].sent_at:
                        known[key] = (pid, d)
            if not known or not channel.status().can_receive:
                continue
            try:
                inbound = channel.poll([KnownThread(d.draft_id, d.recipient, d.sent_at, d.external_id) for _, d in known.values()])
            except ChannelError as exc:
                errors.append(f"{name}: {exc}")
                continue
            for item in inbound:
                match = self._match(item, [(pid, d) for pid, d in sent_drafts if d.channel == name])
                if match is None:
                    ignored += 1
                    continue
                stored = self._store(item, *match)
                if stored is not None:
                    new += 1
                    touched.append((match[0], stored.draft_id))
                else:
                    ignored += 1
        queued = sum(self._auto_reply(pid, root) for pid, root in dict.fromkeys(touched))
        if queued:
            more, waiting = self.dispatch()
            sent += more
        return PollResult(new_messages=new, ignored=ignored, sent=sent, waiting=tuple(waiting), errors=tuple(errors))

    @staticmethod
    def _match(item: Inbound, candidates: List[Tuple[str, OutreachDraft]]) -> Optional[Tuple[str, OutreachDraft]]:
        """Which sent message does this answer? An email's reply headers first, then the sender's address; strangers match nothing."""
        refs = set((item.in_reply_to + " " + item.references).split())
        if item.channel == "email":
            by_header = [(p, d) for p, d in candidates if d.external_id and d.external_id in refs]
            if by_header:
                return by_header[-1]
        same = [(p, d) for p, d in candidates if d.recipient.lower() == item.sender.lower() and item.received_at >= d.sent_at - timedelta(seconds=60)]
        return max(same, key=lambda pd: pd[1].sent_at) if same else None

    def _store(self, item: Inbound, pid: str, d: OutreachDraft) -> Optional[InboundMessage]:
        checklist = self.store.checklists.get(d.category.value, ())
        msg = InboundMessage(
            message_id=uuid.uuid4().hex, plan_id=pid, draft_id=d.parent_id or d.draft_id, vendor_id=d.vendor_id, vendor_name=d.vendor_name, category=d.category,
            channel=item.channel, sender=item.sender, received_at=item.received_at, external_id=item.external_id, body=item.body, truncated=item.truncated,
            attachments=item.attachments, analysis=analyze_reply(item.body, checklist),
            sender_differs=item.channel == "email" and item.sender.lower() != d.recipient.lower(),
            auto_handled=not self.autoreply_status(pid).active,  # a reply that arrives while no permission is active can never be answered automatically later
        )
        return self.store.add_inbound(msg)

    # ------------------------------------------------------------ standing permission (ADR-043)

    def grant_autoreply(self, plan_id: str, sender_name: str, hours: int = 72, max_per_thread: int = 3, max_total: int = 10) -> Mandate:
        self.plan_lookup(plan_id)
        if self.store.killed:
            raise ValueError("the kill switch is on; resume automatic replies first")
        if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 168:
            raise ValueError("hours must be a whole number from 1 to 168")
        now = self.now()
        return self.store.set_mandate(Mandate(plan_id=plan_id, sender_name=sender_name, granted_at=now, expires_at=now + timedelta(hours=hours), max_per_thread=max_per_thread, max_total=max_total))

    def revoke_autoreply(self, plan_id: str) -> AutoreplyStatus:
        self.plan_lookup(plan_id)
        self.store.revoke_mandate(plan_id)
        return self.autoreply_status(plan_id)

    def kill_all(self) -> None:
        self.store.set_killed(True)

    def resume_all(self) -> None:
        self.store.set_killed(False)

    def autoreply_status(self, plan_id: str) -> AutoreplyStatus:
        m, killed = self.store.mandate(plan_id), self.store.killed
        used = sum(1 for d in self.store.list(plan_id) if d.approved_by == "mandate" and d.status != "denied")
        return AutoreplyStatus(plan_id=plan_id, active=bool(m and not killed and m.active_at(self.now())), killed=killed, mandate=m, sent_total=used)

    def _auto_reply(self, pid: str, root: str) -> int:
        """Under an active permission, answer the newest reply with one fixed follow-up question, or leave it to the user. Returns 1 if a message was queued."""
        st = self.autoreply_status(pid)
        replies = [m for m in self.store.inbound(pid) if m.draft_id == root]
        fresh = [m for m in replies if not m.auto_handled]
        if not st.active or not fresh:
            return 0
        last = fresh[-1]
        for m in fresh:
            self.store.mark_auto_handled(pid, m.message_id)  # each reply is looked at once, whatever the outcome
        if any(r.analysis.asks_to_stop for r in replies):
            return 0
        if any(r.analysis.questions or r.analysis.says_unavailable or r.attachments or r.sender_differs for r in fresh):
            return 0  # a person has to read this: a question (even one an earlier reply in this round asked), a refusal, a file, or an unexpected sender
        req = self.store.get(pid, root)
        options = suggest_followups(replies, self.store.checklists.get(req.category.value, ())) if req else []
        if not options:
            return 0  # nothing left to ask
        return 1 if self.queue_automatic(pid, root, options[0], f"reply:{last.message_id}")[0] is not None else 0

    def queue_automatic(self, pid: str, root: str, text: str, action_key: str) -> Tuple[Optional[OutreachDraft], str]:
        """The one way anything is sent without a per-message approval (ADR-043, ADR-044): checks the standing permission and its caps, marks the
        draft as automatic with the disclosure line, and leaves the rest (window, kill switch, send limits) to dispatch. Returns (draft, reason)."""
        st = self.autoreply_status(pid)
        if st.killed:
            return None, "the kill switch is on"
        if not st.active:
            return None, "no standing permission is active for this plan"
        mine = [d for d in self.store.list(pid) if d.approved_by == "mandate" and d.status != "denied"]
        if sum(1 for d in mine if d.parent_id == root) >= st.mandate.max_per_thread:
            return None, f"the limit of {st.mandate.max_per_thread} automatic messages for this vendor is reached"
        if len(mine) >= st.mandate.max_total:
            return None, f"the limit of {st.mandate.max_total} automatic messages for this plan is reached"
        try:
            draft = self.store.followup(pid, root, text, st.mandate.sender_name, automated=True, action_key=action_key)
        except ValueError as exc:
            if "already claimed" in str(exc):
                return None, "another action already claimed this response"
            raise
        self.store.transition(pid, draft.draft_id, ("prepared",), status="approved", approved_by="mandate", decided_at=self.now())
        return self.store.get(pid, draft.draft_id), ""

    # ------------------------------------------------------------ the user's view

    def inbox(self, plan_id: str) -> Inbox:
        self.plan_lookup(plan_id)  # unknown or hostile ids fail here
        drafts, replies = self.store.list(plan_id), self.store.inbound(plan_id)
        threads = []
        for req in (d for d in drafts if d.kind == "request"):
            mine = tuple(d for d in drafts if d.draft_id == req.draft_id or d.parent_id == req.draft_id)
            got = tuple(m for m in replies if m.draft_id == req.draft_id)
            threads.append(ConversationThread(
                request=req, drafts=mine, replies=got, unread=sum(1 for m in got if not m.read),
                awaiting_approval=sum(1 for d in mine if d.status == "prepared"),
            ))
        return Inbox(
            plan_id=plan_id, threads=tuple(threads), unread_total=sum(t.unread for t in threads), approvals_total=sum(t.awaiting_approval for t in threads),
            channels=tuple(c.status() for c in self.channels.values()),
        )

    def suggestions(self, plan_id: str, draft_id: str) -> List[str]:
        req = self.store.get(plan_id, draft_id)
        if req is None or req.kind != "request":
            raise ValueError("unknown request")
        replies = [m for m in self.store.inbound(plan_id) if m.draft_id == draft_id]
        return suggest_followups(replies, self.store.checklists.get(req.category.value, ()))

    def request_all(self, plan_id: str, sender_name: str) -> Tuple[List[OutreachDraft], List[str]]:
        """One proposal per vendor in the plan that has a saved contact and no live request. The user still approves each one."""
        plan = self.plan_lookup(plan_id)
        contacts, existing = self.store.contacts(), self.store.list(plan_id)
        made, skipped = [], []
        for s in plan.selections:
            live = any(d.vendor_id == s.vendor_id and d.kind == "request" and d.status in ("prepared", "approved", "sending", "sent") for d in existing)
            c = contacts.get(s.vendor_id)
            if live:
                skipped.append(f"{s.name}: already asked")
            elif c is None:
                skipped.append(f"{s.name}: no contact saved")
            else:
                to = c.email if c.preferred == "email" else "+" + c.whatsapp  # stored as digits; the parser wants the country-code marker
                made.append(self.store.draft(plan, s.category, c.preferred, to, sender_name))
        return made, skipped
