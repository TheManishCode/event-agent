"""In-memory stage pulse for the arcade UI (``/arcade``).

Records which pipeline stage is running *right now* so the arcade page can
light the matching module while real work happens. This is observability only:

- it changes no decisions and writes nothing to disk;
- hooks are cheap no-ops while no run is active (CLI, tests, other UIs);
- a stage is only ever lit by a hook in the real pipeline, never by the page.
  If a hook is missing or a stage is skipped, the module stays dark - the page
  cannot invent progress.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

# The stages the arcade shows, in pipeline order. Names are stable API.
STAGES = (
    "parse",      # deterministic brief parsing (Manager)
    "decompose",  # task graph + AOP checks (Manager)
    "budget",     # exact integer allocation + contingency (Budget Agent)
    "guests",     # headcount + dietary roll-up (Guest Agent)
    "vendors",    # per-category shortlists, every hard constraint checked (Vendor Agent)
    "select",     # exhaustive vendor-combination search (Planner)
    "schedule",   # CP-SAT schedule solve (Scheduling Agent)
    "symbolic",   # PDDL feasibility: Fast Downward + VAL (SymbolicChecker)
    "validate",   # independent re-derivation of every fact (Validator)
    "store",      # immutable version write + explanation (PlanStore / Explainer)
)


class ArcadePulse:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_id = 0
        self._label = ""
        self._state = "idle"  # idle | running | done | failed
        self._error = ""
        self._stages: Dict[str, Dict[str, Optional[float]]] = {}
        self._t0 = 0.0

    def begin_run(self, label: str) -> int:
        with self._lock:
            self._run_id += 1
            self._label = label[:120]
            self._state = "running"
            self._error = ""
            self._t0 = time.monotonic()
            self._stages = {name: {"state": "waiting", "t0": None, "t1": None} for name in STAGES}
            return self._run_id

    def stage_started(self, name: str) -> None:
        with self._lock:
            if self._state != "running":
                return
            st = self._stages.get(name)
            if not st or st["state"] == "done":
                return
            st["state"] = "active"
            st["t0"] = time.monotonic()

    def stage_done(self, name: str) -> None:
        with self._lock:
            if self._state != "running":
                return
            st = self._stages.get(name)
            if not st or st["state"] == "done":
                return
            if st["t0"] is None:
                st["t0"] = time.monotonic()
            st["t1"] = time.monotonic()
            st["state"] = "done"

    def finish(self, ok: bool, error: str = "") -> None:
        with self._lock:
            if self._state != "running":
                return
            now = time.monotonic()
            for st in self._stages.values():
                if st["state"] == "active":
                    st["state"] = "done" if ok else "failed"
                    st["t1"] = now
            self._state = "done" if ok else "failed"
            self._error = error[:300]

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            stages: List[dict] = []
            for name in STAGES:
                st = self._stages.get(name)
                if not st:
                    stages.append({"name": name, "state": "waiting", "ms": None})
                    continue
                ms: Optional[float] = None
                if st["t0"] is not None:
                    ms = round(((st["t1"] or now) - st["t0"]) * 1000, 1)
                stages.append({"name": name, "state": st["state"], "ms": ms})
            return {
                "run_id": self._run_id,
                "label": self._label,
                "state": self._state,
                "error": self._error,
                "elapsed_ms": round((now - self._t0) * 1000, 1) if self._state != "idle" else None,
                "stages": stages,
            }


# One pulse per process; the web server is single-user and serialises runs.
pulse = ArcadePulse()
