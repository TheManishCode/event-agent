"""Rule packs: versioned, reviewable checklists keyed by dietary requirement (F7 v1, ADR-021).

Advisory only. A pack item is a question to put to a vendor or host; it never changes a plan,
passes or fails a constraint, or claims a fact about a tradition. Every item carries its pack
version, source and review state so a reader can see how far to trust it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

from pydantic import Field, ValidationError, field_validator, model_validator

from eventmate.schemas import DietaryRequirement, EventRequest, _Model

PACK_FILE = Path(__file__).resolve().parent / "data" / "rule_packs.json"
MAX_PACK_BYTES = 200_000


class RulePackError(Exception):
    """The pack file is missing, oversized, malformed or inconsistent; nothing is loaded."""


def _plain(v: str) -> str:
    if "<" in v or ">" in v or "\n" in v:
        raise ValueError("text must be plain: no markup or line breaks")
    return v


class PackItem(_Model):
    title: str = Field(min_length=1, max_length=120)
    detail: str = Field(min_length=1, max_length=400)

    _check = field_validator("title", "detail")(_plain)


class RulePack(_Model):
    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    diet: DietaryRequirement
    version: int = Field(ge=1)
    source: str = Field(min_length=1, max_length=200)
    reviewed: bool
    reviewed_by: Optional[str] = Field(default=None, max_length=120)
    items: Tuple[PackItem, ...] = Field(min_length=1, max_length=20)

    _check = field_validator("source", "reviewed_by")(lambda v: v if v is None else _plain(v))

    @model_validator(mode="after")
    def _review_needs_a_reviewer(self) -> "RulePack":
        if self.reviewed and not self.reviewed_by:
            raise ValueError("a pack marked reviewed must name who reviewed it")
        return self


class Advisory(_Model):
    pack_id: str
    pack_version: int
    diet: DietaryRequirement
    title: str
    detail: str
    source: str
    reviewed: bool
    advisory: bool = True  # always true: these never act as constraints


def load_packs(path: Optional[Path] = None) -> List[RulePack]:
    path = Path(path) if path else PACK_FILE
    try:
        if path.stat().st_size > MAX_PACK_BYTES:
            raise RulePackError(f"{path.name} exceeds {MAX_PACK_BYTES} bytes")
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("schema_version") != 1 or set(doc) != {"schema_version", "packs"}:
            raise RulePackError(f"{path.name}: expected exactly schema_version 1 and packs")
        packs = [RulePack.model_validate(p) for p in doc["packs"]]
    except (OSError, ValueError, ValidationError) as exc:  # json errors are ValueError
        raise RulePackError(f"{path.name}: {exc}") from exc
    ids = [p.id for p in packs]
    if len(set(ids)) != len(ids):
        raise RulePackError(f"{path.name}: duplicate pack ids")
    return packs


def advisories_for(request: EventRequest, packs: Optional[List[RulePack]] = None) -> List[Advisory]:
    packs = load_packs() if packs is None else packs
    wanted = set(request.dietary)
    return [
        Advisory(pack_id=p.id, pack_version=p.version, diet=p.diet, title=i.title, detail=i.detail, source=p.source, reviewed=p.reviewed)
        for p in packs
        if p.diet in wanted
        for i in p.items
    ]
