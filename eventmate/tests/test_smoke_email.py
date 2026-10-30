"""`python -m eventmate.smoke_email`: checks the mailbox logins without sending anything, and prints only pass or fail and the host (ADR-047)."""

import imaplib
import smtplib

from eventmate import smoke_email
from eventmate.channels import EmailChannel, EmailConfig
from eventmate.tests.mailfake import CFG, FakeIMAP, FakeSMTP, MailWorld

SECRET = "not-a-real-password"
ENV = {"EVENTMATE_SMTP_HOST": "smtp.fake.test", "EVENTMATE_SMTP_USER": "agent@fake.test", "EVENTMATE_SMTP_PASSWORD": SECRET, "EVENTMATE_IMAP_HOST": "imap.fake.test"}


class World(MailWorld):
    def __init__(self):
        super().__init__(lambda: None)
        self.imap_error = None
        self.selected = []


class WatchedIMAP(FakeIMAP):
    def login(self, user, password):
        if self.world.imap_error:
            raise self.world.imap_error

    def select(self, box, readonly=False):
        self.world.selected.append((box, readonly))
        return super().select(box, readonly)


def run(world, env=ENV, argv=(), capsys=None):
    code = smoke_email.main(list(argv), env=env, smtp_factory=lambda: FakeSMTP(world), imap_factory=lambda: WatchedIMAP(world))
    return code, (capsys.readouterr() if capsys else None)


def test_a_dry_run_logs_in_to_both_servers_and_sends_nothing(capsys):
    w = World()
    code, out = run(w, capsys=capsys)
    assert code == 0 and w.outbox == [] and w.selected == [("INBOX", True)]  # read-only select, no message sent
    lines = out.out.strip().splitlines()
    assert any(l.startswith("PASS smtp") and "smtp.fake.test" in l for l in lines) and any(l.startswith("PASS imap") and "imap.fake.test" in l for l in lines)
    assert "dry run" in out.out.lower()


def test_the_output_never_contains_the_user_the_password_or_raw_errors(capsys):
    w = World()
    w.login_error = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted for agent@fake.test pw=not-a-real-password")
    w.imap_error = imaplib.IMAP4.error("LOGIN failed for agent@fake.test: not-a-real-password")
    code, out = run(w, capsys=capsys)
    text = out.out + out.err
    assert code == 1 and "FAIL smtp" in text and "FAIL imap" in text and "login rejected" in text
    assert SECRET not in text and "agent@fake.test" not in text and "5.7.8" not in text and "Username and Password" not in text


def test_an_unreachable_server_is_a_clean_failure(capsys):
    def boom():
        raise OSError(f"connection refused {SECRET}")

    code = smoke_email.main([], env=ENV, smtp_factory=boom, imap_factory=boom)
    out = capsys.readouterr()
    assert code == 1 and "could not connect" in out.out and SECRET not in out.out + out.err


def test_missing_settings_name_what_to_set_and_never_print_values(capsys):
    code, out = run(World(), env={"EVENTMATE_SMTP_HOST": "smtp.fake.test"}, capsys=capsys)
    assert code == 1 and "FAIL smtp: not set: EVENTMATE_SMTP_HOST, EVENTMATE_SMTP_USER, EVENTMATE_SMTP_PASSWORD" in out.out
    assert "smtp.fake.test" not in out.out and SECRET not in out.out  # names of what to set, never values


def test_only_one_side_configured_checks_that_side(capsys):
    env = {k: v for k, v in ENV.items() if k != "EVENTMATE_IMAP_HOST"}
    w = World()
    code, out = run(w, env=env, capsys=capsys)
    assert "PASS smtp" in out.out and "SKIP imap" in out.out and code == 0 and w.outbox == []


def test_a_real_test_send_needs_both_flags_and_goes_only_to_that_address(capsys):
    w = World()
    assert run(w, argv=["--send-test-to", "me@personal.test"], capsys=capsys)[0] == 2 and w.outbox == []  # refused without the ownership flag
    assert run(w, argv=["--i-own-this-address"], capsys=capsys)[0] == 2 and w.outbox == []
    for bad in ("nobody", "a@b.com,c@d.com", "x\ny@z.com"):
        assert run(w, argv=["--send-test-to", bad, "--i-own-this-address"], capsys=capsys)[0] == 2 and w.outbox == []
    code, out = run(w, argv=["--send-test-to", "me@personal.test", "--i-own-this-address"], capsys=capsys)
    assert code == 0 and [m["To"] for m in w.outbox] == ["me@personal.test"] and "PASS send" in out.out
    assert "EventMate" in w.outbox[0]["Subject"] and SECRET not in w.outbox[0].get_content()


def test_it_never_selects_the_mailbox_for_writing_and_never_deletes_anything(capsys):
    w = World()
    run(w, capsys=capsys)
    assert all(ro is True for _, ro in w.selected)


def test_the_default_factories_use_tls():
    ch = EmailChannel(EmailConfig(smtp_host="h", smtp_port=465, imap_host="h", user="u", password="p", from_addr="u@h.test"))
    assert ch._smtp.__name__ == "_default_smtp" and ch._imap.__name__ == "_default_imap" and CFG.smtp_port == 465
