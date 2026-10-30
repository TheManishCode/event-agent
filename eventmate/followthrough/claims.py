"""Turn a vendor reply into price claims the user can confirm. Nothing here records a quote (ADR-025 still holds).

Rules: a claim needs exactly one clear amount whose digits really appear in the vendor's text, and a unit we can map to a basis.
Ranges, unclear units and amounts that are not in the text become a question for the user, never a claim.
A bare "66" never vouches for 66,000: a multiplier counts only when the vendor wrote it.
"""

from __future__ import annotations

import re
import uuid
from typing import List, Optional, Tuple

from eventmate.followthrough.jobs import QuoteClaim
from eventmate.schemas import InboundMessage

_TOTAL_WORDS = re.compile(r"\b(?:in total|total|overall|flat|for the (?:whole |entire |full )?event|all[- ]inclusive)\b", re.I)
_PER_WORDS = re.compile(r"\b(?:per|a|each|/)\s?(?:plate|head|person|guest|pax)\b", re.I)
_WRITTEN = re.compile(r"(?<![\d.,])(\d[\d,]*(?:\.\d+)?)(?:\s?(lakhs?|lacs?|crores?|cr|k)\b)?", re.I)
_MULT = {"k": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000}
_VALID_FOR = re.compile(r"\b(?:valid|validity|holds?|good|open)\s+(?:for|up to|upto|of)\s+(?:the next\s+)?(?:(\d{1,3})|(a|one|two|three|four))\s*(day|week)s?\b", re.I)
_SMALL = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}
_BASIS = {"per_plate": "per_plate", "per_person": "per_plate", "per_event": "total"}  # "unspecified" is deliberately absent


def amount_is_in_text(amount: int, text: str) -> bool:
    """The amount must be written out as such: 66000, 66,000, or with its own suffix (66k, 1.5 lakh). A number that only looks like it
    after a multiplier the vendor did not write (a 66 in "66 guests") does not count."""
    for m in _WRITTEN.finditer(text):
        try:
            value = float(m.group(1).replace(",", "")) * (_MULT[m.group(2).lower()] if m.group(2) else 1)
        except ValueError:
            continue
        if abs(value - amount) < 0.5:
            return True
    return False


def stated_validity_days(text: str) -> Optional[int]:
    """How long the vendor said the price holds, in days, only if they wrote it ("valid for 10 days", "valid for two weeks"). Otherwise None: unknown."""
    m = _VALID_FOR.search(text)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1) else _SMALL[m.group(2).lower()]
    days = n * (7 if m.group(3).lower() == "week" else 1)
    return days if 1 <= days <= 365 else None


def extract_claims(msg: InboundMessage) -> Tuple[List[QuoteClaim], Optional[str]]:
    """Returns (claims, problem). ``problem`` is a plain sentence for the user when the price is not clear enough to propose."""
    amounts = msg.analysis.amounts
    if not amounts:
        return [], None
    if len(amounts) > 1 and len({(a.low_inr, a.high_inr) for a in amounts}) > 1:
        return [], "The reply mentions several different amounts. Please read it and say which one is the quote."
    a = amounts[0]
    if a.low_inr != a.high_inr:
        return [], f"The reply gives a range (INR {a.low_inr:,} to {a.high_inr:,}), not a price."
    basis = _BASIS.get(a.unit)
    if basis is None and a.unit == "unspecified" and _TOTAL_WORDS.search(a.snippet) and not _PER_WORDS.search(a.snippet):
        basis = "total"  # the vendor wrote "total" or "for the event" next to the amount; the parser only knows explicit "per ..." units
    if basis is None:
        return [], f"The reply says INR {a.low_inr:,} but not what it covers (per plate, or the whole event?)."
    if not amount_is_in_text(a.low_inr, msg.body):
        return [], "A price was detected but its digits are not clearly in the text. Please read the reply."
    claim = QuoteClaim(claim_id=uuid.uuid4().hex[:16], message_id=msg.message_id, vendor_id=msg.vendor_id, snippet=a.snippet[:200], amount_inr=a.low_inr, basis=basis, stated_valid_days=stated_validity_days(msg.body))
    return [claim], None
