"""Manager Agent: parses requests, decomposes them into a task graph, runs tasks.

Parsing is deterministic (regex + the known-city list from the database). If a
required fact is missing or ambiguous the parser raises ``RequestParseError``
instead of guessing: facts are never invented. The task graph is checked
against the three AOP principles (solvability, completeness, non-redundancy)
plus necessity (every task must feed the final explanation).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import ValidationError

from eventmate.planning.community import load_profiles, match_subtype
from eventmate.agents.budget import BudgetAgent
from eventmate.agents.guest import GuestAgent, GuestRepository
from eventmate.agents.vendor import VendorAgent, VendorRepository
from eventmate.arcade_pulse import pulse as _arcade_pulse
from eventmate.decision_logger import DecisionLogger
from eventmate.pricing.reality import reality_check
from eventmate.schemas import (
    MAX_BUDGET_INR,
    MAX_GUESTS,
    MAX_ORGANIZERS,
    AgentName,
    ConstraintCheck,
    ConstraintViolation,
    DecisionType,
    DietaryRequirement,
    EventRequest,
    CommunityDetails,
    EventType,
    GuestSummary,
    IntakeResult,
    PlanOutcome,
    ReminderKind,
    Task,
    TaskGraph,
    TaskKind,
    TaskStatus,
    VendorCategory,
    VenueSetting,
)

MAX_TEXT_CHARS = 2000
MAX_EVENT_AHEAD_DAYS = 3 * 366  # beyond this the schedule horizon and vendor data are meaningless


class RequestParseError(ValueError):
    """The request is missing required facts or is ambiguous."""

    def __init__(self, problems: Sequence[str]):
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


# ------------------------------------------------------------------ parsing

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_YEAR = r"(?:,?\s+(20\d{2})(?!\d))?"
_DATE_MD = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*:){_YEAR}", re.I | re.A)
_DATE_DM = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b{_YEAR}", re.I | re.A)
_DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b", re.A)
# 12/12/2026, 12-12-2026, 12.12.2026: day first (Indian convention), 4-digit year required, same separator twice
_DATE_NUMERIC = re.compile(r"(?<![\d./-])(\d{1,2})([/.-])(\d{1,2})\2(20\d{2})(?![\d/.-])", re.A)
_DIGIT_RUN = re.compile(r"\d+")  # unicode \d: any decimal digit, including Devanagari, Bengali, Arabic-Indic

_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MONEY = re.compile(
    rf"(?:budget(?:\s+(?:of|is))?\s*:?\s*)?(?:(?P<cur>inr|rs\.?|₹|rupees)\s*)?(?<![\d,.])(?P<num>{_NUM})"
    r"(?:\s*(?P<unit>lakhs?|lacs?|crores?|cr|k|l)\b)?(?:\s*(?P<cur2>inr|rs|rupees)\b)?",
    re.I,
)
_ORGANIZERS = re.compile(r"(?<![\d,.])(\d{1,3})\s*-?\s*(?:organi[sz]ers?|coordinators?|planners?)\b", re.I | re.A)
_BUDGET_WORD = re.compile(r"budget", re.I | re.A)
_UNIT = {"k": 1_000, "lakh": 100_000, "lac": 100_000, "crore": 10_000_000, "cr": 10_000_000, "l": 100_000}

_GUESTS = [
    re.compile(r"(?<![\d,.])(\d{1,12})\s*-?\s*(?:person|persons|people|guests?|pax|attendees?)\b", re.I | re.A),
    re.compile(r"\bguests?(?:\s+count)?\s*(?:of|:|=)\s*(\d{1,12})\b", re.I | re.A),
]
# Used only when no explicit "guests / people / pax" count exists, so "2 kids and 40 guests" is never read as a conflict.
_GUESTS_GROUP = re.compile(r"(?<![\d,.])(\d{1,12})\s*-?\s*(?:friends|relatives|invitees?|colleagues|coworkers|classmates)\b", re.I | re.A)
_CITY_FALLBACK = re.compile(r"\b(?:in|at|near)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\b", re.A)
_NOT_CITIES = frozenset({"rs", "inr", "the", "this", "our", "my", "a", "an"})
_NEGATION = re.compile(r"\b(?:no|not|without|skip|exclude|excluding|never|don't|dont)\b", re.I | re.A)
_CLAUSE_BREAK = re.compile(r"[.;,!?\n]")

_EVENT_TYPES = [
    (re.compile(r"\bengagement\b", re.I | re.A), EventType.ENGAGEMENT_PARTY),
    (re.compile(r"\bwedding\b", re.I | re.A), EventType.WEDDING),
    (re.compile(r"\bbirthday\b", re.I | re.A), EventType.BIRTHDAY),
    (re.compile(r"\bconference\b", re.I | re.A), EventType.CONFERENCE),
    (re.compile(r"\b(?:corporate|offsite|company)\b", re.I | re.A), EventType.CORPORATE),
]
_DIETS = [
    (re.compile(r"\bnon[- ]?veg(?:etarian)?\b", re.I | re.A), DietaryRequirement.NON_VEGETARIAN),
    (re.compile(r"(?<!non-)(?<!non )\bveg(?:etarian)?\b", re.I | re.A), DietaryRequirement.VEGETARIAN),
    (re.compile(r"\bvegan\b", re.I | re.A), DietaryRequirement.VEGAN),
    (re.compile(r"\bjain\b", re.I | re.A), DietaryRequirement.JAIN),
    (re.compile(r"\bhalal\b", re.I | re.A), DietaryRequirement.HALAL),
    (re.compile(r"\bgluten[- ]free\b", re.I | re.A), DietaryRequirement.GLUTEN_FREE),
]
_CATERER = re.compile(r"\b(?:cater(?:er|ers|ing)?|food|meals?)\b", re.I | re.A)
_PHOTO = re.compile(r"\bphoto(?:grapher|graphy|graphers)?\b", re.I | re.A)
_REMINDER = re.compile(r"\breminders?\b", re.I | re.A)
_REM_INVITE = re.compile(r"\binvit(?:e|es|ation|ations)\b", re.I | re.A)
_REM_VENDOR = re.compile(r"\bvendor(?:s)?\s+confirm(?:ation|ations)?\b|\bconfirm(?:ation|ing)?\s+(?:with\s+|of\s+)?vendors?\b", re.I | re.A)
_INDOOR = re.compile(r"\bindoors?\b", re.I | re.A)
_OUTDOOR = re.compile(r"\boutdoors?\b", re.I | re.A)


def _mentioned(rx: "re.Pattern[str]", text: str) -> bool:
    """True if ``rx`` matches somewhere that is not negated ("no", "without", ...) in its clause."""
    for m in rx.finditer(text):
        clause = _CLAUSE_BREAK.split(text[max(0, m.start() - 40) : m.start()])[-1]
        if not _NEGATION.search(clause):
            return True
    return False


# Digit scripts of Indian languages (ADR-005/019). Anything else non-ASCII (e.g. Arabic-Indic) stays unread and fails closed.
_INDIC_DIGITS = ("DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA", "TAMIL", "TELUGU", "KANNADA", "MALAYALAM")


def _is_indic_digit(c: str) -> bool:
    return unicodedata.name(c, "").startswith(_INDIC_DIGITS)


def _parse_organizers(text: str, problems: List[str]) -> int:
    counts = {int(m.group(1)) for m in _ORGANIZERS.finditer(text)}
    if len(counts) > 1:
        problems.append("multiple different organizer counts found: " + ", ".join(str(n) for n in sorted(counts)))
        return 1
    n = next(iter(counts), 1)
    if not 1 <= n <= MAX_ORGANIZERS:
        problems.append(f"organizer count must be between 1 and {MAX_ORGANIZERS}, got {n}")
        return 1
    return n


def _ascii_digits(text: str, problems: List[str], assumptions: List[str]) -> str:
    """Read Indic digits as numbers (locale pack). A single number mixing digit scripts is an error, never a guess."""
    changed = False

    def convert(m: "re.Match[str]") -> str:
        nonlocal changed
        run = m.group(0)
        if run.isascii() or not all(c.isascii() or _is_indic_digit(c) for c in run):
            return run
        if len({ord(c) - unicodedata.digit(c) for c in run}) > 1:
            problems.append(f"a number mixes digit scripts: {run!r}")
            return run
        changed = True
        return "".join(str(unicodedata.digit(c)) for c in run)

    out = _DIGIT_RUN.sub(convert, text)
    if changed:
        assumptions.append("Indic digits were read as their decimal values")
    return out


def _parse_dates(text: str, today: date, problems: List[str], assumptions: List[str]) -> Optional[date]:
    raw: List[Tuple[Optional[int], int, int]] = []
    for m in _DATE_NUMERIC.finditer(text):
        day, month = int(m.group(1)), int(m.group(3))
        if day <= 12 and month <= 12 and day != month:
            problems.append(f"ambiguous date {m.group(0)!r} (day-first or month-first); write it as 2026-12-05 or 5 December 2026")
            return None
        raw.append((int(m.group(4)), month, day))
    for m in _DATE_MD.finditer(text):
        raw.append((int(m.group(3)) if m.group(3) else None, _MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
    for m in _DATE_DM.finditer(text):
        raw.append((int(m.group(3)) if m.group(3) else None, _MONTHS[m.group(2)[:3].lower()], int(m.group(1))))
    for m in _DATE_ISO.finditer(text):
        raw.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    found = set()
    for year, month, day in raw:
        if year is not None:
            try:
                d = date(year, month, day)
            except ValueError:
                problems.append(f"invalid calendar date {year}-{month:02d}-{day:02d}")
                return None
            if d < today:
                problems.append(f"event date {d.isoformat()} is in the past")
                return None
            found.add(d)
        else:
            d = next(
                (date(y, month, day) for y in range(today.year, today.year + 9) if _valid(y, month, day) and date(y, month, day) >= today),
                None,
            )
            if d is None:
                problems.append(f"invalid calendar date {month:02d}-{day:02d}")
                return None
            found.add(d)
            assumptions.append(f"year not given; assumed next occurrence {d.isoformat()}")
    if not found:
        problems.append("event date not found")
        return None
    if len(found) > 1:
        problems.append("multiple different event dates found: " + ", ".join(sorted(d.isoformat() for d in found)))
        return None
    chosen = next(iter(found))
    if (chosen - today).days > MAX_EVENT_AHEAD_DAYS:
        problems.append(f"event date {chosen.isoformat()} is more than 3 years away")
        return None
    return chosen


def _valid(y: int, m: int, d: int) -> bool:
    try:
        date(y, m, d)
        return True
    except ValueError:
        return False


def _parse_budget(text: str, problems: List[str]) -> Optional[int]:
    values = set()
    for m in _MONEY.finditer(text):
        marked = m.group("cur") or m.group("unit") or m.group("cur2") or _BUDGET_WORD.match(m.group(0))
        if not marked:
            continue
        try:
            value = Decimal(m.group("num").replace(",", ""))
            if m.group("unit"):
                value *= _UNIT[m.group("unit").lower().rstrip("s")]
        except (InvalidOperation, KeyError):
            problems.append("budget amount is not a valid number")
            return None
        if value != value.to_integral_value():
            problems.append("budget must be a whole number of rupees")
            return None
        values.add(int(value))
    if not values:
        problems.append("budget not found (e.g. 'under INR 200,000')")
        return None
    if len(values) > 1:
        problems.append("multiple different budgets found: " + ", ".join(f"{v:,}" for v in sorted(values)))
        return None
    value = next(iter(values))
    if not 0 < value <= MAX_BUDGET_INR:
        problems.append(f"budget must be between 1 and {MAX_BUDGET_INR:,}")
        return None
    return value


def _parse_guests(text: str, problems: List[str]) -> Optional[int]:
    values = {int(m.group(1)) for rx in _GUESTS for m in rx.finditer(text)}
    if not values:
        values = {int(m.group(1)) for m in _GUESTS_GROUP.finditer(text)}
    if not values:
        problems.append("guest count not found (e.g. '120 guests')")
        return None
    if len(values) > 1:
        problems.append("multiple different guest counts found: " + ", ".join(map(str, sorted(values))))
        return None
    value = next(iter(values))
    if not 1 <= value <= MAX_GUESTS:
        problems.append(f"guest count must be between 1 and {MAX_GUESTS:,}")
        return None
    return value


def _parse_city(text: str, known: Iterable[str], problems: List[str]) -> Optional[str]:
    cities = sorted({c for c in known if c}, key=lambda c: (-len(c), c))
    found = set()
    for city in cities:
        if re.search(rf"\b(?:in|at|near)\s+{re.escape(city)}\b", text, re.I | re.A):
            found.add(city)
    if not found:
        found = {
            m.group(1)
            for m in _CITY_FALLBACK.finditer(text)
            if not re.fullmatch(_MONTH_RE, m.group(1).split()[0], re.I | re.A)
            and m.group(1).split()[0].lower() not in _NOT_CITIES
        }
    if not found:
        problems.append("city not found (e.g. 'in Pune')")
        return None
    if len(found) > 1:
        problems.append("multiple cities found: " + ", ".join(sorted(found)))
        return None
    return next(iter(found))


def parse_request(
    text: str,
    today: Optional[date] = None,
    known_cities: Iterable[str] = (),
    request_id: Optional[str] = None,
) -> EventRequest:
    """Turn free text into a validated ``EventRequest`` or raise ``RequestParseError``."""
    today = today or date.today()
    if not isinstance(text, str) or not text.strip():
        raise RequestParseError(["request text is empty"])
    if len(text) > MAX_TEXT_CHARS:
        raise RequestParseError([f"request text exceeds {MAX_TEXT_CHARS} characters"])

    raw_text = text
    text = unicodedata.normalize("NFKC", text)  # match on a canonical form; raw_text is kept verbatim
    problems: List[str] = []
    assumptions: List[str] = []
    text = _ascii_digits(text, problems, assumptions)
    guests = _parse_guests(text, problems)
    city = _parse_city(text, known_cities, problems)
    event_date = _parse_dates(text, today, problems, assumptions)
    budget = _parse_budget(text, problems)
    organizers = _parse_organizers(text, problems)
    if problems:
        raise RequestParseError(problems)

    event_type = next((t for rx, t in _EVENT_TYPES if rx.search(text)), None)
    community = None
    if event_type is None:
        hit = match_subtype(text)  # keywords and subtypes are data (community_profiles.json), so the demo subtype is a config choice
        if hit is not None:
            event_type, (subtype, keyword) = EventType.COMMUNITY, hit
            community = CommunityDetails(subtype=subtype)
            if "community" in keyword:
                if subtype == load_profiles().default:
                    assumptions.append(f"community subtype not specified; using '{subtype}'")
            else:
                assumptions.append(f"treated as a community event ({subtype.replace('_', ' ')}) because the text mentions '{keyword}'; check this")
        else:
            event_type = EventType.OTHER
            assumptions.append("event type not recognised; treated as 'other'")

    wants_in, wants_out = _mentioned(_INDOOR, text), _mentioned(_OUTDOOR, text)
    if wants_in and not wants_out:
        setting = VenueSetting.INDOOR
    elif wants_out and not wants_in:
        setting = VenueSetting.OUTDOOR
    else:
        setting = VenueSetting.ANY
        assumptions.append("venue setting not specified or conflicting; any setting accepted")

    services = [VendorCategory.VENUE]
    if not re.search(r"\bvenue\b", text, re.I | re.A):
        assumptions.append("a venue is always required; added to services")
    if _mentioned(_CATERER, text):
        services.append(VendorCategory.CATERER)
    if _mentioned(_PHOTO, text):
        services.append(VendorCategory.PHOTOGRAPHER)

    dietary = tuple(d for rx, d in _DIETS if _mentioned(rx, text))
    caterer_refused = bool(_CATERER.search(text)) and VendorCategory.CATERER not in services
    if dietary and VendorCategory.CATERER not in services and not caterer_refused:
        services.append(VendorCategory.CATERER)
        assumptions.append("dietary requirement given; caterer added to services")

    reminders: List[ReminderKind] = []
    if _mentioned(_REMINDER, text):
        if _mentioned(_REM_INVITE, text):
            reminders.append(ReminderKind.INVITATION)
        if _mentioned(_REM_VENDOR, text):
            reminders.append(ReminderKind.VENDOR_CONFIRMATION)
        if not reminders:
            assumptions.append("reminders mentioned but kind not recognised; none scheduled")

    assumptions = list(dict.fromkeys(assumptions))
    try:
        return EventRequest(
            request_id=request_id or uuid.uuid4().hex[:16],
            raw_text=raw_text,
            event_type=event_type,
            guest_count=guests,
            city=city.strip(),
            event_date=event_date,
            budget_total=budget,
            dietary=dietary,
            venue_setting=setting,
            services=tuple(services),
            reminders=tuple(reminders),
            assumptions=tuple(assumptions),
            parse_confidence=round(max(0.3, 1.0 - 0.1 * len(assumptions)), 2),
            organizers=organizers,
            community=community,
        )
    except ValidationError as exc:
        raise RequestParseError([e["msg"] for e in exc.errors()]) from exc


# ------------------------------------------------------------ decomposition

AGENT_CAPABILITIES: Dict[AgentName, frozenset] = {
    AgentName.BUDGET: frozenset({TaskKind.ALLOCATE_BUDGET}),
    AgentName.GUEST: frozenset({TaskKind.TRACK_GUESTS}),
    AgentName.VENDOR: frozenset({TaskKind.SOURCE_VENDOR}),
    AgentName.SCHEDULER: frozenset({TaskKind.BUILD_SCHEDULE, TaskKind.SCHEDULE_REMINDER}),
    AgentName.PLANNER: frozenset({TaskKind.BUILD_PLAN_MODEL}),
    AgentName.VALIDATOR: frozenset({TaskKind.VALIDATE_PLAN}),
    AgentName.EXPLAINER: frozenset({TaskKind.EXPLAIN_PLAN}),
}
TERMINAL_TASK_ID = "explain_plan:event"


def _t(kind: TaskKind, subject: str, agent: AgentName, deps: Iterable[str], description: str) -> Task:
    return Task(
        id=f"{kind.value}:{subject}",
        kind=kind,
        subject=subject,
        agent=agent,
        depends_on=tuple(deps),
        description=description,
    )


def decompose(request: EventRequest) -> TaskGraph:
    """Build the minimal task DAG for this request. Only requested work appears."""
    budget = _t(TaskKind.ALLOCATE_BUDGET, "event", AgentName.BUDGET, [], "Split the budget across requested services")
    guests = _t(TaskKind.TRACK_GUESTS, "event", AgentName.GUEST, [], "Track headcount, RSVPs and dietary needs")
    tasks = [budget, guests]

    vendor_ids: List[str] = []
    venue_id = "source_vendor:venue"
    for category in request.services:
        deps = [budget.id, guests.id] if category == VendorCategory.VENUE else [budget.id, guests.id, venue_id]
        task = _t(TaskKind.SOURCE_VENDOR, category.value, AgentName.VENDOR, deps, f"Shortlist {category.value} options")
        tasks.append(task)
        vendor_ids.append(task.id)

    schedule = _t(TaskKind.BUILD_SCHEDULE, "event", AgentName.SCHEDULER, vendor_ids, "Create timeline, durations and resources")
    tasks.append(schedule)
    reminder_ids: List[str] = []
    for kind in request.reminders:
        deps = [guests.id, schedule.id] if kind == ReminderKind.INVITATION else [*vendor_ids, schedule.id]
        task = _t(TaskKind.SCHEDULE_REMINDER, kind.value, AgentName.SCHEDULER, deps, f"Schedule {kind.value} reminder")
        tasks.append(task)
        reminder_ids.append(task.id)

    model = _t(TaskKind.BUILD_PLAN_MODEL, "event", AgentName.PLANNER, [schedule.id, guests.id, budget.id, *reminder_ids], "Build PDDL / CP-SAT input")
    validate = _t(TaskKind.VALIDATE_PLAN, "event", AgentName.VALIDATOR, [model.id], "Check solver output against hard constraints")
    explain = _t(TaskKind.EXPLAIN_PLAN, "event", AgentName.EXPLAINER, [validate.id], "Write the human-readable plan")
    tasks += [model, validate, explain]
    return TaskGraph(tasks=tuple(tasks))


def verify_graph(request: EventRequest, graph: TaskGraph) -> List[ConstraintCheck]:
    """AOP principles + necessity. Structure (cycles, dupes) is enforced by ``TaskGraph``."""
    unsolvable = [
        t.id for t in graph.tasks if t.kind not in AGENT_CAPABILITIES.get(t.agent, frozenset())
    ]
    have = {(t.kind, t.subject) for t in graph.tasks}
    required = {(TaskKind.ALLOCATE_BUDGET, "event"), (TaskKind.EXPLAIN_PLAN, "event")}
    required |= {(TaskKind.SOURCE_VENDOR, c.value) for c in request.services}
    required |= {(TaskKind.SCHEDULE_REMINDER, r.value) for r in request.reminders}
    missing = sorted(f"{k.value}:{s}" for k, s in required - have)

    ids = {t.id for t in graph.tasks}
    if TERMINAL_TASK_ID in ids:
        needed = graph.ancestors(TERMINAL_TASK_ID) | {TERMINAL_TASK_ID}
        unnecessary = sorted(ids - needed)
    else:
        unnecessary = []  # already reported as missing
    return [
        ConstraintCheck(
            name="solvability",
            passed=not unsolvable,
            detail="every task is owned by an agent able to perform it"
            if not unsolvable
            else "no capable owner for: " + ", ".join(sorted(unsolvable)),
        ),
        ConstraintCheck(
            name="completeness",
            passed=not missing,
            detail="every requested service and reminder has a task" if not missing else "missing tasks: " + ", ".join(missing),
        ),
        ConstraintCheck(
            name="necessity",
            passed=not unnecessary,
            detail="every task contributes to the final explanation"
            if not unnecessary
            else "tasks not feeding the final output: " + ", ".join(unnecessary),
        ),
        ConstraintCheck(name="non_redundancy", passed=True, detail="no duplicate (kind, subject) pairs; enforced by TaskGraph"),
    ]


# ------------------------------------------------------------------ manager


class ManagerAgent:
    """Parses, decomposes, dispatches every task in dependency order and tracks completion.

    ``run_intake`` stops after sourcing (no plan). ``run`` continues through
    planning, validation, storage and explanation, and only reports ``planned``
    when the Validator passed and the plan version was written.
    """

    agent_name = AgentName.MANAGER

    def __init__(
        self,
        repo: VendorRepository,
        logger: DecisionLogger,
        budget_agent=None,
        vendor_agent=None,
        guest_agent=None,
        planner=None,
        validator=None,
        explainer=None,
        store=None,
    ):
        self.repo = repo
        self.logger = logger
        self.budget = budget_agent or BudgetAgent(logger=logger)
        self.vendor = vendor_agent or VendorAgent(repo, logger)
        self.guest = guest_agent or GuestAgent(GuestRepository(), logger)
        self.planner, self.validator, self.explainer, self.store = planner, validator, explainer, store

    # ------------------------------------------------------------- intake

    def run_intake(self, text: str, today: Optional[date] = None, guest_list_id: Optional[str] = None) -> IntakeResult:
        start = len(self.logger.entry_ids)
        request = self._parse_and_log(text, today)
        return self.intake_request(request, guest_list_id, start)

    def parse(self, text: str, today: Optional[date] = None) -> EventRequest:
        """Parse and log a request without running the pipeline."""
        return self._parse_and_log(text, today)

    def intake_request(self, request: EventRequest, guest_list_id: Optional[str] = None, start: Optional[int] = None) -> IntakeResult:
        """Intake for an already-parsed request (a trade-off option the user chose, or a dry run)."""
        start = len(self.logger.entry_ids) if start is None else start
        graph, request, guests, allocation, shortlists = self._execute_until_sourcing(request, guest_list_id)
        assessment = self.budget.assess(request, allocation, shortlists)
        reality = reality_check(request, self.repo, self.budget.policy)
        self._log_reality(request, reality)
        violations: List[ConstraintViolation] = [v for s in shortlists.values() for v in s.violations]
        violations += [v for v in assessment.violations if v not in violations]
        violations += [ConstraintViolation(constraint=c.name, detail=c.detail, repair="raise the guest count or prune the guest list") for c in guests.checks if not c.passed]
        return IntakeResult(
            request=request,
            task_graph=graph,
            budget_allocation=allocation,
            vendor_shortlists=tuple(shortlists[c] for c in request.services),
            budget_assessment=assessment,
            guests=guests,
            reality=reality,
            status="needs_repair" if violations or assessment.status in ("overspend", "incomplete") else "intake_complete",
            violations=tuple(violations),
            decision_entry_ids=tuple(self.logger.entry_ids[start:]),
        )

    def _log_reality(self, request: EventRequest, reality) -> None:
        self.logger.log(
            request_id=request.request_id,
            agent=AgentName.BUDGET,
            decision_type=DecisionType.ASSESSMENT,
            summary=f"Budget reality check: {reality.verdict}",
            constraints_checked=(ConstraintCheck(name="budget_reality", passed=reality.verdict in ("comfortable", "tight"), detail=reality.message),),
            justification=reality.basis,
            confidence=0.7 if reality.verdict != "unknown" else 0.3,
            source=reality.source,
        )

    def _parse_and_log(self, text: str, today: Optional[date]) -> EventRequest:
        _arcade_pulse.stage_started("parse")
        try:
            request = parse_request(text, today=today, known_cities=self.repo.cities())
        except RequestParseError as exc:
            _arcade_pulse.stage_done("parse")
            self.logger.log(
                request_id="unparsed",
                agent=self.agent_name,
                decision_type=DecisionType.FAILURE,
                summary="Request could not be parsed",
                constraints_checked=tuple(ConstraintCheck(name="parse", passed=False, detail=p) for p in exc.problems[:10]),
                justification="Required facts were missing or ambiguous; nothing was guessed. Ask the user to clarify: "
                + "; ".join(exc.problems[:10]),
                confidence=1.0,
                source="user_input",
            )
            raise
        self.logger.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.PARSE,
            summary=f"Parsed {request.event_type.value} for {request.guest_count} guests in {request.city} on {request.event_date.isoformat()}",
            constraints_checked=(
                ConstraintCheck(name="required_fields", passed=True, detail="guests, city, date and budget all present and unambiguous"),
            ),
            justification="Facts extracted verbatim from the user's text by deterministic rules. "
            + ("Assumptions: " + "; ".join(request.assumptions) if request.assumptions else "No assumptions were needed."),
            confidence=request.parse_confidence,
            source="user_input",
        )
        _arcade_pulse.stage_done("parse")
        return request

    def _execute_until_sourcing(self, request: EventRequest, guest_list_id: Optional[str]):
        _arcade_pulse.stage_started("decompose")
        graph = decompose(request)
        checks = verify_graph(request, graph)
        failed = [c for c in checks if not c.passed]
        self.logger.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.FAILURE if failed else DecisionType.DECOMPOSITION,
            summary=f"Decomposed into {len(graph.tasks)} tasks",
            constraints_checked=tuple(checks),
            justification="Tasks are created only for requested services and reminders, each assigned to the agent "
            "whose capabilities cover it.",
            confidence=1.0,
            source="rule:decompose",
        )
        if failed:
            raise RuntimeError("internal error: decomposition violates " + ", ".join(c.name for c in failed))
        _arcade_pulse.stage_done("decompose")

        allocation, guests = None, None
        shortlists: Dict[VendorCategory, object] = {}
        for task in graph.topological_order():
            if not all(graph.get(d).status == TaskStatus.DONE for d in task.depends_on):
                continue
            if task.kind == TaskKind.ALLOCATE_BUDGET:
                _arcade_pulse.stage_started("budget")
                allocation = self.budget.allocate(request)
                _arcade_pulse.stage_done("budget")
            elif task.kind == TaskKind.TRACK_GUESTS:
                _arcade_pulse.stage_started("guests")
                guests = self.guest.summarize(request, guest_list_id)
                request = self._with_guest_diets(request, guests)
                _arcade_pulse.stage_done("guests")
            elif task.kind == TaskKind.SOURCE_VENDOR:
                _arcade_pulse.stage_started("vendors")
                category = VendorCategory(task.subject)
                shortlists[category] = self.vendor.shortlist_category(request, category, allocation.cap_for(category))
            else:
                continue  # later stages are executed by ``run``
            graph = graph.with_status(task.id, TaskStatus.DONE)
        _arcade_pulse.stage_done("vendors")
        return graph, request, guests, allocation, shortlists

    def _with_guest_diets(self, request: EventRequest, guests: GuestSummary) -> EventRequest:
        added = [d for d in guests.required_diets if d not in request.dietary]
        if not added:
            return request
        note = "dietary needs from the guest list added to the caterer constraints: " + ", ".join(d.value for d in added)
        return request.with_changes(dietary=tuple(request.dietary) + tuple(added), assumptions=tuple(request.assumptions) + (note,))

    # ------------------------------------------------------------ full run

    def run(self, text: str, today: Optional[date] = None, guest_list_id: Optional[str] = None, plan_id: Optional[str] = None) -> PlanOutcome:
        """Parse -> decompose -> source -> plan -> validate -> store -> explain."""
        self._require_pipeline()
        start = len(self.logger.entry_ids)
        today = today or date.today()
        request = self._parse_and_log(text, today)
        return self._run_from(request, today, guest_list_id, plan_id, start, "initial plan", persist=True)

    def run_request(
        self,
        request: EventRequest,
        today: Optional[date] = None,
        guest_list_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        change_reason: str = "initial plan",
        persist: bool = True,
    ) -> PlanOutcome:
        """Same pipeline for an already-parsed request. ``persist=False`` is a dry run: nothing is stored."""
        self._require_pipeline()
        # the request was already parsed (intake conversation or a chosen trade-off)
        _arcade_pulse.stage_started("parse")
        _arcade_pulse.stage_done("parse")
        return self._run_from(request, today or date.today(), guest_list_id, plan_id, len(self.logger.entry_ids), change_reason, persist)

    def _require_pipeline(self) -> None:
        if None in (self.planner, self.validator, self.explainer, self.store):
            raise RuntimeError("ManagerAgent.run needs planner, validator, explainer and store")

    def _run_from(self, request, today, guest_list_id, plan_id, start, change_reason, persist) -> PlanOutcome:
        from datetime import datetime  # local: only the full pipeline needs wall-clock origin

        from eventmate.plan_store import assemble_plan, new_plan_id

        intake = self.intake_request(request, guest_list_id, start)
        graph = intake.task_graph
        request = intake.request

        def outcome(violations, plan=None):
            return PlanOutcome(
                status="planned" if plan is not None else "needs_repair",
                intake=intake,
                task_graph=graph,
                plan=plan,
                violations=tuple(violations),
                decision_entry_ids=tuple(self.logger.entry_ids[start:]),
            )

        if intake.status != "intake_complete":
            return outcome(intake.violations or [ConstraintViolation(constraint="intake", detail=intake.budget_assessment.status, repair="see violations")])

        plan_id = plan_id or new_plan_id()
        origin = datetime.combine(today, datetime.min.time())
        planned = self.planner.plan(request, intake.guests, intake.budget_allocation, intake.vendor_shortlists, origin, plan_id)
        if planned.draft is None:
            return outcome(planned.violations)
        for t in graph.tasks:
            if t.kind in (TaskKind.BUILD_SCHEDULE, TaskKind.SCHEDULE_REMINDER, TaskKind.BUILD_PLAN_MODEL):
                graph = graph.with_status(t.id, TaskStatus.DONE)

        _arcade_pulse.stage_started("validate")
        report = self.validator.validate(planned.draft)
        _arcade_pulse.stage_done("validate")
        if not report.passed:
            return outcome(report.violations)
        graph = graph.with_status("validate_plan:event", TaskStatus.DONE)

        plan = assemble_plan(planned.draft, report, 1, None, change_reason, tuple(self.logger.entry_ids[start:]))
        if persist:
            _arcade_pulse.stage_started("store")
            self.store.save(plan)
            self.explainer.explain(plan)
            _arcade_pulse.stage_done("store")
        graph = graph.with_status(TERMINAL_TASK_ID, TaskStatus.DONE)
        return outcome((), plan)
