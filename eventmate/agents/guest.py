"""Guest Agent: headcount, RSVPs, dietary needs and capacity.

``planning_headcount`` is always the user-stated guest count (a user-provided
fact). RSVP data from the guest database refines it into confirmed / declined /
pending, adds dietary needs that the caterer must cover, and flags guest lists
that contradict the stated headcount. Nothing here is guessed.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Dict, Optional, Tuple

from pydantic import ValidationError

from eventmate.decision_logger import DecisionLogger
from eventmate.schemas import (
    SOURCE_PATTERN,
    RSVP,
    AgentName,
    ConstraintCheck,
    DecisionType,
    DietaryRequirement,
    EventRequest,
    Guest,
    GuestSummary,
    _Model,
)

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "seed_guests.json"
MAX_DATA_BYTES = 5_000_000
_LIST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class GuestDataError(Exception):
    """Guest data is malformed or the requested list does not exist."""


class _GuestList(_Model):
    list_id: str
    source: str
    guests: Tuple[Guest, ...]


class GuestRepository:
    """Read-only, validated view over the guest seed file."""

    def __init__(self, path: Optional[Path] = None):
        path = Path(path) if path else DATA_PATH
        try:
            if path.stat().st_size > MAX_DATA_BYTES:
                raise GuestDataError(f"{path.name} is too large")
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GuestDataError(f"cannot read {path.name}: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("schema_version") != 1 or not isinstance(doc.get("records"), list):
            raise GuestDataError(f"{path.name}: unsupported structure")
        lists = {}
        try:
            for rec in doc["records"]:
                gl = _GuestList.model_validate(rec)
                if not _LIST_ID.fullmatch(gl.list_id) or gl.list_id in lists:
                    raise GuestDataError(f"invalid or duplicate list_id {gl.list_id!r}")
                if not SOURCE_PATTERN.fullmatch(gl.source):
                    raise GuestDataError(f"invalid source on {gl.list_id}")
                ids = [g.id for g in gl.guests]
                if len(set(ids)) != len(ids):
                    raise GuestDataError(f"duplicate guest ids in {gl.list_id}")
                lists[gl.list_id] = gl
        except ValidationError as exc:
            raise GuestDataError(f"invalid record: {exc}") from exc
        self._lists: Dict[str, _GuestList] = lists

    def get(self, list_id: str) -> _GuestList:
        if not isinstance(list_id, str) or not _LIST_ID.fullmatch(list_id) or list_id not in self._lists:
            raise GuestDataError(f"unknown guest list {list_id!r}")
        return self._lists[list_id]

    def list_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._lists))


class GuestAgent:
    agent_name = AgentName.GUEST

    def __init__(self, repo: GuestRepository, logger: DecisionLogger):
        self.repo = repo
        self.logger = logger

    def summarize(self, request: EventRequest, guest_list_id: Optional[str] = None) -> GuestSummary:
        headcount = request.guest_count
        if guest_list_id is None:
            summary = GuestSummary(
                planning_headcount=headcount,
                confirmed=0,
                declined=0,
                pending=headcount,
                required_diets=tuple(request.dietary),
                checks=(
                    ConstraintCheck(name="rsvp_within_headcount", passed=True, detail="no guest list supplied; using the stated headcount"),
                ),
                guest_list_id=None,
                source="user_input",
            )
        else:
            gl = self.repo.get(guest_list_id)
            confirmed = sum(g.party_size for g in gl.guests if g.rsvp == RSVP.YES)
            declined = sum(g.party_size for g in gl.guests if g.rsvp == RSVP.NO)
            listed_pending = sum(g.party_size for g in gl.guests if g.rsvp == RSVP.PENDING)
            unlisted = max(0, headcount - confirmed - declined - listed_pending)
            pending = listed_pending + unlisted
            counts: Counter = Counter()
            for g in gl.guests:
                if g.rsvp != RSVP.NO:
                    counts.update(d.value for d in g.dietary)
            required = tuple(
                dict.fromkeys(list(request.dietary) + [DietaryRequirement(d) for d in sorted(counts)])
            )
            over = confirmed + listed_pending - headcount
            summary = GuestSummary(
                planning_headcount=headcount,
                confirmed=confirmed,
                declined=declined,
                pending=pending,
                dietary_counts=dict(counts),
                required_diets=required,
                checks=(
                    ConstraintCheck(
                        name="rsvp_within_headcount",
                        passed=over <= 0,
                        detail=f"{confirmed} confirmed + {listed_pending} listed pending vs stated headcount {headcount}"
                        + ("" if over <= 0 else f"; {over} too many: raise the guest count or prune the list"),
                    ),
                ),
                guest_list_id=guest_list_id,
                source=gl.source,
            )
        self._log(request, summary)
        return summary

    @staticmethod
    def capacity_check(summary: GuestSummary, venue_capacity: int) -> ConstraintCheck:
        need = summary.planning_headcount
        return ConstraintCheck(
            name="guest_capacity",
            passed=need <= venue_capacity,
            detail=f"{need} guests vs venue capacity {venue_capacity}",
        )

    @staticmethod
    def dietary_check(summary: GuestSummary, caterer_options: Tuple[DietaryRequirement, ...]) -> ConstraintCheck:
        missing = [d for d in summary.required_diets if d not in caterer_options]
        return ConstraintCheck(
            name="guest_dietary",
            passed=not missing,
            detail="caterer covers every guest diet" if not missing else "caterer does not offer: " + ", ".join(d.value for d in missing),
        )

    def _log(self, request: EventRequest, summary: GuestSummary) -> None:
        failed = [c for c in summary.checks if not c.passed]
        self.logger.log(
            request_id=request.request_id,
            agent=self.agent_name,
            decision_type=DecisionType.ASSESSMENT,
            summary=f"Headcount {summary.planning_headcount}: {summary.confirmed} confirmed, {summary.declined} declined, {summary.pending} pending",
            constraints_checked=summary.checks,
            justification=(
                "Required diets for the caterer: " + (", ".join(d.value for d in summary.required_diets) or "none")
                + ". Planning headcount is the user-stated count; RSVPs only refine it."
                + ("" if not failed else " Problem: " + "; ".join(c.detail for c in failed))
            ),
            confidence=1.0 if summary.source != "user_input" else 0.7,
            source=summary.source,
        )
