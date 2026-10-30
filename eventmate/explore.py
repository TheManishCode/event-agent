"""Free-text front door: one model call picks one read-only search; code validates it and runs it (ADR-034).

The model only chooses among a fixed set of searches and fills their parameters. Its output is untrusted data: it is parsed strictly,
validated by the same models the forms use, and refused if anything is off. Nothing it says is shown as a fact, and no search it can
pick writes, books, pays or sends anything. At most two model calls are made per request, with a small token cap.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import Field, ValidationError

from eventmate.discovery import clean_city
from eventmate.llm import LLMError, PromptStore
from eventmate.travel import check_date
from eventmate.schemas import DealQuery, TripRequest, _Model

MAX_TEXT = 600
MAX_REPLY_CHARS = 4000
MAX_MODEL_CALLS = 2
MAX_TOKENS = 400
_FENCE = re.compile(r"<<<|>>>")
_IATA = r"^[A-Z]{3}$"


class _Unusable(ValueError):
    """The model's reply was not a usable search at all (no JSON, unknown action): nothing specific to tell the user."""


class ExploreError(Exception):
    """The request could not be turned into a valid search. The message is safe to show."""


class ExploreNotConfigured(ExploreError):
    pass


class _Flights(_Model):
    origin: str = Field(pattern=_IATA)
    destination: str = Field(pattern=_IATA)
    date: date
    adults: int = Field(default=1, ge=1, le=9)


class _Weather(_Model):
    city: str = Field(min_length=1, max_length=60)
    date: date


class _Discover(_Model):
    kind: str = Field(pattern=r"^(venue|hotel|restaurant)$")
    city: str = Field(min_length=1, max_length=60)


class _Places(_Model):
    kind: str = Field(pattern=r"^(caterer|venue|photographer|hotel)$")
    city: str = Field(min_length=1, max_length=60)
    reviews: bool = False


class _None(_Model):
    reason: str = Field(default="", max_length=200)


MODELS = {"deals": DealQuery, "places": _Places, "trip": TripRequest, "flights": _Flights, "weather": _Weather, "discover": _Discover, "none": _None}


@dataclass(frozen=True)
class Routed:
    action: str
    params: Dict[str, Any]
    result: Optional[Dict[str, Any]]
    note: Optional[str]
    model: str


def _parse(output: str) -> Dict[str, Any]:
    if not isinstance(output, str) or not output.strip() or len(output) > MAX_REPLY_CHARS:
        raise _Unusable("empty or oversized reply")
    text = re.sub(r"^```(?:json)?|```$", "", output.strip(), flags=re.M).strip()
    start = text.find("{")
    if start < 0:
        raise _Unusable("no JSON object")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except RecursionError:
        raise _Unusable("JSON is nested too deeply") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("action"), str):
        raise _Unusable("no action")
    return obj


def _validate(obj: Dict[str, Any], today: date):
    action = obj["action"]
    if action not in MODELS:
        raise _Unusable("unknown action")
    # the prompt tells the model to use null for anything the person did not say, so null means "not provided"
    model = MODELS[action].model_validate({k: v for k, v in obj.items() if k != "action" and v is not None})
    if getattr(model, "city", None):
        clean_city(model.city)  # the same allow-list every search applies, checked at the door
    day = getattr(model, "date", None) or getattr(model, "earliest_start", None)
    if day is not None:
        check_date(day, today)
    return action, model


def _run(em, action: str, model, today: date):
    if action == "deals":
        return em.find_deals(model)
    if action == "places":
        return em.find_places(model.kind, model.city, model.reviews)
    if action == "trip":
        return em.plan_trip(model, today)
    if action == "flights":
        return em.search_flights(model.origin, model.destination, model.date, adults=model.adults)
    if action == "weather":
        return em.weather_forecast(model.city, model.date)
    if action == "discover":
        return em.discover_venues(model.city, model.kind)
    return None


def build_prompt(text: str, today: date):
    body, _ = PromptStore().load("explore.route")
    head, tail = body.split("\nRequest:\n", 1)
    return head.replace("{today}", today.isoformat()), "Request:\n" + tail.replace("{request}", _FENCE.sub(" ", text.strip()))


def route(em, text: str, today: date, clients: List[Any]) -> Routed:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise ExploreError(f"write between 1 and {MAX_TEXT} characters")
    if not clients:
        raise ExploreNotConfigured("language search is not configured: add NVIDIA_API_KEY or OPENROUTER_API_KEY to the .env file, or use the forms")
    system, user = build_prompt(text, today)
    problems: List[str] = []
    attempts = (list(clients) * MAX_MODEL_CALLS)[:MAX_MODEL_CALLS] if len(clients) == 1 else list(clients)[:MAX_MODEL_CALLS]  # a lone provider gets one retry
    for client in attempts:
        try:
            output = client.complete(system, user, MAX_TOKENS)
        except LLMError as exc:
            problems.append(str(exc))
            continue
        try:
            action, model = _validate(_parse(output), today)
        except _Unusable:
            problems.append(f"{getattr(client, 'name', 'the model')} gave an answer that could not be used")
            continue
        except ValidationError as exc:
            fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})[:4]
            problems.append("missing or invalid: " + ", ".join(fields))
            continue
        except ValueError as exc:  # our own policy messages: a date out of range, a city with odd characters
            problems.append(str(exc)[:120])
            continue
        used = getattr(client, "name", "model")
        if action == "none":
            return Routed("none", {}, None, model.reason or "That is not something I can search for.", used)
        try:
            result = _run(em, action, model, today)
        except ValueError as exc:  # the search itself refused the values: not a service failure
            raise ExploreError(f"that search was refused: {str(exc)[:200]}") from None
        return Routed(action, json.loads(model.model_dump_json()), json.loads(result.model_dump_json()), None, used)
    raise ExploreError("I could not turn that into a search (" + "; ".join(dict.fromkeys(problems))[:300] + "); try the forms instead")
