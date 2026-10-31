"""Public contact addresses from a business's own pages (ADR-049).

Reads the business's website and at most one same-domain contact or about page, through the safe fetcher (SSRF checks, robots.txt, size and time caps).
It extracts only what a visitor can see: ``mailto:`` links and addresses printed in the page text. Text a visitor cannot see (scripts, styles, comments,
hidden elements) is ignored. It never decodes obfuscated addresses, never guesses ``info@<domain>``, never follows a link to another domain, and never
asks a model. Every address it returns carries the page it was read from, and none is used until the user confirms it.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

from eventmate.integrations.web_access import FetchError
from eventmate.schemas import _EMAIL

MAX_PAGES = 2
MAX_FOUND = 5
MAX_LINKS = 200
MAX_URL = 2048
FREE_MAIL = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com", "yahoo.in", "yahoo.co.in", "ymail.com",
    "rediffmail.com", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "zoho.com", "gmx.com", "mail.com",
})
FLAG_GENERIC = "generic mailbox, confirm it is the business's"
FLAG_DOMAIN = "different domain from the website"
_IMAGE_TLD = frozenset({"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "bmp", "woff", "woff2"})
_TEXT_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.[A-Za-z]{2,24}"
)
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])|opacity\s*:\s*0(?![.\d])|height\s*:\s*0(?![.\d])\s*;?[^}]*overflow\s*:\s*hidden", re.I)
_CONTACT_PAGE = (
    (0, re.compile(r"contact", re.I)),
    (1, re.compile(r"reach|enquir|inquir|get[- _]?in[- _]?touch|write[- _]?to", re.I)),
    (2, re.compile(r"about", re.I)),
)
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "object", "head", "title", "select", "textarea", "option"})


@dataclass
class ContactSearch:
    found: List[dict] = field(default_factory=list)
    pages: List[str] = field(default_factory=list)  # every address that was fetched
    notes: List[str] = field(default_factory=list)


class _Visible(HTMLParser):
    """The text and links a visitor could see. Hidden elements and everything inside them are dropped."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text: List[str] = []
        self.links: List[Tuple[str, str]] = []  # (href, anchor text)
        self._stack: List[Tuple[str, bool]] = []  # (tag, hides what is inside it)
        self._hidden = 0
        self._anchor: Optional[List] = None

    @staticmethod
    def _hides(tag: str, attrs: Dict[str, Optional[str]]) -> bool:
        if tag in _SKIP or "hidden" in attrs or (attrs.get("aria-hidden") or "").strip().lower() == "true":
            return True
        if tag == "input" and (attrs.get("type") or "").lower() == "hidden":
            return True
        return bool(_HIDDEN_STYLE.search(attrs.get("style") or ""))

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): v for k, v in attrs}
        hides = self._hides(tag, a)
        if tag not in _VOID:
            self._stack.append((tag, hides))
            self._hidden += hides
        if tag == "a" and not self._hidden and a.get("href"):
            self._anchor = [a["href"], []]
        self.text.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.text.append(" ")  # a self-closing tag has no inside: nothing to hide, nothing to open

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        for i in range(len(self._stack) - 1, -1, -1):  # tolerate unclosed or misnested markup: close down to the matching tag
            if self._stack[i][0] == tag:
                for _t, h in self._stack[i:]:
                    self._hidden -= h
                del self._stack[i:]
                break
        if tag == "a" and self._anchor is not None:
            if len(self.links) < MAX_LINKS:
                self.links.append((self._anchor[0], " ".join("".join(self._anchor[1]).split())[:120]))
            self._anchor = None
        self.text.append(" ")

    def handle_data(self, data):
        if not self._hidden:
            self.text.append(data)
            if self._anchor is not None:
                self._anchor[1].append(data)


def _parse(html: str) -> Tuple[str, List[Tuple[str, str]]]:
    p = _Visible()
    try:
        p.feed(html)
        p.close()
    except Exception:  # a parser failure on hostile markup must not take the run down
        pass
    return " ".join("".join(p.text).split()), p.links


