"""Contact details never reach a model (ADR-050).

Before a prompt is built, emails, phone numbers and (with the right context) PAN, Aadhaar and card numbers are replaced by numbered typed placeholders such
as ``<EMAIL_1>`` and ``<PHONE_1>``. The map lives only for one request, in memory, and is never logged or sent. A real value comes back only when text is
rendered locally for the person who typed it. Ordinary numbers (an invoice number, a budget) are left alone; the stricter ``guard.mask_pii`` still masks
when unsure, for logs.
"""

from __future__ import annotations

import re
from typing import Dict, Tuple

from eventmate.assistant.guard import _AADHAAR, _CARD, _EMAIL, _PAN, _PHONE, _luhn

_AADHAAR_CTX = re.compile(r"aadhaar|aadhar|\buid\b|आधार", re.I)
_CARD_CTX = re.compile(r"\bcards?\b|credit|debit|visa|master\s?card|rupay|\bcvv\b|expir", re.I)
_PLACEHOLDER = re.compile(r"<(EMAIL|PHONE|PAN|AADHAAR|CARD)_(\d{1,3})>")
WINDOW = 40  # characters of context checked around a number


class Placeholders:
    def __init__(self):
        self._by_value: Dict[Tuple[str, str], str] = {}
        self._by_token: Dict[str, str] = {}
        self._count: Dict[str, int] = {}

    def _token(self, kind: str, value: str) -> str:
        key = (kind, re.sub(r"[\s-]", "", value).casefold())
        tok = self._by_value.get(key)
        if tok is None:
            self._count[kind] = self._count.get(kind, 0) + 1
            tok = f"<{kind}_{self._count[kind]}>"
            self._by_value[key], self._by_token[tok] = tok, value
        return tok

    def mask(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return text
        text = _EMAIL.sub(lambda m: self._token("EMAIL", m.group(0)), text)
        text = _PAN.sub(lambda m: self._token("PAN", m.group(0)), text)

        def near(rx, m):
            return bool(rx.search(m.string[max(0, m.start() - WINDOW): m.end() + WINDOW]))

        def card(m):
            digits = re.sub(r"\D", "", m.group(0))
            return self._token("CARD", m.group(0)) if 13 <= len(digits) <= 19 and _luhn(digits) and near(_CARD_CTX, m) else m.group(0)

        text = _CARD.sub(card, text)
        text = _AADHAAR.sub(lambda m: self._token("AADHAAR", m.group(0)) if near(_AADHAAR_CTX, m) else m.group(0), text)
        return _PHONE.sub(lambda m: self._token("PHONE", m.group(0)), text)

    def restore(self, text: str) -> str:
        """Real values back into text shown locally. A placeholder this request never issued is left as it is."""
        return _PLACEHOLDER.sub(lambda m: self._by_token.get(m.group(0), m.group(0)), text) if isinstance(text, str) else text
