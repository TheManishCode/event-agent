"""EventMate web UI + JSON API (Flask).

Security model (local-first tool):
- binds to loopback by default; ``Host`` must be an allowed loopback name
  (blocks DNS rebinding);
- every write needs the per-process CSRF token (header) *and* JSON content type,
  and a cross-origin ``Origin`` is refused;
- optional bearer token (``EVENTMATE_WEB_TOKEN``) for the API;
- strict CSP (no inline script/style, no third-party origins), nosniff,
  no-referrer, no-store on API responses;
- 64 KB body cap, per-client write rate limit, allow-listed static files;
- unexpected errors return a generic message; details go to the server log only.
All EventMate calls run under one lock: the planner and loggers are not designed
for concurrent mutation of the same plan.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, List, Literal, Optional

from flask import Flask, Response, abort, g, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eventmate.planning import intake
from eventmate.assistant.agent import Assistant, AssistantError
from eventmate.assistant.approvals import ApprovalError
from eventmate.assistant.identity import LOCAL_ORGANIZER, Perm, Principal
from eventmate.agents.guest import GuestDataError
from eventmate.agents.manager import RequestParseError
from eventmate.agents.vendor import DataIntegrityError
from eventmate.env import load_env
from eventmate.integrations.explore import ExploreError, ExploreNotConfigured, route as explore_route
from eventmate.llm import LLMError, client_from_env
from eventmate.plan_store import PlanIntegrityError
from eventmate.schemas import MAX_BUDGET_INR, CommunityDetails, DealQuery, TripRequest, MAX_MULTI_CITY_LEGS, Disruption, HiddenCost, LedgerKind, ObservationSource, TradeoffOption, VendorCategory
from eventmate.integrations.discovery import DiscoveryError
from eventmate.integrations.places import PlacesError, PlacesNotConfigured
from eventmate.service import EventMate
from eventmate.integrations.weather import WeatherError, WeatherNotConfigured
from eventmate.followthrough.jobs import StaleJob
from eventmate.followthrough.worker import digest
from eventmate.sourcing.search import SourceNotSupported
from eventmate.sourcing.suppression import NeedsAcknowledgement
from eventmate.integrations.websearch import SearchError
from eventmate.integrations.travel import CABINS, TravelError, check_date

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8", "favicon.svg": "image/svg+xml",
    "avatar.glb": "model/gltf-binary", "model-viewer.min.js": "text/javascript; charset=utf-8",  # the assistant avatar and its viewer (docs/ATTRIBUTIONS.md)
}
# The avatar viewer injects one fixed <style> block, reads the model's textures from blob: URLs that its own script creates, and compiles a small WebAssembly decoder.
# Each is allowed as narrowly as possible: one exact hash instead of unsafe-inline, blob: (which only same-origin script can mint) instead of any external source,
# and wasm-unsafe-eval, which permits WebAssembly compilation only and never string eval.
VIEWER_STYLE_HASH = "sha256-PXtZZkJMMyc18o/DN1ZEFOJ2qEGq95IQbVaJFMOXOb0="
LONG_CACHE = frozenset({"avatar.glb", "model-viewer.min.js"})  # large and unchanging: not re-sent on every page load
LOOPBACK = {"127.0.0.1", "localhost", "[::1]", "::1"}
CSP = (
    f"default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' '{VIEWER_STYLE_HASH}'; img-src 'self' data: blob:; "
    "connect-src 'self' blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEAVY_MAX = 4  # concurrent network-bound searches
_PLAN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
log = logging.getLogger("eventmate.web")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanBody(_Strict):
    request: str = Field(min_length=1, max_length=2000)
    guest_list_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class IntakeBody(_Strict):
    messages: List[str] = Field(min_length=1, max_length=intake.MAX_MESSAGES)
    draft: Optional[intake.IntakeDraft] = None


class IntakeConfirmBody(_Strict):
    draft: intake.IntakeDraft
    raw_text: str = Field(default="", max_length=2000)
    source: str = Field(default="rule:parser", max_length=80, pattern=r"^[A-Za-z0-9:._-]+$")
    guest_list_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class AskBody(_Strict):
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    question: str = Field(min_length=1, max_length=1000)


class QuoteBody(_Strict):
    category: VendorCategory
    source_type: ObservationSource
    entered_by: str = Field(min_length=1, max_length=80)
    expires_at: date
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ref: str = Field(default="", max_length=200)
    amount_inr: int = Field(gt=0)
    basis: Literal["total", "per_plate"]
    guest_count: Optional[int] = Field(default=None, ge=1)
    inclusions: List[str] = Field(default_factory=list, max_length=30)
    exclusions: List[str] = Field(default_factory=list, max_length=30)
    hidden_costs: List[HiddenCost] = Field(default_factory=list, max_length=20)
    note: str = Field(default="", max_length=200)


class ExploreBody(_Strict):
    text: str = Field(min_length=1, max_length=600)


class LeadBody(_Strict):
    kind: Literal["caterer", "venue", "photographer", "hotel"]
    url: str = Field(max_length=2048)
    domain: str = Field(max_length=253)
    label: str = Field(max_length=100)
    unit: Literal["per_plate", "per_night", "per_day", "per_hour", "per_event"]
    low_inr: int
    high_inr: int
    note: str = Field(default="", max_length=200)


class OutreachBody(_Strict):
    category: VendorCategory
    channel: Literal["email", "whatsapp"]
    recipient: str = Field(min_length=1, max_length=254)
    sender_name: str = Field(min_length=1, max_length=80)


class ApproveBody(_Strict):
    content_hash: str = Field(min_length=64, max_length=64)


class FollowupBody(_Strict):
    text: str = Field(min_length=1, max_length=1000)
    sender_name: str = Field(min_length=1, max_length=80)


class RequestAllBody(_Strict):
    sender_name: str = Field(min_length=1, max_length=80)


class ContactBody(_Strict):
    vendor_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    email: Optional[str] = Field(default=None, max_length=254)
    whatsapp: Optional[str] = Field(default=None, max_length=30)
    preferred: Literal["email", "whatsapp"] = "whatsapp"


class AutoreplyBody(_Strict):
    sender_name: str = Field(min_length=1, max_length=80)
    hours: int = Field(default=72, ge=1, le=168)
    max_per_thread: int = Field(default=3, ge=1, le=5)
    max_total: int = Field(default=10, ge=1, le=30)


class JobBody(_Strict):
    category: VendorCategory
    vendor_ids: List[str] = Field(min_length=1, max_length=10)
    goal: str = Field(min_length=1, max_length=300)
    sender_name: str = Field(min_length=1, max_length=80)
    deadline_days: int = Field(default=7, ge=1, le=60)
    min_quotes: int = Field(default=3, ge=1, le=10)
    max_amount_inr: Optional[int] = Field(default=None, gt=0)
    max_nudges_per_vendor: int = Field(default=2, ge=0, le=5)
    reply_window_hours: int = Field(default=24, ge=1, le=168)


class ConfirmClaimBody(_Strict):
    valid_days: int = Field(ge=1, le=90)


class ResolveItemBody(_Strict):
    note: str = Field(default="", max_length=200)


class IdeasBody(_Strict):
    preferences: List[str] = Field(default_factory=list, max_length=5)


class DoneBody(_Strict):
    done: bool


class CommunityItemBody(_Strict):
    status: Literal["not_verified", "confirmed_by_user", "not_applicable_by_user"]
    by: str = Field(min_length=1, max_length=60)
    on: date
    note: str = Field(default="", max_length=200)


class AccessBody(_Strict):
    features: List[Literal["step_free_access", "accessible_toilet", "reserved_seating", "hearing_support", "accessible_parking"]] = Field(default_factory=list, max_length=5)
    evidence_ref: str = Field(min_length=1, max_length=200)
    entered_by: str = Field(min_length=1, max_length=80)


class NotifyProposeBody(_Strict):
    destination: str = Field(min_length=1, max_length=254)
    include_vendor_names: bool = False
    include_amounts: bool = False
    include_raw_text: bool = False


class NotifyConfirmBody(_Strict):
    code: str = Field(min_length=1, max_length=128)


class ShortlistSearchBody(_Strict):
    category: VendorCategory
    source: Optional[Literal["osm", "places"]] = None
    limit: int = Field(default=10, ge=1, le=20)


class WebsiteBody(_Strict):
    url: str = Field(min_length=1, max_length=2048)


class ConfirmContactBody(_Strict):
    by: str = Field(min_length=1, max_length=60)
    edited_value: Optional[str] = Field(default=None, max_length=254)
    acknowledge_bounce: bool = False  # required to confirm an address that bounced earlier


class SuppressBody(_Strict):
    channel: Literal["email", "whatsapp"]
    value: str = Field(min_length=3, max_length=254)
    by: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=200)


class LiftBody(_Strict):
    by: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=200)
    acknowledge_reason: bool = False  # required for an address that bounced: you confirm you have read why


class ByBody(_Strict):
    by: str = Field(min_length=1, max_length=80)


class TestVendorBody(_Strict):
    name: str = Field(min_length=1, max_length=120)
    category: VendorCategory
    email: str = Field(min_length=3, max_length=254)
    owns_address: bool
    by: str = Field(min_length=1, max_length=60)
    city: Optional[str] = Field(default=None, max_length=80)


class SessionClearBody(_Strict):
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class EmptyBody(_Strict):
    pass


class FeedbackBody(_Strict):
    value: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=200)


class ApproveAnswerBody(_Strict):
    trace_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    answer: Optional[str] = Field(default=None, min_length=1, max_length=1500)


class PlanRefBody(_Strict):
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ApplyBody(_Strict):
    option: TradeoffOption
    guest_list_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class LedgerBody(_Strict):
    kind: LedgerKind
    category: VendorCategory
    amount: int = Field(gt=0, le=MAX_BUDGET_INR)
    note: str = Field(default="", max_length=200)


_IATA_FIELD = dict(pattern=r"^[A-Z]{3}$")


class FlightBody(_Strict):
    origin: str = Field(**_IATA_FIELD)
    destination: str = Field(**_IATA_FIELD)
    date: date
    adults: int = Field(default=1, ge=1, le=9)
    cabin: Literal[CABINS] = "economy"
    max_stops: Optional[int] = Field(default=None, ge=0, le=2)
    limit: int = Field(default=10, ge=1, le=30)


class LegBody(_Strict):
    origin: str = Field(**_IATA_FIELD)
    destination: str = Field(**_IATA_FIELD)
    date: date


class MultiCityBody(_Strict):
    legs: List[LegBody] = Field(min_length=2, max_length=MAX_MULTI_CITY_LEGS)
    adults: int = Field(default=1, ge=1, le=9)
    cabin: Literal[CABINS] = "economy"
    max_stops: Optional[int] = Field(default=None, ge=0, le=2)
    limit: int = Field(default=5, ge=1, le=10)


class ConfirmBody(_Strict):
    category: VendorCategory


def create_app(
    em: Optional[EventMate] = None,
    api_token: Optional[str] = None,
    rate_limit_per_minute: int = 60,
    today_fn: Callable[[], date] = date.today,
    allowed_hosts: Optional[set] = None,
    assistant: Optional[Assistant] = None,
    principal: Principal = LOCAL_ORGANIZER,
    poll_seconds: Optional[float] = None,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    if em is None:
        load_env()  # keys first, so the model client below can see them
        em = EventMate(run_id=f"web-{date.today():%Y%m%d}", llm=client_from_env())
    lock = threading.RLock()
    assistant = assistant or Assistant.from_env(em)
    csrf_token = secrets.token_urlsafe(32)
    hosts = set(allowed_hosts) if allowed_hosts else set(LOOPBACK)
    writes: Dict[str, Deque[float]] = defaultdict(deque)

    # ------------------------------------------------------------ guards

    # GET routes that spend a paid or rate-limited quota also need the CSRF header: a page on another site can fire a plain GET at a local
    # server (an image tag is enough) but cannot add a custom header without a CORS preflight that this server never grants.
    quota_gets = {"/api/deals", "/api/explore", "/api/travel/flights", "/api/travel/multi-city", "/api/travel/trip", "/api/discover/venues", "/api/weather", "/api/places"}

    def spends_quota(path: str) -> bool:
        return path in quota_gets or (path.startswith("/api/plans/") and path.endswith(("/weather", "/weather-advice")))

    @app.before_request
    def guard():
        hostname = request.host.rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
        if hostname not in hosts:
            abort(Response(json.dumps({"error": "host not allowed"}), 400, mimetype="application/json"))
        if request.path.startswith("/api/") and api_token:
            supplied = request.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {api_token}"):
                return _err(401, "authentication required")
        if request.method in ("GET", "HEAD") and spends_quota(request.path):
            if not hmac.compare_digest(request.headers.get("X-EventMate-CSRF", ""), csrf_token):
                return _err(403, "missing or invalid CSRF token")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not hmac.compare_digest(request.headers.get("X-EventMate-CSRF", ""), csrf_token):
                return _err(403, "missing or invalid CSRF token")
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != request.host:
                return _err(403, "cross-origin request refused")
            if request.mimetype != "application/json":
                return _err(415, "JSON body required")
            now, q = time.monotonic(), writes[request.remote_addr or "?"]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= rate_limit_per_minute:
                return _err(429, "too many requests")
            q.append(now)
        return None

    # Routes that wait on the network are limited to a few at a time, so a burst cannot pin every worker thread (ADR-037).
    heavy = threading.BoundedSemaphore(HEAVY_MAX)

    @app.before_request
    def limit_heavy():
        if spends_quota(request.path):
            if not heavy.acquire(blocking=False):
                return _err(429, "the server is busy with other searches; try again in a moment")
            g.heavy_permit = True
        return None

    @app.teardown_request
    def release_heavy(_exc):
        if g.pop("heavy_permit", False):
            heavy.release()

    @app.after_request
    def headers(resp: Response) -> Response:
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def _err(code: int, message: str, **extra):
        return jsonify({"error": message, **extra}), code

    @app.errorhandler(RequestParseError)
    def _parse(exc):
        return _err(422, "request could not be parsed", problems=list(exc.problems))

    @app.errorhandler(ValidationError)
    def _validation(exc):
        return _err(422, "invalid input", details=[{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()][:10])

    @app.errorhandler(ExploreNotConfigured)
    def _explore_off(exc):
        return _err(503, str(exc)[:300])

    @app.errorhandler(ExploreError)
    def _explore(exc):
        return _err(422, str(exc)[:400])

    @app.errorhandler(LLMError)
    def _llm(exc):
        return _err(502, str(exc)[:300])

    @app.errorhandler(PlacesNotConfigured)
    def _places_off(exc):
        return _err(503, str(exc)[:300])

    @app.errorhandler(PlacesError)
    def _places(exc):
        return _err(502, str(exc)[:300])

    @app.errorhandler(SearchError)
    def _search(exc):
        return _err(502, str(exc)[:300])

    @app.errorhandler(WeatherNotConfigured)
    def _weather_off(exc):
        return _err(503, str(exc)[:300])

    @app.errorhandler(WeatherError)
    def _weather(exc):
        return _err(502, str(exc)[:300])

    @app.errorhandler(DiscoveryError)
    def _discovery(exc):
        return _err(502, str(exc)[:300])

    @app.errorhandler(TravelError)
    def _travel(exc):
        return _err(502, str(exc)[:300])

    @app.errorhandler(GuestDataError)
    def _guests(exc):
        return _err(422, "unknown or invalid guest list")

    @app.errorhandler(DataIntegrityError)
    def _data(exc):
        log.error("vendor data integrity failure: %s", exc)
        return _err(500, "vendor data failed validation")

    @app.errorhandler(FileNotFoundError)
    def _nf(exc):
        return _err(404, "not found")

    @app.errorhandler(PlanIntegrityError)
    def _integrity(exc):
        log.error("plan integrity failure: %s", exc)
        return _err(500, "stored plan failed its integrity check")

    @app.errorhandler(413)
    def _too_big(exc):
        return _err(413, "request too large")

    @app.errorhandler(405)
    def _method(exc):
        return _err(405, "method not allowed")

    @app.errorhandler(404)
    def _404(exc):
        return _err(404, "not found")

    @app.errorhandler(NeedsAcknowledgement)
    def _needs_ack(exc):
        return jsonify({"error": str(exc)[:600], "needs_acknowledgement": True, "reason": exc.reason}), 409

    @app.errorhandler(StaleJob)
    def _stale(exc):
        return _err(409, "this job changed while you were looking at it; reload and try again")

    @app.errorhandler(ValueError)
    def _value(exc):
        return _err(400, str(exc)[:300])

    @app.errorhandler(Exception)
    def _boom(exc):
        if hasattr(exc, "code") and isinstance(exc.code, int):
            return _err(exc.code, getattr(exc, "name", "error"))
        log.exception("unhandled error")
        return _err(500, "internal error")

    def body(model):
        try:
            data = request.get_json(silent=True)
        except RecursionError:  # absurdly nested JSON
            data = None
        if not isinstance(data, dict):
            raise ValueError("JSON object body required")
        return model.model_validate(data)

    def pid(plan_id: str) -> str:
        if not _PLAN_ID.fullmatch(plan_id):
            raise ValueError("invalid plan id")
        return plan_id

    def version_arg(name: str = "version") -> Optional[int]:
        raw = request.args.get(name)
        if raw is None:
            return None
        if not raw.isdigit() or len(raw) > 4:
            raise ValueError(f"{name} must be a positive integer")
        return int(raw)

    def out(model, code: int = 200):
        return Response(model.model_dump_json(), code, mimetype="application/json")

    # ------------------------------------------------------------- pages

    @app.get("/")
    def index():
        return Response((STATIC_DIR / "index.html").read_bytes(), mimetype="text/html")

    @app.get("/static/<name>")
    def static_file(name: str):
        if name not in STATIC_FILES:
            abort(404)
        return Response((STATIC_DIR / name).read_bytes(), mimetype=STATIC_FILES[name].split(";")[0], headers={"Cache-Control": "public, max-age=3600" if name in LONG_CACHE else "no-cache"})

    @app.get("/api/session")
    def session():
        return jsonify({"csrf": csrf_token, "today": today_fn().isoformat(), "roles": [r.value for r in principal.roles], "guest_lists": list(em.manager.guest.repo.list_ids())})

    # --------------------------------------------------------------- api

    @app.get("/api/plans")
    def list_plans():
        with lock:
            rows = []
            for plan_id in em.store.list_plans():
                p = em.store.latest(plan_id)
                rows.append(
                    {
                        "plan_id": plan_id,
                        "version": p.version,
                        "event_type": p.request.event_type.value,
                        "city": p.request.city,
                        "event_date": p.request.event_date.isoformat(),
                        "guest_count": p.request.guest_count,
                        "total_cost": p.total_cost,
                        "budget_total": p.request.budget_total,
                    }
                )
        return jsonify(sorted(rows, key=lambda r: r["event_date"]))

    @app.errorhandler(ApprovalError)
    def _approval(exc):
        return _err(403 if str(exc).startswith("you are not allowed") else 409, str(exc))

    @app.errorhandler(AssistantError)
    def _assistant(exc):
        return _err(400, str(exc))

    @app.post("/api/assistant/ask")
    def assistant_ask():
        b = body(AskBody)
        return out(assistant.ask(b.question, b.plan_id, principal))  # no global lock: a slow model must not stall the app

    @app.get("/api/assistant/session")
    def assistant_session():
        """How much conversation the assistant is holding for you on this plan (your own only)."""
        plan_id = pid(request.args.get("plan_id", ""))
        return jsonify({"plan_id": plan_id, "turns": assistant.sessions.turns(plan_id, principal.user_id)})

    @app.post("/api/assistant/session/clear")
    def assistant_session_clear():
        b = body(SessionClearBody)
        assistant.sessions.clear(b.plan_id, principal.user_id)
        return jsonify({"plan_id": b.plan_id, "turns": 0})

    @app.get("/api/assistant/approvals")
    def assistant_approvals():
        plan_id = pid(request.args.get("plan_id", ""))
        em.store.latest(plan_id)
        return jsonify([json.loads(p.model_dump_json()) for p in assistant.queue.pending(plan_id)] if principal.can(Perm.APPROVE) else [])

    @app.post("/api/assistant/approvals/<proposal_id>/<verdict>")
    def assistant_decide(proposal_id, verdict):
        if verdict not in ("approve", "reject") or not _PLAN_ID.fullmatch(proposal_id):
            abort(404)
        body(EmptyBody)
        with lock:
            p = assistant.decide(proposal_id, principal, verdict == "approve")
        return out(p)

    @app.post("/api/assistant/traces/<trace_id>/feedback")
    def assistant_feedback(trace_id):
        b = body(FeedbackBody)
        assistant.give_feedback(pid(trace_id), principal, b.value, b.comment)
        return jsonify({"ok": True})

    @app.get("/api/assistant/annotations")
    def assistant_annotations():
        plan_id = pid(request.args.get("plan_id", ""))
        if not principal.can(Perm.APPROVE):
            return jsonify([])
        plan = em.store.latest(plan_id)
        state = f"{plan.version}:{len(em.ledger_entries(plan_id))}"
        return jsonify([{**json.loads(a.model_dump_json()), "stale": a.state != state} for a in assistant.annotations.list(plan_id)])

    @app.post("/api/assistant/annotations")
    def assistant_annotate():
        b = body(ApproveAnswerBody)
        a = assistant.approve_answer(b.trace_id, principal, b.answer)
        return Response(a.model_dump_json(), 201, mimetype="application/json")

    @app.post("/api/assistant/annotations/<annotation_id>/delete")
    def assistant_unannotate(annotation_id):
        b = body(PlanRefBody)
        if not principal.can(Perm.APPROVE):
            return _err(403, "you are not allowed to remove approved answers")
        if not assistant.annotations.remove(pid(annotation_id), b.plan_id):
            abort(404)
        return jsonify({"ok": True})

    @app.get("/api/assistant/traces/<trace_id>")
    def assistant_trace(trace_id):
        if not principal.can(Perm.TRACE_READ):
            return _err(403, "you are not allowed to read traces")
        t = assistant.traces.find(pid(trace_id))
        if t is None:
            abort(404)
        return Response(t.model_dump_json(), mimetype="application/json")

    @app.post("/api/intake")
    def intake_turn():
        b = body(IntakeBody)
        with lock:
            turn = em.intake_turn(b.messages, b.draft, today=today_fn())
        return out(turn)

    @app.post("/api/intake/confirm")
    def intake_confirm():
        b = body(IntakeConfirmBody)
        with lock:
            outcome = em.confirm_intake(b.draft, b.raw_text, today=today_fn(), guest_list_id=b.guest_list_id, source=b.source)
        return plan_response(outcome)

    @app.post("/api/plans")
    def create_plan():
        b = body(PlanBody)
        with lock:
            outcome = em.plan(b.request, today=today_fn(), guest_list_id=b.guest_list_id)
        return plan_response(outcome)

    def plan_response(outcome):
        payload = {
            "status": outcome.status,
            "plan": json.loads(outcome.plan.model_dump_json()) if outcome.plan else None,
            "violations": [v.model_dump() for v in outcome.violations],
            "tasks": [{"id": t.id, "agent": t.agent.value, "status": t.status.value, "depends_on": list(t.depends_on)} for t in outcome.task_graph.tasks],
            "reality": json.loads(outcome.intake.reality.model_dump_json()) if outcome.intake.reality else None,
        }
        return jsonify(payload), (201 if outcome.plan else 422)

    @app.get("/api/plans/<plan_id>/advisories")
    def advisories(plan_id):
        plan_id = pid(plan_id)
        with lock:
            items = em.advisories(plan_id)
        return jsonify([json.loads(a.model_dump_json()) for a in items])

    @app.get("/api/plans/<plan_id>/quotes")
    def plan_quotes(plan_id):
        plan_id = pid(plan_id)
        with lock:
            rows = em.quote_status(plan_id, today_fn())
        return jsonify([json.loads(r.model_dump_json()) for r in rows])

    @app.get("/api/plans/<plan_id>/leads")
    def leads_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            items = em.leads(plan_id)
        return jsonify([json.loads(x.model_dump_json()) for x in items])

    @app.post("/api/plans/<plan_id>/leads")
    def leads_add(plan_id):
        """Bookmark an advertised price. Not a quote; it never changes a plan price."""
        plan_id, b = pid(plan_id), body(LeadBody)
        with lock:
            lead = em.save_lead(plan_id, **b.model_dump())
        return out(lead, 201)

    @app.get("/api/plans/<plan_id>/outreach")
    def outreach_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            items = em.outreach(plan_id)
        return jsonify([json.loads(x.model_dump_json()) for x in items])

    @app.post("/api/plans/<plan_id>/outreach")
    def outreach_add(plan_id):
        """Write a quote request and a link to send it from your own mail or WhatsApp. Nothing is sent from here."""
        plan_id, b = pid(plan_id), body(OutreachBody)
        with lock:
            draft = em.draft_outreach(plan_id, **b.model_dump())
        return out(draft, 201)

    @app.post("/api/plans/<plan_id>/outreach/request-all")
    def outreach_request_all(plan_id):
        """One proposal per vendor with a saved contact. Nothing is sent until each one is approved."""
        plan_id, b = pid(plan_id), body(RequestAllBody)
        with lock:
            made, skipped = em.request_all_quotes(plan_id, b.sender_name)
        return jsonify({"drafts": [json.loads(d.model_dump_json()) for d in made], "skipped": skipped}), 201

    @app.post("/api/plans/<plan_id>/outreach/<draft_id>/approve")
    def outreach_approve(plan_id, draft_id):
        """Approve exactly the text the user saw (the content hash). The agent sends it through the configured channel."""
        plan_id, draft_id, b = pid(plan_id), pid(draft_id), body(ApproveBody)
        with lock:
            draft = em.approve_outreach(plan_id, draft_id, b.content_hash)
        return out(draft)

    @app.post("/api/plans/<plan_id>/outreach/<draft_id>/deny")
    def outreach_deny(plan_id, draft_id):
        plan_id, draft_id = pid(plan_id), pid(draft_id)
        body(EmptyBody)
        with lock:
            draft = em.deny_outreach(plan_id, draft_id)
        return out(draft)

    @app.post("/api/plans/<plan_id>/outreach/<request_id>/followup")
    def outreach_followup(plan_id, request_id):
        """A follow-up in the same thread, as a new proposal. Fixed suggestions or the user's own words; never sent without approval."""
        plan_id, request_id, b = pid(plan_id), pid(request_id), body(FollowupBody)
        with lock:
            draft = em.followup_outreach(plan_id, request_id, b.text, b.sender_name)
        return out(draft, 201)

    @app.get("/api/plans/<plan_id>/outreach/<request_id>/suggestions")
    def outreach_suggestions(plan_id, request_id):
        plan_id, request_id = pid(plan_id), pid(request_id)
        with lock:
            options = em.reply_suggestions(plan_id, request_id)
        return jsonify({"suggestions": options})

    @app.get("/api/plans/<plan_id>/inbox")
    def inbox_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            box = em.inbox(plan_id)
        return out(box)

    @app.post("/api/plans/<plan_id>/inbox/<message_id>/read")
    def inbox_read(plan_id, message_id):
        plan_id, message_id = pid(plan_id), pid(message_id)
        body(EmptyBody)
        with lock:
            msg = em.mark_reply_read(plan_id, message_id)
        return out(msg)

    @app.post("/api/inbox/poll")
    def inbox_poll():
        """Send what is approved and read replies now, instead of waiting for the background loop."""
        body(EmptyBody)
        with lock:
            res = em.poll_conversations()
        return out(res)

    @app.get("/api/plans/<plan_id>/autoreply")
    def autoreply_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            st = em.autoreply_status(plan_id)
        return out(st)

    @app.post("/api/plans/<plan_id>/autoreply")
    def autoreply_grant(plan_id):
        """Standing permission for fixed follow-up questions, bounded in time, count and hours. Revocable at any moment."""
        plan_id, b = pid(plan_id), body(AutoreplyBody)
        with lock:
            em.grant_autoreply(plan_id, b.sender_name, b.hours, b.max_per_thread, b.max_total)
            st = em.autoreply_status(plan_id)
        return out(st, 201)

    @app.post("/api/plans/<plan_id>/autoreply/revoke")
    def autoreply_revoke(plan_id):
        plan_id = pid(plan_id)
        body(EmptyBody)
        with lock:
            st = em.revoke_autoreply(plan_id)
        return out(st)

    @app.post("/api/autoreply/kill")
    def autoreply_kill():
        """Stops every automatic message in every plan at once, including ones already queued."""
        body(EmptyBody)
        with lock:
            em.kill_all_autoreplies()
        return jsonify({"killed": True})

    @app.post("/api/autoreply/resume")
    def autoreply_resume():
        body(EmptyBody)
        with lock:
            em.resume_autoreplies()
        return jsonify({"killed": False})

    @app.get("/api/plans/<plan_id>/jobs")
    def jobs_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            em.store.latest(plan_id)
            jobs = em.jobs.store.list(plan_id) if (em.store.root / "jobs.db").exists() else []
            names = em._vendor_names()
        return jsonify([{**json.loads(j.model_dump_json()), "digest": digest(j), "names": {v: names.get(v, v) for v in j.vendors}} for j in jobs])

    @app.post("/api/plans/<plan_id>/jobs")
    def jobs_start(plan_id):
        """A goal such as "3 comparable caterer quotes by the deadline". Drafts one request per vendor; nothing is sent until the user approves them."""
        plan_id, b = pid(plan_id), body(JobBody)
        with lock:
            em.store.latest(plan_id)
            job = em.jobs.start_job(plan_id, b.category, b.vendor_ids, b.goal, datetime.now(timezone.utc) + timedelta(days=b.deadline_days), b.sender_name,
                                    b.min_quotes, b.max_amount_inr, b.max_nudges_per_vendor, b.reply_window_hours)
        return out(job, 201)

    @app.post("/api/jobs/<job_id>/approve-requests")
    def job_approve(job_id):
        job_id = pid(job_id)
        body(EmptyBody)
        with lock:
            job = em.jobs.approve_requests(job_id)
        return out(job)

    @app.post("/api/jobs/<job_id>/claims/<claim_id>/confirm")
    def job_confirm(job_id, claim_id):
        """The only place a vendor's words become a recorded quote. The user chooses how long the price is treated as valid."""
        job_id, claim_id, b = pid(job_id), pid(claim_id), body(ConfirmClaimBody)
        with lock:
            job = em.jobs.confirm_claim(job_id, claim_id, b.valid_days)
        return out(job)

    @app.post("/api/jobs/<job_id>/claims/<claim_id>/reject")
    def job_reject(job_id, claim_id):
        job_id, claim_id = pid(job_id), pid(claim_id)
        body(EmptyBody)
        with lock:
            job = em.jobs.reject_claim(job_id, claim_id)
        return out(job)

    @app.post("/api/jobs/<job_id>/items/<item_id>/resolve")
    def job_resolve(job_id, item_id):
        job_id, item_id, b = pid(job_id), pid(item_id), body(ResolveItemBody)
        with lock:
            job = em.jobs.resolve_item(job_id, item_id, b.note)
        return out(job)

    @app.post("/api/jobs/<job_id>/items/<item_id>/approve-nudge")
    def job_nudge(job_id, item_id):
        job_id, item_id = pid(job_id), pid(item_id)
        body(EmptyBody)
        with lock:
            job = em.jobs.approve_nudge(job_id, item_id)
        return out(job)

    @app.post("/api/jobs/<job_id>/cancel")
    def job_cancel(job_id):
        job_id = pid(job_id)
        body(EmptyBody)
        with lock:
            job = em.jobs.cancel(job_id)
        return out(job)

    @app.post("/api/jobs/tick")
    def jobs_tick():
        """Run one round now instead of waiting for the background loop."""
        body(EmptyBody)
        with lock:
            report = em.background_round()
        return jsonify(report if isinstance(report, dict) else {"ran": [], "errors": [], "poll": json.loads(report.model_dump_json())})

    @app.get("/api/plans/<plan_id>/ideas")
    def ideas_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            em.store.latest(plan_id)
            v = em.ideas.view(plan_id)
        return jsonify({
            "ideas": [json.loads(i.model_dump_json()) for i in v.visible], "dismissed": list(v.dismissed), "pinned": list(v.pinned),
            "notes": [json.loads(n.model_dump_json()) for n in v.notes], "generated_at": v.generated_at.isoformat() if v.generated_at else None,
            "source": v.source, "facts_used": list(v.facts_used), "notice": v.notice,
        })

    @app.post("/api/plans/<plan_id>/ideas")
    def ideas_generate(plan_id):
        """Personalized suggestions for this plan. Labelled as suggestions: nothing here is a verified offer or changes the plan."""
        plan_id, b = pid(plan_id), body(IdeasBody)
        with lock:
            result = em.ideas.generate(em.store.latest(plan_id), b.preferences)
            for pref in b.preferences:  # a preference typed on the form is a user-stated fact, with its source
                assistant.sessions.add_fact(plan_id, principal.user_id, pref, "form:ideas.preferences")
        return out(result, 201)

    @app.post("/api/plans/<plan_id>/ideas/<idea_id>/pin")
    def ideas_pin(plan_id, idea_id):
        plan_id, idea_id = pid(plan_id), pid(idea_id)
        body(EmptyBody)
        with lock:
            em.store.latest(plan_id)
            note = em.ideas.pin(plan_id, idea_id)
        return out(note, 201)

    @app.post("/api/plans/<plan_id>/ideas/<idea_id>/dismiss")
    def ideas_dismiss(plan_id, idea_id):
        plan_id, idea_id = pid(plan_id), pid(idea_id)
        body(EmptyBody)
        with lock:
            em.store.latest(plan_id)
            em.ideas.dismiss(plan_id, idea_id)
        return jsonify({"dismissed": idea_id})

    @app.post("/api/plans/<plan_id>/ideas/<idea_id>/restore")
    def ideas_restore(plan_id, idea_id):
        plan_id, idea_id = pid(plan_id), pid(idea_id)
        body(EmptyBody)
        with lock:
            em.store.latest(plan_id)
            em.ideas.restore(plan_id, idea_id)
        return jsonify({"restored": idea_id})

    @app.post("/api/plans/<plan_id>/notes/<note_id>/done")
    def notes_done(plan_id, note_id):
        plan_id, note_id, b = pid(plan_id), pid(note_id), body(DoneBody)
        with lock:
            em.store.latest(plan_id)
            note = em.ideas.set_done(plan_id, note_id, b.done)
        return out(note)

    @app.get("/api/plans/<plan_id>/digests")
    def digests_get(plan_id):
        """What changed on this plan's vendor jobs and what needs you. Always kept in the app, newest first."""
        plan_id = pid(plan_id)
        with lock:
            em.store.latest(plan_id)
            items = em.digest_store.list(plan_id)
        return jsonify([json.loads(d.model_dump_json()) for d in items])

    @app.post("/api/plans/<plan_id>/digests/<digest_id>/read")
    def digest_read(plan_id, digest_id):
        plan_id, digest_id = pid(plan_id), pid(digest_id)
        body(EmptyBody)
        with lock:
            em.store.latest(plan_id)
            rec = em.digest_store.mark_read(plan_id, digest_id)
        return out(rec)

    @app.get("/api/notify")
    def notify_get():
        with lock:
            st = em.notifier.status()
        return out(st)

    @app.post("/api/notify/propose")
    def notify_propose():
        """Step one of the optional email copy: the exact address and what it may contain. Nothing is sent until the owner confirms."""
        b = body(NotifyProposeBody)
        with lock:
            p = em.notifier.propose(b.destination, b.include_vendor_names, b.include_amounts, b.include_raw_text)
        return out(p, 201)

    @app.post("/api/notify/confirm")
    def notify_confirm():
        b = body(NotifyConfirmBody)
        with lock:
            st = em.notifier.confirm(b.code)
        return out(st)

    @app.post("/api/notify/disable")
    def notify_disable():
        body(EmptyBody)
        with lock:
            st = em.notifier.disable()
        return out(st)

    @app.post("/api/plans/<plan_id>/shortlist/search")
    def shortlist_search(plan_id):
        """Find businesses for this plan's city and category. They are shortlisted, never added to the vendor database, and nothing is contacted."""
        plan_id, b = pid(plan_id), body(ShortlistSearchBody)
        with lock:
            try:
                res = em.search_vendors(plan_id, b.category, b.source, b.limit)
            except SourceNotSupported as exc:
                return jsonify({"status": "not_supported", "error": str(exc)}), 422
        return out(res)

    @app.get("/api/plans/<plan_id>/shortlist")
    def shortlist_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            items = em.shortlist_view(plan_id)
        return jsonify([json.loads(i.model_dump_json()) for i in items])

    @app.post("/api/plans/<plan_id>/shortlist/<vendor_id>/website")
    def shortlist_website(plan_id, vendor_id):
        plan_id, vendor_id, b = pid(plan_id), pid(vendor_id), body(WebsiteBody)
        with lock:
            em.store.latest(plan_id)
            biz = em.set_business_website(vendor_id, b.url)
        return out(biz)

    @app.post("/api/plans/<plan_id>/shortlist/<vendor_id>/find-contacts")
    def shortlist_find_contacts(plan_id, vendor_id):
        """Read the business's own website for public email addresses. Found addresses are candidates; none is used until you confirm it."""
        plan_id, vendor_id = pid(plan_id), pid(vendor_id)
        body(EmptyBody)
        with lock:
            em.store.latest(plan_id)
            res = em.find_business_contacts(vendor_id)
        return out(res)

    @app.post("/api/plans/<plan_id>/shortlist/<vendor_id>/contacts/<candidate_id>/confirm")
    def shortlist_confirm(plan_id, vendor_id, candidate_id):
        plan_id, vendor_id, candidate_id, b = pid(plan_id), pid(vendor_id), pid(candidate_id), body(ConfirmContactBody)
        with lock:
            em.store.latest(plan_id)
            contact = em.confirm_contact(vendor_id, candidate_id, b.by, b.edited_value, b.acknowledge_bounce)
        return out(contact, 201)

    @app.post("/api/plans/<plan_id>/shortlist/<vendor_id>/contacts/<candidate_id>/reject")
    def shortlist_reject(plan_id, vendor_id, candidate_id):
        plan_id, vendor_id, candidate_id = pid(plan_id), pid(vendor_id), pid(candidate_id)
        body(EmptyBody)
        with lock:
            em.store.latest(plan_id)
            cand = em.reject_contact(vendor_id, candidate_id)
        return out(cand)

    @app.post("/api/plans/<plan_id>/shortlist/test-vendor")
    def shortlist_test_vendor(plan_id):
        """A vendor whose address is yours, for testing. It needs your statement that you own the address and is labelled TEST VENDOR everywhere."""
        plan_id, b = pid(plan_id), body(TestVendorBody)
        with lock:
            plan = em.store.latest(plan_id)
            biz, _ = em.add_test_vendor(b.name, b.city or plan.request.city, b.category, b.email, b.owns_address, b.by, plan_id)
            item = next(i for i in em.shortlist_view(plan_id) if i.business.vendor_id == biz.vendor_id)
        return out(item, 201)

    @app.get("/api/vendors/directory")
    def vendor_directory():
        with lock:
            return jsonify(em.vendor_directory())

    @app.get("/api/vendors/labels")
    def vendor_labels():
        with lock:
            return jsonify(em.vendor_labels())

    @app.get("/api/suppressions")
    def suppressions_get():
        """The do-not-contact list: one entry per address, covering every plan and job."""
        with lock:
            items = em.suppression_list()
        return jsonify([{**json.loads(s.model_dump_json()), "active": s.active, "note": note} for s, note in items])

    @app.post("/api/suppressions")
    def suppressions_add():
        b = body(SuppressBody)
        with lock:
            s = em.suppress_address(b.channel, b.value, b.by, b.note)
        return out(s, 201)

    @app.post("/api/suppressions/<suppression_id>/lift")
    def suppressions_lift(suppression_id):
        """Only you can lift one, and the record says who and why."""
        suppression_id, b = pid(suppression_id), body(LiftBody)
        with lock:
            s = em.lift_suppression(suppression_id, b.by, b.reason, b.acknowledge_reason)
        return out(s)

    @app.get("/api/unmatched")
    def unmatched_get():
        """Stop-like mail and delivery failures that are not tied to anything we sent. They change nothing until you decide."""
        with lock:
            items = em.unmatched_messages()
        return jsonify([json.loads(m.model_dump_json()) for m in items])

    @app.post("/api/unmatched/<message_id>/dismiss")
    def unmatched_dismiss(message_id):
        message_id = pid(message_id)
        body(EmptyBody)
        with lock:
            m = em.dismiss_unmatched(message_id)
        return out(m)

    @app.post("/api/unmatched/<message_id>/suppress")
    def unmatched_suppress(message_id):
        message_id, b = pid(message_id), body(ByBody)
        with lock:
            s = em.suppress_unmatched(message_id, b.by)
        return out(s, 201)

    @app.get("/api/community/profiles")
    def community_profiles():
        """The community subtypes (data in community_profiles.json) for the intake review card."""
        from eventmate.planning.community import load_profiles

        p = load_profiles()
        return jsonify({"default": p.default, "version": p.version, "reviewed": p.reviewed, "profiles": [{"key": k, "label": v.label} for k, v in p.profiles.items()]})

    @app.get("/api/plans/<plan_id>/community")
    def community_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            status = em.community_status(plan_id, today_fn())
            details = em.store.latest(plan_id).request.community
        return jsonify({**json.loads(status.model_dump_json()), "details": json.loads(details.model_dump_json()) if details else None,
                        "suggested_roles": [{"role": r, "note": n} for r, n in status.suggested_roles]})

    @app.post("/api/plans/<plan_id>/community")
    def community_update(plan_id):
        """Replace the community details as a validated new version, or refuse with repairs. Nothing is saved on a refusal."""
        plan_id, details = pid(plan_id), body(CommunityDetails)
        with lock:
            res = em.update_community(plan_id, details, today_fn())
        return out(res, 200 if res.status == "replanned" else (409 if res.status == "needs_decision" else 422))

    @app.post("/api/plans/<plan_id>/community/items/<item_id>")
    def community_item(plan_id, item_id):
        """The user's mark on one conditional requirement. EventMate never marks one itself."""
        plan_id, b = pid(plan_id), body(CommunityItemBody)
        if not re.fullmatch(r"[a-z0-9_]{1,40}", item_id):
            raise ValueError("invalid item id")
        with lock:
            rec = em.set_community_item(plan_id, item_id, b.status, b.by, b.on, b.note, today_fn())
        return out(rec, 201)

    @app.post("/api/venues/<venue_id>/accessibility")
    def venue_accessibility(venue_id):
        """What a venue told the user about its access, with evidence. It turns an unverified item into a pass or a failure on the next check."""
        venue_id, b = pid(venue_id), body(AccessBody)
        with lock:
            rec = em.record_venue_accessibility(venue_id, tuple(b.features), b.evidence_ref, b.entered_by)
        return out(rec, 201)

    @app.get("/api/contacts")
    def contacts_get():
        with lock:
            items = em.vendor_contacts()
        return jsonify([json.loads(c.model_dump_json()) for c in items])

    @app.post("/api/contacts")
    def contacts_set():
        b = body(ContactBody)
        with lock:
            c = em.set_vendor_contact(b.vendor_id, b.email, b.whatsapp, b.preferred)
        return out(c, 201)

    @app.post("/api/plans/<plan_id>/outreach/<draft_id>/sent")
    def outreach_sent(plan_id, draft_id):
        """The user says they sent it. EventMate cannot verify that."""
        plan_id, draft_id = pid(plan_id), pid(draft_id)
        body(EmptyBody)
        with lock:
            draft = em.mark_outreach_sent(plan_id, draft_id)
        return out(draft)

    @app.get("/api/plans/<plan_id>/ledger")
    def ledger_get(plan_id):
        plan_id = pid(plan_id)
        with lock:
            summary, entries = em.ledger_summary(plan_id), em.ledger_entries(plan_id)
        return jsonify({"summary": json.loads(summary.model_dump_json()), "entries": [json.loads(e.model_dump_json()) for e in entries]})

    @app.post("/api/plans/<plan_id>/ledger")
    def ledger_add(plan_id):
        """User-reported money only: a quote, a payment or a refund. EventMate never initiates a payment."""
        plan_id, b = pid(plan_id), body(LedgerBody)
        with lock:
            em.ledger_add(plan_id, b.kind.value, b.category, b.amount, b.note)
            summary = em.ledger_summary(plan_id)
        return out(summary, 201)

    @app.post("/api/observations/<vendor_id>/quotes")
    def observation_add(vendor_id):
        """A quote the user was given. Evidence is required when the vendor is the source; nothing is sent to the vendor."""
        vendor_id, b = pid(vendor_id), body(QuoteBody)
        fields = b.model_dump(exclude={"category"})
        with lock:
            obs = em.observe_quote(vendor_id, b.category, observed_at=datetime.now(timezone.utc), **fields)
            report = em.observations.report(vendor_id, today_fn())
        return jsonify({"observation": json.loads(obs.model_dump_json()), "report": json.loads(report.model_dump_json())}), 201

    @app.get("/api/observations/<vendor_id>")
    def observation_get(vendor_id):
        vendor_id = pid(vendor_id)
        with lock:
            quotes, report = em.observations.quotes(vendor_id), em.observations.report(vendor_id, today_fn())
        return jsonify({"quotes": [json.loads(q.model_dump_json()) for q in quotes], "report": json.loads(report.model_dump_json()) if report else None})

    @app.get("/api/observations/compare/<category>")
    def observation_compare(category):
        with lock:
            cmp_ = em.observations.compare(VendorCategory(category), today_fn())
        return out(cmp_)

    @app.get("/api/observations/score/<category>")
    def observation_score(category):
        with lock:
            board = em.vendor_scores(VendorCategory(category), today_fn())
        return out(board)

    @app.get("/api/weather/status")
    def weather_status():
        """Lets the UI ask whether weather is set up without provoking an error response."""
        return jsonify({"configured": em.weather.configured()})

    @app.get("/api/weather")
    def weather_get():
        """Forecast for a city and date. Past the provider's horizon the answer says so instead of guessing."""
        raw = request.args.get("date", "")
        try:
            day = date.fromisoformat(raw)
        except ValueError:
            raise ValueError("date must be YYYY-MM-DD")
        check_date(day, today_fn())
        return out(em.weather_forecast(request.args.get("city", ""), day))

    @app.get("/api/plans/<plan_id>/weather-advice")
    def plan_weather_advice(plan_id):
        """The forecast plus plain-code advice (rain backup, heat) for the chosen venue. Advice only."""
        return out(em.weather_advice(pid(plan_id)))

    @app.get("/api/plans/<plan_id>/weather")
    def plan_weather(plan_id):
        return out(em.plan_weather(pid(plan_id)))

    @app.get("/api/discover/venues")
    def discover_venues():
        """Read-only: places mapped on OpenStreetMap. Candidates only; no price or availability."""
        city, kind = request.args.get("city", ""), request.args.get("kind", "venue")
        try:
            limit = int(request.args.get("limit", "20"))
        except ValueError:
            raise ValueError("limit must be a whole number")
        res = em.discover_venues(city, kind, limit)  # no app-wide lock: a slow public server must not block other requests
        return out(res)

    @app.post("/api/travel/flights")
    def travel_flights():
        """Indicative fares and schedules from fli. Nothing is booked or paid."""
        b = body(FlightBody)
        check_date(b.date, today_fn())
        res = em.search_flights(b.origin, b.destination, b.date, adults=b.adults, cabin=b.cabin, max_stops=b.max_stops, limit=b.limit)
        return out(res)

    @app.get("/api/places/status")
    def places_status():
        return jsonify({"configured": em.places.configured()})

    @app.get("/api/places")
    def places_get():
        """Ratings and review counts (optionally reviews, which cost more) for a service in a city. Opinions, not prices."""
        return out(em.find_places(request.args.get("kind", ""), request.args.get("city", ""), request.args.get("reviews") == "1"))

    @app.get("/api/explore/status")
    def explore_status():
        return jsonify({"configured": bool(em.router_clients())})

    @app.post("/api/explore")
    def explore():
        """Free text in, one read-only search out. A model picks the search; the same validators as the forms check it; nothing is written."""
        r = explore_route(em, body(ExploreBody).text, today_fn(), em.router_clients())
        return jsonify({"action": r.action, "params": r.params, "result": r.result, "note": r.note, "model": r.model})

    @app.post("/api/deals")
    def deals():
        """The agent searches the web, reads pages safely and ranks advertised prices. Leads only: not quotes, and no plan price changes."""
        return out(em.find_deals(body(DealQuery)))  # no app-wide lock: this waits on the network

    @app.post("/api/travel/trip")
    def travel_trip():
        """Give the stops and nights; the system tries the orders and dates, searches each leg once and ranks packages. Nothing is booked."""
        res = em.plan_trip(body(TripRequest), today_fn())  # no app-wide lock: this waits on the network
        return out(res)

    @app.post("/api/travel/multi-city")
    def travel_multi_city():
        """Built from separate one-way searches (Google does not serve multi-city to fli); the response says so."""
        b = body(MultiCityBody)
        for leg in b.legs:
            check_date(leg.date, today_fn())
        res = em.search_multi_city([(l.origin, l.destination, l.date) for l in b.legs], adults=b.adults, cabin=b.cabin, max_stops=b.max_stops, limit=b.limit)
        return out(res)

    @app.post("/api/tradeoffs")
    def tradeoffs():
        b = body(PlanBody)
        with lock:
            report = em.tradeoffs(b.request, today=today_fn(), guest_list_id=b.guest_list_id)
        return out(report)

    @app.post("/api/tradeoffs/apply")
    def apply_tradeoff():
        """Runs the chosen option through the full pipeline; only ``option.request`` is used, and it is re-validated."""
        b = body(ApplyBody)
        with lock:
            outcome = em.apply_tradeoff(b.option, today=today_fn(), guest_list_id=b.guest_list_id)
        payload = {
            "status": outcome.status,
            "plan": json.loads(outcome.plan.model_dump_json()) if outcome.plan else None,
            "violations": [v.model_dump() for v in outcome.violations],
            "tasks": [{"id": t.id, "agent": t.agent.value, "status": t.status.value, "depends_on": list(t.depends_on)} for t in outcome.task_graph.tasks],
        }
        return jsonify(payload), (201 if outcome.plan else 422)

    @app.get("/api/plans/<plan_id>")
    def get_plan(plan_id):
        plan_id, version = pid(plan_id), version_arg()
        with lock:
            doc = json.loads(em.store.load(plan_id, version).model_dump_json())
            doc["current_verification"] = em.verification(plan_id, version)  # not stored with the plan: it reflects today's data
        return jsonify(doc)

    @app.get("/api/plans/<plan_id>/versions")
    def versions(plan_id):
        with lock:
            vs = em.store.versions(pid(plan_id))
        return jsonify(
            [
                {
                    "version": p.version,
                    "parent_version": p.parent_version,
                    "created_at": p.created_at.isoformat(),
                    "change_reason": p.change_reason,
                    "total_cost": p.total_cost,
                    "event_date": p.request.event_date.isoformat(),
                    "guest_count": p.request.guest_count,
                    "budget_total": p.request.budget_total,
                }
                for p in vs
            ]
        )

    @app.get("/api/plans/<plan_id>/diff")
    def diff(plan_id):
        old, new = version_arg("old"), version_arg("new")
        if old is None or new is None:
            raise ValueError("old and new are required")
        with lock:
            return out(em.diff(pid(plan_id), old, new))

    @app.get("/api/plans/<plan_id>/explanation")
    def explanation(plan_id):
        with lock:
            ex = em.explain(pid(plan_id), version_arg())
        return jsonify({"markdown": ex.markdown, "used_llm": ex.used_llm})

    @app.get("/api/plans/<plan_id>/decisions")
    def decisions(plan_id):
        with lock:
            plan = em.store.load(pid(plan_id), version_arg())
            entries = em.decisions_for(plan)
        return Response("[" + ",".join(e.model_dump_json() for e in entries) + "]", mimetype="application/json")

    @app.get("/api/plans/<plan_id>/validate")
    def validate(plan_id):
        with lock:
            report = em.revalidate(pid(plan_id), version_arg())
        return out(report)

    @app.post("/api/plans/<plan_id>/confirm")
    def confirm(plan_id):
        b = body(ConfirmBody)
        with lock:
            res = em.confirm(pid(plan_id), b.category)
        return out(res, 200 if res.status == "replanned" else 409)

    @app.post("/api/plans/<plan_id>/replan")
    def replan(plan_id):
        d = body(Disruption)
        with lock:
            res = em.replan(pid(plan_id), d, today_fn())
        return out(res, 200 if res.status == "replanned" else (409 if res.status == "needs_decision" else 422))

    # The agent's background loop: send what is approved and read replies. Off unless a channel is set up (or EVENTMATE_POLL_SECONDS says otherwise).
    if poll_seconds is None:
        raw = os.environ.get("EVENTMATE_POLL_SECONDS", "").strip()
        ready = any(c.status().can_send or c.status().can_receive for c in em.conversations.channels.values())
        poll_seconds = float(raw) if raw.isdigit() else (60.0 if ready else 0.0)
    if poll_seconds > 0:
        stop = threading.Event()

        def poll_round():
            try:
                with lock:
                    em.background_round()
            except Exception as exc:  # one failed round must not end the agent; the next round retries
                print(f"conversation poll failed: {type(exc).__name__}", file=sys.stderr)

        def loop():
            while not stop.wait(max(5.0, poll_seconds)):
                poll_round()

        threading.Thread(target=loop, name="eventmate-conversations", daemon=True).start()
        app.config["POLL_STOP"], app.config["POLL_ROUND"] = stop, poll_round

    return app
