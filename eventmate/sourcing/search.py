"""Find businesses for a plan and category and put them on its shortlist (ADR-049).

Sources are explicit about what they cover. OpenStreetMap is queried for venues only: there is no verified OSM tag mapping here for caterers or
photographers, so asking for them says "not supported by this source" instead of returning an empty list that looks like "none exist". Google Places
covers every category but needs an API key. A found business becomes an ``ExternalBusiness``; it is never written to the vendor database.
"""

from __future__ import annotations

import re
from typing import List

from eventmate.integrations.discovery import OverpassSource
from eventmate.integrations.places import PlacesSource
from eventmate.schemas import PlanVersion, SearchOutcome, VendorCategory
from eventmate.sourcing.contacts import FLAG_GENERIC, FREE_MAIL
from eventmate.sourcing.externals import ExternalStore
from eventmate.sourcing.shortlist import ShortlistStore

SOURCES = ("osm", "places")
OSM_KINDS = {VendorCategory.VENUE: "venue"}
_TAGS = re.compile(r"<[^>]*>")


class SourceNotSupported(ValueError):
    """The chosen source does not cover this category. Not an empty result: a statement that this source cannot answer."""


def clean_name(text: str) -> str:
    text = _TAGS.sub(" ", text or "")
    return " ".join("".join(c for c in text if c.isprintable() or c == " ").split())[:120]


class VendorSearch:
    def __init__(self, externals: ExternalStore, shortlist: ShortlistStore, discovery: OverpassSource, places: PlacesSource):
        self.externals, self.shortlist, self.discovery, self.places = externals, shortlist, discovery, places

    def default_source(self, category: VendorCategory) -> str:
        return "osm" if category in OSM_KINDS else "places"

    def search(self, plan: PlanVersion, category: VendorCategory, source: str = None, limit: int = 10) -> SearchOutcome:
        source = source or self.default_source(category)
        if source not in SOURCES:
            raise ValueError(f"source must be one of {', '.join(SOURCES)}")
        if not 1 <= limit <= 20:
            raise ValueError("limit must be 1-20")
        city = plan.request.city
        if source == "osm":
            if category not in OSM_KINDS:
                raise SourceNotSupported(
                    f"{category.value} is not supported by OpenStreetMap search here (it is queried for venues only; there is no verified tag mapping for {category.value}s). "
                    "Use Google Places for this category."
                )
            return self._osm(plan, category, city, limit)
        return self._places(plan, category, city, limit)

    def _osm(self, plan: PlanVersion, category: VendorCategory, city: str, limit: int) -> SearchOutcome:
        result = self.discovery.venues(city, OSM_KINDS[category], limit)
        added = 0
        for c in result.candidates:
            name = clean_name(c.name)
            if not name:
                continue
            url = f"https://www.openstreetmap.org/{c.osm_ref}"
            before = self.externals.get(self._vid(f"osm:{c.osm_ref}"))
            biz = self.externals.add(f"osm:{c.osm_ref}", name, city, category, "osm", source_url=url, website=c.website)
            self.shortlist.add_entry(plan.plan_id, category, biz.vendor_id)
            if c.email:  # the listing's own field: a candidate with its source, never a contact until the user confirms it
                value = c.email.strip().casefold()
                flags = (FLAG_GENERIC,) if value.rsplit("@", 1)[-1] in FREE_MAIL else ()
                self.shortlist.add_candidates(biz.vendor_id, [{"channel": "email", "value": value, "source_url": url, "source_kind": "listing_field", "flags": flags}])
            added += before is None
        return SearchOutcome(source="osm", category=category, city=city, added=added, total=len(self.shortlist.entries(plan.plan_id, category)), warnings=tuple(result.warnings))

    def _places(self, plan: PlanVersion, category: VendorCategory, city: str, limit: int) -> SearchOutcome:
        result = self.places.search(category.value, city, limit=limit, websites=True)  # websites are the higher-priced field: asked for here because this is the contact step
        added, skipped = 0, 0
        warnings: List[str] = list(result.warnings)
        for p in result.places:
            if not p.place_id:
                skipped += 1
                continue
            name = clean_name(p.name)
            if not name:
                continue
            ref = f"gplaces:{p.place_id}"
            before = self.externals.get(self._vid(ref))
            biz = self.externals.add(ref, name, city, category, "gplaces", source_url=p.maps_uri, website=p.website, rating=p.rating)
            self.shortlist.add_entry(plan.plan_id, category, biz.vendor_id)
            added += before is None
        if skipped:
            warnings.append(f"{skipped} place(s) without a place id were skipped")
        return SearchOutcome(source="places", category=category, city=city, added=added, total=len(self.shortlist.entries(plan.plan_id, category)), warnings=tuple(warnings))

    @staticmethod
    def _vid(external_id: str) -> str:
        from eventmate.schemas import external_vendor_id

        return external_vendor_id(external_id)
