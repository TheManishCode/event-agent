"""Deals agent: search the web, read pages safely, pull out advertised prices in code, rank the best of the best (ADR-031).

An agent with tools, not a chatbot: the plan is fixed code (queries from templates, a page budget, a deadline), page text is only ever
parsed by regular expressions, and an offer exists only if the price was found in the page text. Nothing here asks an LLM for a fact.
Results are leads, labelled as advertised prices; they never set a plan price (ADR-025: public listings do not price).
"""

from __future__ import annotations

import json
import re
import statistics
import time
import unicodedata
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from eventmate.integrations.discovery import clean_city
from eventmate.schemas import MAX_BUDGET_INR, DealOffer, DealQuery, DealReport, MarketSummary, SkippedPage
from eventmate.integrations.web_access import FetchError, _clean
from eventmate.integrations.websearch import SearchError, SearchProvider

RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "deal_rules.json"
MAX_MENTIONS_PER_PAGE = 12
MAX_OFFERS_PER_DOMAIN = 3
UNUSUALLY_LOW_PCT = -50
WIDE_RANGE_FACTOR = 5
CORROBORATION_BAND = 0.25
OnCall = Callable[[str, Dict[str, object], bool, str, float], None]

_CUR = r"(?<![A-Za-z])(?:₹|Rs\.?|INR)"
_NUM = r"\d[\d,]*(?:\.\d+)?"
_SUF = r"(?:\s?(?:lakhs?|lacs?|crores?|cr|k)\b)?"
_PRICE = re.compile(
    rf"{_CUR}\s?(?P<n1>{_NUM})(?:\s?(?P<s1>lakhs?|lacs?|crores?|cr|k)\b)?"
    rf"(?:\s?(?:-|–|—|\bto\b)\s?(?:{_CUR})?\s?(?P<n2>{_NUM})(?:\s?(?P<s2>lakhs?|lacs?|crores?|cr|k)\b)?)?\+?",
    re.I,
)
_UNIT = re.compile(r"^[\s+]*(?:per|/)\s?(plate|head|person|guest|pax|day|night|hour|hr|event|session)s?\b", re.I)
_UNIT_NAMES = {"plate": "per_plate", "head": "per_person", "person": "per_person", "guest": "per_person", "pax": "per_person",
               "day": "per_day", "night": "per_night", "hour": "per_hour", "hr": "per_hour", "event": "per_event", "session": "per_event"}
