"""Observation ledger: real vendor quotes with provenance, validity and deterministic quote intelligence.

An append-only JSONL file holds what the user records. Nothing here asks an LLM for a fact; the report is
computed from the latest unedited quote. Observations do not yet feed planning (ADR-025).
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from eventmate.agents.vendor import DATA_DIR, VendorRepository
from eventmate.jsonl import JsonlLog
from eventmate.schemas import ObservationSource, QuoteComparison, QuoteObservation, QuoteReport, VendorCategory

EXPIRING_DAYS = 3


class _ObservationFile(JsonlLog[QuoteObservation]):
    channel = "observation"
    model = QuoteObservation


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _load_checklists() -> Dict[str, Tuple[str, ...]]:
    doc = json.loads((DATA_DIR / "quote_checklists.json").read_text(encoding="utf-8"))
    if doc.get("schema_version") != 1:
        raise ValueError("quote_checklists.json: unsupported schema_version")
    return {k: tuple(v) for k, v in doc["checklists"].items()}


def _mentions(items: Tuple[str, ...], wanted: str) -> bool:
    w = _norm(wanted)
    return any(w in _norm(i) or _norm(i) in w for i in items)


class ObservationStore:
    def __init__(self, root: Path, repo: VendorRepository):
        self.root = Path(root) / "observations"
        self.repo = repo
        self.checklists = _load_checklists()
        self.external_lookup: Callable[[str], Optional[object]] = lambda vendor_id: None  # set by EventMate: finds a discovered or test business by its ext_ id

    def _file(self) -> _ObservationFile:
        return _ObservationFile(self.root, run_id="all")

    def add_quote(self, vendor_id: str, category: VendorCategory, **fields) -> QuoteObservation:
        if vendor_id.startswith("ext_"):  # a discovered or test business: its own record, never a seed vendor
            biz = self.external_lookup(vendor_id)
            if biz is None:
                raise ValueError(f"unknown vendor {vendor_id!r}")
            if biz.category != category:
                raise ValueError(f"{vendor_id} is a {biz.category.value}, not a {category.value}")
            record = biz
        else:
            record = self.repo.get(category, vendor_id)
        if record is None:
            owner = next((c for c in VendorCategory if self.repo.get(c, vendor_id)), None)
            if owner is None:
                raise ValueError(f"unknown vendor {vendor_id!r}")
            raise ValueError(f"{vendor_id} is a {owner.value}, not a {category.value}")
        if fields.get("basis") == "per_plate" and category != VendorCategory.CATERER:
            raise ValueError("a per_plate quote only applies to a caterer")
        obs = QuoteObservation(observation_id=uuid.uuid4().hex, vendor_id=vendor_id, category=category, **fields)
        return self._file()._append(obs, obs.observation_id)

    def quotes(self, vendor_id: str) -> List[QuoteObservation]:
        return [o for o in self._file().read() if o.vendor_id == vendor_id]

    def pricing_quote(self, vendor_id: str) -> Optional[QuoteObservation]:
        """The latest quote that may set a price. A public listing never does (ADR-025/026)."""
        usable = [o for o in self.quotes(vendor_id) if o.source_type != ObservationSource.PUBLIC_LISTING]
        return usable[-1] if usable else None

    def vendor_confirmed(self, vendor_id: str) -> bool:
        """True if a vendor_confirmation observation (which always carries an evidence_ref) exists for this vendor."""
        return any(o.source_type == ObservationSource.VENDOR_CONFIRMATION for o in self.quotes(vendor_id))

    def report(self, vendor_id: str, today: date) -> Optional[QuoteReport]:
        quotes = self.quotes(vendor_id)
        return self._analyze(quotes[-1], today) if quotes else None  # file order is chronological

    def latest_with_reports(self, category: VendorCategory, today: date) -> List[Tuple[QuoteObservation, QuoteReport]]:
        """The latest quote of every vendor in a category with its report, ordered by vendor id."""
        latest: Dict[str, QuoteObservation] = {}
        for o in self._file().read():
            if o.category == category:
                latest[o.vendor_id] = o
        return [(o, self._analyze(o, today)) for _, o in sorted(latest.items())]

    def compare(self, category: VendorCategory, today: date) -> QuoteComparison:
        latest: Dict[str, QuoteObservation] = {}
        for o in self._file().read():
            if o.category == category:
                latest[o.vendor_id] = o
        reports = [self._analyze(o, today) for o in latest.values()]
        live = sorted((r for r in reports if r.status != "expired"), key=lambda r: (r.effective_total, r.vendor_id))
        dead = sorted((r for r in reports if r.status == "expired"), key=lambda r: r.vendor_id)
        return QuoteComparison(category=category, ranked=tuple(live), expired=tuple(dead))

    def _analyze(self, o: QuoteObservation, today: date) -> QuoteReport:
        total = o.amount_inr * o.guest_count if o.basis == "per_plate" else o.amount_inr
        known = sum(h.amount_inr for h in o.hidden_costs if h.amount_inr)
        unknown = tuple(h.label for h in o.hidden_costs if not h.amount_inr)
        expected = self.checklists.get(o.category.value, ())
        excluded = tuple(e for e in expected if _mentions(o.exclusions, e))
        missing = tuple(e for e in expected if e not in excluded and not _mentions(o.inclusions, e))
        if today > o.expires_at:
            status = "expired"
        elif today >= o.expires_at - timedelta(days=EXPIRING_DAYS):
            status = "expiring"
        else:
            status = "valid"
        flags = []
        if status == "expired":
            flags.append(f"re-quote needed: the quote expired on {o.expires_at.isoformat()}")
        elif status == "expiring":
            flags.append(f"quote expires on {o.expires_at.isoformat()}; confirm or re-quote soon")
        flags += [f"unquantified hidden cost: {label}; ask the vendor for the amount" for label in unknown]
        flags += [f"not included: {e}; budget for it separately" for e in excluded]
        flags += [f"not mentioned in the quote: {m}; ask the vendor" for m in missing]
        return QuoteReport(
            vendor_id=o.vendor_id,
            category=o.category,
            observation_id=o.observation_id,
            source_type=o.source_type,
            confidence=o.confidence,
            expires_at=o.expires_at,
            status=status,
            quoted_total=total,
            known_hidden_total=known,
            effective_total=total + known,
            unquantified_hidden=unknown,
            missing_items=missing,
            excluded_items=excluded,
            flags=tuple(flags),
        )
