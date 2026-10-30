"""Channels (ADR-042): email over SMTP/IMAP and WhatsApp through WAHA, with fake transports. No test touches the network."""

import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest

from eventmate.messaging.channels import (
    ChannelError, EmailChannel, EmailConfig, KnownThread, OutcomeUnknown, ThreadRef, WahaConfig, WhatsAppChannel, clean_reply_text, channels_from_env,
)

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
CFG = EmailConfig(smtp_host="smtp.x.test", imap_host="imap.x.test", user="me@x.test", password="s3cret-pass", from_addr="me@x.test")


class FakeSMTP:
    def __init__(self, login_error=None, send_error=None):
        self.sent, self.login_error, self.send_error, self.closed = [], login_error, send_error, False

    def login(self, user, password):
        if self.login_error:
            raise self.login_error

    def send_message(self, msg):
        if self.send_error:
            raise self.send_error
        self.sent.append(msg)

    def quit(self):
        self.closed = True


def mail_channel(smtp=None, imap=None):
    return EmailChannel(CFG, smtp_factory=lambda: smtp, imap_factory=lambda: imap, now=lambda: NOW)


def test_status_never_contains_a_secret_and_names_what_is_missing():
    assert EmailChannel(EmailConfig()).status().can_send is False and "EVENTMATE_SMTP" in EmailChannel(EmailConfig()).status().detail
    st = mail_channel().status()
    assert st.can_send and st.can_receive and "s3cret" not in repr(CFG) and "s3cret" not in st.model_dump_json()


def test_email_send_builds_a_plain_message_with_a_message_id():
    smtp = FakeSMTP()
    mid = mail_channel(smtp).send("sales@v.test", "Quote request", "Hello\nbody")
    m = smtp.sent[0]
    assert m["To"] == "sales@v.test" and m["From"] == "me@x.test" and m["Message-ID"] == mid and mid.startswith("<") and smtp.closed
    assert "Hello" in m.get_content()


def test_a_reply_carries_threading_headers():
    smtp = FakeSMTP()
    mail_channel(smtp).send("sales@v.test", "Re: Quote", "More", ThreadRef(in_reply_to="<a@x>", references="<z@x>"))
    assert smtp.sent[0]["In-Reply-To"] == "<a@x>" and smtp.sent[0]["References"] == "<z@x> <a@x>"


@pytest.mark.parametrize("subject,to", [("Hi\nBcc: evil@x.test", "a@v.test"), ("Hi", "a@v.test\nBcc: evil@x.test")])
def test_header_injection_is_refused_before_anything_is_sent(subject, to):
    smtp = FakeSMTP()
    with pytest.raises(ChannelError):
        mail_channel(smtp).send(to, subject, "body")
    assert smtp.sent == []


def test_login_and_refusals_are_definite_failures_but_a_broken_connection_is_unknown():
    with pytest.raises(ChannelError, match="rejected the login") as e:
        mail_channel(FakeSMTP(login_error=smtplib.SMTPAuthenticationError(535, b"no"))).send("a@v.test", "s", "b")
    assert not isinstance(e.value, OutcomeUnknown)
    with pytest.raises(ChannelError, match="refused the address") as e:
        mail_channel(FakeSMTP(send_error=smtplib.SMTPRecipientsRefused({"a@v.test": (550, b"x")}))).send("a@v.test", "s", "b")
    assert not isinstance(e.value, OutcomeUnknown)
    with pytest.raises(OutcomeUnknown):
        mail_channel(FakeSMTP(send_error=smtplib.SMTPServerDisconnected("gone"))).send("a@v.test", "s", "b")
    with pytest.raises(OutcomeUnknown):
        mail_channel(FakeSMTP(send_error=TimeoutError())).send("a@v.test", "s", "b")


def test_unreachable_server_is_a_definite_failure_and_does_not_leak_the_password():
    def boom():
        raise OSError("connection refused for s3cret-pass")

    with pytest.raises(ChannelError) as e:
        EmailChannel(CFG, smtp_factory=boom).send("a@v.test", "s", "b")
    assert "s3cret" not in str(e.value) and not isinstance(e.value, OutcomeUnknown)


def test_unconfigured_email_refuses():
    with pytest.raises(ChannelError, match="not configured"):
        EmailChannel(EmailConfig()).send("a@v.test", "s", "b")


