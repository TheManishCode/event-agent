"""Typed data contracts for EventMate.

Every structure that crosses an agent boundary, is persisted, or is logged is
a frozen Pydantic model with ``extra="forbid"``: unknown fields are rejected
rather than silently dropped, and nothing is mutated after validation.

Money is whole INR (``int``) to avoid float drift; budget sums are exact.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_GUESTS = 100_000
MAX_BUDGET_INR = 10_000_000_000
MAX_ORGANIZERS = 10

# Decision sources are restricted so a log entry can never claim a fact came
# from somewhere unaccountable. LLM-originated content must be tagged "llm:".
SOURCE_PATTERN = re.compile(
    r"^(user_input|database:[A-Za-z0-9_./-]+|rule:[A-Za-z0-9_.-]+"
    r"|tool:[A-Za-z0-9_.-]+|llm:[A-Za-z0-9_.-]+|web:[a-z0-9.-]+)$"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventType(str, Enum):
    ENGAGEMENT_PARTY = "engagement_party"
    WEDDING = "wedding"
    BIRTHDAY = "birthday"
    CORPORATE = "corporate"
    CONFERENCE = "conference"
    OTHER = "other"


class DietaryRequirement(str, Enum):
    VEGETARIAN = "vegetarian"
    NON_VEGETARIAN = "non_vegetarian"
    VEGAN = "vegan"
    JAIN = "jain"
    HALAL = "halal"
    GLUTEN_FREE = "gluten_free"


class VenueSetting(str, Enum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    ANY = "any"


class VendorCategory(str, Enum):
    VENUE = "venue"
    CATERER = "caterer"
    PHOTOGRAPHER = "photographer"


class ReminderKind(str, Enum):
    INVITATION = "invitation"
    VENDOR_CONFIRMATION = "vendor_confirmation"


class BookingStatus(str, Enum):
    AVAILABLE = "available"
    HELD = "held"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class AgentName(str, Enum):
    MANAGER = "manager"
    BUDGET = "budget"
    VENDOR = "vendor"
    GUEST = "guest"
    SCHEDULER = "scheduler"
    PLANNER = "planner"
    VALIDATOR = "validator"
    EXPLAINER = "explainer"
    REPLANNER = "replanner"


class TaskKind(str, Enum):
    ALLOCATE_BUDGET = "allocate_budget"
    TRACK_GUESTS = "track_guests"
    SOURCE_VENDOR = "source_vendor"
    BUILD_SCHEDULE = "build_schedule"
    SCHEDULE_REMINDER = "schedule_reminder"
    BUILD_PLAN_MODEL = "build_plan_model"
    VALIDATE_PLAN = "validate_plan"
    EXPLAIN_PLAN = "explain_plan"


class TaskStatus(str, Enum):
    PENDING = "pending"
    DONE = "done"


class ConstraintCheck(_Model):
    """Outcome of one named hard-constraint check; the unit of explainability."""

    name: str
    passed: bool
    detail: str


class ConstraintViolation(_Model):
    """A failed constraint with a concrete proposed repair (quality rule)."""

    constraint: str
    detail: str
    repair: str


# ---------------------------------------------------------------- request


class CapWeight(_Model):
    """A user-approved share (relative weight) for one category's budget cap."""

    category: VendorCategory
    weight: int = Field(gt=0, le=MAX_BUDGET_INR)


class EventRequest(_Model):
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    raw_text: str = Field(min_length=1, max_length=2000)
    event_type: EventType
    guest_count: int = Field(ge=1, le=MAX_GUESTS)
    city: str = Field(min_length=1, max_length=80)
    event_date: date
    budget_total: int = Field(gt=0, le=MAX_BUDGET_INR)
    currency: Literal["INR"] = "INR"
    dietary: Tuple[DietaryRequirement, ...] = ()
    venue_setting: VenueSetting = VenueSetting.ANY
    services: Tuple[VendorCategory, ...] = (VendorCategory.VENUE,)
    reminders: Tuple[ReminderKind, ...] = ()
    assumptions: Tuple[str, ...] = ()
    parse_confidence: float = Field(ge=0.0, le=1.0)
    # empty = the BudgetPolicy default split; otherwise exactly one weight per requested service
    cap_weights: Tuple[CapWeight, ...] = ()
    # people who can make bookings in parallel; the capacity of the scheduler's organizer resource (ADR-020)
    organizers: int = Field(default=1, ge=1, le=MAX_ORGANIZERS)

    def with_changes(self, **changes) -> "EventRequest":
        """Copy with changes, re-running validation (``model_copy`` would skip it)."""
        return EventRequest.model_validate({**self.model_dump(), **changes})

    @model_validator(mode="after")
    def _venue_always_required(self) -> "EventRequest":
        if VendorCategory.VENUE not in self.services:
            raise ValueError("services must include a venue")
        if len(set(self.services)) != len(self.services):
            raise ValueError("services contain duplicates")
        if self.cap_weights and sorted(w.category.value for w in self.cap_weights) != sorted(c.value for c in self.services):
            raise ValueError("cap_weights must name each requested service exactly once")
        return self


# ------------------------------------------------------------- task graph


class Task(_Model):
    id: str = Field(pattern=r"^[a-z_]+:[a-z_]+$")
    kind: TaskKind
    subject: str = Field(pattern=r"^[a-z_]+$")
    agent: AgentName
    depends_on: Tuple[str, ...] = ()
    description: str
    status: TaskStatus = TaskStatus.PENDING


