"""Budget ledger: planned / quoted / paid / pending / variance per category.

An append-only JSONL file per plan holds what the user reports (quotes, payments, refunds). The summary is
recomputed from the file and the latest plan version, so a replan never loses payment history. The ledger
records money; it never initiates any payment.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from eventmate.jsonl import JsonlLog
from eventmate.schemas import LedgerEntry, LedgerKind, LedgerLine, LedgerSummary, PlanVersion, VendorCategory


class _LedgerFile(JsonlLog[LedgerEntry]):
    channel = "ledger"
    model = LedgerEntry


class Ledger:
    def __init__(self, root: Path):
        self.root = Path(root) / "ledger"

    def _file(self, plan_id: str) -> _LedgerFile:
        return _LedgerFile(self.root, run_id=plan_id)  # run_id pattern == plan id pattern, so path-safe

    def entries(self, plan_id: str) -> List[LedgerEntry]:
        return self._file(plan_id).read()

    def add(self, plan: PlanVersion, kind: str, category: VendorCategory, amount: int, note: str = "") -> LedgerEntry:
        selection = plan.selection_for(category)
        if selection is None:
            raise ValueError(f"{category.value} is not part of this plan")
        log = self._file(plan.plan_id)
        entry = LedgerEntry(
            entry_id=uuid.uuid4().hex,
            timestamp=datetime.now(timezone.utc),
            plan_id=plan.plan_id,
            kind=LedgerKind(kind),
            category=category,
            amount=amount,
            note=note,
        )
        if entry.kind == LedgerKind.REFUND:
            paid = sum(e.amount if e.kind == LedgerKind.PAYMENT else -e.amount for e in log.read() if e.category == category and e.kind != LedgerKind.QUOTE)
            if entry.amount > paid:
                raise ValueError(f"refund of INR {entry.amount:,} exceeds the INR {paid:,} paid to the {category.value}")
        return log._append(entry, entry.entry_id)

    def summarize(self, plan: PlanVersion) -> LedgerSummary:
        entries = self.entries(plan.plan_id)
        lines = []
        for s in plan.selections:
            mine = [e for e in entries if e.category == s.category]
            quotes = [e.amount for e in mine if e.kind == LedgerKind.QUOTE]
            quoted = quotes[-1] if quotes else None  # file order is chronological
            paid = max(0, sum(e.amount if e.kind == LedgerKind.PAYMENT else -e.amount for e in mine if e.kind != LedgerKind.QUOTE))
            committed = quoted if quoted is not None else s.cost
            lines.append(
                LedgerLine(
                    category=s.category,
                    vendor_name=s.name,
                    planned=s.cost,
                    quoted=quoted,
                    committed=committed,
                    paid=paid,
                    pending=max(0, committed - paid),
                    overpaid=max(0, paid - committed),
                    variance=committed - s.cost,
                )
            )
        planned = {s.category for s in plan.selections}
        orphaned = {}
        for e in entries:
            if e.category not in planned and e.kind != LedgerKind.QUOTE:
                orphaned[e.category.value] = orphaned.get(e.category.value, 0) + (e.amount if e.kind == LedgerKind.PAYMENT else -e.amount)
        orphaned = {c: n for c, n in orphaned.items() if n > 0}
        committed_total = sum(l.committed for l in lines)
        return LedgerSummary(
            plan_id=plan.plan_id,
            plan_version=plan.version,
            budget_total=plan.request.budget_total,
            lines=tuple(lines),
            planned_total=sum(l.planned for l in lines),
            quoted_total=sum(l.quoted or 0 for l in lines),
            committed_total=committed_total,
            paid_total=sum(l.paid for l in lines),
            pending_total=sum(l.pending for l in lines),
            variance_total=sum(l.variance for l in lines),
            headroom=plan.request.budget_total - committed_total,
            orphaned_paid=orphaned,
        )
