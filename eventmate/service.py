"""EventMate facade: wires every agent and exposes the whole workflow.

    em = EventMate()
    outcome = em.plan("Plan a 120-person engagement party in Pune ...")
    em.confirm(plan_id, VendorCategory.VENUE)        # the user confirmed a booking
    em.replan(plan_id, Disruption(kind=..., ...))    # new immutable version + diff
    em.explain(plan_id)                              # readable text with reasons
"""

from __future__ import annotations

import os
import uuid
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

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
from eventmate.leads import LeadStore
from eventmate import community as community_mod
from eventmate.community import AccessibilityStore, ChecklistStore
from eventmate.digests import DigestStore
from eventmate.notify import OwnerNotifier
from eventmate.ideas import IdeaService
from eventmate.channels import Channel, channels_from_env
from eventmate.conversations import ConversationService
from eventmate.outreach import OutreachStore
from eventmate.worker import JobWorker
from eventmate.jobs import JobStore
from eventmate.ledger import Ledger
from eventmate.observations import ObservationStore
from eventmate.vendorscore import score_category
from eventmate import intake
from eventmate.llm import LLMClient, client_from_env
from eventmate.logs import RunLogs
from eventmate.rules import Advisory, advisories_for
from eventmate.plan_store import PlanStore, draft_from_plan
from eventmate.schemas import WeatherAdvice, BookingStatus, DecisionLogEntry, SelectionQuoteStatus, LedgerEntry, LedgerSummary, Disruption, DisruptionKind, PlanDiff, PlanOutcome, PlanVersion, ReplanOutcome, TradeoffOption, TradeoffReport, ValidationReport, VendorCategory
from eventmate.symbolic import SymbolicChecker
from eventmate.deals import DealAgent
from eventmate.discovery import OverpassSource
from eventmate.web_access import SafeFetcher
from eventmate.websearch import default_provider
from eventmate.env import load_env
from eventmate.places import PlacesSource
from eventmate.weather import OpenWeatherSource
from eventmate.weather_advice import advise
from eventmate.travel import FlightProvider, FliProvider, check_search, search_multi_city
from eventmate.tripsearch import search_trip


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

    def _dry_run_ok(self, request) -> bool:
        return self.manager.run_request(request, today=self._today, guest_list_id=self._guest_list_id, persist=False).status == "planned"

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
        return {p.id: p.name for c in VendorCategory for p in self.repo.providers(c)}

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
            record = self.repo.get(category, vendor_id)
            if record is None:
                raise ValueError(f"unknown {category.value} {vendor_id!r}")
            vendor = (record.id, record.name)
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

    def set_vendor_contact(self, vendor_id: str, email: Optional[str], whatsapp: Optional[str], preferred: str):
        if not any(self.repo.get(c, vendor_id) for c in VendorCategory):
            raise ValueError(f"unknown vendor {vendor_id!r}")
        return self.outreach_store.set_contact(vendor_id, email, whatsapp, preferred)

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
        return self.explainer.explain(plan, diff, comm)

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