class TaskGraph(_Model):
    """A DAG of tasks. Structural validity is enforced at construction."""

    tasks: Tuple[Task, ...]

    @model_validator(mode="after")
    def _structure(self) -> "TaskGraph":
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task ids")
        keys = [(t.kind, t.subject) for t in self.tasks]
        if len(set(keys)) != len(keys):
            raise ValueError("redundant tasks: duplicate (kind, subject)")
        known = set(ids)
        for t in self.tasks:
            if t.id in t.depends_on:
                raise ValueError(f"task {t.id} depends on itself")
            if len(set(t.depends_on)) != len(t.depends_on):
                raise ValueError(f"task {t.id} has duplicate dependencies")
            missing = set(t.depends_on) - known
            if missing:
                raise ValueError(f"task {t.id} depends on unknown {sorted(missing)}")
        if len(self._topological(self.tasks)) != len(self.tasks):
            raise ValueError("task graph contains a cycle")
        return self

    @staticmethod
    def _topological(tasks: Tuple[Task, ...]) -> List[Task]:
        remaining = {t.id: t for t in tasks}
        done: set = set()
        ordered: List[Task] = []
        while remaining:
            ready = sorted(
                (t for t in remaining.values() if set(t.depends_on) <= done),
                key=lambda t: t.id,
            )
            if not ready:
                break
            for t in ready:
                ordered.append(t)
                done.add(t.id)
                del remaining[t.id]
        return ordered

    def topological_order(self) -> List[Task]:
        return self._topological(self.tasks)

    def get(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def ancestors(self, task_id: str) -> set:
        seen: set = set()
        stack = list(self.get(task_id).depends_on)
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(self.get(current).depends_on)
        return seen

    def with_status(self, task_id: str, status: TaskStatus) -> "TaskGraph":
        self.get(task_id)
        return TaskGraph(
            tasks=tuple(
                t.model_copy(update={"status": status}) if t.id == task_id else t
                for t in self.tasks
            )
        )


# ----------------------------------------------------------------- budget


class BudgetLine(_Model):
    category: str
    amount: int = Field(ge=0)
    share: float = Field(ge=0.0, le=1.0)


class BudgetAllocation(_Model):
    total: int = Field(gt=0)
    currency: Literal["INR"] = "INR"
    lines: Tuple[BudgetLine, ...]
    contingency: int = Field(ge=0)

    @model_validator(mode="after")
    def _sums_exactly(self) -> "BudgetAllocation":
        spent = sum(line.amount for line in self.lines) + self.contingency
        if spent != self.total:
            raise ValueError(f"allocation sums to {spent}, expected {self.total}")
        return self

    def cap_for(self, category: VendorCategory) -> int:
        for line in self.lines:
            if line.category == category.value:
                return line.amount
        raise KeyError(category.value)


class BudgetAssessment(_Model):
    status: Literal["within_budget", "contingency_breach", "overspend", "incomplete"]
    cheapest_total: Optional[int] = None
    headroom: Optional[int] = None
    violations: Tuple[ConstraintViolation, ...] = ()


# ----------------------------------------------------------------- vendors


class _Provider(_Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=120)
    city: str = Field(min_length=1, max_length=80)
    available_dates: Tuple[date, ...] = ()
    booking_status: BookingStatus = BookingStatus.AVAILABLE
    rating: float = Field(ge=0.0, le=5.0)
    source: str

    @field_validator("source")
    @classmethod
    def _source_ok(cls, v: str) -> str:
        if not SOURCE_PATTERN.fullmatch(v):
            raise ValueError("invalid source tag")
        return v


class Venue(_Provider):
    setting: Literal["indoor", "outdoor"]
    capacity: int = Field(ge=1, le=MAX_GUESTS)
    flat_fee: int = Field(ge=0, le=MAX_BUDGET_INR)

    def cost_for(self, guest_count: int) -> int:
        return self.flat_fee


class Vendor(_Provider):
    """Caterer or photographer. Pricing fields depend on ``category``."""

    category: Literal[VendorCategory.CATERER, VendorCategory.PHOTOGRAPHER]
    price_per_plate: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET_INR)
    min_guests: Optional[int] = Field(default=None, ge=1, le=MAX_GUESTS)
    max_guests: Optional[int] = Field(default=None, ge=1, le=MAX_GUESTS)
    dietary_options: Tuple[DietaryRequirement, ...] = ()
    flat_fee: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET_INR)

    @model_validator(mode="after")
    def _pricing_matches_category(self) -> "Vendor":
        if self.category == VendorCategory.CATERER:
            if None in (self.price_per_plate, self.min_guests, self.max_guests):
                raise ValueError("caterer needs price_per_plate, min_guests, max_guests")
            if self.flat_fee is not None:
                raise ValueError("caterer must not set flat_fee")
            if self.min_guests > self.max_guests:
                raise ValueError("min_guests > max_guests")
        else:
            if self.flat_fee is None:
                raise ValueError("photographer needs flat_fee")
            if self.price_per_plate is not None:
                raise ValueError("photographer must not set price_per_plate")
        return self

    def cost_for(self, guest_count: int) -> int:
        if self.category == VendorCategory.CATERER:
            return self.price_per_plate * guest_count
        return self.flat_fee


class VendorCandidate(_Model):
    vendor_id: str
    name: str
    category: VendorCategory
    estimated_cost: int = Field(ge=0)
    score: float
    checks: Tuple[ConstraintCheck, ...]
    source: str


class RejectedVendor(_Model):
    vendor_id: str
    name: str
    category: VendorCategory
    estimated_cost: int = Field(ge=0)
    failed: Tuple[ConstraintCheck, ...]


class VendorShortlist(_Model):
    category: VendorCategory
    budget_cap: int
    candidates: Tuple[VendorCandidate, ...]
    rejected: Tuple[RejectedVendor, ...]
    violations: Tuple[ConstraintViolation, ...] = ()


# -------------------------------------------------------------- decisions


class Alternative(_Model):
    option: str
    outcome: str
    reason_not_chosen: str


class DecisionType(str, Enum):
    PARSE = "parse"
    DECOMPOSITION = "decomposition"
    ALLOCATION = "allocation"
    RECOMMENDATION = "recommendation"
    ASSESSMENT = "assessment"
    FAILURE = "failure"


class DecisionLogEntry(_Model):
    entry_id: str
    timestamp: datetime
    run_id: str
    request_id: str
    agent: AgentName
    decision_type: DecisionType
    summary: str = Field(min_length=1, max_length=500)
    alternatives: Tuple[Alternative, ...] = ()
    no_alternative_reason: Optional[str] = None
    constraints_checked: Tuple[ConstraintCheck, ...] = ()
    justification: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    source: str

    @field_validator("source")
    @classmethod
    def _source_ok(cls, v: str) -> str:
        if not SOURCE_PATTERN.fullmatch(v):
            raise ValueError("invalid source tag")
        return v

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _recommendations_need_alternatives(self) -> "DecisionLogEntry":
        if (
            self.decision_type == DecisionType.RECOMMENDATION
            and not self.alternatives
            and not self.no_alternative_reason
        ):
            raise ValueError("recommendation needs alternatives or no_alternative_reason")
        return self


# ------------------------------------------------------------ intake result


class CategoryFloor(_Model):
    category: VendorCategory
    vendor_id: str
    vendor_name: str
    cost: int = Field(ge=0)


class RealityCheck(_Model):
    """Is the budget realistic? An estimate from database records, never a quote."""

    verdict: Literal["comfortable", "tight", "contingency_breach", "unrealistic", "unknown"]
    budget: int = Field(gt=0)
    floor: Optional[int] = Field(default=None, ge=0)
    per_category: Tuple[CategoryFloor, ...] = ()
    suggested_minimum_budget: Optional[int] = Field(default=None, gt=0)
    message: str
    basis: str
    source: str = "tool:reality_check"


class IntakeResult(_Model):
    """Output of the first milestone. ``final_plan`` is always None here."""

    request: EventRequest
    task_graph: TaskGraph
    budget_allocation: BudgetAllocation
    vendor_shortlists: Tuple[VendorShortlist, ...]
    budget_assessment: BudgetAssessment
    guests: Optional["GuestSummary"] = None
    reality: Optional[RealityCheck] = None
    status: Literal["intake_complete", "needs_repair"]
    violations: Tuple[ConstraintViolation, ...] = ()
    decision_entry_ids: Tuple[str, ...] = ()
    final_plan: None = None




ShortlistMap = Dict[VendorCategory, VendorShortlist]

# ------------------------------------------------------------------ guests


class RSVP(str, Enum):
    YES = "yes"
    NO = "no"
    PENDING = "pending"


class Guest(_Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=120)
    rsvp: RSVP = RSVP.PENDING
    party_size: int = Field(default=1, ge=1, le=20)
    dietary: Tuple[DietaryRequirement, ...] = ()


class GuestSummary(_Model):
    """Headcount facts. ``planning_headcount`` is always the user-stated count."""

    planning_headcount: int = Field(ge=1)
    confirmed: int = Field(ge=0)
    declined: int = Field(ge=0)
    pending: int = Field(ge=0)
    dietary_counts: Dict[str, int] = {}
    required_diets: Tuple[DietaryRequirement, ...] = ()
    checks: Tuple[ConstraintCheck, ...] = ()
    guest_list_id: Optional[str] = None
    source: str


