"""PDDL symbolic feasibility check using the base repo's solver toolchain.

PDDL is generated deterministically from the schedule's dependency structure
(never by an LLM): one propositional action per task, preconditions are its
dependencies, the goal is every task done. Fast Downward (as configured in the
base repo's ``config.json``) finds a plan; VAL independently validates it.
Tasks already done (confirmed bookings) are initial facts.

Binary lookup order: constructor args, env ``EVENTMATE_FD`` / ``EVENTMATE_VAL``,
the base repo ``config.json``, then ``solvers/`` built by this project. If the
binaries are missing the result is ``unavailable`` (never a false ``valid``).
Subprocesses run without a shell, in a temp dir, with a timeout and a scrubbed
environment (so API keys are never exposed to them).
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404: used only with fixed argv lists (see _run calls)
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from eventmate.logs import ToolCallLog
from eventmate.schemas import ScheduleSpec, SymbolicResult

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_TIMEOUT_S = 300.0
_MAX_OUTPUT = 4000


def build_pddl(spec: ScheduleSpec, already_done: Sequence[str] = ()) -> Tuple[str, str, List[str]]:
    ids = [t.id for t in spec.tasks]
    unknown = set(already_done) - set(ids)
    if unknown:
        raise ValueError(f"already_done contains unknown tasks: {sorted(unknown)}")
    done0 = set(already_done)
    preds = " ".join(f"(done_{i})" for i in ids)
    deps = {i: sorted({d.before for d in spec.dependencies if d.after == i}) for i in ids}
    actions: List[str] = []
    blocks: List[str] = []
    for i in ids:
        if i in done0:
            continue
        pre = " ".join(f"(done_{d})" for d in deps[i])
        blocks.append(
            f"  (:action do_{i}\n    :parameters ()\n    :precondition (and {pre})\n    :effect (done_{i}))"
        )
        actions.append(f"do_{i}")
    domain = (
        "(define (domain eventmate)\n  (:requirements :strips)\n"
        f"  (:predicates {preds})\n" + "\n".join(blocks) + ")\n"
    )
    init = " ".join(f"(done_{i})" for i in sorted(done0))
    goal = " ".join(f"(done_{i})" for i in ids)
    problem = (
        "(define (problem event_plan)\n  (:domain eventmate)\n"
        f"  (:init {init})\n  (:goal (and {goal})))\n"
    )
    return domain, problem, actions


def order_respects_dependencies(spec: ScheduleSpec, actions: Sequence[str], already_done: Sequence[str] = ()) -> bool:
    """Independent (pure Python) check that a linear plan honours every precedence."""
    done = set(already_done)
    wanted = {t.id for t in spec.tasks} - done
    deps = {t.id: {d.before for d in spec.dependencies if d.after == t.id} for t in spec.tasks}
    seen = set()
    for a in actions:
        if not a.startswith("do_"):
            return False
        task = a[3:]
        if task not in wanted or task in seen or not deps[task] <= done:
            return False
        done.add(task)
        seen.add(task)
    return seen == wanted


def _locate(candidates: Sequence[Optional[Path]]) -> Optional[Path]:
    for c in candidates:
        if c is not None and Path(c).is_file():
            return Path(c)
    return None


def _base_config() -> dict:
    try:
        return json.loads((REPO_ROOT / "config.json").read_text())
    except (OSError, ValueError):
        return {}


class SymbolicChecker:
    def __init__(self, fd_path: Optional[Path] = None, val_path: Optional[Path] = None, timeout_s: float = 60.0):
        if not 0 < timeout_s <= MAX_TIMEOUT_S:
            raise ValueError(f"timeout_s must be in (0, {MAX_TIMEOUT_S}]")
        cfg = _base_config()
        env_fd, env_val = os.environ.get("EVENTMATE_FD"), os.environ.get("EVENTMATE_VAL")
        self.fd = _locate(
            [fd_path] if fd_path else [
                Path(env_fd) if env_fd else None,
                REPO_ROOT / cfg["solver_fd_binary"] if cfg.get("solver_fd_binary") else None,
                REPO_ROOT / "solvers/fast-downward-24.06.1/fast-downward.py",
            ]
        )
        self.val = _locate(
            [val_path] if val_path else [
                Path(env_val) if env_val else None,
                REPO_ROOT / cfg["validator_bin"] if cfg.get("validator_bin") else None,
                REPO_ROOT / "solvers/VAL/build/bin/Validate",
            ]
        )
        self.timeout_s = float(timeout_s)
        self._fd_requested, self._val_requested = fd_path, val_path

    @property
    def available(self) -> bool:
        return self.fd is not None and self.val is not None

    @staticmethod
    def _env() -> dict:
        return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C"}

    def plan_pddl(self, domain: str, problem: str) -> Tuple[str, Tuple[str, ...], str]:
        """Run Fast Downward. Returns (status, actions, detail); status in ok/no_plan/error."""
        with tempfile.TemporaryDirectory(prefix="eventmate-fd-") as tmp:
            d, p, plan = Path(tmp, "domain.pddl"), Path(tmp, "problem.pddl"), Path(tmp, "plan.txt")
            d.write_text(domain)
            p.write_text(problem)
            cmd = [sys.executable, str(self.fd), "--plan-file", str(plan), "--alias", "lama-first", str(d), str(p)]
            try:
                out = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, timeout=self.timeout_s, env=self._env(), shell=False)  # nosec B603: fixed argv list, no shell, trusted solver paths, temp-dir args
            except subprocess.TimeoutExpired:
                return "error", (), f"planner timed out after {self.timeout_s}s"
            except OSError as exc:
                return "error", (), f"planner could not start: {exc}"
            if plan.exists():
                actions = tuple(line.strip().strip("()").strip() for line in plan.read_text().splitlines() if line.startswith("("))
                return "ok", actions, "plan found"
            text = (out.stdout + out.stderr)[-_MAX_OUTPUT:]
            if "unsolvable" in text.lower() or "no solution" in text.lower():
                return "no_plan", (), "planner proved the problem unsolvable"
            return "error", (), f"planner exit code {out.returncode}: {text[-300:]}"

    def validate_plan(self, domain: str, problem: str, actions: Sequence[str]) -> Tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="eventmate-val-") as tmp:
            d, p, plan = Path(tmp, "domain.pddl"), Path(tmp, "problem.pddl"), Path(tmp, "plan.txt")
            d.write_text(domain)
            p.write_text(problem)
            plan.write_text("".join(f"({a})\n" for a in actions))
            try:
                out = subprocess.run([str(self.val), str(d), str(p), str(plan)], cwd=tmp, capture_output=True, text=True, timeout=self.timeout_s, env=self._env(), shell=False)  # nosec B603: fixed argv list, no shell, trusted solver paths, temp-dir args
            except subprocess.TimeoutExpired:
                return False, "validator timed out"
            except OSError as exc:
                return False, f"validator could not start: {exc}"
            text = out.stdout + out.stderr
            ok = "Successful plans:" in text and "Failed plans:" not in text
            return ok, ("VAL accepted the plan" if ok else "VAL rejected the plan: " + text[-300:].strip())

    def check(
        self,
        spec: ScheduleSpec,
        already_done: Sequence[str] = (),
        tool_log: Optional[ToolCallLog] = None,
        request_id: str = "unknown",
    ) -> SymbolicResult:
        started = time.perf_counter()
        result = self._check(spec, already_done)
        if tool_log is not None:
            tool_log.log(
                request_id=request_id,
                tool="pddl.check",
                args={"tasks": len(spec.tasks), "already_done": len(already_done), "timeout_s": self.timeout_s},
                ok=result.status == "valid",
                result=f"{result.status}: {result.detail}"[:1900],
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        return result

    def _check(self, spec: ScheduleSpec, already_done: Sequence[str]) -> SymbolicResult:
        if not self.available:
            missing = [n for n, v in (("Fast Downward", self.fd), ("VAL", self.val)) if v is None]
            return SymbolicResult(status="unavailable", detail="solver binaries not found: " + ", ".join(missing))
        domain, problem, _ = build_pddl(spec, already_done)
        status, actions, detail = self.plan_pddl(domain, problem)
        if status == "no_plan":
            return SymbolicResult(status="no_plan", detail=detail)
        if status != "ok":
            return SymbolicResult(status="error", detail=detail)
        if not order_respects_dependencies(spec, actions, already_done):
            return SymbolicResult(status="invalid", plan_actions=actions, detail="planner output violates a dependency")
        ok, vdetail = self.validate_plan(domain, problem, actions)
        return SymbolicResult(status="valid" if ok else "invalid", plan_actions=actions, detail=vdetail)
