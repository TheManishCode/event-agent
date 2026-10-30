"""EventMate command line.

    python -m eventmate plan "<request>" [--guest-list ID]
    python -m eventmate confirm PLAN_ID venue|caterer|photographer
    python -m eventmate replan PLAN_ID (--cancel-vendor ID | --budget N | --guests N | --date YYYY-MM-DD) [--override-confirmed ID ...]
    python -m eventmate show PLAN_ID [--version N] [--json]
    python -m eventmate diff PLAN_ID OLD NEW
    python -m eventmate validate PLAN_ID [--version N]
    python -m eventmate list
    python -m eventmate intake "<request>"          # milestone-1 view: no plan is produced
    python -m eventmate tradeoffs "<request>" [--apply N] [--json]   # verified ways out of a failing request
    python -m eventmate ledger <plan_id> [--payment CATEGORY AMOUNT]   # budget ledger (money you report)
    python -m eventmate flights ORIGIN DEST YYYY-MM-DD [--limit N] [--json]   # indicative fares and schedules (fli); nothing is booked
    python -m eventmate deals {caterer|venue|photographer|hotel} CITY [--guests N] [--nights N] [--budget INR] [--limit N] [--json]   # web agent: advertised prices, best first (leads, not quotes)
    python -m eventmate weather CITY YYYY-MM-DD [--json]   # OpenWeather forecast (needs OPENWEATHER_API_KEY in .env); beyond ~5 days it says so
    python -m eventmate discover CITY [--kind venue|hotel|restaurant] [--limit N] [--json]   # OpenStreetMap places: candidates only
    python -m eventmate multicity LEG LEG... [--json]   # LEG = ORIGIN,DEST,YYYY-MM-DD; built from separate one-way searches
    python -m eventmate outreach PLAN_ID CATEGORY {email|whatsapp} RECIPIENT --name NAME [--json]   # write a quote request and a link; you send it, nothing is sent
    python -m eventmate jobs [show|start|approve|confirm|reject|resolve|nudge|cancel|tick] ...   # follow-through jobs; `python -m eventmate.followthrough.worker --every 60` runs them
    python -m eventmate inbox [show|poll|approve|deny|autoreply|kill|resume] [PLAN_ID] [DRAFT_ID --hash H] [--watch SECONDS]   # the agent sends what you approve and reads replies
    python -m eventmate quotes {show VENDOR_ID | compare CATEGORY | score CATEGORY} [--json]   # recorded vendor quotes (record them via the web API)

Exit codes: 0 ok, 2 bad input / unknown plan, 3 needs repair or a user decision.
Nothing here sends email, makes payments or books a vendor.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from eventmate.agents.manager import RequestParseError
from eventmate.agents.guest import GuestDataError
from eventmate.agents.vendor import DataIntegrityError
from eventmate.env import load_env
from eventmate.llm import client_from_env
from eventmate.plan_store import PlanIntegrityError
from eventmate.schemas import Disruption, DisruptionKind, VendorCategory
from eventmate.service import EventMate
from eventmate.integrations.discovery import DiscoveryError
from eventmate.integrations.travel import TravelError, check_date
from eventmate.integrations.weather import WeatherError

COMMANDS = {"jobs", "inbox", "outreach", "plan", "confirm", "replan", "show", "diff", "validate", "list", "intake", "tradeoffs", "ledger", "quotes", "flights", "multicity", "discover", "weather", "deals"}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="eventmate", description="Multi-agent event planner")
    ap.add_argument("--today", type=date.fromisoformat, default=None, help="override today's date (testing)")
    ap.add_argument("--log-dir", type=Path, default=None)
    ap.add_argument("--plan-dir", type=Path, default=None)
    ap.add_argument("--llm", action="store_true", help="let an LLM reword explanations (needs EVENTMATE_ENABLE_LLM=1 and an API key in the environment)")
    ap.add_argument("--allow-no-symbolic", action="store_true", help="plan even if Fast Downward/VAL are not installed")
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("plan", "intake"):
        p = sub.add_parser(name)
        p.add_argument("request")
        p.add_argument("--guest-list", default=None)
        p.add_argument("--json", action="store_true")
    p = sub.add_parser("tradeoffs")
    p.add_argument("request")
    p.add_argument("--guest-list", default=None)
    p.add_argument("--apply", type=int, default=None, metavar="N", help="apply option N (1-based) and store the resulting plan")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("ledger", help="budget ledger: money you report (quotes, payments, refunds); nothing is ever paid by EventMate")
    p.add_argument("plan_id")
    g = p.add_mutually_exclusive_group()
    for kind in ("quote", "payment", "refund"):
        g.add_argument(f"--{kind}", nargs=2, metavar=("CATEGORY", "AMOUNT_INR"), help=f"record a {kind}")
    p.add_argument("--note", default="")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("flights", help="indicative fares and schedules from Google Flights through fli; EventMate never books")
    p.add_argument("origin")
    p.add_argument("destination")
    p.add_argument("date", type=date.fromisoformat)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("deals", help="a web agent searches, reads pages safely and ranks advertised prices; leads only, not quotes")
    p.add_argument("kind", choices=["caterer", "venue", "photographer", "hotel"])
    p.add_argument("city")
    p.add_argument("--guests", type=int, default=None)
    p.add_argument("--nights", type=int, default=None, help="hotels: turns the nightly price into a total")
    p.add_argument("--budget", type=int, default=None, help="INR; flags offers whose estimated total fits")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("weather", help="forecast for a city and date from OpenWeather; nothing is estimated beyond its horizon")
    p.add_argument("city")
    p.add_argument("date", type=date.fromisoformat)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("discover", help="places mapped on OpenStreetMap (Overpass): candidates to ask about, no prices or availability")
    p.add_argument("city")
    p.add_argument("--kind", default="venue", choices=["venue", "hotel", "restaurant"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("multicity", help="several legs, each searched separately (Google does not serve multi-city to fli)")
    p.add_argument("leg", nargs="+", help="ORIGIN,DEST,YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("quotes", help="read the vendor quotes you recorded: validity, hidden costs, missing items")
    p.add_argument("what", choices=["show", "compare", "score"])
    p.add_argument("target", help="a vendor id for show, a category for compare and score")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("outreach", help="write a quote request for a vendor in a plan and print a mailto:/WhatsApp link; you send it yourself, nothing is sent")
    p.add_argument("plan_id")
    p.add_argument("category", choices=[c.value for c in VendorCategory])
    p.add_argument("channel", choices=["email", "whatsapp"])
    p.add_argument("recipient", help="the vendor's email address or WhatsApp number (+country code)")
    p.add_argument("--name", required=True, help="your name, signed at the end of the message")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("inbox", help="the vendor conversations: send what you approved, read replies, show what needs you")
    p.add_argument("what", choices=["show", "poll", "approve", "deny", "autoreply", "kill", "resume"], nargs="?", default="show")
    p.add_argument("plan_id", nargs="?")
    p.add_argument("draft_id", nargs="?")
    p.add_argument("--hash", help="content hash of the draft as shown by `inbox show` (approve only)")
    p.add_argument("--watch", type=int, metavar="SECONDS", help="with poll: keep polling every SECONDS until interrupted")
    p.add_argument("--name", help="autoreply: your name, signed on automatic messages (giving it grants the permission)")
    p.add_argument("--hours", type=int, default=72, help="autoreply: how long the permission lasts (1-168)")
    p.add_argument("--max-per-thread", type=int, default=3, help="autoreply: automatic messages per vendor (1-5)")
    p.add_argument("--max-total", type=int, default=10, help="autoreply: automatic messages per plan (1-30)")
    p.add_argument("--off", action="store_true", help="autoreply: revoke the permission")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("jobs", help="follow-through jobs: get N comparable quotes by a deadline; the worker sends what you approve, reads replies, nudges, and asks you")
    p.add_argument("what", choices=["show", "start", "approve", "confirm", "reject", "resolve", "nudge", "cancel", "tick"], nargs="?", default="show")
    p.add_argument("target", nargs="?", help="show/start: PLAN_ID; the others: JOB_ID")
    p.add_argument("extra", nargs="?", help="start: CATEGORY; confirm/reject: CLAIM_ID; resolve/nudge: ITEM_ID")
    p.add_argument("--vendors", help="start: comma-separated vendor ids that have saved contacts")
    p.add_argument("--name", help="start: your name, signed on the requests")
    p.add_argument("--goal", help="start: what you want, in a sentence (default: N quotes)")
    p.add_argument("--min", type=int, default=3, dest="min_quotes")
    p.add_argument("--cap", type=int, help="start: quotes above this total in INR do not count")
    p.add_argument("--days", type=int, default=7, help="start: deadline in days")
    p.add_argument("--valid-days", type=int, help="confirm: how many days (1-90) to treat the price as valid; the vendor's reply rarely says")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("confirm")
    p.add_argument("plan_id")
    p.add_argument("category", choices=[c.value for c in VendorCategory])
    p = sub.add_parser("replan")
    p.add_argument("plan_id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--cancel-vendor")
    g.add_argument("--budget", type=int)
    g.add_argument("--guests", type=int)
    g.add_argument("--date", type=date.fromisoformat)
    p.add_argument("--override-confirmed", nargs="*", default=[])
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("plan_id")
    p.add_argument("--version", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("diff")
    p.add_argument("plan_id")
    p.add_argument("old", type=int)
    p.add_argument("new", type=int)
    p = sub.add_parser("validate")
    p.add_argument("plan_id")
    p.add_argument("--version", type=int)
    sub.add_parser("list")
    return ap


def _disruption(a) -> Disruption:
    over = tuple(a.override_confirmed)
    if a.cancel_vendor:
        return Disruption(kind=DisruptionKind.VENDOR_CANCELLATION, vendor_id=a.cancel_vendor, override_confirmed=over)
    if a.budget is not None:
        return Disruption(kind=DisruptionKind.BUDGET_CHANGE, new_budget=a.budget, override_confirmed=over)
    if a.guests is not None:
        return Disruption(kind=DisruptionKind.GUEST_COUNT_CHANGE, new_guest_count=a.guests, override_confirmed=over)
    return Disruption(kind=DisruptionKind.DATE_CHANGE, new_date=a.date, override_confirmed=over)


def _print_violations(violations) -> None:
    for v in violations:
        print(f"  - {v.constraint}: {v.detail}\n    repair: {v.repair}", file=sys.stderr)


_GLOBAL_OPTIONS_WITH_VALUE = {"--today", "--log-dir", "--plan-dir"}


def _first_positional_index(argv):
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBAL_OPTIONS_WITH_VALUE:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return i
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    idx = _first_positional_index(argv)
    if idx is not None and argv[idx] not in COMMANDS:
        argv.insert(idx, "intake")  # legacy form: python -m eventmate "<request>"
    a = _parser().parse_args(argv)
    try:
        em = EventMate(
            log_dir=a.log_dir,
            plan_dir=a.plan_dir,
            llm=(load_env(), client_from_env())[1] if a.llm else None,
            require_symbolic=not a.allow_no_symbolic,
        )
        return _run(em, a)
    except (RequestParseError, ValueError, ValidationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:  # missing plan, read-only disk, permissions, lost write race
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (PlanIntegrityError, DataIntegrityError, GuestDataError) as exc:
        print(f"integrity error: {exc}", file=sys.stderr)
        return 2


def _run(em: EventMate, a) -> int:
    if a.command == "intake":
        res = em.manager.run_intake(a.request, today=a.today, guest_list_id=a.guest_list)
        print(res.model_dump_json(indent=2))
        return 0 if res.status == "intake_complete" else 3
    if a.command == "plan":
        out = em.plan(a.request, today=a.today, guest_list_id=a.guest_list)
        _print_reality(out.intake.reality)
        if out.plan is None:
            print("No plan: hard constraints cannot be met.", file=sys.stderr)
            _print_violations(out.violations)
            return 3
        if a.json:
            print(out.plan.model_dump_json(indent=2))
        else:
            print(em.explain(out.plan.plan_id).markdown)
        print(f"plan id: {out.plan.plan_id} (version {out.plan.version})", file=sys.stderr)
        return 0
    if a.command == "tradeoffs":
        return _tradeoffs(em, a)
    if a.command == "ledger":
        return _ledger(em, a)
    if a.command == "quotes":
        return _quotes(em, a)
    if a.command == "outreach":
        return _outreach(em, a)
    if a.command == "inbox":
        return _inbox(em, a)
    if a.command == "jobs":
        return _jobs(em, a)
    if a.command in ("flights", "multicity"):
        return _travel(em, a)
    if a.command == "discover":
        return _discover(em, a)
    if a.command == "weather":
        return _weather(em, a)
    if a.command == "deals":
        return _deals(em, a)
    if a.command == "confirm":
        out = em.confirm(a.plan_id, VendorCategory(a.category))
        return _print_replan(em, out, False)
    if a.command == "replan":
        out = em.replan(a.plan_id, _disruption(a), a.today)
        return _print_replan(em, out, a.json)
    if a.command == "show":
        if a.json:
            print(em.store.load(a.plan_id, a.version).model_dump_json(indent=2))
        else:
            print(em.explain(a.plan_id, a.version).markdown)
        return 0
    if a.command == "diff":
        print(em.diff(a.plan_id, a.old, a.new).model_dump_json(indent=2))
        return 0
    if a.command == "validate":
        report = em.revalidate(a.plan_id, a.version)
        print(f"{'PASSED' if report.passed else 'FAILED'}: {sum(c.passed for c in report.checks)}/{len(report.checks)} checks")
        if report.passed:
            v = em.verification(a.plan_id, a.version)
            print(("VERIFIED" if v["state"] == "verified" else "NEEDS VERIFICATION") + ": " + v["label"] + "".join(f"\n  - {i['detail']}. To resolve: {i['repair']}" for i in v["items"]))
        _print_violations(report.violations)
        return 0 if report.passed else 3
    for pid in em.store.list_plans():
        latest = em.store.latest(pid)
        print(f"{pid}\tv{latest.version}\t{latest.request.event_type.value}\t{latest.request.city}\t{latest.request.event_date}")
    return 0


def _print_reality(rc) -> None:
    if rc is not None:  # stderr: stdout stays the plan itself
        print(f"Budget reality check: {rc.message}\n  ({rc.basis})", file=sys.stderr)


def _ledger(em: EventMate, a) -> int:
    for kind in ("quote", "payment", "refund"):
        args = getattr(a, kind)
        if args:
            em.ledger_add(a.plan_id, kind, VendorCategory(args[0]), int(args[1]), a.note)
            print(f"recorded {kind} of INR {int(args[1]):,} for the {args[0]}")
    s = em.ledger_summary(a.plan_id)
    if a.json:
        entries = [json.loads(e.model_dump_json()) for e in em.ledger_entries(a.plan_id)]
        print(json.dumps({"summary": json.loads(s.model_dump_json()), "entries": entries}, indent=2))
        return 0
    print(f"{'category':14}{'planned':>12}{'quoted':>12}{'paid':>12}{'pending':>12}{'variance':>12}")
    for l in s.lines:
        quoted = f"{l.quoted:,}" if l.quoted is not None else "-"
        print(f"{l.category.value:14}{l.planned:>12,}{quoted:>12}{l.paid:>12,}{l.pending:>12,}{l.variance:>+12,}")
    print(f"{'total':14}{s.planned_total:>12,}{s.quoted_total:>12,}{s.paid_total:>12,}{s.pending_total:>12,}{s.variance_total:>+12,}")
    print(f"budget INR {s.budget_total:,}; committed INR {s.committed_total:,}; headroom INR {s.headroom:,}")
    return 0


def _fare(o) -> str:
    first, last = o.segments[0], o.segments[-1]
    price = f"INR {o.price_inr:,}" if o.price_inr is not None else "no INR price"
    stops = "non-stop" if o.stops == 0 else f"{o.stops} stop(s)"
    return f"  {price:>12}  {first.airline_name} {first.flight_number}  {first.from_code} {first.departs:%H:%M} -> {last.to_code} {last.arrives:%H:%M}  {stops}, {o.duration_min // 60}h{o.duration_min % 60:02d}"


def _travel(em: EventMate, a) -> int:
    try:
        today = a.today or date.today()
        for day in ([a.date] if a.command == "flights" else [date.fromisoformat(x.split(",")[2]) for x in a.leg if x.count(",") == 2]):
            check_date(day, today)
        if a.command == "flights":
            res = em.search_flights(a.origin, a.destination, a.date, limit=a.limit)
            legs = [res]
        else:
            parts = [x.split(",") for x in a.leg]
            if any(len(p) != 3 for p in parts):
                print("each leg must be ORIGIN,DEST,YYYY-MM-DD", file=sys.stderr)
                return 2
            res = em.search_multi_city([(o, d, date.fromisoformat(day)) for o, d, day in parts], limit=a.limit)
            legs = list(res.legs)
    except (ValueError, TravelError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(res.model_dump_json(indent=2))
        return 0
    for leg in legs:
        print(f"{leg.origin} -> {leg.destination} on {leg.travel_date.isoformat()}" + (f"  (failed: {leg.error})" if leg.error else ""))
        for o in leg.options:
            print(_fare(o))
        for w in leg.warnings:
            print(f"  note: {w}", file=sys.stderr)
    if a.command == "multicity":
        total = f"INR {res.total_cheapest_inr:,}" if res.total_cheapest_inr is not None else "unavailable (a leg has no priced option)"
        print(f"cheapest total across legs: {total}\n{res.note}")
    else:
        print(legs[0].notice)
    return 0


def _deals(em: EventMate, a) -> int:
    from eventmate.schemas import DealQuery
    from eventmate.integrations.websearch import SearchError

    try:
        rep = em.find_deals(DealQuery(kind=a.kind, city=a.city, guests=a.guests, nights=a.nights, budget_inr=a.budget, limit=a.limit))
    except (ValueError, SearchError) as exc:  # pydantic's ValidationError is a ValueError
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(rep.model_dump_json(indent=2))
        return 0
    if rep.market:
        m = rep.market
        print(f"market ({m.unit}): median INR {m.median_low_inr:,} to {m.median_high_inr:,} across {m.n_offers} advertised prices on {m.n_domains} sites")
    for o in rep.offers:
        est = f"  est total INR {o.est_total_low_inr:,} to {o.est_total_high_inr:,}" if o.est_total_low_inr is not None else ""
        fit = {True: "  [fits budget]", False: "  [over budget]", None: ""}[o.within_budget]
        print(f"#{o.rank} {o.domain}  INR {o.low_inr:,} to {o.high_inr:,} {o.unit}{est}{fit}  ({o.vs_market_pct:+d}% vs market, {o.corroborated_by} other site(s) agree)")
        print(f"     {o.label}  {o.url}")
        for f in o.flags:
            print(f"     ! {f}")
    for sk in rep.skipped:
        print(f"skipped {sk.url}: {sk.reason}", file=sys.stderr)
    for w in rep.warnings:
        print(f"note: {w}", file=sys.stderr)
    if not rep.offers:
        print("no advertised prices could be read; many sites load prices with JavaScript or block automated readers")
    print(f"searched with {rep.provider}, read {rep.pages_read} page(s). {rep.notice}")
    return 0


def _weather(em: EventMate, a) -> int:
    try:
        w = em.weather_forecast(a.city, a.date)
    except (ValueError, WeatherError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(w.model_dump_json(indent=2))
        return 0
    if not w.slots:
        print(f"{w.resolved_place}: {w.note}")
    else:
        print(f"{w.resolved_place} on {w.travel_date.isoformat()}: {w.temp_min}C to {w.temp_max}C, {w.conditions or 'no description'}; rain chance up to {round(w.max_rain_chance * 100)}%")
        for x in w.slots:
            print(f"  {x.local_time[11:16]}  {x.temp_c:5.1f}C  rain {round(x.rain_chance * 100):3d}%  {x.description or ''}")
    print(w.attribution)
    return 0


def _discover(em: EventMate, a) -> int:
    try:
        res = em.discover_venues(a.city, a.kind, a.limit)
    except (ValueError, DiscoveryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(res.model_dump_json(indent=2))
        return 0
    for c in res.candidates:
        where = ", ".join(x for x in (c.street, c.city) if x)
        cap = f", capacity {c.capacity_tagged} (tagged, unverified)" if c.capacity_tagged else ""
        print(f"{c.name} [{c.osm_ref}] {where}{cap} {c.website or ''}".rstrip())
    for w in res.warnings:
        print(f"note: {w}", file=sys.stderr)
    print(f"{len(res.candidates)} place(s). {res.notice}\n{res.attribution}")
    return 0


def _outreach(em: EventMate, a) -> int:
    d = em.draft_outreach(a.plan_id, VendorCategory(a.category), a.channel, a.recipient, a.name)
    if a.json:
        print(d.model_dump_json(indent=2))
        return 0
    print(d.notice)
    if d.subject:
        print(f"Subject: {d.subject}")
    print(f"\n{d.body}\n\nOpen to send: {d.link}")
    return 0


def _jobs(em: EventMate, a) -> int:
    from datetime import datetime, timedelta, timezone

    from eventmate.followthrough.worker import digest

    w = em.jobs
    if a.what == "tick":
        print(w.tick())
        return 0
    if a.what == "start":
        if not (a.target and a.extra and a.vendors and a.name):
            print("jobs start PLAN_ID CATEGORY --vendors id1,id2,id3 --name NAME [--min 3 --cap INR --days 7]", file=sys.stderr)
            return 2
        job = w.start_job(a.target, VendorCategory(a.extra), [v.strip() for v in a.vendors.split(",") if v.strip()], a.goal or f"{a.min_quotes} {a.extra} quotes",
                          datetime.now(timezone.utc) + timedelta(days=a.days), a.name, a.min_quotes, a.cap)
        print(f"job {job.job_id}: {len(job.vendors)} requests drafted, nothing sent. Read them with `inbox show {job.plan_id}`, then `jobs approve {job.job_id}`.")
        return 0
    if a.what in ("approve", "cancel"):
        if not a.target:
            print("give a job id", file=sys.stderr)
            return 2
        job = w.approve_requests(a.target) if a.what == "approve" else w.cancel(a.target)
        print(f"job {job.job_id}: {job.status}; {job.next_action}")
        return 0
    if a.what in ("confirm", "reject", "resolve", "nudge"):
        if not (a.target and a.extra):
            print(f"jobs {a.what} JOB_ID {'CLAIM_ID' if a.what in ('confirm', 'reject') else 'ITEM_ID'}", file=sys.stderr)
            return 2
        if a.what == "confirm":
            if a.valid_days is None:
                print("confirm needs --valid-days N (1-90): the vendor's reply rarely says how long the price holds, so you choose", file=sys.stderr)
                return 2
            job = w.confirm_claim(a.target, a.extra, a.valid_days)
        elif a.what == "reject":
            job = w.reject_claim(a.target, a.extra)
        elif a.what == "resolve":
            job = w.resolve_item(a.target, a.extra)
        else:
            job = w.approve_nudge(a.target, a.extra)
        print(f"job {job.job_id}: {job.status}; {job.next_action}")
        return 0
    jobs = [j for j in (w.store.list(a.target) if (em.store.root / "jobs.db").exists() else [])]
    if a.json:
        print(json.dumps([json.loads(j.model_dump_json()) for j in jobs], indent=2))
        return 0
    for j in jobs:
        print(f"[{j.job_id}] " + digest(j).replace("\n", "\n    "))
        for i in j.open_items():
            print(f"    item {i.item_id} ({i.kind})" + (f" claim {i.ref}" if i.kind == "confirm_claim" else ""))
    if not jobs:
        print("no jobs")
    return 0


def _inbox(em: EventMate, a) -> int:
    if a.what == "poll":
        import time

        while True:
            res = em.poll_conversations()
            print(f"sent {res.sent}, new replies {res.new_messages}" + "".join(f"; waiting: {w}" for w in res.waiting) + "".join(f"; ERROR {e}" for e in res.errors))
            if not a.watch:
                return 1 if res.errors else 0
            time.sleep(max(5, a.watch))
    if a.what in ("kill", "resume"):
        (em.kill_all_autoreplies if a.what == "kill" else em.resume_autoreplies)()
        print("every automatic message is stopped" if a.what == "kill" else "automatic replies may be granted again; existing permissions apply")
        return 0
    if not a.plan_id:
        print("give a plan id", file=sys.stderr)
        return 2
    if a.what == "autoreply":
        if a.off:
            em.revoke_autoreply(a.plan_id)
        elif a.name:
            em.grant_autoreply(a.plan_id, a.name, a.hours, a.max_per_thread, a.max_total)
        st = em.autoreply_status(a.plan_id)
        m = st.mandate
        if st.killed:
            print("automatic replies are OFF: the kill switch is on (`inbox resume` lifts it)")
        elif st.active:
            print(f"automatic follow-up questions are ON until {m.expires_at:%Y-%m-%d %H:%M} UTC ({st.sent_total} of {m.max_total} used, {m.max_per_thread} per vendor, {m.window_start:02d}:00-{m.window_end:02d}:00 IST)")
        else:
            print("automatic replies are off for this plan")
        return 0
    if a.what in ("approve", "deny"):
        if not a.draft_id or (a.what == "approve" and not a.hash):
            print("approve needs DRAFT_ID and --hash (see `inbox show`)", file=sys.stderr)
            return 2
        d = em.approve_outreach(a.plan_id, a.draft_id, a.hash) if a.what == "approve" else em.deny_outreach(a.plan_id, a.draft_id)
        print(f"{d.vendor_name}: {d.status}" + (f" ({d.error})" if d.error else ""))
        return 0
    box = em.inbox(a.plan_id)
    if a.json:
        print(box.model_dump_json(indent=2))
        return 0
    print("Channels: " + "; ".join(f"{c.channel} {'ready' if c.can_send else c.detail}" for c in box.channels))
    print(f"{box.unread_total} unread, {box.approvals_total} waiting for your approval")
    for t in box.threads:
        print(f"\n{t.request.vendor_name} ({t.request.channel}, {t.request.status})")
        for d in t.drafts:
            print(f"  -> [{d.status}] {d.draft_id} hash {d.content_hash[:12]}…: {d.body.splitlines()[0][:70]}")
        for m in t.replies:
            print(f"  <- {'' if m.read else '(new) '}{m.received_at:%Y-%m-%d %H:%M}: {m.body[:100].replace(chr(10), ' ')}")
            for x in m.analysis.amounts:
                print(f"       they wrote: INR {x.low_inr:,}" + (f" to {x.high_inr:,}" if x.high_inr != x.low_inr else "") + f" ({x.unit}); not recorded as a quote")
    return 0


def _quote_scores(em: EventMate, a) -> int:
    board = em.vendor_scores(VendorCategory(a.target), a.today)
    if a.json:
        print(board.model_dump_json(indent=2))
        return 0
    for s in board.scores:
        overall = "n/a" if s.overall is None else str(s.overall)
        print(f"{s.vendor_id}: {overall}/100 ({s.status}; {s.coverage:.0%} of the formula had data)")
        for c in s.components:
            print(f"  - {c.name} ({c.weight:.0%}): {'left out' if c.score is None else c.score}; {c.detail}")
        for n in s.notes:
            print(f"  ! {n}")
    for u in board.unscored:
        print(f"{u.vendor_id}: not scored; {u.reason}")
    if not board.scores and not board.unscored:
        print("no quotes recorded for this category")
    elif board.scores:
        print("Not scored (no data): " + ", ".join(board.scores[0].not_scored))
    return 0


def _quotes(em: EventMate, a) -> int:
    today = a.today or date.today()
    if a.what == "score":
        return _quote_scores(em, a)
    if a.what == "show":
        reports = [r for r in [em.observations.report(a.target, today)] if r]
        if not reports:
            print(f"no quote recorded for {a.target}", file=sys.stderr)
            return 2
    else:
        cmp_ = em.observations.compare(VendorCategory(a.target), today)
        reports = list(cmp_.ranked) + list(cmp_.expired)
    if a.json:
        print(json.dumps([json.loads(r.model_dump_json()) for r in reports], indent=2))
        return 0
    for r in reports:
        print(f"{r.vendor_id}: INR {r.effective_total:,} ({r.status}, until {r.expires_at.isoformat()}, from {r.source_type.value}, confidence {r.confidence:.0%})")
        for f in r.flags:
            print(f"  - {f}")
    if not reports:
        print("no quotes recorded for this category")
    return 0


def _tradeoffs(em: EventMate, a) -> int:
    report = em.tradeoffs(a.request, today=a.today, guest_list_id=a.guest_list)
    if a.json:
        print(report.model_dump_json(indent=2))
    elif report.status == "not_needed":
        print("No trade-off needed: this request already produces a valid plan.")
    elif report.status == "no_option":
        print("No verified trade-off exists.", file=sys.stderr)
        _print_violations(report.violations)
    else:
        for i, o in enumerate(report.options, 1):
            extra = f", costs INR {o.extra_spend:,} more" if o.extra_spend else ""
            print(f"{i}. {o.title}  [{o.kind.value}]\n   {o.detail}\n   preference impact {o.preference_harm:.0%}{extra}; verified by a full dry run")
    if a.apply is None:
        return 3 if report.status == "no_option" else 0
    if not 1 <= a.apply <= len(report.options):
        raise ValueError(f"no option {a.apply}: {len(report.options)} available")
    out = em.apply_tradeoff(report.options[a.apply - 1], today=a.today, guest_list_id=a.guest_list)
    if out.plan is None:
        print("The chosen option no longer plans.", file=sys.stderr)
        _print_violations(out.violations)
        return 3
    print(em.explain(out.plan.plan_id).markdown)
    print(f"plan id: {out.plan.plan_id} (version {out.plan.version})", file=sys.stderr)
    return 0


def _print_replan(em: EventMate, out, as_json: bool) -> int:
    if out.status != "replanned":
        print(f"{out.status.replace('_', ' ')}: nothing was saved; the previous version stays valid.", file=sys.stderr)
        _print_violations(out.violations)
        return 3
    if as_json:
        print(out.model_dump_json(indent=2))
    else:
        print(em.explain(out.plan.plan_id).markdown)
    print(f"plan id: {out.plan.plan_id} (version {out.plan.version}, from {out.plan.parent_version})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
