"""Explainer Agent: turns a validated plan into readable text with reasons.

It accepts only a ``PlanVersion`` (which cannot exist unless every hard
constraint passed). The text is built deterministically from plan facts; an LLM
may optionally reword it, but only through ``guard_rewrite`` (no changed numbers,
no dropped vendor names, no markup/links) and every attempt is traced.
"""

from __future__ import annotations

import re
import time
from typing import List, Optional

from eventmate.llm import LLMClient, PromptError, PromptStore, guard_rewrite
from eventmate.logs import RunLogs
from eventmate.schemas import (
    AgentName,
    BookingStatus,
    ConstraintCheck,
    DecisionType,
    DiffItem,
    PlanDiff,
    PlanVersion,
    VendorCategory,
    _Model,
)

_PROMPT_ID = "explainer.rewrite"
_MAX_TOKENS = 3000


class Explanation(_Model):
    plan_id: str
    version: int
    markdown: str
    used_llm: bool = False
    llm_rejection_reason: Optional[str] = None


def _esc(text: str) -> str:
    """Neutralise characters that could break Markdown tables or inject markup."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = re.sub(r"([\\`*\[\]<>|])", r"\\\1", text)
    return re.sub(r"(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])", r"\\_", text)  # emphasis only at word edges


def _inr(n: int) -> str:
    return f"INR {n:,}"


def _when(dt) -> str:
    return dt.strftime("%a %d %b %Y, %H:%M")


class ExplainerAgent:
    agent_name = AgentName.EXPLAINER

    def __init__(self, logs: RunLogs, llm: Optional[LLMClient] = None, prompts: Optional[PromptStore] = None):
        self.logs = logs
        self.llm = llm
        self.prompts = prompts or PromptStore()

    def explain(self, plan: PlanVersion, diff: Optional[PlanDiff] = None, community=None) -> Explanation:
        markdown = self._render(plan, diff, community)
        used, reason = False, None
        if self.llm is not None:
            polished, reason = self._polish(plan, markdown)
            if polished is not None:
                markdown, used = polished, True
        self.logs.decisions.log(
            request_id=plan.request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.ASSESSMENT,
            summary=f"Explained plan {plan.plan_id} v{plan.version}" + (" (LLM reworded)" if used else ""),
            constraints_checked=(ConstraintCheck(name="plan_validated", passed=plan.validation.passed, detail=f"{len(plan.validation.checks)} hard checks passed"),),
            justification="Text is built from validated plan facts. "
            + ("An LLM reworded it and passed the fact guard." if used else "Deterministic text used" + (f" (LLM output rejected: {reason})." if reason else ".")),
            confidence=1.0,
            source="rule:explainer",
        )
        return Explanation(plan_id=plan.plan_id, version=plan.version, markdown=markdown, used_llm=used, llm_rejection_reason=reason)

    # ----------------------------------------------------------------- LLM

    def _polish(self, plan: PlanVersion, markdown: str):
        try:
            body, version = self.prompts.load(_PROMPT_ID)
        except PromptError as exc:
            return None, f"prompt unavailable: {exc}"
        user = body.replace("{plan_markdown}", markdown)
        started = time.perf_counter()
        output, reason = "", None
        try:
            output = self.llm.complete("You edit verified event plans without changing facts.", user, _MAX_TOKENS)
            reason = guard_rewrite(output, markdown, [s.name for s in plan.selections])
        except Exception as exc:  # any provider failure must degrade to deterministic text
            reason = f"llm call failed: {type(exc).__name__}: {exc}"[:400]
        self.logs.llm_traces.log(
            request_id=plan.request.request_id,
            prompt_id=_PROMPT_ID,
            prompt_version=version,
            model=getattr(self.llm, "name", "unknown")[:80],
            input_text=user,
            output_text=output,
            accepted=reason is None,
            rejection_reason=reason,
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return (output.strip(), None) if reason is None else (None, reason)

    # ------------------------------------------------------------- render

    def _render(self, plan: PlanVersion, diff: Optional[PlanDiff], community=None) -> str:
        r = plan.request
        out: List[str] = [
            f"# {_esc(r.event_type.value.replace('_', ' ').title())} in {_esc(r.city)} on {r.event_date.strftime('%d %b %Y')}",
            "",
            f"Plan `{plan.plan_id}` version {plan.version}"
            + (f" (from version {plan.parent_version})" if plan.parent_version else "")
            + f": {_esc(plan.change_reason)}",
            "",
            "## Summary",
            f"- {r.guest_count} guests; budget {_inr(r.budget_total)}",
            f"- Vendors total {_inr(plan.total_cost)}; {_inr(r.budget_total - plan.total_cost)} left of the budget",
            f"- Dietary needs: {', '.join(d.value.replace('_', ' ') for d in plan.guests.required_diets) or 'none stated'}",
            f"- Venue setting: {r.venue_setting.value}",
            "",
            "## Vendors",
        ]
        for s in plan.selections:
            status = " (confirmed booking)" if s.booking_status == BookingStatus.CONFIRMED else ""
            out += [f"### {s.category.value.title()}: {_esc(s.name)}{status}", f"- Cost: {_inr(s.cost)}", f"- Why: {_esc(s.justification)}"]
            if s.alternatives:
                out.append("- Alternatives considered:")
                out += [f"  - {_esc(a.option)} ({_esc(a.outcome)}): {_esc(a.reason_not_chosen)}" for a in s.alternatives]
            else:
                out.append(f"- Alternatives considered: none. {_esc(s.no_alternative_reason or '')}")
            out.append("")

        out += ["## Budget", "| Item | Allocated | Planned |", "|---|---|---|"]
        for line in plan.allocation.lines:
            sel = plan.selection_for(VendorCategory(line.category))
            out.append(f"| {_esc(line.category.title())} | {_inr(line.amount)} | {_inr(sel.cost) if sel else '-'} |")
        out += [f"| Contingency | {_inr(plan.allocation.contingency)} | held in reserve |", f"| **Total** | {_inr(plan.allocation.total)} | {_inr(plan.total_cost)} |", ""]

        out += ["## Timeline"]
        for title, group in (("Before the event", "pre_event"), ("Event day", "event_day")):
            out.append(f"### {title}")
            out += [f"- {_when(i.start)} to {i.end.strftime('%H:%M')}: {_esc(i.name)}" + (f" ({i.resource})" if i.resource else "") for i in plan.items if i.group == group]
            out.append("")

        if community is not None:
            out += self._community_section(community)
        out += ["## Why this plan is valid", f"- {sum(c.passed for c in plan.validation.checks)} of {len(plan.validation.checks)} hard constraints re-checked independently against the database: all passed"]
        out.append(f"- Symbolic (PDDL) feasibility: {plan.symbolic.status}" + (f", {len(plan.symbolic.plan_actions)} steps" if plan.symbolic.plan_actions else ""))
        out.append("- Checked: vendor availability, city, capacity, dietary options, prices, budget, schedule dependencies, resource clashes, lead times, requested reminders")
        if plan.version > 1:
            out.append("- Confirmed bookings from earlier versions were checked for preservation")
        out.append("")
        if r.assumptions or plan.warnings:
            out.append("## Assumptions and warnings")
            out += [f"- Assumption: {_esc(a)}" for a in r.assumptions] + [f"- Warning: {_esc(w)}" for w in plan.warnings]
            out.append("")
        if diff is not None:
            out += self._render_diff(diff)
        return "\n".join(out).rstrip() + "\n"

    @staticmethod
    def _community_section(c) -> List[str]:
        """What was checked, what could not be decided, and what only the user can mark. Never says safe, compliant or approved."""
        out = ["## Community checks", f"- {_esc(c.profile_label)} (profile v{c.profile_version}{', not reviewed by any authority' if not c.profile_reviewed else ''})"]
        out += [f"- {'Checked and passed' if k.passed else 'FAILED'}: {_esc(k.name.replace('_', ' '))}: {_esc(k.detail)}" for k in c.checks]
        for u in c.unverified:
            out.append(f"- Could not be decided: {_esc(u.name.replace('_', ' '))}: {_esc(u.detail)}. To fix: {_esc(u.repair)}")
        out.append(f"- {_esc(c.summary)}. EventMate does not verify these; each one is marked only by you.")
        for i in c.items:
            who = f" ({_esc(i.recorded_by)}, {i.recorded_on.isoformat()})" if i.recorded_by and i.recorded_on else ""
            out.append(f"  - {_esc(i.title)}: {_esc(i.instruction)} [{i.status.replace('_', ' ')}{who}]")
        return out + [""]

    @staticmethod
    def _render_diff(diff: PlanDiff) -> List[str]:
        def line(i: DiffItem, sign: str) -> str:
            if sign == "~":
                text = f"{_esc(i.key)}: {_esc(i.before or '-')} -> {_esc(i.after or '-')}"
            elif sign == "-":
                text = f"{_esc(i.key)}: {_esc(i.before or '-')}"
            else:
                text = f"{_esc(i.key)}: {_esc(i.after or '-')}"
            return f"- {text}" + (f" ({_esc(i.note)})" if i.note else "")

        out = [f"## Changes from version {diff.parent_version} to {diff.new_version}"]
        for title, items, sign in (("Added", diff.added, "+"), ("Removed", diff.removed, "-"), ("Changed", diff.changed, "~"), ("Preserved", diff.preserved, "=")):
            out.append(f"### {title} ({len(items)})")
            out += [line(i, sign) for i in items] or ["- none"]
        return out + [""]
