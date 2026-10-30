"""Channels the agent talks to vendors through (ADR-042): email over SMTP/IMAP and WhatsApp through a self-hosted WAHA gateway.

A channel only moves text. It decides nothing: what is sent, to whom and when is decided by the approval flow in
`conversations.py`. Secrets come from the environment and never appear in a repr, a status or an error message.
Everything a vendor sends back is untrusted text: it is cleaned, size-capped and returned as data.

WhatsApp note: WAHA drives a WhatsApp Web session. That is not the official Business API and WhatsApp's terms do not
allow unofficial clients, so the account can be restricted. EventMate therefore sends slowly, only after approval, and
only to numbers WhatsApp reports as registered (ADR-042).
"""

from __future__ import annotations

import email
import hashlib
import email.policy
import email.utils
import http.client
import imaplib
import re
import smtplib
import ssl
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Callable, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple
from urllib.parse import quote, urlencode, urlsplit

from eventmate.netutil import NETWORK_ERRORS, loads, read_capped
from eventmate.schemas import ChannelStatus

MAX_BODY_CHARS = 4000
MAX_POLL_MESSAGES = 50
MAX_FETCH_BYTES = 262_144
TIMEOUT_S = 20
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class ChannelError(Exception):
    """The message was definitely not sent (or the channel could not be read)."""


class OutcomeUnknown(ChannelError):
    """The message may or may not have gone out. Never resend automatically."""


@dataclass(frozen=True)
class ThreadRef:
    in_reply_to: str = ""
    references: str = ""


@dataclass(frozen=True)
class KnownThread:
    draft_id: str
    recipient: str
    sent_at: datetime
    external_id: str


@dataclass(frozen=True)
class Inbound:
    channel: str
    sender: str
    external_id: str
    received_at: datetime
    body: str
    subject: str = ""
    in_reply_to: str = ""
    references: str = ""
    attachments: int = 0
    truncated: bool = False


class Channel(Protocol):
    name: str

    def status(self) -> ChannelStatus: ...

    def send(self, recipient: str, subject: str, body: str, thread: Optional[ThreadRef] = None) -> str: ...

    def poll(self, threads: Sequence[KnownThread]) -> List[Inbound]: ...


# ----------------------------------------------------------------- text hygiene

_QUOTE_START = re.compile(r"^(?:On .{5,200} wrote:|-{2,}\s*Original Message\s*-{2,}|_{5,}|From:\s.+|Sent from my .+)\s*$", re.I)


def clean_reply_text(raw: str) -> Tuple[str, bool]:
    """The vendor's new text only: quoted history and signatures' boilerplate cut, control characters removed, size capped."""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = "".join(ch for ch in raw if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    kept: List[str] = []
    for line in raw.split("\n"):
        if kept and any(k.strip() for k in kept) and _QUOTE_START.match(line.strip()):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line.rstrip())
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return (text[:MAX_BODY_CHARS], True) if len(text) > MAX_BODY_CHARS else (text, False)


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    return re.sub(r"<[^>]+>", " ", html)


# ----------------------------------------------------------------- email


@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str = ""
    smtp_port: int = 465
    imap_host: str = ""
    imap_port: int = 993
    user: str = ""
    password: str = field(default="", repr=False)
    from_addr: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "EmailConfig":
        def num(name: str, default: int) -> int:
            raw = env.get(name, "").strip()
            return int(raw) if raw.isdigit() and 0 < int(raw) < 65536 else default

        user = env.get("EVENTMATE_SMTP_USER", "").strip()
        return cls(
            smtp_host=env.get("EVENTMATE_SMTP_HOST", "").strip(), smtp_port=num("EVENTMATE_SMTP_PORT", 465),
            imap_host=env.get("EVENTMATE_IMAP_HOST", "").strip(), imap_port=num("EVENTMATE_IMAP_PORT", 993),
            user=user, password=env.get("EVENTMATE_SMTP_PASSWORD", ""), from_addr=env.get("EVENTMATE_MAIL_FROM", "").strip() or user,
        )


def _imap_date(d: datetime) -> str:
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


