"""Vendor Agent: shortlists venues, caterers and photographers.

Every fact (availability, price, capacity, dietary options, booking status)
comes from the seed database through ``VendorRepository``. Nothing here asks
an LLM for facts. All hard constraints are evaluated for every record (no
short-circuit) so a rejection lists *every* reason, not just the first.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from datetime import date
from typing import Callable, Collection, List, Optional, Tuple, Union

from pydantic import ValidationError

from eventmate.decision_logger import DecisionLogger
from eventmate.schemas import (
    AgentName,
    Alternative,
    BookingStatus,
    BudgetAllocation,
    ConstraintCheck,
    ConstraintViolation,
    DecisionType,
    EventRequest,
    RejectedVendor,
    ShortlistMap,
    Vendor,
    VendorCandidate,
    VendorCategory,
    VendorShortlist,
    Venue,
    VenueSetting,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_DATA_BYTES = 5_000_000
MAX_LOGGED_ALTERNATIVES = 10

Provider = Union[Venue, Vendor]


class DataIntegrityError(Exception):
    """Seed data is malformed. The agent fails closed rather than guessing."""


def _load_records(path: Path) -> List[dict]:
    try:
        if path.stat().st_size > MAX_DATA_BYTES:
            raise DataIntegrityError(f"{path.name} exceeds {MAX_DATA_BYTES} bytes")
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DataIntegrityError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema_version") != 1:
        raise DataIntegrityError(f"{path.name}: unsupported schema_version")
    records = doc.get("records")
    if not isinstance(records, list):
        raise DataIntegrityError(f"{path.name}: 'records' must be a list")
    return records


class VendorRepository:
    """Read-only, validated view over the seed JSON files."""

    def __init__(self, venues_path: Optional[Path] = None, vendors_path: Optional[Path] = None):
        venues_path = Path(venues_path) if venues_path else DATA_DIR / "seed_venues.json"
        vendors_path = Path(vendors_path) if vendors_path else DATA_DIR / "seed_vendors.json"
        try:
            venues = tuple(Venue.model_validate(r) for r in _load_records(venues_path))
            vendors = tuple(Vendor.model_validate(r) for r in _load_records(vendors_path))
        except ValidationError as exc:
            raise DataIntegrityError(f"invalid record: {exc}") from exc
        ids = [p.id for p in venues + vendors]
        dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
        if dupes:
            raise DataIntegrityError(f"duplicate record ids: {dupes}")
        self._venues = venues
        self._vendors = vendors
        self.quotes = None  # ObservationStore, attached by EventMate (ADR-026)
        self.clock: Callable[[], date] = date.today

    def _quote(self, provider: "Provider"):
        return self.quotes.pricing_quote(provider.id) if self.quotes is not None else None

    def quote_expired(self, provider: "Provider") -> bool:
        q = self._quote(provider)
        return q is not None and self.clock() > q.expires_at

    def price(self, provider: "Provider", guests: int) -> int:
        """Price for ``guests``: the latest unexpired recorded quote (plus its known hidden costs), else the seed record.

        An expired quote is not used; ``evaluate`` then fails the vendor on ``quote_valid`` instead of silently using stale money.
        """
        q = self._quote(provider)
        if q is None or self.quote_expired(provider):
            return provider.cost_for(guests)
        base = q.amount_inr * guests if q.basis == "per_plate" else q.amount_inr
        return base + sum(h.amount_inr for h in q.hidden_costs if h.amount_inr)

    def providers(self, category: VendorCategory) -> Tuple[Provider, ...]:
        if category == VendorCategory.VENUE:
            return self._venues
        return tuple(v for v in self._vendors if v.category == category)

    def get(self, category: VendorCategory, provider_id: str) -> Optional[Provider]:
        return next((p for p in self.providers(category) if p.id == provider_id), None)

    def cities(self) -> Tuple[str, ...]:
        return tuple(sorted({p.city for p in self._venues + self._vendors}))


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def evaluate(
    provider: Provider, request: EventRequest, category: VendorCategory, cap: int, excluded: bool = False,
    price: Optional[int] = None, quote_expired: bool = False,
) -> List[ConstraintCheck]:
    """Run every hard constraint against one record."""
    guests = request.guest_count
    checks = [
        ConstraintCheck(
            name="booking_status",
            passed=provider.booking_status == BookingStatus.AVAILABLE and not excluded,
            detail="vendor was cancelled or excluded for this event" if excluded else f"status is {provider.booking_status.value}",
        ),
        ConstraintCheck(
            name="city",
            passed=_norm(provider.city) == _norm(request.city),
            detail=f"vendor city {provider.city}, requested {request.city}",
        ),
        ConstraintCheck(
            name="date_availability",
            passed=request.event_date in provider.available_dates,
            detail=f"{request.event_date.isoformat()} "
            + ("is" if request.event_date in provider.available_dates else "is not")
            + " in the vendor's available dates",
        ),
    ]
    if category == VendorCategory.VENUE:
        checks.append(
            ConstraintCheck(
                name="capacity",
                passed=guests <= provider.capacity,
                detail=f"{guests} guests vs capacity {provider.capacity}",
            )
        )
        wanted = request.venue_setting
        checks.append(
            ConstraintCheck(
                name="venue_setting",
                passed=wanted == VenueSetting.ANY or wanted.value == provider.setting,
                detail=f"venue is {provider.setting}, requested {wanted.value}",
            )
        )
    elif category == VendorCategory.CATERER:
        checks.append(
            ConstraintCheck(
                name="capacity",
                passed=provider.min_guests <= guests <= provider.max_guests,
                detail=f"{guests} guests vs served range {provider.min_guests}-{provider.max_guests}",
            )
        )
        missing = [d for d in request.dietary if d not in provider.dietary_options]
        checks.append(
            ConstraintCheck(
                name="dietary",
                passed=not missing,
                detail="supports all required diets"
                if not missing
                else "does not offer: " + ", ".join(d.value for d in missing),
            )
        )
    cost = provider.cost_for(guests) if price is None else price
    if quote_expired:
        checks.append(ConstraintCheck(name="quote_valid", passed=False, detail="re-quote needed: the recorded quote has expired"))
    checks.append(
        ConstraintCheck(
            name="budget_cap",
            passed=cost <= cap,
            detail=f"cost INR {cost:,} vs category cap INR {cap:,}",
        )
    )
    return checks


def _score(rating: float, cost: int, cap: int) -> float:
    price_term = 1.0 - cost / cap if cap > 0 else 0.0
    return round(0.6 * (rating / 5.0) + 0.4 * price_term, 4)


class VendorAgent:
    agent_name = AgentName.VENDOR

    def __init__(self, repo: VendorRepository, logger: DecisionLogger, max_candidates: int = 3):
        if max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        self.repo = repo
        self.logger = logger
        self.max_candidates = max_candidates

    def shortlist(self, request: EventRequest, allocation: BudgetAllocation, exclude_ids: Collection[str] = ()) -> ShortlistMap:
        return {
            category: self.shortlist_category(request, category, allocation.cap_for(category), exclude_ids)
            for category in request.services
        }

    def shortlist_category(
        self, request: EventRequest, category: VendorCategory, cap: int, exclude_ids: Collection[str] = ()
    ) -> VendorShortlist:
        passed: List[Tuple[Provider, int, float, List[ConstraintCheck]]] = []
        rejected: List[Tuple[Provider, List[ConstraintCheck]]] = []
        providers = self.repo.providers(category)
        for p in providers:
            checks = evaluate(p, request, category, cap, excluded=p.id in exclude_ids, price=self.repo.price(p, request.guest_count), quote_expired=self.repo.quote_expired(p))
            failed = [c for c in checks if not c.passed]
            if failed:
                rejected.append((p, failed))
            else:
                cost = self.repo.price(p, request.guest_count)
                passed.append((p, cost, _score(p.rating, cost, cap), checks))
        passed.sort(key=lambda t: (-t[2], t[1], t[0].id))
        top, rest = passed[: self.max_candidates], passed[self.max_candidates :]

        candidates = tuple(
            VendorCandidate(
                vendor_id=p.id,
                name=p.name,
                category=category,
                estimated_cost=cost,
                score=score,
                checks=tuple(checks),
                source=p.source,
            )
            for p, cost, score, checks in top
        )
        rejected_models = tuple(
            RejectedVendor(
                vendor_id=p.id,
                name=p.name,
                category=category,
                estimated_cost=self.repo.price(p, request.guest_count),
                failed=tuple(f),
            )
            for p, f in sorted(rejected, key=lambda t: t[0].id)
        )
        violations = () if candidates else self._repair(request, category, cap, providers, rejected)
        shortlist = VendorShortlist(
            category=category,
            budget_cap=cap,
            candidates=candidates,
            rejected=rejected_models,
            violations=tuple(violations),
        )
        self._log(request, shortlist, rest)
        return shortlist

    # ------------------------------------------------------------ repairs

    def _repair(self, request, category, cap, providers, rejected) -> List[ConstraintViolation]:
        label = category.value
        if not providers:
            return [
                ConstraintViolation(
                    constraint="vendor_coverage",
                    detail=f"database has no {label} records",
                    repair=f"add {label} records to the database before planning",
                )
            ]
        only_budget = [(p, self.repo.price(p, request.guest_count)) for p, f in rejected if {c.name for c in f} == {"budget_cap"}]
        if only_budget:
            p, cost = min(only_budget, key=lambda t: (t[1], t[0].id))
            return [
                ConstraintViolation(
                    constraint="budget_cap",
                    detail=f"cheapest otherwise-eligible {label} ({p.name}) costs INR {cost:,}; cap is INR {cap:,}",
                    repair=f"raise the {label} allocation to at least INR {cost:,} or raise the total budget",
                )
            ]
        counts = Counter(c.name for _, f in rejected for c in f)
        worst, _ = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        return [
            ConstraintViolation(
                constraint=worst,
                detail=f"no {label} satisfies all constraints; most common failure is {worst} "
                f"({counts[worst]} of {len(providers)} records)",
                repair=self._repair_text(worst, request, category, rejected),
            )
        ]

    @staticmethod
    def _repair_text(constraint, request, category, rejected) -> str:
        label = category.value
        if constraint == "date_availability":
            near = sorted(
                {
                    d
                    for p, f in rejected
                    if {c.name for c in f} == {"date_availability"}
                    for d in p.available_dates
                },
                key=lambda d: (abs((d - request.event_date).days), d),
            )[:3]
            if near:
                return "move the event to one of: " + ", ".join(d.isoformat() for d in near)
            return f"no {label} is free near {request.event_date.isoformat()}; choose a different date"
        if constraint == "city":
            return f"no {label} in {request.city}; choose a different city or add vendors there"
        if constraint == "capacity":
            return f"change the guest count ({request.guest_count}) or relax capacity needs for the {label}"
        if constraint == "dietary":
            return "relax the dietary requirements or add caterers that offer them"
        if constraint == "venue_setting":
            return "allow either indoor or outdoor venues"
        if constraint == "quote_valid":
            return f"re-quote needed: ask the {label} vendors for a fresh quote and record it"
        if constraint == "booking_status":
            return f"all matching {label}s are unavailable; add alternatives or choose another date"
        return f"relax the {constraint} constraint"

    # ------------------------------------------------------------ logging

    def _log(self, request: EventRequest, shortlist: VendorShortlist, rest) -> None:
        label = shortlist.category.value
        db = "database:seed_venues.json" if shortlist.category == VendorCategory.VENUE else "database:seed_vendors.json"
        if not shortlist.candidates:
            v = shortlist.violations[0]
            self.logger.log(
                request_id=request.request_id,
                agent=self.agent_name,
                decision_type=DecisionType.FAILURE,
                summary=f"No eligible {label} found",
                alternatives=self._alternatives(shortlist, rest),
                constraints_checked=tuple(c for r in shortlist.rejected[:3] for c in r.failed),
                justification=f"{v.detail}. Repair: {v.repair}.",
                confidence=1.0,
                source=db,
            )
            return
        top = shortlist.candidates[0]
        alts = self._alternatives(shortlist, rest)
        self.logger.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.RECOMMENDATION,
            summary=f"Shortlisted {len(shortlist.candidates)} {label}(s); top choice {top.name}",
            alternatives=alts,
            no_alternative_reason=None if alts else f"only {top.name} exists in the database for {request.city}",
            constraints_checked=top.checks,
            justification=(
                f"{top.name} passes every hard constraint at INR {top.estimated_cost:,} "
                f"(cap INR {shortlist.budget_cap:,}) and has the best rating/price score {top.score}. "
                f"{len(shortlist.rejected)} record(s) were rejected."
            ),
            confidence=0.9 if len(shortlist.candidates) >= 2 else 0.6,
            source=top.source,
        )

    @staticmethod
    def _alternatives(shortlist: VendorShortlist, rest) -> Tuple[Alternative, ...]:
        alts: List[Alternative] = []
        for c in shortlist.candidates[1:]:
            alts.append(
                Alternative(
                    option=c.name,
                    outcome=f"eligible, INR {c.estimated_cost:,}, score {c.score}",
                    reason_not_chosen="lower rating/price score than the top choice",
                )
            )
        for p, cost, score, _ in rest:
            alts.append(
                Alternative(
                    option=p.name,
                    outcome=f"eligible, INR {cost:,}, score {score}",
                    reason_not_chosen="ranked below the shortlist size limit",
                )
            )
        for r in shortlist.rejected:
            alts.append(
                Alternative(
                    option=r.name,
                    outcome="rejected",
                    reason_not_chosen="; ".join(f"{c.name}: {c.detail}" for c in r.failed),
                )
            )
        return tuple(alts[:MAX_LOGGED_ALTERNATIVES])