# --------------------------------------------------------------- scheduling

TaskGroup = Literal["pre_event", "event_day"]


class ScheduleTask(_Model):
    """One interval to place. Times are whole minutes from the plan origin."""

    id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    name: str = Field(min_length=1, max_length=120)
    duration: int = Field(ge=1, le=525_600)
    resource: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_]{1,40}$")
    group: TaskGroup = "pre_event"
    earliest_start: int = Field(default=0, ge=0)
    fixed_start: Optional[int] = Field(default=None, ge=0)
    deadline_end: Optional[int] = Field(default=None, ge=0)
    prefer: Literal["early", "late", "none"] = "none"
    weight: int = Field(default=1, ge=1, le=1000)


class ScheduleDependency(_Model):
    before: str
    after: str
    min_lag: int = Field(default=0, ge=0)


def _has_cycle(ids: List[str], edges: List[Tuple[str, str]]) -> bool:
    indegree = {i: 0 for i in ids}
    for _, b in edges:
        indegree[b] += 1
    queue = [i for i, n in indegree.items() if n == 0]
    seen = 0
    while queue:
        node = queue.pop()
        seen += 1
        for a, b in edges:
            if a == node:
                indegree[b] -= 1
                if indegree[b] == 0:
                    queue.append(b)
    return seen != len(ids)


class ScheduleSpec(_Model):
    tasks: Tuple[ScheduleTask, ...]
    dependencies: Tuple[ScheduleDependency, ...] = ()
    resources: Dict[str, int] = {}
    horizon: int = Field(gt=0, le=5_256_000)
    anchor_task: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "ScheduleSpec":
        ids = [t.id for t in self.tasks]
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("schedule needs unique, non-empty task ids")
        known = set(ids)
        for d in self.dependencies:
            if d.before not in known or d.after not in known or d.before == d.after:
                raise ValueError(f"bad dependency {d.before}->{d.after}")
        if self.anchor_task is not None and self.anchor_task not in known:
            raise ValueError("anchor_task is not a task")
        if self.anchor_task is None and any(t.prefer == "late" for t in self.tasks):
            raise ValueError("tasks preferring 'late' need an anchor_task")
        if _has_cycle(ids, [(d.before, d.after) for d in self.dependencies]):
            raise ValueError("dependencies contain a cycle")
        for t in self.tasks:
            if t.resource is not None and self.resources.get(t.resource, 0) < 1:
                raise ValueError(f"task {t.id} uses undeclared resource {t.resource}")
            if t.earliest_start + t.duration > self.horizon:
                raise ValueError(f"task {t.id} cannot fit inside the horizon")
        return self


class ScheduledEntry(_Model):
    task_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ScheduleSolution(_Model):
    status: Literal["optimal", "feasible", "infeasible", "error"]
    entries: Tuple[ScheduledEntry, ...] = ()
    objective: Optional[int] = None
    infeasibility_core: Tuple[str, ...] = ()
    detail: str = ""


class SymbolicResult(_Model):
    """PDDL feasibility check. ``unavailable`` means the solver binaries are absent."""

    status: Literal["valid", "invalid", "no_plan", "unavailable", "error"]
    plan_actions: Tuple[str, ...] = ()
    detail: str = ""


# -------------------------------------------------------------------- plans


class VendorSelection(_Model):
    category: VendorCategory
    vendor_id: str
    name: str
    cost: int = Field(ge=0)
    booking_status: BookingStatus = BookingStatus.AVAILABLE
    score: float = 0.0
    justification: str
    alternatives: Tuple[Alternative, ...] = ()
    no_alternative_reason: Optional[str] = None
    source: str

    @model_validator(mode="after")
    def _has_alternative_or_reason(self) -> "VendorSelection":
        if not self.alternatives and not self.no_alternative_reason:
            raise ValueError("every recommendation needs an alternative or a no_alternative_reason")
        return self


class PlanItem(_Model):
    """A scheduled task with wall-clock times (timezone-naive local event time)."""

    task_id: str
    name: str
    resource: Optional[str] = None
    group: TaskGroup
    start: datetime
    end: datetime


class ValidationReport(_Model):
    passed: bool
    checks: Tuple[ConstraintCheck, ...]
    violations: Tuple[ConstraintViolation, ...] = ()
    warnings: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _passed_matches_checks(self) -> "ValidationReport":
        if self.passed != all(c.passed for c in self.checks):
            raise ValueError("passed must equal all(check.passed)")
        if self.passed and self.violations:
            raise ValueError("a passing report cannot list violations")
        return self


class PlanVersion(_Model):
    """One immutable version of an event plan. Only validated plans can exist."""

    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    version: int = Field(ge=1)
    parent_version: Optional[int] = Field(default=None, ge=1)
    created_at: datetime
    origin: datetime
    change_reason: str = Field(min_length=1, max_length=500)
    request: EventRequest
    guests: GuestSummary
    allocation: BudgetAllocation
    selections: Tuple[VendorSelection, ...]
    spec: ScheduleSpec
    solution: ScheduleSolution
    items: Tuple[PlanItem, ...]
    total_cost: int = Field(ge=0)
    validation: ValidationReport
    symbolic: SymbolicResult
    decision_entry_ids: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    excluded_vendor_ids: Tuple[str, ...] = ()
    # vendors the user confirmed *in this version* (the provenance of CONFIRMED status)
    newly_confirmed_vendor_ids: Tuple[str, ...] = ()
    # confirmed vendors this version let go of (vendor cancelled, or the user explicitly overrode)
    released_vendor_ids: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _only_validated_plans_exist(self) -> "PlanVersion":
        if not self.validation.passed:
            raise ValueError("a plan with failed hard constraints cannot be constructed")
        if self.parent_version is not None and self.parent_version >= self.version:
            raise ValueError("parent_version must be older than version")
        if self.total_cost != sum(s.cost for s in self.selections):
            raise ValueError("total_cost must equal the sum of selection costs")
        return self

    def selection_for(self, category: VendorCategory) -> Optional[VendorSelection]:
        return next((s for s in self.selections if s.category == category), None)


DiffKind = Literal["request", "vendor", "task", "budget"]


class DiffItem(_Model):
    kind: DiffKind
    key: str
    before: Optional[str] = None
    after: Optional[str] = None
    note: str = ""


class PlanDiff(_Model):
    parent_version: int
    new_version: int
    added: Tuple[DiffItem, ...] = ()
    removed: Tuple[DiffItem, ...] = ()
    changed: Tuple[DiffItem, ...] = ()
    preserved: Tuple[DiffItem, ...] = ()


class DisruptionKind(str, Enum):
    VENDOR_CANCELLATION = "vendor_cancellation"
    BUDGET_CHANGE = "budget_change"
    GUEST_COUNT_CHANGE = "guest_count_change"
    DATE_CHANGE = "date_change"


