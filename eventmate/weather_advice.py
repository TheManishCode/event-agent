"""Weather advice: compare a forecast with the venue's setting and suggest a backup (ADR-039).

Every threshold is a constant here, every sentence is built from forecast numbers, and no model is involved. Nothing beyond the provider's
horizon is claimed, and advice never changes a plan.
"""

from __future__ import annotations

from typing import List, Optional

from eventmate.schemas import RAIN_POSSIBLE_AT, AdviceItem, WeatherDay

INDOOR_RAIN_NOTE_AT = 0.7  # indoors, rain only matters for guests arriving
HEAT_C = 35.0


def advise(day: WeatherDay, setting: Optional[str]) -> List[AdviceItem]:
    if not day.slots:
        return [AdviceItem(level="info", title="No forecast yet", detail=day.note or "There is no forecast for that day yet; check again within five days of the event.")]
    rain = day.max_rain_chance or 0.0
    pct = round(rain * 100)
    items: List[AdviceItem] = []
    if setting != "indoor" and rain >= RAIN_POSSIBLE_AT:
        where = "an outdoor venue" if setting == "outdoor" else "a venue whose setting is not known"
        items.append(AdviceItem(
            level="warn", title="Rain is possible: ask about a covered backup",
            detail=f"The forecast gives up to a {pct}% chance of rain and the plan uses {where}. Ask the venue for a covered or indoor fallback, a tent price and the cut-off for deciding.",
        ))
    elif setting == "indoor" and rain >= INDOOR_RAIN_NOTE_AT:
        items.append(AdviceItem(level="info", title="Rain likely: think about arrival", detail=f"Up to a {pct}% chance of rain. The venue is indoors, but guest arrival, parking and photos outside may need cover."))
    if setting != "indoor" and (day.temp_max or 0) >= HEAT_C:
        items.append(AdviceItem(level="warn", title="Heat: plan shade and water", detail=f"The forecast high is {day.temp_max}°C. For outdoor time, arrange shade, drinking water and a cooler start time."))
    if not items:
        items.append(AdviceItem(level="ok", title="Nothing to prepare for", detail=f"{day.temp_min}°C to {day.temp_max}°C with up to a {pct}% chance of rain. Check again closer to the day: forecasts change."))
    return items
