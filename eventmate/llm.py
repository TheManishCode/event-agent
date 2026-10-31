"""Optional LLM polish for explanations, behind hard guard rails.

The deterministic explanation is always the source of truth. An LLM may only
reword it, and its output is accepted only if it adds or changes no number,
keeps every vendor name, and contains no markup or links. Anything else falls
back to the deterministic text. Prompts are versioned Markdown under /prompts.

Clients come from NVIDIA and/or OpenRouter (ADR-032): none is created unless a key is present in the
environment (never in files) and EVENTMATE_ENABLE_LLM is not 0.

What each model stage is sent (see docs/MODEL_INPUTS.md): the explanation stage gets only the already-verified plan summary (no raw request,
no guest names). Intake gets the user's message and the reviewed draft, with contact details replaced by placeholders. The assistant gets the question
(placeholders for contact details), a short fenced conversation, and tool results. Ideas get eight fact fields. See each stage's masking there.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

from eventmate.netutil import NETWORK_ERRORS, loads, read_capped
from typing import Iterable, Optional, Protocol, Tuple

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,3}")
_NUMBER = re.compile(r"\d[\d,.:]*\d|\d")
# Number words can restate a fact in prose ("sixty thousand"), so they are held to the same rule as digits.
# "one" is exempt (it is ordinary prose: "one of the vendors"); "a"/"an" carry no value.
_NUMBER_WORDS = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen"
    r"|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|lakhs?|lacs?|crores?|million|billion|percent|dozen|zero)\b",
    re.I,
)
_MARKUP = re.compile(r"<[A-Za-z/!][^>]*>|```|\]\(|javascript:", re.I)
_LINK = re.compile(r"https?://|www\.|\b[\w.-]+@[\w.-]+\.\w+", re.I)


class PromptError(Exception):
    pass


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, user: str, max_tokens: int) -> str: ...


class PromptStore:
    def __init__(self, directory: Optional[Path] = None):
        self.dir = Path(directory) if directory is not None else PROMPT_DIR

    def load(self, prompt_id: str) -> Tuple[str, int]:
        """Return (body, version) of the highest version of ``prompt_id``."""
        if not isinstance(prompt_id, str) or not _PROMPT_ID.fullmatch(prompt_id):
            raise PromptError(f"invalid prompt id {prompt_id!r}")
        best: Optional[Tuple[int, Path]] = None
        for p in self.dir.glob(f"{prompt_id}.v*.md"):
            m = re.fullmatch(re.escape(prompt_id) + r"\.v(\d+)\.md", p.name)
            if m and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), p)
        if best is None:
            raise PromptError(f"no prompt named {prompt_id!r}")
        text = best[1].read_text(encoding="utf-8")
        if text.startswith("---"):
            parts = text.split("---", 2)  # drop the YAML-ish front matter
            text = parts[2] if len(parts) == 3 else text
        return text.strip(), best[0]


def guard_rewrite(output: str, source: str, names: Iterable[str]) -> Optional[str]:
    """Return None if ``output`` is an acceptable rewording of ``source``, else the reason."""
    if not output.strip():
        return "empty output"
    if len(output) > 3 * len(source) + 200:
        return "output length out of range"
    if _MARKUP.search(output):
        return "markup not allowed"
    if _LINK.search(output) and not _LINK.search(source):
        return "link not allowed"
    allowed = set(_NUMBER.findall(source))
    extra = sorted(set(_NUMBER.findall(output)) - allowed)
    if extra:
        return f"number not in the verified plan: {extra[:5]}"
    new_words = sorted({w.lower() for w in _NUMBER_WORDS.findall(output)} - {w.lower() for w in _NUMBER_WORDS.findall(source)})
    if new_words:
        return f"number word not in the verified plan: {new_words[:5]}"
    missing = [n for n in names if n not in output]
    if missing:
        return f"vendor name changed or dropped: {missing[:3]}"
    return None


# ------------------------------------------------------------------ providers
# NVIDIA's hosted API and OpenRouter both speak the OpenAI chat-completions protocol (ADR-032). Keys come from the environment only.

MODELS_PATH = Path(__file__).resolve().parent / "data" / "llm_models.json"
MAX_REPLY_BYTES = 2_000_000
TIMEOUT_S = 90
_KEY_OK = re.compile(r"[A-Za-z0-9_.-]{8,200}")
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


class LLMError(Exception):
    """A model call failed. The message names the provider, never contains a key, and is safe to show."""


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except ValueError:
        return default


class UsageMeter:
    """Counts model calls so a bug or a loop cannot burn credits. Failed calls count: the provider may still have billed them."""

    def __init__(self, max_calls: Optional[int] = None):
        self._max = max_calls
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.calls, self.by_provider = 0, {}

    def charge(self, provider: str) -> None:
        with self._lock:
            limit = self._max if self._max is not None else _int_env("EVENTMATE_LLM_MAX_CALLS", 300, 1, 100000)
            if self.calls >= limit:
                raise LLMError(f"the model call budget ({limit}) for this run is used up; restart to reset it or raise EVENTMATE_LLM_MAX_CALLS")
            self.calls += 1
            self.by_provider[provider] = self.by_provider.get(provider, 0) + 1

    def status(self) -> dict:
        with self._lock:
            return {"calls": self.calls, "by_provider": dict(self.by_provider)}


METER = UsageMeter()


def _post(url: str, headers: dict, data: bytes, timeout: float) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as resp:
        return read_capped(resp, MAX_REPLY_BYTES + 1)


class OpenAICompatClient:
    def __init__(self, name: str, base_url: str, api_key: str, model: str, transport=_post, meter: Optional[UsageMeter] = None, extra: Optional[dict] = None, timeout: float = TIMEOUT_S):
        if not base_url.startswith("https://"):
            raise ValueError("a model endpoint must be https")
        self.name, self.model, self._url, self._key, self._transport, self._meter = name, model, base_url.rstrip("/") + "/chat/completions", api_key, transport, meter or METER
        self._extra, self._timeout = dict(extra or {}), timeout

    def __repr__(self) -> str:
        return f"OpenAICompatClient({self.name}, {self.model}, key=<hidden>)"

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        cap = _int_env("EVENTMATE_LLM_MAX_TOKENS", 2000, 16, 8000)
        if not isinstance(max_tokens, int) or max_tokens < 1:
            raise LLMError(f"{self.name}: max_tokens must be a positive whole number")
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": min(max_tokens, cap), "temperature": 0, **self._extra}
        self._meter.charge(self.name)
        try:
            raw = self._transport(self._url, {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json", "Accept": "application/json", "X-Title": "EventMate"}, json.dumps(body).encode(), self._timeout)
        except urllib.error.HTTPError as exc:
            reason = {401: "rejected the API key", 403: "refused the request", 402: "is out of credit", 429: "rate limit reached"}.get(exc.code, f"failed (HTTP {exc.code})")
            raise LLMError(f"{self.name} {reason}") from None
        except NETWORK_ERRORS:
            raise LLMError(f"{self.name} could not be reached") from None
        if len(raw) > MAX_REPLY_BYTES:
            raise LLMError(f"{self.name} returned an oversized reply")
        try:
            content = loads(raw)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError(f"{self.name} returned something unreadable") from None
        if content is None:
            return ""  # e.g. the model spent its whole token budget thinking
        if not isinstance(content, str):
            raise LLMError(f"{self.name} returned something unreadable")
        return _THINK.sub("", content).strip()


class FallbackClient:
    """Tries each provider in order; the first that answers wins. ``last_used`` says which one did."""

    def __init__(self, clients):
        self.clients = list(clients)
        self.last_used: Optional[str] = None

    @property
    def name(self) -> str:
        return "+".join(f"{c.name}:{c.model}" for c in self.clients)

    def complete(self, system: str, user: str, max_tokens: int) -> str:
        errors = []
        for c in self.clients:
            try:
                out = c.complete(system, user, max_tokens)
                self.last_used = c.name
                return out
            except LLMError as exc:
                errors.append(str(exc))
        raise LLMError("no model answered: " + "; ".join(errors))


def _key(name: str) -> Optional[str]:
    v = os.environ.get(name, "").strip()
    return v if _KEY_OK.fullmatch(v) else None


def client_from_env(model: Optional[str] = None, tier: str = "heavy", transport=_post) -> Optional[FallbackClient]:
    """NVIDIA and/or OpenRouter, ordered by ``tier``; None when no key is set or EVENTMATE_ENABLE_LLM=0.

    heavy: NVIDIA first (the big model), OpenRouter's free heavy model as backup. light: OpenRouter's free light model first, NVIDIA as backup.
    ``model`` overrides the model of the first provider.
    """
    if os.environ.get("EVENTMATE_ENABLE_LLM") == "0":
        return None
    cfg = json.loads(MODELS_PATH.read_text(encoding="utf-8"))
    nv_key, or_key = _key("NVIDIA_API_KEY"), _key("OPENROUTER_API_KEY")
    built = {}
    if nv_key:
        nv = cfg["nvidia"]
        if tier == "light":
            light = nv.get("light", {})
            built["nvidia"] = OpenAICompatClient("nvidia", nv["base_url"], nv_key, os.environ.get("EVENTMATE_NVIDIA_MODEL_LIGHT") or light.get("model") or nv["model"], transport,
                                                 extra=light.get("extra"), timeout=light.get("timeout", nv.get("timeout", TIMEOUT_S)))
        else:
            built["nvidia"] = OpenAICompatClient("nvidia", nv["base_url"], nv_key, os.environ.get("EVENTMATE_NVIDIA_MODEL") or nv["model"], transport, timeout=nv.get("timeout", TIMEOUT_S))
    if or_key:
        env_name = "EVENTMATE_OPENROUTER_MODEL_LIGHT" if tier == "light" else "EVENTMATE_OPENROUTER_MODEL_HEAVY"
        built["openrouter"] = OpenAICompatClient("openrouter", cfg["openrouter"]["base_url"], or_key, os.environ.get(env_name) or cfg["openrouter"]["light" if tier == "light" else "heavy"], transport)
    order = ["openrouter", "nvidia"] if tier == "light" else ["nvidia", "openrouter"]
    clients = [built[n] for n in order if n in built]
    if not clients:
        return None
    if model:
        clients[0].model = model
    return FallbackClient(clients)
