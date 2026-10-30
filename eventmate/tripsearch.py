"""Multi-city trip search: order the stops, choose dates, search each distinct leg once, rank complete itineraries (ADR-030).

All prices come from the flight tool. The ranking is plain code, so every figure can be re-derived from the legs. Nothing is booked.
"""

from __future__ import annotations

import itertools
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from eventmate.schemas import FlightOption, FlightSearch, TripLeg, TripPackage, TripRequest, TripSearchResult
from eventmate.travel import MULTI_CITY_NOTE, check_date, FlightProvider, TravelError

Leg = Tuple[str, str, date]
OnCall = Callable[[Leg, bool, str, float], None]
EXTRA_PACKAGES = 4


def _plans(req: TripRequest) -> List[Tuple[Tuple[str, ...], date, Tuple[Leg, ...]]]:
    by_code = {s.code: s.nights for s in req.stops}
    orders = [tuple(s.code for s in req.stops)] if req.fixed_order else list(itertools.permutations(by_code))
    end = req.return_to or req.origin
    plans = []
    for offset in range(req.flex_days + 1):
        start = req.earliest_start + timedelta(days=offset)
        for order in orders:
            legs, here, day = [], req.origin, start
            for code in order:
                legs.append((here, code, day))
                here, day = code, day + timedelta(days=by_code[code])
            legs.append((here, end, day))
            plans.append((order, start, tuple(legs)))
    return plans


def _pick(search: FlightSearch, key) -> Optional[FlightOption]:
    priced = [o for o in search.options if o.price_inr is not None]
    return min(priced, key=key) if priced else None


