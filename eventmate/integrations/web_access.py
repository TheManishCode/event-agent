"""Safe, read-only fetching of public web pages (ADR-031).

The open web is hostile input. Every URL is checked before and after each redirect: http(s) only, default ports, no credentials, and
every address the host resolves to must be public (no loopback, private, link-local or metadata ranges). The connection is pinned to
the address that was checked, so a second DNS answer cannot redirect it. robots.txt is obeyed, size and time are capped, no cookies are
sent, only identity encoding is accepted, and page code is never run: scripts, styles and comments are dropped and only text is kept.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import unicodedata
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Dict, List, Tuple

from eventmate.netutil import read_capped

USER_AGENT = "EventMateBot/0.1 (+read-only price research)"
ROBOTS_TOKEN = "EventMateBot"
MAX_URL = 2048
MAX_HOST = 253
TOTAL_S = 25.0
MAX_BYTES = 1_500_000
MAX_TEXT_CHARS = 200_000
MAX_REDIRECTS = 3
TIMEOUT_S = 15
REDIRECTS = (301, 302, 303, 307, 308)
TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
_LABEL = re.compile(r"[a-z0-9-]{1,63}")


class UnsafeURL(ValueError):
    pass


class FetchError(Exception):
    """A page could not be read. The message is safe to show: no addresses or internals."""


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    text: str


def default_resolver(host: str) -> List[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return sorted({i[4][0] for i in infos})


def _public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_url(url: str, resolver: Callable[[str], List[str]] = default_resolver) -> Tuple[str, str, List[str]]:
    """Return (normalised url, host, public addresses) or raise UnsafeURL."""
    if not isinstance(url, str) or len(url) > MAX_URL or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise UnsafeURL("the address is malformed")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURL("only http and https addresses are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("addresses with credentials are not allowed")
    try:
        port = parts.port
    except ValueError:
        raise UnsafeURL("the port is invalid") from None
    if port not in (None, 80 if parts.scheme == "http" else 443):
        raise UnsafeURL("only the default port is allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURL("the address has no host")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not _public(host):
            raise UnsafeURL("that address is not public")
        ips = [host]
    else:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".localdomain")):
            raise UnsafeURL("that host is not public")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise UnsafeURL("the host name is invalid") from None
        labels = host.split(".")
        if len(host) > MAX_HOST or len(labels) < 2 or not all(_LABEL.fullmatch(l) for l in labels) or not re.search(r"[a-z]", labels[-1]):
            raise UnsafeURL("the host name is invalid")
        ips = resolver(host)
        if not ips or not all(_public(i) for i in ips):
            raise UnsafeURL("that host does not resolve to a public address")
    path = parts.path or "/"
    norm = f"{parts.scheme}://{host if literal is None else '[' + host + ']' if ':' in host else host}{path}" + (f"?{parts.query}" if parts.query else "")
    return norm, host, ips


class _Pinned(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


class _PinnedTLS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # certificate is checked against the real host name


def pinned_get(url: str, ip: str, headers: Dict[str, str], timeout: float, max_bytes: int, total_s: float = TOTAL_S) -> Tuple[int, Dict[str, str], bytes]:
    """GET ``url`` over a connection to ``ip`` (already checked). Reads at most ``max_bytes + 1`` bytes within ``total_s`` seconds in all
    (a server that dribbles bytes cannot hold the worker open). Does not follow redirects."""
    parts = urllib.parse.urlsplit(url)
    cls = _PinnedTLS if parts.scheme == "https" else _Pinned
    conn = cls(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), ip, timeout)
    try:
        conn.request("GET", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""), headers=headers)
        resp = conn.getresponse()
        body = read_capped(resp, max_bytes + 1, total_s)
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "object", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.title: List[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1
        self.parts.append(" ")

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self.parts.append(data)


def _clean(text: str) -> str:
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf") or c in "\t\n\r ")
    return " ".join(text.split())


def extract_text(html: str) -> Tuple[str, str]:
    p = _Text()
    try:
        p.feed(html)
        p.close()
    except Exception:  # a parser failure on hostile markup must not take the run down
        pass
    return _clean("".join(p.title))[:200], _clean("".join(p.parts))[:MAX_TEXT_CHARS]


class SafeFetcher:
    def __init__(
        self,
        transport=pinned_get,
        resolver: Callable[[str], List[str]] = default_resolver,
        max_bytes: int = MAX_BYTES,
        timeout: float = TIMEOUT_S,
        max_redirects: int = MAX_REDIRECTS,
    ):
        self.transport, self.resolver, self.max_bytes, self.timeout, self.max_redirects = transport, resolver, max_bytes, timeout, max_redirects
        self._robots: Dict[str, object] = {}

    # ------------------------------------------------------------- internals

    def _headers(self) -> Dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9", "Accept-Encoding": "identity", "Accept-Language": "en-IN,en;q=0.8"}

    def _check(self, url: str) -> Tuple[str, str, List[str]]:
        try:
            return check_url(url, self.resolver)
        except UnsafeURL as exc:
            raise FetchError(f"unsafe address refused: {exc}") from None

    def _get(self, url: str, check_robots: bool, max_bytes: int) -> Tuple[str, int, Dict[str, str], bytes]:
        """Follow redirects (re-checking each hop). Returns (final url, status, headers, body)."""
        for _ in range(self.max_redirects + 1):
            url, _host, ips = self._check(url)
            if check_robots and not self._allowed(url):
                raise FetchError("blocked by robots.txt")
            try:
                status, headers, body = self.transport(url, ips[0], self._headers(), self.timeout, max_bytes)
            except FetchError:
                raise
            except Exception:
                raise FetchError("the page could not be fetched") from None
            if status in REDIRECTS and headers.get("location"):
                url = urllib.parse.urljoin(url, headers["location"])
                continue
            return url, status, headers, body
        raise FetchError("too many redirects")

    def _allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            verdict: object
            try:
                _, status, _, body = self._get(origin + "/robots.txt", False, 500_000)
            except FetchError:
                status, body = 599, b""
            if status == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(body[:500_000].decode("utf-8", "replace").splitlines())
                verdict = rp
            elif 400 <= status < 500 and status != 429:
                verdict = True  # no robots file: everything is allowed
            else:
                verdict = False  # the file exists but cannot be read: be conservative
            self._robots[origin] = verdict
        verdict = self._robots[origin]
        return verdict if isinstance(verdict, bool) else verdict.can_fetch(ROBOTS_TOKEN, url)

    # ---------------------------------------------------------------- public

    def fetch(self, url: str) -> Page:
        final, status, headers, body = self._get(url, True, self.max_bytes)
        if status != 200:
            raise FetchError(f"the page answered HTTP {status}")
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in TEXT_TYPES:
            raise FetchError("not a web page (unsupported content type)")
        if headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            raise FetchError("unsupported content encoding")
        if len(body) > self.max_bytes:
            raise FetchError("the page is too large")
        m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""), re.I)
        try:
            html = body.decode(m.group(1) if m else "utf-8", "replace")
        except LookupError:
            html = body.decode("utf-8", "replace")
        title, text = extract_text(html)
        return Page(url=final, title=title, text=text)
