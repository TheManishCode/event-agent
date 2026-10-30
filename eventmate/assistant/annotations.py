"""Approved answers: a human-curated question and answer that later, similar questions reuse with no model (the Dify annotation reply).

Only someone with the approval permission can create one, from a real answer's trace, and may edit the wording first. It applies
only to askers with the same roles, and only while the plan is in the state it was approved for; when the plan or ledger moves on it
stops matching (shown as stale) rather than serving an out-of-date fact. Matching is word-set similarity above a threshold.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import Field

from eventmate.assistant.guard import mask_pii
from eventmate.assistant.knowledge import _tokens
from eventmate.schemas import _Model

MATCH_THRESHOLD = 0.8
MAX_ANNOTATIONS = 500
_PLAIN = re.compile(r"[<>]")


class Annotation(_Model):
    id: str
    plan_id: str
    roles: Tuple[str, ...]
    question: str = Field(max_length=240)
    answer: str = Field(min_length=1, max_length=1500)
    citations: Tuple[str, ...] = ()
    state: str  # plan version : ledger entries when approved
    created_by: str
    created_at: datetime


def similarity(a: str, b: str) -> float:
    x, y = set(_tokens(a)), set(_tokens(b))
    return len(x & y) / len(x | y) if x and y else 0.0


class AnnotationStore:
    def __init__(self, directory: Path):
        self.path = Path(directory) / "assistant_annotations.json"
        self._lock = threading.Lock()
        self._items: List[Annotation] = []
        if self.path.exists():
            self._items = [Annotation.model_validate(a) for a in json.loads(self.path.read_text(encoding="utf-8"))]

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([json.loads(a.model_dump_json()) for a in self._items], indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, *, plan_id: str, roles: Tuple[str, ...], question: str, answer: str, citations: Tuple[str, ...], state: str, created_by: str) -> Annotation:
        answer = mask_pii(answer.strip())
        if not answer or _PLAIN.search(answer):
            raise ValueError("an approved answer must be plain text")
        with self._lock:
            if len(self._items) >= MAX_ANNOTATIONS:
                raise ValueError("too many approved answers; remove some first")
            a = Annotation(id=uuid.uuid4().hex[:12], plan_id=plan_id, roles=tuple(sorted(roles)), question=mask_pii(question)[:240], answer=answer[:1500],
                           citations=citations, state=state, created_by=created_by, created_at=datetime.now(timezone.utc))
            self._items = [x for x in self._items if not (x.plan_id == plan_id and x.roles == a.roles and x.question == a.question)] + [a]  # re-approving replaces
            self._save()
            return a

    def remove(self, annotation_id: str, plan_id: str) -> bool:
        with self._lock:
            kept = [a for a in self._items if not (a.id == annotation_id and a.plan_id == plan_id)]
            changed = len(kept) != len(self._items)
            self._items = kept
            if changed:
                self._save()
            return changed

    def list(self, plan_id: str) -> List[Annotation]:
        with self._lock:
            return [a for a in self._items if a.plan_id == plan_id]

    def match(self, question: str, plan_id: str, roles: Tuple[str, ...], state: str) -> Optional[Annotation]:
        best, best_score = None, 0.0
        with self._lock:
            for a in self._items:
                if a.plan_id != plan_id or a.roles != tuple(sorted(roles)) or a.state != state:
                    continue
                score = similarity(question, a.question)
                if score >= MATCH_THRESHOLD and score > best_score:
                    best, best_score = a, score
        return best