class EmailChannel:
    name = "email"

    def __init__(self, config: EmailConfig, smtp_factory: Optional[Callable] = None, imap_factory: Optional[Callable] = None, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.config = config
        self._smtp = smtp_factory or self._default_smtp
        self._imap = imap_factory or self._default_imap
        self._now = now

    def status(self) -> ChannelStatus:
        c = self.config
        can_send = bool(c.smtp_host and c.user and c.password and c.from_addr)
        can_receive = bool(c.imap_host and c.user and c.password)
        missing = [n for n, ok in (("EVENTMATE_SMTP_HOST/USER/PASSWORD", can_send), ("EVENTMATE_IMAP_HOST", can_receive)) if not ok]
        return ChannelStatus(channel="email", can_send=can_send, can_receive=can_receive, detail="ready" if not missing else "not set: " + ", ".join(missing))

    def _default_smtp(self):
        c = self.config
        if c.smtp_port == 465:
            return smtplib.SMTP_SSL(c.smtp_host, c.smtp_port, timeout=TIMEOUT_S, context=ssl.create_default_context())
        server = smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=TIMEOUT_S)
        server.starttls(context=ssl.create_default_context())  # never fall back to plain text
        return server

    def _default_imap(self):
        return imaplib.IMAP4_SSL(self.config.imap_host, self.config.imap_port, ssl_context=ssl.create_default_context(), timeout=TIMEOUT_S)

    def send(self, recipient: str, subject: str, body: str, thread: Optional[ThreadRef] = None) -> str:
        c = self.config
        if not self.status().can_send:
            raise ChannelError("email sending is not configured")
        msg = EmailMessage(policy=email.policy.SMTP)
        try:
            msg["From"], msg["To"], msg["Subject"] = c.from_addr, recipient, subject
            msg["Date"] = email.utils.format_datetime(self._now())
            msg["Message-ID"] = email.utils.make_msgid(domain=c.from_addr.rsplit("@", 1)[-1] or None)
            if thread and thread.in_reply_to:
                msg["In-Reply-To"] = thread.in_reply_to
                msg["References"] = (thread.references + " " + thread.in_reply_to).strip()
            msg.set_content(body)
        except (ValueError, TypeError) as exc:  # header injection attempts end here
            raise ChannelError(f"the message could not be built: {exc}") from None
        try:
            server = self._smtp()
            server.login(c.user, c.password)
        except smtplib.SMTPAuthenticationError:
            raise ChannelError("the mail server rejected the login") from None
        except (smtplib.SMTPException, OSError, ssl.SSLError):
            raise ChannelError("could not connect to the mail server") from None
        try:
            server.send_message(msg)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused):
            raise ChannelError("the mail server refused the address") from None
        except (smtplib.SMTPException, OSError, TimeoutError):
            raise OutcomeUnknown("the connection failed while sending; check your Sent folder before trying again") from None
        finally:
            try:
                server.quit()
            except Exception:  # the message is already accepted or failed; closing is best effort
                pass
        return msg["Message-ID"]

    def poll(self, threads: Sequence[KnownThread]) -> List[Inbound]:
        if not threads:
            return []
        if not self.status().can_receive:
            raise ChannelError("email reading is not configured")
        since = min(t.sent_at for t in threads) - timedelta(days=1)
        out: List[Inbound] = []
        try:
            box = self._imap()
            try:
                box.login(self.config.user, self.config.password)
                box.select("INBOX", readonly=True)
                typ, data = box.search(None, "SINCE", _imap_date(since))
                ids = (data[0] or b"").split()[-MAX_POLL_MESSAGES:] if typ == "OK" and data else []
                for num in ids:
                    typ, parts = box.fetch(num, f"(BODY.PEEK[]<0.{MAX_FETCH_BYTES}>)")
                    raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None) if typ == "OK" else None
                    item = self._parse(raw) if raw else None
                    if item:
                        out.append(item)
            finally:
                try:
                    box.logout()
                except Exception:
                    pass
        except imaplib.IMAP4.error:
            raise ChannelError("the mail server rejected the login or the request") from None
        except (OSError, ssl.SSLError):
            raise ChannelError("could not reach the mail server") from None
        return out

    def _parse(self, raw: bytes) -> Optional[Inbound]:
        try:
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            sender = email.utils.parseaddr(str(msg.get("From", "")))[1].lower()
            if not sender or sender == self.config.from_addr.lower():
                return None
            if str(msg.get("Auto-Submitted", "no")).lower() != "no" or str(msg.get("Precedence", "")).lower() in ("bulk", "junk", "list") or msg.get("List-Id"):
                return None  # robots and mailing lists are not vendor replies
            part = msg.get_body(preferencelist=("plain",))
            text = part.get_content() if part is not None else ""
            if not text:
                html = msg.get_body(preferencelist=("html",))
                text = _strip_html(html.get_content()) if html is not None else ""
            body, truncated = clean_reply_text(str(text))
            when = email.utils.parsedate_to_datetime(str(msg["Date"])) if msg["Date"] else self._now()
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            mid = str(msg.get("Message-ID", "")).strip()
            attachments = sum(1 for _ in msg.iter_attachments())
            return Inbound(
                channel="email", sender=sender[:254], external_id=(mid or "nomsgid-" + hashlib.sha256(raw).hexdigest()[:32])[:400], received_at=when.astimezone(timezone.utc), body=body,
                subject=str(msg.get("Subject", ""))[:200], in_reply_to=str(msg.get("In-Reply-To", "")).strip()[:400], references=str(msg.get("References", ""))[:2000],
                attachments=min(attachments, 100), truncated=truncated,
            )
        except (ValueError, TypeError, KeyError, LookupError):
            return None  # one malformed message must not stop the others


# ----------------------------------------------------------------- WhatsApp through WAHA


