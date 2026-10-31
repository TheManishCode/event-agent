"""Businesses found by discovery, kept apart from the vendor database (ADR-028, ADR-049).

An external business has its own identity (``ext_...``). It is never merged with, renamed to, or bound to a seed vendor, and it never appears in the
vendor repository. Where it came from (discovered, or a test vendor whose address the user owns) travels with it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from eventmate.jsonl import InterProcessLock, JsonlLog
from eventmate.schemas import ExternalBusiness, VendorCategory, VendorRef, external_vendor_id, _EMAIL


@dataclass(frozen=True)
class ResolvedVendor:
    ref: VendorRef
    name: str
    category: VendorCategory
    origin: str  # seed | discovered | test
    source_url: Optional[str] = None


class _ExternalFile(JsonlLog[ExternalBusiness]):
    channel = "external"
    model = ExternalBusiness


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:30] or "vendor"


class ExternalStore:
    def __init__(self, root: Path, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.root = Path(root) / "externals"
        self.now = now
        self._lock = InterProcessLock(self.root / ".lock")

    def _file(self) -> _ExternalFile:
        return _ExternalFile(self.root, run_id="all")

    def all(self) -> List[ExternalBusiness]:
        latest: Dict[str, ExternalBusiness] = {}
        for b in self._file().read():
            latest[b.vendor_id] = b
        return list(latest.values())

    def get(self, vendor_id: str) -> Optional[ExternalBusiness]:
        return next((b for b in self.all() if b.vendor_id == vendor_id), None)

    def add(self, external_id: str, name: str, city: str, category: VendorCategory, source: str, source_url: Optional[str] = None,
            website: Optional[str] = None, rating: Optional[float] = None, owner_confirmed_by: Optional[str] = None) -> ExternalBusiness:
        """Record a business once. The same source reference is the same business; its first record stays."""
        vid = external_vendor_id(external_id)
        with self._lock():
            have = self.get(vid)
            if have is not None:
                return have
            biz = ExternalBusiness(
                vendor_id=vid, external_id=external_id, name=name, city=city, category=category, source=source, source_url=source_url, website=website,
                rating=rating, is_test=source == "test", origin="test" if source == "test" else "discovered", owner_confirmed_by=owner_confirmed_by, created_at=self.now(),
            )
            return self._file()._append(biz, vid)

    def set_website(self, vendor_id: str, url: str) -> ExternalBusiness:
        """A website the user typed for a business whose listing had none. It is marked as the user's, not the listing's."""
        with self._lock():
            cur = self.get(vendor_id)
            if cur is None:
                raise ValueError(f"unknown business {vendor_id!r}")
            new = ExternalBusiness.model_validate({**cur.model_dump(), "website": url, "website_source": "user_input"})
            return self._file()._append(new, vendor_id)


def clean_email(value: str) -> str:
    value = (value or "").strip()
    if not _EMAIL.fullmatch(value):
        raise ValueError("not a valid email address (one address, no names or extra parameters)")
    return value
