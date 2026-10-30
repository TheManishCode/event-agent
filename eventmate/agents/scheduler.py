"""Scheduling Agent: tasks, durations, dependencies and resource requirements.

Turns a request plus the selected vendor categories into a typed
``ScheduleSpec`` for the CP-SAT backend, and turns a solution back into
wall-clock ``PlanItem``s. Lead-time rules live in ``EventPolicy`` (code), never
in prompts. Time is whole minutes from ``origin`` (midnight of the planning day).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from pydantic import Field

from eventmate.cpsat import solve_schedule
from eventmate.decision_logger import DecisionLogger
from eventmate.schemas import (
    AgentName,
    ConstraintViolation,
    EventRequest,
    EventType,
    PlanItem,
    ReminderKind,
    ScheduleDependency,
    ScheduleSolution,
    ScheduleSpec,
    ScheduleTask,
    VendorCategory,
    _Model,
)

DAY = 1440


class EventPolicy(_Model):
    work_start_hour: int = Field(default=9, ge=0, le=23)
    booking_minutes: int = Field(default=60, ge=1)
    reminder_minutes: int = Field(default=15, ge=1)
    send_invitations_minutes: int = Field(default=120, ge=1)
    invitation_lead_days: int = Field(default=14, ge=0)
    reminder_to_send_gap_days: int = Field(default=1, ge=0)
    vendor_confirmation_window_start_days: int = Field(default=7, ge=0)
    vendor_confirmation_window_end_days: int = Field(default=2, ge=0)
    venue_access_hour: int = Field(default=8, ge=0, le=23)
    venue_setup_minutes: int = Field(default=120, ge=1)
    catering_setup_minutes: int = Field(default=90, ge=1)
    photography_prep_minutes: int = Field(default=30, ge=1)
    teardown_minutes: int = Field(default=120, ge=1)
    setup_buffer_minutes: int = Field(default=30, ge=0)
    # event type -> (start hour, duration minutes)
    event_defaults: Dict[str, Tuple[int, int]] = {
        EventType.ENGAGEMENT_PARTY.value: (18, 240),
        EventType.WEDDING.value: (17, 360),
        EventType.BIRTHDAY.value: (18, 180),
        EventType.CORPORATE.value: (10, 240),
        EventType.CONFERENCE.value: (9, 480),
        EventType.COMMUNITY.value: (16, 240),  # the fallback; a community event normally takes its window from its subtype profile
        EventType.OTHER.value: (18, 180),
    }

    def event_window(self, request) -> Tuple[int, int]:
        """(start hour, minutes) of the event. A community event reads it from its profile (data), every other type from ``event_defaults``."""
        if request.event_type == EventType.COMMUNITY:
            from eventmate.community import profile_for

            prof = profile_for(request)
            return prof.start_hour, prof.minutes
        return self.event_defaults[request.event_type.value]


class SchedulingAgent:
    agent_name = AgentName.SCHEDULER

    def __init__(self, logger: DecisionLogger, policy: Optional[EventPolicy] = None):
        self.logger = logger
        self.policy = policy or EventPolicy()

    # ------------------------------------------------------------ spec

    def build_spec(
        self,
        request: EventRequest,
        categories: Sequence[VendorCategory],
        origin: datetime,
        fixed_starts: Optional[Dict[str, int]] = None,
        now_offset: Optional[int] = None,
    ) -> ScheduleSpec:
        p = self.policy
        now = p.work_start_hour * 60 if now_offset is None else now_offset
        day0 = (request.event_date - origin.date()).days * DAY
        start_hour, event_minutes = p.event_window(request)
        event_start = day0 + start_hour * 60
        has = set(categories)
        tasks: List[ScheduleTask] = []
        deps: List[ScheduleDependency] = []

        def task(id_, name, minutes, **kw):
            tasks.append(ScheduleTask(id=id_, name=name, duration=minutes, **kw))

        def dep(a, b, lag=0):
            deps.append(ScheduleDependency(before=a, after=b, min_lag=lag))

        pre = dict(group="pre_event", earliest_start=now, prefer="early", resource="organizer")
        task("book_venue", "Book venue", p.booking_minutes, **pre)
        if VendorCategory.CATERER in has:
            task("book_caterer", "Book caterer", p.booking_minutes, **pre)
            dep("book_venue", "book_caterer")
        if VendorCategory.PHOTOGRAPHER in has:
            task("book_photographer", "Book photographer", p.booking_minutes, **pre)
            dep("book_venue", "book_photographer")

        if ReminderKind.INVITATION in request.reminders:
            task("reminder_invitation", "Reminder: send invitations", p.reminder_minutes, **pre)
            task("send_invitations", "Send invitations", p.send_invitations_minutes, deadline_end=max(0, day0 - p.invitation_lead_days * DAY), **pre)
            dep("book_venue", "reminder_invitation")
            dep("reminder_invitation", "send_invitations", p.reminder_to_send_gap_days * DAY)
        if ReminderKind.VENDOR_CONFIRMATION in request.reminders:
            task(
                "reminder_vendor_confirmation",
                "Reminder: confirm vendors",
                p.reminder_minutes,
                group="pre_event",
                earliest_start=max(now, day0 - p.vendor_confirmation_window_start_days * DAY + p.work_start_hour * 60),
                deadline_end=max(0, day0 - p.vendor_confirmation_window_end_days * DAY),  # before origin = already impossible
                prefer="early",
                resource="organizer",
            )
            for t in [t.id for t in tasks if t.id.startswith("book_")]:
                dep(t, "reminder_vendor_confirmation")

        day = dict(group="event_day")
        access = day0 + p.venue_access_hour * 60
        task("venue_setup", "Venue setup", p.venue_setup_minutes, earliest_start=access, prefer="late", resource="venue", **day)
        dep("book_venue", "venue_setup")
        setups = ["venue_setup"]
        if VendorCategory.CATERER in has:
            task("catering_setup", "Catering setup", p.catering_setup_minutes, earliest_start=access, prefer="late", resource="caterer", **day)
            dep("venue_setup", "catering_setup")
            dep("book_caterer", "catering_setup")
            setups.append("catering_setup")
        if VendorCategory.PHOTOGRAPHER in has:
            task("photography_prep", "Photography prep", p.photography_prep_minutes, earliest_start=access, prefer="late", resource="photographer", **day)
            dep("venue_setup", "photography_prep")
            dep("book_photographer", "photography_prep")
            setups.append("photography_prep")
        task("event", "Event", event_minutes, fixed_start=event_start, resource="venue", **day)
        for s in setups:
            dep(s, "event", p.setup_buffer_minutes)
        program = request.community.program if request.community else ()
        for a in program:  # programme activities share named resources and must sit inside the event; the same CP-SAT model places them
            task(f"activity_{a.activity_id}", f"Activity: {a.title}", a.minutes, earliest_start=event_start, deadline_end=event_start + event_minutes, prefer="early", resource=f"res_{a.resource}", **day)
            if a.after:
                dep(f"activity_{a.after}", f"activity_{a.activity_id}")
        task("teardown", "Teardown", p.teardown_minutes, resource="venue", **day)
        dep("event", "teardown")

        for tid, start in (fixed_starts or {}).items():
            if tid not in {t.id for t in tasks}:
                raise ValueError(f"cannot fix unknown task {tid!r}")
        # A confirmed booking already happened. It cannot be ordered after a step that has not
        # happened yet, so precedences *into* a fixed task from a non-fixed task are dropped.
        fixed_ids = set(fixed_starts or {})
        deps = [d for d in deps if not (d.after in fixed_ids and d.before not in fixed_ids)]
        # a fixed task (confirmed booking) is also exempt from the "not before now" floor
        tasks = [t.model_copy(update={"fixed_start": fixed_starts[t.id], "earliest_start": 0}) if fixed_starts and t.id in fixed_starts else t for t in tasks]
        # model_copy skips validation; rebuild through the validated constructor
        tasks = [ScheduleTask.model_validate(t.model_dump()) for t in tasks]

        resources = {"organizer": request.organizers, "venue": 1}
        for a in program:
            resources[f"res_{a.resource}"] = 1
        if VendorCategory.CATERER in has:
            resources["caterer"] = 1
        if VendorCategory.PHOTOGRAPHER in has:
            resources["photographer"] = 1
        horizon = event_start + event_minutes + p.teardown_minutes + DAY
        return ScheduleSpec(tasks=tuple(tasks), dependencies=tuple(deps), resources=resources, horizon=max(horizon, now + DAY), anchor_task="event")

    def _earliest_event_start(self, categories: Sequence[VendorCategory]) -> int:
        """Minutes after midnight: venue access, venue setup, then the longest parallel setup, then the buffer."""
        p = self.policy
        parallel = max([p.catering_setup_minutes if VendorCategory.CATERER in categories else 0, p.photography_prep_minutes if VendorCategory.PHOTOGRAPHER in categories else 0])
        return p.venue_access_hour * 60 + p.venue_setup_minutes + parallel + p.setup_buffer_minutes

    # ----------------------------------------------------------- results

    @staticmethod
    def to_items(spec: ScheduleSpec, solution: ScheduleSolution, origin: datetime) -> Tuple[PlanItem, ...]:
        by_id = {t.id: t for t in spec.tasks}
        items = [
            PlanItem(
                task_id=e.task_id,
                name=by_id[e.task_id].name,
                resource=by_id[e.task_id].resource,
                group=by_id[e.task_id].group,
                start=origin + timedelta(minutes=e.start),
                end=origin + timedelta(minutes=e.end),
            )
            for e in solution.entries
        ]
        return tuple(sorted(items, key=lambda i: (i.start, i.task_id)))

    def explain_infeasibility(
        self,
        solution: ScheduleSolution,
        request: EventRequest,
        categories: Sequence[VendorCategory],
        origin: datetime,
        fixed_starts: Optional[Dict[str, int]] = None,
        now_offset: Optional[int] = None,
    ) -> List[ConstraintViolation]:
        """Name the violated rule and, where the solver can prove one, a feasible later date."""
        core = set(solution.infeasibility_core)
        p = self.policy
        if any("send_invitations" in c or "reminder_invitation" in c for c in core):
            constraint = "invitation_lead_time"
            detail = f"invitations must be sent at least {p.invitation_lead_days} days before the event; conflicting: {sorted(core)}"
        elif any("reminder_vendor_confirmation" in c for c in core):
            constraint = "vendor_confirmation_window"
            detail = f"vendor confirmation must fall {p.vendor_confirmation_window_end_days}-{p.vendor_confirmation_window_start_days} days before the event; conflicting: {sorted(core)}"
        elif any("activity_" in c for c in core):
            constraint = "program_does_not_fit"
            _, event_minutes = p.event_window(request)
            totals: Dict[str, int] = {}
            for a in (request.community.program if request.community else ()):
                totals[a.resource] = totals.get(a.resource, 0) + a.minutes
            crowded = sorted((r for r, m in totals.items() if m > event_minutes), key=lambda r: -totals[r])
            detail = (
                "; ".join(f"activities on {r} need {totals[r]} minutes but the event lasts {event_minutes}" for r in crowded)
                if crowded else f"the programme's order and resources do not fit inside the {event_minutes}-minute event; conflicting: {sorted(core)}"
            )
            return [ConstraintViolation(
                constraint=constraint, detail=detail,
                repair="shorten or remove an activity, move one to another resource, drop a declared order, or lengthen the event window in the community profile",
            )]
        elif "fixed:event" in core and self._earliest_event_start(categories) > p.event_window(request)[0] * 60:
            earliest = self._earliest_event_start(categories)
            return [ConstraintViolation(
                constraint="event_start_too_early",
                detail=f"the event starts at {p.event_window(request)[0]:02d}:00, but venue access opens at {p.venue_access_hour:02d}:00 and setup plus the {p.setup_buffer_minutes}-minute buffer cannot finish before {earliest // 60:02d}:{earliest % 60:02d}",
                repair=f"start the event at or after {earliest // 60:02d}:{earliest % 60:02d} (edit the event window), or drop the caterer or photographer to shorten setup",
            )]
        elif any(c.startswith("fixed:") for c in core):
            constraint = "confirmed_booking_timing"
            detail = f"a preserved booking's fixed time conflicts with the schedule; conflicting: {sorted(core)}"
        else:
            constraint = "schedule_infeasible"
            detail = f"no schedule satisfies the timing rules; conflicting: {sorted(core) or ['unknown']}"
        later = self.earliest_feasible_date(request, categories, origin, fixed_starts, now_offset)
        repair = (
            f"move the event to on or after {later.isoformat()} (earliest date the scheduler can satisfy; re-check vendor availability)"
            if later
            else "no later date within one year is schedulable; relax the timing rules or the confirmed bookings"
        )
        return [ConstraintViolation(constraint=constraint, detail=detail, repair=repair)]

    def earliest_feasible_date(
        self,
        request: EventRequest,
        categories: Sequence[VendorCategory],
        origin: datetime,
        fixed_starts: Optional[Dict[str, int]] = None,
        now_offset: Optional[int] = None,
        horizon_days: int = 365,
    ) -> Optional[date]:
        """Binary search the (monotone) feasibility of the event date with the real solver.

        ``now_offset`` is the planning "now" (minutes from origin): during a replan the search must
        start from today, never from the original planning day, or the suggestion would not work.
        """

        def feasible(d: date) -> bool:
            spec = self.build_spec(request.with_changes(event_date=d), categories, origin, fixed_starts, now_offset)
            return solve_schedule(spec).status in ("optimal", "feasible")

        today = origin.date() + timedelta(days=(now_offset or 0) // DAY)
        lo = max(request.event_date, today)
        hi = lo + timedelta(days=horizon_days)
        if not feasible(hi):
            return None
        while lo < hi:
            mid = lo + (hi - lo) // 2
            if feasible(mid):
                hi = mid
            else:
                lo = mid + timedelta(days=1)
        return lo