# ---- reading replies

def raw_mail(sender="Vendor <sales@v.test>", body="Rs 650 per plate.\n\nOn Fri, Oct 30 you wrote:\n> Hello", subject="Re: Quote", extra=None, html=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = sender, "me@x.test", subject, "Fri, 30 Oct 2026 10:00:00 +0000", "<r1@v.test>"
    m["In-Reply-To"] = "<sent1@x.test>"
    for k, v in (extra or {}).items():
        m[k] = v
    if html:
        m.set_content("x")
        m.clear_content()
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, messages, login_error=None):
        self.messages, self.login_error, self.searched = messages, login_error, None

    def login(self, u, p):
        if self.login_error:
            import imaplib
            raise imaplib.IMAP4.error("bad login")

    def select(self, box, readonly=False):
        assert readonly  # polling must never change the mailbox
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.searched = criteria
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, num, spec):
        assert "PEEK" in spec  # reading must not mark mail as seen
        return "OK", [(b"1 (BODY[] {n}", self.messages[int(num) - 1]), b")"]

    def logout(self):
        pass


T = [KnownThread("d1", "sales@v.test", NOW - timedelta(hours=2), "<sent1@x.test>")]


def test_poll_returns_the_new_text_only_with_threading_headers():
    out = mail_channel(imap=FakeIMAP([raw_mail()])).poll(T)
    assert len(out) == 1 and out[0].sender == "sales@v.test" and out[0].in_reply_to == "<sent1@x.test>" and out[0].external_id == "<r1@v.test>"
    assert out[0].body == "Rs 650 per plate." and "wrote" not in out[0].body and out[0].received_at == datetime(2026, 10, 30, 10, 0, tzinfo=timezone.utc)


def test_poll_searches_by_date_and_does_nothing_without_threads():
    imap = FakeIMAP([raw_mail()])
    mail_channel(imap=imap).poll(T)
    assert imap.searched == ("SINCE", "29-Oct-2026")
    assert mail_channel(imap=None).poll([]) == []


def test_robots_mailing_lists_and_our_own_mail_are_skipped():
    msgs = [raw_mail(extra={"Auto-Submitted": "auto-replied"}), raw_mail(extra={"Precedence": "bulk"}), raw_mail(extra={"List-Id": "<l.x>"}), raw_mail(sender="me@x.test")]
    assert mail_channel(imap=FakeIMAP(msgs)).poll(T) == []


def test_html_only_mail_is_reduced_to_text_without_scripts():
    out = mail_channel(imap=FakeIMAP([raw_mail(html="<p>Total <b>Rs 700</b></p><script>alert(1)</script>")])).poll(T)
    assert "Rs 700" in out[0].body and "alert" not in out[0].body and "<" not in out[0].body


def test_one_malformed_message_does_not_stop_the_rest():
    out = mail_channel(imap=FakeIMAP([b"\x00\xff garbage", raw_mail()])).poll(T)
    assert [o.body for o in out][-1] == "Rs 650 per plate."


def test_imap_login_failure_is_a_clean_error():
    with pytest.raises(ChannelError, match="rejected"):
        mail_channel(imap=FakeIMAP([], login_error=True)).poll(T)


def test_reply_text_cleaning():
    text, cut = clean_reply_text("Yes we can.\r\n> quoted\r\n\x1b[31mRs 5\x00 00\n\n\n\nBye\nOn Mon, X wrote:\nold stuff")
    assert text == "Yes we can.\n[31mRs 5 00\n\nBye"  # the escape byte is gone; its printable tail is just text
    assert "old stuff" not in text and "quoted" not in text and "\x00" not in text and "\x1b" not in text and not cut
    big, cut = clean_reply_text("x" * 10_000)
    assert len(big) == 4000 and cut
    assert clean_reply_text("On Mon, A wrote:\nonly quoted")[0].startswith("On Mon")  # a reply that starts with the quote marker is kept, not emptied


# ---- WhatsApp through WAHA

