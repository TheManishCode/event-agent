"""EventMate facade: wires every agent and exposes the whole workflow.

    em = EventMate()
    outcome = em.plan("Plan a 120-person engagement party in Pune ...")
    em.confirm(plan_id, VendorCategory.VENUE)        # the user confirmed a booking
    em.replan(plan_id, Disruption(kind=..., ...))    # new immutable version + diff
    em.explain(plan_id)                              # readable text with reasons
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional

from eventmate.agents.budget import BudgetAgent, BudgetPolicy
from eventmate.agents.explainer import ExplainerAgent, Explanation
from eventmate.agents.guest import GuestAgent, GuestRepository
from eventmate.agents.manager import ManagerAgent
from eventmate.agents.planner import PlannerAgent
from eventmate.agents.replanner import ReplannerAgent, diff_plans
from eventmate.agents.scheduler import SchedulingAgent
from eventmate.agents.tradeoff import TradeoffAgent
from eventmate.agents.validator import ValidatorAgent
from eventmate.agents.vendor import VendorAgent, VendorRepository
from eventmate.integrations.leads import LeadStore
from eventmate.planning import community as community_mod
from eventmate.planning.community import AccessibilityStore, ChecklistStore
from eventmate.messaging.digests import DigestStore
from eventmate.messaging.notify import OwnerNotifier
from eventmate.planning.ideas import IdeaService
from eventmate.messaging.channels import Channel, channels_from_env
from eventmate.messaging.conversations import ConversationService
from eventmate.messaging.outreach import OutreachStore
from eventmate.followthrough.worker import JobWorker
from eventmate.followthrough.jobs import JobStore
from eventmate.sourcing.contacts import find_contacts
from eventmate.sourcing.externals import ExternalStore, ResolvedVendor, clean_email, slug
from eventmate.sourcing.search import VendorSearch, clean_name
from eventmate.sourcing.suppression import SuppressionStore, UnmatchedStore, domain_note
from eventmate.sourcing.shortlist import ShortlistStore
from eventmate.pricing.ledger import Ledger
from eventmate.pricing.observations import ObservationStore
from eventmate.pricing.vendorscore import score_category
from eventmate.planning import intake
from eventmate.llm import LLMClient, client_from_env
from eventmate.logs import RunLogs
from eventmate.planning.rules import Advisory, advisories_for
from eventmate.plan_store import PlanStore, draft_from_plan
from eventmate.schemas import ContactFindResult, ExternalBusiness, SearchOutcome, ShortlistItem, VendorContact, VendorRef, WeatherAdvice, BookingStatus, DecisionLogEntry, SelectionQuoteStatus, LedgerEntry, LedgerSummary, Disruption, DisruptionKind, PlanDiff, PlanOutcome, PlanVersion, ReplanOutcome, TradeoffOption, TradeoffReport, ValidationReport, VendorCategory
from eventmate.symbolic import SymbolicChecker
from eventmate.integrations.deals import DealAgent
from eventmate.integrations.discovery import OverpassSource
from eventmate.integrations.web_access import SafeFetcher
from eventmate.integrations.websearch import default_provider
from eventmate.env import load_env
from eventmate.integrations.places import PlacesSource
from eventmate.integrations.weather import OpenWeatherSource
from eventmate.integrations.weather_advice import advise
from eventmate.integrations.travel import FlightProvider, FliProvider, check_search, search_multi_city
from eventmate.integrations.tripsearch import search_trip


class _NoClients:
    clients: list = []


class EventMate:
    def __init__(
        self,
        log_dir: Optional[Path] = None,
        plan_dir: Optional[Path] = None,
        run_id: Optional[str] = None,
        repo: Optional[VendorRepository] = None,
        guest_repo: Optional[GuestRepository] = None,
        symbolic: Optional[SymbolicChecker] = None,
        llm: Optional[LLMClient] = None,
        require_symbolic: bool = True,
        store_llm_text: bool = False,
        flights: Optional[FlightProvider] = None,
        discovery: Optional[OverpassSource] = None,
        weather: Optional[OpenWeatherSource] = None,
        places: Optional[PlacesSource] = None,
        channels: Optional[Dict[str, Channel]] = None,
    ):
        self.env = load_env()  # secrets from .env (names only are kept); values already in the environment win
        self._today = date.today()
        self.logs = RunLogs.create(log_dir, run_id, store_llm_text)
        self.repo = repo or VendorRepository()
        self.store = PlanStore(plan_dir)
        self.ledger = Ledger(self.store.root)
        self.lead_store = LeadStore(self.store.root)
        self.flights = flights or FliProvider()
        self.discovery = discovery or OverpassSource()
        self.weather = weather or OpenWeatherSource()
        self.places = places or PlacesSource()
        self.router_clients = lambda: (client_from_env(tier="light") or _NoClients()).clients  # light tier: OpenRouter free first, NVIDIA behind
        self.deals_factory = lambda on_call: DealAgent(default_provider(), SafeFetcher(), on_call=on_call)
        self.observations = ObservationStore(self.store.root, self.repo)
        self.repo.quotes = self.observations
        self.externals = ExternalStore(self.store.root)
        self.shortlist = ShortlistStore(self.store.root)
        self.observations.external_lookup = self.externals.get
        self.suppressions = SuppressionStore(self.store.root)
        self.unmatched = UnmatchedStore(self.store.root)
        self.vendor_search = VendorSearch(self.externals, self.shortlist, self.discovery, self.places)
        self.contact_fetcher: Callable[[], object] = SafeFetcher  # tests replace this; the real one has the SSRF, robots and size rules
        self.access_store = AccessibilityStore(self.store.root)
        self.checklist_store = ChecklistStore(self.store.root)
        self.repo.access = self.access_store
        self.outreach_store = OutreachStore(self.store.root, self.observations.checklists)
        self._jobs: Optional[JobWorker] = None
        self.jobs_clock = None  # tests inject a clock here before the worker is first used
        self._job_store: Optional[JobStore] = None
        self.digest_store = DigestStore(self.store.root)
        self.conversations = ConversationService(self.outreach_store, channels or channels_from_env(os.environ), self.store.latest)
        self.conversations.guards.append(self._job_guard)
        self.conversations.suppressions, self.conversations.unmatched = self.suppressions, self.unmatched
        self.notifier = OwnerNotifier(
            self.store.root, self.digest_store, lambda: self.conversations.channels["email"], self._jobs_of, self._vendor_names, self._vendor_addresses,
            self._own_addresses,
            lambda: self.outreach_store.killed,
        )
        decisions = self.logs.decisions
        scheduler = SchedulingAgent(decisions)
        budget_policy = BudgetPolicy()
        budget = BudgetAgent(decisions, budget_policy)
        vendor = VendorAgent(self.repo, decisions)
        guest = GuestAgent(guest_repo or GuestRepository(), decisions)
        planner = PlannerAgent(self.logs, scheduler, symbolic or SymbolicChecker())
        self.validator = ValidatorAgent(self.repo, self.logs, scheduler.policy, require_symbolic, budget_policy)
        self.llm = llm
        self.ideas = IdeaService(self.store.root, self.logs, self.repo, llm)
        self.explainer = ExplainerAgent(self.logs, llm)
        self.manager = ManagerAgent(self.repo, decisions, budget, vendor, guest, planner, self.validator, self.explainer, self.store)
        self.tradeoff = TradeoffAgent(self.repo, decisions, self._dry_run_ok, budget_policy)
        self._guest_list_id: Optional[str] = None
        self.replanner = ReplannerAgent(self.logs, self.repo, budget, vendor, guest, planner, self.validator, self.store)

    def _clock(self, today: Optional[date]) -> None:
        """Quote expiry is judged against the caller's 'today' (tests and the web app inject one)."""
        if today is not None:
            self.repo.clock = lambda: today

    def plan(self, text: str, today: Optional[date] = None, guest_list_id: Optional[str] = None) -> PlanOutcome:
        self._clock(today)
        return self.manager.run(text, today=today, guest_list_id=guest_list_id)

    def intake_turn(self, messages, prior: Optional[intake.IntakeDraft] = None, today: Optional[date] = None) -> intake.IntakeTurn:
        """Propose a draft from the organiser's words. Nothing is planned or stored until ``confirm_intake``."""
        return intake.propose(messages, prior, today or date.today(), self.repo.cities(), self.logs, self.llm)

    def confirm_intake(self, draft: intake.IntakeDraft, raw_text: str, today: Optional[date] = None, guest_list_id: Optional[str] = None, source: str = "rule:parser") -> PlanOutcome:
        """The user confirmed the review card: run the full pipeline on exactly what they approved."""
        today = today or date.today()
        self._clock(today)
        request = intake.to_request(draft, raw_text, today, source)
        return self.manager.run_request(request, today=today, guest_list_id=guest_list_id, change_reason="initial plan from the intake conversation, confirmed by the user")

    def _dry_run_ok(self, request):
        """(a full plan was built, what that plan would leave undecided). Used by the trade-off negotiator."""
        out = self.manager.run_request(request, today=self._today, guest_list_id=self._guest_list_id, persist=False)
        if out.status != "planned":
            return False, ()
        return True, tuple(u.label or u.name for u in out.plan.validation.blocking_unverified())

    def verification(self, plan_id: str, version: Optional[int] = None) -> dict:
        """Where a plan stands right now: "verified" or "needs_verification". Community plans are re-checked against today's data, so a venue's answer recorded after the plan was made counts at once."""
        plan = self.store.load(plan_id, version)
        report = self.revalidate(plan_id, version) if plan.request.event_type.value == "community" else plan.validation
        blocking = report.blocking_unverified()
        return {
            "state": "verified" if report.fully_verified else ("needs_verification" if report.passed else "failed"),
            "label": report.verification_label(),
            "items": [{"name": u.name, "label": u.label or u.name, "detail": u.detail, "repair": u.repair} for u in blocking],
            "other_unverified": [{"name": u.name, "label": u.label or u.name, "detail": u.detail} for u in report.unverified if not u.blocks_verified],
        }

    def tradeoffs(self, text: str, today: Optional[date] = None, guest_list_id: Optional[str] = None) -> TradeoffReport:
        """Ranked, verified ways to make a failing request plannable. Nothing is stored or applied."""
        self._today, self._guest_list_id = today or date.today(), guest_list_id
        self._clock(self._today)
        return self.tradeoff.propose(self.manager.parse(text, self._today), self._today)

    def apply_tradeoff(self, option: TradeoffOption, today: Optional[date] = None, guest_list_id: Optional[str] = None) -> PlanOutcome:
        """The user chose this option: run it through the full pipeline and store the resulting plan."""
        self._clock(today)
        return self.manager.run_request(
            option.request, today=today, guest_list_id=guest_list_id, change_reason=f"trade-off chosen by the user: {option.title}"[:500]
        )

    def advisories(self, plan_id: str) -> List[Advisory]:
        """Advisory checklists for the plan's dietary requirements. Never constraints."""
        return advisories_for(self.store.latest(plan_id).request)

    def ledger_add(self, plan_id: str, kind: str, category: VendorCategory, amount: int, note: str = "") -> LedgerEntry:
        """Record money the user reports (quote, payment, refund) against the latest plan. Nothing is paid by EventMate."""
        return self.ledger.add(self.store.latest(plan_id), kind, category, amount, note)

    def ledger_entries(self, plan_id: str):
        self.store.latest(plan_id)  # unknown / hostile ids fail here
        return self.ledger.entries(plan_id)

    def ledger_summary(self, plan_id: str) -> LedgerSummary:
        return self.ledger.summarize(self.store.latest(plan_id))

    def observe_quote(self, vendor_id: str, category: VendorCategory, **fields):
        """Record a vendor quote you were given (ADR-025). Later plans and validation price from it (ADR-026)."""
        return self.observations.add_quote(vendor_id, category, **fields)

    def vendor_scores(self, category: VendorCategory, today: Optional[date] = None):
        """Transparent score of every vendor with a current quote in a category (ADR-040). Reads only; changes no price."""
        return score_category(self.observations, category, today or date.today())

    def quote_status(self, plan_id: str, today: Optional[date] = None) -> List[SelectionQuoteStatus]:
        """Quote report, staleness and confirmation source for each selection of the latest plan (ADR-006, ADR-027)."""
        today = today or date.today()
        self._clock(today)
        plan = self.store.latest(plan_id)
        rows = []
        for s in plan.selections:
            rec = self.repo.get(s.category, s.vendor_id)
            current = self.repo.price(rec, plan.request.guest_count) if rec else s.cost
            confirmed = s.booking_status == BookingStatus.CONFIRMED
            by = None
            if confirmed:
                by = "vendor" if self.observations.vendor_confirmed(s.vendor_id) else "you"
            rows.append(SelectionQuoteStatus(
                category=s.category, vendor_id=s.vendor_id, vendor_name=s.name, booking_status=s.booking_status,
                plan_cost=s.cost, current_price=current, price_stale=current != s.cost, confirmed_by=by,
                report=self.observations.report(s.vendor_id, today),
            ))
        return rows

    def discover_venues(self, city: str, kind: str = "venue", limit: int = 20):
        """Places mapped on OpenStreetMap: candidates to ask about, never vendor-database facts (ADR-028)."""
        return self.discovery.venues(city, kind, limit)

    def weather_forecast(self, city: str, day: date):
        """Forecast from OpenWeather. Past its horizon the answer says so; nothing is estimated (ADR-029)."""
        return self.weather.forecast(city, day)

    def weather_advice(self, plan_id: str):
        """Forecast for the event day plus code-built advice for the chosen venue's setting (ADR-039). Changes nothing."""
        plan = self.store.latest(plan_id)
        venue = plan.selection_for(VendorCategory.VENUE)
        rec = self.repo.get(VendorCategory.VENUE, venue.vendor_id) if venue else None
        setting = getattr(rec, "setting", None)
        forecast = self.weather.forecast(plan.request.city, plan.request.event_date)
        return WeatherAdvice(plan_id=plan_id, venue_setting=setting, items=tuple(advise(forecast, setting)), forecast=forecast)

    def plan_weather(self, plan_id: str):
        plan = self.store.latest(plan_id)
        return self.weather.forecast(plan.request.city, plan.request.event_date)

    def save_lead(self, plan_id: str, **fields):
        """Bookmark an advertised price against a plan. It is not a quote and changes no price (ADR-038)."""
        self.store.latest(plan_id)  # unknown or hostile ids fail here
        return self.lead_store.add(plan_id, **fields)

    def leads(self, plan_id: str):
        self.store.latest(plan_id)
        return self.lead_store.list(plan_id)

    @property
    def jobs(self) -> JobWorker:
        """The follow-through worker (ADR-044). The job database is created on first use."""
        if self._jobs is None:
            self._jobs = JobWorker(self, JobStore(self.store.root), self.jobs_clock) if self.jobs_clock else JobWorker(self, JobStore(self.store.root))
        return self._jobs

    def _job_guard(self, draft) -> Optional[str]:
        """Dispatch-time veto, independent of the standing permission: an automatic follow-up that belongs to a finished or cancelled job never goes out."""
        if draft.kind != "followup" or not (self.store.root / "jobs.db").exists():
            return None
        if self._job_store is None:
            self._job_store = JobStore(self.store.root)
        for job in self._job_store.list(draft.plan_id):
            if any(p.draft_id == draft.parent_id for p in job.vendors.values()):
                return "the job this belongs to has ended" if job.status in ("done", "expired", "cancelled") else None
        return None

    def _jobs_of(self, plan_id: str):
        return self.jobs.store.list(plan_id) if (self.store.root / "jobs.db").exists() else []

    def _own_addresses(self) -> set:
        """The mailboxes EventMate itself sends from."""
        return {a for a in (getattr(getattr(ch, "config", None), "from_addr", "") for ch in self.conversations.channels.values()) if a}

    def _vendor_names(self):
        names = {p.id: p.name for c in VendorCategory for p in self.repo.providers(c)}
        for b in self.externals.all():
            names[b.vendor_id] = f"{b.name} (TEST VENDOR, address owned by {b.owner_confirmed_by})" if b.is_test else b.name  # a test vendor is labelled wherever it is named
        return names

    def _vendor_addresses(self) -> set:
        """Every email address EventMate has written to or saved for a vendor: none of these may ever be the owner's notification address."""
        out = {c.email for c in self.outreach_store.contacts().values() if c.email}
        for pid in self.outreach_store.plan_ids():
            out |= {d.recipient for d in self.outreach_store.list(pid) if d.channel == "email"}
        return out

    def background_round(self):
        """One round of the agent's background work: the job worker if any job exists (it polls conversations itself), otherwise just the conversations."""
        if (self.store.root / "jobs.db").exists():
            return self.jobs.tick()
        return self.poll_conversations()

    def draft_outreach(self, plan_id: str, category: VendorCategory, channel: str, recipient: str, sender_name: str, vendor_id: Optional[str] = None):
        """Write a quote request for a vendor in the plan, or for another vendor of the category when ``vendor_id`` is given (to compare quotes).
        It is a proposal: nothing leaves until it is approved (ADR-041, ADR-042)."""
        vendor = None
        if vendor_id is not None:
            rv = self.resolve_vendor(vendor_id, category)
            if rv.ref.kind == "external":
                self._require_confirmed_address(rv, channel, recipient)
            vendor = (rv.ref.id, rv.name, rv.origin)
        hit = self.suppressions.active_for(channel, recipient) if channel in ("email", "whatsapp") else None
        if hit is not None:
            raise ValueError("Blocked: do not contact. That address is on the do-not-contact list (" + hit.reason.replace("_", " ") + "); only you can lift it.")
        return self.outreach_store.draft(self.store.latest(plan_id), category, channel, recipient, sender_name, vendor)

    def outreach(self, plan_id: str):
        self.store.latest(plan_id)
        return self.outreach_store.list(plan_id)

    def mark_outreach_sent(self, plan_id: str, draft_id: str):
        """The manual path: the user sent it themselves from the link."""
        self.store.latest(plan_id)
        return self.outreach_store.mark_sent(plan_id, draft_id)

    def approve_outreach(self, plan_id: str, draft_id: str, content_hash: str):
        """Approve exactly this text; the agent sends it through the configured channel (ADR-042)."""
        self.store.latest(plan_id)
        return self.conversations.approve(plan_id, draft_id, content_hash)

    def deny_outreach(self, plan_id: str, draft_id: str):
        self.store.latest(plan_id)
        return self.conversations.deny(plan_id, draft_id)

    def followup_outreach(self, plan_id: str, request_id: str, text: str, sender_name: str):
        self.store.latest(plan_id)
        return self.outreach_store.followup(plan_id, request_id, text, sender_name)

    def request_all_quotes(self, plan_id: str, sender_name: str):
        return self.conversations.request_all(plan_id, sender_name)

    def inbox(self, plan_id: str):
        return self.conversations.inbox(plan_id)

    def poll_conversations(self):
        """Send what is approved and read replies. Called by the background loop and by the CLI."""
        return self.conversations.poll()

    def grant_autoreply(self, plan_id: str, sender_name: str, hours: int = 72, max_per_thread: int = 3, max_total: int = 10):
        """Standing permission for fixed follow-up questions, bounded and revocable (ADR-043)."""
        return self.conversations.grant_autoreply(plan_id, sender_name, hours, max_per_thread, max_total)

    def revoke_autoreply(self, plan_id: str):
        return self.conversations.revoke_autoreply(plan_id)

    def autoreply_status(self, plan_id: str):
        self.store.latest(plan_id)
        return self.conversations.autoreply_status(plan_id)

    def kill_all_autoreplies(self):
        self.conversations.kill_all()

    def resume_autoreplies(self):
        self.conversations.resume_all()

    def reply_suggestions(self, plan_id: str, request_id: str):
        self.store.latest(plan_id)
        return self.conversations.suggestions(plan_id, request_id)

    def mark_reply_read(self, plan_id: str, message_id: str):
        self.store.latest(plan_id)
        return self.outreach_store.mark_read(plan_id, message_id)

    def resolve_vendor(self, vendor_id: str, category: Optional[VendorCategory] = None) -> ResolvedVendor:
        """The one place a vendor id becomes a vendor. A seed id is looked up in the vendor database, an ``ext_`` id only in the external store; nothing crosses over."""
        ref = VendorRef.of(vendor_id)  # a malformed id fails here
        if ref.kind == "external":
            biz = self.externals.get(vendor_id)
            if biz is None:
                raise ValueError(f"unknown {category.value if category else 'vendor'} {vendor_id!r}")
            if category is not None and biz.category != category:
                raise ValueError(f"{biz.name} is a {biz.category.value}, not a {category.value}")
            return ResolvedVendor(ref, biz.name, biz.category, biz.origin, biz.source_url or biz.website)
        for c in ([category] if category else list(VendorCategory)):
            rec = self.repo.get(c, vendor_id)
            if rec is not None:
                return ResolvedVendor(ref, rec.name, c, "seed", None)
        if category is not None and any(self.repo.get(c, vendor_id) for c in VendorCategory):
            raise ValueError(f"{vendor_id} is not a {category.value}")
        raise ValueError(f"unknown {category.value if category else 'vendor'} {vendor_id!r}")

    def _require_confirmed_address(self, rv: ResolvedVendor, channel: str, recipient: str) -> None:
        """A discovered business is written to only at an address the user confirmed for it. A typed or extracted address is not enough."""
        c = self.outreach_store.contacts().get(rv.ref.id)
        if c is None or not c.confirmed_by:
            raise ValueError(f"{rv.name} has no confirmed contact address yet: confirm one that was found on its own page or listing")
        have = c.email if channel == "email" else c.whatsapp
        want = recipient.strip().casefold() if channel == "email" else "".join(ch for ch in recipient if ch.isdigit())
        if have is None or have.casefold() != want:
            raise ValueError(f"that recipient is not the confirmed address for {rv.name}")

    def set_vendor_contact(self, vendor_id: str, email: Optional[str], whatsapp: Optional[str], preferred: str):
        """The user types a contact for a seed vendor. A discovered business gets its contact only by confirming a found address."""
        if VendorRef.of(vendor_id).kind == "external":
            raise ValueError("a discovered business's contact is saved only by confirming an address found on its own page or listing, or as a test vendor")
        if not any(self.repo.get(c, vendor_id) for c in VendorCategory):
            raise ValueError(f"unknown vendor {vendor_id!r}")
        return self.outreach_store.set_contact(vendor_id, email, whatsapp, preferred)

    def add_discovered(self, external_id: str, name: str, city: str, category: VendorCategory, source: str, source_url: Optional[str] = None,
                       website: Optional[str] = None, rating: Optional[float] = None) -> ExternalBusiness:
        """Record a business that discovery found. It is not a vendor record and is never promoted to one (ADR-028, ADR-049)."""
        if source not in ("osm", "gplaces"):
            raise ValueError("a discovered business comes from OpenStreetMap or Google Places")
        return self.externals.add(external_id, name, city, category, source, source_url, website, rating)

    def add_test_vendor(self, name: str, city: str, category: VendorCategory, email: str, owns_address: bool, confirmed_by: str, plan_id: Optional[str] = None):
        """A vendor for testing whose address is the user's own. Needs the user's explicit statement that they own it, and is labelled TEST VENDOR everywhere."""
        if owns_address is not True or not (confirmed_by or "").strip():
            raise ValueError("a test vendor needs your confirmation that you own the address")
        email = clean_email(email)
        name = clean_name(name)
        if not name:
            raise ValueError("give the test vendor a name")
        biz = self.externals.add(f"test:{slug(name)}-{hashlib.sha256(email.casefold().encode()).hexdigest()[:8]}",
                                 name, city, category, "test", owner_confirmed_by=confirmed_by.strip())
        now = self.outreach_store.now()
        contact = self.outreach_store.set_contact_record(VendorContact(
            vendor_id=biz.vendor_id, email=email, preferred="email", updated_at=now, origin="test", confirmed_by=confirmed_by.strip(), confirmed_at=now))
        if plan_id is not None:
            self.store.latest(plan_id)
            self.shortlist.add_entry(plan_id, category, biz.vendor_id)
        return biz, contact

    def vendor_directory(self) -> dict:
        """Every vendor id the app can show, with its plain name and where it came from (seed, discovered, test). The UI never shows a raw id."""
        d = {p.id: {"name": p.name, "origin": "seed", "label": None} for c in VendorCategory for p in self.repo.providers(c)}
        for b in self.externals.all():
            d[b.vendor_id] = {"name": b.name, "origin": b.origin, "label": f"TEST VENDOR (address owned by {b.owner_confirmed_by})" if b.is_test else None}
        return d

    def vendor_labels(self) -> dict:
        """Labels the UI must show next to a business: a test vendor says so and whose address it is. Seed vendors and discovered businesses have none."""
        return {b.vendor_id: f"TEST VENDOR (address owned by {b.owner_confirmed_by})" for b in self.externals.all() if b.is_test}

    def search_vendors(self, plan_id: str, category: VendorCategory, source: Optional[str] = None, limit: int = 10) -> SearchOutcome:
        """Find businesses for a plan's city and category and shortlist them. Not vendor records, and nothing is contacted."""
        return self.vendor_search.search(self.store.latest(plan_id), category, source, limit)

    def shortlist_view(self, plan_id: str, category: Optional[VendorCategory] = None) -> List[ShortlistItem]:
        self.store.latest(plan_id)
        contacts = self.outreach_store.contacts()
        out = []
        for e in self.shortlist.entries(plan_id, category):
            biz = self.externals.get(e.vendor_id)
            if biz is None:
                continue
            c = contacts.get(e.vendor_id)
            confirmed = c if c is not None and c.confirmed_by else None
            hit = self.suppressions.active_for("email", confirmed.email) if confirmed and confirmed.email else None
            out.append(ShortlistItem(
                category=e.category, business=biz, candidates=tuple(self.shortlist.candidates(e.vendor_id)), contact=confirmed,
                blocked=None if hit is None else ("do not contact: " + hit.reason.replace("_", " ")),
            ))
        return out

    def _external(self, vendor_id: str) -> ExternalBusiness:
        biz = self.externals.get(vendor_id) if VendorRef.of(vendor_id).kind == "external" else None
        if biz is None:
            raise ValueError(f"unknown discovered business {vendor_id!r}")
        return biz

    def set_business_website(self, vendor_id: str, url: str) -> ExternalBusiness:
        """The business's own website, typed by the user when its listing has none. Its pages are the only place contacts are read from."""
        biz = self._external(vendor_id)
        if biz.is_test:
            raise ValueError("a test vendor has no website to read")
        return self.externals.set_website(vendor_id, (url or "").strip())

    def find_business_contacts(self, vendor_id: str) -> ContactFindResult:
        """Read the business's own website (and one contact page on the same domain) for public email addresses. Found addresses are candidates only."""
        biz = self._external(vendor_id)
        if biz.is_test:
            raise ValueError("a test vendor's address is already confirmed by its owner; there is no website to read")
        if not biz.website:
            raise ValueError(f"{biz.name} has no website: type its website first (its listing did not publish one)")
        started = time.perf_counter()
        res = find_contacts(self.contact_fetcher(), biz.website)
        added = self.shortlist.add_candidates(vendor_id, res.found)
        self.logs.tool_calls.log(
            request_id="sourcing", tool="contacts.find", args={"vendor_id": vendor_id, "pages": len(res.pages)}, ok=bool(res.pages),
            result=f"{len(res.found)} found, {len(added)} new", duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return ContactFindResult(vendor_id=vendor_id, added=tuple(added), pages=tuple(res.pages), notes=tuple(res.notes))

    def reject_contact(self, vendor_id: str, candidate_id: str):
        """The user says this address is not the business's. It will not be offered again."""
        self._external(vendor_id)
        cand = self.shortlist.get_candidate(candidate_id)
        if cand is None or cand.vendor_id != vendor_id:
            raise ValueError("unknown business or contact candidate")
        if cand.status != "found":
            raise ValueError(f"that address is already {cand.status}")
        return self.shortlist.update(candidate_id, status="rejected")

    def confirm_contact(self, vendor_id: str, candidate_id: str, by: str, edited_value: Optional[str] = None) -> VendorContact:
        """The user confirms an address that was found for a discovered business, optionally correcting it. Only this creates its contact."""
        biz = self.externals.get(vendor_id)
        cand = self.shortlist.get_candidate(candidate_id)
        if biz is None or cand is None or cand.vendor_id != vendor_id:
            raise ValueError("unknown business or contact candidate")
        if cand.status != "found":
            raise ValueError(f"that address is already {cand.status}")
        if not (by or "").strip():
            raise ValueError("say who confirmed it (a name)")
        value = clean_email(edited_value) if edited_value else cand.value
        edited = value.casefold() != cand.value.casefold()
        now = self.outreach_store.now()
        contact = self.outreach_store.set_contact_record(VendorContact(
            vendor_id=vendor_id, email=value, preferred="email", updated_at=now, origin="discovered", source_url=cand.source_url,
            confirmed_by=by.strip(), confirmed_at=now, edited_by_user=edited))
        self.shortlist.update(candidate_id, status="confirmed", confirmed_by=by.strip(), confirmed_at=now, confirmed_value=value if edited else None)
        self.suppressions.lift_bad_address("email", value, by.strip(), "address re-confirmed by the user")  # a bad address is cleared by editing or re-confirming; a stop request never is
        return contact

    # ------------------------------------------------------------ do not contact

    def is_suppressed(self, channel: str, value: str):
        return self.suppressions.active_for(channel, value)

    def suppress_address(self, channel: str, value: str, by: str, note: str = ""):
        """The user marks an address do-not-contact. It stops every unsent draft to it, on every plan."""
        if not (by or "").strip():
            raise ValueError("say who is adding it (a name)")
        s = self.suppressions.add(channel, value, "user", (note or "").strip(), by.strip())
        self.conversations.apply_block(channel, value)
        return s

    def lift_suppression(self, suppression_id: str, by: str, reason: str):
        """Only the user lifts one, with a recorded reason. Drafts that were blocked stay blocked: write a new one."""
        return self.suppressions.lift(suppression_id, by, reason)

    def suppression_list(self):
        return [(s, domain_note(s.channel, s.value)) for s in self.suppressions.all()]

    def unmatched_messages(self, include_reviewed: bool = False):
        return [m for m in self.unmatched.all() if include_reviewed or not m.reviewed]

    def dismiss_unmatched(self, message_id: str):
        return self.unmatched.dismiss(message_id)

    def suppress_unmatched(self, message_id: str, by: str):
        """The user reviewed an unmatched message and decided to stop contacting that sender. Their decision, not the message's."""
        m = self.unmatched.get(message_id)
        if m is None:
            raise ValueError(f"unknown message {message_id!r}")
        s = self.suppress_address(m.channel, m.sender, by, f"from an unmatched message ({m.kind})")
        self.unmatched.dismiss(message_id)
        return s

    def vendor_contacts(self):
        return list(self.outreach_store.contacts().values())

    def find_places(self, kind: str, city: str, reviews: bool = False, limit: int = 10):
        """Google ratings, review counts and price level for a service in a city (ADR-033)."""
        return self.places.search(kind, city, reviews, limit)

    def find_deals(self, query):
        """Search the web for advertised prices and rank the best of the best. Leads only; every search and fetch is logged (ADR-031)."""
        rid = "deals-" + uuid.uuid4().hex[:16]

        def log(tool, args, ok, text, ms):
            safe = {k: (v[:300] if isinstance(v, str) else v) for k, v in args.items()}
            self.logs.tool_calls.log(request_id=rid, tool=tool, args=safe, ok=ok, result=text[:2000], duration_ms=ms)

        return self.deals_factory(log).run(query)

    def search_flights(self, origin: str, destination: str, day: date, **kw):
        """Indicative fares and schedules for one direction. Nothing is booked (ADR-028)."""
        check_search(origin, destination, kw.get("adults", 1), kw.get("cabin", "economy"), kw.get("limit", 10))  # whatever the provider is
        return self.flights.search_one_way(origin, destination, day, **kw)

    def plan_trip(self, request, today: Optional[date] = None, **kw):
        """Order the stops, choose dates, search every distinct leg and rank complete itineraries (ADR-030). Every search is logged."""
        rid = "trip-" + uuid.uuid4().hex[:16]

        def log(leg, ok, text, ms):
            self.logs.tool_calls.log(
                request_id=rid, tool="flights.search_one_way", ok=ok, result=text, duration_ms=ms,
                args={"origin": leg[0], "destination": leg[1], "date": leg[2].isoformat(), "adults": request.adults, "cabin": request.cabin},
            )

        return search_trip(self.flights, request, today or date.today(), on_call=log, **kw)

    def search_multi_city(self, legs, **kw):
        return search_multi_city(self.flights, legs, **kw)

    def confirm(self, plan_id: str, category: VendorCategory) -> ReplanOutcome:
        return self.replanner.confirm(plan_id, category)

    def replan(self, plan_id: str, disruption: Disruption, today: Optional[date] = None) -> ReplanOutcome:
        self._clock(today)
        return self.replanner.replan(plan_id, disruption, today)

    def update_community(self, plan_id: str, details, today: Optional[date] = None) -> ReplanOutcome:
        """Replace a community plan's details (attendance, funding, volunteers, programme, access needs) as a validated new version, or refuse with repairs."""
        return self.replan(plan_id, Disruption(kind=DisruptionKind.COMMUNITY_UPDATE, new_community=details), today)

    def record_venue_accessibility(self, venue_id: str, features, evidence_ref: str, entered_by: str):
        """What a venue told the user about its access. Needs evidence; it turns an unverified item into a pass or a failure on the next validation."""
        if self.repo.get(VendorCategory.VENUE, venue_id) is None:
            raise ValueError(f"{venue_id!r} is not a venue in the database")
        return self.access_store.record(venue_id, features, evidence_ref, entered_by)

    def explain(self, plan_id: str, version: Optional[int] = None, with_diff: bool = True) -> Explanation:
        plan = self.store.load(plan_id, version)
        diff = None
        if with_diff and plan.parent_version:
            diff = diff_plans(self.store.load(plan_id, plan.parent_version), plan)
        comm = community_mod.build_status(plan, self.checklist_store, self.revalidate(plan_id, version)) if plan.request.event_type.value == "community" else None
        return self.explainer.explain(plan, diff, comm, self.verification(plan_id, version))

    def community_status(self, plan_id: str, today: Optional[date] = None):
        """The community view of a plan: hard checks, what could not be decided, and the conditional items with the user's marks."""
        plan = self.store.latest(plan_id)
        report = self.revalidate(plan_id) if plan.request.event_type.value == "community" else None  # current data: a venue's recorded answer counts at once
        return community_mod.build_status(plan, self.checklist_store, report)

    def set_community_item(self, plan_id: str, item_id: str, status: str, by, on, note: str = "", today: Optional[date] = None):
        """The user's mark on one conditional item (confirmed, not applicable, or reopened). Nothing else ever changes an item."""
        plan = self.store.latest(plan_id)
        if plan.request.event_type.value != "community":
            raise ValueError("this plan is not a community event")
        shown = [d.item_id for d, _ in community_mod.shown_items(plan.request)]
        return self.checklist_store.mark(plan_id, item_id, shown, status, by, on, note, today or date.today())

    def diff(self, plan_id: str, old_version: int, new_version: int) -> PlanDiff:
        return diff_plans(self.store.load(plan_id, old_version), self.store.load(plan_id, new_version))

    def decisions_for(self, plan: PlanVersion, max_files: int = 40):
        """Decision-log entries recorded while this plan version was produced (any run's log file)."""
        wanted = set(plan.decision_entry_ids)
        found = []
        files = sorted(self.logs.decisions.log_dir.glob("decisions-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
        for path in files:
            if path.stat().st_size > 20_000_000:
                continue
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    if any(i in line for i in wanted):
                        entry = DecisionLogEntry.model_validate_json(line)
                        if entry.entry_id in wanted:
                            found.append(entry)
        return sorted(found, key=lambda e: e.timestamp)

    def revalidate(self, plan_id: str, version: Optional[int] = None) -> ValidationReport:
        """Re-check a stored plan against the *current* database and rules."""
        plan = self.store.load(plan_id, version)
        parent = self.store.load(plan_id, plan.parent_version) if plan.parent_version else None
        return self.validator.validate(draft_from_plan(plan), parent, released_vendor_ids=plan.released_vendor_ids, newly_confirmed_vendor_ids=plan.newly_confirmed_vendor_ids)
