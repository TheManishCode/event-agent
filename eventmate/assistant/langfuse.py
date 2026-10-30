"""Optional export of assistant traces and scores to a Langfuse server (self-hosted or cloud).

Off unless LANGFUSE_HOST (or LANGFUSE_BASE_URL), LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are all set; keys come from the environment only. What
leaves the machine is exactly what the local trace already holds: PII-masked text, hashes and step metadata. Export runs on a
background thread and can never delay or break an answer. A non-local host must use https, so keys and traces are not sent in clear.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from eventmate.assistant.trace import FeedbackEvent, TraceEntry

log = logging.getLogger("eventmate.langfuse")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
BATCH = 50
TIMEOUT = 5


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def trace_events(t: TraceEntry) -> List[Dict[str, Any]]:
    """Map one trace to Langfuse ingestion events: a trace, a span or generation per step, sessions keyed by plan."""
    end = t.timestamp
    start = end - timedelta(milliseconds=t.total_ms)
    events: List[Dict[str, Any]] = [{
        "id": uuid.uuid4().hex, "type": "trace-create", "timestamp": _iso(start),
        "body": {"id": t.request_id, "timestamp": _iso(start), "name": "eventmate-assistant", "userId": t.user_id, "sessionId": t.plan_id,
                 "input": t.question_masked, "output": t.answer_masked, "tags": [t.route, t.outcome, f"cache:{t.cache}", *t.roles],
                 "metadata": {"prompt_sha256": t.prompt_sha256, "kb_version": t.kb_version, "cited": list(t.cited), "timing": "step times are laid end to end"}},
    }]
    cursor = start
    for i, s in enumerate(t.steps):
        s_start, cursor = cursor, cursor + timedelta(milliseconds=s.ms)
        body: Dict[str, Any] = {"id": f"{t.request_id}-{i}", "traceId": t.request_id, "name": f"{s.kind}:{s.name}", "startTime": _iso(s_start), "endTime": _iso(cursor),
                                "level": "DEFAULT" if s.ok else "WARNING", "statusMessage": s.detail or None, "metadata": {"kind": s.kind}}
        kind = "generation-create" if s.kind == "llm" and s.name not in ("unusable_reply",) and s.ok else "span-create"
        if kind == "generation-create":
            body["model"] = s.name
            body["usageDetails"] = {"input": t.tokens_in_est, "output": t.tokens_out_est}  # estimates (chars / 4), not billed usage
        events.append({"id": uuid.uuid4().hex, "type": kind, "timestamp": _iso(s_start), "body": body})
    return events


def score_event(f: FeedbackEvent) -> Dict[str, Any]:
    return {"id": uuid.uuid4().hex, "type": "score-create", "timestamp": _iso(f.timestamp),
            "body": {"id": f.entry_id, "traceId": f.request_id, "name": "user-feedback", "value": f.value, "dataType": "NUMERIC", "comment": f.comment or None}}


class LangfuseExporter:
    def __init__(self, host: str, public_key: str, secret_key: str, post: Optional[Callable[[str, bytes, Dict[str, str]], None]] = None):
        u = urlparse(host)
        if u.scheme not in ("http", "https") or not u.hostname or (u.scheme == "http" and u.hostname not in LOCAL_HOSTS):
            raise ValueError("LANGFUSE_HOST must be https (http only for localhost)")
        self.url = host.rstrip("/") + "/api/public/ingestion"
        self.headers = {"Content-Type": "application/json", "Authorization": "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()}
        self._post = post or self._http
        self._q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=2000)
        self.sent = self.failed = self.dropped = 0
        self._pending = 0
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True, name="langfuse-export").start()

    @classmethod
    def from_env(cls) -> Optional["LangfuseExporter"]:
        host = os.environ.get("LANGFUSE_HOST") or os.environ.get("LANGFUSE_BASE_URL")  # Langfuse's own SDKs say BASE_URL
        pub, sec = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
        if not (host and pub and sec):
            return None
        try:
            return cls(host, pub, sec)
        except ValueError as exc:
            log.warning("Langfuse export disabled: %s", exc)
            return None

    def export_trace(self, t: TraceEntry) -> None:
        for e in trace_events(t):
            self._enqueue(e)

    def export_score(self, f: FeedbackEvent) -> None:
        self._enqueue(score_event(f))

    def _enqueue(self, event: Dict[str, Any]) -> None:
        try:
            self._q.put_nowait(event)
            with self._lock:
                self._pending += 1
        except queue.Full:
            self.dropped += 1  # shedding telemetry is better than slowing answers

    def flush(self, timeout: float = 5) -> None:
        """Wait (bounded) for queued events to be sent; used by tests and at shutdown."""
        end = time.monotonic() + timeout
        while time.monotonic() < end and self._pending:
            time.sleep(0.01)

    def _run(self) -> None:
        while True:
            batch = [self._q.get()]
            while len(batch) < BATCH:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            try:
                self._post(self.url, json.dumps({"batch": batch, "metadata": {"sdk": "eventmate"}}).encode(), self.headers)
                self.sent += len(batch)
            except Exception as exc:  # network, auth, server: count it, never raise into the app
                self.failed += len(batch)
                log.warning("Langfuse export failed: %s", type(exc).__name__)
            with self._lock:
                self._pending -= len(batch)

    @staticmethod
    def _http(url: str, data: bytes, headers: Dict[str, str]) -> None:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310  (scheme checked in __init__)
            if resp.status >= 300:
                raise urllib.error.HTTPError(url, resp.status, "ingestion refused", resp.headers, None)