class FakeWaha:
    def __init__(self, exists=True, messages=None, send_status=201, send_body=None):
        self.calls, self.exists, self.messages, self.send_status = [], exists, messages or [], send_status
        self.send_body = {"id": "true_919876543210@c.us_AAA"} if send_body is None else send_body

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        if "/api/checkNumberStatus" in url:
            return 200, json.dumps({"numberExists": self.exists, "chatId": "919876543210@c.us" if self.exists else None}).encode()
        if url.split("?")[0].endswith("/api/sendText"):
            return self.send_status, json.dumps(self.send_body).encode()
        if "/messages" in url:
            return 200, json.dumps(self.messages).encode()
        return 404, b"{}"


def wa(fake, url="http://localhost:3000", key="k-123"):
    return WhatsAppChannel(WahaConfig(url=url, api_key=key, session="default"), http_fn=fake, now=lambda: NOW)


def test_whatsapp_checks_the_number_then_sends_with_the_api_key():
    fake = FakeWaha()
    mid = wa(fake).send("919876543210", "", "Hello vendor")
    assert mid == "true_919876543210@c.us_AAA"
    (m1, u1, h1, _), (m2, u2, h2, body) = fake.calls
    assert "checkNumberStatus" in u1 and h2["X-Api-Key"] == "k-123" and body == {"session": "default", "chatId": "919876543210@c.us", "text": "Hello vendor", "linkPreview": False}


def test_a_number_not_on_whatsapp_is_refused_and_nothing_is_sent():
    fake = FakeWaha(exists=False)
    with pytest.raises(ChannelError, match="not on WhatsApp"):
        wa(fake).send("919876543210", "", "Hello")
    assert not any(u.split("?")[0].endswith("sendText") for _, u, _, _ in fake.calls)


def test_whatsapp_gateway_errors_are_classified():
    with pytest.raises(ChannelError, match="API key"):
        wa(lambda *a: (401, b"{}")).send("919876543210", "", "x")

    def down(*a):
        raise ConnectionRefusedError()

    with pytest.raises(OutcomeUnknown):
        wa(down).send("919876543210", "", "x")
    with pytest.raises(OutcomeUnknown, match="no message id"):
        wa(FakeWaha(send_body={})).send("919876543210", "", "x")
    with pytest.raises(ChannelError, match="HTTP 500"):
        wa(FakeWaha(send_status=500)).send("919876543210", "", "x")
    with pytest.raises(ChannelError, match="not JSON"):
        wa(lambda m, u, h, b: (200, b"<html>")).send("919876543210", "", "x")


@pytest.mark.parametrize("url", ["", "ftp://x", "http://user:pw@localhost:3000", "localhost:3000", "javascript:alert(1)"])
def test_a_bad_gateway_url_is_unconfigured(url):
    ch = wa(FakeWaha(), url=url)
    assert not ch.status().can_send
    with pytest.raises(ChannelError):
        ch.send("919876543210", "", "x")


def test_whatsapp_poll_returns_only_vendor_messages_after_the_request():
    ts = int(NOW.timestamp())
    msgs = [
        {"id": "m0", "timestamp": ts - 7200, "fromMe": False, "body": "old chat"},  # before we wrote
        {"id": "m1", "timestamp": ts + 60, "fromMe": True, "body": "our own"},
        {"id": "m2", "timestamp": ts + 120, "fromMe": False, "body": "Rs 700 per plate\n> quoted"},
        {"id": 7, "timestamp": ts + 120, "fromMe": False, "body": "bad id type"},
        {"id": "m3", "timestamp": "x", "fromMe": False, "body": "bad ts"},
        "junk",
    ]
    out = wa(FakeWaha(messages=msgs)).poll([KnownThread("d1", "919876543210", NOW, "true_x")])
    assert [(o.external_id, o.body, o.sender) for o in out] == [("m2", "Rs 700 per plate", "919876543210")]


def test_whatsapp_poll_rejects_a_non_list_answer():
    fake = FakeWaha(messages={"error": "x"})
    with pytest.raises(ChannelError, match="unexpected"):
        wa(fake).poll([KnownThread("d1", "919876543210", NOW, "x")])


def test_channels_from_env_reads_names_only_and_repr_hides_secrets():
    chans = channels_from_env({"EVENTMATE_WAHA_URL": "http://localhost:3000/", "EVENTMATE_WAHA_API_KEY": "topsecret", "EVENTMATE_SMTP_PORT": "99999999"})
    assert chans["whatsapp"].status().can_send and not chans["email"].status().can_send
    assert "topsecret" not in repr(chans["whatsapp"].config) and chans["email"].config.smtp_port == 465
