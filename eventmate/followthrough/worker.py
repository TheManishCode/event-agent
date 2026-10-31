"""The follow-through worker: wakes due jobs, reads what changed, takes only allowed actions, and always ends in a named state (ADR-044).

It sends nothing by itself except what an existing per-message approval or the standing permission of ADR-043 covers, and it never books,
pays or states a price. Rules decide everything; no model is involved. Run once: ``python -m eventmate.followthrough.worker --once``. Loop: ``--every 60``.
Stop the loop by creating the file named in EVENTMATE_WORKER_STOP; stop automatic messages with the kill switch (``inbox kill``).
Automatic nudges go through ``ConversationService.queue_automatic`` (one permission, one kill switch, the window and the automation disclosure);
confirming a claim needs the validity the user chooses; the vendor list can come from saved contacts.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from eventmate.followthrough.claims import extract_claims
from eventmate.messaging.digests import digest_text
from eventmate.followthrough.jobs import TERMINAL, Job, JobStore, QuoteClaim, StaleJob, UserItem, VendorProgress
from eventmate.schemas import ObservationSource, VendorCategory

NUDGE_TEXT = "Just checking whether you had a chance to look at our quote request. Could you share your price and availability for the date?"
MAX_JOBS_PER_TICK = 20
CONFIRM_CONFIDENCE = 0.7  # evidence weight for a vendor price the user confirmed from a message (the quote ledger's 0-1 scale)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _item(kind: str, text: str, vendor_id: str = "", ref: str = "") -> UserItem:
    return UserItem(item_id=uuid.uuid4().hex[:12], kind=kind, vendor_id=vendor_id, text=text[:600], ref=ref)


VendorSpec = Union[Dict[str, Tuple[str, str]], Sequence[str]]


class JobWorker:
    def __init__(self, em, store: JobStore, now: Callable[[], datetime] = _utcnow):
        self.em, self.store, self.now = em, store, now

    def _persist(self, job: Job, actor: str, event: str, now: datetime, detail: Optional[dict] = None) -> Job:
        """Save the job, and write a digest to the in-app inbox if what the owner should know has changed."""
        saved = self.store.save(job, actor, event, now, detail)
        self.em.digest_store.write(saved, digest_text(saved, self.em._vendor_names()))
        return saved

    # ------------------------------------------------------------ creating and deciding (the user's side)

    def _contacts_for(self, vendor_ids: Sequence[str]) -> Dict[str, Tuple[str, str]]:
        contacts = self.em.outreach_store.contacts()
        out: Dict[str, Tuple[str, str]] = {}
        for vid in vendor_ids:
            c = contacts.get(vid)
            if c is None:
                raise ValueError(f"no saved contact for {vid}: save one first")
            if vid.startswith("ext_") and not c.confirmed_by:
                raise ValueError(f"the contact for {vid} was not confirmed by you")
            out[vid] = (c.preferred, c.email if c.preferred == "email" else "+" + c.whatsapp)  # whatsapp is stored as digits; the parser wants the +
        return out

    def start_job(
        self, plan_id: str, category: VendorCategory, vendors: VendorSpec, goal: str, deadline: datetime, sender_name: str,
        min_quotes: int = 3, max_amount_inr: Optional[int] = None, max_nudges_per_vendor: int = 2, reply_window_hours: int = 24,
    ) -> Job:
        """Draft one quote request per vendor. Nothing is sent: the job waits for the user to approve the exact drafts.
        ``vendors`` is {vendor_id: (channel, recipient)} or a list of vendor ids with saved contacts."""
        spec = dict(vendors) if isinstance(vendors, dict) else self._contacts_for(list(vendors))
        if len(spec) < min_quotes:
            raise ValueError(f"asking {len(spec)} vendor(s) cannot give {min_quotes} quotes")
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("the deadline must carry a time zone (a naive time could mean the wrong moment)")
        if deadline <= self.now():
            raise ValueError("the deadline must be in the future")
        drafts = [(vid, ch, to) for vid, (ch, to) in spec.items()]
        progress: Dict[str, VendorProgress] = {}
        for vid, ch, to in drafts:
            d = self.em.draft_outreach(plan_id, category, ch, to, sender_name, vendor_id=vid)
            progress[vid] = VendorProgress(contact_channel=ch, recipient=d.recipient, draft_id=d.draft_id)
        job = Job(
            job_id=uuid.uuid4().hex[:16], plan_id=plan_id, category=category.value, goal=goal, sender_name=sender_name, min_quotes=min_quotes,
            max_amount_inr=max_amount_inr, deadline=deadline, status="waiting_user", vendors=progress, reply_window_hours=reply_window_hours,
            max_nudges_per_vendor=max_nudges_per_vendor, created_at=self.now(),
            items=(_item("approve_requests", f"{len(progress)} quote requests are drafted. Read them, then approve.", ref=",".join(p.draft_id for p in progress.values())),),
            next_action="waiting for you to approve the drafted requests",
        )
        return self.store.create(job, self.now())

    def approve_requests(self, job_id: str) -> Job:
        """Approve exactly the drafts the user was shown (each approval is bound to that draft's text hash)."""
        job = self.store.get(job_id)
        if job.status in TERMINAL:  # checked before any draft is approved: an approved draft is sent by the next dispatch, whatever happens to the job after
            raise ValueError(f"this job is {job.status}; start a new one to ask these vendors")
        if job.deadline <= self.now():  # a job waiting for approval has no wake-up, so nothing else would end it
            raise ValueError("the deadline of this job has passed; start a new one with a new deadline")
        for p in job.vendors.values():
            d = self.em.outreach_store.get(job.plan_id, p.draft_id)
            if d is not None and d.status == "prepared":
                try:
                    self.em.approve_outreach(job.plan_id, d.draft_id, d.content_hash)
                except ValueError:
                    continue  # blocked (do not contact), expired or channel not set up: that draft keeps its own status and the next tick tells the user; the others still go
        items = tuple(i.model_copy(update={"resolved": True}) if i.kind == "approve_requests" else i for i in job.items)
        return self._persist(job.model_copy(update={"items": items, "wake_at": self.now(), "status": "active", "next_action": "sending approved requests"}), "user", "requests_approved", self.now())

    def confirm_claim(self, job_id: str, claim_id: str, valid_days: int) -> Job:
        """The user's tap. This is the only place a vendor's words become a recorded quote. The vendor's reply rarely says how long a price
        holds, so the user chooses the validity (1-90 days); it is recorded in the quote's note as the user's choice."""
        if not isinstance(valid_days, int) or isinstance(valid_days, bool) or not 1 <= valid_days <= 90:
            raise ValueError("say how many days (1-90) this price should be treated as valid")
        job = self.store.get(job_id)
        claim = next((c for c in job.claims if c.claim_id == claim_id and c.status == "proposed"), None)
        if claim is None:
            raise ValueError("no such open claim")
        plan = self.em.store.latest(job.plan_id)
        now = self.now()
        self.em.observe_quote(
            claim.vendor_id, VendorCategory(job.category), source_type=ObservationSource.VENDOR_QUOTE, entered_by="user_confirmed", observed_at=now,
            expires_at=(now + timedelta(days=valid_days)).date(), confidence=CONFIRM_CONFIDENCE, evidence_ref=claim.message_id[:200], amount_inr=claim.amount_inr,
            basis=claim.basis, guest_count=plan.request.guest_count if claim.basis == "per_plate" else None,
            note=f"valid {valid_days} days: your choice at confirmation; check taxes and extras in the vendor's words"[:200],
        )
        claims = tuple(
            c.model_copy(update={"status": "confirmed"}) if c.claim_id == claim_id
            else c.model_copy(update={"status": "superseded"}) if (c.vendor_id == claim.vendor_id and c.status == "confirmed") else c
            for c in job.claims
        )
        items = tuple(i.model_copy(update={"resolved": True}) if (i.kind == "confirm_claim" and i.ref == claim_id) else i for i in job.items)
        return self._persist(job.model_copy(update={"claims": claims, "items": items, "wake_at": now}), "user", "claim_confirmed", now, {"claim": claim_id, "message": claim.message_id, "valid_days": valid_days})

    def reject_claim(self, job_id: str, claim_id: str) -> Job:
        job = self.store.get(job_id)
        claims = tuple(c.model_copy(update={"status": "rejected"}) if (c.claim_id == claim_id and c.status == "proposed") else c for c in job.claims)
        items = tuple(i.model_copy(update={"resolved": True}) if (i.kind == "confirm_claim" and i.ref == claim_id) else i for i in job.items)
        return self._persist(job.model_copy(update={"claims": claims, "items": items, "wake_at": self.now()}), "user", "claim_rejected", self.now(), {"claim": claim_id})

    def resolve_item(self, job_id: str, item_id: str, note: str = "") -> Job:
        """The user dealt with it outside the job (answered a vendor question, checked an unknown send)."""
        job = self.store.get(job_id)
        if not any(i.item_id == item_id for i in job.items):
            raise ValueError("no such item")
        items = tuple(i.model_copy(update={"resolved": True}) if i.item_id == item_id else i for i in job.items)
        return self._persist(job.model_copy(update={"items": items, "wake_at": self.now()}), "user", "item_resolved", self.now(), {"item": item_id, "note": note[:200]})

    def approve_nudge(self, job_id: str, item_id: str) -> Job:
        job = self.store.get(job_id)
        item = next((i for i in job.items if i.item_id == item_id and i.kind == "approve_nudge" and not i.resolved), None)
        if item is None:
            raise ValueError("no such open nudge")
        d = self.em.outreach_store.get(job.plan_id, item.ref)
        if d is not None and d.status == "prepared":
            self.em.approve_outreach(job.plan_id, d.draft_id, d.content_hash)
        vendors = dict(job.vendors)
        vendors[item.vendor_id] = vendors[item.vendor_id].model_copy(update={"nudges": vendors[item.vendor_id].nudges + 1, "asked_at": self.now(), "pending_nudge_draft_id": ""})
        items = tuple(i.model_copy(update={"resolved": True}) if i.item_id == item_id else i for i in job.items)
        return self._persist(job.model_copy(update={"items": items, "vendors": vendors, "wake_at": self.now()}), "user", "nudge_approved", self.now(), {"draft": item.ref})

    def _deny_queued(self, job: Job) -> None:
        """A job that ends leaves no message queued behind it: its automatic follow-ups and nudge drafts are denied."""
        requests = {p.draft_id for p in job.vendors.values()}
        for d in self.em.outreach_store.list(job.plan_id):
            if d.draft_id in requests and d.status in ("prepared", "approved"):  # a request that was never sent
                self.em.outreach_store.transition(job.plan_id, d.draft_id, ("prepared", "approved"), status="denied", error="the job ended")
            elif d.kind == "followup" and d.parent_id in requests and d.status in ("prepared", "approved"):
                self.em.outreach_store.transition(job.plan_id, d.draft_id, ("prepared", "approved"), status="denied", error="the job ended")

    def cancel(self, job_id: str) -> Job:
        job = self.store.get(job_id)
        self._deny_queued(job)
        return self._persist(job.model_copy(update={"status": "cancelled", "wake_at": None, "next_action": "cancelled by you"}), "user", "cancelled", self.now())

    # ------------------------------------------------------------ the loop

    def tick(self) -> Dict[str, object]:
        """One bounded pass. Safe to call any number of times, from any process."""
        now = self.now()
        report: Dict[str, object] = {"ran": [], "errors": []}
        try:
            self.em.poll_conversations()  # sends what is approved and within limits, then reads replies; one failing channel is reported, not fatal
        except Exception as exc:  # the loop must survive a bad channel
            report["errors"].append(f"poll: {type(exc).__name__}")
        for job in self.store.due(now, MAX_JOBS_PER_TICK):
            try:
                self._run(job, now)
                report["ran"].append(job.job_id)
            except StaleJob:
                continue  # someone else (the user, another worker) changed it first; it is due again next tick
            except Exception as exc:
                report["errors"].append(f"{job.job_id}: {type(exc).__name__}: {str(exc)[:160]}")
        try:
            self.em.notifier.flush()  # the optional email copy to the owner, if they turned it on; every gate is checked inside
        except Exception as exc:
            report["errors"].append(f"notify: {type(exc).__name__}")
        try:
            self.em.conversations.dispatch()  # what the jobs just queued (automatic nudges) goes out now, within the limits, not a tick later
        except Exception as exc:
            report["errors"].append(f"dispatch: {type(exc).__name__}")
        return report

    def _run(self, job: Job, now: datetime) -> Job:
        plan_id = job.plan_id
        vendors = dict(job.vendors)
        claims: List[QuoteClaim] = list(job.claims)
        items: List[UserItem] = list(job.items)
        seen = set(job.seen_message_ids)
        awaiting = list(job.answered_replies)
        events: List[str] = []

        def has_item(kind: str, ref: str) -> bool:
            return any(i.kind == kind and i.ref == ref for i in items)

        # 1. did the requests actually go out?
        for vid, p in list(vendors.items()):
            if p.state != "drafted":
                continue
            d = self.em.outreach_store.get(plan_id, p.draft_id)
            if d is None:
                continue
            if d.status == "sent" and d.sent_at is not None:
                vendors[vid] = p.model_copy(update={"state": "asked", "asked_at": d.sent_at})
                events.append(f"sent:{vid}")
            elif d.status == "blocked" and not has_item("do_not_contact", p.draft_id):
                vendors[vid] = p.model_copy(update={"state": "unresponsive"})
                items.append(_item("do_not_contact", f"The request to {vid} was not sent: blocked, do not contact. {d.error}".strip(), vid, p.draft_id))
            elif d.status in ("outcome_unknown", "failed", "expired", "denied") and not has_item("check_unknown_send", p.draft_id):
                vendors[vid] = p.model_copy(update={"state": "unresponsive"})
                items.append(_item("check_unknown_send", f"The request to {vid} is '{d.status}'. Check the vendor chat before sending again. {d.error}".strip(), vid, p.draft_id))

        # 2. new replies
        for m in self.em.outreach_store.inbound(plan_id):
            p = next(((v, x) for v, x in vendors.items() if x.draft_id == m.draft_id), None)
            if p is None or m.message_id in seen:
                continue
            vid, prog = p
            seen.add(m.message_id)
            events.append(f"reply:{vid}")
            vendors[vid] = prog.model_copy(update={"state": "unavailable" if m.analysis.says_unavailable else "replied"})
            if m.sender_differs:  # a reply from another address is never turned into a claim; the user reads it first
                items.append(_item("different_sender", f"{vid}: a reply came from {m.sender}, not the address you wrote to. Read it before trusting anything in it.", vid, m.message_id))
                continue
            answered = any(x.action_key == f"reply:{m.message_id}" and x.status not in ("denied", "expired") for x in self.em.outreach_store.list(plan_id))  # the Mandate already asked its question
            if answered:
                awaiting.append(m.message_id)  # watched below: if that question never goes out, the user is told
            new_claims, problem = extract_claims(m)
            for c in new_claims:
                for i, old in enumerate(claims):
                    if old.vendor_id == vid and old.status == "proposed":
                        claims[i] = old.model_copy(update={"status": "superseded"})
                        items = [x.model_copy(update={"resolved": True}) if (x.kind == "confirm_claim" and x.ref == old.claim_id) else x for x in items]
                claims.append(c)
                unit = "per plate" if c.basis == "per_plate" else "for the event"
                items.append(_item("confirm_claim", f"{vid} wrote: \"{c.snippet}\" -> INR {c.amount_inr:,} {unit}. Confirm to record it as a quote (check taxes and extras in their words).", vid, c.claim_id))
            if problem:
                items.append(_item("unclear_price", f"{vid}: {problem}", vid, m.message_id))
            if m.analysis.questions:
                items.append(_item("vendor_question", f"{vid} asks: " + " / ".join(m.analysis.questions), vid, m.message_id))
            if m.analysis.says_unavailable:
                items.append(_item("vendor_unavailable", f"{vid} says it is not available.", vid, m.message_id))
            if not (new_claims or problem or m.analysis.questions or m.analysis.says_unavailable or answered):
                items.append(_item("unclear_price", f"{vid} replied without a price. Please read the reply.", vid, m.message_id))

        # 2b. an automatic answer that was queued must actually go out; if it was denied, expired or failed, the reply is the user's again
        if awaiting:
            by_id = {x.message_id: x for x in self.em.outreach_store.inbound(plan_id)}
            drafts = self.em.outreach_store.list(plan_id)
            for mid in list(awaiting):
                d = next((x for x in drafts if x.action_key == f"reply:{mid}"), None)
                if d is not None and d.status in ("prepared", "approved", "sending"):
                    continue  # still on its way
                awaiting.remove(mid)
                if d is None or d.status in ("denied", "expired", "failed", "outcome_unknown", "blocked"):
                    owner = by_id.get(mid)
                    vid = next((v for v, x in vendors.items() if owner and x.draft_id == owner.draft_id), "")
                    why = "no automatic answer was sent" if d is None else f"the automatic answer to them is {d.status.replace('_', ' ')}"
                    items.append(_item("unclear_price", f"{vid} replied without a price and {why}. Please read the reply.", vid, mid))

        # 3. silence: an automatic nudge only through the standing permission (ADR-043), otherwise a draft for the user to approve
        window = timedelta(hours=job.reply_window_hours)
        for vid, p in list(vendors.items()):
            if p.state != "asked" or p.asked_at is None or now < p.asked_at + window:
                continue
            if p.nudges >= job.max_nudges_per_vendor:
                vendors[vid] = p.model_copy(update={"state": "unresponsive"})
                events.append(f"unresponsive:{vid}")
            elif not p.pending_nudge_draft_id:
                sent_draft, why = self.em.conversations.queue_automatic(plan_id, p.draft_id, NUDGE_TEXT, f"nudge:{job.job_id}:{vid}:{p.nudges + 1}")
                if sent_draft is not None:
                    vendors[vid] = p.model_copy(update={"nudges": p.nudges + 1, "asked_at": now})
                    events.append(f"auto_nudge:{vid}:{p.nudges + 1}")
                else:
                    follow = self.em.outreach_store.followup(plan_id, p.draft_id, NUDGE_TEXT, job.sender_name, action_key=f"nudge-draft:{job.job_id}:{vid}:{p.nudges + 1}")
                    vendors[vid] = p.model_copy(update={"pending_nudge_draft_id": follow.draft_id})
                    items.append(_item("approve_nudge", f"{vid} has not replied in {job.reply_window_hours}h. A short follow-up is drafted. Approve it? (not automatic: {why})", vid, follow.draft_id))

        # 4. finished, or impossible?
        best: Dict[str, QuoteClaim] = {c.vendor_id: c for c in claims if c.status == "confirmed"}
        within = [c for c in best.values() if job.max_amount_inr is None or self._total_for(job, c) <= job.max_amount_inr]
        status, next_action, wake = job.status, job.next_action, None
        if len(within) >= job.min_quotes:
            status, next_action = "done", f"{len(within)} confirmed quotes within your cap, each tied to a vendor message"
            items = [x.model_copy(update={"resolved": True}) if x.kind in ("approve_nudge", "vendor_unavailable") else x for x in items]
        elif now > job.deadline:
            status, next_action = "expired", f"deadline passed with {len(within)} of {job.min_quotes} confirmed quotes"
        else:
            reachable = len(within) + sum(1 for v, p in vendors.items() if v not in best and p.state in ("drafted", "asked", "replied"))
            if reachable < job.min_quotes and not any(i.kind == "no_quotes_possible" and not i.resolved for i in items):
                items.append(_item("no_quotes_possible", f"Only {reachable} vendor(s) can still give a quote but {job.min_quotes} are needed. Add vendors or lower the number."))
            open_items = [i for i in items if not i.resolved]
            if open_items:
                wake = now + timedelta(minutes=job.poll_minutes)  # still watching while the user decides: a newer reply can supersede an open claim
            if any(i.kind == "check_unknown_send" for i in open_items):
                status, next_action = "blocked", "a send has an unknown outcome; check the vendor chat"
            elif open_items:
                status, next_action = "waiting_user", f"{len(open_items)} item(s) need you"
            else:
                waiting = [p for p in vendors.values() if p.state in ("drafted", "asked")]
                if waiting:
                    due = [p.asked_at + window for p in waiting if p.asked_at is not None]
                    wake = min([now + timedelta(minutes=job.poll_minutes)] + [t for t in due if t > now])
                    status, next_action = "waiting_vendor", f"waiting for {len(waiting)} vendor(s); next check {wake.isoformat(timespec='minutes')}"
                else:
                    status, next_action, wake = "active", "re-checking", now + timedelta(minutes=job.poll_minutes)
        new = job.model_copy(update={"vendors": vendors, "claims": tuple(claims), "items": tuple(items), "seen_message_ids": tuple(sorted(seen)), "answered_replies": tuple(awaiting), "status": status, "next_action": next_action, "wake_at": wake})
        saved = self._persist(new, "worker", "tick", now, {"events": events})
        if saved.status in ("done", "expired"):
            self._deny_queued(saved)
        return saved

    def _total_for(self, job: Job, c: QuoteClaim) -> int:
        if c.basis == "per_plate":
            return c.amount_inr * self.em.store.latest(job.plan_id).request.guest_count
        return c.amount_inr


def digest(job: Job) -> str:
    """A short plain-text update for the user. Pure text: sending it anywhere is a separate, explicit step."""
    lines = [f"{job.goal} - {job.status.replace('_', ' ')}", job.next_action]
    for n, i in enumerate(job.open_items(), 1):
        lines.append(f"{n}. {i.text}")
    ok = job.confirmed()
    if ok:
        lines.append("Confirmed: " + "; ".join(f"{c.vendor_id} INR {c.amount_inr:,}{' per plate' if c.basis == 'per_plate' else ''}" for c in ok))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    from eventmate.service import EventMate

    ap = argparse.ArgumentParser(prog="eventmate.followthrough.worker")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--every", type=int, default=60, help="seconds between ticks")
    ap.add_argument("--plan-dir")
    a = ap.parse_args(argv)
    em = EventMate(plan_dir=Path(a.plan_dir) if a.plan_dir else None)
    stop = os.environ.get("EVENTMATE_WORKER_STOP", "")
    while True:
        if stop and Path(stop).exists():
            print("stop file found; exiting")
            return 0
        print(em.jobs.tick())
        if a.once:
            return 0
        time.sleep(max(5, a.every))


if __name__ == "__main__":
    raise SystemExit(main())