def search_trip(
    provider: FlightProvider, req: TripRequest, today: date, deadline_s: float = 90.0, workers: int = 4, on_call: Optional[OnCall] = None
) -> TripSearchResult:
    check_date(req.earliest_start, today)
    plans = _plans(req)
    unique = list(dict.fromkeys(leg for _, _, legs in plans for leg in legs))
    if len(unique) > req.max_searches:
        raise ValueError(
            f"this trip needs {len(unique)} searches but the limit is {req.max_searches}; set fixed_order, remove a stop, or reduce flex_days"
        )

    results: Dict[Leg, Optional[FlightSearch]] = {}
    errors: Dict[Leg, str] = {}
    warnings: List[str] = []

    def run(leg: Leg) -> None:
        o, d, day = leg
        t0 = time.monotonic()
        try:
            res = provider.search_one_way(o, d, day, adults=req.adults, cabin=req.cabin, max_stops=req.max_stops, limit=5)
            results[leg] = res
            ok, text = True, f"{len(res.options)} option(s)"
        except (TravelError, ValueError) as exc:
            errors[leg] = str(exc)[:200]
            ok, text = False, str(exc)[:200]
        if on_call:
            on_call(leg, ok, text, (time.monotonic() - t0) * 1000)

    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = [pool.submit(run, leg) for leg in unique]
    done, pending = wait(futures, timeout=deadline_s)
    for f in pending:
        f.cancel()
    pool.shutdown(wait=False, cancel_futures=True)
    if pending:
        warnings.append(f"the search stopped at the time limit ({deadline_s:g}s); results are partial")
    used = len(results) + len(errors)

    def label(leg: Leg) -> str:
        return f"{leg[0]} to {leg[1]} on {leg[2].isoformat()}"

    for leg in unique:
        if leg in errors:
            warnings.append(f"{label(leg)} could not be searched: {errors[leg]}")
        elif leg in results and not any(o.price_inr is not None for o in results[leg].options):
            warnings.append(f"{label(leg)} has no priced flights")

    hours = req.value_per_hour_inr / 60.0
    metrics = {
        "cheapest": lambda o: (o.price_inr, o.duration_min),
        "fastest": lambda o: (o.duration_min, o.price_inr),
        "best_value": lambda o: (o.price_inr + o.duration_min * hours, o.duration_min),
    }
    candidates: Dict[str, List[TripPackage]] = {k: [] for k in metrics}
    cheapest_totals: Dict[Tuple[Tuple[str, ...], date], int] = {}
    dropped = 0
    for order, start, legs in plans:
        chosen: Dict[str, List[TripLeg]] = {k: [] for k in metrics}
        complete = True
        for leg in legs:
            res = results.get(leg)
            for k, key in metrics.items():
                opt = _pick(res, key) if res else None
                if opt is None:
                    complete = False
                    break
                chosen[k].append(TripLeg(origin=leg[0], destination=leg[1], date=leg[2], option=opt))
            if not complete:
                break
        if not complete:
            dropped += 1
            continue
        for k, tl in chosen.items():
            total = sum(x.option.price_inr for x in tl)
            minutes = sum(x.option.duration_min for x in tl)
            candidates[k].append(TripPackage(
                label=k if k in ("cheapest", "fastest", "best_value") else "alternative", order=order, start_date=start, legs=tuple(tl),
                total_price_inr=total, total_flight_minutes=minutes, value_score_inr=round(total + minutes * hours), below_median_inr=0,
            ))
            if k == "cheapest":
                cheapest_totals[(order, start)] = total

    if not cheapest_totals:
        warnings.append("no complete itinerary could be priced; widen the dates, try fixed_order, or search again later")
        return TripSearchResult(request=req, searches_planned=len(unique), searches_used=used, orderings_considered=len({p[0] for p in plans}),
                                dropped_orders=dropped, warnings=tuple(warnings), fetched_at=datetime.now(timezone.utc), note=MULTI_CITY_NOTE)

    median = int(statistics.median_low(cheapest_totals.values()))
    rank = {
        "cheapest": lambda p: (p.total_price_inr, p.total_flight_minutes, p.order, p.start_date),
        "fastest": lambda p: (p.total_flight_minutes, p.total_price_inr, p.order, p.start_date),
        "best_value": lambda p: (p.value_score_inr, p.total_price_inr, p.order, p.start_date),
    }
    top = {k: min(candidates[k], key=rank[k]) for k in metrics}
    packages = [p.model_copy(update={"below_median_inr": median - p.total_price_inr}) for p in (top["cheapest"], top["fastest"], top["best_value"])]
    seen = {(p.order, p.start_date, tuple(l.option.price_inr for l in p.legs)) for p in packages}
    extras = []
    for p in sorted(candidates["best_value"], key=rank["best_value"]):
        sig = (p.order, p.start_date, tuple(l.option.price_inr for l in p.legs))
        if sig not in seen:
            seen.add(sig)
            extras.append(p.model_copy(update={"label": "alternative", "below_median_inr": median - p.total_price_inr}))
        if len(extras) == EXTRA_PACKAGES:
            break
    return TripSearchResult(
        request=req, packages=tuple(packages + extras), searches_planned=len(unique), searches_used=used, orderings_considered=len({p[0] for p in plans}),
        dropped_orders=dropped, insights=tuple(_insights(req, cheapest_totals)), warnings=tuple(warnings), fetched_at=datetime.now(timezone.utc), note=MULTI_CITY_NOTE,
    )


def _insights(req: TripRequest, totals: Dict[Tuple[Tuple[str, ...], date], int]) -> List[str]:
    out: List[str] = []
    best_by_order: Dict[Tuple[str, ...], int] = {}
    for (order, _), t in totals.items():
        best_by_order[order] = min(t, best_by_order.get(order, t))
    if len(best_by_order) > 1:
        lo, hi = min(best_by_order, key=best_by_order.get), max(best_by_order, key=best_by_order.get)
        gap = best_by_order[hi] - best_by_order[lo]
        if gap > 0:
            out.append(f"Visiting {' then '.join(lo)} is INR {gap:,} cheaper than {' then '.join(hi)}.")
    if req.flex_days:
        (best_order, best_start), best_total = min(totals.items(), key=lambda kv: (kv[1], kv[0][1], kv[0][0]))
        base = totals.get((best_order, req.earliest_start))
        if base is not None and best_start > req.earliest_start and base > best_total:
            days = (best_start - req.earliest_start).days
            out.append(f"Starting {days} day{'s' if days != 1 else ''} later ({best_start.isoformat()}) saves INR {base - best_total:,} on the cheapest route.")
    return out