class Disruption(_Model):
    """A user- or tool-reported change. Exactly one payload field matches ``kind``."""

    kind: DisruptionKind
    vendor_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    new_budget: Optional[int] = Field(default=None, gt=0, le=MAX_BUDGET_INR)
    new_guest_count: Optional[int] = Field(default=None, ge=1, le=MAX_GUESTS)
    new_date: Optional[date] = None
    # Confirmed bookings are kept unless their vendor id is listed here.
    override_confirmed: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> "Disruption":
        payload = {
            DisruptionKind.VENDOR_CANCELLATION: ("vendor_id", self.vendor_id),
            DisruptionKind.BUDGET_CHANGE: ("new_budget", self.new_budget),
            DisruptionKind.GUEST_COUNT_CHANGE: ("new_guest_count", self.new_guest_count),
            DisruptionKind.DATE_CHANGE: ("new_date", self.new_date),
        }
        for kind, (name, value) in payload.items():
            if kind == self.kind and value is None:
                raise ValueError(f"{self.kind.value} requires {name}")
            if kind != self.kind and value is not None:
                raise ValueError(f"{name} is not valid for {self.kind.value}")
        return self


class ReplanOutcome(_Model):
    status: Literal["replanned", "needs_decision", "failed"]
    plan: Optional[PlanVersion] = None
    diff: Optional[PlanDiff] = None
    violations: Tuple[ConstraintViolation, ...] = ()
    decision_entry_ids: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "ReplanOutcome":
        if self.status == "replanned" and (self.plan is None or self.diff is None):
            raise ValueError("replanned outcome needs plan and diff")
        if self.status != "replanned" and (self.plan is not None or self.diff is not None):
            raise ValueError("only a replanned outcome carries a plan")
        if self.status != "replanned" and not self.violations:
            raise ValueError("a non-replanned outcome must say which constraint failed")
        return self


class PlanOutcome(_Model):
    status: Literal["planned", "needs_repair"]
    intake: IntakeResult
    task_graph: TaskGraph
    plan: Optional[PlanVersion] = None
    violations: Tuple[ConstraintViolation, ...] = ()
    decision_entry_ids: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "PlanOutcome":
        if (self.status == "planned") != (self.plan is not None):
            raise ValueError("plan present iff status is planned")
        if self.status == "needs_repair" and not self.violations:
            raise ValueError("needs_repair must list violations")
        return self


# ------------------------------------------------------------------- drafts


class PlanDraft(_Model):
    """A candidate plan. It becomes a ``PlanVersion`` only after the Validator passes it."""

    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    origin: datetime
    request: EventRequest
    guests: GuestSummary
    allocation: BudgetAllocation
    selections: Tuple[VendorSelection, ...]
    spec: ScheduleSpec
    solution: ScheduleSolution
    symbolic: SymbolicResult
    items: Tuple[PlanItem, ...]
    already_done: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    excluded_vendor_ids: Tuple[str, ...] = ()


