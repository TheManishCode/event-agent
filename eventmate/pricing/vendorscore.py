"""F10 vendor score: a published formula over recorded quotes (ADR-040).

Three components, each 0-100, weights below. A component with too little data is left out and the overall is
re-weighted over what exists; `coverage` says how much of the formula that was. Response time, terms and ratings
are named in `not_scored` because the data model holds none of them. Nothing here calls a model.
"""

from __future__ import annotations

from datetime import date
from statistics import median
from typing import Dict, List, Optional

from eventmate.pricing.observations import ObservationStore
from eventmate.schemas import (
    ObservationSource, QuoteObservation, QuoteReport, ScoreComponent, UnscoredVendor, VendorCategory, VendorScore,
    VendorScoreboard,
)

WEIGHTS = {"price": 0.5, "completeness": 0.3, "evidence": 0.2}
MIN_COMPARABLE = 3
PRICE_BEST_RATIO = 0.8  # at or below 80% of the median unit price scores 100
PRICE_WORST_RATIO = 1.5  # at or above 150% of the median scores 0
LOW_RATIO = 0.5  # below half the median is flagged, not trusted
EVIDENCE_BASE = {
    ObservationSource.VENDOR_CONFIRMATION: 100,
    ObservationSource.VENDOR_QUOTE: 80,
    ObservationSource.USER_INPUT: 50,
    ObservationSource.PUBLIC_LISTING: 20,
}
NOT_SCORED = ("response time", "terms and cancellation", "ratings")


def _unit_price(o: QuoteObservation, r: QuoteReport) -> float:
    """What one comparable unit costs: per plate (hidden costs spread over the guests) or the whole total."""
    return r.effective_total / o.guest_count if o.basis == "per_plate" else float(r.effective_total)


def _price(o: QuoteObservation, unit: float, medians: Dict[str, float], counts: Dict[str, int], notes: List[str]) -> ScoreComponent:
    w = WEIGHTS["price"]
    if o.source_type == ObservationSource.PUBLIC_LISTING:
        return ScoreComponent(name="price", weight=w, detail="an advertised price, not a quote: it is not compared")
    if counts.get(o.basis, 0) < MIN_COMPARABLE:
        return ScoreComponent(
            name="price", weight=w,
            detail=f"needs {MIN_COMPARABLE} current {o.basis.replace('_', ' ')} quotes to compare; has {counts.get(o.basis, 0)}",
        )
    ratio = unit / medians[o.basis]
    score = round(100 * (PRICE_WORST_RATIO - ratio) / (PRICE_WORST_RATIO - PRICE_BEST_RATIO))
    if ratio < LOW_RATIO:
        notes.append("unusually low price: check what the quote leaves out before relying on it")
    return ScoreComponent(
        name="price", weight=w, score=max(0, min(100, score)),
        detail=f"{ratio:.0%} of the median {o.basis.replace('_', ' ')} price among {counts[o.basis]} current quotes, hidden costs included",
    )


def _completeness(r: QuoteReport, expected: int) -> ScoreComponent:
    gaps = len(r.missing_items) + len(r.excluded_items)
    denominator = expected + len(r.unquantified_hidden)
    score = 100 if denominator == 0 else round(100 * (expected - gaps) / denominator)
    parts = [f"{expected - gaps} of {expected} checklist items covered"]
    if r.unquantified_hidden:
        parts.append("unquantified hidden costs: " + ", ".join(r.unquantified_hidden))
    return ScoreComponent(name="completeness", weight=WEIGHTS["completeness"], score=max(0, min(100, score)), detail="; ".join(parts))


def _evidence(o: QuoteObservation) -> ScoreComponent:
    base = EVIDENCE_BASE[o.source_type]
    return ScoreComponent(
        name="evidence", weight=WEIGHTS["evidence"], score=round(base * o.confidence),
        detail=f"{o.source_type.value.replace('_', ' ')} ({base}) at confidence {o.confidence:.0%}",
    )


def score_category(store: ObservationStore, category: VendorCategory, today: date) -> VendorScoreboard:
    pairs = store.latest_with_reports(category, today)
    live = [(o, r) for o, r in pairs if r.status != "expired"]
    unscored = tuple(
        UnscoredVendor(vendor_id=o.vendor_id, reason=f"re-quote needed: the quote expired on {o.expires_at.isoformat()}")
        for o, r in pairs if r.status == "expired"
    )
    units: Dict[str, List[float]] = {}
    for o, r in live:
        if o.source_type != ObservationSource.PUBLIC_LISTING:
            units.setdefault(o.basis, []).append(_unit_price(o, r))
    medians = {b: median(v) for b, v in units.items()}
    counts = {b: len(v) for b, v in units.items()}
    expected = len(store.checklists.get(category.value, ()))
    scores = []
    for o, r in live:
        notes: List[str] = []
        if r.status == "expiring":
            notes.append(f"quote expires on {o.expires_at.isoformat()}; confirm or re-quote soon")
        components = (_price(o, _unit_price(o, r), medians, counts, notes), _completeness(r, expected), _evidence(o))
        have = [c for c in components if c.score is not None]
        weight = sum(c.weight for c in have)
        overall: Optional[int] = round(sum(c.weight * c.score for c in have) / weight) if weight else None
        scores.append(VendorScore(
            vendor_id=o.vendor_id, category=category, observation_id=o.observation_id, status=r.status, overall=overall,
            coverage=round(weight, 4), components=components, not_scored=NOT_SCORED, notes=tuple(notes),
        ))
    scores.sort(key=lambda s: (-(s.overall if s.overall is not None else -1), s.vendor_id))
    return VendorScoreboard(category=category, scores=tuple(scores), unscored=unscored)
