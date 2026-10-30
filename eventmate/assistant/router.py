"""Model routing: answer routine questions with no model, send simple ones to a small model, hard ones to a large one.

The router is plain code so it is itself testable and cannot be talked into spending more. "direct" answers are built from
tool output with templates: zero tokens, and grounded by construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_DIRECT = [
    (re.compile(r"\b(?:what|which)\s+(?:is\s+)?(?:the\s+)?(?:date|city)\b|\bwhen\s+is\b|\bwhere\s+is\s+(?:it|the\s+event)\b", re.I), "get_public_event_info"),
    (re.compile(r"\b(?:is|are)\b[^?]{0,30}\b(?:budget|it)\b[^?]{0,20}\b(?:realistic|enough|ok|okay|feasible)\b|\bafford\b", re.I), "check_budget_reality"),
    (re.compile(r"\b(?:how\s+much|what)\b[^?]{0,40}\b(?:spent|paid|left|remaining|headroom)\b|\bbudget\s+(?:status|left|remaining)\b", re.I), "get_budget_status"),
    (re.compile(r"\b(?:how\s+does\s+eventmate|does\s+eventmate|contingency|what\s+is\s+the\s+policy)\b", re.I), "search_knowledge"),
]
_HARD = re.compile(r"\b(?:compare|trade-?offs?|why|should|replan|instead|recommend|best|confirm|record|log|add|cancel|move|change|swap|reduce|increase)\b", re.I)
LONG_QUESTION = 240


@dataclass(frozen=True)
class Route:
    kind: str  # direct | small | large
    tool: Optional[str] = None
    reason: str = ""


def route(question: str) -> Route:
    q = question.strip()
    if len(q) <= LONG_QUESTION and not _HARD.search(q):
        for rx, tool in _DIRECT:
            if rx.search(q):
                return Route("direct", tool, f"routine question matched {tool}")
    if len(q) > LONG_QUESTION or _HARD.search(q):
        return Route("large", None, "needs reasoning or may propose a change")
    return Route("small", None, "short factual question")