class PlanningResult(_Model):
    draft: Optional[PlanDraft] = None
    violations: Tuple[ConstraintViolation, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "PlanningResult":
        if (self.draft is None) == (not self.violations):
            raise ValueError("exactly one of draft or violations")
        return self


# --------------------------------------------------------------- trade-offs


class TradeoffKind(str, Enum):
    REBALANCE_CAPS = "rebalance_caps"
    DROP_SERVICE = "drop_service"
    SHIFT_DATE = "shift_date"
    RAISE_BUDGET = "raise_budget"


class TradeoffOption(_Model):
    """A verified way out of a failed plan. Proposing it changes nothing; the user applies it."""

    option_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    kind: TradeoffKind
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(min_length=1, max_length=1000)
    request: EventRequest
    preference_harm: float = Field(ge=0.0, le=1.0)
    extra_spend: int = Field(ge=0)
    utility: float
    estimated_total: int = Field(ge=0)
    verified: Literal[True] = True  # a full plan was built and validated (dry run, nothing stored)
    source: str = "tool:tradeoff"


class TradeoffReport(_Model):
    status: Literal["not_needed", "options", "no_option"]
    request: EventRequest
    options: Tuple[TradeoffOption, ...] = ()
    violations: Tuple[ConstraintViolation, ...] = ()
    decision_entry_ids: Tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "TradeoffReport":
        if (self.status == "options") != bool(self.options):
            raise ValueError("options present iff status is options")
        if self.status == "no_option" and not self.violations:
            raise ValueError("no_option must say which constraint blocks every repair")
        return self


IntakeResult.model_rebuild()  # resolves the forward reference to GuestSummary


# ----------------------------------------------------------------- budget ledger
# Money the user reports. EventMate never moves money; every entry is `source=user_input`.


class LedgerKind(str, Enum):
    QUOTE = "quote"  # the vendor's current price; the latest quote per category wins
    PAYMENT = "payment"
    REFUND = "refund"


class LedgerEntry(_Model):
    entry_id: str
    timestamp: datetime
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    kind: LedgerKind
    category: VendorCategory
    amount: int = Field(gt=0, le=MAX_BUDGET_INR)
    note: str = Field(default="", max_length=200)
    source: Literal["user_input"] = "user_input"


class LedgerLine(_Model):
    category: VendorCategory
    vendor_name: str
    planned: int = Field(ge=0)
    quoted: Optional[int] = Field(default=None, ge=0)
    committed: int = Field(ge=0)  # the quote if there is one, else the planned cost
    paid: int = Field(ge=0)
    pending: int = Field(ge=0)
    overpaid: int = Field(ge=0)
    variance: int  # committed - planned; positive = costs more than planned


class LedgerSummary(_Model):
    plan_id: str
    plan_version: int = Field(ge=1)
    budget_total: int = Field(gt=0)
    lines: Tuple[LedgerLine, ...]
    planned_total: int = Field(ge=0)
    quoted_total: int = Field(ge=0)
    committed_total: int = Field(ge=0)
    paid_total: int = Field(ge=0)
    pending_total: int = Field(ge=0)
    variance_total: int
    headroom: int  # budget_total - committed_total; negative = over budget
    # net paid to categories that are no longer in the latest plan (money is never silently dropped)
    orphaned_paid: Dict[str, int] = {}


# ------------------------------------------------------------ observation ledger
# Real vendor data with provenance (ADR-006, ADR-025). Text is data: control characters are refused.

MAX_OBSERVATION_ITEMS = 30
MAX_HIDDEN_COSTS = 20


def _clean_text(v: str) -> str:
    if any(unicodedata.category(ch) == "Cc" for ch in v):
        raise ValueError("control characters are not allowed")
    return v


class ObservationSource(str, Enum):
    USER_INPUT = "user_input"
    VENDOR_QUOTE = "vendor_quote"
    VENDOR_CONFIRMATION = "vendor_confirmation"
    PUBLIC_LISTING = "public_listing"


class HiddenCost(_Model):
    label: str = Field(min_length=1, max_length=80)
    amount_inr: Optional[int] = Field(default=None, gt=0, le=MAX_BUDGET_INR)  # None = the vendor named it but not the price

    _label_ok = field_validator("label")(lambda cls, v: _clean_text(v))


class QuoteObservation(_Model):
    observation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    vendor_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    category: VendorCategory
    source_type: ObservationSource
    entered_by: str = Field(min_length=1, max_length=80)
    observed_at: datetime
    expires_at: date
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ref: str = Field(default="", max_length=200)
    amount_inr: int = Field(gt=0, le=MAX_BUDGET_INR)
    basis: Literal["total", "per_plate"]
    guest_count: Optional[int] = Field(default=None, ge=1, le=MAX_GUESTS)
    inclusions: Tuple[str, ...] = Field(default=(), max_length=MAX_OBSERVATION_ITEMS)
    exclusions: Tuple[str, ...] = Field(default=(), max_length=MAX_OBSERVATION_ITEMS)
    hidden_costs: Tuple[HiddenCost, ...] = Field(default=(), max_length=MAX_HIDDEN_COSTS)
    note: str = Field(default="", max_length=200)

    @field_validator("entered_by", "evidence_ref", "note")
    @classmethod
    def _text_ok(cls, v: str) -> str:
        return _clean_text(v)

    @field_validator("inclusions", "exclusions")
    @classmethod
    def _items_ok(cls, v: Tuple[str, ...]) -> Tuple[str, ...]:
        for item in v:
            if not 1 <= len(item) <= 80:
                raise ValueError("each item must be 1-80 characters")
            _clean_text(item)
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "QuoteObservation":
        if self.source_type in (ObservationSource.VENDOR_QUOTE, ObservationSource.VENDOR_CONFIRMATION) and not self.evidence_ref.strip():
            raise ValueError("evidence_ref is required when the vendor is the source")
        if self.basis == "per_plate" and self.guest_count is None:
            raise ValueError("guest_count is required for a per_plate quote")
        if self.basis == "total" and self.guest_count is not None:
            raise ValueError("guest_count only applies to a per_plate quote")
        if self.expires_at < self.observed_at.date():
            raise ValueError("expires_at is before observed_at")
        return self


class QuoteReport(_Model):
    vendor_id: str
    category: VendorCategory
    observation_id: str
    source_type: ObservationSource
    confidence: float
    expires_at: date
    status: Literal["valid", "expiring", "expired"]
    quoted_total: int = Field(ge=0)
    known_hidden_total: int = Field(ge=0)
    effective_total: int = Field(ge=0)
    unquantified_hidden: Tuple[str, ...] = ()
    missing_items: Tuple[str, ...] = ()
    excluded_items: Tuple[str, ...] = ()
    flags: Tuple[str, ...] = ()


class QuoteComparison(_Model):
    category: VendorCategory
    ranked: Tuple[QuoteReport, ...] = ()
    expired: Tuple[QuoteReport, ...] = ()


class ScoreComponent(_Model):
    name: Literal["price", "completeness", "evidence"]
    weight: float = Field(ge=0.0, le=1.0)
    score: Optional[int] = Field(default=None, ge=0, le=100)  # None = not enough data; the component is left out, never guessed
    detail: str = Field(min_length=1, max_length=300)


class VendorScore(_Model):
    vendor_id: str
    category: VendorCategory
    observation_id: str
    status: Literal["valid", "expiring"]
    overall: Optional[int] = Field(default=None, ge=0, le=100)
    coverage: float = Field(ge=0.0, le=1.0)  # share of the formula's weight that had data
    components: Tuple[ScoreComponent, ...]
    not_scored: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()


class UnscoredVendor(_Model):
    vendor_id: str
    reason: str


class VendorScoreboard(_Model):
    category: VendorCategory
    scores: Tuple[VendorScore, ...] = ()
    unscored: Tuple[UnscoredVendor, ...] = ()


class SelectionQuoteStatus(_Model):
    """Per selection in a plan: what the plan paid, what the vendor's price is now, and who confirmed the booking."""

    category: VendorCategory
    vendor_id: str
    vendor_name: str
    booking_status: BookingStatus
    plan_cost: int = Field(ge=0)
    current_price: int = Field(ge=0)
    price_stale: bool  # a later quote (or expiry) means the plan was priced differently; replan to re-price
    confirmed_by: Optional[Literal["you", "vendor"]] = None
    report: Optional[QuoteReport] = None


# ------------------------------------------------------------------ travel
# Flight fares and schedules from an external tool (ADR-028). Indicative only; EventMate never books or pays.

_IATA = r"^[A-Z]{3}$"
GOOGLE_FLIGHTS_PREFIX = "https://www.google.com/travel/flights"
MAX_FLIGHT_OPTIONS = 30
MAX_MULTI_CITY_LEGS = 6


class FlightSegmentInfo(_Model):
    airline_code: str = Field(min_length=2, max_length=3, pattern=r"^[A-Z0-9]{2,3}$")
    airline_name: str = Field(min_length=1, max_length=80)
    flight_number: str = Field(min_length=1, max_length=8, pattern=r"^[A-Za-z0-9]{1,8}$")
    from_code: str = Field(pattern=_IATA)
    to_code: str = Field(pattern=_IATA)
    departs: datetime  # local time at the departure airport, as published
    arrives: datetime  # local time at the arrival airport
    duration_min: int = Field(gt=0, le=60 * 72)

    @field_validator("airline_name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        return _clean_text(v)


class FlightOption(_Model):
    price_inr: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET_INR)  # None = no INR price was published
    duration_min: int = Field(gt=0, le=60 * 100)
    stops: int = Field(ge=0, le=5)
    segments: Tuple[FlightSegmentInfo, ...] = Field(min_length=1, max_length=8)
    booking_url: Optional[str] = Field(default=None, max_length=2000)
    source: str

    @field_validator("source")
    @classmethod
    def _source_ok(cls, v: str) -> str:
        if not SOURCE_PATTERN.fullmatch(v):
            raise ValueError("invalid source tag")
        return v

    @field_validator("booking_url")
    @classmethod
    def _url_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.startswith(GOOGLE_FLIGHTS_PREFIX) or any(ord(c) < 33 or ord(c) > 126 for c in v):
            raise ValueError("booking_url must be a plain https://www.google.com/travel/flights link")
        return v


class FlightSearch(_Model):
    origin: str = Field(pattern=_IATA)
    destination: str = Field(pattern=_IATA)
    travel_date: date
    options: Tuple[FlightOption, ...] = Field(default=(), max_length=MAX_FLIGHT_OPTIONS)
    fetched_at: datetime
    source: str
    warnings: Tuple[str, ...] = ()
    error: Optional[str] = Field(default=None, max_length=300)  # set when this leg could not be searched
    notice: str = "Indicative fare and schedule from Google Flights through fli. Fares change; EventMate does not book or pay."


class MultiCityResult(_Model):
    legs: Tuple[FlightSearch, ...] = Field(min_length=2, max_length=MAX_MULTI_CITY_LEGS)
    method: Literal["separate_one_way_searches"] = "separate_one_way_searches"
    total_cheapest_inr: Optional[int] = Field(default=None, ge=0)  # None if any leg has no priced option
    incomplete_legs: Tuple[int, ...] = ()
    note: str


# ------------------------------------------------------------ venue discovery
# Candidates from OpenStreetMap (ADR-028). They carry no price or availability and never enter the vendor database.

OSM_ATTRIBUTION = "Data (c) OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)."


class VenueCandidate(_Model):
    osm_ref: str = Field(pattern=r"^(node|way|relation)/[0-9]{1,15}$")
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["venue", "hotel", "restaurant"]
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    street: Optional[str] = Field(default=None, max_length=120)
    city: Optional[str] = Field(default=None, max_length=80)
    website: Optional[str] = Field(default=None, max_length=300)
    capacity_tagged: Optional[int] = Field(default=None, ge=1, le=MAX_GUESTS)  # as tagged by an OSM contributor, unverified
    source: Literal["tool:overpass"] = "tool:overpass"

    @field_validator("name", "street", "city")
    @classmethod
    def _text_ok(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _clean_text(v)

    @field_validator("website")
    @classmethod
    def _site_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and (not re.fullmatch(r"https?://[^\s<>\"']+", v)):
            raise ValueError("website must be a plain http(s) URL")
        return v


class DiscoveryResult(_Model):
    city: str = Field(min_length=1, max_length=60)
    kind: Literal["venue", "hotel", "restaurant"]
    candidates: Tuple[VenueCandidate, ...] = ()
    fetched_at: datetime
    source: Literal["tool:overpass"] = "tool:overpass"
    attribution: str = OSM_ATTRIBUTION
    warnings: Tuple[str, ...] = ()
    notice: str = "Places other people have mapped. No prices, availability or quality are implied; ask the venue before relying on anything here."


# ---------------------------------------------------------------------- weather
# Forecast from OpenWeather (ADR-029). Only what the service returned; nothing is estimated beyond its horizon.

RAIN_POSSIBLE_AT = 0.3


class WeatherSlot(_Model):
    local_time: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")  # local time at the place
    temp_c: float = Field(ge=-90.0, le=60.0)
    feels_like_c: float = Field(ge=-90.0, le=70.0)
    humidity_pct: Optional[int] = Field(default=None, ge=0, le=100)
    rain_chance: float = Field(ge=0.0, le=1.0)
    wind_ms: Optional[float] = Field(default=None, ge=0.0, le=120.0)
    description: Optional[str] = Field(default=None, max_length=80)

    @field_validator("description")
    @classmethod
    def _desc_ok(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _clean_text(v)


class WeatherDay(_Model):
    city: str = Field(min_length=1, max_length=60)
    travel_date: date
    resolved_place: str = Field(max_length=200)
    slots: Tuple[WeatherSlot, ...] = ()
    temp_min: Optional[float] = None
    temp_max: Optional[float] = None
    max_rain_chance: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    rain_possible: Optional[bool] = None
    conditions: Optional[str] = Field(default=None, max_length=80)
    note: Optional[str] = Field(default=None, max_length=300)
    warnings: Tuple[str, ...] = ()
    fetched_at: datetime
    source: Literal["tool:openweathermap"] = "tool:openweathermap"
    attribution: str = "Weather data by OpenWeather (https://openweathermap.org)."


# ------------------------------------------------------------- multi-city trip search
# The system orders the stops, picks dates and searches each distinct leg (ADR-030). Prices come only from the flight tool.

MAX_TRIP_STOPS = 4
MAX_TRIP_SEARCHES = 60


class TripStop(_Model):
    code: str = Field(pattern=_IATA)
    nights: int = Field(ge=1, le=30)


class TripRequest(_Model):
    origin: str = Field(pattern=_IATA)
    stops: Tuple[TripStop, ...] = Field(min_length=1, max_length=MAX_TRIP_STOPS)
    return_to: Optional[str] = Field(default=None, pattern=_IATA)  # default: back to the origin
    earliest_start: date
    flex_days: int = Field(default=0, ge=0, le=3)  # try starting up to this many days later
    adults: int = Field(default=1, ge=1, le=9)
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    max_stops: Optional[int] = Field(default=None, ge=0, le=2)
    fixed_order: bool = False  # True = visit the stops in the order given
    max_searches: int = Field(default=30, ge=1, le=MAX_TRIP_SEARCHES)
    value_per_hour_inr: int = Field(default=300, ge=0, le=5000)  # what an hour in the air is worth to the traveller; an assumption, not data

    @model_validator(mode="after")
    def _consistent(self) -> "TripRequest":
        codes = [s.code for s in self.stops]
        if len(set(codes)) != len(codes):
            raise ValueError("a stop appears twice")
        if self.origin in codes or (self.return_to and self.return_to in codes):
            raise ValueError("the origin and return airport cannot also be stops")
        return self


class TripLeg(_Model):
    origin: str = Field(pattern=_IATA)
    destination: str = Field(pattern=_IATA)
    date: date
    option: FlightOption


class TripPackage(_Model):
    label: Literal["cheapest", "fastest", "best_value", "alternative"]
    order: Tuple[str, ...]
    start_date: date
    legs: Tuple[TripLeg, ...] = Field(min_length=2)
    total_price_inr: int = Field(ge=0)
    total_flight_minutes: int = Field(gt=0)
    value_score_inr: int = Field(ge=0)  # price plus the traveller's time valued at value_per_hour_inr; lower is better
    below_median_inr: int  # how far under the median complete itinerary's cheapest total this is (negative = above)


class TripSearchResult(_Model):
    request: TripRequest
    packages: Tuple[TripPackage, ...] = ()
    searches_planned: int = Field(ge=0)
    searches_used: int = Field(ge=0)
    orderings_considered: int = Field(ge=0)
    dropped_orders: int = Field(default=0, ge=0)
    insights: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    fetched_at: datetime
    source: Literal["tool:fli"] = "tool:fli"
    method: Literal["separate_one_way_searches"] = "separate_one_way_searches"
    note: str
    notice: str = "Indicative fares from Google Flights through fli. Fares change; EventMate does not book or pay."


# ------------------------------------------------------------------------- deals
# Advertised prices found on public web pages (ADR-031). They are leads, never quotes, and never set a plan's price.

DEAL_KINDS = ("caterer", "venue", "photographer", "hotel")


class DealQuery(_Model):
    kind: Literal["caterer", "venue", "photographer", "hotel"]
    city: str = Field(min_length=1, max_length=60)
    guests: Optional[int] = Field(default=None, ge=1, le=MAX_GUESTS)
    nights: Optional[int] = Field(default=None, ge=1, le=60)  # hotels: turns a nightly price into a total
    budget_inr: Optional[int] = Field(default=None, gt=0, le=MAX_BUDGET_INR)
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("city")
    @classmethod
    def _city_ok(cls, v: str) -> str:
        return _clean_text(v)


class DealOffer(_Model):
    rank: int = Field(ge=1)
    title: str = Field(max_length=200)
    label: str = Field(max_length=100)
    url: str = Field(max_length=2048, pattern=r"^https?://\S+$")
    domain: str = Field(max_length=253)
    unit: Literal["per_plate", "per_night", "per_day", "per_hour", "per_event"]
    unit_assumed: bool = False  # the page gave no unit; the service's usual one was assumed
    low_inr: int = Field(gt=0, le=MAX_BUDGET_INR)
    high_inr: int = Field(gt=0, le=MAX_BUDGET_INR)
    est_total_low_inr: Optional[int] = Field(default=None, ge=0)
    est_total_high_inr: Optional[int] = Field(default=None, ge=0)
    within_budget: Optional[bool] = None
    vs_market_pct: int  # advertised low against the median low found: negative = cheaper than typical
    corroborated_by: int = Field(ge=0)  # other sites advertising a similar entry price (within 25 percent)
    flags: Tuple[str, ...] = ()  # cautions computed in code, e.g. unusually low or a wide range
    snippet: str = Field(max_length=200)
    source: str

    @field_validator("source")
    @classmethod
    def _source_ok(cls, v: str) -> str:
        if not SOURCE_PATTERN.fullmatch(v):
            raise ValueError("invalid source tag")
        return v


class MarketSummary(_Model):
    unit: Literal["per_plate", "per_night", "per_day", "per_hour", "per_event"]
    median_low_inr: int = Field(ge=0)
    median_high_inr: int = Field(ge=0)
    n_offers: int = Field(ge=0)
    n_domains: int = Field(ge=0)


class SkippedPage(_Model):
    url: str = Field(max_length=2048)
    reason: str = Field(max_length=200)


class DealReport(_Model):
    query: DealQuery
    offers: Tuple[DealOffer, ...] = ()
    market: Optional[MarketSummary] = None
    pages_read: int = Field(ge=0)
    skipped: Tuple[SkippedPage, ...] = ()
    queries_run: Tuple[str, ...] = ()
    provider: str = Field(max_length=40)
    warnings: Tuple[str, ...] = ()
    fetched_at: datetime
    notice: str = (
        "Advertised prices found on public web pages: often 'starting at' figures, not a quote, and not checked with the vendor. "
        "Ask for a written quote before relying on any of them."
    )


# ------------------------------------------------------------------- places
# Ratings and reviews from Google Places (ADR-033). A price level is a coarse band, never a price.

GOOGLE_MAPS_PREFIXES = ("https://maps.google.com/", "https://www.google.com/maps", "https://maps.app.goo.gl/", "https://goo.gl/maps/")


class PlaceReview(_Model):
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    text: str = Field(max_length=300)
    when: Optional[str] = Field(default=None, max_length=60)


class PlaceInfo(_Model):
    name: str = Field(min_length=1, max_length=120)
    address: Optional[str] = Field(default=None, max_length=200)
    rating: Optional[float] = Field(default=None, ge=0.0, le=5.0)
    rating_count: Optional[int] = Field(default=None, ge=0)
    weighted_rating: Optional[float] = Field(default=None, ge=0.0, le=5.0)  # rating pulled toward the average when there are few reviews
    price_level: Optional[int] = Field(default=None, ge=1, le=4)  # 1 inexpensive ... 4 very expensive: a band, not a price
    maps_uri: Optional[str] = Field(default=None, max_length=500)
    reviews: Tuple[PlaceReview, ...] = Field(default=(), max_length=10)

    @field_validator("name", "address")
    @classmethod
    def _text_ok(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else _clean_text(v)

    @field_validator("maps_uri")
    @classmethod
    def _uri_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and (not v.startswith(GOOGLE_MAPS_PREFIXES) or any(ord(c) <= 32 or ord(c) == 127 for c in v)):
            raise ValueError("maps_uri must be a Google Maps link")
        return v


class PlaceSearch(_Model):
    kind: Literal["caterer", "venue", "photographer", "hotel"]
    city: str = Field(min_length=1, max_length=60)
    places: Tuple[PlaceInfo, ...] = ()
    fetched_at: datetime
    source: Literal["tool:google_places"] = "tool:google_places"
    attribution: str = "Ratings and reviews from Google Maps."
    warnings: Tuple[str, ...] = ()
    notice: str = "Ratings and reviews are other people's opinions. A price level is a coarse band, not a price; ask the vendor for a quote."


# ------------------------------------------------------------------- saved leads
# A bookmark of an advertised price the user found (ADR-038). Never a quote: it does not price a plan or enter the ledger.


class DealLead(_Model):
    lead_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    saved_at: datetime
    kind: Literal["caterer", "venue", "photographer", "hotel"]
    url: str = Field(max_length=2048, pattern=r"^https?://[^\s\x00-\x1f\x7f]+$")
    domain: str = Field(min_length=1, max_length=253)
    label: str = Field(max_length=100)
    unit: Literal["per_plate", "per_night", "per_day", "per_hour", "per_event"]
    low_inr: int = Field(gt=0, le=MAX_BUDGET_INR)
    high_inr: int = Field(gt=0, le=MAX_BUDGET_INR)
    note: str = Field(default="", max_length=200)
    source: Literal["user_input"] = "user_input"
    notice: str = "Saved by you from a public page: an advertised price, not a quote, and not checked with the vendor."

    @field_validator("domain", "label", "note")
    @classmethod
    def _text_ok(cls, v: str) -> str:
        return _clean_text(v)

    @model_validator(mode="after")
    def _range_ok(self) -> "DealLead":
        if self.low_inr > self.high_inr:
            raise ValueError("low_inr is above high_inr")
        return self


# ----------------------------------------------------------------- weather advice
# Plain-code suggestions from a forecast and the chosen venue's setting (ADR-039). Advice only: it never changes a plan.


class AdviceItem(_Model):
    level: Literal["ok", "info", "warn"]
    title: str = Field(max_length=100)
    detail: str = Field(max_length=400)


class WeatherAdvice(_Model):
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    venue_setting: Optional[Literal["indoor", "outdoor"]] = None
    items: Tuple[AdviceItem, ...] = Field(min_length=1)
    forecast: WeatherDay
    source: Literal["rule:weather_advice"] = "rule:weather_advice"


# ----------------------------------------------------------------- outreach and conversations
# A quote request EventMate drafts, the user approves, and the agent sends and follows (ADR-041, ADR-042). Content is bound to a hash
# at proposal time; approval is for that exact content. Replies are stored as untrusted text with a rule-based analysis, never as facts.

_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}$")
_LINK = re.compile(r"^(mailto:[A-Za-z0-9._%+@-]+\?[^\s]*|https://wa\.me/[1-9][0-9]{7,14}\?text=[^\s]*)$")
OutreachStatus = Literal["prepared", "approved", "sending", "sent", "failed", "outcome_unknown", "denied", "expired", "marked_sent"]
OUTREACH_FINAL = frozenset({"sent", "failed", "outcome_unknown", "denied", "expired", "marked_sent"})


def draft_content_hash(d: Dict) -> str:
    """What an approval is bound to: where it goes, what it says, and which thread it belongs to."""
    import hashlib
    import json as _json

    parts = [d.get(k) for k in ("plan_id", "vendor_id", "channel", "recipient", "subject", "body")] + [d.get("kind") or "request", d.get("parent_id") or None]
    return hashlib.sha256(_json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class OutreachDraft(_Model):
    draft_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    created_at: datetime
    category: VendorCategory
    vendor_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    vendor_name: str = Field(min_length=1, max_length=120)
    channel: Literal["email", "whatsapp"]
    recipient: str = Field(max_length=254)  # an email address, or WhatsApp digits with the country code
    subject: str = Field(default="", max_length=200)  # email only
    body: str = Field(min_length=1, max_length=2000)
    link: str = Field(max_length=4000)  # manual fallback: open in your own app
    kind: Literal["request", "followup"] = "request"
    parent_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")  # the request a follow-up continues
    content_hash: str = Field(default="", max_length=64)
    expires_at: Optional[datetime] = None  # a proposal nobody approved by then is dead
    status: OutreachStatus = "prepared"
    marked_at: Optional[datetime] = None
    decided_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    external_id: str = Field(default="", max_length=400)  # Message-ID or WhatsApp message id of what was sent
    error: str = Field(default="", max_length=300)
    action_key: str = Field(default="", max_length=200)  # who claimed this outbound action, e.g. "reply:<message_id>" or "nudge:<job>:<vendor>:<n>"; one live draft per key
    approved_by: Literal["user", "mandate"] = "user"  # "mandate": sent under the user's standing permission (ADR-043), not a per-message approval
    source: Literal["user_input"] = "user_input"
    notice: str = "Not sent until you approve it. After that EventMate sends it through the channel you configured and reads the vendor's reply."

    @model_validator(mode="before")
    @classmethod
    def _bind_hash(cls, data):
        if isinstance(data, dict):
            want = draft_content_hash(data)
            if not data.get("content_hash"):
                data = {**data, "content_hash": want}
            elif data["content_hash"] != want:
                raise ValueError("content_hash does not match the content: the record was changed")
        return data

    @field_validator("vendor_name", "subject", "error")
    @classmethod
    def _text_ok(cls, v: str) -> str:
        return _clean_text(v)

    @field_validator("body")
    @classmethod
    def _body_ok(cls, v: str) -> str:
        if any(unicodedata.category(ch) == "Cc" and ch != "\n" for ch in v):
            raise ValueError("control characters are not allowed")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "OutreachDraft":
        if not _LINK.fullmatch(self.link):
            raise ValueError("link must be a mailto: or https://wa.me/ link")
        if self.channel == "email":
            if not _EMAIL.fullmatch(self.recipient) or not self.link.startswith("mailto:"):
                raise ValueError("an email draft needs a valid address and a mailto: link")
        elif not re.fullmatch(r"[1-9][0-9]{7,14}", self.recipient) or not self.link.startswith(f"https://wa.me/{self.recipient}?text="):
            raise ValueError("a WhatsApp draft needs digits with the country code and a wa.me link to that number")
        if (self.status == "marked_sent") != (self.marked_at is not None):
            raise ValueError("marked_at is set exactly when the status is marked_sent")
        if self.status == "sent" and self.sent_at is None:
            raise ValueError("a sent draft needs sent_at")
        if (self.kind == "followup") != (self.parent_id is not None):
            raise ValueError("a follow-up needs parent_id and a request must not have one")
        return self


class VendorContact(_Model):
    """Where to reach a vendor. Typed by the user; vendor records hold no contact details."""

    vendor_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    email: Optional[str] = Field(default=None, max_length=254)
    whatsapp: Optional[str] = Field(default=None, pattern=r"^[1-9][0-9]{7,14}$")
    preferred: Literal["email", "whatsapp"] = "whatsapp"
    updated_at: datetime

    @model_validator(mode="after")
    def _ok(self) -> "VendorContact":
        if self.email is None and self.whatsapp is None:
            raise ValueError("give an email address or a WhatsApp number")
        if self.email is not None and not _EMAIL.fullmatch(self.email):
            raise ValueError("not a valid email address")
        if getattr(self, self.preferred) is None:
            raise ValueError(f"preferred channel {self.preferred} has no contact")
        return self


class AmountMention(_Model):
    low_inr: int = Field(gt=0, le=MAX_BUDGET_INR)
    high_inr: int = Field(gt=0, le=MAX_BUDGET_INR)
    unit: str = Field(max_length=20)  # as written by the vendor, or "unspecified"
    snippet: str = Field(max_length=200)


class ReplyAnalysis(_Model):
    """Rule-based reading of a reply. Every item is a quotation of the vendor's own words; nothing here is a recorded quote."""

    amounts: Tuple[AmountMention, ...] = Field(default=(), max_length=5)
    says_unavailable: bool = False
    says_available: bool = False
    questions: Tuple[str, ...] = Field(default=(), max_length=5)  # sentences the vendor asked you
    asks_to_stop: bool = False  # "stop", "do not contact me", "wrong number": nothing automatic is ever sent after this
    checklist_mentioned: Tuple[str, ...] = ()
    source: Literal["rule:reply_analysis"] = "rule:reply_analysis"


class InboundMessage(_Model):
    message_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    draft_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")  # the request this answers
    vendor_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    vendor_name: str = Field(min_length=1, max_length=120)
    category: VendorCategory
    channel: Literal["email", "whatsapp"]
    sender: str = Field(max_length=254)
    received_at: datetime
    external_id: str = Field(min_length=1, max_length=400)
    body: str = Field(max_length=4000)
    truncated: bool = False
    attachments: int = Field(default=0, ge=0, le=100)  # counted, never opened
    analysis: ReplyAnalysis = ReplyAnalysis()
    read: bool = False
    auto_handled: bool = False  # the standing permission has already looked at this reply (answered it or handed it to the user)
    notice: str = "The vendor's own words, shown as text. Nothing in a reply changes a plan or records a quote until you do."

    @field_validator("vendor_name", "sender")
    @classmethod
    def _text_ok(cls, v: str) -> str:
        return _clean_text(v)

    @field_validator("body")
    @classmethod
    def _body_ok(cls, v: str) -> str:
        return "".join(ch for ch in v if ch in "\n\t" or unicodedata.category(ch) != "Cc")


class ChannelStatus(_Model):
    channel: Literal["email", "whatsapp"]
    can_send: bool
    can_receive: bool
    detail: str = Field(max_length=300)  # what is missing; never a secret


class ConversationThread(_Model):
    """One request to one vendor, everything sent on it, and everything the vendor answered."""

    request: OutreachDraft
    drafts: Tuple[OutreachDraft, ...]  # the request first, then follow-ups, oldest first
    replies: Tuple[InboundMessage, ...] = ()
    unread: int = Field(default=0, ge=0)
    awaiting_approval: int = Field(default=0, ge=0)


class Inbox(_Model):
    plan_id: str
    threads: Tuple[ConversationThread, ...] = ()
    unread_total: int = Field(default=0, ge=0)
    approvals_total: int = Field(default=0, ge=0)
    channels: Tuple[ChannelStatus, ...] = ()


class PollResult(_Model):
    new_messages: int = Field(ge=0)
    ignored: int = Field(ge=0)  # mail from strangers or replies already stored
    sent: int = Field(default=0, ge=0)  # approved drafts dispatched in this round
    waiting: Tuple[str, ...] = ()  # why approved drafts have not gone out yet
    errors: Tuple[str, ...] = ()


MANDATE_MAX_HOURS = 168


class Mandate(_Model):
    """The user's standing permission for the agent to ask routine follow-up questions in a plan's vendor threads without approving each one (ADR-043).

    Narrow by design: fixed questions about missing information only. Bounded in time, count and hours of the day, and revocable at any moment.
    """

    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    sender_name: str = Field(min_length=1, max_length=80)
    granted_at: datetime
    expires_at: datetime
    max_per_thread: int = Field(default=3, ge=1, le=5)
    max_total: int = Field(default=10, ge=1, le=30)
    window_start: int = Field(default=9, ge=0, le=23)  # hours in IST; automatic messages wait outside [start, end)
    window_end: int = Field(default=20, ge=1, le=24)
    revoked_at: Optional[datetime] = None

    @field_validator("sender_name")
    @classmethod
    def _name_ok(cls, v: str) -> str:
        return _clean_text(v)

    @model_validator(mode="after")
    def _bounds(self) -> "Mandate":
        if not self.granted_at < self.expires_at <= self.granted_at + timedelta(hours=MANDATE_MAX_HOURS):
            raise ValueError(f"the permission must end after it starts and within {MANDATE_MAX_HOURS} hours")
        if self.window_start >= self.window_end:
            raise ValueError("window_start must be before window_end")
        return self

    def active_at(self, now: datetime) -> bool:
        return self.revoked_at is None and self.granted_at <= now < self.expires_at


class AutoreplyStatus(_Model):
    plan_id: str
    active: bool  # a permission exists, is not revoked or expired, and the global kill switch is off
    killed: bool  # the global kill switch is on
    mandate: Optional[Mandate] = None
    sent_total: int = Field(default=0, ge=0)  # automatic messages queued or sent under this plan's permission

