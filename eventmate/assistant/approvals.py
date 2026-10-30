"""Human approval queue: the only way an assistant answer can change anything.

A mutating tool call from the model becomes a ``Proposal``. It is bound to the plan version it was made against, the
tool and the exact arguments (a content hash). Only a human with the approval permission, through a separate CSRF-protected
route, can apply it, and only while the plan has not changed underneath it. Approve and reject are logged either way.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from pydantic import Field

from eventmate.assistant.identity import Perm, Principal
from eventmate.assistant.trace import ProposalLog, sha
from eventmate.schemas import _Model

MAX_PENDING_PER_PLAN = 10


class ApprovalError(ValueError):
    """The decision is refused (unknown, already decided, not allowed, or the plan moved on)."""


class Proposal(_Model):
    id: str
    plan_id: str
    plan_version: int
    proposer: str
    tool: str
    args: Dict[str, Any]
    summary: str = Field(max_length=300)
    content_sha256: str
    created_at: datetime
    status: str = "pending"  # pending | approved | rejected | stale | failed
    decided_by: Optional[str] = None
    result: Optional[str] = Field(default=None, max_length=500)


def content_hash(plan_id: str, plan_version: int, tool: str, args: Dict[str, Any]) -> str:
    return sha(json.dumps([plan_id, plan_version, tool, args], sort_keys=True, default=str))


class ApprovalQueue:
    def __init__(self, log: ProposalLog):
        self.log = log
        self._items: Dict[str, Proposal] = {}
        self._lock = threading.Lock()

    def propose(self, *, plan_id: str, plan_version: int, proposer: str, tool: str, args: Dict[str, Any], summary: str) -> Proposal:
        with self._lock:
            if sum(1 for p in self._items.values() if p.plan_id == plan_id and p.status == "pending") >= MAX_PENDING_PER_PLAN:
                raise ApprovalError("too many proposals are waiting for a decision; decide on some first")
            clean = json.loads(json.dumps(args, default=str))
            digest = content_hash(plan_id, plan_version, tool, clean)
            dup = next((p for p in self._items.values() if p.status == "pending" and p.content_sha256 == digest), None)
            if dup:
                return dup  # the same request twice is one proposal
            p = Proposal(id=uuid.uuid4().hex[:12], plan_id=plan_id, plan_version=plan_version, proposer=proposer, tool=tool, args=clean,
                         summary=summary[:300], content_sha256=digest, created_at=datetime.now(timezone.utc))
            self._items[p.id] = p
        self.log.write(request_id=p.id, event="proposed", plan_id=plan_id, tool=tool, actor=proposer, content_sha256=digest, detail=summary)
        return p

    def pending(self, plan_id: str) -> List[Proposal]:
        with self._lock:
            return sorted((p for p in self._items.values() if p.plan_id == plan_id and p.status == "pending"), key=lambda p: p.created_at)

    def get(self, proposal_id: str) -> Proposal:
        with self._lock:
            if proposal_id not in self._items:
                raise ApprovalError("unknown proposal")
            return self._items[proposal_id]

    def decide(self, proposal_id: str, approver: Principal, approve: bool, latest_version: Callable[[str], int], apply: Callable[[Proposal], str], needs: Perm) -> Proposal:
        """``apply`` runs the real action and returns a short result; it is only called for a fresh, intact, approved proposal."""
        if not approver.can(Perm.APPROVE) or not approver.can(needs):
            raise ApprovalError("you are not allowed to decide this proposal")
        with self._lock:
            p = self._items.get(proposal_id)
            if p is None:
                raise ApprovalError("unknown proposal")
            if p.status != "pending":
                raise ApprovalError(f"proposal already {p.status}")
            if content_hash(p.plan_id, p.plan_version, p.tool, p.args) != p.content_sha256:
                raise ApprovalError("proposal content does not match its hash")
            status, result = ("rejected", None) if not approve else (None, None)
            if approve and latest_version(p.plan_id) != p.plan_version:
                status, result = "stale", "the plan changed after this was proposed; ask again"
            if status is None:
                try:
                    result = apply(p)
                    status = "approved"
                except Exception as exc:  # the real action refused (a hard constraint, unknown vendor...)
                    status, result = "failed", f"{type(exc).__name__}: {exc}"[:500]
            done = p.model_copy(update={"status": status, "decided_by": approver.user_id, "result": result})
            self._items[p.id] = done
        self.log.write(request_id=p.id, event=status, plan_id=p.plan_id, tool=p.tool, actor=approver.user_id, content_sha256=p.content_sha256, detail=result or "")
        return done
