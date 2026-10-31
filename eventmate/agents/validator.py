"""Validator Agent: the gate between a candidate plan and the user.

Independent of the Planner and the solvers: it re-derives every fact from the
database (price, availability, capacity, diets, status), re-checks the schedule
with its own logic (dependencies, bounds, resource sweep-line, policy rules)
and re-reads the symbolic result. A plan that fails any hard check cannot become
a ``PlanVersion``. Every failure names the constraint and a repair, and is
written to the validation-failure log.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Iterable, List, Optional, Tuple

from eventmate.planning import community
from eventmate.agents.budget import BudgetPolicy, compute_allocation
from eventmate.agents.guest import GuestAgent
from eventmate.agents.scheduler import DAY, EventPolicy
from eventmate.agents.vendor import VendorRepository, evaluate
from eventmate.logs import RunLogs
from eventmate.schemas import (
    AgentName,
    BookingStatus,
    ConstraintCheck,
    ConstraintViolation,
    DecisionType,
    PlanDraft,
    PlanVersion,
    ReminderKind,
    ValidationReport,
    VendorCategory,
)

REPAIRS = {
    "budget_total": "raise the budget, drop a service, or choose cheaper vendors",
    "city": "choose a vendor located in the event city",
    "date_availability": "choose a vendor free on the event date, or move the date",
    "capacity": "choose a vendor whose capacity covers the guest count, or change the guest count",
    "dietary": "choose a caterer that offers every required diet, or relax the diet",
    "venue_setting": "choose a venue with the requested setting, or allow any setting",
    "booking_status": "choose a vendor that is available and not cancelled",
    "budget_cap": "choose a cheaper vendor or rebalance the category budget",
    "cost_matches_database": "recompute the cost from the vendor record",
    "vendor_exists": "select a vendor that exists in the database",
    "confirmation_provenance": "only a user-confirmed booking from the parent plan may be marked confirmed",
    "services_match_request": "select exactly one vendor for each requested service",
    "guest_capacity": "choose a larger venue or reduce the guest count",
    "guest_dietary": "choose a caterer covering every guest diet",
    "rsvp_within_headcount": "raise the guest count or prune the guest list",
    "headcount_matches": "rebuild the plan from the current guest count",
    "spec_matches_policy": "rebuild the schedule spec from the event policy",
    "allocation_matches_request": "recompute the budget allocation from the request",
    "symbolic_feasibility": "fix the task dependencies; the PDDL check did not confirm feasibility",
    "invitation_lead_time": "choose a later event date or fewer lead-time days",
    "confirmed_bookings_preserved": "keep the confirmed booking, or explicitly override it",
    "confirmed_booking_time_preserved": "keep the confirmed booking's original time",
    "community_profile": "choose one of the known community subtypes listed in the detail, or add it to the community profile file",
    "attendance_vs_capacity": "choose a venue that holds the top of the attendance range, or lower the range",
    "funding_vs_budget": "record funds as received, opt in to counting committed funds (it is then shown as such), or lower the budget to what the funding covers",
    "volunteer_coverage": "assign more volunteers to the uncovered role, remove the double booking, or name the missing setup or cleanup owner",
    "timeline_dependencies": "shorten or reorder the programme so setup ends before doors, cleanup follows the close, and no resource is used twice at once",
    "accessibility_declared": "choose a venue that declares the needed access, or ask the venue again and record its answer",
}


def _chk(name: str, passed: bool, detail: str) -> ConstraintCheck:
    return ConstraintCheck(name=name, passed=passed, detail=detail)


class ValidatorAgent:
    agent_name = AgentName.VALIDATOR

    def __init__(self, repo: VendorRepository, logs: RunLogs, policy: Optional[EventPolicy] = None, require_symbolic: bool = True, budget_policy: Optional[BudgetPolicy] = None):
        self.budget_policy = budget_policy or BudgetPolicy()
        self.repo = repo
        self.logs = logs
        self.policy = policy or EventPolicy()
        self.require_symbolic = require_symbolic

    def validate(
        self,
        draft: PlanDraft,
        parent: Optional[PlanVersion] = None,
        released_vendor_ids: Iterable[str] = (),
        newly_confirmed_vendor_ids: Iterable[str] = (),
    ) -> ValidationReport:
        released = set(released_vendor_ids)
        newly = set(newly_confirmed_vendor_ids)  # per-call state: the validator is shared across threads
        checks: List[ConstraintCheck] = []
        warnings: List[str] = []
        checks += self._request_and_guests(draft)
        checks += self._vendors(draft, parent, newly)
        checks += self._budget(draft, warnings)
        checks += self._policy_structure(draft)
        checks += self._schedule(draft)
        checks += self._policy_rules(draft)
        checks += self._symbolic(draft, warnings)
        community_checks, unverified = community.evaluate(draft, self.repo, self.policy)
        checks += community_checks
        if parent is not None:
            checks += self._preservation(draft, parent, released)
        warnings = list(dict.fromkeys(list(draft.warnings) + warnings))

        violations = [
            ConstraintViolation(constraint=c.name, detail=c.detail, repair=REPAIRS.get(c.name.split(":")[-1], f"fix the {c.name} constraint and re-validate"))
            for c in checks
            if not c.passed
        ]
        report = ValidationReport(passed=not violations, checks=tuple(checks), violations=tuple(violations), warnings=tuple(warnings), unverified=tuple(unverified))
        self._log(draft, report)
        return report

    # --------------------------------------------------------- request/guests

    def _request_and_guests(self, d: PlanDraft) -> List[ConstraintCheck]:
        cats = [s.category for s in d.selections]
        want = sorted(c.value for c in d.request.services)
        out = [
            _chk("services_match_request", sorted(c.value for c in cats) == want and len(set(cats)) == len(cats), f"selected {sorted(c.value for c in cats)}, requested {want}"),
            _chk("headcount_matches", d.guests.planning_headcount == d.request.guest_count, f"plan headcount {d.guests.planning_headcount} vs request {d.request.guest_count}"),
        ]
        expected = compute_allocation(d.request, self.budget_policy)
        out.append(_chk("allocation_matches_request", d.allocation == expected, f"allocation {[(l.category, l.amount) for l in d.allocation.lines]} + contingency {d.allocation.contingency:,} vs recomputed {[(l.category, l.amount) for l in expected.lines]} + {expected.contingency:,}"))
        out += list(d.guests.checks)
        return out

    # ----------------------------------------------------------------- vendors

    def _vendors(self, d: PlanDraft, parent: Optional[PlanVersion], newly: set) -> List[ConstraintCheck]:
        out: List[ConstraintCheck] = []
        diets = tuple(dict.fromkeys(list(d.request.dietary) + list(d.guests.required_diets)))
        eff = d.request.with_changes(dietary=diets)
        for s in d.selections:
            p = f"{s.category.value}:"
            rec = self.repo.get(s.category, s.vendor_id)
            if rec is None:
                out.append(_chk(p + "vendor_exists", False, f"{s.vendor_id} is not in the database"))
                continue
            out.append(_chk(p + "vendor_exists", True, f"{rec.name} found"))
            confirmed = s.booking_status == BookingStatus.CONFIRMED
            cap = d.allocation.cap_for(s.category) if s.category in d.request.services else 0
            for c in evaluate(rec, eff, s.category, cap, excluded=s.vendor_id in d.excluded_vendor_ids, price=self.repo.price(rec, d.request.guest_count), quote_expired=self.repo.quote_expired(rec)):
                if c.name == "budget_cap" and confirmed:
                    continue  # a preserved booking is held to the total budget, not the category cap
                out.append(_chk(p + c.name, c.passed, c.detail))
            expected = self.repo.price(rec, d.request.guest_count)
            out.append(_chk(p + "cost_matches_database", s.cost == expected, f"plan cost INR {s.cost:,}, database says INR {expected:,}"))
            if confirmed:
                ok = s.vendor_id in newly or (parent is not None and any(x.vendor_id == s.vendor_id and x.booking_status == BookingStatus.CONFIRMED for x in parent.selections))
                out.append(_chk(p + "confirmation_provenance", ok, "confirmed by the user" if ok else "no user confirmation of this vendor exists"))
            if s.category == VendorCategory.VENUE:
                out.append(GuestAgent.capacity_check(d.guests, getattr(rec, "capacity", 0)))
            elif s.category == VendorCategory.CATERER:
                out.append(GuestAgent.dietary_check(d.guests, rec.dietary_options))
        return out

    # ------------------------------------------------------------------ budget

    def _budget(self, d: PlanDraft, warnings: List[str]) -> List[ConstraintCheck]:
        total = sum(s.cost for s in d.selections)
        budget = d.request.budget_total
        if total <= budget and total > budget - d.allocation.contingency:
            warnings.append(f"contingency reserve of INR {d.allocation.contingency:,} is not fully available (total INR {total:,} of INR {budget:,})")
        return [_chk("budget_total", total <= budget, f"vendor total INR {total:,} vs budget INR {budget:,}" + ("" if total <= budget else f"; over by INR {total - budget:,}: raise the budget"))]

    # ---------------------------------------------------------------- schedule

    def _policy_structure(self, d: PlanDraft) -> List[ConstraintCheck]:
        """The spec is part of the draft, so it cannot be trusted: compare it with the policy."""
        p = self.policy
        _, event_minutes = p.event_window(d.request)
        want = {
            "book_venue": (p.booking_minutes, "organizer"),
            "venue_setup": (p.venue_setup_minutes, "venue"),
            "event": (event_minutes, "venue"),
            "teardown": (p.teardown_minutes, "venue"),
        }
        if VendorCategory.CATERER in d.request.services:
            want["book_caterer"] = (p.booking_minutes, "organizer")
            want["catering_setup"] = (p.catering_setup_minutes, "caterer")
        if VendorCategory.PHOTOGRAPHER in d.request.services:
            want["book_photographer"] = (p.booking_minutes, "organizer")
            want["photography_prep"] = (p.photography_prep_minutes, "photographer")
        if ReminderKind.INVITATION in d.request.reminders:
            want["reminder_invitation"] = (p.reminder_minutes, "organizer")
            want["send_invitations"] = (p.send_invitations_minutes, "organizer")
        if ReminderKind.VENDOR_CONFIRMATION in d.request.reminders:
            want["reminder_vendor_confirmation"] = (p.reminder_minutes, "organizer")
        for a in (d.request.community.program if d.request.community else ()):
            want[f"activity_{a.activity_id}"] = (a.minutes, f"res_{a.resource}")
        problems = []
        have = {t.id: (t.duration, t.resource) for t in d.spec.tasks}
        for tid, exp in want.items():
            if have.get(tid) != exp:
                problems.append(f"{tid}: spec has {have.get(tid)}, policy says {exp}")
        problems += [f"{tid}: not allowed for this request" for tid in sorted(set(have) - set(want))]
        used = {r for _, r in want.values()}
        expected = {r: (d.request.organizers if r == "organizer" else 1) for r in used}
        if dict(d.spec.resources) != expected:
            problems.append(f"resources must be exactly {expected} (organizers come from the request, all others capacity 1), spec has {dict(d.spec.resources)}")
        return [_chk("spec_matches_policy", not problems, "task durations, resources and capacities follow the policy" if not problems else "; ".join(problems[:5]))]

    def _schedule(self, d: PlanDraft) -> List[ConstraintCheck]:
        spec, entries = d.spec, {e.task_id: e for e in d.solution.entries}
        tasks = {t.id: t for t in spec.tasks}
        out: List[ConstraintCheck] = []
        expected = self._expected_tasks(d)
        missing = sorted((set(tasks) | expected) - set(entries))
        extra = sorted(set(entries) - set(tasks))
        out.append(_chk("schedule_complete", not missing and not extra and expected <= set(tasks), "every expected task is scheduled" if not (missing or extra) and expected <= set(tasks) else f"missing {missing or sorted(expected - set(tasks))}, unexpected {extra}"))
        known = [t for t in tasks.values() if t.id in entries]
        bad_dur = [t.id for t in known if entries[t.id].end - entries[t.id].start != t.duration]
        out.append(_chk("schedule_durations", not bad_dur, "all durations match" if not bad_dur else f"wrong duration: {bad_dur}"))
        bad_dep = [f"{x.before}->{x.after}" for x in spec.dependencies if x.before in entries and x.after in entries and entries[x.after].start < entries[x.before].end + x.min_lag]
        out.append(_chk("schedule_dependencies", not bad_dep, "all precedences and lags hold" if not bad_dep else f"violated: {bad_dep}"))
        bad_bounds = []
        for t in known:
            e = entries[t.id]
            if e.start < t.earliest_start or e.end > spec.horizon or e.start < 0:
                bad_bounds.append(f"{t.id}: outside earliest/horizon")
            if t.fixed_start is not None and e.start != t.fixed_start:
                bad_bounds.append(f"{t.id}: not at fixed start")
            if t.deadline_end is not None and e.end > t.deadline_end:
                bad_bounds.append(f"{t.id}: misses deadline")
        out.append(_chk("schedule_bounds", not bad_bounds, "all time bounds hold" if not bad_bounds else "; ".join(bad_bounds)))
        out.append(_chk("schedule_resources", not (over := self._resource_overuse(spec, entries)), "no resource is over capacity" if not over else f"over capacity: {over}"))
        by_task = {i.task_id: i for i in d.items}
        ok_items = set(by_task) == set(entries) and all(
            by_task[k].start == d.origin + timedelta(minutes=e.start) and by_task[k].end == d.origin + timedelta(minutes=e.end) for k, e in entries.items()
        )
        out.append(_chk("items_match_solution", ok_items, "wall-clock items equal the solved schedule" if ok_items else "plan items differ from the solved schedule"))
        return out

    def _expected_tasks(self, d: PlanDraft) -> set:
        ids = {"book_venue", "venue_setup", "event", "teardown"}
        if VendorCategory.CATERER in d.request.services:
            ids |= {"book_caterer", "catering_setup"}
        if VendorCategory.PHOTOGRAPHER in d.request.services:
            ids |= {"book_photographer", "photography_prep"}
        if ReminderKind.INVITATION in d.request.reminders:
            ids |= {"reminder_invitation", "send_invitations"}
        if ReminderKind.VENDOR_CONFIRMATION in d.request.reminders:
            ids |= {"reminder_vendor_confirmation"}
        ids |= {f"activity_{a.activity_id}" for a in (d.request.community.program if d.request.community else ())}
        return ids

    @staticmethod
    def _resource_overuse(spec, entries) -> List[str]:
        bad: List[str] = []
        for resource, cap in spec.resources.items():
            events: List[Tuple[int, int]] = []
            for t in spec.tasks:
                if t.resource == resource and t.id in entries:
                    events += [(entries[t.id].start, 1), (entries[t.id].end, -1)]
            level = 0
            for _, delta in sorted(events, key=lambda x: (x[0], x[1])):  # ends before starts at the same minute
                level += delta
                if level > cap:
                    bad.append(resource)
                    break
        return bad

    def _policy_rules(self, d: PlanDraft) -> List[ConstraintCheck]:
        """Business rules re-derived from the request, not read back from the spec."""
        p, e = self.policy, {x.task_id: x for x in d.solution.entries}
        day0 = (d.request.event_date - d.origin.date()).days * DAY
        hour, minutes = p.event_window(d.request)
        out: List[ConstraintCheck] = []
        ev = e.get("event")
        out.append(_chk("event_time", ev is not None and ev.start == day0 + hour * 60 and ev.end - ev.start == minutes, f"event must run {hour:02d}:00 for {minutes} min on {d.request.event_date.isoformat()}"))
        if "send_invitations" in e:
            ok = e["send_invitations"].end <= day0 - p.invitation_lead_days * DAY
            out.append(_chk("invitation_lead_time", ok, f"invitations must finish {p.invitation_lead_days} days before the event"))
        if "reminder_vendor_confirmation" in e:
            r = e["reminder_vendor_confirmation"]
            ok = day0 - p.vendor_confirmation_window_start_days * DAY <= r.start and r.end <= day0 - p.vendor_confirmation_window_end_days * DAY
            out.append(_chk("vendor_confirmation_window", ok, f"vendor confirmation reminder must fall {p.vendor_confirmation_window_end_days}-{p.vendor_confirmation_window_start_days} days before the event"))
        if ev is not None:
            setups = [e[t] for t in ("venue_setup", "catering_setup", "photography_prep") if t in e]
            ok = all(s.end + p.setup_buffer_minutes <= ev.start for s in setups)
            out.append(_chk("setup_before_event", ok, f"all setups must end {p.setup_buffer_minutes} min before the event starts"))
            out.append(_chk("teardown_after_event", "teardown" in e and e["teardown"].start >= ev.end, "teardown starts after the event ends"))
        wanted = {ReminderKind.INVITATION: "reminder_invitation", ReminderKind.VENDOR_CONFIRMATION: "reminder_vendor_confirmation"}
        items = {i.task_id for i in d.items}
        miss = [wanted[r] for r in d.request.reminders if wanted[r] not in items]
        out.append(_chk("reminders_scheduled", not miss, "every requested reminder is scheduled" if not miss else f"missing reminders: {miss}"))
        return out

    # ---------------------------------------------------------------- symbolic

    def _symbolic(self, d: PlanDraft, warnings: List[str]) -> List[ConstraintCheck]:
        s = d.symbolic.status
        if s == "valid":
            return [_chk("symbolic_feasibility", True, "Fast Downward found a plan and VAL validated it")]
        if s == "unavailable" and not self.require_symbolic:
            warnings.append("symbolic (PDDL) check skipped: " + d.symbolic.detail)
            return [_chk("symbolic_feasibility", True, "skipped (solvers unavailable, not required)")]
        return [_chk("symbolic_feasibility", False, f"symbolic check status {s}: {d.symbolic.detail}")]

    # ------------------------------------------------------------ preservation

    def _preservation(self, d: PlanDraft, parent: PlanVersion, released: set) -> List[ConstraintCheck]:
        out: List[ConstraintCheck] = []
        now = {s.vendor_id: s for s in d.selections}
        old_items = {i.task_id: i for i in parent.items}
        new_items = {i.task_id: i for i in d.items}
        booking = {VendorCategory.VENUE: "book_venue", VendorCategory.CATERER: "book_caterer", VendorCategory.PHOTOGRAPHER: "book_photographer"}
        lost, moved = [], []
        for s in parent.selections:
            if s.booking_status != BookingStatus.CONFIRMED or s.vendor_id in released:
                continue
            kept = now.get(s.vendor_id)
            if kept is None or kept.booking_status != BookingStatus.CONFIRMED:
                lost.append(s.name)
                continue
            t = booking[s.category]
            if t in old_items and (t not in new_items or new_items[t].start != old_items[t].start):
                moved.append(s.name)
        out.append(_chk("confirmed_bookings_preserved", not lost, "every confirmed booking is kept" if not lost else f"confirmed bookings dropped without override: {lost}"))
        out.append(_chk("confirmed_booking_time_preserved", not moved, "confirmed booking times are unchanged" if not moved else f"confirmed booking time changed: {moved}"))
        return out

    # ----------------------------------------------------------------- logging

    def _log(self, d: PlanDraft, report: ValidationReport) -> None:
        for v in report.violations:
            self.logs.validation_failures.log(request_id=d.request.request_id, plan_id=d.plan_id, constraint=v.constraint[:80], detail=v.detail[:1000], repair=v.repair[:1000])
        self.logs.decisions.log(
            request_id=d.request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.ASSESSMENT if report.passed else DecisionType.FAILURE,
            summary=f"Validation {'passed' if report.passed else 'FAILED'}: {sum(c.passed for c in report.checks)}/{len(report.checks)} checks",
            constraints_checked=tuple(report.checks[:60]),
            justification="All hard constraints hold; the plan may be shown to the user."
            if report.passed
            else "; ".join(f"{v.constraint}: {v.detail}. Repair: {v.repair}" for v in report.violations[:6]),
            confidence=1.0,
            source="rule:validator",
        )