@dataclass(frozen=True)
class WahaConfig:
    url: str = ""
    api_key: str = field(default="", repr=False)
    session: str = "default"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "WahaConfig":
        session = env.get("EVENTMATE_WAHA_SESSION", "").strip()
        return cls(url=env.get("EVENTMATE_WAHA_URL", "").strip().rstrip("/"), api_key=env.get("EVENTMATE_WAHA_API_KEY", ""), session=session if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", session) else "default")


HttpFn = Callable[[str, str, Dict[str, str], Optional[bytes]], Tuple[int, bytes]]


def _http(method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, bytes]:
    parts = urlsplit(url)
    conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port, timeout=TIMEOUT_S)  # no redirects are followed
    try:
        conn.request(method, parts.path + (f"?{parts.query}" if parts.query else ""), body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, read_capped(resp, 1_000_000, total_s=30)
    finally:
        conn.close()


class WhatsAppChannel:
    name = "whatsapp"

    def __init__(self, config: WahaConfig, http_fn: HttpFn = _http, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.config = config
        self._http = http_fn
        self._now = now
        self._chat_ids: Dict[str, str] = {}

    def _valid_url(self) -> bool:
        p = urlsplit(self.config.url)
        return p.scheme in ("http", "https") and bool(p.hostname) and not p.username and not p.password

    def status(self) -> ChannelStatus:
        ok = self._valid_url()
        return ChannelStatus(channel="whatsapp", can_send=ok, can_receive=ok, detail="ready" if ok else "not set: EVENTMATE_WAHA_URL (a self-hosted WAHA gateway; see docs)")

    def _call(self, method: str, path: str, query: Optional[Dict[str, str]] = None, payload: Optional[dict] = None):
        if not self._valid_url():
            raise ChannelError("WhatsApp gateway is not configured")
        import json

        url = self.config.url + path + (("?" + urlencode(query)) if query else "")
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["X-Api-Key"] = self.config.api_key
        body = None
        if payload is not None:
            body, headers["Content-Type"] = json.dumps(payload).encode("utf-8"), "application/json"
        try:
            status, raw = self._http(method, url, headers, body)
        except NETWORK_ERRORS:
            raise OutcomeUnknown("could not reach the WhatsApp gateway") from None
        if status in (401, 403):
            raise ChannelError("the WhatsApp gateway rejected the API key")
        if status >= 400:
            raise ChannelError(f"the WhatsApp gateway answered HTTP {status}")
        try:
            return loads(raw) if raw else None
        except ValueError:
            raise ChannelError("the WhatsApp gateway sent something that is not JSON") from None

    def _chat_id(self, digits: str) -> str:
        if digits not in self._chat_ids:
            res = self._call("GET", "/api/checkNumberStatus", {"session": self.config.session, "phone": digits})
            if not isinstance(res, dict) or res.get("numberExists") is not True:
                raise ChannelError("that number is not on WhatsApp")
            chat = res.get("chatId")
            self._chat_ids[digits] = chat if isinstance(chat, str) and re.fullmatch(r"[0-9A-Za-z.:_-]{3,60}@(?:c\.us|lid|s\.whatsapp\.net)", chat) else f"{digits}@c.us"
        return self._chat_ids[digits]

    def send(self, recipient: str, subject: str, body: str, thread: Optional[ThreadRef] = None) -> str:
        chat = self._chat_id(recipient)  # refuses numbers that are not on WhatsApp before anything is sent
        res = self._call("POST", "/api/sendText", payload={"session": self.config.session, "chatId": chat, "text": body, "linkPreview": False})
        mid = res.get("id") if isinstance(res, dict) else None
        if isinstance(mid, dict):
            mid = mid.get("_serialized")
        if not isinstance(mid, str) or not mid:
            raise OutcomeUnknown("the gateway accepted the request but gave no message id")
        return mid[:400]

    def poll(self, threads: Sequence[KnownThread]) -> List[Inbound]:
        out: List[Inbound] = []
        for t in threads:
            chat = self._chat_id(t.recipient)
            res = self._call("GET", f"/api/{quote(self.config.session, safe='')}/chats/{quote(chat, safe='@')}/messages", {"limit": "20", "downloadMedia": "false"})
            if not isinstance(res, list):
                raise ChannelError("the WhatsApp gateway sent an unexpected message list")
            for m in res[:MAX_POLL_MESSAGES]:
                if not isinstance(m, dict) or m.get("fromMe") is not False:
                    continue
                ts, mid, text = m.get("timestamp"), m.get("id"), m.get("body")
                if not isinstance(ts, (int, float)) or isinstance(ts, bool) or not isinstance(mid, str) or not isinstance(text, str):
                    continue
                when = datetime.fromtimestamp(min(ts, 4e9), tz=timezone.utc)
                if when < t.sent_at - timedelta(seconds=60):
                    continue
                body, truncated = clean_reply_text(text)
                out.append(Inbound(channel="whatsapp", sender=t.recipient, external_id=mid[:400], received_at=when, body=body, attachments=1 if m.get("hasMedia") else 0, truncated=truncated))
        return out


def channels_from_env(env: Mapping[str, str]) -> Dict[str, "Channel"]:
    return {"email": EmailChannel(EmailConfig.from_env(env)), "whatsapp": WhatsAppChannel(WahaConfig.from_env(env))}
