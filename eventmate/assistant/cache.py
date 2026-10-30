"""Answer cache with single-flight, for the Monday-morning stampede.

Identical questions asked at the same moment run once; the rest wait and share the result. The key holds everything an
answer depends on: the asker's roles (never share across permissions), the plan and ledger state, the knowledge base version
and the prompt. A change to any of them is a different key, so nothing stale is served.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Callable, Dict, Tuple

from eventmate.assistant.trace import sha


def cache_key(question: str, roles: Tuple[str, ...], plan_id: str, plan_state: str, kb_version: str, prompt_sha: str) -> str:
    q = re.sub(r"\s+", " ", question.strip().lower())
    return sha(json.dumps([q, sorted(roles), plan_id, plan_state, kb_version, prompt_sha]))


class AnswerCache:
    def __init__(self, ttl_seconds: float = 300, max_items: int = 1000, wait_seconds: float = 30):
        self.ttl, self.max, self.wait = ttl_seconds, max_items, wait_seconds
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._inflight: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.hits = self.misses = self.coalesced = 0

    def _fresh(self, key: str):
        item = self._store.get(key)
        if item and time.monotonic() - item[0] <= self.ttl:
            return item[1]
        self._store.pop(key, None)
        return None

    def get_or_compute(self, key: str, compute: Callable[[], Tuple[Any, bool]]) -> Tuple[Any, str]:
        """``compute`` returns (value, cacheable). Returns (value, "hit" | "miss" | "coalesced")."""
        with self._lock:
            val = self._fresh(key)
            if val is not None:
                self.hits += 1
                return val, "hit"
            event = self._inflight.get(key)
            leader = event is None
            if leader:
                event = self._inflight[key] = threading.Event()
        if not leader:
            event.wait(self.wait)
            with self._lock:
                val = self._fresh(key)
                if val is not None:
                    self.coalesced += 1
                    return val, "coalesced"
            return compute()[0], "miss"  # the leader's answer was not cacheable or timed out: do our own work
        try:
            value, cacheable = compute()
            with self._lock:
                self.misses += 1
                if cacheable:
                    if len(self._store) >= self.max:
                        self._store.pop(min(self._store, key=lambda k: self._store[k][0]))
                    self._store[key] = (time.monotonic(), value)
            return value, "miss"
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()