_MULT = {"k": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000, "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000}
_LEADING_UNIT = re.compile(r"^[\s+.,;:|-]*(?:(?:per|/)\s?(?:plate|head|person|guest|pax|day|night|hour|hr|event|session)s?\b[\s.,;:|-]*)+", re.I)
_TRAILING_FILLER = re.compile(r"(?:\b(?:starting|starts|start|from|only|at|is|just|for|rs|inr|approx|around|about)\b[\s:,-]*)+$", re.I)
_SENTENCE_BREAK = re.compile(r"[.!?|•\n]\s")


@dataclass(frozen=True)
class Mention:
    low: int
    high: int
    unit: str  # as written: per_plate, per_person, per_day, per_hour, per_event, or unspecified
    label: str
    snippet: str


def _value(num: str, suffix: Optional[str]) -> Optional[int]:
    try:
        v = float(num.replace(",", "")) * (_MULT[suffix.lower()] if suffix else 1)
    except ValueError:
        return None
    return round(v) if 0 < v <= MAX_BUDGET_INR else None


def _label(before: str) -> str:
    before = _LEADING_UNIT.sub("", before)
    parts = _SENTENCE_BREAK.split(before.strip() + " ")
    words = _TRAILING_FILLER.sub("", parts[-1].strip()).split()
    return " ".join(words[-8:]).strip(" -:(),;|")[:100]


def extract_mentions(text: str) -> List[Mention]:
    """Advertised price mentions in page text. Pure regular expressions: a mention exists only if its digits are in the text."""
    text = _clean(text)
    out: List[Mention] = []
    seen = set()
    prev_end = 0
    for m in _PRICE.finditer(text):
        low = _value(m.group("n1"), m.group("s1"))
        high = _value(m.group("n2"), m.group("s2") or m.group("s1")) if m.group("n2") else low
        start = m.start()
        before_start = max(prev_end, start - 150)
        prev_end = m.end()
        if low is None or high is None or low > high:
            continue
        u = _UNIT.match(text[m.end(): m.end() + 40])
        unit = _UNIT_NAMES[u.group(1).lower()] if u else "unspecified"
        label = _label(text[before_start:start])
        key = (low, high, unit, label)
        if key in seen:
            continue
        seen.add(key)
        out.append(Mention(low, high, unit, label, text[max(0, start - 60): m.end() + 40].strip()[:200]))
        if len(out) == MAX_MENTIONS_PER_PAGE:
            break
    return out


def load_rules(path: Path = RULES_PATH) -> dict:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema_version") != 1 or not isinstance(doc.get("kinds"), dict):
        raise ValueError("deal_rules.json: unsupported format")
    return doc


def _domain(url: str) -> str:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:  # e.g. an unterminated IPv6 literal
        return ""
    return host[4:] if host.startswith("www.") else host


def clean_hit_url(url: object) -> Optional[str]:
    """A search-result URL we are willing to even try: plain http(s), bounded, no control or bidi characters, a real host and port."""
    if not isinstance(url, str) or len(url) > 2048 or not url.startswith(("http://", "https://")):
        return None
    if any(ord(c) <= 32 or ord(c) == 127 or unicodedata.category(c) in ("Cc", "Cf") for c in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # raises ValueError for a port outside 0-65535
    except ValueError:
        return None
    host = parts.hostname or ""
    return url.split("#", 1)[0] if host and len(host) <= 253 else None


@dataclass
class _Candidate:
    url: str
    title: str
    domain: str
    unit: str
    assumed: bool
    low: int
    high: int
    label: str
    snippet: str


class _Row(NamedTuple):
    low_alone: bool
    cand: _Candidate
    corroborated: int
    vs_market_pct: int
    flags: Tuple[str, ...]
    est_low: Optional[int]
    est_high: Optional[int]


def _cap_per_domain(cands: List[_Candidate]) -> List[_Candidate]:
    """Cheapest first, at most MAX_OFFERS_PER_DOMAIN from any one site."""
    kept, per_domain = [], Counter()
    for c in sorted(cands, key=lambda c: (c.low, c.high, c.domain, c.label)):
        if per_domain[c.domain] < MAX_OFFERS_PER_DOMAIN:
            per_domain[c.domain] += 1
            kept.append(c)
    return kept


def _score(q: DealQuery, c: _Candidate, cands: List[_Candidate], med_low: int, unit: str) -> _Row:
    """Corroboration by other sites, distance from the market median, honesty flags, and the estimate scaled to the user's event."""
    corr = len({o.domain for o in cands if o.domain != c.domain and abs(o.low - c.low) <= CORROBORATION_BAND * c.low})
    pct = round((c.low - med_low) / med_low * 100) if med_low else 0
    flags = []
    low_alone = corr == 0 and pct <= UNUSUALLY_LOW_PCT
    if low_alone:
        flags.append("unusually low: no other site advertises a similar price; check what is included")
    if c.high > c.low * WIDE_RANGE_FACTOR:
        flags.append(f"wide range: the top is over {WIDE_RANGE_FACTOR} times the bottom, so packages differ a lot")
    if c.assumed:
        flags.append("the page gave no unit; the usual one for this service was assumed")
    scale = q.guests if (unit == "per_plate" and q.guests) else (q.nights if (unit == "per_night" and q.nights) else (1 if unit == "per_event" else None))
    est_lo, est_hi = (c.low * scale, c.high * scale) if scale else (None, None)
    return _Row(low_alone, c, corr, pct, tuple(flags), est_lo, est_hi)


def _offer(q: DealQuery, r: _Row, rank: int) -> DealOffer:
    c = r.cand
    return DealOffer(
        rank=rank, title=c.title[:200], label=c.label, url=c.url, domain=c.domain, unit=c.unit, unit_assumed=c.assumed, low_inr=c.low, high_inr=c.high,
        est_total_low_inr=r.est_low, est_total_high_inr=r.est_high, within_budget=(r.est_low <= q.budget_inr) if (q.budget_inr and r.est_low is not None) else None,
        vs_market_pct=r.vs_market_pct, corroborated_by=r.corroborated, flags=r.flags, snippet=c.snippet[:200], source=f"web:{c.domain}",
    )


class DealAgent:
    def __init__(
        self,
        search: SearchProvider,
        fetcher,
        on_call: Optional[OnCall] = None,
        max_queries: int = 3,
        results_per_query: int = 8,
        max_pages: int = 8,
        max_per_domain: int = 2,
        deadline_s: float = 60.0,
        workers: int = 4,
        rules: Optional[dict] = None,
    ):
        self.search, self.fetcher, self.on_call = search, fetcher, on_call
        self.max_queries, self.results_per_query, self.max_pages = max_queries, results_per_query, max_pages
        self.max_per_domain, self.deadline_s, self.workers = max_per_domain, deadline_s, workers
        self.rules = rules or load_rules()

    def _log(self, tool: str, args: Dict[str, object], ok: bool, text: str, t0: float) -> None:
        if self.on_call:
            self.on_call(tool, args, ok, text, (time.monotonic() - t0) * 1000)

    # ------------------------------------------------------------------ run

    def run(self, q: DealQuery) -> DealReport:
        city = clean_city(q.city)  # the only user text that reaches a search query
        kind = self.rules["kinds"][q.kind]
        warnings: List[str] = []
        queries = tuple(dict.fromkeys(t.format(city=city) for t in kind["queries"][: self.max_queries]))
        hits: List[Tuple[str, str]] = []
        failures = 0
        for query in queries:
            t0 = time.monotonic()
            try:
                found = self.search.search(query, self.results_per_query)
            except SearchError as exc:
                failures += 1
                warnings.append(f"a search failed: {exc}")
                self._log("web.search", {"query": query, "provider": self.search.name}, False, str(exc)[:200], t0)
                continue
            self._log("web.search", {"query": query, "provider": self.search.name}, True, f"{len(found)} result(s)", t0)
            hits += [(h.url, h.title) for h in found]
        if failures == len(queries):
            raise SearchError(warnings[-1].removeprefix("a search failed: ") if warnings else "the web search failed")

        urls, per_domain, seen = [], Counter(), set()
        for url, title in hits:
            clean = clean_hit_url(url)
            dom = _domain(clean) if clean else ""
            if not clean or clean in seen or not dom or per_domain[dom] >= self.max_per_domain:
                continue
            seen.add(clean)
            per_domain[dom] += 1
            urls.append(clean)
            if len(urls) == self.max_pages:
                break
        if not urls:
            warnings.append("no search results could be read")
            return self._report(q, (), None, 0, (), queries, warnings)

        pages, skipped = self._fetch_all(urls, warnings)
        candidates: List[_Candidate] = []
        for page in pages:
            mentions = extract_mentions(page.text)
            if not mentions:
                skipped.append(SkippedPage(url=page.url, reason="no prices found"))
                continue
            got = self._candidates(page, mentions, kind)
            if not got:
                skipped.append(SkippedPage(url=page.url, reason=f"no plausible {q.kind} prices"))
            candidates += got
        return self._rank(q, candidates, len(pages), skipped, queries, warnings)

    # --------------------------------------------------------------- fetching

    def _fetch_all(self, urls: List[str], warnings: List[str]):
        pages, skipped = {}, []

        def one(url: str) -> None:
            t0 = time.monotonic()
            try:
                page = self.fetcher.fetch(url)
                pages[url] = page
                self._log("web.fetch", {"url": url}, True, f"{len(page.text)} chars", t0)
            except FetchError as exc:
                skipped.append(SkippedPage(url=url, reason=str(exc)[:200]))
                self._log("web.fetch", {"url": url}, False, str(exc)[:200], t0)
            except Exception:
                skipped.append(SkippedPage(url=url, reason="the page could not be read"))
                self._log("web.fetch", {"url": url}, False, "the page could not be read", t0)

        pool = ThreadPoolExecutor(max_workers=max(1, self.workers))
        futures = [pool.submit(one, u) for u in urls]
        _, pending = wait(futures, timeout=self.deadline_s)
        for f in pending:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        if pending:
            warnings.append(f"stopped at the time limit ({self.deadline_s:g}s); results are partial")
        return [pages[u] for u in urls if u in pages], skipped

    # ---------------------------------------------------------------- ranking

    def _candidates(self, page, mentions: List[Mention], kind: dict) -> List[_Candidate]:
        out = []
        for m in mentions:
            assumed = m.unit == "unspecified"
            unit = kind.get("default_unit") if assumed else kind["units"].get(m.unit)
            if not unit:
                continue
            lo, hi = kind.get("bounds", {}).get(unit) or self.rules["bounds"][unit]
            if m.low < lo or m.high > hi or m.high > m.low * 50:
                continue
            if not self._relevant(kind, m, page):
                continue
            out.append(_Candidate(page.url, page.title, _domain(page.url), unit, assumed, m.low, m.high, m.label or page.title[:100], m.snippet))
        return out

    @staticmethod
    def _relevant(kind: dict, m: Mention, page) -> bool:
        words = kind.get("keywords")
        if not words:
            return True
        hay = f"{m.label} {m.snippet} {page.title}".casefold()
        return any(w in hay for w in words)

    def _rank(self, q: DealQuery, cands: List[_Candidate], pages_read: int, skipped: List[SkippedPage], queries, warnings) -> DealReport:
        if not cands:
            return self._report(q, (), None, pages_read, skipped, queries, warnings)
        unit = Counter(c.unit for c in cands).most_common(1)[0][0]
        cands = list({(c.domain, c.low, c.high): c for c in cands if c.unit == unit}.values())
        med_low = int(statistics.median(c.low for c in cands))
        med_high = int(statistics.median(c.high for c in cands))
        market = MarketSummary(unit=unit, median_low_inr=med_low, median_high_inr=med_high, n_offers=len(cands), n_domains=len({c.domain for c in cands}))
        rows = [_score(q, c, cands, med_low, unit) for c in _cap_per_domain(cands)]
        rows.sort(key=lambda r: (r.low_alone, r.cand.low, -r.corroborated, r.cand.domain, r.cand.label))  # solid offers first, cheapest first; lone low outliers last
        offers, unbuilt = [], 0
        for i, row in enumerate(rows[: q.limit], 1):
            try:
                offers.append(_offer(q, row, i))
            except ValueError:  # one odd page must not cost the user every other result
                unbuilt += 1
        if unbuilt:
            warnings.append(f"{unbuilt} offer(s) could not be built from unusual page data and were skipped")
        built = tuple(o.model_copy(update={"rank": n}) for n, o in enumerate(offers, 1))
        return self._report(q, built, market, pages_read, skipped, queries, warnings)

    def _report(self, q, offers, market, pages_read, skipped, queries, warnings) -> DealReport:
        return DealReport(
            query=q, offers=tuple(offers), market=market, pages_read=pages_read, skipped=tuple(skipped), queries_run=tuple(queries),
            provider=self.search.name[:40], warnings=tuple(warnings), fetched_at=datetime.now(timezone.utc),
        )