def _valid_email(value: str) -> Optional[str]:
    value = value.strip()
    if not _EMAIL.fullmatch(value) or value.rsplit(".", 1)[-1].casefold() in _IMAGE_TLD:
        return None
    return value.casefold()


def _mailto_addresses(href: str) -> List[str]:
    if not href[:7].casefold() == "mailto:":
        return []
    decoded = urllib.parse.unquote(href[7:])
    if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
        return []  # an encoded line break is a header-injection attempt: the whole link is dropped
    out = []
    for token in decoded.split("?", 1)[0].split(","):  # only the address part: parameters (cc, bcc, subject, body) are never read
        v = _valid_email(token)
        if v:
            out.append(v)
    return out


def _host(url: str) -> str:
    h = (urllib.parse.urlsplit(url).hostname or "").casefold().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _same_business(email_domain: str, site_host: str) -> bool:
    return bool(site_host) and (email_domain == site_host or email_domain.endswith("." + site_host))


def _flags(value: str, site_host: str) -> Tuple[str, ...]:
    domain = value.rsplit("@", 1)[1]
    flags = []
    if domain in FREE_MAIL:
        flags.append(FLAG_GENERIC)
    if site_host and not _same_business(domain, site_host):  # with no website to compare against there is nothing to differ from
        flags.append(FLAG_DOMAIN)
    return tuple(flags)


def _contact_link(links: List[Tuple[str, str]], base_url: str) -> Optional[str]:
    base_host, best = _host(base_url), None
    for href, text in links:
        if len(href) > MAX_URL or href[:11].casefold() in ("javascript:",) or href[:7].casefold() == "mailto:":
            continue
        try:
            url, _ = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href))
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if parts.scheme not in ("http", "https") or _host(url) != base_host or url == base_url or len(url) > MAX_URL:
            continue  # another domain is never followed
        label = parts.path + " " + text
        for rank, rx in _CONTACT_PAGE:
            if rx.search(label):
                if best is None or rank < best[0]:
                    best = (rank, url)
                break
    return best[1] if best else None


def find_contacts(fetcher, website_url: str, max_pages: int = MAX_PAGES) -> ContactSearch:
    """Addresses a business publishes on its own website, each with the page it was read from. Nothing is sent and nothing is saved."""
    res = ContactSearch()
    if not isinstance(website_url, str) or len(website_url) > MAX_URL or urllib.parse.urlsplit(website_url).scheme not in ("http", "https"):
        res.notes.append("the website address is not a plain http(s) address, so it could not be read")
        return res
    site_host = _host(website_url)
    seen: Dict[str, dict] = {}
    overflow = False

    def take(value: str, source_url: str, kind: str) -> None:
        nonlocal overflow
        have = seen.get(value)
        if have is not None:
            if kind == "page_mailto" and have["source_kind"] != "page_mailto":
                have.update(source_kind=kind, source_url=source_url)  # a link says more than loose text
            return
        if len(seen) >= MAX_FOUND:
            overflow = True
            return
        seen[value] = {"channel": "email", "value": value, "source_url": source_url, "source_kind": kind, "flags": _flags(value, site_host)}

    next_url: Optional[str] = website_url
    for n in range(max(1, min(max_pages, MAX_PAGES))):
        if next_url is None:
            break
        try:
            raw = fetcher.fetch_html(next_url)
        except FetchError as exc:
            res.notes.append(f"{'the website' if n == 0 else 'the contact page'} could not be read: {str(exc)[:120]}")
            break
        res.pages.append(raw.url)
        text, links = _parse(raw.html)
        for href, _ in links:
            for v in _mailto_addresses(href):
                take(v, raw.url, "page_mailto")
        for m in _TEXT_EMAIL.finditer(text):
            v = _valid_email(m.group(0))
            if v:
                take(v, raw.url, "page_text")
        next_url = _contact_link(links, raw.url) if n == 0 else None
    res.found = sorted(seen.values(), key=lambda f: (f["source_kind"] != "page_mailto", f["value"]))
    if overflow:
        res.notes.append(f"more than {MAX_FOUND} addresses were found; only the first {MAX_FOUND} are shown")
    if not res.found and res.pages:
        res.notes.append(f"no public email address was found on {site_host}")
    return res
