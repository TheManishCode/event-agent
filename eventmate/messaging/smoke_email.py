"""Check the mailbox settings without sending anything: ``python -m eventmate.messaging.smoke_email`` (ADR-047).

Default is a dry run: log in to SMTP and to IMAP (read-only select of the inbox) and say PASS or FAIL with the host. It prints no user name, no
password and no raw server message, only a short category. A real one-message test send is possible only to an address you name, with
``--send-test-to ADDRESS --i-own-this-address`` together.

Reads the same settings as the app (`.env` or the environment): EVENTMATE_SMTP_HOST/PORT/USER/PASSWORD, EVENTMATE_IMAP_HOST/PORT.
"""

from __future__ import annotations

import argparse
import imaplib
import os
import smtplib
import ssl
import sys
from typing import Callable, List, Mapping, Optional, Tuple

from eventmate.messaging.channels import ChannelError, EmailChannel, EmailConfig
from eventmate.env import load_env
from eventmate.schemas import _EMAIL

Row = Tuple[str, str, str]  # (status, name, text)


def _category(exc: BaseException) -> str:
    if isinstance(exc, (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error)):
        return "login rejected"
    if isinstance(exc, (smtplib.SMTPException, OSError, ssl.SSLError, TimeoutError)):
        return "could not connect"
    return "failed"


def check_smtp(cfg: EmailConfig, factory: Callable) -> Row:
    if not (cfg.smtp_host and cfg.user and cfg.password):
        return ("FAIL", "smtp", "not set: EVENTMATE_SMTP_HOST, EVENTMATE_SMTP_USER, EVENTMATE_SMTP_PASSWORD")
    try:
        server = factory()
        try:
            server.login(cfg.user, cfg.password)
            noop = getattr(server, "noop", None)
            if noop:
                noop()
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except Exception as exc:  # never show the server's own words: they can echo the user name or password
        return ("FAIL", "smtp", f"{_category(exc)} ({cfg.smtp_host})")
    return ("PASS", "smtp", f"logged in ({cfg.smtp_host}); nothing sent")


def check_imap(cfg: EmailConfig, factory: Callable) -> Row:
    if not cfg.imap_host:
        return ("SKIP", "imap", "EVENTMATE_IMAP_HOST is not set, so replies cannot be read")
    if not (cfg.user and cfg.password):
        return ("FAIL", "imap", "not set: EVENTMATE_SMTP_USER, EVENTMATE_SMTP_PASSWORD")
    try:
        box = factory()
        try:
            box.login(cfg.user, cfg.password)
            box.select("INBOX", readonly=True)  # read-only: nothing is marked seen or deleted
        finally:
            try:
                box.logout()
            except Exception:
                pass
    except Exception as exc:
        return ("FAIL", "imap", f"{_category(exc)} ({cfg.imap_host})")
    return ("PASS", "imap", f"logged in and opened the inbox read-only ({cfg.imap_host})")


def main(argv: Optional[List[str]] = None, env: Optional[Mapping[str, str]] = None, smtp_factory: Optional[Callable] = None, imap_factory: Optional[Callable] = None) -> int:
    ap = argparse.ArgumentParser(prog="eventmate.messaging.smoke_email")
    ap.add_argument("--send-test-to", metavar="ADDRESS", help="also send one real test message to this address (it must be yours)")
    ap.add_argument("--i-own-this-address", action="store_true", help="required with --send-test-to")
    a = ap.parse_args(argv)
    if bool(a.send_test_to) != bool(a.i_own_this_address):
        print("a test send needs both --send-test-to ADDRESS and --i-own-this-address", file=sys.stderr)
        return 2
    if a.send_test_to and not _EMAIL.fullmatch(a.send_test_to.strip()):
        print("--send-test-to needs one plain email address", file=sys.stderr)
        return 2
    if env is None:
        load_env()
        env = os.environ
    cfg = EmailConfig.from_env(env)
    channel = EmailChannel(cfg)
    rows = [check_smtp(cfg, smtp_factory or channel._smtp), check_imap(cfg, imap_factory or channel._imap)]
    if a.send_test_to and rows[0][0] == "PASS":
        try:
            sender = EmailChannel(cfg, smtp_factory=smtp_factory) if smtp_factory else channel
            sender.send(a.send_test_to.strip(), "EventMate mail test", "This is a test message from EventMate's mail check. Nothing else was sent.", None)
            rows.append(("PASS", "send", f"one test message sent to the address you named ({cfg.smtp_host})"))
        except ChannelError:
            rows.append(("FAIL", "send", "the server refused or dropped the message"))
    for status, name, text in rows:
        print(f"{status} {name}: {text}")
    if not a.send_test_to:
        print("dry run: nothing was sent. Add --send-test-to ADDRESS --i-own-this-address for one real test message.")
    return 0 if all(r[0] in ("PASS", "SKIP") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
