"""Shared append-only JSONL writer.

Every log channel (decisions, tool calls, LLM traces, validation failures)
goes through this class so they all get the same guarantees: entries are
validated Pydantic models before anything touches disk, files are created
owner-only (0600), symlinks are refused, and writes are serialised.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Generic, List, Optional, Type, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")


class JsonlLog(Generic[M]):
    channel = "log"
    model: Type[M]

    def __init__(self, log_dir: Optional[Path] = None, run_id: Optional[str] = None):
        run_id = run_id if run_id is not None else uuid.uuid4().hex
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("run_id must match [A-Za-z0-9_-]{1,64}")
        self.run_id = run_id
        self.log_dir = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.log_dir / f"{self.channel}-{run_id}.jsonl"
        self.entry_ids: List[str] = []
        self._lock = threading.Lock()

    def _append(self, entry: M, entry_id: str) -> M:
        line = (entry.model_dump_json() + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        with self._lock:
            fd = os.open(self.path, flags, 0o600)
            try:
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            self.entry_ids.append(entry_id)
        return entry

    def read(self) -> List[M]:
        """Load every entry; a corrupt line raises rather than being skipped."""
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8") as fh:
            return [self.model.model_validate_json(line) for line in fh if line.strip()]


class InterProcessLock:
    """An exclusive lock for read-check-write steps over a JSONL store: a thread lock plus a file lock, so two processes cannot interleave.
    Re-entrant within one thread."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._thread = threading.RLock()
        self._depth = 0

    @contextlib.contextmanager
    def __call__(self):
        with self._thread:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
