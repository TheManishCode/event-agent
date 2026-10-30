"""Who is asking. Identity comes from the server session, never from the model or the request body."""

from __future__ import annotations

from enum import Enum
from typing import Tuple

from pydantic import Field

from eventmate.schemas import _Model


class Role(str, Enum):
    ORGANIZER = "organizer"  # owns the plan: everything, including money and approvals
    HELPER = "helper"  # family or co-planner: sees the plan, not the money, cannot approve
    GUEST = "guest"  # public facts about the event only


class Perm(str, Enum):
    KB_READ = "kb:read"
    PLAN_PUBLIC = "plan:public"
    PLAN_READ = "plan:read"
    VENDORS_READ = "vendors:read"
    LEDGER_READ = "ledger:read"
    LEDGER_WRITE = "ledger:write"
    BOOKING_CONFIRM = "booking:confirm"
    PLAN_REPLAN = "plan:replan"
    TRACE_READ = "trace:read"
    APPROVE = "approval:decide"


ROLE_PERMS = {
    Role.ORGANIZER: frozenset(Perm),
    Role.HELPER: frozenset({Perm.KB_READ, Perm.PLAN_PUBLIC, Perm.PLAN_READ, Perm.VENDORS_READ}),
    Role.GUEST: frozenset({Perm.KB_READ, Perm.PLAN_PUBLIC}),
}


class Principal(_Model):
    user_id: str = Field(pattern=r"^[A-Za-z0-9_.@-]{1,64}$")
    roles: Tuple[Role, ...] = Field(min_length=1)

    def perms(self) -> frozenset:
        return frozenset().union(*(ROLE_PERMS[r] for r in self.roles))

    def can(self, perm: Perm) -> bool:
        return perm in self.perms()


LOCAL_ORGANIZER = Principal(user_id="local", roles=(Role.ORGANIZER,))
