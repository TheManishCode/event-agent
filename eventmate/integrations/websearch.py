"""Web search providers for the deals agent (ADR-031).

Brave Search (official API, needs BRAVE_SEARCH_API_KEY) is used when a key exists. Otherwise DuckDuckGo's HTML endpoint is read: it is
unofficial, may be rate-limited or change without notice, and its terms are the user's to check. Results are untrusted data; only
http(s) links survive, and the fetcher re-checks every address.
"""

from __future__ import annotations

import html
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List

from eventmate.netutil import NETWORK_ERRORS, loads, read_capped

MAX_REPLY_BYTES = 1_500_000
TIMEOUT_S = 15
_TAGS = re.compile(r"<[^>]+>")
_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) EventMate/0.1"


class SearchError(Exception):
    """A search failed. The message is safe to show and never contains a key."""


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


def _text(raw: str, cap: int) -> str:
    return " ".join("".join(c for c in html.unescape(_TAGS.sub("", raw)) if c.isprintable() or c == " ").split())[:cap]


def _http_url(url: str) -> bool:
    return url.startswith(("http://", "https://")) and len(url) <= 2048 and not any(ord(c) <= 32 for c in url)


class SearchProvider:
    name = "none"

    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        raise NotImplementedError


def _ddg_post(query: str) -> str:
    data = urllib.parse.urlencode({"q": query, "kl": "in-en"}).encode()
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data, headers={"User-Agent": _BROWSER_UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # nosec B310 - fixed https URL
        return read_capped(resp, MAX_REPLY_BYTES).decode("utf-8", "replace")


class DuckDuckGoHtml(SearchProvider):
    name = "duckduckgo"

    def __init__(self, transport: Callable[[str], str] = _ddg_post):
        self._transport = transport

    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        try:
            page = self._transport(query)
        except NETWORK_ERRORS:  # URLError and HTTPError are OSErrors
            raise SearchError("the web search could not be reached; try again shortly") from None
        hits: List[SearchHit] = []
        for block in re.split(r'(?=<div class="result[ "])', page):
            if 'class="result' not in block[:40] or "result--ad" in block[:80]:
                continue
            a = re.search(r'<a [^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not a:
                continue
            url = html.unescape(a.group(1))
            if url.startswith("//"):
                url = "https:" + url
            if "duckduckgo.com/l/" in url:
                url = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("uddg", [""])[0]
            if not _http_url(url) or "duckduckgo.com" in urllib.parse.urlsplit(url).netloc:
                continue
            snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
            hits.append(SearchHit(title=_text(a.group(2), 200), url=url, snippet=_text(snip.group(1), 400) if snip else ""))
            if len(hits) == limit:
                break
        return hits


def _brave_get(url: str, headers: dict) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_S) as resp:  # nosec B310 - URL is built from the fixed https Brave endpoint
        return read_capped(resp, MAX_REPLY_BYTES)


class BraveSearch(SearchProvider):
    name = "brave"

    def __init__(self, api_key: str, transport: Callable[[str, dict], bytes] = _brave_get):
        self._key, self._transport = api_key, transport

    def __repr__(self) -> str:
        return "BraveSearch(key=<hidden>)"

    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": min(limit, 20), "country": "in"})
        try:
            raw = self._transport(url, {"X-Subscription-Token": self._key, "Accept": "application/json", "User-Agent": "EventMate/0.1"})
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise SearchError("Brave Search rejected the API key") from None
            if exc.code == 429:
                raise SearchError("Brave Search rate limit reached; try again later") from None
            raise SearchError(f"Brave Search failed (HTTP {exc.code})") from None
        except NETWORK_ERRORS:
            raise SearchError("Brave Search could not be reached; try again shortly") from None
        try:
            results = loads(raw)["web"]["results"]
            return [
                SearchHit(title=_text(str(r.get("title", "")), 200), url=r["url"], snippet=_text(str(r.get("description", "")), 400))
                for r in results[:limit]
                if isinstance(r, dict) and isinstance(r.get("url"), str) and _http_url(r["url"])
            ]
        except (ValueError, KeyError, TypeError):
            raise SearchError("Brave Search returned something unreadable") from None


class FallbackSearch(SearchProvider):
    """Tries each provider in order and returns the first answer. DuckDuckGo is the free provider that is always ready behind a keyed one."""

    def __init__(self, providers):
        self.providers = list(providers)
        self.last_used = None

    @property
    def name(self) -> str:
        return self.last_used or "+".join(p.name for p in self.providers)

    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        errors = []
        for p in self.providers:
            try:
                hits = p.search(query, limit)
                self.last_used = p.name
                return hits
            except SearchError as exc:
                errors.append(str(exc))
        raise SearchError(errors[0] if len(errors) == 1 else errors[0] + " (the backup search failed too: " + errors[-1] + ")")


class MergedSearch(SearchProvider):
    """Asks every provider and interleaves the answers, de-duplicated by URL: more distinct sites than either gives alone.
    One provider failing is tolerated; it raises only when all fail."""

    def __init__(self, providers):
        self.providers = list(providers)
        self.used: List[str] = []

    @property
    def name(self) -> str:
        return "+".join(self.used) if self.used else "+".join(p.name for p in self.providers)

    def search(self, query: str, limit: int = 8) -> List[SearchHit]:
        answers, errors, used = [], [], []
        for p in self.providers:
            try:
                answers.append(p.search(query, limit))
                used.append(p.name)
            except SearchError as exc:
                errors.append(str(exc))
        if not answers:
            raise SearchError(errors[0] if len(errors) == 1 else errors[0] + " (the other search failed too: " + errors[-1] + ")")
        self.used = used
        out, seen = [], set()
        for rank in range(max(len(a) for a in answers)):
            for a in answers:
                if rank < len(a) and a[rank].url not in seen:
                    seen.add(a[rank].url)
                    out.append(a[rank])
        return out[:limit]


def default_provider() -> SearchProvider:
    key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    return MergedSearch([BraveSearch(key), DuckDuckGoHtml()]) if key else DuckDuckGoHtml()
