"""A fake mail world for tests: the real EmailChannel talks to a fake SMTP server and a fake IMAP mailbox. No network, no credentials.

The vendor is a function: it reads what was sent (the outbox) and writes replies into the mailbox, with real headers, so threading and parsing are exercised.
"""

import email.utils
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import List, Optional

from eventmate.messaging.channels import EmailChannel, EmailConfig

CFG = EmailConfig(smtp_host="smtp.fake.test", imap_host="imap.fake.test", user="agent@fake.test", password="not-a-real-password", from_addr="agent@fake.test")


class FakeSMTP:
    def __init__(self, world):
        self.world = world

    def login(self, user, password):
        if self.world.login_error:
            raise self.world.login_error

    def send_message(self, msg):
        if self.world.send_error:
            raise self.world.send_error
        self.world.outbox.append(msg)

    def noop(self):
        if self.world.login_error:
            raise self.world.login_error
        return (250, b"ok")

    def quit(self):
        self.world.smtp_quit = getattr(self.world, "smtp_quit", 0) + 1


class FakeIMAP:
    def __init__(self, world):
        self.world = world

    def login(self, user, password):
        pass

    def select(self, box, readonly=False):
        assert readonly  # polling never changes the mailbox
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.world.inbox)))]

    def fetch(self, num, spec):
        assert "PEEK" in spec
        return "OK", [(b"1 (BODY[] {n}", self.world.inbox[int(num) - 1]), b")"]

    def logout(self):
        pass


class MailWorld:
    def __init__(self, clock):
        self.clock = clock
        self.outbox: List[EmailMessage] = []
        self.inbox: List[bytes] = []
        self.send_error: Optional[Exception] = None
        self.login_error: Optional[Exception] = None
        self._n = 0

    def channel(self) -> EmailChannel:
        return EmailChannel(CFG, smtp_factory=lambda: FakeSMTP(self), imap_factory=lambda: FakeIMAP(self), now=self.clock)

    def sent_to(self, address: str) -> List[EmailMessage]:
        return [m for m in self.outbox if m["To"] == address]

    def last_to(self, address: str) -> EmailMessage:
        return self.sent_to(address)[-1]

    def reply(self, address: str, body: str, *, sender: Optional[str] = None, minutes: int = 5, message_id: Optional[str] = None, thread: bool = True, extra: Optional[dict] = None) -> str:
        """The vendor at ``address`` answers the newest message sent to them. Returns the new Message-ID."""
        sent = self.last_to(address)
        self._n += 1
        mid = message_id or f"<vendor-{self._n}@vendor.test>"
        m = EmailMessage()
        m["From"], m["To"], m["Subject"] = sender or address, CFG.from_addr, "Re: " + str(sent["Subject"])
        m["Date"] = email.utils.format_datetime(self.clock() + timedelta(minutes=minutes))
        m["Message-ID"] = mid
        if thread:
            m["In-Reply-To"] = str(sent["Message-ID"])
        for k, v in (extra or {}).items():
            m[k] = v
        m.set_content(body)
        self.inbox.append(m.as_bytes())
        return mid


def _dsn(world, address, *, permanent=True, original_message_id=None, status=None, action=None, with_original="headers", sender="MAILER-DAEMON@googlemail.com", diagnostic="smtp; 550 {status} user unknown"):
    world._n += 1
    sent = [m for m in world.outbox if m["To"] == address]
    orig = original_message_id if original_message_id is not None else (str(sent[-1]["Message-ID"]) if sent else "<unknown@elsewhere.test>")
    id_line = f"Message-ID: {orig}\n" if orig else ""
    status = status or ("5.1.1" if permanent else "4.2.2")
    action = action or ("failed" if permanent else "delayed")
    original = (
        f"--B\nContent-Type: text/rfc822-headers\n\n{id_line}From: {CFG.from_addr}\nTo: {address}\nSubject: Quote request\n\n" if with_original == "headers"
        else f"--B\nContent-Type: message/rfc822\n\nMessage-ID: {orig}\nFrom: {CFG.from_addr}\nTo: {address}\nSubject: Quote request\n\nbody\n\n" if with_original == "message" else ""
    )
    raw = (
        f"From: {sender}\nTo: {CFG.from_addr}\nSubject: Delivery Status Notification (Failure)\nDate: {email.utils.format_datetime(world.clock() + timedelta(minutes=2))}\n"
        f"Message-ID: <dsn-{world._n}@googlemail.com>\nMIME-Version: 1.0\nContent-Type: multipart/report; report-type=delivery-status; boundary=\"B\"\n\n"
        f"--B\nContent-Type: text/plain; charset=utf-8\n\nYour message could not be delivered.\n\n"
        f"--B\nContent-Type: message/delivery-status\n\nReporting-MTA: dns; googlemail.com\n\nFinal-Recipient: rfc822; {address}\nAction: {action}\nStatus: {status}\nDiagnostic-Code: {diagnostic.format(status=status)}\n\n"
        f"{original}--B--\n"
    )
    world.inbox.append(raw.encode())
    return orig


MailWorld.bounce = lambda self, address, **kw: _dsn(self, address, **kw)


def disconnected():
    return smtplib.SMTPServerDisconnected("connection lost during send")


__all__ = ["CFG", "MailWorld", "disconnected", "datetime"]
